"""选股策略本地存储（SQLite）。

策略以「名字 -> (wencai_clauses, local_checks)」保存在 strategies.db：
- wencai_clauses：发给问财的条件模板（含 {T}/{T1}/{T2}/{T3} 占位符）
- local_clauses：本地指标配置，存储为 JSON 字符串（结构化列表），读出时反序列化
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sqlite3
import threading
from typing import Optional

from core import DEFAULT_STRATEGY_TEMPLATE, default_local_checks

DB_PATH = pathlib.Path(__file__).parent / "strategies.db"
DEFAULT_NAME = "默认策略"

_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _deserialize(row: dict) -> dict:
    """将数据库行中的 local_clauses JSON 字符串反序列化为列表。"""
    lc = row.get("local_clauses") or "[]"
    try:
        row["local_checks"] = json.loads(lc)
    except (json.JSONDecodeError, TypeError):
        row["local_checks"] = default_local_checks()
    row.pop("local_clauses", None)
    return row


def _migrate(conn: sqlite3.Connection) -> None:
    """将旧表（含 template 列）迁移为新表（wencai_clauses + local_clauses）。"""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(strategies)")}
    if "template" not in cols:
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS strategies_new (
            name           TEXT PRIMARY KEY,
            wencai_clauses TEXT NOT NULL DEFAULT '',
            local_clauses  TEXT NOT NULL DEFAULT '[]',
            updated_at     TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO strategies_new(name, wencai_clauses, local_clauses, updated_at)
        SELECT name, template, '[]', updated_at FROM strategies
        """
    )
    conn.execute("DROP TABLE strategies")
    conn.execute("ALTER TABLE strategies_new RENAME TO strategies")


def init_db() -> None:
    """建表（或迁移旧表），并让「默认策略」始终与代码中的默认值同步。"""
    default_lc = json.dumps(default_local_checks(), ensure_ascii=False)
    with _lock, _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS strategies (
                name           TEXT PRIMARY KEY,
                wencai_clauses TEXT NOT NULL DEFAULT '',
                local_clauses  TEXT NOT NULL DEFAULT '[]',
                updated_at     TEXT NOT NULL
            )
            """
        )
        _migrate(conn)
        conn.execute(
            """
            INSERT INTO strategies(name, wencai_clauses, local_clauses, updated_at) VALUES(?,?,?,?)
            ON CONFLICT(name) DO UPDATE SET
                wencai_clauses=excluded.wencai_clauses,
                local_clauses=excluded.local_clauses,
                updated_at=excluded.updated_at
            """,
            (DEFAULT_NAME, DEFAULT_STRATEGY_TEMPLATE, default_lc, _now()),
        )


def list_strategies() -> list[dict]:
    """返回全部策略（默认策略置顶，其余按名称排序）。local_checks 已反序列化为列表。"""
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT name, wencai_clauses, local_clauses, updated_at FROM strategies"
        ).fetchall()
    out = [_deserialize(dict(r)) for r in rows]
    out.sort(key=lambda r: (r["name"] != DEFAULT_NAME, r["name"]))
    return out


def get_strategy(name: str) -> Optional[dict]:
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT name, wencai_clauses, local_clauses, updated_at FROM strategies WHERE name=?",
            (name,),
        ).fetchone()
    return _deserialize(dict(row)) if row else None


def save_strategy(name: str, wencai_clauses: str, local_clauses: str) -> dict:
    """新增或更新（upsert）一条策略。local_clauses 传入 JSON 字符串。"""
    name = (name or "").strip()
    wencai_clauses = (wencai_clauses or "").strip()
    local_clauses = (local_clauses or "[]").strip()
    if not name:
        raise ValueError("策略名称不能为空")
    if not wencai_clauses:
        raise ValueError("问财条件不能为空")
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO strategies(name, wencai_clauses, local_clauses, updated_at) VALUES(?,?,?,?)
            ON CONFLICT(name) DO UPDATE SET
                wencai_clauses=excluded.wencai_clauses,
                local_clauses=excluded.local_clauses,
                updated_at=excluded.updated_at
            """,
            (name, wencai_clauses, local_clauses, _now()),
        )
    return _deserialize({"name": name, "wencai_clauses": wencai_clauses,
                         "local_clauses": local_clauses})


def delete_strategy(name: str) -> None:
    """删除策略；默认策略不允许删除。"""
    if name == DEFAULT_NAME:
        raise ValueError("默认策略不可删除")
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM strategies WHERE name=?", (name,))


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")
