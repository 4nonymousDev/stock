"""选股策略回测核心逻辑。

依赖 thsdk 的 wencai_nlp 进行选股，klines 取行情，
以上证指数 K 线作为交易日历（天然排除周末/节假日/临时休市）。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd
from thsdk import THS

logger = logging.getLogger("backtest.core")


def _j(**kw) -> str:
    return json.dumps(kw, ensure_ascii=False, default=str)

# 上证指数，作为交易日历来源（指数不会停牌，比单只股票可靠）
TRADING_CALENDAR_CODE = "USHI1A0001"

# klines 官方限频 20ms，留足余量
_KLINE_INTERVAL = 0.05
# wencai_nlp 官方限频 250ms/次，留足余量
_WENCAI_INTERVAL = 0.26

# 计算拉升资金需 EMA89 预热，单只股票往前多取的自然日数（约 270 个交易日）
_LIFT_LOOKBACK_DAYS = 400

# ---------------------------------------------------------------------------
# 5 日均线角度公式（实测反推问财，30 只跨价位样本 RMSE≈0.0004°，逐股吻合）：
#   角度 = arctan(K * (MA5今 / MA5昨 - 1))      口径：前复权、涨幅归一
#   即 tan(角度) = 5 日均线单日涨幅的百分数值；MA5 单日涨 1% ⟺ 恰好 45°
# {T} 角度不走问财（问财用收盘价，含未来信息且非 9:26 状态），改由本引擎用
# {T} 当日 9:26 价（= 开盘价，竞价 9:25 已定价）替换今日收盘价后按本公式计算。
# {T1}/{T2}/{T3} 为已收盘交易日，其角度本公式与问财完全一致。
# ---------------------------------------------------------------------------
_ANGLE_MA = 5                 # 均线周期（固定 5 日）
_ANGLE_K = 100.0              # 缩放系数（实测整数 100）
_ANGLE_ADJUST = "forward"     # 角度口径：前复权（与问财一致）

# 5 日均线 vs 中轨线（通达信自定义指标，问财不认，必须本地核验）：
#   压力线 = REF(HHV(H,N),1)   即 {T} 前 N 个交易日（不含当日）的最高价
#   支撑线 = REF(LLV(L,N),1)   即 {T} 前 N 个交易日（不含当日）的最低价
#   中轨线 = (压力线 + 支撑线) / 1.9        （除数实测为 1.9，非 2）
#   条件   = MA(C,5) > 中轨线；{T} 的 MA5 用 9:26 开盘价替换今日收盘价。
_MIDLINE_N = 10               # 压力/支撑回看周期 N
_MIDLINE_DIVISOR = 1.9        # 中轨线除数
_MA_MID = 5                   # 中轨比较所用均线周期（5 日）

# ---------------------------------------------------------------------------
# 本地指标注册表
# ---------------------------------------------------------------------------
# 每条指标定义：
#   type      唯一 key，前后端共用
#   label     网页端显示的文字描述（只读，用户不可改）
#   params    参数列表，每项 {name, label, type("number"|"select"), options?, default}
#   enabled   默认是否启用
#
# 新增指标：在此列表追加一条，再在 BacktestEngine._pass_local_checks 里处理同名 type。
# ---------------------------------------------------------------------------
LOCAL_INDICATORS: list[dict] = [
    {
        "type": "angle_gt",
        "label": "{T} 均线角度大于阈值",
        "params": [{"name": "threshold", "label": "角度阈值（°）", "type": "number", "default": 70}],
        "enabled": True,
    },
    {
        "type": "midline",
        "label": "{T} 5日均线大于中轨线",
        "params": [],
        "enabled": True,
    },
]

# type -> 定义，供快速查找
_INDICATOR_MAP: dict[str, dict] = {ind["type"]: ind for ind in LOCAL_INDICATORS}


def default_local_checks() -> list[dict]:
    """返回所有默认启用的本地指标配置（含默认参数值）。"""
    result = []
    for ind in LOCAL_INDICATORS:
        if not ind["enabled"]:
            continue
        item: dict = {"type": ind["type"]}
        for p in ind["params"]:
            item[p["name"]] = p["default"]
        result.append(item)
    return result

# 问财返回的代码后缀 -> klines 所需的市场前缀
_SUFFIX_TO_PREFIX = {
    "SH": "USHA",   # 上海 A 股
    "SZ": "USZA",   # 深圳 A 股
    "BJ": "USTM",   # 北交所
}

# ---------------------------------------------------------------------------
# 策略模板：{T} 选股日，{T1}/{T2}/{T3} 为 T 往前推的第 1/2/3 个交易日
# 日期占位符在运行时替换为 “YYYY年M月D日”。可由前端覆盖、或从本地策略库选用。
#
# 注意：模板里的 {T} 实时子句（均线角度阈值/比较、5日均线大于中轨线）是 9:26
# 决策条件，不能交给问财（问财用 {T} 收盘价，既含未来信息又非 9:26 状态）。
# 引擎在送问财前自动剥离这些子句，改用 {T} 开盘价本地核验（见 _parse_local_checks /
# _strip_local_clauses / _run_day / _ma5_angle / _midline_ok）。因此这些子句可留在
# 模板中作为唯一事实来源，改阈值/口径只改模板即可。
# {T1}/{T2}/{T3} 角度仍由问财判定（已收盘、与本公式一致）。
# ---------------------------------------------------------------------------
# 发给问财的部分（不含本地核验子句）
DEFAULT_STRATEGY_TEMPLATE = (
    "剔除ST，只看主板和创业板，流通市值高于60亿且低于600亿，"
    "{T}竞价急速上涨或竞价抢筹或大买单试盘或竞价砸盘，"
    "{T1}均线角度大于{T3}前均线角度，"
    "归属于上市公司股东的净利润同期增长大于1%，"
    "{T}集合竞价涨幅小于4%，机构数大于2家，{T}高开，"
    "{T1}均线角度大于70，{T}股价高于20日均线，"
    "{T}均线角度大于{T2}前均线角度"
)

# 本地核验部分（用 9:26 开盘价计算，不送问财）
DEFAULT_LOCAL_CLAUSES = (
    "{T}均线角度大于70，"
    "{T}5日均线大于中轨线"
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
    base_price: Optional[float] = None        # 选股日(T)开盘价（=9:26 买入价=竞价价）
    close_t: Optional[float] = None            # 选股日(T)收盘价（参考）
    prev_close: Optional[float] = None         # 前一日(T-1)收盘价（昨收，竞价涨幅基准）
    forwards: list[dict[str, Any]] = field(default_factory=list)  # T+1/T+2/T+3
    lift: bool = False                         # 截至 T 是否连续 3 天拉升资金增加
    angle_t: Optional[float] = None            # {T} 9:26 均线角度（开盘价自算）
    angle_t2: Optional[float] = None           # {T2} 均线角度（收盘，对比基准）


# 单个交易日最多处理的问财候选数。过宽策略可命中上千只，每只还要拉 2 次行情
# （前复权角度窗口 + 不复权买卖价，各受 20ms 限频），不设上限会让单日耗时数分钟、
# 前端长时间无响应。超限则只处理前 N 只并给出提示，引导用户收紧策略。
_MAX_CANDIDATES_PER_DAY = 200


@dataclass
class DayResult:
    date: str                                  # 选股日 YYYY-MM-DD
    stocks: list[StockForward] = field(default_factory=list)
    error: str = ""
    notice: str = ""                           # 非致命提示（如候选超限被截断）
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
        logger.debug(_j(api="klines", code=TRADING_CALENDAR_CODE,
                        start=str(s), end=str(e), interval="day"))
        k = self._ths.klines(
            TRADING_CALENDAR_CODE,
            start_time=dt.datetime(s.year, s.month, s.day),
            end_time=dt.datetime(e.year, e.month, e.day),
            interval="day",
        )
        logger.debug(_j(api="klines", code=TRADING_CALENDAR_CODE,
                        success=k.success, rows=len(k.data) if k.data else 0,
                        error=k.error or None, sample=k.data[0] if k.data else None))
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
        """取该股票行情并判断拉升资金（不复权，反映真实可成交价）。

        窗口从 T 往前 _LIFT_LOOKBACK_DAYS 自然日（供 EMA89 预热）到最后一个
        前向交易日。返回 (T开盘价, {日期:收盘价}, 是否连续3天拉升资金增加)。
        T 开盘价即 9:26 买入价（竞价 9:25 已定价）。
        """
        valid = [d for d in fdays if d]
        last = max([t] + valid)
        first = t - dt.timedelta(days=_LIFT_LOOKBACK_DAYS)
        logger.debug(_j(api="klines", code=kcode,
                        start=str(first), end=str(last), interval="day"))
        k = self._ths.klines(
            kcode,
            start_time=dt.datetime(first.year, first.month, first.day),
            end_time=dt.datetime(last.year, last.month, last.day),
            interval="day",
        )
        logger.debug(_j(api="klines", code=kcode,
                        success=k.success, rows=len(k.data) if k.data else 0,
                        error=k.error or None, sample=k.data[0] if k.data else None))
        time.sleep(_KLINE_INTERVAL)
        closes: dict[dt.date, float] = {}
        if not k.success or not k.data:
            return None, closes, False
        rows: list[tuple[dt.date, float]] = []
        open_t: Optional[float] = None
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
            if d == t:
                try:
                    open_t = float(row.get("开盘价"))
                except (TypeError, ValueError):
                    open_t = None
        rows.sort()
        hist = [c for d, c in rows if d <= t]   # 截至 T（含）的收盘价序列
        lift = self._lift_increasing(hist)
        return open_t, closes, lift

    # ----- 5 日均线角度（本地自算，替代问财的 {T} 角度） ------------------
    def _fwd_window(self, kcode: str, t: dt.date
                    ) -> dict[dt.date, tuple[float, float, float, float]]:
        """取 t 前约 40 自然日到 t 的【前复权】日线 {日期:(开,高,低,收)}。

        覆盖 t 往前 ~27 个交易日，足够算：5 日均线角度（前 5 日收盘）、
        5 日均线（前 4 日收盘 + 今日开盘）、中轨线（前 N=10 日的最高/最低）。
        前复权口径与问财的均线角度一致。
        """
        first = t - dt.timedelta(days=40)
        logger.debug(_j(api="klines", code=kcode,
                        start=str(first), end=str(t), interval="day", adjust=_ANGLE_ADJUST))
        k = self._ths.klines(
            kcode,
            start_time=dt.datetime(first.year, first.month, first.day),
            end_time=dt.datetime(t.year, t.month, t.day),
            interval="day",
            adjust=_ANGLE_ADJUST,
        )
        logger.debug(_j(api="klines", code=kcode,
                        success=k.success, rows=len(k.data) if k.data else 0,
                        error=k.error or None, sample=k.data[0] if k.data else None))
        time.sleep(_KLINE_INTERVAL)
        out: dict[dt.date, tuple[float, float, float, float]] = {}
        if not k.success or not k.data:
            return out
        for row in k.data:
            d = _to_date(row.get("时间"))
            if d is None:
                continue
            try:
                out[d] = (float(row.get("开盘价")), float(row.get("最高价")),
                          float(row.get("最低价")), float(row.get("收盘价")))
            except (TypeError, ValueError):
                continue
        return out

    @staticmethod
    def _ma5_angle(prices: dict[dt.date, tuple[float, float, float, float]],
                   calendar: list[dt.date], day: dt.date,
                   use_open: bool) -> Optional[float]:
        """按反推公式算 day 的 5 日均线角度（度）。

        use_open=True 时用 day 的开盘价替换今日收盘价（{T} 的 9:26 口径）；
        否则用收盘价（已收盘交易日，等同问财）。
        角度 = arctan(K * (MA5今 / MA5昨 - 1))。prices 元组为 (开,高,低,收)。
        """
        try:
            idx = calendar.index(day)
        except ValueError:
            return None
        if idx < _ANGLE_MA:                      # 需要 day 前 5 个交易日
            return None
        prev5 = [calendar[idx - i] for i in range(1, _ANGLE_MA + 1)]  # day-1..day-5
        try:
            closes_prev = [prices[d][3] for d in prev5]               # 收盘 day-1..day-5
            today = prices[day][0] if use_open else prices[day][3]
        except KeyError:
            return None
        ma_prev = sum(closes_prev) / _ANGLE_MA
        ma_today = (sum(closes_prev[:-1]) + today) / _ANGLE_MA        # day-1..day-4 + 今日
        if ma_prev == 0:
            return None
        return math.degrees(math.atan(_ANGLE_K * (ma_today / ma_prev - 1.0)))

    @staticmethod
    def _midline_ok(prices: dict[dt.date, tuple[float, float, float, float]],
                    calendar: list[dt.date], day: dt.date) -> Optional[bool]:
        """day 的「5 日均线 > 中轨线」是否成立（{T} 的 MA5 用 9:26 开盘价）。

        压力线=前 N 日最高价, 支撑线=前 N 日最低价, 中轨=(压力+支撑)/1.9；
        MA5 = day 前 4 日收盘 + 今日(开盘价)。数据不足/缺失返回 None（按不通过处理）。
        """
        try:
            idx = calendar.index(day)
        except ValueError:
            return None
        if idx < max(_MIDLINE_N, _MA_MID - 1):   # 需 day 前 N 日及前 4 日收盘
            return None
        prev_n = [calendar[idx - i] for i in range(1, _MIDLINE_N + 1)]   # day-1..day-N
        prev_close = [calendar[idx - i] for i in range(1, _MA_MID)]     # day-1..day-(MA_MID-1)
        try:
            highs = [prices[d][1] for d in prev_n]
            lows = [prices[d][2] for d in prev_n]
            closes = [prices[d][3] for d in prev_close]
            today = prices[day][0]                                      # 9:26 开盘价
        except KeyError:
            return None
        midline = (max(highs) + min(lows)) / _MIDLINE_DIVISOR
        ma = (sum(closes) + today) / _MA_MID
        return ma > midline

    # ----- 选股 -----------------------------------------------------------
    def _select(self, query: str) -> tuple[list[dict], str]:
        logger.debug(_j(api="wencai_nlp", query=query))
        resp = self._ths.wencai_nlp(query)
        time.sleep(_WENCAI_INTERVAL)
        data = resp.data
        if isinstance(data, dict):
            data = [data]
        hits = len(data) if data else 0
        logger.debug(_j(api="wencai_nlp",
                        success=resp.success, hits=hits, error=resp.error or None,
                        sample=data[0] if data else None))
        if not resp.success:
            return [], resp.error
        return (data or []), ""

    def _pass_local_checks(self, prices, calendar: list[dt.date], t: dt.date,
                           prevs: tuple[dt.date, dt.date, dt.date],
                           angle_t: Optional[float],
                           checks: list[dict]) -> bool:
        """对单只股票逐条核验本地指标，全部满足才返回 True。

        checks 为结构化指标列表，每项 {"type": ..., 参数...}。
        新增指标：在 LOCAL_INDICATORS 注册后，此处追加对应 type 的处理分支。
        """
        ref = dict(zip(("T1", "T2", "T3"), prevs))
        for chk in checks:
            t_type = chk["type"]
            if t_type == "angle_gt":
                if angle_t is None or not angle_t > chk["threshold"]:
                    return False
            elif t_type == "angle_gt_ref":
                if angle_t is None:
                    return False
                ref_angle = self._ma5_angle(prices, calendar, ref[chk["ref"]], use_open=False)
                if ref_angle is None or not angle_t > ref_angle:
                    return False
            elif t_type == "midline":
                if self._midline_ok(prices, calendar, t) is not True:
                    return False
        return True

    # ----- 单个交易日 -----------------------------------------------------
    def _query_day(self, calendar: list[dt.date], t: dt.date,
                   wencai_template: str, checks: list[dict]):
        """选股日的「问财阶段」：构造并发送问财请求、截断候选。

        wencai_template 为含占位符的问财语句模板（不含本地指标）；
        checks 为结构化本地指标列表，直接透传给 _process_candidates。
        返回 (day, stocks_raw, checks, prevs, fdays)。
        """
        day = DayResult(date=t.isoformat())
        try:
            prev = self._prev_trading_days(calendar, t, 3)  # [T1,T2,T3]
        except ValueError:
            day.error = "交易日历缺失"
            return day, [], checks, None, None
        t1, t2, t3 = prev
        query = wencai_template.format(
            T=_fmt_cn(t), T1=_fmt_cn(t1), T2=_fmt_cn(t2), T3=_fmt_cn(t3)
        )
        day.query = query
        stocks_raw, err = self._select(query)
        day.raw_response = stocks_raw
        if err:
            day.error = err
        hits = len(stocks_raw)
        if hits > _MAX_CANDIDATES_PER_DAY:
            day.notice = (f"问财命中 {hits} 只，超过单日处理上限 "
                          f"{_MAX_CANDIDATES_PER_DAY}，仅处理前 {_MAX_CANDIDATES_PER_DAY} 只。"
                          f"策略过宽会很慢，建议增加筛选条件收紧结果。")
            stocks_raw = stocks_raw[:_MAX_CANDIDATES_PER_DAY]
        fdays = self._next_trading_days(calendar, t, 3)  # [T+1,T+2,T+3]
        return day, stocks_raw, checks, (t1, t2, t3), fdays

    def _process_candidates(self, day: DayResult, stocks_raw: list[dict],
                            calendar: list[dt.date], t: dt.date,
                            prevs, fdays, checks: list[dict]) -> None:
        """选股日的「本地核验阶段」：逐只拉行情、9:26 本地核验、算后向收益。"""
        if prevs is None:
            return
        t1, t2, t3 = prevs
        for s in stocks_raw:
            wcode = s.get("股票代码") or s.get("THSCODE") or ""
            name = s.get("股票简称") or s.get("名称") or wcode
            kcode = convert_code(wcode)
            if not kcode:
                continue                          # 无法定位 K 线则无法核验角度，剔除

            # 本地核验所有 {T} 实时子句（9:26 开盘价口径），任一不满足即剔除。
            prices = self._fwd_window(kcode, t)
            angle_t = self._ma5_angle(prices, calendar, t, use_open=True)
            if not self._pass_local_checks(prices, calendar, t,
                                           (t1, t2, t3), angle_t, checks):
                continue
            # {T2} 角度作参考展示
            angle_t2 = self._ma5_angle(prices, calendar, t2, use_open=False)

            sf = StockForward(
                code=wcode, name=name,
                angle_t=round(angle_t, 2) if angle_t is not None else None,
                angle_t2=round(angle_t2, 2) if angle_t2 is not None else None)
            base, closes, lift = self._stock_closes(kcode, t, fdays)  # base=T开盘价
            sf.base_price = base
            sf.close_t = closes.get(t)            # T 收盘价（参考）
            sf.prev_close = closes.get(t1)        # T-1 收盘价（昨收）
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
            day.stocks.append(sf)

    def _run_day(self, calendar: list[dt.date], t: dt.date,
                 wencai_template: str, checks: list[dict]) -> DayResult:
        """完整跑一个选股日（问财阶段 + 本地核验阶段），供非流式接口使用。"""
        day, stocks_raw, checks, prevs, fdays = self._query_day(
            calendar, t, wencai_template, checks)
        self._process_candidates(day, stocks_raw, calendar, t, prevs, fdays, checks)
        return day

    # ----- 主流程 ---------------------------------------------------------
    def backtest_iter(self, start: dt.date, end: dt.date,
                      wencai_template: str = DEFAULT_STRATEGY_TEMPLATE,
                      checks: list[dict] | None = None):
        """逐个交易日产出进度事件，含问财接口请求状态：

        - {"type": "start", "total": N}
        - {"type": "querying", "index": i, "total": N, "date": ...}
        - {"type": "queried", "index": i, "total": N, "date": ...,
             "hits": 命中数, "processing": 待本地核验数, "error": ..., "notice": ...}
        - {"type": "day", "index": i, "total": N, "day": DayResult}
        - {"type": "done", "total": N}
        """
        if checks is None:
            checks = default_local_checks()
        self.connect()
        with self._lock:
            calendar = self._load_calendar(start, end)
            self._calendar = calendar
            sel_days = [d for d in calendar if start <= d <= end]
            total = len(sel_days)
            yield {"type": "start", "total": total}
            for i, t in enumerate(sel_days, 1):
                iso = t.isoformat()
                yield {"type": "querying", "index": i, "total": total, "date": iso}
                day, stocks_raw, chks, prevs, fdays = self._query_day(
                    calendar, t, wencai_template, checks)
                hits = len(day.raw_response) if isinstance(day.raw_response, list) else 0
                yield {"type": "queried", "index": i, "total": total, "date": iso,
                       "hits": hits, "processing": len(stocks_raw),
                       "error": day.error, "notice": day.notice}
                self._process_candidates(day, stocks_raw, calendar, t, prevs, fdays, chks)
                yield {"type": "day", "index": i, "total": total, "day": day}
            yield {"type": "done", "total": total}

    def backtest(self, start: dt.date, end: dt.date,
                 wencai_template: str = DEFAULT_STRATEGY_TEMPLATE,
                 checks: list[dict] | None = None,
                 progress=None) -> list[DayResult]:
        results: list[DayResult] = []
        for ev in self.backtest_iter(start, end, wencai_template, checks):
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
