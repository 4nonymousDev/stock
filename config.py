"""账户配置加载。

读取项目根目录下的 config.json（可由 config.example.json 复制而来），
环境变量 THS_USERNAME / THS_PASSWORD / THS_MAC 优先级更高，可覆盖文件配置。

未配置 username 时返回 None，引擎将以临时游客账户运行（仅供测试，可能随时失效）。
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
from typing import Optional

logger = logging.getLogger("backtest.config")

CONFIG_PATH = pathlib.Path(__file__).parent / "config.json"
_KEYS = ("username", "password", "mac")


def load_account_ops() -> Optional[dict]:
    """返回传给 THS(ops) 的账户配置；无有效账户时返回 None（游客模式）。"""
    ops: dict[str, str] = {}

    # 1) 配置文件
    if CONFIG_PATH.exists():
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            acct = raw.get("account", {}) if isinstance(raw, dict) else {}
            for k in _KEYS:
                v = acct.get(k)
                if v:
                    ops[k] = str(v).strip()
        except Exception as e:  # noqa: BLE001
            logger.warning("读取 %s 失败，忽略：%s", CONFIG_PATH.name, e)

    # 2) 环境变量覆盖
    for k in _KEYS:
        v = os.environ.get("THS_" + k.upper())
        if v:
            ops[k] = v.strip()

    if ops.get("username"):
        logger.info("使用配置账户登录：%s", ops["username"])
        return ops

    logger.info("未配置账户，使用临时游客账户（仅供测试，可能随时失效）")
    return None
