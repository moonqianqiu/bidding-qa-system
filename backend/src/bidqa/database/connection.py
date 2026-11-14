"""PG 连接管理（docs/01 §4.3）：schema.sql 幂等建表 + 连接惰性建立 + 断线重连。

从 postgres.py 拆出（重构 C2）：连接/事务/建表是本模块唯一职责——业务查询在
bp_repo，会话存储在 conversations，反馈存储在 feedback；PgClient 名称退役。

**autocommit=True（2026-11-08）**：读查询不再滞留「闲置在事务中」——schema.sql
引入 ALTER TABLE（ACCESS EXCLUSIVE）后，常驻连接若持 AccessShareLock 不放，
任何进程重跑 ensure_schema 都会永久排队（实测事故：backend 读过 conversations
后 pytest 全量卡死）。单语句自动提交；多语句写路径用 conn.transaction() 保原子。
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class PgConn:
    """连接惰性建立 + 断线重连；ensure_schema 幂等（docs/01 §4.3 开发友好约定）。"""

    def __init__(self, host: str, port: int, dbname: str, user: str, password: str,
                 connect_timeout: int = 5):
        self._conninfo = (
            f"host={host} port={port} dbname={dbname} user={user} "
            f"password={password} connect_timeout={connect_timeout}"
        )
        self._conn: psycopg.Connection | None = None

    def connection(self) -> psycopg.Connection:
        if self._conn is not None and not self._conn.closed:
            return self._conn
        self._conn = psycopg.connect(self._conninfo, row_factory=dict_row,
                                     autocommit=True)
        return self._conn

    def ensure_schema(self) -> None:
        """schema.sql 单一事实来源：按分号拆分逐条幂等执行（单事务保全有或全无）。"""
        script = SCHEMA_PATH.read_text(encoding="utf-8")
        statements = [s.strip() for s in script.split(";")
                      if s.strip() and not all(
                          line.strip().startswith("--") or not line.strip()
                          for line in s.strip().splitlines())]
        conn = self.connection()
        with conn.transaction():  # autocommit 连接下显式包事务：DDL 全有或全无
            for stmt in statements:
                conn.execute(stmt)

    def ping(self) -> bool:
        try:
            self.connection().execute("SELECT 1")
            return True
        except psycopg.Error:
            self._conn = None
            return False

    def commit(self) -> None:
        self.connection().commit()

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()


def pg_json(value: Any) -> str:
    """PG JSONB 参数序列化（日期时间 ISO 化，其余 str 兜底）；存储模块共用。"""
    return json.dumps(value, ensure_ascii=False, default=_json_default)


def _json_default(value: Any) -> str:
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat()
    return str(value)
