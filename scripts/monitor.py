#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定时监测（给 cron 调用）。每次运行：判断该市场是否在交易 → 取行情 → 判断规则 →
把需要发送的内容写进 outbox/ → 输出一行结果。

输出协议（Muse 只需看第一行）：
  NOOP                      没有任何事要做：不要回复、不要做任何其他事，立即结束
  ACTION                    有待发内容；第二行是 JSON：{"outbox":[文件...]}
                            对每个文件：读取 draft（或 items/snapshot），按当前渠道排版发送；
                            发送成功后执行：monitor.py ack <文件名>

用法：
  monitor.py --market US|CN|HK [--force]
  monitor.py ack <outbox 文件名>
  monitor.py pending
"""
import argparse
import datetime as dt
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

ACTIVE = {"US": ("pre", "regular", "post", "night"), "CN": ("regular",), "HK": ("regular", "closing")}
DEFAULT_RULES = {"day_move_pct": 5, "ext_move_pct": 3, "pnl_vs_cost_pct": [10, -10]}
NEWS_MINUTES = {"realtime": 10, "balanced": 15, "eco": 30}
EXT_NAME = {"pre": "盘前", "post": "盘后", "night": "夜盘"}


# ----------------------------------------------------------------- 工具
def min_interval(sess, prefs):
    mm = C.MODE_MINUTES.get(prefs.get("monitor_mode", "balanced"), C.MODE_MINUTES["balanced"])
    if sess == "night":
        return mm["night"]
    if sess in ("pre", "post"):
        return mm["ext"]
    return mm["regular"]


def in_quiet(prefs, now):
    q = prefs.get("quiet_hours")
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$", q or "")
    if not m:
        return False
    a, b = int(m.group(1)) * 60 + int(m.group(2)), int(m.group(3)) * 60 + int(m.group(4))
    try:
        u = now.astimezone(C.ZoneInfo(prefs.get("user_timezone", "Asia/Shanghai")))
    except Exception:
        u = now
    t = u.hour * 60 + u.minute
    return (a <= t < b) if a < b else (t >= a or t < b)


def thr(rules, sym, key):
    per = (rules.get("per_symbol") or {}).get(sym) or {}
    if key in per:
        return per[key]
    return (rules.get("defaults") or {}).get(key, DEFAULT_RULES.get(key))


def _should_fire(fired, key, tier, now):
    prev = fired.get(key)
    if prev is None:
        return True
    if tier <= prev["tier"]:
        return False
    since = (now - C.parse_iso(prev["ts"])).total_seconds()
    return since >= 1800 or tier - prev["tier"] >= 2


def _position_view(p, q):
    price, prev, cost = q["price"], q.get("prev_close"), p.get("cost")
    return {"shares": p["shares"], "cost": cost, "value": p["shares"] * price,
            "pnl": None if cost is None else p["shares"] * (price - cost),
            "pnl_pct": None if cost is None else (price / cost - 1) * 100,
            "day_pnl": None if prev is None else p["shares"] * (price - prev)}


def _event(p, q, typ, headline, major, pct, commit, impact):
    return {"id": "%s|%s|%s" % (p["symbol"], typ, q.get("price_time")), "symbol": p["symbol"],
            "name": q.get("name") or p.get("name") or p["symbol"], "market": q["market"], "type": typ,
            "headline": headline, "level": "major" if major else "normal", "price": q["price"],
            "currency": q["currency"], "pct": pct, "session": q.get("session"),
            "session_label": q.get("session_label"),
            "price_time": q.get("price_time"), "source": q.get("source"), "status_label": q.get("status_label"),
            "position": _position_view(p, q), "reason": None, "impact": impact, "_commit": commit}


# ----------------------------------------------------------------- 规则判断
def evaluate(positions, quotes, rules, st, now, weights=None, to_base=None):
    """返回候选事件；不修改“已触发”状态，只在事件的 _commit 里记录要写入的内容。
    价格再次回到阈值之内的“重新武装”会直接修改 st。"""
    weights = weights or {}
    to_base = to_base or {}
    fired = st.setdefault("fired", {})
    events = []
    for p in positions:
        sym = p["symbol"]
        q = quotes.get(sym) or {}
        if not q.get("price") or q.get("error") or q.get("holiday_hint"):
            continue
        # Fix (2026-11-12): never alert on a quote with no timestamp or a stale
        # quote, and never alert when a second source disagrees by >3%. Before
        # this gate, a Nasdaq pre-market quote with price_time=None ("time
        # unknown") fired a +10.43% alert at a stale price while another source
        # showed a materially different price.
        if q.get("time_unknown") or q.get("stale"):
            continue
        if q.get("age_sec") is not None and q["age_sec"] > 3600:  # hard cap (2026-11-12): a quote older than 60 minutes is never used for an alert
            continue
        _cross = q.get("cross") or {}
        if _cross.get("ok") and _cross.get("diff_pct") is not None and _cross["diff_pct"] > 3:
            continue
        sd = C.session_date(q["market"], now).isoformat()
        impact = abs((q.get("day_change") or 0) * p["shares"]) * (1.0 if q["currency"] == "CNY" else (to_base.get(q["currency"]) or 1.0))
        heavy = (weights.get(sym) or 0) >= 20

        pct, t = q.get("day_change_pct"), thr(rules, sym, "day_move_pct")
        if pct is not None and t and abs(pct) >= t:
            sign = "+" if pct > 0 else "-"
            tier = int((abs(pct) - t) // 2) + 1
            key = "%s|day|%s|%s" % (sym, sign, sd)
            if _should_fire(fired, key, tier, now):
                events.append(_event(p, q, "day_move", "日内%s %s" % ("涨幅" if pct > 0 else "跌幅", C.fmt_pct(pct)),
                                     abs(pct) >= 2 * t or heavy, pct, (key, {"tier": tier, "ts": C.iso(now)}), impact))

        ep, t = q.get("ext_change_pct"), thr(rules, sym, "ext_move_pct")
        if ep is not None and t and q.get("price_kind") in EXT_NAME and abs(ep) >= t:
            sign = "+" if ep > 0 else "-"
            tier = int((abs(ep) - t) // 2) + 1
            key = "%s|ext|%s|%s" % (sym, sign, sd)
            if _should_fire(fired, key, tier, now):
                events.append(_event(p, q, "ext_move", "%s%s %s（较收盘）" % (EXT_NAME[q["price_kind"]], "涨幅" if ep > 0 else "跌幅", C.fmt_pct(ep)),
                                     abs(ep) >= 2 * t or heavy, pct, (key, {"tier": tier, "ts": C.iso(now)}), impact))

        per = (rules.get("per_symbol") or {}).get(sym) or {}
        for typ, label, ok in (("price_above", "突破", lambda a, b: a >= b), ("price_below", "跌破", lambda a, b: a <= b)):
            lvl = per.get(typ)
            if lvl is None:
                continue
            key = "%s|%s|%s" % (sym, typ, lvl)
            if ok(q["price"], lvl):
                if key not in fired:
                    events.append(_event(p, q, typ, "%s %s" % (label, C.fmt_price(lvl, q["currency"])), False, pct,
                                         (key, {"tier": 1, "ts": C.iso(now)}), impact))
            elif key in fired and ((typ == "price_above" and q["price"] < lvl * 0.995) or (typ == "price_below" and q["price"] > lvl * 1.005)):
                del fired[key]

        cost = p.get("cost")
        if cost:
            pp = (q["price"] / cost - 1) * 100
            for th in (thr(rules, sym, "pnl_vs_cost_pct") or []):
                key = "%s|pnl|%s" % (sym, "+" if th > 0 else "-")
                hit = pp >= th if th > 0 else pp <= th
                if hit:
                    tier = int((abs(pp) - abs(th)) // 5) + 1
                    prev = fired.get(key)
                    if prev is None or tier > prev["tier"]:
                        events.append(_event(p, q, "pnl_vs_cost", "浮%s已达 %s（超过 %s%%）" % ("盈" if th > 0 else "亏", C.fmt_pct(pp, 1), abs(th)),
                                             False, pct, (key, {"tier": tier, "ts": C.iso(now)}), impact))
                elif key in fired and ((th > 0 and pp < th - 2) or (th < 0 and pp > th + 2)):
                    del fired[key]
    return events


def apply_limits(events, st, prefs, now):
    """免打扰、每日上限：超出的普通事件被压下，重大事件始终放行。返回 (保留, 压下)。"""
    quiet = in_quiet(prefs, now)
    cap = int(prefs.get("daily_cap", 15))
    kept, dropped = [], []
    for e in sorted(events, key=lambda x: -x["impact"]):
        if e["level"] != "major" and (quiet or st.get("count", 0) + len(kept) >= cap):
            dropped.append(e)
        else:
            kept.append(e)
    return kept, dropped


def _clean_state(st, now, prefs):
    today = now.astimezone(C.ZoneInfo(prefs.get("user_timezone", "Asia/Shanghai"))).date().isoformat()
    if st.get("date") != today:
        st["date"], st["count"] = today, 0
    cutoff = (now - dt.timedelta(days=3)).date().isoformat()
    st["fired"] = {k: v for k, v in (st.get("fired") or {}).items()
                   if not re.search(r"\|\d{4}-\d{2}-\d{2}$", k) or k.rsplit("|", 1)[1] >= cutoff}
    return today


# ----------------------------------------------------------------- outbox
def outbox_dir():
    return C.path("outbox")


def pending_files():
    d = outbox_dir()
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d) if f.endswith(".json") and os.path.isfile(os.path.join(d, f)))


def write_outbox(kind, market, payload, now):
    name = "%s-%s-%s.json" % (now.strftime("%Y%m%dT%H%M%SZ"), kind, market)
    payload.update({"type": kind, "market": market, "created": C.iso(now), "acked": False})
    C.save_json(os.path.join(outbox_dir(), name), payload)
    return name


def ack_file(name, now=None):
    now = now or C.now_utc()
    p = os.path.join(outbox_dir(), os.path.basename(name))
    data = C.load_json(p, None)
    if data is None:
        return False, "找不到 %s" % name
    if data.get("type") in ("news", "report"):
        import news
        news.ack(data.get("ids") or [], now)
    if data.get("type") == "space_snapshot":
        ms = C.load_json(C.path("state", "monitor_state.json"), {}) or {}
        ms["last_space_push"] = C.iso(now)
        C.save_json(C.path("state", "monitor_state.json"), ms)
    sent = os.path.join(outbox_dir(), "sent")
    os.makedirs(sent, exist_ok=True)
    os.replace(p, os.path.join(sent, os.path.basename(name)))
    cutoff = now.timestamp() - 3 * 86400
    for f in os.listdir(sent):
        fp = os.path.join(sent, f)
        if os.path.getmtime(fp) < cutoff:
            os.remove(fp)
    return True, "已确认"


# ----------------------------------------------------------------- 主流程
def tick(market, now=None, force=False):
    import fx as FX
    import holdings
    import news as N
    import quotes as Q
    import render
    import snapshot as S

    now = now or C.now_utc()
    prefs = C.load_prefs()
    feats = C.features()
    result = {"status": "noop", "outbox": []}
    pend = pending_files()

    def finish(status):
        result["status"] = status
        result["outbox"] = pending_files()
        return result

    pu = prefs.get("paused_until")
    if pu and C.parse_iso(pu) and now < C.parse_iso(pu):
        return finish("paused")
    if not prefs["enable"].get("alerts", True):
        return finish("alerts_disabled")
    positions_all = holdings.load()["positions"]
    positions = [p for p in positions_all if C.market_of(p["symbol"]) == market]
    if not positions:
        return finish("no_positions")
    sess = C.session(market, now, night=bool(feats.get("us_night") and prefs["enable"].get("night", True)))
    if sess not in ACTIVE[market]:
        return finish("not_active")
    ms = C.load_json(C.path("state", "monitor_state.json"), {}) or {}
    last = C.parse_iso((ms.get("last_run") or {}).get(market))
    if not force and last and (now - last).total_seconds() < 0.8 * 60 * min_interval(sess, prefs):
        return finish("throttled")
    ms.setdefault("last_run", {})[market] = C.iso(now)

    quotes = Q.get_quotes([p["symbol"] for p in positions], now=now, cross=True)  # Fix (2026-11-12): cross-check on, so evaluate() can gate on source disagreement
    # 行情全部失败：不推空看板；连续失败 5 次发一条异常通知（6 小时内不重复）
    fs = ms.setdefault("fail_streak", {})
    if not any(q.get("price") for q in quotes.values()):
        fs[market] = fs.get(market, 0) + 1
        notified = C.parse_iso((ms.get("fail_notice") or {}).get(market))
        if fs[market] >= 5 and (not notified or (now - notified).total_seconds() > 6 * 3600):
            ms.setdefault("fail_notice", {})[market] = C.iso(now)
            err = next((q.get("error") for q in quotes.values() if q.get("error")), "") or ""
            write_outbox("notice", market, {"draft": "⚠️ %s行情已连续 %d 次获取失败，暂时无法监测异动。原因：%s\n恢复后会自动继续。"
                                                 % (C.MARKET_LABEL[market], fs[market], err[:160])}, now)
        C.log("monitor_no_data", market=market, streak=fs[market])
        C.save_json(C.path("state", "monitor_state.json"), ms)
        return finish("action" if pending_files() else "no_data")
    fs[market] = 0
    Q.save_cache(quotes)
    Q.append_series(quotes)
    ctx = N.context_from_snapshot()
    weights = {s: (v.get("weight") or 0) for s, v in ctx.items()}
    rules = C.load_json(C.path("data", "rules.json"), {}) or {}
    rules.setdefault("defaults", dict(DEFAULT_RULES))
    fxr = FX.get_rates(prefs["base_currency"], now=now, max_age=1800)
    to_base = (fxr or {}).get("to_base") or {}

    st = C.load_json(C.path("state", "alerts_state.json"), {}) or {}
    today = _clean_state(st, now, prefs)
    events = evaluate(positions, quotes, rules, st, now, weights, to_base)
    kept, dropped = apply_limits(events, st, prefs, now)
    for e in kept:
        k, v = e["_commit"]
        st["fired"][k] = v
        st["count"] = st.get("count", 0) + 1
        if feats.get("us_news") or feats.get("cn_announce") or feats.get("cn_hk_news") or feats.get("edgar"):
            try:
                e["reason"] = N.find_reason(e["symbol"], positions, now, prefs, feats)
            except Exception:
                e["reason"] = None
    # Fix (2026-11-12): at most one alert block per symbol per message. Before
    # this, one tick could stack day_move + ext_move + pnl_vs_cost for the same
    # stock (the same symbol appeared 3x in a single message at the same
    # price). All triggers above are already committed to state, so suppressed
    # ones do not leak out as a second message on the next tick.
    if len(kept) > 1:
        _prio = {"day_move": 0, "ext_move": 1, "price_above": 2, "price_below": 2, "pnl_vs_cost": 3}
        _best = {}
        for e in kept:
            _cur = _best.get(e["symbol"])
            if _cur is None or _prio.get(e["type"], 9) < _prio.get(_cur["type"], 9):
                _best[e["symbol"]] = e
        if len(_best) < len(kept):
            C.log("alerts_deduped_per_symbol", before=len(kept), after=len(_best))
            kept = [e for e in kept if _best.get(e["symbol"]) is e]
    for e in kept + dropped:
        e.pop("_commit", None)
    if dropped:
        C.log("alerts_suppressed", count=len(dropped))
    if kept:
        first = st.get("disclaimer_date") != today
        st["disclaimer_date"] = today
        draft = render.render_alerts(kept, prefs, first_of_day=first)
        write_outbox("alert", market, {"draft": draft, "events": kept, "count": len(kept)}, now)
    C.save_json(C.path("state", "alerts_state.json"), st)

    # 信息推送（只推和持仓对得上的）
    if prefs["enable"].get("news", True) and any(feats.get(k) for k in ("us_news", "edgar", "cn_announce", "cn_hk_news")):
        lastn = C.parse_iso((ms.get("last_news") or {}).get(market))
        every = NEWS_MINUTES.get(prefs.get("monitor_mode", "balanced"), 15)
        if force or not lastn or (now - lastn).total_seconds() >= every * 60:
            ms.setdefault("last_news", {})[market] = C.iso(now)
            items = N.check(positions, now, "realtime", prefs, feats)
            if items:
                write_outbox("news", market, {"draft": "\n\n".join(i["draft"] for i in items),
                                              "items": items, "ids": [i["id"] for i in items]}, now)

    # Space 看板刷新（有节制）
    if prefs["enable"].get("snapshot", True):
        lasts = C.parse_iso(ms.get("last_space_gen"))
        every = max(int(prefs.get("space_refresh_minutes", 10)), 1)
        if force or not lasts or (now - lasts).total_seconds() >= every * 60:
            ms["last_space_gen"] = C.iso(now)
            allq = S.fill_from_cache([p["symbol"] for p in positions_all], quotes)
            snap = S.build_snapshot(positions_all, allq, fxr, prefs, now)
            C.save_json(C.path("state", "last_snapshot.json"), snap)
            S.write_space_outbox(snap)
    C.save_json(C.path("state", "monitor_state.json"), ms)
    return finish("action" if pending_files() else "noop")


def main(argv=None):
    ap = argparse.ArgumentParser(description="定时监测")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run")
    r.add_argument("--market", required=True, help="US / CN / HK，可用逗号同时跑多个，如 CN,HK（共用一个定时任务，省唤醒次数）")
    r.add_argument("--force", action="store_true")
    k = sub.add_parser("ack")
    k.add_argument("name")
    sub.add_parser("pending")
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0].startswith("--"):      # 兼容 monitor.py --market US
        argv.insert(0, "run")
    a = ap.parse_args(argv)
    C.ensure_dirs()
    if a.cmd == "ack":
        ok, msg = ack_file(a.name)
        print(json.dumps({"ok": ok, "message": msg}, ensure_ascii=False))
        return 0 if ok else 1
    if a.cmd == "pending":
        print(json.dumps({"pending": pending_files()}, ensure_ascii=False))
        return 0
    if a.cmd != "run":
        ap.print_help()
        return 2
    markets = [m.strip().upper() for m in a.market.split(",") if m.strip()]
    if not markets or any(m not in ACTIVE for m in markets):
        print("NOOP")
        C.log("monitor_bad_market", value=a.market)
        return 2
    res = {"outbox": []}
    for m in markets:
        try:
            res = tick(m, force=a.force)
        except Exception as e:  # 任何意外都不能让 cron 空转报错：记录日志，保持安静
            C.log("monitor_error", error="%s: %s" % (type(e).__name__, e))
    res["outbox"] = pending_files()
    if res["outbox"]:
        print("ACTION")
        print(json.dumps({"outbox": res["outbox"]}, ensure_ascii=False))
    else:
        print("NOOP")
    return 0


if __name__ == "__main__":
    sys.exit(main())
