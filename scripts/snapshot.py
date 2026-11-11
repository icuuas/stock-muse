#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""持仓快照：取行情 + 汇率，计算市值、盈亏、仓位，输出 JSON 或可直接发送的文本草稿。

用法：
  snapshot.py                 # 文本草稿（模板 A 的内容顺序）
  snapshot.py --json          # 完整 JSON
  snapshot.py --no-cross      # 不做第二数据源交叉核对（更快）
  snapshot.py --space         # 同时写 outbox/space_snapshot.json，供 Muse 推送到 Space
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402


def _sum(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) if vals else None


def build_snapshot(positions, quotes, fx, prefs, now):
    base = prefs.get("base_currency", "CNY")
    to_base = (fx or {}).get("to_base") or {}
    rows, notes, missing = [], [], []
    for p in positions:
        sym = p["symbol"]
        q = quotes.get(sym) or {}
        cur = p.get("currency") or C.currency_of(sym)
        price, prev = q.get("price"), q.get("prev_close")
        shares, cost = p["shares"], p.get("cost")
        value = shares * price if price else None
        day_pnl = shares * (price - prev) if (price and prev) else None
        cost_total = shares * cost if cost is not None else None
        pnl = (value - cost_total) if (value is not None and cost_total is not None) else None
        pnl_pct = ((price / cost - 1) * 100) if (price and cost) else None
        conv = 1.0 if cur == base else to_base.get(cur)
        row = {
            "symbol": sym, "name": q.get("name") or p.get("name") or sym, "market": C.market_of(sym),
            "currency": cur, "shares": shares, "cost": cost,
            "price": price, "price_time": q.get("price_time"), "price_kind": q.get("price_kind"),
            "status_label": q.get("status_label"), "session_label": q.get("session_label"),
            "prev_close": prev, "day_change_pct": q.get("day_change_pct"), "ext_change_pct": q.get("ext_change_pct"),
            "value": value, "day_pnl": day_pnl, "cost_total": cost_total, "pnl": pnl, "pnl_pct": pnl_pct,
            "value_base": value * conv if (value is not None and conv) else None,
            "day_pnl_base": day_pnl * conv if (day_pnl is not None and conv) else None,
            "cost_base": cost_total * conv if (cost_total is not None and conv) else None,
            "pnl_base": pnl * conv if (pnl is not None and conv) else None,
            "source": q.get("source"), "cross": q.get("cross"), "stale": q.get("stale", False),
            "holiday_hint": q.get("holiday_hint", False), "from_cache": q.get("from_cache", False),
            "weight": None,
        }
        rows.append(row)
        if not price:
            missing.append(sym)
            notes.append("%s 暂无行情：%s" % (sym, q.get("error") or "未取到数据"))
            continue
        if conv is None:
            missing.append(sym)
            notes.append("%s 缺少 %s→%s 汇率，未计入总金额" % (sym, cur, base))
        if q.get("stale"):
            notes.append("%s 数据时间较旧（%s）" % (sym, q.get("status_label")))
        if q.get("holiday_hint"):
            notes.append("%s 所在市场今日可能休市，显示的是上一交易日数据" % sym)
        cr = q.get("cross")
        if cr and cr.get("flag"):
            notes.append("%s 两个数据源价格相差 %.2f%%（%s 对比）" % (sym, cr["diff_pct"], cr["source"]))
        if q.get("night_unavailable"):
            notes.append("%s 夜盘数据暂不可用，显示最近成交价" % sym)
        if cost is None:
            notes.append("%s 未记录成本，盈亏显示“—”" % sym)
    total_val = _sum(r["value_base"] for r in rows)
    for r in rows:
        r["weight"] = (r["value_base"] / total_val * 100) if (total_val and r["value_base"] is not None) else None
    rows.sort(key=lambda r: (r["value_base"] is None, -(r["value_base"] or 0)))
    day_pnl = _sum(r["day_pnl_base"] for r in rows)
    day_base_val = _sum(r["value_base"] - r["day_pnl_base"] for r in rows
                        if r["value_base"] is not None and r["day_pnl_base"] is not None)
    cost_known = [r for r in rows if r["cost_base"] is not None and r["pnl_base"] is not None]
    pnl = _sum(r["pnl_base"] for r in cost_known)
    cost_sum = _sum(r["cost_base"] for r in cost_known)
    totals = {
        "value": total_val, "day_pnl": day_pnl,
        "day_pct": (day_pnl / day_base_val * 100) if (day_pnl is not None and day_base_val) else None,
        "pnl": pnl, "pnl_pct": (pnl / cost_sum * 100) if (pnl is not None and cost_sum) else None,
        "partial": bool(missing), "missing": missing,
    }
    if fx is not None and not fx.get("ok", True):
        notes.append("汇率获取失败，总金额未折算")
    elif fx and fx.get("stale"):
        notes.append("汇率使用了缓存数据（Yahoo，非最新）")
    markets = {}
    for m in C.MARKET_ORDER:
        ms = [r for r in rows if r["market"] == m]
        if not ms:
            continue
        times = [C.parse_iso(r["price_time"]) for r in ms if r["price_time"]]
        markets[m] = {
            "label": C.MARKET_LABEL[m], "session_label": C.session_label(m, now),
            "latest_time": C.iso(max(times)) if times else None,
            "sources": sorted({r["source"] for r in ms if r["source"]}),
            "status_labels": sorted({r["status_label"] for r in ms if r["status_label"]}),
        }
    return {"generated": C.iso(now), "base_currency": base, "positions": rows, "markets": markets,
            "totals": totals, "notes": notes,
            "fx": {"pairs": (fx or {}).get("pairs"), "to_base": (fx or {}).get("to_base"), "time": (fx or {}).get("time"),
                   "source": (fx or {}).get("source", "Yahoo"), "ok": (fx or {}).get("ok", False),
                   "stale": (fx or {}).get("stale", False)}}


def fill_from_cache(symbols, quotes):
    """未取到行情的标的，用 state/quotes_cache.json 里的最近一次数据补上，并标记 from_cache。"""
    import quotes as Q
    cache = Q.load_cache()
    out = dict(quotes)
    for s in symbols:
        if not (out.get(s) or {}).get("price") and cache.get(s):
            q = dict(cache[s])
            q["from_cache"] = True
            out[s] = q
    return out


def run(cross=True, now=None):
    import fx as FX
    import holdings
    import quotes as Q
    now = now or C.now_utc()
    prefs = C.load_prefs()
    positions = holdings.load()["positions"]
    syms = [p["symbol"] for p in positions]
    quotes = Q.get_quotes(syms, now=now, cross=cross) if syms else {}
    quotes = fill_from_cache(syms, quotes)
    Q.save_cache({s: q for s, q in quotes.items() if not q.get("from_cache")})
    Q.append_series(quotes)
    fx = FX.get_rates(prefs["base_currency"], now=now) if positions else {"ok": True, "to_base": {}}
    snap = build_snapshot(positions, quotes, fx, prefs, now)
    C.save_json(C.path("state", "last_snapshot.json"), snap)
    return snap


def write_space_outbox(snap):
    import quotes as Q
    ser = C.load_json(C.path("state", "series.json"), {}) or {}
    payload = {"type": "space_snapshot", "needs_push": True, "created": snap["generated"],
               "snapshot": snap, "series": {s: pts[-60:] for s, pts in ser.items()}}
    p = C.path("outbox", "space_snapshot.json")
    C.save_json(p, payload)
    return p


def main(argv=None):
    ap = argparse.ArgumentParser(description="持仓快照")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-cross", action="store_true")
    ap.add_argument("--space", action="store_true")
    a = ap.parse_args(argv)
    C.ensure_dirs()
    snap = run(cross=not a.no_cross)
    if a.space:
        write_space_outbox(snap)
    if a.json:
        print(json.dumps(snap, ensure_ascii=False, indent=2, default=str))
    else:
        import render
        print(render.render_snapshot(snap, C.load_prefs()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
