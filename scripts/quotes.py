#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""行情：美股 / 港股 / A 股，多源降级、前收盘口径、交叉核对。

用法：
  quotes.py                      # 取全部持仓的行情，输出 JSON
  quotes.py --symbols NVDA.US,00700.HK --cross

数据源均为免费或非官方接口，可能随时失效；每个解析函数都做了字段校验，
失败只会让该源标记为失败并降级，不会抛异常中断整轮。
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

_BUDGET = {"deadline": None}


def _over_budget():
    d = _BUDGET["deadline"]
    return d is not None and time.time() > d


def _f(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _parse_dt(s, market):
    """把 '2026-11-10 15:00:00' / '2026/11/10 16:08:20' 按市场时区转成 UTC。"""
    s = (s or "").strip().replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            d = dt.datetime.strptime(s, fmt)
            return d.replace(tzinfo=C.TZ[market]).astimezone(dt.timezone.utc)
        except ValueError:
            continue
    return None


# ================================================================== 新浪
def parse_sina_cn(f):
    """A 股：0 名称 1 今开 2 昨收 3 现价 4 最高 5 最低 8 成交量 9 成交额 30 日期 31 时间。"""
    if len(f) < 32:
        return None
    prev, price = _f(f[2]), _f(f[3])
    if not prev or prev <= 0:
        return None
    no_trade = price is None or price <= 0
    if no_trade:
        price = prev
    t = _parse_dt("%s %s" % (f[30], f[31]), "CN")
    return {"price": price, "time": t, "prev_close": prev, "name": f[0] or None,
            "kind": "close" if no_trade else "regular", "realtime": None, "source": "sina",
            "no_trade_yet": no_trade,
            "extra": {"open": _f(f[1]), "high": _f(f[4]), "low": _f(f[5]),
                      "volume": _f(f[8]), "amount": _f(f[9])}}


def parse_sina_hk(f):
    """港股：0 英文名 1 中文名 2 今开 3 昨收 4 最高 5 最低 6 现价 7 涨跌 8 涨跌幅
    9 买入 10 卖出 11 成交额 12 成交量 15/16 52 周高低 17 日期 18 时间。"""
    if len(f) < 19:
        return None
    prev, price = _f(f[3]), _f(f[6])
    if not prev or prev <= 0:
        return None
    no_trade = price is None or price <= 0
    if no_trade:
        price = prev
    t = _parse_dt("%s %s" % (f[17], f[18]), "HK")
    return {"price": price, "time": t, "prev_close": prev, "name": f[1] or f[0] or None,
            "kind": "close" if no_trade else "regular", "realtime": None, "source": "sina",
            "no_trade_yet": no_trade,
            "extra": {"open": _f(f[2]), "high": _f(f[4]), "low": _f(f[5]),
                      "volume": _f(f[12]), "amount": _f(f[11]),
                      "week52_high": _f(f[15]), "week52_low": _f(f[16])}}


_EST_RE = re.compile(r"([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{1,2}):(\d{2})\s*(AM|PM)\s+(E[SD]T)")


def _parse_est(s, now):
    m = _EST_RE.search(s or "")
    if not m:
        return None
    try:
        d = dt.datetime.strptime("%s %s %s:%s %s %d" % (m.group(1), m.group(2), m.group(3), m.group(4),
                                                         m.group(5), (now or C.now_utc()).year),
                                 "%b %d %I:%M %p %Y")
    except ValueError:
        return None
    return d.replace(tzinfo=C.TZ["US"]).astimezone(dt.timezone.utc)


def parse_sina_us(f, now=None):
    """美股：1 现价 2 涨跌幅 3 时间（按北京时间解析，自测会与 Yahoo 对比校验）。
    盘后字段（尽力解析，布局未经验证）：21 盘后价 24 盘后时间。"""
    if len(f) < 4:
        return None
    price = _f(f[1])
    if not price or price <= 0:
        return None
    raw = {"price": price, "time": _parse_dt(f[3], "CN"), "prev_close": None,
           "name": f[0] or None, "kind": "regular", "realtime": None, "source": "sina",
           "extra": {"change_pct": _f(f[2])}}
    if len(f) > 24:
        ext, ext_t = _f(f[21]), _parse_est(f[24], now)
        if ext and ext > 0 and ext_t:
            raw["ext_price"], raw["ext_time"] = ext, ext_t
    return raw


def parse_sina_text(text, symbols, now=None):
    out = {}
    by_code = {C.sina_code(s): s for s in symbols}
    for m in re.finditer(r'var hq_str_([\w$.]+)="([^"]*)";', text or ""):
        sym = by_code.get(m.group(1))
        body = m.group(2)
        if not sym or not body:
            continue
        f = body.split(",")
        try:
            mk = C.market_of(sym)
            raw = parse_sina_cn(f) if mk == "CN" else parse_sina_hk(f) if mk == "HK" else parse_sina_us(f, now)
        except Exception:
            raw = None
        if raw:
            out[sym] = raw
    return out


def fetch_sina(symbols, now=None):
    url = "https://hq.sinajs.cn/list=" + ",".join(C.sina_code(s) for s in symbols)
    r = C.http_get(url, headers={"Referer": "https://finance.sina.com.cn"}, encoding="gbk")
    C.log("fetch", src="sina", **r.brief())
    if not r.ok:
        return {}, r.error
    return parse_sina_text(r.text, symbols, now), None


# ================================================================== Yahoo
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/"


def _yahoo_url(sym, mode):
    code = urllib.parse.quote(C.yahoo_code(sym), safe="")
    if mode == "daily":
        return "%s%s?range=10d&interval=1d" % (YAHOO, code)
    pp = "&includePrePost=true" if C.market_of(sym) == "US" else ""
    return "%s%s?range=1d&interval=1m%s" % (YAHOO, code, pp)


def _chart_result(obj):
    try:
        return obj["chart"]["result"][0]
    except (KeyError, IndexError, TypeError):
        return None


def parse_yahoo_chart(obj, sym):
    res = _chart_result(obj)
    if not res:
        return None
    meta = res.get("meta") or {}
    ts = res.get("timestamp") or []
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
    closes = q.get("close") or []
    price, t = None, None
    for i in range(min(len(ts), len(closes)) - 1, -1, -1):
        if closes[i] is not None:
            price, t = closes[i], ts[i]
            break
    if price is None:
        price, t = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
    if price is None:
        return None
    kind = "regular"
    ctp = meta.get("currentTradingPeriod") or {}

    def within(p):
        p = p or {}
        return p.get("start") is not None and t is not None and p["start"] <= t < p["end"]

    if C.market_of(sym) == "US":
        kind = ("pre" if within(ctp.get("pre")) else "regular" if within(ctp.get("regular"))
                else "post" if within(ctp.get("post")) else "close")
    else:
        kind = "regular" if within(ctp.get("regular")) else "close"
    return {"price": float(price), "time": C.from_ts(t), "prev_close": _f(meta.get("chartPreviousClose")),
            "name": meta.get("longName") or meta.get("shortName"), "kind": kind, "realtime": None,
            "source": "yahoo", "reg_price": _f(meta.get("regularMarketPrice")),
            "currency": meta.get("currency"), "extra": {}}


def parse_yahoo_daily(obj):
    """返回 [(日期 'YYYY-MM-DD', 收盘价), ...]，按日期升序。"""
    res = _chart_result(obj)
    if not res:
        return []
    meta = res.get("meta") or {}
    gmt = meta.get("gmtoffset") or 0
    ts = res.get("timestamp") or []
    closes = (((res.get("indicators") or {}).get("quote") or [{}])[0] or {}).get("close") or []
    bars = []
    for i, t in enumerate(ts):
        if i >= len(closes) or closes[i] is None:
            continue
        d = dt.datetime.fromtimestamp(t + gmt, dt.timezone.utc).date()
        bars.append((d.isoformat(), float(closes[i])))
    bars.sort()
    return bars


def fetch_yahoo(symbols, now=None):
    out, err = {}, None
    for s in symbols:
        if _over_budget():
            err = "超出时间预算"
            break
        obj, r = C.http_json(_yahoo_url(s, "intraday"))
        C.log("fetch", src="yahoo", sym_market=C.market_of(s), **r.brief())
        if obj is None:
            err = r.error
            continue
        raw = parse_yahoo_chart(obj, s)
        if raw:
            out[s] = raw
    return out, (err if not out else None)


def prev_from_bars(bars, sd):
    """前收盘 = 早于当前交易日 sd 的最近一根日线收盘价。"""
    sd = sd.isoformat() if hasattr(sd, "isoformat") else sd
    prev = [b for b in bars if b[0] < sd]
    return prev[-1][1] if prev else None


def close_on(bars, d):
    d = d.isoformat() if hasattr(d, "isoformat") else d
    for b in bars:
        if b[0] == d:
            return b[1]
    return None


def daily_bars(sym, sd, need_today=False):
    p = C.path("state", "daily_cache.json")
    cache = C.load_json(p, {}) or {}
    c = cache.get(sym)
    ttl = 600 if need_today else 12 * 3600
    now = time.time()
    if c and c.get("sd") == sd.isoformat() and now - c.get("fetched", 0) < ttl:
        return [tuple(b) for b in c["bars"]]
    obj, r = C.http_json(_yahoo_url(sym, "daily"))
    C.log("fetch", src="yahoo_daily", **r.brief())
    bars = parse_yahoo_daily(obj) if obj else []
    if bars:
        cache[sym] = {"sd": sd.isoformat(), "fetched": now, "bars": [list(b) for b in bars]}
        try:
            C.save_json(p, cache)
        except OSError:
            pass
        return bars
    return [tuple(b) for b in c["bars"]] if c else []


# ================================================================== Finnhub
def parse_finnhub(obj):
    if not isinstance(obj, dict):
        return None
    c, t = _f(obj.get("c")), obj.get("t")
    if not c or c <= 0 or not t:
        return None
    return {"price": c, "time": C.from_ts(t), "prev_close": _f(obj.get("pc")), "name": None,
            "kind": "regular", "realtime": None, "source": "finnhub",
            "extra": {"open": _f(obj.get("o")), "high": _f(obj.get("h")), "low": _f(obj.get("l"))}}


def fetch_finnhub(symbols, now=None):
    key = C.finnhub_key()
    if not key:
        return {}, "无 Finnhub key"
    out, err = {}, None
    for s in symbols:
        if C.market_of(s) != "US":
            continue
        if _over_budget():
            err = "超出时间预算"
            break
        url = "https://finnhub.io/api/v1/quote?symbol=%s&token=%s" % (urllib.parse.quote(C.plain_code(s)), key)
        obj, r = C.http_json(url, timeout=6)
        C.log("fetch", src="finnhub", **r.brief())
        if r.status in (401, 403):
            return out, "Finnhub key 无效或无权限（HTTP %s）" % r.status
        if r.status == 429:
            return out, "Finnhub 限频（429）"
        raw = parse_finnhub(obj)
        if raw:
            out[s] = raw
        elif obj is None:
            err = r.error
    return out, (err if not out else None)


# ================================================================== Nasdaq
_NQ_TS = re.compile(r"([A-Z][a-z]{2})\s+(\d{1,2}),\s*(\d{4})\s+(\d{1,2}):(\d{2})\s*(AM|PM)")


def _price_str(s):
    if s is None:
        return None
    s = str(s).replace("$", "").replace(",", "").strip()
    return _f(s)


def parse_nasdaq(obj):
    d = (obj or {}).get("data") if isinstance(obj, dict) else None
    if not d:
        return None
    p = d.get("primaryData") or {}
    price = _price_str(p.get("lastSalePrice"))
    if not price or price <= 0:
        return None
    t = None
    m = _NQ_TS.search(str(p.get("lastTradeTimestamp") or ""))
    if m:
        try:
            x = dt.datetime.strptime("%s %s %s %s:%s %s" % m.groups(), "%b %d %Y %I:%M %p")
            t = x.replace(tzinfo=C.TZ["US"]).astimezone(dt.timezone.utc)
        except ValueError:
            t = None
    status = str(d.get("marketStatus") or "").lower()
    kind = ("post" if ("after" in status or "post" in status) else "pre" if "pre" in status
            else "regular" if "open" in status else "close")
    return {"price": price, "time": t, "prev_close": None, "name": d.get("companyName"),
            "kind": kind, "realtime": p.get("isRealTime") if isinstance(p.get("isRealTime"), bool) else None,
            "source": "nasdaq", "extra": {"net_change": p.get("netChange"), "pct": p.get("percentageChange")}}


def fetch_nasdaq(symbols, now=None):
    out, err = {}, None
    hdr = {"Accept": "application/json, text/plain, */*", "Origin": "https://www.nasdaq.com",
           "Referer": "https://www.nasdaq.com/"}
    for s in symbols:
        if C.market_of(s) != "US":
            continue
        if _over_budget():
            err = "超出时间预算"
            break
        code = urllib.parse.quote(C.plain_code(s))
        raw = None
        for ac in ("stocks", "etf"):
            obj, r = C.http_json("https://api.nasdaq.com/api/quote/%s/info?assetclass=%s" % (code, ac),
                                 headers=hdr, timeout=6)
            C.log("fetch", src="nasdaq", **r.brief())
            raw = parse_nasdaq(obj) if obj else None
            if raw:
                break
            if obj is None:
                err = r.error
        if raw:
            out[s] = raw
    return out, (err if not out else None)


# ================================================================== Robinhood（夜盘候选，未公开接口）
def _rh_time(s):
    return C.parse_iso(s) if s else None


def parse_robinhood(obj):
    out = {}
    for r in ((obj or {}).get("results") or []) if isinstance(obj, dict) else []:
        if not r or not r.get("symbol"):
            continue
        ext = _f(r.get("last_extended_hours_trade_price"))
        bid, ask = _f(r.get("bid_price")), _f(r.get("ask_price"))
        t = _rh_time(r.get("venue_last_non_reg_trade_time")) or _rh_time(r.get("updated_at"))
        basis, price = "last", ext
        if not price and bid and ask and bid > 0 and ask > 0:
            price, basis, t = (bid + ask) / 2.0, "mid", _rh_time(r.get("updated_at"))
        if not price or price <= 0:
            continue
        out[r["symbol"].upper() + ".US"] = {
            "price": price, "time": t, "prev_close": _f(r.get("previous_close")), "name": None,
            "kind": "night", "realtime": None, "source": "robinhood",
            "extra": {"bid": bid, "ask": ask, "basis": basis, "last_trade_price": _f(r.get("last_trade_price"))}}
    return out


def fetch_robinhood(symbols, now=None):
    us = [s for s in symbols if C.market_of(s) == "US"]
    if not us:
        return {}, None
    url = "https://api.robinhood.com/quotes/?symbols=" + ",".join(C.plain_code(s) for s in us)
    obj, r = C.http_json(url, timeout=8)
    C.log("fetch", src="robinhood", **r.brief())
    if obj is None:
        return {}, r.error
    return parse_robinhood(obj), None


FETCH = {"sina": fetch_sina, "yahoo": fetch_yahoo, "finnhub": fetch_finnhub,
         "nasdaq": fetch_nasdaq, "robinhood": fetch_robinhood}


# ================================================================== 编排
def _chain(market, sess, feats, key):
    if market == "US":
        if sess == "regular":
            c = ["finnhub", "yahoo", "nasdaq", "sina"]
        elif sess in ("pre", "post"):
            c = ["nasdaq", "yahoo", "sina"]
        elif sess == "night":
            c = (["robinhood"] if feats.get("us_night") else []) + ["yahoo"]
        else:
            c = ["yahoo", "finnhub", "nasdaq", "sina"]
        if not key:
            c = [x for x in c if x != "finnhub"]
        return c
    return ["sina", "yahoo"]


def _freshness(raw, sess, now):
    t = raw.get("time")
    if t is None:
        return "unknown"
    if sess in ("closed", "lunch"):
        return "fresh"
    limit = {"regular": 20, "auction": 20, "closing": 30, "pre": 30, "post": 30, "night": 60}.get(sess, 20) * 60  # Fix (2026-11-12): no session counts as real-time beyond 60 minutes
    return "fresh" if (now - t).total_seconds() <= limit else "stale"


def _status_label(market, sess, raw_src, kind, age, holiday, realtime_flag):
    if holiday:
        return "今日可能休市"
    if sess == "lunch":
        return "午休"
    if sess not in C.TRADING_SESSIONS[market]:
        return "收盘"
    if raw_src == "robinhood":
        return "夜盘参考价" + ("" if age is None or age <= 180 else "（%d分钟前）" % round(age / 60))
    if realtime_flag is False:
        return "延迟"
    if age is None:
        return "时间未知"
    return "实时" if age <= 180 else "%d分钟前" % round(age / 60)


def _finalize(sym, raw, verdict, sess, now, tried, feats):
    m = C.market_of(sym)
    sd = C.session_date(m, now)
    price, ptime, src = raw["price"], raw.get("time"), raw["source"]
    kind = raw.get("kind") or "regular"
    if raw.get("ext_price") and raw.get("ext_time") and (ptime is None or raw["ext_time"] >= ptime):
        price, ptime = raw["ext_price"], raw["ext_time"]
        kind = sess if sess in ("pre", "post", "night") else "post"
    # 统一 kind
    trading = sess in C.TRADING_SESSIONS[m]
    if m != "US":
        kind = "regular" if (trading and not raw.get("no_trade_yet")) else "close"
    elif src == "finnhub" or (src == "sina" and not raw.get("ext_price")):
        kind = "regular" if sess == "regular" else "close"
    night_unavailable = bool(m == "US" and sess == "night" and src != "robinhood")
    if night_unavailable and kind not in ("pre", "post"):
        kind = "close"

    prev, prev_src = None, None
    bars = []
    if m == "US":
        bars = daily_bars(sym, sd, need_today=(sess == "post"))
        prev = prev_from_bars(bars, sd)
        prev_src = "yahoo日线" if prev else None
    if prev is None:
        prev, prev_src = raw.get("prev_close"), src
    if prev is None and m != "US":
        bars = daily_bars(sym, sd)
        prev, prev_src = prev_from_bars(bars, sd), "yahoo日线"

    reg_close = None
    if m == "US":
        if sess == "post":
            reg_close = close_on(bars, sd) or raw.get("reg_price")
        elif sess in ("pre", "night"):
            reg_close = prev
    ext_pct = None
    if kind in ("pre", "post", "night") and reg_close:
        ext_pct = (price / reg_close - 1) * 100

    day_change = (price - prev) if prev else None
    day_pct = (day_change / prev * 100) if prev else None
    age = (now - ptime).total_seconds() if ptime else None
    pdate = ptime.astimezone(C.TZ[m]).date().isoformat() if ptime else None
    holiday = bool(C.should_have_traded(m, now) and sess != "night" and pdate and pdate < sd.isoformat())
    return {
        "symbol": sym, "market": m, "name": raw.get("name"), "currency": C.currency_of(sym),
        "price": price, "price_time": C.iso(ptime), "price_kind": kind,
        "session": sess, "session_label": C.session_label(m, now),
        "prev_close": prev, "prev_close_source": prev_src,
        "day_change": day_change, "day_change_pct": day_pct,
        "reg_close": reg_close, "ext_change_pct": ext_pct,
        "source": src, "sources_tried": tried, "cross": None,
        "age_sec": age, "stale": verdict == "stale", "time_unknown": verdict == "unknown",
        "holiday_hint": holiday, "night_unavailable": night_unavailable,
        "status_label": _status_label(m, sess, src, kind, age, holiday, raw.get("realtime")),
        "extra": raw.get("extra") or {}, "error": None,
    }


def _cross_check(sym, q, sess, now, key):
    m = C.market_of(sym)
    src = q["source"]
    if sess == "night":
        return None
    if m == "US":
        alt = {"finnhub": "yahoo", "nasdaq": "yahoo", "sina": "yahoo",
               "yahoo": ("finnhub" if (key and sess == "regular") else "nasdaq")}.get(src)
    else:
        alt = {"sina": "yahoo", "yahoo": "sina"}.get(src)
    if not alt or _over_budget():
        return None
    raws, _ = FETCH[alt]([sym], now)
    r = raws.get(sym)
    if not r or not r.get("price"):
        return {"source": alt, "price": None, "diff_pct": None, "flag": False, "ok": False}
    diff = abs(r["price"] - q["price"]) / q["price"] * 100
    return {"source": alt, "price": r["price"], "diff_pct": round(diff, 3), "flag": diff > 1.0, "ok": True}


def _quotes_market(m, syms, now, feats, key, cross):
    sess = C.session(m, now, night=True)
    chain = _chain(m, sess, feats, key)
    accepted, stale, tried, errs = {}, {}, {s: [] for s in syms}, {}
    remaining = list(syms)
    for src in chain:
        if not remaining or _over_budget():
            break
        try:
            raws, err = FETCH[src](remaining, now)
        except Exception as e:  # 任何源的意外错误都只导致降级
            raws, err = {}, "%s: %s" % (type(e).__name__, e)
        if err:
            errs[src] = err
        for s in remaining:
            tried[s].append(src if s in raws else src + "✗")
        for s, raw in raws.items():
            v = _freshness(raw, sess, now)
            if v in ("fresh", "unknown"):
                accepted[s] = (raw, v)
            else:
                stale.setdefault(s, (raw, v))
        remaining = [s for s in remaining if s not in accepted]
    for s in list(remaining):
        if s in stale:
            accepted[s] = stale[s]
    out = {}
    for s in syms:
        if s not in accepted:
            out[s] = {"symbol": s, "market": m, "name": None, "currency": C.currency_of(s), "price": None,
                      "session": sess, "sources_tried": tried[s], "error": "所有数据源均失败：" + (
                          "；".join("%s: %s" % kv for kv in errs.items()) or "无返回"),
                      "status_label": "暂无数据"}
            continue
        raw, v = accepted[s]
        try:
            q = _finalize(s, raw, v, sess, now, tried[s], feats)
        except Exception as e:
            out[s] = {"symbol": s, "market": m, "price": None, "session": sess, "sources_tried": tried[s],
                      "error": "整理数据失败：%s" % e, "status_label": "暂无数据", "currency": C.currency_of(s),
                      "name": raw.get("name")}
            continue
        if cross and q["price"]:
            try:
                q["cross"] = _cross_check(s, q, sess, now, key)
            except Exception:
                q["cross"] = None
        out[s] = q
    return out


def get_quotes(symbols, now=None, cross=False, budget=45):
    now = now or C.now_utc()
    symbols = list(dict.fromkeys(symbols))
    feats, key = C.features(), C.finnhub_key()
    _BUDGET["deadline"] = time.time() + budget
    result = {}
    try:
        for m in C.MARKET_ORDER:
            syms = [s for s in symbols if C.market_of(s) == m]
            if syms:
                result.update(_quotes_market(m, syms, now, feats, key, cross))
    finally:
        _BUDGET["deadline"] = None
    return result


# ================================================================== 缓存与走势点
def load_cache():
    return C.load_json(C.path("state", "quotes_cache.json"), {}) or {}


def save_cache(quotes):
    cache = load_cache()
    for s, q in quotes.items():
        if q.get("price"):
            cache[s] = q
    C.save_json(C.path("state", "quotes_cache.json"), cache)


def append_series(quotes, keep=300):
    p = C.path("state", "series.json")
    ser = C.load_json(p, {}) or {}
    for s, q in quotes.items():
        if not q.get("price") or not q.get("price_time"):
            continue
        pts = ser.setdefault(s, [])
        if not pts or pts[-1][0] != q["price_time"]:
            pts.append([q["price_time"], q["price"]])
        ser[s] = pts[-keep:]
    C.save_json(p, ser)


def main(argv=None):
    ap = argparse.ArgumentParser(description="取行情")
    ap.add_argument("--symbols", help="逗号分隔，如 NVDA.US,00700.HK；默认取全部持仓")
    ap.add_argument("--cross", action="store_true", help="交叉核对第二个数据源")
    a = ap.parse_args(argv)
    C.ensure_dirs()
    if a.symbols:
        syms = [C.normalize_symbol(x) for x in a.symbols.split(",") if x.strip()]
    else:
        import holdings
        syms = [p["symbol"] for p in holdings.load()["positions"]]
    q = get_quotes(syms, cross=a.cross)
    save_cache(q)
    print(json.dumps(q, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
