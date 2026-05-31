"""选股策略回测核心逻辑。

依赖 thsdk 的 wencai_nlp 进行选股，klines 取行情，
以上证指数 K 线作为交易日历（天然排除周末/节假日/临时休市）。
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd
from thsdk import THS

logger = logging.getLogger("backtest.core")

# 上证指数，作为交易日历来源（指数不会停牌，比单只股票可靠）
TRADING_CALENDAR_CODE = "USHI1A0001"

# klines 官方限频 20ms，留足余量
_KLINE_INTERVAL = 0.05

# 计算拉升资金需 EMA89 预热，单只股票往前多取的自然日数（约 270 个交易日）
_LIFT_LOOKBACK_DAYS = 400

# 问财返回的代码后缀 -> klines 所需的市场前缀
_SUFFIX_TO_PREFIX = {
    "SH": "USHA",   # 上海 A 股
    "SZ": "USZA",   # 深圳 A 股
    "BJ": "USTM",   # 北交所
}

# ---------------------------------------------------------------------------
# 策略模板：{T} 选股日，{T1}/{T2}/{T3} 为 T 往前推的第 1/2/3 个交易日
# 日期占位符在运行时替换为 “YYYY年M月D日”。可由前端覆盖。
# ---------------------------------------------------------------------------
DEFAULT_STRATEGY_TEMPLATE = (
    "剔除ST，只看主板和创业板，流通市值高于60亿且低于500亿，"
    "{T1}均线角度大于60，{T}竞价异动，竞价金额大于1000万。"
    "{T1}均线角度大于60，{T1}均线角度大于{T3}前均线角度，"
    "{T}均线角度大于{T2}均线角度，归属于上市公司股东的净利润同期增长大于0%。"
    "机构数大于2家。{T}高开，{T}竞价涨幅小于4%。"
    "{T}所属板块涨幅大于0.5%，{T}竞价急速上涨或竞价抢筹或大买单试盘或竞价砸盘。"
    "{T2}开盘价低于{T2}收盘价0.5%以上，{T1}开盘价低于{T1}收盘价0.5%以上"
)


def _fmt_cn(d: dt.date) -> str:
    """日期 -> 问财可识别的 “YYYY年M月D日”。"""
    return f"{d.year}年{d.month}月{d.day}日"


def convert_code(wencai_code: str) -> Optional[str]:
    """问财代码（如 000510.SZ） -> klines 代码（如 USZA000510）。"""
    if not wencai_code or "." not in wencai_code:
        return None
    num, suffix = wencai_code.split(".", 1)
    prefix = _SUFFIX_TO_PREFIX.get(suffix.upper())
    if not prefix:
        return None
    return f"{prefix}{num}"


@dataclass
class StockForward:
    """单只选股及其选股日与后 3 个交易日表现。"""
    code: str
    name: str
    base_price: Optional[float] = None        # 选股日(T)收盘价
    forwards: list[dict[str, Any]] = field(default_factory=list)  # T+1/T+2/T+3
    lift: bool = False                         # 截至 T 是否连续 3 天拉升资金增加


@dataclass
class DayResult:
    date: str                                  # 选股日 YYYY-MM-DD
    stocks: list[StockForward] = field(default_factory=list)
    error: str = ""
    query: str = ""                            # 实际发送给 wencai_nlp 的请求语句
    raw_response: Any = None                   # wencai_nlp 原始返回数据（调试用）


class BacktestEngine:
    """封装 THS 连接、交易日历与回测流程，连接复用、加锁保证线程安全。"""

    def __init__(self, ops: Optional[dict] = None):
        self._ths = THS(ops)
        self._lock = threading.Lock()
        self._connected = False
        self._calendar: list[dt.date] = []   # 升序交易日缓存

    # ----- 连接管理 -------------------------------------------------------
    def connect(self) -> None:
        with self._lock:
            if self._connected:
                return
            r = self._ths.connect()
            if not r.success:
                raise RuntimeError(f"THS 连接失败: {r.error}")
            self._connected = True
            logger.info("THS 已连接")

    def close(self) -> None:
        with self._lock:
            if self._connected:
                try:
                    self._ths.disconnect()
                finally:
                    self._connected = False

    # ----- 交易日历 -------------------------------------------------------
    def _load_calendar(self, start: dt.date, end: dt.date) -> list[dt.date]:
        """取覆盖 [start-缓冲, end+缓冲] 的交易日（升序）。"""
        # 往前留 ~15 个自然日以容纳 T-3，往后留 ~15 天容纳 T+3
        s = start - dt.timedelta(days=20)
        e = end + dt.timedelta(days=20)
        k = self._ths.klines(
            TRADING_CALENDAR_CODE,
            start_time=dt.datetime(s.year, s.month, s.day),
            end_time=dt.datetime(e.year, e.month, e.day),
            interval="day",
        )
        time.sleep(_KLINE_INTERVAL)
        if not k.success or not k.data:
            raise RuntimeError(f"获取交易日历失败: {k.error}")
        days: list[dt.date] = []
        for row in k.data:
            ts = row.get("时间")
            d = _to_date(ts)
            if d:
                days.append(d)
        days.sort()
        return days

    @staticmethod
    def _prev_trading_days(calendar: list[dt.date], t: dt.date, n: int) -> list[dt.date]:
        """返回 t 之前的第 1..n 个交易日（[T-1, T-2, ... T-n]）。"""
        idx = calendar.index(t)
        return [calendar[idx - i] for i in range(1, n + 1)]

    @staticmethod
    def _next_trading_days(calendar: list[dt.date], t: dt.date, n: int) -> list[dt.date]:
        idx = calendar.index(t)
        out = []
        for i in range(1, n + 1):
            j = idx + i
            out.append(calendar[j] if j < len(calendar) else None)
        return out

    # ----- 行情 -----------------------------------------------------------
    @staticmethod
    def _lift_increasing(closes: list[float], days: int = 3) -> bool:
        """复刻通达信“拉升资金”，判断截至最后一日是否连续 days 天增加。

        拉升资金 = IF(VAR1D>0.015, VAR1F, 0)/45，其中
        VAR1B=EMA(C,3)-EMA(C,89); VAR1C=EMA(VAR1B,21);
        VAR1D=(VAR1B-VAR1C)*10; VAR1F=VAR1D^3*0.1+VAR1D^2。
        通达信 EMA(X,N) 等价于 pandas ewm(span=N, adjust=False)。
        """
        if len(closes) < days + 1:
            return False
        c = pd.Series(closes, dtype=float)
        var1b = c.ewm(span=3, adjust=False).mean() - c.ewm(span=89, adjust=False).mean()
        var1c = var1b.ewm(span=21, adjust=False).mean()
        var1d = (var1b - var1c) * 10
        var1f = var1d ** 3 * 0.1 + var1d ** 2
        lift = var1f.where(var1d > 0.015, 0.0) / 45
        tail = lift.iloc[-(days + 1):].tolist()
        return all(tail[i] < tail[i + 1] for i in range(len(tail) - 1))

    def _stock_closes(self, kcode: str, t: dt.date,
                      fdays: list[Optional[dt.date]]
                      ) -> tuple[Optional[float], dict[dt.date, float], bool]:
        """取该股票收盘价并判断拉升资金。

        窗口从 T 往前 _LIFT_LOOKBACK_DAYS 自然日（供 EMA89 预热）到最后一个
        前向交易日。返回 (T收盘价, {日期:收盘价}, 是否连续3天拉升资金增加)。
        """
        valid = [d for d in fdays if d]
        last = max([t] + valid)
        first = t - dt.timedelta(days=_LIFT_LOOKBACK_DAYS)
        k = self._ths.klines(
            kcode,
            start_time=dt.datetime(first.year, first.month, first.day),
            end_time=dt.datetime(last.year, last.month, last.day),
            interval="day",
        )
        time.sleep(_KLINE_INTERVAL)
        closes: dict[dt.date, float] = {}
        if not k.success or not k.data:
            return None, closes, False
        rows: list[tuple[dt.date, float]] = []
        for row in k.data:
            d = _to_date(row.get("时间"))
            c = row.get("收盘价")
            if d is None or c is None:
                continue
            try:
                cf = float(c)
            except (TypeError, ValueError):
                continue
            closes[d] = cf
            rows.append((d, cf))
        rows.sort()
        hist = [c for d, c in rows if d <= t]   # 截至 T（含）的收盘价序列
        lift = self._lift_increasing(hist)
        return closes.get(t), closes, lift

    # ----- 选股 -----------------------------------------------------------
    def _select(self, query: str) -> tuple[list[dict], str]:
        resp = self._ths.wencai_nlp(query)
        if not resp.success:
            return [], resp.error
        data = resp.data
        if isinstance(data, dict):
            data = [data]
        return (data or []), ""

    # ----- 单个交易日 -----------------------------------------------------
    def _run_day(self, calendar: list[dt.date], t: dt.date,
                 template: str) -> DayResult:
        day = DayResult(date=t.isoformat())
        try:
            prev = self._prev_trading_days(calendar, t, 3)  # [T1,T2,T3]
        except ValueError:
            day.error = "交易日历缺失"
            return day
        t1, t2, t3 = prev
        query = template.format(
            T=_fmt_cn(t), T1=_fmt_cn(t1), T2=_fmt_cn(t2), T3=_fmt_cn(t3)
        )
        day.query = query
        stocks_raw, err = self._select(query)
        day.raw_response = stocks_raw
        if err:
            day.error = err
        fdays = self._next_trading_days(calendar, t, 3)  # [T+1,T+2,T+3]
        for s in stocks_raw:
            wcode = s.get("股票代码") or s.get("THSCODE") or ""
            name = s.get("股票简称") or s.get("名称") or wcode
            kcode = convert_code(wcode)
            sf = StockForward(code=wcode, name=name)
            if kcode:
                base, closes, lift = self._stock_closes(kcode, t, fdays)
                sf.base_price = base
                sf.lift = lift
                for fd in fdays:
                    entry: dict[str, Any] = {"date": fd.isoformat() if fd else None}
                    if fd and base and fd in closes:
                        price = closes[fd]
                        pct = (price / base - 1.0) * 100.0
                        entry["price"] = round(price, 2)
                        entry["pct"] = round(pct, 2)
                    else:
                        entry["price"] = None
                        entry["pct"] = None
                    sf.forwards.append(entry)
            else:
                sf.forwards = [{"date": fd.isoformat() if fd else None,
                                "price": None, "pct": None} for fd in fdays]
            day.stocks.append(sf)
        return day

    # ----- 主流程 ---------------------------------------------------------
    def backtest_iter(self, start: dt.date, end: dt.date,
                      template: str = DEFAULT_STRATEGY_TEMPLATE):
        """逐个交易日产出进度事件：

        - {"type": "start", "total": N}
        - {"type": "day", "index": i, "total": N, "day": DayResult}
        - {"type": "done"}
        """
        self.connect()
        with self._lock:
            calendar = self._load_calendar(start, end)
            self._calendar = calendar
            # 选股日 = 区间内、且 T-3 有数据的交易日
            sel_days = [d for d in calendar if start <= d <= end]
            total = len(sel_days)
            yield {"type": "start", "total": total}
            for i, t in enumerate(sel_days, 1):
                day = self._run_day(calendar, t, template)
                yield {"type": "day", "index": i, "total": total, "day": day}
            yield {"type": "done", "total": total}

    def backtest(self, start: dt.date, end: dt.date,
                 template: str = DEFAULT_STRATEGY_TEMPLATE,
                 progress=None) -> list[DayResult]:
        results: list[DayResult] = []
        for ev in self.backtest_iter(start, end, template):
            if ev["type"] == "day":
                results.append(ev["day"])
                if progress:
                    progress(ev["day"].date, len(ev["day"].stocks))
        return results


def _to_date(ts: Any) -> Optional[dt.date]:
    """klines 的 时间 字段 -> date。兼容 datetime/Timestamp/str。"""
    if ts is None:
        return None
    if isinstance(ts, dt.datetime):
        return ts.date()
    if isinstance(ts, dt.date):
        return ts
    s = str(ts).split()[0].strip()       # 去掉可能的时间部分
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:
        return dt.datetime.fromisoformat(s).date()
    except ValueError:
        return None
