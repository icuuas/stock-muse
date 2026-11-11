#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自测：逐项实测数据源，写入 state/selftest.json，并据此开启或关闭功能开关。

原则：测不了的写 pending，不写成“能用”。需要特定时段才能验证的项目（美股夜盘、
港股/A 股盘中延迟）在对应时段重新运行即可补测。

用法：selftest.py [--json]
"""
import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402
import quotes as Q  # noqa: E402

DEFAULT_TEST = {"US": "AAPL.US", "HK": "00700.HK", "CN": "600519.SH"}


def _r(name, ok, detail, pending=False):
    return {"name": name, "ok": ok, "pending": pending, "detail": detail}


def _src_check(src, sym, now):
    t0 = dt.datetime.now()
    try:
        raws, err = Q.FETCH[src]([sym], now)
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, e), 0.0
    el = (dt.datetime.now() - t0).total_seconds()
    return raws.get(sym), err, el


def run_selftest(now=None):
    now = now or C.now_utc()
    prefs = C.load_prefs()
    old = (C.load_json(C.path("state", "selftest.json"), {}) or {})
    feats = dict(C.FEATURE_DEFAULTS)
    feats.update(old.get("features", {}))
    results, delay = [], dict(old.get("delay", {}))

    # 1 环境
    results.append(_r("python 版本", sys.version_info >= (3, 9), sys.version.split()[0]))
    try:
        C.ensure_dirs()
        probe = C.path("state", ".write_test")
        open(probe, "w").write("ok")
        os.remove(probe)
        results.append(_r("数据目录可写", True, C.home()))
    except OSError as e:
        results.append(_r("数据目录可写", False, str(e)))
    try:
        C.TZ["US"].utcoffset(now)
        results.append(_r("时区数据", True, "zoneinfo 可用"))
    except Exception as e:
        results.append(_r("时区数据", False, str(e)))

    # 2 测试标的：优先用户持仓，没有就用默认
    import holdings
    held = [p["symbol"] for p in holdings.load()["positions"]]
    test = {}
    for m in C.MARKET_ORDER:
        mine = [s for s in held if C.market_of(s) == m]
        test[m] = mine[0] if mine else DEFAULT_TEST[m]
    key = C.finnhub_key()

    # 3 行情源
    plan = {"US": ["finnhub", "yahoo", "nasdaq", "sina"], "HK": ["sina", "yahoo"], "CN": ["sina", "yahoo"]}
    prices = {}
    for m, srcs in plan.items():
        sym = test[m]
        sess = C.session(m, now)
        for src in srcs:
            if src == "finnhub" and not key:
                results.append(_r("US finnhub 行情", False, "没有 key", pending=True))
                continue
            raw, err, el = _src_check(src, sym, now)
            if raw:
                age = (now - raw["time"]).total_seconds() if raw.get("time") else None
                prices.setdefault(m, {})[src] = raw["price"]
                d = "价 %s｜数据时间 %s｜距今 %s｜耗时 %.1fs｜时段 %s" % (
                    raw["price"], C.iso(raw.get("time")) or "无", "未知" if age is None else "%d 秒" % age, el, sess)
                if raw.get("time") and sess in C.TRADING_SESSIONS[m] and m != "US" or (m == "US" and sess == "regular"):
                    delay["%s:%s" % (m, src)] = age
                results.append(_r("%s %s 行情" % (m, src), True, d))
            else:
                results.append(_r("%s %s 行情" % (m, src), False, "失败：%s｜耗时 %.1fs" % (err or "无数据", el)))
        ps = prices.get(m, {})
        if len(ps) >= 2:
            vals = list(ps.values())
            diff = (max(vals) - min(vals)) / min(vals) * 100
            results.append(_r("%s 多源价格一致性" % m, diff < 1.0, "最大相差 %.3f%%（%s）" % (diff, ps)))

    # 4 新浪美股时间字段（时区假设是否成立）
    r_sina, _, _ = _src_check("sina", test["US"], now)
    r_yh, _, _ = _src_check("yahoo", test["US"], now)
    if r_sina and r_yh and r_sina.get("time") and r_yh.get("time"):
        mins = abs((r_sina["time"] - r_yh["time"]).total_seconds()) / 60
        results.append(_r("新浪美股时间字段时区", mins < 45, "与 Yahoo 的时间差 %.0f 分钟（>45 分钟说明时区假设不对）" % mins))

    # 5 美股夜盘
    sess = C.session("US", now)
    if sess == "night":
        raw, err, el = _src_check("robinhood", test["US"], now)
        if raw and raw.get("time") and (now - raw["time"]).total_seconds() <= 3 * 3600:
            feats["us_night"] = True
            results.append(_r("美股夜盘（Robinhood）", True, "价 %s｜基准 %s｜时间 %s" % (
                raw["price"], raw["extra"].get("basis"), C.iso(raw["time"]))))
        else:
            feats["us_night"] = False
            results.append(_r("美股夜盘（Robinhood）", False, "未取到可用夜盘价：%s" % (err or "无数据或时间过旧")))
    else:
        results.append(_r("美股夜盘（Robinhood）", False,
                          "当前不在夜盘时段（美东 20:00–04:00），待补测；验证前保持关闭", pending=True))

    # 6 港股 / A 股盘中延迟
    for m in ("CN", "HK"):
        if C.session(m, now) not in C.TRADING_SESSIONS[m]:
            results.append(_r("%s 盘中延迟" % m, False, "当前不在盘中，待补测", pending=True))

    # 7 新闻与公告
    ua = prefs["sec_user_agent"]
    if key:
        obj, r = C.http_json("https://finnhub.io/api/v1/company-news?symbol=AAPL&from=%s&to=%s&token=%s" % (
            (now - dt.timedelta(days=1)).date().isoformat(), now.date().isoformat(), key), timeout=8)
        ok = isinstance(obj, list)
        feats["us_news"] = ok
        results.append(_r("美股新闻（Finnhub company-news）", ok, "24 小时 %d 条" % len(obj) if ok else (r.error or "失败")))
    else:
        feats["us_news"] = False
        results.append(_r("美股新闻（Finnhub company-news）", False, "没有 key", pending=True))
    import news as N
    cik = N._cik_map(ua).get("AAPL")
    if cik:
        obj, r = C.http_json("https://data.sec.gov/submissions/CIK%010d.json" % cik, headers={"User-Agent": ua}, timeout=10)
        ok = isinstance(obj, dict) and "filings" in obj
    else:
        ok, r = False, None
    feats["edgar"] = ok
    results.append(_r("美股公告（SEC EDGAR）", ok, "可用" if ok else "失败：%s" % (r.error if r else "取不到 CIK 映射")))
    obj, r = C.http_json("https://np-anotice-stock.eastmoney.com/api/security/ann?sr=-1&page_size=5&page_index=1"
                         "&ann_type=A&client_source=web&stock_list=600519", timeout=8)
    ok = isinstance(obj, dict) and isinstance((obj.get("data") or {}).get("list"), list)
    feats["cn_announce"] = ok
    results.append(_r("A 股公告（东方财富）", ok, "可用" if ok else "失败：%s" % r.error))
    obj, r = C.http_json("https://query1.finance.yahoo.com/v1/finance/search?q=0700.HK&newsCount=5&quotesCount=0", timeout=8)
    ok = isinstance(obj, dict) and isinstance(obj.get("news"), list)
    feats["cn_hk_news"] = ok
    results.append(_r("港股、A 股新闻（Yahoo search）", ok, "返回 %d 条" % len(obj["news"]) if ok else "失败：%s" % r.error))
    feats["hk_announce"] = False
    r_hk = _r("港股公告（披露易）", False, "目前没有可用的自动抓取源，已关闭；可尝试浏览器流程后再手动开启")
    r_hk["unsupported"] = True
    results.append(r_hk)

    # 8 汇率
    import fx as FX
    fxr = FX.get_rates("CNY", now=now, max_age=0)
    results.append(_r("汇率（Yahoo）", bool(fxr.get("ok")) and not fxr.get("stale"),
                      "USD/CNY=%s HKD/CNY=%s" % (fxr.get("pairs", {}).get("USDCNY"), fxr.get("pairs", {}).get("HKDCNY"))
                      if fxr.get("ok") else fxr.get("error", "失败")))

    pending = [x["name"] for x in results if x["pending"]]
    state = {"ts": C.iso(now), "features": feats, "pending": pending, "delay": delay, "results": results}
    C.save_json(C.path("state", "selftest.json"), state)
    return state


def main(argv=None):
    ap = argparse.ArgumentParser(description="自测")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    st = run_selftest()
    if a.json:
        print(json.dumps(st, ensure_ascii=False, indent=2, default=str))
    else:
        for r in st["results"]:
            tag = "待测" if r["pending"] else ("未支持" if r.get("unsupported") else ("通过" if r["ok"] else "失败"))
            print("%s %s：%s" % (tag, r["name"], r["detail"]))
        print("功能开关：" + json.dumps(st["features"], ensure_ascii=False))
        if st["pending"]:
            print("待补测：" + "、".join(st["pending"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
