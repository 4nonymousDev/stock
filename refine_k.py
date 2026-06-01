"""在已判定的【前复权·涨幅】口径上精修 k：
   - 扫描最小化角度RMSE的最优 k
   - 对比整数 100 的拟合
   - 列出每只残差，看是否被个别离群股拉大
"""
import datetime as dt
import math
import time

from thsdk import THS
from calibrate_ma5_angle import (OPS, SAMPLE, fmt_cn, to_date,
                                  pick_calib_date, ma5_pair, wencai_angle)


def main():
    with THS(OPS) as ths:
        calib = pick_calib_date(ths)
        print(f">>> 标定日: {calib.isoformat()}（前复权·涨幅口径精修）\n")
        recs = []
        for code in SAMPLE:
            theta = wencai_angle(ths, code, calib)
            if theta is None:
                continue
            m = ma5_pair(ths, code, calib, "forward")
            time.sleep(0.02)
            if not m or m["ma_prev"] == 0:
                continue
            r = m["dMA"] / m["ma_prev"]          # 5MA 的单日涨幅
            if r == 0:
                continue
            recs.append({"code": code, "theta": theta, "r": r})

        rmse = lambda k: math.sqrt(sum(
            (math.degrees(math.atan(k * x["r"])) - x["theta"]) ** 2 for x in recs
        ) / len(recs))

        # 扫描最优 k
        best_k, best = min(((k / 100, rmse(k / 100)) for k in range(7000, 11001, 1)),
                           key=lambda t: t[1])
        print(f"扫描最优 k = {best_k:.2f}   RMSE = {best:.4f}°")
        print(f"整数   k = 100.00   RMSE = {rmse(100):.4f}°")
        print(f"      tan(基准角)= k·1% ->  k=100 时 5MA单日涨1% 对应 "
              f"{math.degrees(math.atan(1.0)):.2f}°\n")

        print("每只残差（按 |残差| 降序，全局 k=最优）:")
        rows = sorted(recs, key=lambda x: -abs(
            math.degrees(math.atan(best_k * x["r"])) - x["theta"]))
        for x in rows:
            pred = math.degrees(math.atan(best_k * x["r"]))
            print(f"  {x['code']}  实际={x['theta']:7.2f}°  回算={pred:7.2f}°  "
                  f"残差={pred - x['theta']:+6.2f}°  (5MA涨幅={x['r'] * 100:+.3f}%)")


if __name__ == "__main__":
    main()
