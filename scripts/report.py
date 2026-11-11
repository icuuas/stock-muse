#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日报（盘前 / 盘后 / 收盘）。生成内容写入 outbox，由 Muse 按当前渠道排版发送。

用法：
  report.py --market US --kind pre|post
  report.py --market CN --kind close
  report.py --market HK --kind close
  report.py --market ALL --kind close        # 日报模式为 merged 时使用
  加 --force 可在疑似休市时也生成。

输出：NOOP（无持仓或疑似休市）或 ACTION + {"outbox":[文件名]}；发送成功后执行 monitor.py ack <文件名>。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402


def build_report(market, kind, now=None, cross=True, key_move_min_pct=2.0):
    import holdings
    import news as N
    import render
    import snapshot as S

    now = now or C.now_utc()
    prefs = C.load_prefs()
    feats = C.features()
    allpos = holdings.load()["positions"]
    pos = allpos if market == "ALL" else [p for p in allpos if C.market_of(p["symbol"]) == market]
    if not pos:
        return None
    snap = S.run(cross=cross, now=now)
    rows = [r for r in snap["positions"] if market == "ALL" or r["market"] == market]
    holiday_all = bool(rows) and all(r.get("holiday_hint") for r in rows)
    # 重点异动：按对账户的影响金额排序，取前 3，且涨跌幅不小于阈值
    movers = [r for r in rows if r.get("day_change_pct") is not None and abs(r["day_change_pct"]) >= key_move_min_pct]
    movers.sort(key=lambda r: -abs(r.get("day_pnl_base") or 0))
    moves = []
    for r in movers[:3]:
        reason = None
        try:
            reason = N.find_reason(r["symbol"], allpos, now, prefs, feats)
        except Exception:
            reason = None
        moves.append({"symbol": r["symbol"], "pct": r["day_change_pct"], "reason": reason})
    news_items = []
    if prefs["enable"].get("news", True):
        try:
            news_items = N.check(pos, now, "digest", prefs, feats)
        except Exception as e:
            C.log("report_news_error", error=str(e))
    rep = {"kind": kind, "market": market, "snapshot": snap, "key_moves": moves,
           "news": news_items, "holiday_all": holiday_all}
    rep["draft"] = render.render_report(rep, prefs)
    rep["ids"] = [i["id"] for i in news_items]
    return rep


def main(argv=None):
    ap = argparse.ArgumentParser(description="日报")
    ap.add_argument("--market", required=True, choices=["US", "HK", "CN", "ALL"])
    ap.add_argument("--kind", required=True, choices=["pre", "post", "close"])
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    C.ensure_dirs()
    import monitor
    try:
        rep = build_report(a.market, a.kind)
    except Exception as e:
        C.log("report_error", error="%s: %s" % (type(e).__name__, e))
        print("NOOP")
        return 0
    if rep is None:
        print("NOOP")
        return 0
    if rep["holiday_all"] and not a.force:
        C.log("report_skipped_holiday", market=a.market)
        print("NOOP")
        return 0
    payload = {"kind": a.kind, "draft": rep["draft"], "ids": rep["ids"], "report": {
        "key_moves": rep["key_moves"], "news": rep["news"], "snapshot": rep["snapshot"]}}
    name = monitor.write_outbox("report", a.market, payload, C.now_utc())
    print("ACTION")
    print(json.dumps({"outbox": [name]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
