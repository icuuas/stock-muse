# -*- coding: utf-8 -*-
"""把结构化数据渲染成“内容草稿”（纯文本，渠道无关）。
模板只规定内容和顺序；具体排版由 Muse 按当前渠道自行调整。对应 templates/*.md。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

DISCLAIMER = "以上仅为信息整理，不构成投资建议。"
SRC_NAME = {"finnhub": "Finnhub", "nasdaq": "Nasdaq", "yahoo": "Yahoo", "sina": "新浪",
            "robinhood": "Robinhood", "edgar": "SEC", "eastmoney": "东方财富"}
KIND_LABEL = {"news": "新闻", "filing": "公告"}
LEVEL_LABEL = {"high": "高", "mid": "中", "low": "低"}


def _src(s):
    return SRC_NAME.get(s, s or "未知来源")


def _scheme(prefs):
    return prefs.get("color_scheme", "red_up")


def _tz(prefs):
    return prefs.get("user_timezone", "Asia/Shanghai")


def pos_lines(r, scheme):
    cur = r["currency"]
    head = "%s %s %s" % (C.plain_code(r["symbol"]), r["name"], C.fmt_price(r["price"], cur))
    if r["price"] is not None:
        head += " %s%s" % (C.trend_icon(r["day_change_pct"], scheme), C.fmt_pct(r["day_change_pct"], 2))
        if r.get("ext_change_pct") is not None and r.get("price_kind") in ("pre", "post", "night"):
            head += "（%s较收盘 %s）" % ({"pre": "盘前", "post": "盘后", "night": "夜盘"}[r["price_kind"]],
                                       C.fmt_pct(r["ext_change_pct"], 2))
    else:
        head += "（暂无行情）"
    cost_txt = "成本—" if r["cost"] is None else "成本%s" % C.fmt_price(r["cost"], cur)
    pnl_txt = "浮盈—" if r["pnl_pct"] is None else "浮盈%s" % C.fmt_pct(r["pnl_pct"], 1)
    w = "" if r["weight"] is None else " · 仓位%.0f%%" % r["weight"]
    return [head, " %s股 · %s · %s%s" % (C.fmt_shares(r["shares"]), cost_txt, pnl_txt, w)]


def _data_line(snap, prefs, markets=None):
    parts = []
    for m, info in snap["markets"].items():
        if markets and m not in markets:
            continue
        t = C.parse_iso(info["latest_time"]) if info["latest_time"] else None
        parts.append("%s %s · %s · %s" % (info["label"], C.fmt_time(m, t, _tz(prefs)) if t else "时间未知",
                                         "/".join(_src(s) for s in info["sources"]) or "无",
                                         "/".join(info["status_labels"]) or "—"))
    return "数据：" + "｜".join(parts)


def _fx_line(snap):
    fx, base = snap.get("fx") or {}, snap["base_currency"]
    to_base = fx.get("to_base") or {}
    used = sorted({r["currency"] for r in snap["positions"]} - {base})
    items = ["%s→%s %.4f" % (c, base, to_base[c]) for c in used if to_base.get(c)]
    if not items:
        return None
    t = C.parse_iso(fx.get("time")) if fx.get("time") else None
    return "汇率 %s（Yahoo%s）" % ("｜".join(items), "" if not t else " · " + t.strftime("%H:%M UTC"))


def _totals_lines(snap):
    t, base = snap["totals"], snap["base_currency"]
    lines = ["总金额 %s%s" % (C.fmt_money(t["value"], base), "（部分标的缺数据，未计入）" if t["partial"] else ""),
             "今日 %s（%s）" % (C.fmt_money(t["day_pnl"], base, sign=True), C.fmt_pct(t["day_pct"], 2)),
             "累计浮盈 %s（%s）" % (C.fmt_money(t["pnl"], base, sign=True), C.fmt_pct(t["pnl_pct"], 1))]
    fl = _fx_line(snap)
    if fl:
        lines.append(fl)
    return lines


def render_snapshot(snap, prefs):
    scheme = _scheme(prefs)
    now = C.parse_iso(snap["generated"])
    if not snap["positions"]:
        return "还没有记录持仓。发给我代码、股数、每股成本就行，比如：NVDA 100 股 成本 180。"
    lines = ["📊 我的持仓 · " + C.fmt_user_time(now, _tz(prefs))] + _totals_lines(snap) + [""]
    for m, info in snap["markets"].items():
        lines.append("【%s·%s】" % (info["label"], info["session_label"]))
        for r in [x for x in snap["positions"] if x["market"] == m]:
            lines += pos_lines(r, scheme)
        lines.append("")
    lines.append(_data_line(snap, prefs))
    for n in snap["notes"]:
        lines.append("⚠ " + n)
    return "\n".join(lines).strip()


def render_alert(ev, prefs, first_of_day=False):
    scheme = _scheme(prefs)
    cur = ev["currency"]
    t = C.parse_iso(ev.get("price_time")) if ev.get("price_time") else None
    lines = ["⚠️ %s %s｜%s" % (C.plain_code(ev["symbol"]), ev["name"], ev["headline"])]
    if ev.get("price") is not None:
        lines.append("现价 %s %s%s（%s）" % (C.fmt_price(ev["price"], cur), C.trend_icon(ev.get("pct"), scheme),
                                           C.fmt_pct(ev.get("pct"), 2), ev.get("session_label") or ""))
    # Merged triggers for the same symbol (monitor.py attaches them as "also"):
    # one block per symbol, the extra trigger information listed below it.
    for _h in ev.get("also") or []:
        lines.append("· %s" % _h)
    rs = ev.get("reason")
    lines.append("原因：%s" % ("%s（%s · %s）" % (rs["title"], _src(rs.get("source")), rs.get("time_text", ""))
                              if rs else "暂未找到明确消息"))
    p = ev.get("position") or {}
    if p:
        lines.append("你的持仓：%s股 · %s · 市值%s · 浮盈%s · 今日%s" % (
            C.fmt_shares(p["shares"]),
            "成本—" if p.get("cost") is None else "成本" + C.fmt_price(p["cost"], cur),
            C.fmt_money(p.get("value"), cur),
            "—" if p.get("pnl") is None else "%s(%s)" % (C.fmt_money(p["pnl"], cur, sign=True), C.fmt_pct(p.get("pnl_pct"), 1)),
            C.fmt_money(p.get("day_pnl"), cur, sign=True)))
    lines.append("数据：%s · %s · %s" % (C.fmt_time(ev["market"], t, _tz(prefs)) if t else "时间未知",
                                       _src(ev.get("source")), ev.get("status_label") or "—"))
    if ev.get("session") == "night":
        lines.append("夜盘价差大、无成交时仅供参考。")
    if first_of_day:
        lines.append(DISCLAIMER)
    return "\n".join(lines)


def render_alerts(events, prefs, first_of_day=False):
    out = []
    for i, e in enumerate(events):
        out.append(render_alert(e, prefs, first_of_day and i == len(events) - 1))
    return "\n\n".join(out)


def render_news(item, prefs):
    scheme = _scheme(prefs)
    pos = item.get("position") or {}
    t = C.parse_iso(item.get("time")) if item.get("time") else None
    head = "📰 %s %s" % (C.plain_code(item["symbol"]), item.get("name", ""))
    ctx = []
    if pos.get("weight") is not None:
        ctx.append("仓位%.0f%%" % pos["weight"])
    if pos.get("day_change_pct") is not None:
        ctx.append("今日%s%s" % (C.trend_icon(pos["day_change_pct"], scheme), C.fmt_pct(pos["day_change_pct"], 1)))
    if pos.get("pnl_pct") is not None:
        ctx.append("浮盈%s" % C.fmt_pct(pos["pnl_pct"], 1))
    if ctx:
        head += "｜" + " · ".join(ctx)
    when = item.get("time_text") or (t.strftime("%m-%d %H:%M UTC") if t else "")
    lines = [head, "【%s·%s】%s" % (KIND_LABEL.get(item["kind"], "消息"), LEVEL_LABEL.get(item["level"], ""), item["title"]),
             "关联：直接涉及你持有的 %s" % C.plain_code(item["symbol"]),
             "来源：%s · %s%s" % (_src(item.get("source")), when,
                               "｜另有 %d 家媒体报道" % item["also"] if item.get("also") else "")]
    return "\n".join(lines)


REPORT_NAME = {"pre": "盘前日报", "post": "盘后日报", "close": "收盘日报"}


def render_report(rep, prefs):
    snap, m, kind = rep["snapshot"], rep["market"], rep["kind"]
    scheme = _scheme(prefs)
    now = C.parse_iso(snap["generated"])
    label = "全部市场" if m == "ALL" else C.MARKET_LABEL[m]
    lines = ["📋 %s%s · %s" % (label, REPORT_NAME.get(kind, "日报"), C.fmt_user_time(now, _tz(prefs)))]
    if rep.get("holiday_all"):
        lines.append("今日可能休市，以下为上一交易日收盘数据。")
    lines += ["", "【账户总览】"] + _totals_lines(snap) + [""]
    show = list(snap["markets"].keys()) if m == "ALL" else [m]
    for mk in show:
        if mk not in snap["markets"]:
            continue
        lines.append("【%s持仓明细】按市值排序（%s）" % (C.MARKET_LABEL[mk], snap["markets"][mk]["session_label"]))
        for r in [x for x in snap["positions"] if x["market"] == mk]:
            lines += pos_lines(r, scheme)
        lines.append("")
    if rep.get("key_moves"):
        lines.append("【重点异动】")
        for k in rep["key_moves"]:
            rs = k.get("reason")
            lines.append("• %s %s：%s" % (C.plain_code(k["symbol"]), C.fmt_pct(k["pct"], 1),
                                       "%s（%s）" % (rs["title"], _src(rs.get("source"))) if rs else "暂未找到明确消息"))
        lines.append("")
    if rep.get("news"):
        lines.append("【持仓相关消息】")
        for n in rep["news"]:
            lines.append("• %s｜%s（%s · %s）[%s]" % (C.plain_code(n["symbol"]), n["title"], _src(n.get("source")),
                                                   n.get("time_text", ""), LEVEL_LABEL.get(n["level"], "")))
        lines.append("")
    lines.append("【数据说明】")
    lines.append(_data_line(snap, prefs, None if m == "ALL" else [m]))
    for n in snap["notes"]:
        lines.append("⚠ " + n)
    lines.append(DISCLAIMER)
    return "\n".join(lines)
