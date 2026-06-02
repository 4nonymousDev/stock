"""选股策略本地存储（SQLite）。

策略以「名字 -> 模板」键值对保存在项目根目录的 strategies.db。
模板保留 {T}/{T1}/{T2}/{T3} 占位符（运行时由引擎替换为中文日期），
因此存的是「带占位符的原始模板」，而非某一天展开后的语句。

首次使用自动建表并以 core.DEFAULT_STRATEGY_TEMPLATE 播种「默认策略」。
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sqlite3
import threading
from typing import Optional

from core import DEFAULT_STRATEGY_TEMPLATE

DB_PATH = pathlib.Path(__file__).parent / "strategies.db"
DEFAULT_NAME = "默认策略"

_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """建表，并让「默认策略」始终与代码 DEFAULT_STRATEGY_TEMPLATE 同步。

    默认策略以代码为唯一事实来源：每次启动 upsert 同步，改默认只改代码即可。
    需要个性化的请另存为新名字（默认策略受删除保护、会被覆盖回代码值）。
    """
    with _lock, _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS strategies (
                name       TEXT PRIMARY KEY,
                template   TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO strategies(name, template, updated_at) VALUES(?,?,?)
            ON CONFLICT(name) DO UPDATE SET
                template=excluded.template, updated_at=excluded.updated_at
            """,
            (DEFAULT_NAME, DEFAULT_STRATEGY_TEMPLATE, _now()),
        )


def list_strategies() -> list[dict]:
    """返回全部策略（默认策略置顶，其余按名称排序）。"""
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT name, template, updated_at FROM strategies"
        ).fetchall()
    out = [dict(r) for r in rows]
    out.sort(key=lambda r: (r["name"] != DEFAULT_NAME, r["name"]))
    return out


def get_strategy(name: str) -> Optional[dict]:
    with _lock, _connect() as conn:
        row = conn.execute(
            "SELECT name, template, updated_at FROM strategies WHERE name=?", (name,)
        ).fetchone()
    return dict(row) if row else None


def save_strategy(name: str, template: str) -> dict:
    """新增或更新（upsert）一条策略，返回保存后的记录。"""
    name = (name or "").strip()
    template = (template or "").strip()
    if not name:
        raise ValueError("策略名称不能为空")
    if not template:
        raise ValueError("策略内容不能为空")
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO strategies(name, template, updated_at) VALUES(?,?,?)
            ON CONFLICT(name) DO UPDATE SET
                template=excluded.template, updated_at=excluded.updated_at
            """,
            (name, template, _now()),
        )
    return {"name": name, "template": template}


def delete_strategy(name: str) -> None:
    """删除策略；默认策略不允许删除。"""
    if name == DEFAULT_NAME:
        raise ValueError("默认策略不可删除")
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM strategies WHERE name=?", (name,))


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")
