"""SQLite 引擎构造：WAL + FULL 同步确保持久提交，busy_timeout 吸收写锁等待。

所有连接以 DBAPI 自动提交模式运行（isolation_level=None），事务边界完全由
service 层显式的 BEGIN IMMEDIATE / COMMIT / ROLLBACK 控制，避免驱动隐式
BEGIN 造成的升级死锁。
"""
from __future__ import annotations

import os

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool

from .models import Base, Operation


def _migrate_operation_identity(engine: Engine) -> None:
    with engine.connect() as conn:
        columns = conn.exec_driver_sql("PRAGMA table_info(operations)").mappings().all()
        if not columns or any(column["name"] == "id" for column in columns):
            return

        conn.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            conn.exec_driver_sql(
                """CREATE TEMPORARY TABLE operations_identity_backup AS
                   SELECT client_op_id, scene_id, notes, payload_hash,
                          shot_number, created_at
                   FROM operations"""
            )
            conn.exec_driver_sql("DROP TABLE operations")
            Operation.__table__.create(conn)
            conn.exec_driver_sql(
                """INSERT INTO operations
                   (client_op_id, scene_id, notes, payload_hash, shot_number, created_at)
                   SELECT client_op_id, scene_id, notes, payload_hash,
                          shot_number, created_at
                   FROM operations_identity_backup"""
            )
            conn.exec_driver_sql("DROP TABLE operations_identity_backup")
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
    _migrate_operation_identity(engine)
    return engine
