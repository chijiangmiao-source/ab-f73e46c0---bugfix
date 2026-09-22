"""SQLite 引擎构造：WAL + FULL 同步确保持久提交，busy_timeout 吸收写锁等待。

所有连接以 DBAPI 自动提交模式运行（isolation_level=None），事务边界完全由
service 层显式的 BEGIN IMMEDIATE / COMMIT / ROLLBACK 控制，避免驱动隐式
BEGIN 造成的升级死锁。
"""
from __future__ import annotations

import os

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

from .models import Base, Operation


def _repair_operations_table(engine: Engine) -> None:
    """启动时自愈，覆盖两类历史数据：

    1. 旧版表结构（无 id 列，或旧的 (scene_id, client_op_id) 复合唯一约束）->
       按当前结构重建表；
    2. 已落库的跨场次重复映射（同一 client_op_id 多行，旧版本缺陷所致）->
       以最早持久提交（最小 rowid）的行为权威保留，删除其余重复行，
       再按剩余记录校正各场次计数器：权威场次计数器不回退，
       记录全部为重复行的场次移除计数器（下次从 1 重新开始），不留号码缺口。
    """
    with engine.connect() as conn:
        schema = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name='operations'")
        ).scalar()
        if schema is None:
            return  # 全新数据库，create_all 已建立最新结构

        duplicate_groups = conn.execute(
            text(
                "SELECT COUNT(*) FROM "
                "(SELECT 1 FROM operations GROUP BY client_op_id HAVING COUNT(*) > 1)"
            )
        ).scalar_one()
        if "uq_operations_client_op" in schema and duplicate_groups == 0:
            return  # 结构最新且无重复映射，无需修复

        conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            # 每组只留最早提交（最小 rowid）的一条 —— 首次持久提交即权威。
            conn.exec_driver_sql(
                """CREATE TEMPORARY TABLE operations_recover AS
                   SELECT client_op_id, scene_id, notes, payload_hash, shot_number, created_at
                   FROM operations
                   WHERE rowid IN (SELECT MIN(rowid) FROM operations GROUP BY client_op_id)"""
            )
            conn.exec_driver_sql("DROP TABLE operations")
            Operation.__table__.create(conn)
            conn.exec_driver_sql(
                """INSERT INTO operations
                   (client_op_id, scene_id, notes, payload_hash, shot_number, created_at)
                   SELECT client_op_id, scene_id, notes, payload_hash, shot_number, created_at
                   FROM operations_recover"""
            )
            conn.exec_driver_sql("DROP TABLE operations_recover")

            # 权威场次：计数器校正为 max(shot_number)+1（正常情况下与原值一致，不回退）；
            # 剩余记录为空的场次（其号码全部来自被清除的重复映射）：移除计数器，
            # 下一次发号从 1 开始，杜绝号码缺口。
            conn.execute(
                text(
                    """UPDATE scene_counters
                       SET next_number = (
                           SELECT COALESCE(MAX(shot_number), 0) + 1
                           FROM operations
                           WHERE operations.scene_id = scene_counters.scene_id
                       )
                       WHERE EXISTS (
                           SELECT 1 FROM operations
                           WHERE operations.scene_id = scene_counters.scene_id
                       )"""
                )
            )
            conn.execute(
                text(
                    """DELETE FROM scene_counters
                       WHERE NOT EXISTS (
                           SELECT 1 FROM operations
                           WHERE operations.scene_id = scene_counters.scene_id
                       )"""
                )
            )
            conn.exec_driver_sql("COMMIT")
        except Exception:
            conn.exec_driver_sql("ROLLBACK")
            raise


def make_engine(db_path: str) -> Engine:
    if db_path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path}",
        # DBAPI 自动提交；事务由 service 层显式控制
        connect_args={"check_same_thread": False, "timeout": 30, "isolation_level": None},
        poolclass=NullPool,
    )

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, _connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=FULL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    Base.metadata.create_all(engine)
    _repair_operations_table(engine)
    return engine
