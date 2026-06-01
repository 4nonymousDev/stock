"""反推问财「5日均线角度」公式中的缩放系数 k，并判定问财采用的口径。

已确定的两项：
  - 斜率窗口：今日 5MA 对比 昨日 5MA（1 个交易日的台阶）
  - 模型：角度 = arctan(k * x)，x 为归一后的斜率

本脚本不预设口径，而是把四种组合都拟合出来比拟合优度：
  归一口径   ：绝对(元)   x = ΔMA          /   涨幅   x = ΔMA / MA5(T-1)
  复权口径   ：前复权 forward / 不复权 ""
  恒等式     ：ΔMA = MA5(T) - MA5(T-1) = (Close(T) - Close(T-5)) / 5

判优标准：对每只股票反解 k，取均值作为该组合的全局 k，再用它回算角度，
看与问财真值的 RMSE（度）。RMSE 最小的组合即问财最可能采用的公式，其 k 即所求。

标定完拿到 k 后，9:26 把竞价价当作 Close(T) 套同一公式即可。
"""
import datetime as dt
import math
import statistics as st
import time

from thsdk import THS

try:
    from config import load_account_ops
    OPS = load_account_ops()
except Exception:
    OPS = None

CALENDAR_CODE = "USHI1A0001"   # 上证指数当交易日历
TODAY = dt.date(2026, 6, 1)    # 运行日；标定日自动取其前最近的已收盘交易日
KLINE_SLEEP = 0.05             # klines 官方限频 20ms，留余量

# 跨价位样本（2 元到 1000+ 元），覆盖不同价位以诊断归一口径。
# 取不到/停牌/斜率为 0 的会自动跳过。
SAMPLE = [
    "USHA600519",  # 贵州茅台   ~1300
    "USZA300750",  # 宁德时代   ~250
    "USHA601318",  # 中国平安   ~50
    "USZA000001",  # 平安银行   ~12
    "USZA300033",  # 同花顺     ~250
    "USHA603259",  # 药明康德   ~70
    "USHA600036",  # 招商银行   ~40
    "USHA601398",  # 工商银行   ~7
    "USHA601288",  # 农业银行   ~5
    "USHA600000",  # 浦发银行   ~10
    "USHA600030",  # 中信证券   ~25
    "USHA600276",  # 恒瑞医药   ~50
    "USHA600887",  # 伊利股份   ~28
    "USHA601012",  # 隆基绿能   ~20
    "USHA600900",  # 长江电力   ~28
    "USZA000651",  # 格力电器   ~45
    "USZA000333",  # 美的集团   ~70
    "USZA002594",  # 比亚迪     ~250
    "USHA600104",  # 上汽集团   ~16
    "USHA601857",  # 中国石油   ~9
    "USHA600028",  # 中国石化   ~6
    "USHA601988",  # 中国银行   ~5
    "USHA600585",  # 海螺水泥   ~25
    "USZA002415",  # 海康威视   ~30
    "USHA600309",  # 万华化学   ~70
    "USHA603288",  # 海天味业   ~40
    "USHA688981",  # 中芯国际   ~90
    "USHA688111",  # 金山办公   ~300
    "USZA300059",  # 东方财富   ~15
    "USZA002475",  # 立讯精密   ~35
]


def fmt_cn(d: dt.date) -> str:
    return f"{d.year}年{d.month}月{d.day}日"


def to_date(ts) -> dt.date:
    if isinstance(ts, dt.datetime):
        return ts.date()
    if isinstance(ts, dt.date):
        return ts
    s = str(ts).split()[0].replace("-", "").replace("/", "")
    return dt.datetime.strptime(s, "%Y%m%d").date()


def pick_calib_date(ths) -> dt.date:
    k = ths.klines(CALENDAR_CODE, count=20, interval="day")
    if not k.success or not k.data:
        raise RuntimeError(f"取交易日历失败: {k.error}")
    days = sorted(to_date(r["时间"]) for r in k.data)
    closed = [d for d in days if d < TODAY]
    if not closed:
        raise RuntimeError("没有已收盘交易日")
    return closed[-1]


def ma5_pair(ths, code, calib, adjust):
    """取 calib 当日与前一日的 5MA、当日收盘价（指定复权口径）。"""
    r = ths.klines(code, count=40, interval="day", adjust=adjust)
    time.sleep(KLINE_SLEEP)
    if not r.success or not r.data:
        return None
    rows = sorted(((to_date(x["时间"]), float(x["收盘价"])) for x in r.data),
                  key=lambda t: t[0])
    rows = [t for t in rows if t[0] <= calib]
    if len(rows) < 6:
        return None
    c = [v for _, v in rows][-6:]          # c[-1]=T ... c[-6]=T-5
    ma_today = sum(c[-5:]) / 5
    ma_prev = sum(c[-6:-1]) / 5
    return {"close_T": c[-1], "ma_today": ma_today, "ma_prev": ma_prev,
            "dMA": ma_today - ma_prev}


def wencai_angle(ths, code, calib):
    digits = code[-6:]
    cond = f"{digits} {fmt_cn(calib)}5日均线角度"
    r = ths.wencai_nlp(cond)
    if not r.success or not r.data:
        return None
    row = r.data[0] if isinstance(r.data, list) else r.data
    for key, val in row.items():
        if "均线角度" in str(key):
            try:
                return float(val)
            except (TypeError, ValueError):
                return None
    return None


def fit_report(name, samples, x_of):
    """samples: [(theta_deg, feature_dict)]；x_of(feature)->归一斜率 x。
    对每只解 k=tan(theta)/x，取均值为全局 k，再回算角度看 RMSE。"""
    ks, xs, thetas = [], [], []
    for theta, feat in samples:
        x = x_of(feat)
        if x == 0:
            continue
        t = math.tan(math.radians(theta))
        ks.append(t / x)
        xs.append(x)
        thetas.append(theta)
    if len(ks) < 2:
        print(f"[{name}] 有效样本不足")
        return None
    k = st.mean(ks)
    cv = st.pstdev(ks) / abs(k) if k else float("inf")
    # 用全局 k 回算角度，算 RMSE（度）
    sq = []
    for x, theta in zip(xs, thetas):
        pred = math.degrees(math.atan(k * x))
        sq.append((pred - theta) ** 2)
    rmse = math.sqrt(sum(sq) / len(sq))
    print(f"[{name:18s}] k={k:10.4f}  CV={cv:6.2%}  回算RMSE={rmse:6.3f}°  n={len(ks)}")
    return {"name": name, "k": k, "cv": cv, "rmse": rmse}


def main():
    with THS(OPS) as ths:
        calib = pick_calib_date(ths)
        print(f">>> 标定日（最近已收盘交易日）: {calib.isoformat()}\n")

        # 收集每只股票：问财角度 + 两种复权口径下的 MA 信息
        recs = []
        for code in SAMPLE:
            theta = wencai_angle(ths, code, calib)
            if theta is None:
                print(f"跳过 {code}: 未取到问财角度")
                continue
            m_fwd = ma5_pair(ths, code, calib, "forward")
            m_raw = ma5_pair(ths, code, calib, "")
            if not m_fwd or not m_raw:
                print(f"跳过 {code}: klines 数据不足")
                continue
            recs.append({"code": code, "theta": theta, "fwd": m_fwd, "raw": m_raw})
            print(f"{code} 价≈{m_raw['close_T']:8.2f}  角度={theta:7.3f}°  "
                  f"ΔMA(raw)={m_raw['dMA']:+.4f}  ΔMA(fwd)={m_fwd['dMA']:+.4f}")

        if len(recs) < 2:
            print("\n有效样本不足，无法标定。")
            return

        print(f"\n=== 四组合拟合（共 {len(recs)} 只）===")
        results = []
        for adj_name, key in (("前复权", "fwd"), ("不复权", "raw")):
            abs_samples = [(r["theta"], r[key]) for r in recs]
            results.append(fit_report(f"{adj_name}·绝对(元)", abs_samples,
                                      lambda f: f["dMA"]))
            results.append(fit_report(f"{adj_name}·涨幅", abs_samples,
                                      lambda f: f["dMA"] / f["ma_prev"] if f["ma_prev"] else 0))

        results = [r for r in results if r]
        if results:
            best = min(results, key=lambda r: r["rmse"])
            print(f"\n>>> 拟合最优: 【{best['name']}】  k = {best['k']:.4f}  "
                  f"(回算RMSE {best['rmse']:.3f}°, CV {best['cv']:.2%})")
            print(">>> RMSE 越小越像问财的真实公式；该口径的 k 即所求系数。")


if __name__ == "__main__":
    main()
