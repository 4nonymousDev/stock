"""选股策略回测 - FastAPI 后端（BS 架构服务端）。

运行：uv run uvicorn app:app --host 0.0.0.0 --port 8000
浏览器访问 http://localhost:8000
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import pathlib

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import load_account_ops
from core import DEFAULT_STRATEGY_TEMPLATE, BacktestEngine, DayResult

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("backtest.app")

BASE_DIR = pathlib.Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="选股策略回测")
engine = BacktestEngine(load_account_ops())


@app.on_event("shutdown")
def _shutdown() -> None:
    engine.close()


class BacktestRequest(BaseModel):
    start_date: str                       # YYYY-MM-DD
    end_date: str                         # YYYY-MM-DD
    template: str | None = None


@app.get("/api/template")
def get_template() -> dict:
    return {"template": DEFAULT_STRATEGY_TEMPLATE}


def _day_to_row(day: DayResult) -> dict:
    return {
        "date": day.date,
        "error": day.error,
        "query": day.query,
        "raw_response": day.raw_response,
        "stocks": [
            {
                "code": s.code,
                "name": s.name,
                "base_price": s.base_price,
                "forwards": s.forwards,
                "lift": s.lift,
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
    template = req.template or DEFAULT_STRATEGY_TEMPLATE
    try:
        results = engine.backtest(start, end, template)
    except Exception as e:  # noqa: BLE001
        logger.exception("回测失败")
        raise HTTPException(500, f"回测失败: {e}")
    return {"rows": [_day_to_row(day) for day in results]}


@app.post("/api/backtest/stream")
def run_backtest_stream(req: BacktestRequest) -> StreamingResponse:
    """流式回测：逐个交易日推送 NDJSON 进度事件，供前端进度条与增量渲染使用。"""
    start, end = _parse_range(req)
    template = req.template or DEFAULT_STRATEGY_TEMPLATE

    def gen():
        try:
            for ev in engine.backtest_iter(start, end, template):
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
