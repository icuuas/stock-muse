#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""持仓记录：买入、卖出、覆盖、清仓、撤销、查看。所有输出均为 JSON（含中文 message，可直接回显给用户）。

用法：
  holdings.py normalize NVDA                      # 规范化代码
  holdings.py buy NVDA 100 180 [--fee 1] [--name 英伟达]
  holdings.py sell NVDA 50 190 [--fee 1]
  holdings.py set NVDA 200 [--cost 165] [--name 英伟达]   # 覆盖数量与成本
  holdings.py remove NVDA
  holdings.py aliases NVDA "Nvidia,黄仁勋,Jensen Huang"
  holdings.py undo                                # 恢复到上一版（仅一步）
  holdings.py show
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402


class HoldingsError(Exception):
    pass


def hpath():
    return C.path("data", "holdings.json")


def load():
    h = C.load_json(hpath(), None, strict=True)
    if h is None:
        h = {"updated_at": None, "positions": []}
    h.setdefault("positions", [])
    return h


def save(h):
    h["updated_at"] = C.iso(C.now_utc())
    C.save_json(hpath(), h, backup=True)


def log_trade(**kw):
    kw["ts"] = C.iso(C.now_utc())
    C.append_jsonl(C.path("data", "trades.jsonl"), kw)


def find(h, sym):
    for p in h["positions"]:
        if p["symbol"] == sym:
            return p
    return None


def _norm(text):
    try:
        return C.normalize_symbol(text)
    except C.SymbolError as e:
        raise HoldingsError(str(e))


def _warn_size(old, new):
    w = []
    if old and old > 0 and new > 0 and (new / old >= 10 or new / old <= 0.1):
        w.append("数量与原持仓相差 10 倍以上，请和用户确认")
    return w


def buy(h, sym, shares, price, fee=0.0, name=None):
    if shares <= 0 or price <= 0:
        raise HoldingsError("数量和价格必须大于 0")
    p = find(h, sym)
    warnings = []
    if p is None:
        p = {"symbol": sym, "name": name or sym, "shares": 0.0, "cost": None,
             "currency": C.currency_of(sym), "aliases": [], "note": ""}
        h["positions"].append(p)
        new_cost = (shares * price + fee) / shares
        p["shares"], p["cost"] = shares, round(new_cost, 4)
    else:
        warnings += _warn_size(p["shares"], p["shares"] + shares)
        total = p["shares"] + shares
        if p.get("cost") is None:
            new_cost = None
        else:
            new_cost = (p["shares"] * p["cost"] + shares * price + fee) / total
        p["shares"] = total
        p["cost"] = None if new_cost is None else round(new_cost, 4)
        if name:
            p["name"] = name
    log_trade(action="buy", symbol=sym, shares=shares, price=price, fee=fee)
    cost_txt = "未知" if p["cost"] is None else C.fmt_price(p["cost"], p["currency"])
    return p, "已记录：买入 %s %s 股 @ %s；现持 %s 股，成本 %s。发“撤销”可恢复。" % (
        sym, C.fmt_shares(shares), C.fmt_price(price, p["currency"]), C.fmt_shares(p["shares"]), cost_txt), warnings


def sell(h, sym, shares, price, fee=0.0):
    p = find(h, sym)
    if p is None:
        raise HoldingsError("持仓里没有 %s" % sym)
    if shares <= 0 or price <= 0:
        raise HoldingsError("数量和价格必须大于 0")
    if shares > p["shares"] + 1e-9:
        raise HoldingsError("卖出 %s 股超过持仓 %s 股，请和用户确认" % (C.fmt_shares(shares), C.fmt_shares(p["shares"])))
    realized = None if p.get("cost") is None else round(shares * (price - p["cost"]) - fee, 2)
    p["shares"] = round(p["shares"] - shares, 6)
    cleared = p["shares"] <= 1e-9
    if cleared:
        h["positions"].remove(p)
    log_trade(action="sell", symbol=sym, shares=shares, price=price, fee=fee, realized=realized)
    rtxt = "" if realized is None else "，已实现盈亏 %s" % C.fmt_money(realized, p["currency"], sign=True)
    left = "已清仓" if cleared else "余 %s 股" % C.fmt_shares(p["shares"])
    return p, "已记录：卖出 %s %s 股 @ %s%s；%s。发“撤销”可恢复。" % (
        sym, C.fmt_shares(shares), C.fmt_price(price, p["currency"]), rtxt, left), []


def set_pos(h, sym, shares, cost=None, name=None):
    if shares <= 0:
        raise HoldingsError("数量必须大于 0；清仓请用 remove")
    p = find(h, sym)
    warnings = []
    if p is None:
        p = {"symbol": sym, "name": name or sym, "shares": shares, "cost": cost,
             "currency": C.currency_of(sym), "aliases": [], "note": ""}
        h["positions"].append(p)
    else:
        warnings += _warn_size(p["shares"], shares)
        p["shares"] = shares
        if cost is not None:
            p["cost"] = cost
        if name:
            p["name"] = name
    log_trade(action="set", symbol=sym, shares=shares, cost=cost)
    cost_txt = "未知" if p["cost"] is None else C.fmt_price(p["cost"], p["currency"])
    return p, "已记录：%s 持仓设为 %s 股，成本 %s。发“撤销”可恢复。" % (sym, C.fmt_shares(shares), cost_txt), warnings


def remove(h, sym):
    p = find(h, sym)
    if p is None:
        raise HoldingsError("持仓里没有 %s" % sym)
    h["positions"].remove(p)
    log_trade(action="remove", symbol=sym)
    return p, "已清仓记录：%s。发“撤销”可恢复。" % sym, []


def set_aliases(h, sym, aliases):
    p = find(h, sym)
    if p is None:
        raise HoldingsError("持仓里没有 %s" % sym)
    p["aliases"] = [a.strip() for a in aliases if a.strip()]
    return p, "已更新 %s 的别名：%s" % (sym, "、".join(p["aliases"]) or "（空）"), []


def undo():
    bak = hpath() + ".bak"
    prev = C.load_json(bak, None)
    if prev is None:
        raise HoldingsError("没有可撤销的上一版")
    cur = C.load_json(hpath(), {"positions": []})
    C.save_json(hpath(), prev)
    C.save_json(bak, cur)  # 再撤销一次可以回到撤销前（重做）
    log_trade(action="undo")
    return None, "已撤销上一次持仓变更。", []


def _emit(ok, message, **extra):
    out = {"ok": ok, "message": message}
    out.update(extra)
    print(json.dumps(out, ensure_ascii=False))
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="持仓记录")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("normalize")
    s.add_argument("text")
    for name in ("buy", "sell"):
        s = sub.add_parser(name)
        s.add_argument("symbol")
        s.add_argument("shares", type=float)
        s.add_argument("price", type=float)
        s.add_argument("--fee", type=float, default=0.0)
        s.add_argument("--name")
    s = sub.add_parser("set")
    s.add_argument("symbol")
    s.add_argument("shares", type=float)
    s.add_argument("--cost", type=float)
    s.add_argument("--name")
    s = sub.add_parser("remove")
    s.add_argument("symbol")
    s = sub.add_parser("aliases")
    s.add_argument("symbol")
    s.add_argument("aliases")
    sub.add_parser("undo")
    sub.add_parser("show")
    a = ap.parse_args(argv)
    C.ensure_dirs()
    try:
        if a.cmd == "normalize":
            sym = _norm(a.text)
            return _emit(True, sym, symbol=sym, market=C.market_of(sym))
        if a.cmd == "undo":
            _, msg, w = undo()
            return _emit(True, msg)
        h = load()
        if a.cmd == "show":
            return _emit(True, "共 %d 只持仓" % len(h["positions"]), positions=h["positions"],
                         updated_at=h.get("updated_at"))
        sym = _norm(a.symbol)
        if a.cmd == "buy":
            p, msg, w = buy(h, sym, a.shares, a.price, a.fee, a.name)
        elif a.cmd == "sell":
            p, msg, w = sell(h, sym, a.shares, a.price, a.fee)
        elif a.cmd == "set":
            p, msg, w = set_pos(h, sym, a.shares, a.cost, a.name)
        elif a.cmd == "remove":
            p, msg, w = remove(h, sym)
        else:
            p, msg, w = set_aliases(h, sym, a.aliases.split(","))
        save(h)
        return _emit(True, msg, position=p, warnings=w)
    except HoldingsError as e:
        return _emit(False, str(e))
    except json.JSONDecodeError:
        return _emit(False, "holdings.json 已损坏，请从 holdings.json.bak 恢复，不要覆盖")


if __name__ == "__main__":
    sys.exit(main())
