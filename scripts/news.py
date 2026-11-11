#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""信息推送：只保留和持仓对得上的公告新闻。四道闸：关联 → 新鲜 → 重要 → 去重。

用法：
  news.py check --mode realtime|digest [--market US|HK|CN|ALL]   # 输出候选条目（JSON）
  news.py ack ID [ID ...]                                         # 发送成功后标记已发，避免重复

脚本只负责抓取、粗筛、分级、去重，输出条目与草稿；“一句话概括”和排版由 Muse 完成。
v0.1 只处理“直接关联”（持仓股自身的消息）；板块、宏观等间接关联默认不推。
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

HIGH = ["earnings", "guidance", "outlook", "acquire", "acquisition", "merger", "buyback", "repurchase",
        "dividend", "stock split", "offering", "lawsuit", "sues", "sued", "probe", "investigation",
        "subpoena", "fined", "recall", "bankruptcy", "delist", "halt", "downgrade", "upgrade",
        "price target", "resign", "steps down", "layoff", "antitrust", "fda", "contract win",
        "财报", "业绩", "指引", "并购", "收购", "重组", "回购", "分红", "派息", "拆股", "增发", "配股",
        "定增", "诉讼", "调查", "立案", "处罚", "监管", "退市", "停牌", "复牌", "评级", "目标价",
        "辞任", "辞职", "召回", "减持", "增持", "中标", "重大合同", "问询函", "违规"]
MID = ["launch", "unveil", "partnership", "collaborat", "new product", "release", "announces", "deal",
       "expands", "invest", "supply", "产品发布", "合作", "签约", "发布", "投资", "扩产", "行业数据"]
LOW = ["stocks to buy", "top stocks", "best stocks", "should you buy", "better buy", "worth buying",
       "magnificent seven", "dividend stocks", "reasons to", "here's why", "what to know",
       "值得买", "盘点", "榜单", "必看", "推荐买入"]
FORM_HIGH = {"8-K", "10-K", "10-Q", "6-K", "20-F", "40-F"}
FORM_MID = {"S-1", "S-3", "SC 13D", "SC 13D/A", "DEF 14A"}
SOURCE_RANK = ["SEC", "公司公告", "Reuters", "Bloomberg", "Dow Jones", "WSJ", "CNBC", "Barron", "Yahoo"]


# ----------------------------------------------------------------- 关联闸
def aliases_of(pos):
    code = C.plain_code(pos["symbol"])
    names = [pos.get("name")] + list(pos.get("aliases") or [])
    out = []
    for n in names:
        n = (n or "").strip()
        if len(n) >= 2 and n != pos["symbol"] and n not in out:
            out.append(n)
    return code, out


def _has(text, term):
    if re.search(r"[\u4e00-\u9fff]", term):
        return term in text
    return re.search(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(term), text, re.I) is not None


def match_score(title, summary, pos):
    """2=标题命中；1=仅摘要命中；0=不相关。"""
    code, names = aliases_of(pos)
    title, summary = title or "", summary or ""
    ticker_ok = C.market_of(pos["symbol"]) == "US" and len(code) >= 3
    if (ticker_ok and re.search(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(code), title)) or any(_has(title, n) for n in names):
        return 2
    if (ticker_ok and re.search(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(code), summary)) or any(_has(summary, n) for n in names):
        return 1
    return 0


# ----------------------------------------------------------------- 重要闸
def classify(title, kind, form=None):
    if kind == "filing" and form:
        if form in FORM_HIGH:
            return "high"
        if form in FORM_MID:
            return "mid"
        return "low"
    t = (title or "").lower()
    has_high = any(k in t for k in HIGH)
    if any(k in t for k in LOW) and not has_high:
        return "low"
    if has_high:
        return "high"
    if any(k in t for k in MID):
        return "mid"
    return "mid" if kind == "filing" else "low"


# ----------------------------------------------------------------- 去重闸
def _tokens(title):
    t = (title or "").lower()
    words = set(w for w in re.findall(r"[a-z0-9]{3,}", t))
    cjk = re.findall(r"[\u4e00-\u9fff]+", t)
    for seg in cjk:
        words.update(seg[i:i + 2] for i in range(len(seg) - 1))
    return words


def similar(a, b, thr=0.55):
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / float(len(ta | tb)) >= thr


def _rank(item):
    for i, s in enumerate(SOURCE_RANK):
        if s.lower() in (item.get("source") or "").lower():
            return i
    return len(SOURCE_RANK)


def dedupe(items):
    clusters = []
    for it in sorted(items, key=lambda x: (x.get("time") or "9999")):
        for c in clusters:
            if c[0]["symbol"] == it["symbol"] and similar(c[0]["title"], it["title"]):
                c.append(it)
                break
        else:
            clusters.append([it])
    out = []
    for c in clusters:
        best = sorted(c, key=lambda x: (_rank(x), x.get("time") or "9999"))[0]
        best = dict(best)
        best["also"] = len(c) - 1
        out.append(best)
    return out


# ----------------------------------------------------------------- 状态
def _sent_path():
    return C.path("state", "news_sent.json")


def hid(symbol, raw_id):
    return hashlib.sha1(("%s|%s" % (symbol, raw_id)).encode("utf-8")).hexdigest()[:12]


def load_sent():
    return C.load_json(_sent_path(), {"items": {}, "counts": {}}) or {"items": {}, "counts": {}}


def ack(ids, now=None):
    now = now or C.now_utc()
    st = load_sent()
    items = st.setdefault("items", {})
    counts = st.setdefault("counts", {})
    day = now.astimezone(C.TZ["CN"]).date().isoformat()
    for i in ids:
        if i not in items:
            sym = (st.get("pending", {}) or {}).get(i)
            items[i] = C.iso(now)
            if sym:
                counts["%s|%s" % (day, sym)] = counts.get("%s|%s" % (day, sym), 0) + 1
    st["pending"] = {k: v for k, v in (st.get("pending") or {}).items() if k not in ids}
    cutoff = C.iso(now - dt.timedelta(days=7))
    st["items"] = {k: v for k, v in items.items() if v >= cutoff}
    st["counts"] = {k: v for k, v in counts.items() if k.split("|")[0] >= (now - dt.timedelta(days=3)).astimezone(C.TZ["CN"]).date().isoformat()}
    C.save_json(_sent_path(), st)


def _remember_pending(items):
    st = load_sent()
    pend = st.setdefault("pending", {})
    for it in items:
        pend[it["id"]] = it["symbol"]
    C.save_json(_sent_path(), st)


# ----------------------------------------------------------------- 抓取
def parse_finnhub_news(arr, sym):
    out = []
    for a in arr if isinstance(arr, list) else []:
        t = C.from_ts(a.get("datetime"))
        if not a.get("headline"):
            continue
        out.append({"id": hid(sym, "fh:%s" % a.get("id")), "symbol": sym, "kind": "news", "source": a.get("source") or "Finnhub",
                    "title": a["headline"], "summary": a.get("summary") or "", "time": C.iso(t), "date": None,
                    "url": a.get("url"), "form": None})
    return out


def fetch_finnhub_news(sym, now):
    key = C.finnhub_key()
    if not key:
        return []
    d1, d0 = now.date().isoformat(), (now - dt.timedelta(days=1)).date().isoformat()
    url = ("https://finnhub.io/api/v1/company-news?symbol=%s&from=%s&to=%s&token=%s"
           % (urllib.parse.quote(C.plain_code(sym)), d0, d1, key))
    obj, r = C.http_json(url, timeout=8)
    C.log("fetch", src="finnhub_news", **r.brief())
    return parse_finnhub_news(obj, sym) if obj is not None else []


def parse_yahoo_news(obj, sym):
    out = []
    for a in ((obj or {}).get("news") or []) if isinstance(obj, dict) else []:
        if not a.get("title"):
            continue
        out.append({"id": hid(sym, "yn:%s" % (a.get("uuid") or a.get("link"))), "symbol": sym, "kind": "news",
                    "source": a.get("publisher") or "Yahoo", "title": a["title"], "summary": "",
                    "time": C.iso(C.from_ts(a.get("providerPublishTime"))), "date": None,
                    "url": a.get("link"), "form": None})
    return out


def fetch_yahoo_news(sym, now):
    url = ("https://query1.finance.yahoo.com/v1/finance/search?q=%s&newsCount=10&quotesCount=0"
           % urllib.parse.quote(C.yahoo_code(sym), safe=""))
    obj, r = C.http_json(url, timeout=8)
    C.log("fetch", src="yahoo_news", **r.brief())
    return parse_yahoo_news(obj, sym) if obj is not None else []


def _cik_map(ua):
    p = C.path("state", "sec_tickers.json")
    c = C.load_json(p, None)
    if c and (C.now_utc().timestamp() - c.get("fetched", 0)) < 7 * 86400 and c.get("map"):
        return c["map"]
    obj, r = C.http_json("https://www.sec.gov/files/company_tickers.json", headers={"User-Agent": ua}, timeout=10)
    C.log("fetch", src="sec_tickers", **r.brief())
    if not isinstance(obj, dict):
        return (c or {}).get("map") or {}
    m = {}
    for v in obj.values():
        try:
            m[v["ticker"].upper()] = int(v["cik_str"])
        except (KeyError, ValueError, TypeError):
            continue
    C.save_json(p, {"fetched": C.now_utc().timestamp(), "map": m})
    return m


def parse_edgar(obj, sym, cik, since_date):
    out = []
    rec = (((obj or {}).get("filings") or {}).get("recent") or {}) if isinstance(obj, dict) else {}
    forms, dates, accs, docs, descs = (rec.get(k) or [] for k in
                                       ("form", "filingDate", "accessionNumber", "primaryDocument", "primaryDocDescription"))
    for i in range(min(len(forms), len(dates), len(accs))):
        if dates[i] < since_date:
            continue
        form = forms[i]
        if form not in FORM_HIGH and form not in FORM_MID:
            continue
        doc = docs[i] if i < len(docs) else ""
        url = "https://www.sec.gov/Archives/edgar/data/%d/%s/%s" % (cik, accs[i].replace("-", ""), doc)
        desc = descs[i] if i < len(descs) and descs[i] else form
        out.append({"id": hid(sym, "sec:%s" % accs[i]), "symbol": sym, "kind": "filing", "source": "SEC",
                    "title": "%s：%s" % (form, desc), "summary": "", "time": None, "date": dates[i],
                    "url": url, "form": form})
    return out


def fetch_edgar(sym, now, ua):
    cik = _cik_map(ua).get(C.plain_code(sym).upper())
    if not cik:
        return []
    obj, r = C.http_json("https://data.sec.gov/submissions/CIK%010d.json" % cik, headers={"User-Agent": ua}, timeout=10)
    C.log("fetch", src="edgar", **r.brief())
    return parse_edgar(obj, sym, cik, (now - dt.timedelta(days=2)).date().isoformat()) if obj else []


def parse_eastmoney(obj, sym):
    out = []
    lst = (((obj or {}).get("data") or {}).get("list") or []) if isinstance(obj, dict) else []
    code = C.plain_code(sym)
    for a in lst:
        title = a.get("title") or a.get("title_ch")
        if not title:
            continue
        date = str(a.get("notice_date") or "")[:10] or None
        out.append({"id": hid(sym, "em:%s" % a.get("art_code")), "symbol": sym, "kind": "filing", "source": "公司公告",
                    "title": title, "summary": "", "time": None, "date": date,
                    "url": "https://data.eastmoney.com/notices/detail/%s/%s.html" % (code, a.get("art_code")),
                    "form": None})
    return out


def fetch_eastmoney(sym, now):
    url = ("https://np-anotice-stock.eastmoney.com/api/security/ann?sr=-1&page_size=20&page_index=1"
           "&ann_type=A&client_source=web&stock_list=%s" % C.plain_code(sym))
    obj, r = C.http_json(url, timeout=8)
    C.log("fetch", src="eastmoney_ann", **r.brief())
    return parse_eastmoney(obj, sym) if obj is not None else []


def collect_symbol(sym, now, feats, prefs):
    """抓取单只持仓的原始条目，10 分钟缓存。"""
    cp = C.path("state", "news_cache.json")
    cache = C.load_json(cp, {}) or {}
    c = cache.get(sym)
    if c and now.timestamp() - c["ts"] < 600:
        return c["items"]
    m = C.market_of(sym)
    items = []
    try:
        if m == "US":
            if feats.get("us_news"):
                items += fetch_finnhub_news(sym, now)
            if feats.get("edgar"):
                items += fetch_edgar(sym, now, prefs["sec_user_agent"])
        elif m == "CN":
            if feats.get("cn_announce"):
                items += fetch_eastmoney(sym, now)
            if feats.get("cn_hk_news"):
                items += fetch_yahoo_news(sym, now)
        elif m == "HK":
            if feats.get("cn_hk_news"):
                items += fetch_yahoo_news(sym, now)
    except Exception as e:
        C.log("news_error", symbol_market=m, error=str(e))
    cache[sym] = {"ts": now.timestamp(), "items": items}
    C.save_json(cp, cache)
    return items


# ----------------------------------------------------------------- 流水线
def _fresh(it, mode, now):
    if it.get("time"):
        age = (now - C.parse_iso(it["time"])).total_seconds()
        return age <= (3600 if mode == "realtime" else 86400)
    d = it.get("date")
    if not d:
        return False
    days = (now.date() - dt.date.fromisoformat(d)).days
    return days <= (1 if mode == "realtime" else 2)


def _time_text(it, prefs):
    if it.get("time"):
        d = C.parse_iso(it["time"]).astimezone(C.ZoneInfo(prefs.get("user_timezone", "Asia/Shanghai")))
        return d.strftime("%m-%d %H:%M")
    return "%s 披露" % it["date"] if it.get("date") else ""


def process(items, positions, now, mode, prefs, sent=None, ctx=None, apply_caps=True):
    """items 为原始条目；返回通过四道闸的条目（含 position 上下文与 draft）。"""
    import render
    sent = sent or {"items": {}, "counts": {}}
    pos_by = {p["symbol"]: p for p in positions}
    level_ok = {"realtime": ("high",), "digest": ("high", "mid")}[mode]
    if prefs.get("info_level") == "minimal":
        level_ok = ("high",)
    kept = []
    for it in items:
        pos = pos_by.get(it["symbol"])
        if not pos or it["id"] in sent.get("items", {}):
            continue
        # 公告是按“发行人”抓取的（SEC / 东财 / 披露易），本身就属于该持仓，不再要求标题含公司名
        score = 2 if it["kind"] == "filing" else match_score(it["title"], it.get("summary"), pos)
        level = classify(it["title"], it["kind"], it.get("form"))
        if score == 0 or (score == 1 and level != "high"):          # 关联闸
            continue
        if not _fresh(it, mode, now):                                # 新鲜闸
            continue
        if level not in level_ok:                                    # 重要闸
            continue
        x = dict(it)
        x["level"] = level
        kept.append(x)
    kept = dedupe(kept)                                              # 去重闸
    day = now.astimezone(C.TZ["CN"]).date().isoformat()
    cap = int(prefs.get("news_cap_per_symbol", 3))
    out, used = [], {}
    for it in sorted(kept, key=lambda x: (x["level"] != "high", x.get("time") or x.get("date") or "")):
        if apply_caps and mode == "realtime":
            n = sent.get("counts", {}).get("%s|%s" % (day, it["symbol"]), 0) + used.get(it["symbol"], 0)
            if n >= cap:
                continue
            used[it["symbol"]] = used.get(it["symbol"], 0) + 1
        pos = pos_by[it["symbol"]]
        it["name"] = pos.get("name") or it["symbol"]
        it["position"] = (ctx or {}).get(it["symbol"], {})
        it["time_text"] = _time_text(it, prefs)
        it["needs_summary"] = True
        it["draft"] = render.render_news(it, prefs)
        out.append(it)
    return out


def context_from_snapshot():
    snap = C.load_json(C.path("state", "last_snapshot.json"), None)
    if not snap:
        return {}
    return {r["symbol"]: {"weight": r.get("weight"), "day_change_pct": r.get("day_change_pct"),
                          "pnl_pct": r.get("pnl_pct")} for r in snap.get("positions", [])}


def check(positions, now, mode, prefs, feats=None, apply_caps=True):
    feats = feats if feats is not None else C.features()
    raw = []
    for p in positions:
        raw += collect_symbol(p["symbol"], now, feats, prefs)
    items = process(raw, positions, now, mode, prefs, load_sent(), context_from_snapshot(), apply_caps)
    _remember_pending(items)
    return items


def find_reason(sym, positions, now, prefs, feats=None):
    """给异动找原因：取该标的近 24 小时最重要的一条消息，没有返回 None。"""
    pos = [p for p in positions if p["symbol"] == sym]
    if not pos:
        return None
    feats = feats if feats is not None else C.features()
    raw = collect_symbol(sym, now, feats, prefs)
    items = process(raw, pos, now, "digest", prefs, None, None, apply_caps=False)
    if not items:
        return None
    items.sort(key=lambda x: x.get("time") or x.get("date") or "", reverse=True)   # 先按时间新→旧
    items.sort(key=lambda x: x["level"] != "high")                                   # 再把“高”排前面（稳定排序）
    best = items[0]
    return {"title": best["title"], "source": best.get("source"), "time_text": best["time_text"],
            "url": best.get("url"), "level": best["level"]}


def main(argv=None):
    ap = argparse.ArgumentParser(description="持仓相关信息")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--mode", choices=["realtime", "digest"], default="realtime")
    c.add_argument("--market", default="ALL")
    a_ = sub.add_parser("ack")
    a_.add_argument("ids", nargs="+")
    a = ap.parse_args(argv)
    C.ensure_dirs()
    if a.cmd == "ack":
        ack(a.ids)
        print(json.dumps({"ok": True, "acked": len(a.ids)}, ensure_ascii=False))
        return 0
    import holdings
    positions = holdings.load()["positions"]
    if a.market != "ALL":
        positions = [p for p in positions if C.market_of(p["symbol"]) == a.market]
    items = check(positions, C.now_utc(), a.mode, C.load_prefs())
    print(json.dumps({"ok": True, "count": len(items), "items": items}, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
