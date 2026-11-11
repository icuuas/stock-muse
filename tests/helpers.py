# -*- coding: utf-8 -*-
"""离线测试的公共夹具。所有数据都是模拟的，只验证解析与逻辑；真实接口是否可用由 scripts/selftest.py 在线验证。"""
import datetime as dt
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))

UTC = dt.timezone.utc


def at(y, mo, d, h, mi, s=0):
    return dt.datetime(y, mo, d, h, mi, s, tzinfo=UTC)


# 2026-11-10 是周二；美股冬令时 ET=UTC-5，北京 UTC+8
US_PRE = at(2026, 11, 10, 13, 30)       # 08:30 ET
US_REG = at(2026, 11, 10, 15, 0)        # 10:00 ET
US_POST = at(2026, 11, 10, 21, 42)      # 16:42 ET
US_NIGHT = at(2026, 11, 11, 3, 0)       # 22:00 ET（周二晚）
CN_CLOSE = at(2026, 11, 10, 7, 34)      # 15:34 北京
CN_REG = at(2026, 11, 10, 2, 0)         # 10:00 北京
HK_CLOSE = at(2026, 11, 10, 8, 8)       # 16:08 香港


class TempHome(unittest.TestCase):
    """每个用例使用独立的数据目录。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="stockpush-")
        os.environ["STOCK_PUSH_HOME"] = self.tmp
        os.environ.pop("STOCK_PUSH_NOW", None)
        os.environ.pop("FINNHUB_KEY", None)
        import common as C
        C.ensure_dirs()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def set_features(self, **kw):
        import common as C
        f = dict(C.FEATURE_DEFAULTS)
        f.update(kw)
        C.save_json(C.path("state", "selftest.json"), {"features": f})

    def set_prefs(self, **kw):
        import common as C
        C.save_json(C.path("data", "prefs.json"), kw)


# ---------------------------------------------------------------- 数据夹具
def sina_cn_fields(name="贵州茅台", open_="1450.00", prev="1453.00", price="1444.99", date="2026-11-10", time_="15:00:00"):
    f = [name, open_, prev, price, "1460.00", "1440.00", "1444.00", "1445.00", "2500000", "3600000000"]
    f += ["0"] * 20
    f += [date, time_, "00"]
    return f


def sina_hk_fields(prev="419.8", price="418.4", date="2026/11/10", time_="16:08:20"):
    return ["TENCENT", "腾讯控股", "420.0", prev, "421.0", "415.0", price, "-1.4", "-0.33", "418.4", "418.6",
            "5000000000", "12000000", "20", "0.5", "500", "300", date, time_]


def sina_us_fields(price="174.15", time_cn="2026-11-11 04:00:00", ext="182.35", ext_time="Nov 10 04:42PM EST"):
    f = ["英伟达", price, "2.4", time_cn, "4.0", "170", "175", "169", "200", "90"] + ["0"] * 20
    f[21] = ext
    f[24] = ext_time
    return f


def yahoo_chart(prices, start_ts, step=60, prev_close=170.0, kind="us", name="NVIDIA Corp", period_day=None):
    """分时图夹具：prices 为每分钟收盘价列表，start_ts 为首根 K 线的 UTC 时间戳。
    period_day：meta.currentTradingPeriod 所属的 UTC 日期（默认与 K 线同一天）。"""
    ts = [start_ts + i * step for i in range(len(prices))]
    day0 = dt.datetime.fromtimestamp(start_ts, UTC).replace(hour=0, minute=0, second=0)
    if period_day:
        day0 = dt.datetime(period_day.year, period_day.month, period_day.day, tzinfo=UTC)
    d = day0.timestamp()
    periods = {
        "pre": {"start": int(d + 9 * 3600), "end": int(d + 14.5 * 3600)},
        "regular": {"start": int(d + 14.5 * 3600), "end": int(d + 21 * 3600)},
        "post": {"start": int(d + 21 * 3600), "end": int(d + 25 * 3600)},
    }
    return {"chart": {"result": [{
        "meta": {"currency": "USD", "longName": name, "regularMarketPrice": 174.15,
                 "regularMarketTime": int(d + 21 * 3600), "chartPreviousClose": prev_close, "gmtoffset": -18000,
                 "currentTradingPeriod": periods},
        "timestamp": ts, "indicators": {"quote": [{"close": prices}]}}]}}


def yahoo_daily(closes, last_date=dt.date(2026, 11, 10)):
    """日线夹具：closes 按日期升序，最后一根对应 last_date（仅工作日）。"""
    dates = []
    d = last_date
    while len(dates) < len(closes):
        if d.weekday() < 5:
            dates.append(d)
        d -= dt.timedelta(days=1)
    dates.reverse()
    ts = [int(dt.datetime(x.year, x.month, x.day, 14, 30, tzinfo=UTC).timestamp()) for x in dates]
    return {"chart": {"result": [{"meta": {"gmtoffset": -18000}, "timestamp": ts,
                                  "indicators": {"quote": [{"close": closes}]}}]}}


def raw_quote(price, t, src="nasdaq", prev=None, kind="regular", name="NVIDIA", realtime=None, **kw):
    r = {"price": price, "time": t, "prev_close": prev, "name": name, "kind": kind, "realtime": realtime,
         "source": src, "extra": {}}
    r.update(kw)
    return r
