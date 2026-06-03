"""选股策略回测 - FastAPI 后端（BS 架构服务端）。

运行：uv run uvicorn app:app --host 0.0.0.0 --port 8000
浏览器访问 http://localhost:8000
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging

import os
import pathlib

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import strategies
from config import load_account_ops
from core import (DEFAULT_STRATEGY_TEMPLATE, LOCAL_INDICATORS,
                  BacktestEngine, DayResult, default_local_checks)

_log_level = logging.DEBUG if os.environ.get("LOG_LEVEL", "").upper() == "DEBUG" else logging.INFO
logging.basicConfig(level=_log_level,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
# basicConfig 在 uvicorn 已初始化日志后不会重新配置 handler，需显式覆盖级别
logging.getLogger("backtest").setLevel(_log_level)
logger = logging.getLogger("backtest.app")

BASE_DIR = pathlib.Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"

engine = BacktestEngine(load_account_ops())


@contextlib.asynccontextmanager
async def _lifespan(_app):
    strategies.init_db()
    yield
    engine.close()


app = FastAPI(title="选股策略回测", lifespan=_lifespan)


class BacktestRequest(BaseModel):
    start_date: str                        # YYYY-MM-DD
    end_date: str                          # YYYY-MM-DD
    wencai_clauses: str | None = None      # 发给问财的条件模板（含占位符）
    local_checks: list[dict] | None = None # 本地指标列表，None 则取默认值


class StrategyRequest(BaseModel):
    name: str
    wencai_clauses: str
    local_checks: list[dict] = []


def _resolve(req: BacktestRequest) -> tuple[str, list[dict]]:
    wencai = req.wencai_clauses or DEFAULT_STRATEGY_TEMPLATE
    checks = req.local_checks if req.local_checks is not None else default_local_checks()
    return wencai, checks


@app.get("/api/local-indicators")
def get_local_indicators() -> dict:
    """返回所有已注册的本地指标定义（label、params），供前端动态渲染表单。"""
    return {"indicators": LOCAL_INDICATORS}


@app.get("/api/template")
def get_template() -> dict:
    return {"wencai_clauses": DEFAULT_STRATEGY_TEMPLATE,
            "local_checks": default_local_checks()}


@app.get("/api/strategies")
def list_strategies() -> dict:
    """全部已保存策略 + 默认策略名（供前端下拉选择）。"""
    return {"strategies": strategies.list_strategies(),
            "default_name": strategies.DEFAULT_NAME}


@app.post("/api/strategies")
def save_strategy(req: StrategyRequest) -> dict:
    """新增或更新策略。"""
    try:
        saved = strategies.save_strategy(req.name, req.wencai_clauses,
                                         json.dumps(req.local_checks, ensure_ascii=False))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "strategy": saved}


@app.delete("/api/strategies/{name}")
def delete_strategy(name: str) -> dict:
    try:
        strategies.delete_strategy(name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


def _day_to_row(day: DayResult) -> dict:
    return {
        "date": day.date,
        "error": day.error,
        "notice": day.notice,
        "query": day.query,
        "raw_response": day.raw_response,
        "stocks": [
            {
                "code": s.code,
                "name": s.name,
                "base_price": s.base_price,
                "close_t": s.close_t,
                "prev_close": s.prev_close,
                "forwards": s.forwards,
                "lift": s.lift,
                "angle_t": s.angle_t,
                "angle_t2": s.angle_t2,
            }
            for s in day.stocks
        ],
    }


def _parse_range(req: BacktestRequest) -> tuple[dt.date, dt.date]:
    try:
        start = dt.date.fromisoformat(req.start_date)
        end = dt.date.fromisoformat(req.end_date)
    except ValueError:
        raise HTTPException(400, "日期格式应为 YYYY-MM-DD")
    if start > end:
        raise HTTPException(400, "开始日期不能晚于结束日期")
    return start, end


@app.post("/api/backtest")
def run_backtest(req: BacktestRequest) -> dict:
    start, end = _parse_range(req)
    wencai, checks = _resolve(req)
    try:
        results = engine.backtest(start, end, wencai, checks)
    except Exception as e:  # noqa: BLE001
        logger.exception("回测失败")
        raise HTTPException(500, f"回测失败: {e}")
    return {"rows": [_day_to_row(day) for day in results]}


@app.post("/api/backtest/stream")
def run_backtest_stream(req: BacktestRequest) -> StreamingResponse:
    """流式回测：逐个交易日推送 NDJSON 进度事件，供前端进度条与增量渲染使用。"""
    start, end = _parse_range(req)
    wencai, checks = _resolve(req)

    def gen():
        try:
            for ev in engine.backtest_iter(start, end, wencai, checks):
                if ev["type"] == "day":
                    out = {"type": "day", "index": ev["index"],
                           "total": ev["total"], "row": _day_to_row(ev["day"])}
                else:
                    out = ev
                yield json.dumps(out, ensure_ascii=False, default=str) + "\n"
        except Exception as e:  # noqa: BLE001
            logger.exception("回测失败")
            yield json.dumps({"type": "error", "message": str(e)},
                             ensure_ascii=False) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
