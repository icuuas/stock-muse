# -*- coding: utf-8 -*-
import datetime as dt
import json
import os
import unittest
from unittest import mock

from helpers import (TempHome, at, US_PRE, US_REG, US_POST, US_NIGHT, CN_CLOSE, CN_REG, HK_CLOSE,
                     raw_quote, yahoo_daily)
import common as C
import quotes as Q
import holdings as H
import snapshot as S
import monitor as M
import news as N
import render as R
import report as RP
import fx as FX

BARS_US = Q.parse_yahoo_daily(yahoo_daily([168.0, 169.0, 170.0, 174.15]))   # 最后一根=11-10 收盘 174.15


def fake(mapping, err=None):
    """构造一个假的数据源：mapping 为 {symbol: raw}。"""
    def f(syms, now=None):
        return {s: dict(mapping[s]) for s in syms if s in mapping}, (None if any(s in mapping for s in syms) else err)
    return f


class TestQuotesOrchestration(TempHome):
    def patched(self, **fetchers):
        d = dict(Q.FETCH)
        d.update(fetchers)
        return mock.patch.dict(Q.FETCH, d, clear=True)

    def test_post_market_uses_nasdaq_and_prev_close_from_bars(self):
        raw = raw_quote(182.35, at(2026, 11, 10, 21, 40), src="nasdaq", kind="post", realtime=True)
        with self.patched(nasdaq=fake({"NVDA.US": raw})), mock.patch.object(Q, "daily_bars", return_value=BARS_US):
            q = Q.get_quotes(["NVDA.US"], now=US_POST)["NVDA.US"]
        self.assertEqual(q["source"], "nasdaq")
        self.assertEqual(q["price_kind"], "post")
        self.assertEqual(q["prev_close"], 170.0)                  # 前收盘 = 周一收盘，不是周二
        self.assertAlmostEqual(q["day_change_pct"], (182.35 / 170 - 1) * 100, 4)
        self.assertEqual(q["reg_close"], 174.15)                  # 盘后相对常规收盘
        self.assertAlmostEqual(q["ext_change_pct"], (182.35 / 174.15 - 1) * 100, 4)
        self.assertEqual(q["status_label"], "实时")
        self.assertFalse(q["stale"])

    def test_fallback_chain_when_first_source_fails(self):
        raw = raw_quote(182.0, at(2026, 11, 10, 21, 30), src="yahoo", kind="post")
        with self.patched(nasdaq=fake({}, err="HTTP 403"), yahoo=fake({"NVDA.US": raw})), \
                mock.patch.object(Q, "daily_bars", return_value=BARS_US):
            q = Q.get_quotes(["NVDA.US"], now=US_POST)["NVDA.US"]
        self.assertEqual(q["source"], "yahoo")
        self.assertEqual(q["sources_tried"], ["nasdaq✗", "yahoo"])
        self.assertEqual(q["status_label"], "12分钟前")

    def test_stale_source_falls_through_then_accepts_stale(self):
        old = at(2026, 11, 10, 20, 0)          # 盘后 1 小时 42 分钟前，超过 30 分钟
        raw = raw_quote(182.0, old, src="nasdaq", kind="post")
        with self.patched(nasdaq=fake({"NVDA.US": raw}), yahoo=fake({}), sina=fake({})), \
                mock.patch.object(Q, "daily_bars", return_value=BARS_US):
            q = Q.get_quotes(["NVDA.US"], now=US_POST)["NVDA.US"]
        self.assertTrue(q["stale"])
        self.assertEqual(q["sources_tried"], ["nasdaq", "yahoo✗", "sina✗"])

    def test_all_sources_fail(self):
        with self.patched(nasdaq=fake({}, "HTTP 403"), yahoo=fake({}, "timeout"), sina=fake({}, "HTTP 403")):
            q = Q.get_quotes(["NVDA.US"], now=US_POST)["NVDA.US"]
        self.assertIsNone(q["price"])
        self.assertIn("所有数据源均失败", q["error"])

    def test_night_without_feature_marks_unavailable(self):
        raw = raw_quote(182.35, at(2026, 11, 10, 21, 42), src="yahoo", kind="post")
        with self.patched(yahoo=fake({"NVDA.US": raw})), mock.patch.object(Q, "daily_bars", return_value=BARS_US):
            q = Q.get_quotes(["NVDA.US"], now=US_NIGHT)["NVDA.US"]
        self.assertTrue(q["night_unavailable"])
        self.assertEqual(q["prev_close"], 174.15)                 # 夜盘：前收盘=周二收盘
        self.assertEqual(q["sources_tried"], ["yahoo"])           # 功能没开启，不调用 Robinhood

    def test_night_with_feature_uses_robinhood(self):
        self.set_features(us_night=True)
        raw = raw_quote(182.5, at(2026, 11, 11, 2, 55), src="robinhood", kind="night", prev=174.15)
        with self.patched(robinhood=fake({"NVDA.US": raw})), mock.patch.object(Q, "daily_bars", return_value=BARS_US):
            q = Q.get_quotes(["NVDA.US"], now=US_NIGHT)["NVDA.US"]
        self.assertEqual(q["source"], "robinhood")
        self.assertEqual(q["price_kind"], "night")
        self.assertFalse(q["night_unavailable"])
        self.assertTrue(q["status_label"].startswith("夜盘参考价"))
        self.assertAlmostEqual(q["ext_change_pct"], (182.5 / 174.15 - 1) * 100, 4)

    def test_cn_uses_sina_prev_close_and_cross_check_flag(self):
        sina = raw_quote(1444.99, at(2026, 11, 10, 7, 0), src="sina", prev=1453.0, kind="regular", name="贵州茅台")
        yahoo = raw_quote(1460.0, at(2026, 11, 10, 7, 0), src="yahoo", kind="close")
        with self.patched(sina=fake({"600519.SH": sina}), yahoo=fake({"600519.SH": yahoo})):
            q = Q.get_quotes(["600519.SH"], now=CN_CLOSE, cross=True)["600519.SH"]
        self.assertEqual(q["source"], "sina")
        self.assertEqual(q["prev_close"], 1453.0)
        self.assertEqual(q["price_kind"], "close")
        self.assertEqual(q["status_label"], "收盘")
        self.assertTrue(q["cross"]["flag"])                       # 1.04% > 1%
        self.assertEqual(q["cross"]["source"], "yahoo")

    def test_holiday_hint(self):
        sina = raw_quote(1444.99, at(2026, 11, 9, 7, 0), src="sina", prev=1453.0, kind="regular")   # 数据停在昨天
        with self.patched(sina=fake({"600519.SH": sina}), yahoo=fake({})):
            q = Q.get_quotes(["600519.SH"], now=CN_CLOSE)["600519.SH"]
        self.assertTrue(q["holiday_hint"])
        self.assertEqual(q["status_label"], "今日可能休市")

    def test_hk_closing_auction(self):
        raw = raw_quote(418.4, at(2026, 11, 10, 8, 8), src="sina", prev=419.8, name="腾讯控股")
        with self.patched(sina=fake({"00700.HK": raw})):
            q = Q.get_quotes(["00700.HK"], now=HK_CLOSE)["00700.HK"]
        self.assertEqual(q["currency"], "HKD")
        self.assertAlmostEqual(q["day_change_pct"], (418.4 / 419.8 - 1) * 100, 4)

    def test_cache_and_series(self):
        raw = raw_quote(182.35, at(2026, 11, 10, 21, 40), src="nasdaq", kind="post")
        with self.patched(nasdaq=fake({"NVDA.US": raw})), mock.patch.object(Q, "daily_bars", return_value=BARS_US):
            qs = Q.get_quotes(["NVDA.US"], now=US_POST)
        Q.save_cache(qs)
        Q.append_series(qs)
        Q.append_series(qs)                                       # 同一时间点不重复追加
        self.assertIn("NVDA.US", Q.load_cache())
        self.assertEqual(len(C.load_json(C.path("state", "series.json"))["NVDA.US"]), 1)


def quote(sym="NVDA.US", price=182.35, prev=170.0, name="英伟达", market=None, cur=None, kind="post",
          sess="post", ext=None, t=None, **kw):
    m = market or C.market_of(sym)
    q = {"symbol": sym, "market": m, "name": name, "currency": cur or C.currency_of(sym), "price": price,
         "price_time": C.iso(t or US_POST), "price_kind": kind, "session": sess,
         "session_label": C.SESSION_LABEL.get(m, {}).get(sess, "已收盘"), "prev_close": prev,
         "day_change": price - prev, "day_change_pct": (price / prev - 1) * 100, "reg_close": None,
         "ext_change_pct": ext, "source": "nasdaq", "sources_tried": ["nasdaq"], "cross": None, "age_sec": 60,
         "stale": False, "holiday_hint": False, "night_unavailable": False, "status_label": "实时",
         "extra": {}, "error": None}
    q.update(kw)
    return q


RATES = {"ok": True, "to_base": {"CNY": 1.0, "USD": 7.2, "HKD": 0.92}, "pairs": {"USDCNY": 7.2, "HKDCNY": 0.92},
         "time": C.iso(US_POST), "source": "Yahoo", "stale": False}
POS = [{"symbol": "NVDA.US", "name": "英伟达", "shares": 100, "cost": 150.0, "currency": "USD", "aliases": ["Nvidia"]},
       {"symbol": "00700.HK", "name": "腾讯控股", "shares": 200, "cost": 380.0, "currency": "HKD", "aliases": ["腾讯"]},
       {"symbol": "600519.SH", "name": "贵州茅台", "shares": 10, "cost": None, "currency": "CNY", "aliases": ["茅台"]}]


class TestSnapshot(TempHome):
    def make(self):
        qs = {"NVDA.US": quote(),
              "00700.HK": quote("00700.HK", 418.4, 419.8, "腾讯控股", kind="close", sess="closed", status_label="收盘"),
              "600519.SH": quote("600519.SH", 1444.99, 1453.0, "贵州茅台", kind="close", sess="closed", status_label="收盘")}
        return S.build_snapshot(POS, qs, RATES, C.load_prefs(), US_POST)

    def test_totals_and_weights(self):
        s = self.make()
        v_nvda, v_tc, v_mt = 100 * 182.35 * 7.2, 200 * 418.4 * 0.92, 10 * 1444.99
        self.assertAlmostEqual(s["totals"]["value"], v_nvda + v_tc + v_mt, 2)
        by = {r["symbol"]: r for r in s["positions"]}
        self.assertAlmostEqual(by["NVDA.US"]["day_pnl_base"], 100 * (182.35 - 170.0) * 7.2, 2)
        self.assertAlmostEqual(sum(r["weight"] for r in s["positions"]), 100.0, 6)
        self.assertEqual(s["positions"][0]["symbol"], "NVDA.US")           # 按折算市值降序
        self.assertAlmostEqual(by["NVDA.US"]["pnl_pct"], (182.35 / 150 - 1) * 100, 4)
        self.assertIsNone(by["600519.SH"]["pnl"])                           # 没有成本，盈亏为空
        # 累计浮盈只统计有成本的两只
        want = 100 * (182.35 - 150) * 7.2 + 200 * (418.4 - 380) * 0.92
        self.assertAlmostEqual(s["totals"]["pnl"], want, 2)
        self.assertIn("600519.SH 未记录成本，盈亏显示“—”", s["notes"])

    def test_missing_quote_and_fx(self):
        qs = {"NVDA.US": quote(), "00700.HK": {"symbol": "00700.HK", "price": None, "error": "所有数据源均失败"}}
        s = S.build_snapshot(POS[:2], qs, {"ok": False, "to_base": {}}, C.load_prefs(), US_POST)
        self.assertTrue(s["totals"]["partial"])
        self.assertIn("汇率获取失败，总金额未折算", s["notes"])
        self.assertIsNone(s["totals"]["value"])

    def test_render_snapshot_text(self):
        s = self.make()
        text = R.render_snapshot(s, C.load_prefs())
        for must in ("📊 我的持仓", "总金额", "【美股·盘后】", "NVDA 英伟达 $182.35", "100股", "仓位", "数据：", "汇率"):
            self.assertIn(must, text)
        self.assertIn("🔴", text)                                  # 红涨绿跌（默认）
        self.set_prefs(color_scheme="green_up")
        self.assertIn("🟢+7.26%", R.render_snapshot(s, C.load_prefs()))

    def test_empty_snapshot_text(self):
        s = S.build_snapshot([], {}, RATES, C.load_prefs(), US_POST)
        self.assertIn("还没有记录持仓", R.render_snapshot(s, C.load_prefs()))

    def test_run_end_to_end_with_patches(self):
        h = H.load()
        H.buy(h, "NVDA.US", 100, 150.0, name="英伟达")
        H.save(h)
        with mock.patch.object(Q, "get_quotes", return_value={"NVDA.US": quote()}), \
                mock.patch.object(FX, "get_rates", return_value=RATES):
            snap = S.run(cross=False, now=US_POST)
        self.assertEqual(snap["positions"][0]["symbol"], "NVDA.US")
        self.assertIsNotNone(C.load_json(C.path("state", "last_snapshot.json")))
        p = S.write_space_outbox(snap)
        self.assertTrue(C.load_json(p)["needs_push"])


POS_NC = [dict(POS[0], cost=None)]       # 无成本：避免“相对成本盈亏”规则干扰其他规则的断言


class TestMonitorRules(TempHome):
    def quotes(self, pct_price):
        return {"NVDA.US": quote(price=pct_price, prev=170.0, ext=None)}

    def test_day_move_tiers_and_cooldown(self):
        st = {}
        pos = POS_NC
        rules = {"defaults": dict(M.DEFAULT_RULES)}
        # 5.5% → 第 1 档
        ev = M.evaluate(pos, self.quotes(170 * 1.055), rules, st, US_POST)
        self.assertEqual([e["type"] for e in ev], ["day_move"])
        k, v = ev[0]["_commit"]
        st["fired"][k] = v
        # 同一档再次出现：不重复
        self.assertEqual(M.evaluate(pos, self.quotes(170 * 1.056), rules, st, US_POST + dt.timedelta(minutes=5)), [])
        # 涨到 7.5%（第 2 档），但仍在 30 分钟冷却内：不推
        self.assertEqual(M.evaluate(pos, self.quotes(170 * 1.075), rules, st, US_POST + dt.timedelta(minutes=5)), [])
        # 冷却结束后：推第 2 档
        ev = M.evaluate(pos, self.quotes(170 * 1.075), rules, st, US_POST + dt.timedelta(minutes=31))
        self.assertEqual(len(ev), 1)
        # 一次跳两档（5%→9.5%）无视冷却
        st2 = {"fired": {k: v}}
        ev = M.evaluate(pos, self.quotes(170 * 1.095), rules, st2, US_POST + dt.timedelta(minutes=2))
        self.assertEqual(len(ev), 1)
        # 低于阈值不触发
        self.assertEqual(M.evaluate(pos, self.quotes(170 * 1.03), rules, {}, US_POST), [])

    def test_major_flag(self):
        rules = {"defaults": dict(M.DEFAULT_RULES)}
        ev = M.evaluate(POS_NC, self.quotes(170 * 1.11), rules, {}, US_POST)
        self.assertEqual(ev[0]["level"], "major")                 # ≥ 2 倍阈值
        ev = M.evaluate(POS_NC, self.quotes(170 * 1.06), rules, {}, US_POST, weights={"NVDA.US": 35})
        self.assertEqual(ev[0]["level"], "major")                 # 仓位 ≥ 20%
        ev = M.evaluate(POS_NC, self.quotes(170 * 1.06), rules, {}, US_POST)
        self.assertEqual(ev[0]["level"], "normal")

    def test_ext_move(self):
        q = quote(price=182.35, prev=170.0, ext=4.7)
        ev = M.evaluate(POS_NC, {"NVDA.US": q}, {"defaults": {"day_move_pct": 50, "ext_move_pct": 3}}, {}, US_POST)
        self.assertEqual([e["type"] for e in ev], ["ext_move"])
        self.assertIn("盘后涨幅", ev[0]["headline"])

    def test_price_levels_rearm(self):
        rules = {"defaults": {"day_move_pct": 50}, "per_symbol": {"NVDA.US": {"price_above": 180, "price_below": 150}}}
        st = {}
        ev = M.evaluate(POS_NC, self.quotes(181), rules, st, US_POST)
        self.assertEqual([e["type"] for e in ev], ["price_above"])
        k, v = ev[0]["_commit"]
        st.setdefault("fired", {})[k] = v
        self.assertEqual(M.evaluate(POS_NC, self.quotes(182), rules, st, US_POST), [])      # 不重复
        M.evaluate(POS_NC, self.quotes(178), rules, st, US_POST)                              # 回落超过 0.5% → 重新武装
        self.assertNotIn(k, st["fired"])
        ev = M.evaluate(POS_NC, self.quotes(149), rules, st, US_POST)
        self.assertEqual([e["type"] for e in ev], ["price_below"])

    def test_pnl_vs_cost(self):
        pos = [dict(POS[0], cost=150.0)]
        rules = {"defaults": {"day_move_pct": 50, "pnl_vs_cost_pct": [10, -10]}}
        ev = M.evaluate(pos, {"NVDA.US": quote(price=170.0, prev=169.0)}, rules, {}, US_POST)   # +13.3%
        self.assertEqual([e["type"] for e in ev], ["pnl_vs_cost"])
        ev = M.evaluate(pos, {"NVDA.US": quote(price=130.0, prev=131.0)}, rules, {}, US_POST)   # -13.3%
        self.assertEqual(len(ev), 1)
        self.assertIn("浮亏", ev[0]["headline"])
        self.assertEqual(M.evaluate([dict(POS[0], cost=None)], {"NVDA.US": quote(price=170.0, prev=169.0)}, rules, {}, US_POST), [])

    def test_skips_holiday_and_failed_quotes(self):
        rules = {"defaults": dict(M.DEFAULT_RULES)}
        q = quote(price=190.0, prev=170.0, holiday_hint=True)
        self.assertEqual(M.evaluate(POS_NC, {"NVDA.US": q}, rules, {}, US_POST), [])
        self.assertEqual(M.evaluate(POS_NC, {"NVDA.US": {"price": None, "error": "x"}}, rules, {}, US_POST), [])

    def test_limits_quiet_and_cap(self):
        rules = {"defaults": dict(M.DEFAULT_RULES)}
        evs = M.evaluate(POS_NC, self.quotes(170 * 1.06), rules, {}, US_POST) + \
            M.evaluate(POS_NC, self.quotes(170 * 1.12), rules, {}, US_POST)
        major = [e for e in evs if e["level"] == "major"]
        normal = [e for e in evs if e["level"] == "normal"]
        self.set_prefs(quiet_hours="05:00-09:00")                 # US_POST = 北京 05:42
        kept, dropped = M.apply_limits(evs, {}, C.load_prefs(), US_POST)
        self.assertEqual(len(kept), len(major))
        self.assertEqual(len(dropped), len(normal))
        self.set_prefs(daily_cap=0)
        kept, dropped = M.apply_limits(evs, {}, C.load_prefs(), at(2026, 11, 10, 12, 0))
        self.assertEqual(len(kept), len(major))                   # 达到上限后只放行重大事件

    def test_quiet_hours_wrap(self):
        self.set_prefs(quiet_hours="23:00-07:00")
        self.assertTrue(M.in_quiet(C.load_prefs(), at(2026, 11, 10, 17, 0)))       # 北京 01:00
        self.assertFalse(M.in_quiet(C.load_prefs(), at(2026, 11, 10, 2, 0)))       # 北京 10:00


class TestMonitorTick(TempHome):
    def setup_holdings(self):
        h = H.load()
        H.buy(h, "NVDA.US", 100, 150.0, name="英伟达")
        H.buy(h, "600519.SH", 10, 1500.0, name="贵州茅台")
        H.save(h)

    def run_tick(self, market, now, quotes, **kw):
        with mock.patch.object(Q, "get_quotes", return_value=quotes), \
                mock.patch.object(FX, "get_rates", return_value=RATES):
            return M.tick(market, now, **kw)

    def test_noop_cases(self):
        self.assertEqual(self.run_tick("US", US_POST, {})["status"], "no_positions")
        self.setup_holdings()
        self.assertEqual(self.run_tick("CN", CN_CLOSE, {})["status"], "not_active")           # 已收盘
        self.assertEqual(self.run_tick("US", US_NIGHT, {})["status"], "not_active")           # 夜盘功能未开启
        self.set_prefs(paused_until="2026-11-11T00:00:00Z")
        self.assertEqual(self.run_tick("US", US_POST, {})["status"], "paused")

    def test_alert_written_and_acked(self):
        self.setup_holdings()
        self.set_features()
        q = {"NVDA.US": quote(price=182.35, prev=170.0, ext=4.7)}
        res = self.run_tick("US", US_POST, q)
        self.assertEqual(res["status"], "action")
        names = [n for n in res["outbox"] if "alert" in n]
        self.assertEqual(len(names), 1)
        data = C.load_json(C.path("outbox", names[0]))
        self.assertIn("⚠️ NVDA 英伟达", data["draft"])
        self.assertIn("你的持仓：100股", data["draft"])
        self.assertIn(R.DISCLAIMER, data["draft"])                # 当天第一条带免责声明
        self.assertTrue(any("space_snapshot" in n for n in res["outbox"]))
        # 立刻再跑：被限频跳过；确认后再强制跑：同一档位不重复报警
        self.assertEqual(self.run_tick("US", US_POST + dt.timedelta(minutes=1), q)["status"], "throttled")
        for n in res["outbox"]:
            self.assertTrue(M.ack_file(n)[0])
        self.assertEqual(M.pending_files(), [])
        res2 = self.run_tick("US", US_POST + dt.timedelta(minutes=6), q)
        self.assertEqual([n for n in res2["outbox"] if "alert" in n], [])
        self.assertTrue(os.path.isdir(C.path("outbox", "sent")))

    def test_pending_outbox_is_reported_until_acked(self):
        self.setup_holdings()
        res = self.run_tick("US", US_POST, {"NVDA.US": quote(price=182.35, prev=170.0, ext=4.7)})
        later = self.run_tick("US", US_POST + dt.timedelta(minutes=10), {"NVDA.US": quote(price=171, prev=170.0, ext=0.5)})
        self.assertEqual(later["status"], "action")               # 上一批没确认，继续提醒

    def test_no_data_streak_notice(self):
        self.setup_holdings()
        bad = {"NVDA.US": {"symbol": "NVDA.US", "market": "US", "price": None, "error": "所有数据源均失败：HTTP 403"}}
        statuses = []
        for i in range(6):
            res = self.run_tick("US", US_POST + dt.timedelta(minutes=10 * i), bad)
            statuses.append(res["status"])
        self.assertEqual(statuses[:4], ["no_data"] * 4)
        self.assertEqual(statuses[4], "action")                    # 第 5 次连续失败：发一条异常通知
        notices = [n for n in M.pending_files() if "notice" in n]
        self.assertEqual(len(notices), 1)                          # 6 小时内不重复
        self.assertIn("连续 5 次获取失败", C.load_json(C.path("outbox", notices[0]))["draft"])
        self.assertFalse(any("space_snapshot" in n for n in M.pending_files()))   # 没数据不推空看板

    def test_cli_protocol_noop(self):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = M.main(["--market", "CN"])
        self.assertEqual(buf.getvalue().strip(), "NOOP")
        self.assertEqual(code, 0)

    def test_multi_market_arg(self):
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = M.main(["run", "--market", "CN,HK"])
        self.assertEqual((buf.getvalue().strip(), code), ("NOOP", 0))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = M.main(["run", "--market", "XX"])
        self.assertEqual(code, 2)

    def test_monitor_never_crashes(self):
        import io
        import contextlib
        self.setup_holdings()
        buf = io.StringIO()
        with mock.patch.object(M, "tick", side_effect=RuntimeError("boom")), contextlib.redirect_stdout(buf):
            M.main(["--market", "US"])
        self.assertEqual(buf.getvalue().strip(), "NOOP")


class TestNews(TempHome):
    P = {"symbol": "NVDA.US", "name": "NVIDIA", "aliases": ["Nvidia", "Jensen Huang", "黄仁勋"], "shares": 100, "cost": 150.0}

    def item(self, title, minutes_ago=10, source="Reuters", kind="news", form=None, date=None, id_=None, summary=""):
        t = None if kind == "filing" else C.iso(US_POST - dt.timedelta(minutes=minutes_ago))
        return {"id": id_ or N.hid("NVDA.US", title), "symbol": "NVDA.US", "kind": kind, "source": source, "title": title,
                "summary": summary, "time": t, "date": date, "url": "u", "form": form}

    def test_relevance_gate(self):
        p = self.P
        self.assertEqual(N.match_score("Nvidia beats earnings estimates", "", p), 2)
        self.assertEqual(N.match_score("NVDA shares jump", "", p), 2)
        self.assertEqual(N.match_score("Chip stocks rally", "Nvidia was among the gainers", p), 1)
        self.assertEqual(N.match_score("Apple unveils new iPhone", "", p), 0)
        self.assertEqual(N.match_score("黄仁勋谈 AI 需求", "", p), 2)
        short = {"symbol": "T.US", "name": "AT&T", "aliases": []}
        self.assertEqual(N.match_score("T-Mobile reports results", "", short), 0)       # 1 位代码不单独匹配

    def test_importance_gate(self):
        self.assertEqual(N.classify("Nvidia faces SEC investigation", "news"), "high")
        self.assertEqual(N.classify("Nvidia unveils new product", "news"), "mid")
        self.assertEqual(N.classify("3 top stocks to buy now including Nvidia", "news"), "low")
        self.assertEqual(N.classify("Nvidia stock moves", "news"), "low")
        self.assertEqual(N.classify("x", "filing", "8-K"), "high")
        self.assertEqual(N.classify("x", "filing", "S-3"), "mid")
        self.assertEqual(N.classify("关于回购股份的公告", "filing"), "high")
        self.assertEqual(N.classify("关于召开股东大会的通知", "filing"), "mid")

    def test_dedupe_gate(self):
        a = self.item("Nvidia CEO faces board investigation over trading", 30, "Yahoo", id_="a")
        b = self.item("Nvidia CEO faces board investigation over trades", 20, "Reuters", id_="b")
        c = self.item("Nvidia announces buyback of 50 billion", 5, "CNBC", id_="c")
        out = N.dedupe([a, b, c])
        self.assertEqual(len(out), 2)
        merged = [o for o in out if o["id"] == "b"][0]            # 保留权威来源（Reuters）
        self.assertEqual(merged["also"], 1)

    def test_process_realtime_filters(self):
        items = [self.item("Nvidia faces SEC investigation", 10, id_="1"),
                 self.item("Nvidia unveils new product", 10, id_="2"),                  # 中：实时不推
                 self.item("Apple faces SEC investigation", 10, id_="3"),               # 不相关
                 self.item("Nvidia faces SEC investigation again", 120, id_="4"),       # 过旧
                 self.item("3 top stocks to buy: Nvidia", 5, id_="5")]                  # 低
        out = N.process(items, [self.P], US_POST, "realtime", C.load_prefs(), None, {"NVDA.US": {"weight": 32.0, "day_change_pct": 4.8, "pnl_pct": 21.0}})
        self.assertEqual([o["id"] for o in out], ["1"])
        self.assertIn("仓位32%", out[0]["draft"])
        self.assertIn("关联：直接涉及你持有的 NVDA", out[0]["draft"])
        self.assertTrue(out[0]["needs_summary"])
        digest = N.process(items, [self.P], US_POST, "digest", C.load_prefs(), None, None)
        self.assertEqual(sorted(o["id"] for o in digest), ["2", "4"])     # 1 与 4 是同一事件，合并后保留较早的一条
        self.assertEqual([o["also"] for o in digest if o["id"] == "4"], [1])

    def test_filing_freshness_and_sent_and_cap(self):
        f = self.item("8-K：Current report", kind="filing", form="8-K", date="2026-11-10", id_="f1")
        old = self.item("8-K：Old report", kind="filing", form="8-K", date="2026-11-01", id_="f2")
        out = N.process([f, old], [self.P], US_POST, "realtime", C.load_prefs(), None, None)
        self.assertEqual([o["id"] for o in out], ["f1"])
        self.assertIn("2026-11-10 披露", out[0]["time_text"])
        sent = {"items": {"f1": "x"}, "counts": {}}
        self.assertEqual(N.process([f], [self.P], US_POST, "realtime", C.load_prefs(), sent, None), [])
        many = [self.item("Nvidia faces probe number %d into sales" % i, 5 + i, id_="m%d" % i) for i in range(6)]
        many = [dict(m, title="Nvidia probe %s %s" % ("alpha beta gamma delta"[i:i + 5], i * 7919)) for i, m in enumerate(many)]
        capped = N.process(many, [self.P], US_POST, "realtime", C.load_prefs(), None, None)
        self.assertLessEqual(len(capped), 3)                      # 每只每天最多 3 条实时推送

    def test_ack_prevents_repeat(self):
        it = self.item("Nvidia faces SEC investigation", 10, id_="z9")
        N._remember_pending([it])
        N.ack(["z9"], US_POST)
        self.assertIn("z9", N.load_sent()["items"])
        self.assertEqual(N.process([it], [self.P], US_POST, "realtime", C.load_prefs(), N.load_sent(), None), [])

    def test_find_reason(self):
        raw = [self.item("Nvidia unveils new product", 600, "Yahoo", id_="r1"),
               self.item("Nvidia faces SEC investigation", 300, "Reuters", id_="r2")]
        with mock.patch.object(N, "collect_symbol", return_value=raw):
            r = N.find_reason("NVDA.US", [self.P], US_POST, C.load_prefs(), {})
        self.assertEqual(r["level"], "high")
        self.assertIn("investigation", r["title"])
        with mock.patch.object(N, "collect_symbol", return_value=[]):
            self.assertIsNone(N.find_reason("NVDA.US", [self.P], US_POST, C.load_prefs(), {}))


class TestRenderAndReport(TempHome):
    def test_alert_render_has_reason_or_fallback(self):
        ev = M.evaluate(POS[:1], {"NVDA.US": quote(price=182.35, prev=170.0, ext=4.7)},
                        {"defaults": dict(M.DEFAULT_RULES)}, {}, US_POST)[0]
        ev.pop("_commit")
        text = R.render_alert(ev, C.load_prefs())
        self.assertIn("原因：暂未找到明确消息", text)
        ev["reason"] = {"title": "Nvidia unveils chip", "source": "Reuters", "time_text": "11-10 20:30"}
        self.assertIn("原因：Nvidia unveils chip（Reuters · 11-10 20:30）", R.render_alert(ev, C.load_prefs()))
        self.assertIn("16:42 ET（北京 05:42）", text)

    def test_report_build_and_cli(self):
        h = H.load()
        H.buy(h, "NVDA.US", 100, 150.0, name="英伟达")
        H.save(h)
        self.set_features()
        qs = {"NVDA.US": quote()}
        with mock.patch.object(Q, "get_quotes", return_value=qs), mock.patch.object(FX, "get_rates", return_value=RATES):
            rep = RP.build_report("US", "post", now=US_POST, cross=False)
        self.assertEqual(rep["key_moves"][0]["symbol"], "NVDA.US")
        d = rep["draft"]
        for must in ("📋 美股盘后日报", "【账户总览】", "【美股持仓明细】", "【重点异动】", "暂未找到明确消息", "【数据说明】", R.DISCLAIMER):
            self.assertIn(must, d)
        self.assertIsNone(RP.build_report("CN", "close", now=CN_CLOSE, cross=False))       # 该市场无持仓
        import io
        import contextlib
        buf = io.StringIO()
        with mock.patch.object(Q, "get_quotes", return_value=qs), mock.patch.object(FX, "get_rates", return_value=RATES), \
                mock.patch.object(C, "now_utc", return_value=US_POST), contextlib.redirect_stdout(buf):
            RP.main(["--market", "US", "--kind", "post"])
        lines = buf.getvalue().strip().splitlines()
        self.assertEqual(lines[0], "ACTION")
        name = json.loads(lines[1])["outbox"][0]
        self.assertTrue(M.ack_file(name)[0])

    def test_report_skips_holiday(self):
        h = H.load()
        H.buy(h, "NVDA.US", 100, 150.0, name="英伟达")
        H.save(h)
        qs = {"NVDA.US": quote(holiday_hint=True)}
        import io
        import contextlib
        buf = io.StringIO()
        with mock.patch.object(Q, "get_quotes", return_value=qs), mock.patch.object(FX, "get_rates", return_value=RATES), \
                contextlib.redirect_stdout(buf):
            RP.main(["--market", "US", "--kind", "post"])
        self.assertEqual(buf.getvalue().strip(), "NOOP")


if __name__ == "__main__":
    unittest.main()
