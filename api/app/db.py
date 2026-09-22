"""SQLite 引擎构造：WAL + FULL 同步确保持久提交，busy_timeout 吸收写锁等待。

所有连接以 DBAPI 自动提交模式运行（isolation_level=None），事务边界完全由
service 层显式的 BEGIN IMMEDIATE / COMMIT / ROLLBACK 控制，避免驱动隐式
BEGIN 造成的升级死锁。
"""
from __future__ import annotations

import logging
import os

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

from .models import Base, Operation

logger = logging.getLogger(__name__)

# 迁移时为跨场次重复映射生成的替代标识前缀（首次提交保留原标识）
_RECOVERED_PREFIX = "recovered-op-"


def _client_op_id_globally_unique(conn, columns) -> bool:
    """operations 表是否已对 client_op_id 施加全局唯一约束（单列主键或单列唯一索引）。"""
    if [column["name"] for column in columns if column["pk"]] == ["client_op_id"]:
        return True
    for index in conn.exec_driver_sql("PRAGMA index_list(operations)").mappings().all():
        if not index["unique"]:
            continue
        indexed = [
            row["name"]
            for row in conn.exec_driver_sql(f"PRAGMA index_info({index['name']})").mappings().all()
        ]
        if indexed == ["client_op_id"]:
            return True
    return False


def _migrate_operation_identity(engine: Engine) -> None:
    """把 operations 表迁移到“client_op_id 全局唯一”的结构。

    旧版缺陷结构（唯一约束为 (scene_id, client_op_id)）可能已写入跨场次重复
    映射：同一 client_op_id 在不同场次各占一个号码。迁移以首次持久提交
    （id 最小）为权威保留该标识；后续重复行不删除——其号码可能已被现场知悉——
    而是改用 recovered-op-<id> 重新标识。场次序列与计数器原样保留，
    不产生号码缺口，也不回退计数。
    """
    with engine.connect() as conn:
        columns = conn.exec_driver_sql("PRAGMA table_info(operations)").mappings().all()
        if not columns:
            return  # 新库：create_all 已建好目标结构
        has_id = any(column["name"] == "id" for column in columns)
        if has_id and _client_op_id_globally_unique(conn, columns):
            return  # 已是目标结构

        conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            rows = conn.exec_driver_sql(
                f"SELECT * FROM operations ORDER BY {'id' if has_id else 'rowid'}"
            ).mappings().all()
            conn.exec_driver_sql("DROP TABLE operations")
            Operation.__table__.create(conn)

            seen: set[str] = set()
            duplicates = 0
            for seq, row in enumerate(rows, start=1):
                row_id = row["id"] if has_id else seq
                client_op_id = row["client_op_id"]
                if client_op_id in seen:
                    duplicates += 1
                    replacement = f"{_RECOVERED_PREFIX}{row_id}"
                    while replacement in seen:
                        replacement = f"{replacement}-x"
                    logger.warning(
                        "client_op_id %r 存在跨场次重复映射：以首次持久提交为权威，"
                        "场次 %r 的镜号 %s 改用标识 %r",
                        client_op_id,
                        row["scene_id"],
                        row["shot_number"],
                        replacement,
                    )
                    client_op_id = replacement
                seen.add(client_op_id)
                # 原样拷贝（含 created_at 原始存储值），仅替换冲突的标识
                conn.exec_driver_sql(
                    "INSERT INTO operations"
                    " (id, client_op_id, scene_id, notes, payload_hash, shot_number, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        row_id,
                        client_op_id,
                        row["scene_id"],
                        row["notes"],
                        row["payload_hash"],
                        row["shot_number"],
                        row["created_at"],
                    ),
                )
            conn.exec_driver_sql("COMMIT")
            if duplicates:
                logger.warning(
                    "operations 迁移完成：%d 条跨场次重复映射已重新标识，"
                    "各场次号码序列与计数器保持不变",
                    duplicates,
                )
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
    _migrate_operation_identity(engine)
    return engine
