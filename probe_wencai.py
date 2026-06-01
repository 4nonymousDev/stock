"""探针：确认连接、交易日历、问财返回的真实列名与 klines 结构。

先把"问财到底返回什么"摸清楚，再决定标定脚本怎么取角度字段。
"""
import datetime as dt
import json

from thsdk import THS

try:
    from config import load_account_ops
    OPS = load_account_ops()
except Exception:
    OPS = None

CALENDAR_CODE = "USHI1A0001"      # 上证指数，做交易日历
TEST_CODE = "USHA600519"          # 贵州茅台


def fmt_cn(d: dt.date) -> str:
    return f"{d.year}年{d.month}月{d.day}日"


def main():
    with THS(OPS) as ths:
        # 0) 取最近交易日历，自动挑一个已收盘交易日
        today = dt.date(2026, 6, 1)
        k = ths.klines(CALENDAR_CODE, count=15, interval="day")
        print("=== 交易日历连接 success:", k.success, "error:", k.error)
        if not k.success or not k.data:
            print("连接/取数失败，停止")
            return
        days = sorted(dt.datetime.strptime(str(r["时间"]).split()[0].replace("-", ""), "%Y%m%d").date()
                      if not isinstance(r["时间"], (dt.date, dt.datetime))
                      else (r["时间"].date() if isinstance(r["时间"], dt.datetime) else r["时间"])
                      for r in k.data)
        closed = [d for d in days if d < today]
        calib = closed[-1]
        print("最近 15 个交易日:", [d.isoformat() for d in days])
        print(">>> 选定标定日(最近已收盘交易日):", calib.isoformat())

        # 1) klines 结构
        kl = ths.klines(TEST_CODE, count=10, interval="day", adjust="forward")
        print("\n=== klines(forward) 列名:", list(kl.data[0].keys()) if kl.data else "空")
        print("最后一行:", kl.data[-1] if kl.data else "空")

        # 2) 问财：用中文日期格式（项目里已验证可用的写法）
        digits = TEST_CODE[-6:]
        for cond in (f"{digits} {fmt_cn(calib)}5日均线角度",
                     f"{digits} {fmt_cn(calib)}的5日均线角度"):
            print(f"\n=== wencai_nlp 条件: {cond!r}")
            r = ths.wencai_nlp(cond)
            print("success:", r.success, "error:", r.error)
            if r.data:
                row = r.data[0] if isinstance(r.data, list) else r.data
                print("返回键名:")
                print(json.dumps(list(row.keys()), ensure_ascii=False, indent=2))
                # 挑出含"角度"的键
                hits = {k: v for k, v in row.items() if "角度" in str(k)}
                print("含'角度'的字段:", json.dumps(hits, ensure_ascii=False))
            else:
                print("无 data")


if __name__ == "__main__":
    main()
