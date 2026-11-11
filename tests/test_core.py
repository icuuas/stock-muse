# -*- coding: utf-8 -*-
import datetime as dt
import json
import unittest

from helpers import (TempHome, at, US_PRE, US_REG, US_POST, US_NIGHT, CN_CLOSE, CN_REG, HK_CLOSE,
                     sina_cn_fields, sina_hk_fields, sina_us_fields, yahoo_chart, yahoo_daily)
import common as C
import quotes as Q
import holdings as H
import fx as FX
import news as N


class TestSymbols(unittest.TestCase):
    def test_normalize(self):
        cases = {"nvda": "NVDA.US", "NVDA.US": "NVDA.US", "600519": "600519.SH", "sh600519": "600519.SH",
                 "600519.SS": "600519.SH", "000001": "000001.SZ", "sz000001": "000001.SZ", "300750": "300750.SZ",
                 "688981": "688981.SH", "00700": "00700.HK", "0700.HK": "00700.HK", "HK9988": "09988.HK",
                 "BRK-B": "BRK.B.US", "brk.b": "BRK.B.US"}
        for raw, want in cases.items():
            self.assertEqual(C.normalize_symbol(raw), want, raw)

    def test_normalize_errors(self):
        for bad in ["", "700", "830799", "900901", "茅台", "TOOLONGNAME", "123456789"]:
            with self.assertRaises(C.SymbolError, msg=bad):
                C.normalize_symbol(bad)

    def test_source_codes(self):
        self.assertEqual(C.sina_code("NVDA.US"), "gb_nvda")
        self.assertEqual(C.sina_code("600519.SH"), "sh600519")
        self.assertEqual(C.sina_code("00700.HK"), "hk00700")
        self.assertEqual(C.yahoo_code("600519.SH"), "600519.SS")
        self.assertEqual(C.yahoo_code("000001.SZ"), "000001.SZ")
        self.assertEqual(C.yahoo_code("00700.HK"), "0700.HK")
        self.assertEqual(C.yahoo_code("09988.HK"), "9988.HK")
        self.assertEqual(C.yahoo_code("BRK.B.US"), "BRK-B")
        self.assertEqual(C.market_of("600519.SH"), "CN")


class TestSessions(unittest.TestCase):
    def test_us(self):
        self.assertEqual(C.session("US", US_PRE), "pre")
        self.assertEqual(C.session("US", US_REG), "regular")
        self.assertEqual(C.session("US", US_POST), "post")
        self.assertEqual(C.session("US", US_NIGHT), "night")
        self.assertEqual(C.session("US", US_NIGHT, night=False), "closed")
        self.assertEqual(C.session("US", at(2026, 11, 14, 2, 0)), "closed")     # 周五 21:00 ET
        self.assertEqual(C.session("US", at(2026, 11, 15, 15, 0)), "closed")    # 周六
        self.assertEqual(C.session("US", at(2026, 11, 16, 2, 0)), "night")      # 周日 21:00 ET
        self.assertEqual(C.session("US", at(2026, 11, 16, 8, 0)), "night")      # 周一 03:00 ET

    def test_cn_hk(self):
        self.assertEqual(C.session("CN", CN_REG), "regular")
        self.assertEqual(C.session("CN", at(2026, 11, 10, 4, 0)), "lunch")
        self.assertEqual(C.session("CN", CN_CLOSE), "closed")
        self.assertEqual(C.session("CN", at(2026, 11, 10, 1, 20)), "auction")
        self.assertEqual(C.session("HK", HK_CLOSE), "closing")
        self.assertEqual(C.session("HK", at(2026, 11, 10, 3, 0)), "regular")
        self.assertEqual(C.session("HK", at(2026, 11, 14, 3, 0)), "closed")     # 周六

    def test_session_date(self):
        self.assertEqual(C.session_date("US", US_POST), dt.date(2026, 11, 10))
        self.assertEqual(C.session_date("US", US_NIGHT), dt.date(2026, 11, 11))   # 夜盘属于下一交易日
        self.assertEqual(C.session_date("US", at(2026, 11, 15, 15, 0)), dt.date(2026, 11, 13))  # 周六→周五
        self.assertEqual(C.session_date("US", at(2026, 11, 16, 2, 0)), dt.date(2026, 11, 16))   # 周日晚→周一
        self.assertEqual(C.session_date("CN", CN_REG), dt.date(2026, 11, 10))
        self.assertEqual(C.session_date("CN", at(2026, 11, 9, 22, 0)), dt.date(2026, 11, 9))    # 周二 06:00 北京，未开盘→周一

    def test_fmt_time(self):
        d = at(2026, 11, 10, 21, 42)
        self.assertEqual(C.fmt_time("US", d), "16:42 ET（北京 05:42）")
        self.assertEqual(C.fmt_time("HK", at(2026, 11, 10, 8, 8)), "16:08 香港")   # 与北京同偏移，只写一个


class TestFormat(unittest.TestCase):
    def test_fmt(self):
        self.assertEqual(C.fmt_price(182.35, "USD"), "$182.35")
        self.assertEqual(C.fmt_price(0.52, "HKD"), "HK$0.520")
        self.assertEqual(C.fmt_money(1286400, "CNY"), "¥1,286,400")
        self.assertEqual(C.fmt_money(8320.5, "CNY", sign=True), "+¥8,320.50")
        self.assertEqual(C.fmt_money(-12.3, "USD"), "-$12.30")
        self.assertEqual(C.fmt_pct(4.8), "+4.80%")
        self.assertEqual(C.fmt_pct(None), "—")
        self.assertEqual(C.fmt_shares(100.0), "100")
        self.assertEqual(C.fmt_shares(1.5), "1.5")
        self.assertEqual(C.trend_icon(1, "red_up"), "🔴")
        self.assertEqual(C.trend_icon(1, "green_up"), "🟢")


class TestParsers(unittest.TestCase):
    def test_sina_cn(self):
        r = Q.parse_sina_cn(sina_cn_fields())
        self.assertEqual(r["price"], 1444.99)
        self.assertEqual(r["prev_close"], 1453.0)
        self.assertEqual(C.iso(r["time"]), "2026-11-10T07:00:00Z")
        self.assertEqual(r["name"], "贵州茅台")

    def test_sina_cn_not_traded_yet(self):
        r = Q.parse_sina_cn(sina_cn_fields(price="0.000"))
        self.assertEqual(r["price"], 1453.0)
        self.assertTrue(r["no_trade_yet"])
        self.assertIsNone(Q.parse_sina_cn(["short"]))

    def test_sina_hk(self):
        r = Q.parse_sina_hk(sina_hk_fields())
        self.assertEqual((r["price"], r["prev_close"], r["name"]), (418.4, 419.8, "腾讯控股"))
        self.assertEqual(C.iso(r["time"]), "2026-11-10T08:08:20Z")
        self.assertEqual(r["extra"]["week52_high"], 500.0)

    def test_sina_us_with_ext(self):
        r = Q.parse_sina_us(sina_us_fields(), at(2026, 11, 10, 22, 0))
        self.assertEqual(r["price"], 174.15)
        self.assertEqual(r["ext_price"], 182.35)
        self.assertEqual(C.iso(r["ext_time"]), "2026-11-10T21:42:00Z")
        self.assertEqual(C.iso(r["time"]), "2026-11-10T20:00:00Z")     # 北京 04:00 → UTC 20:00（待自测校验时区）

    def test_sina_text(self):
        text = ('var hq_str_sh600519="%s";\nvar hq_str_hk00700="%s";\nvar hq_str_gb_zzz="";\n'
                % (",".join(sina_cn_fields()), ",".join(sina_hk_fields())))
        out = Q.parse_sina_text(text, ["600519.SH", "00700.HK", "ZZZ.US"])
        self.assertEqual(set(out), {"600519.SH", "00700.HK"})

    def test_yahoo_chart_post(self):
        start = int(at(2026, 11, 10, 21, 40).timestamp())
        obj = yahoo_chart([182.0, 182.1, 182.35], start)
        r = Q.parse_yahoo_chart(obj, "NVDA.US")
        self.assertEqual(r["price"], 182.35)
        self.assertEqual(r["kind"], "post")
        self.assertEqual(C.iso(r["time"]), "2026-11-10T21:42:00Z")
        self.assertEqual(r["prev_close"], 170.0)

    def test_yahoo_chart_regular_and_close(self):
        r = Q.parse_yahoo_chart(yahoo_chart([174.0], int(at(2026, 11, 10, 15, 0).timestamp())), "NVDA.US")
        self.assertEqual(r["kind"], "regular")
        r = Q.parse_yahoo_chart(yahoo_chart([174.0], int(at(2026, 11, 9, 15, 0).timestamp()),
                                            period_day=dt.date(2026, 11, 10)), "NVDA.US")
        self.assertEqual(r["kind"], "close")      # K 线是前一天的，不在当前交易时段里
        self.assertIsNone(Q.parse_yahoo_chart({}, "NVDA.US"))

    def test_yahoo_chart_skips_null_tail(self):
        start = int(at(2026, 11, 10, 15, 0).timestamp())
        r = Q.parse_yahoo_chart(yahoo_chart([174.0, 175.0, None, None], start), "NVDA.US")
        self.assertEqual(r["price"], 175.0)

    def test_yahoo_daily_and_prev_close(self):
        bars = Q.parse_yahoo_daily(yahoo_daily([168.0, 169.0, 170.0, 174.15]))
        self.assertEqual([b[0] for b in bars], ["2026-11-05", "2026-11-06", "2026-11-09", "2026-11-10"])
        self.assertEqual(Q.prev_from_bars(bars, dt.date(2026, 11, 10)), 170.0)      # 盘后：前收盘=周一
        self.assertEqual(Q.prev_from_bars(bars, dt.date(2026, 11, 11)), 174.15)     # 夜盘：前收盘=周二收盘
        self.assertEqual(Q.close_on(bars, dt.date(2026, 11, 10)), 174.15)

    def test_finnhub(self):
        r = Q.parse_finnhub({"c": 174.15, "d": 4, "dp": 2.4, "h": 175, "l": 169, "o": 170, "pc": 170.0, "t": 1762808400})
        self.assertEqual((r["price"], r["prev_close"]), (174.15, 170.0))
        self.assertIsNone(Q.parse_finnhub({"c": 0, "t": 0}))
        self.assertIsNone(Q.parse_finnhub({"error": "Invalid API key"}))

    def test_nasdaq(self):
        obj = {"data": {"companyName": "NVIDIA Corporation", "marketStatus": "After-Hours",
                        "primaryData": {"lastSalePrice": "$182.35", "lastTradeTimestamp": "DATA AS OF Nov 10, 2026 4:42 PM ET",
                                        "isRealTime": True, "netChange": "+8.2", "percentageChange": "+4.71%"}}}
        r = Q.parse_nasdaq(obj)
        self.assertEqual(r["price"], 182.35)
        self.assertEqual(r["kind"], "post")
        self.assertTrue(r["realtime"])
        self.assertEqual(C.iso(r["time"]), "2026-11-10T21:42:00Z")
        self.assertIsNone(Q.parse_nasdaq({"data": None}))
        self.assertIsNone(Q.parse_nasdaq({"data": {"primaryData": {"lastSalePrice": "N/A"}}}))

    def test_robinhood(self):
        obj = {"results": [
            {"symbol": "NVDA", "last_extended_hours_trade_price": "182.50", "bid_price": "182.4", "ask_price": "182.6",
             "previous_close": "174.15", "venue_last_non_reg_trade_time": "2026-11-11T02:55:00Z", "updated_at": "2026-11-11T02:56:00Z"},
            {"symbol": "AAPL", "last_extended_hours_trade_price": None, "bid_price": "228.0", "ask_price": "228.4",
             "previous_close": "228.1", "venue_last_non_reg_trade_time": None, "updated_at": "2026-11-11T02:58:00Z"},
            None]}
        out = Q.parse_robinhood(obj)
        self.assertEqual(out["NVDA.US"]["price"], 182.5)
        self.assertEqual(out["NVDA.US"]["extra"]["basis"], "last")
        self.assertEqual(out["AAPL.US"]["price"], 228.2)
        self.assertEqual(out["AAPL.US"]["extra"]["basis"], "mid")

    def test_fx(self):
        obj = {"chart": {"result": [{"meta": {"regularMarketPrice": 7.1234, "regularMarketTime": 1762808400}}]}}
        p, t = FX.parse_fx(obj)
        self.assertEqual(p, 7.1234)
        self.assertEqual(FX.parse_fx({}), (None, None))
        pairs = {"USDCNY": 7.2, "USDHKD": 7.8, "HKDCNY": 0.92}
        m = FX.to_base_map(pairs, "CNY")
        self.assertEqual((m["USD"], m["HKD"], m["CNY"]), (7.2, 0.92, 1.0))
        m = FX.to_base_map(pairs, "USD")
        self.assertAlmostEqual(m["CNY"], 1 / 7.2)
        self.assertAlmostEqual(m["HKD"], 1 / 7.8)
        m = FX.to_base_map(pairs, "HKD")
        self.assertEqual(m["USD"], 7.8)
        self.assertAlmostEqual(m["CNY"], 1 / 0.92)

    def test_news_parsers(self):
        fh = [{"id": 1, "datetime": 1762808400, "headline": "Nvidia beats earnings", "source": "Reuters",
               "summary": "s", "url": "u"}, {"headline": ""}]
        self.assertEqual(len(N.parse_finnhub_news(fh, "NVDA.US")), 1)
        yn = {"news": [{"uuid": "a", "title": "腾讯发布财报", "publisher": "Yahoo", "link": "l", "providerPublishTime": 1762808400}]}
        self.assertEqual(N.parse_yahoo_news(yn, "00700.HK")[0]["title"], "腾讯发布财报")
        em = {"data": {"list": [{"art_code": "AN1", "title": "关于回购股份的公告", "notice_date": "2026-11-10 00:00:00"}]}}
        it = N.parse_eastmoney(em, "600519.SH")[0]
        self.assertEqual((it["date"], it["kind"]), ("2026-11-10", "filing"))
        ed = {"filings": {"recent": {"form": ["8-K", "4", "10-Q"], "filingDate": ["2026-11-10", "2026-11-10", "2026-10-01"],
                                     "accessionNumber": ["0001-26-000001", "0001-26-000002", "0001-26-000003"],
                                     "primaryDocument": ["a.htm", "b.htm", "c.htm"],
                                     "primaryDocDescription": ["Current report", "", ""]}}}
        out = N.parse_edgar(ed, "NVDA.US", 1045810, "2026-11-09")
        self.assertEqual([x["form"] for x in out], ["8-K"])
        self.assertTrue(out[0]["url"].endswith("/104581000000126000001/a.htm".replace("104581000000126000001", "000126000001")))


class TestHoldings(TempHome):
    def test_buy_average_cost_and_sell(self):
        h = H.load()
        p, msg, w = H.buy(h, "NVDA.US", 100, 170.0)
        self.assertEqual(p["cost"], 170.0)
        H.buy(h, "NVDA.US", 100, 190.0, fee=10)
        self.assertEqual(p["shares"], 200)
        self.assertAlmostEqual(p["cost"], (100 * 170 + 100 * 190 + 10) / 200, 4)
        p, msg, w = H.sell(h, "NVDA.US", 50, 200.0, fee=1)
        self.assertEqual(p["shares"], 150)
        self.assertIn("已实现盈亏", msg)
        with self.assertRaises(H.HoldingsError):
            H.sell(h, "NVDA.US", 1000, 200.0)
        with self.assertRaises(H.HoldingsError):
            H.sell(h, "AAPL.US", 1, 200.0)
        H.sell(h, "NVDA.US", 150, 210.0)
        self.assertIsNone(H.find(h, "NVDA.US"))

    def test_set_remove_aliases_and_warning(self):
        h = H.load()
        H.set_pos(h, "00700.HK", 200, 380.0, "腾讯控股")
        p, msg, w = H.set_pos(h, "00700.HK", 5000, None)
        self.assertTrue(w)                              # 数量变化 10 倍以上要提醒确认
        self.assertEqual(p["cost"], 380.0)              # 没给成本则保留原成本
        self.assertEqual(p["currency"], "HKD")
        H.set_aliases(h, "00700.HK", ["腾讯", "Tencent"])
        self.assertEqual(p["aliases"], ["腾讯", "Tencent"])
        H.remove(h, "00700.HK")
        self.assertEqual(h["positions"], [])

    def test_cli_save_undo_and_log(self):
        import io
        import contextlib

        def run(args):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = H.main(args)
            return code, json.loads(buf.getvalue())

        self.assertEqual(run(["buy", "nvda", "100", "180"])[0], 0)
        self.assertEqual(run(["buy", "600519", "10", "1500"])[0], 0)
        code, out = run(["show"])
        self.assertEqual(len(out["positions"]), 2)
        code, out = run(["undo"])
        self.assertEqual(code, 0)
        self.assertEqual(len(run(["show"])[1]["positions"]), 1)
        code, out = run(["sell", "NVDA", "500", "190"])
        self.assertEqual(code, 1)
        self.assertIn("超过持仓", out["message"])
        code, out = run(["normalize", "茅台"])
        self.assertEqual(code, 1)
        with open(C.path("data", "trades.jsonl"), encoding="utf-8") as f:
            trades = f.read().strip().splitlines()
        self.assertGreaterEqual(len(trades), 3)


if __name__ == "__main__":
    unittest.main()
