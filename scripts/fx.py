#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""汇率：只用 Yahoo（CNY=X、HKD=X、HKDCNY=X），不混用其他源，避免在岸/离岸价差。

get_rates(base) 返回 {"ok", "to_base": {"USD": x, "HKD": y, "CNY": z}, "pairs": {...}, "time", "source", "stale"}
to_base[币种] 表示 1 单位该币种折合多少 base。
"""
import argparse
import json
import os
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402

PAIRS = {"USDCNY": "CNY=X", "USDHKD": "HKD=X", "HKDCNY": "HKDCNY=X"}


def parse_fx(obj):
    try:
        meta = obj["chart"]["result"][0]["meta"]
        p = float(meta["regularMarketPrice"])
        t = C.from_ts(meta.get("regularMarketTime"))
    except (KeyError, IndexError, TypeError, ValueError):
        return None, None
    return (p, t) if p > 0 else (None, None)


def _fetch_pair(code):
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=5m" % urllib.parse.quote(code, safe="")
    obj, r = C.http_json(url, timeout=6)
    C.log("fetch", src="yahoo_fx", **r.brief())
    return parse_fx(obj) if obj else (None, None)


def to_base_map(pairs, base):
    """pairs: {"USDCNY": x, "USDHKD": y, "HKDCNY": z}（缺失为 None）。"""
    usdcny, usdhkd, hkdcny = pairs.get("USDCNY"), pairs.get("USDHKD"), pairs.get("HKDCNY")
    if base == "CNY":
        return {"CNY": 1.0, "USD": usdcny, "HKD": hkdcny}
    if base == "USD":
        return {"USD": 1.0, "CNY": (1 / usdcny) if usdcny else None, "HKD": (1 / usdhkd) if usdhkd else None}
    if base == "HKD":
        return {"HKD": 1.0, "USD": usdhkd, "CNY": (1 / hkdcny) if hkdcny else None}
    raise ValueError("汇总币种只支持 CNY / USD / HKD")


def get_rates(base="CNY", now=None, max_age=600):
    now = now or C.now_utc()
    p = C.path("state", "fx_cache.json")
    cache = C.load_json(p, {}) or {}
    age = time.time() - cache.get("fetched", 0)
    if cache.get("pairs") and age < max_age and all(cache["pairs"].get(k) for k in PAIRS):
        pairs, t, stale = cache["pairs"], C.parse_iso(cache.get("time")), False
    else:
        pairs, times = {}, []
        for k, code in PAIRS.items():
            v, t = _fetch_pair(code)
            pairs[k] = v
            if t:
                times.append(t)
        t = max(times) if times else None
        stale = False
        if not any(pairs.values()):
            if cache.get("pairs") and age < 24 * 3600:
                pairs, t, stale = cache["pairs"], C.parse_iso(cache.get("time")), True
            else:
                return {"ok": False, "to_base": {}, "pairs": {}, "time": None, "source": "Yahoo", "stale": False,
                        "error": "汇率获取失败"}
        else:
            C.save_json(p, {"fetched": time.time(), "time": C.iso(t), "pairs": pairs})
    return {"ok": True, "to_base": to_base_map(pairs, base), "pairs": pairs, "time": C.iso(t),
            "source": "Yahoo", "stale": stale}


def main(argv=None):
    ap = argparse.ArgumentParser(description="汇率")
    ap.add_argument("--base", default=None)
    a = ap.parse_args(argv)
    C.ensure_dirs()
    base = a.base or C.load_prefs()["base_currency"]
    print(json.dumps(get_rates(base), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
