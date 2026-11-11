# -*- coding: utf-8 -*-
"""公共函数（仅用 Python 标准库）：路径、JSON 读写、HTTP、市场时段、代码规范、格式化。"""
import datetime as dt
import gzip
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.request
import zlib
from zoneinfo import ZoneInfo

VERSION = "0.1.0"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")

# ----------------------------------------------------------------- 路径
def home():
    return os.path.expanduser(os.environ.get("STOCK_PUSH_HOME", "~/workspace/stock-push"))


def path(*parts):
    return os.path.join(home(), *parts)


def ensure_dirs():
    for d in ("data", "state", "outbox", "logs", "secrets"):
        os.makedirs(path(d), exist_ok=True)
    try:
        os.chmod(path("secrets"), 0o700)
    except OSError:
        pass


# ----------------------------------------------------------------- JSON
def load_json(p, default=None, strict=False):
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError):
        if strict:
            raise
        return default


def save_json(p, obj, backup=False):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if backup and os.path.exists(p):
        shutil.copy2(p, p + ".bak")
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def append_jsonl(p, obj):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")


# ----------------------------------------------------------------- 时间
def parse_iso(s):
    """解析 ISO 时间字符串，返回 UTC aware datetime。失败返回 None。"""
    if not s or not isinstance(s, str):
        return None
    s = s.strip().replace("Z", "+00:00")
    if "T" not in s and " " in s:
        s = s.replace(" ", "T", 1)
    m = re.match(r"^(.*?T\d{2}:\d{2}:\d{2})(\.\d+)?(.*)$", s)
    if m:
        frac = (m.group(2) or "")[:7]
        frac = frac + "0" * (7 - len(frac)) if frac else ""
        s = m.group(1) + frac + m.group(3)
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.astimezone(dt.timezone.utc)


def now_utc():
    o = os.environ.get("STOCK_PUSH_NOW")
    if o:
        d = parse_iso(o)
        if d:
            return d
    return dt.datetime.now(dt.timezone.utc)


def iso(d):
    if d is None:
        return None
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_ts(ts):
    try:
        return dt.datetime.fromtimestamp(float(ts), dt.timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


# ----------------------------------------------------------------- 日志
def log(event, **kw):
    """写运行日志。注意：调用方不得传入持仓数量等敏感信息。"""
    try:
        ensure_dirs()
        now = now_utc()
        p = path("logs", now.strftime("%Y-%m-%d") + ".log")
        first = not os.path.exists(p)
        append_jsonl(p, dict(ts=iso(now), event=event, **kw))
        if first:
            _prune_logs(now)
    except Exception:
        pass


def _prune_logs(now, keep_days=14):
    try:
        for fn in os.listdir(path("logs")):
            m = re.match(r"(\d{4}-\d{2}-\d{2})\.log$", fn)
            if m and (now.date() - dt.date.fromisoformat(m.group(1))).days > keep_days:
                os.remove(path("logs", fn))
    except Exception:
        pass


# ----------------------------------------------------------------- HTTP
class HttpResult:
    def __init__(self, status=0, text="", elapsed=0.0, error=None, url=""):
        self.status = status
        self.text = text
        self.elapsed = elapsed
        self.error = error
        self.url = url

    @property
    def ok(self):
        return self.status == 200 and not self.error

    def brief(self):
        return {"status": self.status, "elapsed": round(self.elapsed, 2), "error": self.error}


def http_get(url, headers=None, timeout=8, encoding="utf-8", data=None, method=None):
    h = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "gzip, deflate"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    t0 = time.time()
    status, raw, enc_hdr = 0, b"", ""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status = r.status
            raw = r.read()
            enc_hdr = r.headers.get("Content-Encoding", "") or ""
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            raw = e.read()
            enc_hdr = e.headers.get("Content-Encoding", "") or ""
        except Exception:
            raw = b""
    except Exception as e:  # 超时、DNS、被代理拒绝等
        return HttpResult(0, "", time.time() - t0, "%s: %s" % (type(e).__name__, e), url)
    try:
        if "gzip" in enc_hdr:
            raw = gzip.decompress(raw)
        elif "deflate" in enc_hdr:
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                raw = zlib.decompress(raw, -15)
    except Exception:
        pass
    text = raw.decode(encoding, errors="replace")
    err = None if status == 200 else "HTTP %s" % status
    return HttpResult(status, text, time.time() - t0, err, url)


def http_json(url, **kw):
    r = http_get(url, **kw)
    if not r.ok:
        return None, r
    try:
        return json.loads(r.text), r
    except json.JSONDecodeError:
        r.error = "JSON 解析失败"
        return None, r


# ----------------------------------------------------------------- 市场与时段
TZ = {
    "US": ZoneInfo("America/New_York"),
    "CN": ZoneInfo("Asia/Shanghai"),
    "HK": ZoneInfo("Asia/Hong_Kong"),
}
CURRENCY = {"US": "USD", "HK": "HKD", "CN": "CNY"}
MARKET_LABEL = {"US": "美股", "HK": "港股", "CN": "A股"}
MARKET_ORDER = ["US", "HK", "CN"]
CUR_SYMBOL = {"USD": "$", "HKD": "HK$", "CNY": "¥"}
TZ_ABBR = {"US": "ET", "CN": "北京", "HK": "香港"}


def local(market, now=None):
    return (now or now_utc()).astimezone(TZ[market])


def _minutes(loc):
    return loc.hour * 60 + loc.minute


def session(market, now=None, night=True):
    """返回当前时段：
    US: pre/regular/post/night/closed；CN: auction/regular/lunch/closed；
    HK: regular/lunch/closing/closed。"""
    loc = local(market, now)
    wd, t = loc.weekday(), _minutes(loc)
    if market == "US":
        s = "closed"
        if wd <= 4:
            if t < 240:
                s = "night"
            elif t < 570:
                s = "pre"
            elif t < 960:
                s = "regular"
            elif t < 1200:
                s = "post"
            else:
                s = "night" if wd <= 3 else "closed"
        elif wd == 6 and t >= 1200:
            s = "night"
        if s == "night" and not night:
            s = "closed"
        return s
    if wd > 4:
        return "closed"
    if market == "CN":
        if 555 <= t < 570:
            return "auction"
        if 570 <= t < 690 or 780 <= t < 900:
            return "regular"
        if 690 <= t < 780:
            return "lunch"
        return "closed"
    if market == "HK":
        if 570 <= t < 720 or 780 <= t < 960:
            return "regular"
        if 720 <= t < 780:
            return "lunch"
        if 960 <= t < 970:
            return "closing"
        return "closed"
    return "closed"


def _prev_weekday(d):
    d -= dt.timedelta(days=1)
    while d.weekday() > 4:
        d -= dt.timedelta(days=1)
    return d


def _next_weekday(d):
    d += dt.timedelta(days=1)
    while d.weekday() > 4:
        d += dt.timedelta(days=1)
    return d


def session_date(market, now=None):
    """“当前所属交易日”。用于确定前收盘 = 早于该日的最近一根日线收盘价。
    美股夜盘（20:00 ET 起）属于下一个交易日；周末显示上一个交易日。"""
    loc = local(market, now)
    d, wd, t = loc.date(), loc.weekday(), _minutes(loc)
    if market == "US":
        if wd <= 3:
            return d if t < 1200 else d + dt.timedelta(days=1)
        if wd == 4:
            return d
        if wd == 5:
            return d - dt.timedelta(days=1)
        return d - dt.timedelta(days=2) if t < 1200 else d + dt.timedelta(days=1)
    if wd > 4:
        return _prev_weekday(d)
    return d if t >= 555 else _prev_weekday(d)


def should_have_traded(market, now=None):
    """工作日且已过开盘 15 分钟：此时若最新成交不是当天，疑似休市。"""
    loc = local(market, now)
    return loc.weekday() <= 4 and _minutes(loc) >= (600 if market == "US" else 585)


SESSION_LABEL = {
    "US": {"pre": "盘前", "regular": "盘中", "post": "盘后", "night": "夜盘"},
    "CN": {"auction": "集合竞价", "regular": "盘中", "lunch": "午休"},
    "HK": {"regular": "盘中", "lunch": "午休", "closing": "收市竞价"},
}


def session_label(market, now=None, night=True):
    s = session(market, now, night)
    if s != "closed":
        return SESSION_LABEL[market].get(s, s)
    loc = local(market, now)
    if loc.weekday() > 4:
        return "休市"
    if market == "US":
        return "已收盘"
    return "未开盘" if _minutes(loc) < 555 else "已收盘"


TRADING_SESSIONS = {
    "US": ("pre", "regular", "post", "night"),
    "CN": ("regular",),
    "HK": ("regular", "closing"),
}


# ----------------------------------------------------------------- 代码规范
class SymbolError(ValueError):
    pass


def _cn(code, exch=None):
    if code[0] in "48" or code.startswith("92"):
        raise SymbolError("暂不支持北交所代码 %s" % code)
    if code[0] in "29":
        raise SymbolError("暂不支持 B 股代码 %s" % code)
    if code[0] == "6":
        e = "SH"
    elif code[0] in "03":
        e = "SZ"
    else:
        raise SymbolError("无法识别的 A 股代码 %s" % code)
    if exch and exch != e:
        raise SymbolError("代码 %s 与交易所 %s 不匹配" % (code, exch))
    return "%s.%s" % (code, e)


def normalize_symbol(text):
    """统一成 NVDA.US / 00700.HK / 600519.SH / 000001.SZ。无法判断时抛 SymbolError。"""
    s = (text or "").strip().upper().replace(" ", "")
    if not s:
        raise SymbolError("代码为空")
    m = re.fullmatch(r"(SH|SZ|BJ)(\d{6})", s)
    if m:
        return _cn(m.group(2), None if m.group(1) == "BJ" else m.group(1))
    m = re.fullmatch(r"(\d{6})\.(SS|SH|SZ|BJ)", s)
    if m:
        ex = {"SS": "SH"}.get(m.group(2), m.group(2))
        return _cn(m.group(1), None if ex == "BJ" else ex)
    m = re.fullmatch(r"HK(\d{1,5})", s) or re.fullmatch(r"(\d{1,5})\.HK", s)
    if m:
        return "%s.HK" % m.group(1).zfill(5)
    if re.fullmatch(r"\d{6}", s):
        return _cn(s)
    if re.fullmatch(r"\d{5}", s):
        return "%s.HK" % s
    if re.fullmatch(r"\d{1,4}", s):
        raise SymbolError("无法判断市场，请写成 00700.HK 这样的格式")
    s2 = s[:-3] if s.endswith(".US") else s
    if re.fullmatch(r"[A-Z]{1,5}([.\-][A-Z])?", s2):
        return "%s.US" % s2.replace("-", ".")
    raise SymbolError("无法识别的代码：%s" % text)


def split_symbol(sym):
    code, suf = sym.rsplit(".", 1)
    return code, suf


def market_of(sym):
    suf = sym.rsplit(".", 1)[1]
    return {"US": "US", "HK": "HK", "SH": "CN", "SZ": "CN"}[suf]


def currency_of(sym):
    return CURRENCY[market_of(sym)]


def sina_code(sym):
    code, suf = split_symbol(sym)
    if suf == "US":
        return "gb_" + code.lower()
    if suf in ("SH", "SZ"):
        return suf.lower() + code
    return "hk" + code


def yahoo_code(sym):
    code, suf = split_symbol(sym)
    if suf == "US":
        return code.replace(".", "-")
    if suf == "SH":
        return code + ".SS"
    if suf == "SZ":
        return code + ".SZ"
    return str(int(code)).zfill(4) + ".HK"


def plain_code(sym):
    return split_symbol(sym)[0]


# ----------------------------------------------------------------- 偏好与功能开关
DEFAULT_PREFS = {
    "base_currency": "CNY",
    "user_timezone": "Asia/Shanghai",
    "color_scheme": "red_up",          # red_up=红涨绿跌；green_up=绿涨红跌
    "quiet_hours": None,               # 如 "23:00-07:00"（用户时区）
    "daily_cap": 15,
    "info_level": "standard",          # minimal / standard / detailed
    "report_mode": "per_market",       # per_market / merged
    "monitor_mode": "balanced",        # realtime / balanced / eco
    "paused_until": None,
    "news_cap_per_symbol": 3,
    "space_refresh_minutes": 10,
    "sec_user_agent": "stock-push/0.1 (personal use; set contact in prefs.json)",
    "enable": {"snapshot": True, "alerts": True, "reports": True, "night": True, "news": True},
}

MODE_MINUTES = {
    "realtime": {"regular": 1, "ext": 2, "night": 5},
    "balanced": {"regular": 5, "ext": 5, "night": 15},
    "eco": {"regular": 15, "ext": 15, "night": 30},
}

FEATURE_DEFAULTS = {
    "us_night": False,      # 美股夜盘：自测通过才开启
    "us_news": False,       # Finnhub 公司新闻
    "edgar": False,         # SEC EDGAR 公告
    "cn_announce": False,   # 东财 A 股公告
    "cn_hk_news": False,    # 港股、A 股新闻
    "hk_announce": False,   # 港交所披露易：目前无可用源
}


def load_prefs():
    p = dict(DEFAULT_PREFS)
    p["enable"] = dict(DEFAULT_PREFS["enable"])
    user = load_json(path("data", "prefs.json"), {}) or {}
    for k, v in user.items():
        if k == "enable" and isinstance(v, dict):
            p["enable"].update(v)
        else:
            p[k] = v
    return p


def features():
    f = dict(FEATURE_DEFAULTS)
    st = load_json(path("state", "selftest.json"), {}) or {}
    f.update(st.get("features", {}))
    return f


def finnhub_key():
    k = os.environ.get("FINNHUB_KEY")
    if k:
        return k.strip()
    try:
        with open(path("secrets", "finnhub.key"), "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


# ----------------------------------------------------------------- 格式化
def fmt_shares(x):
    if x is None:
        return "—"
    return str(int(x)) if abs(x - round(x)) < 1e-9 else ("%.4f" % x).rstrip("0").rstrip(".")


def fmt_price(v, cur):
    if v is None:
        return "—"
    nd = 3 if abs(v) < 1 else 2
    return "%s%s" % (CUR_SYMBOL.get(cur, ""), format(v, ",.%df" % nd))


def fmt_money(v, cur, sign=False):
    if v is None:
        return "—"
    nd = 0 if abs(v) >= 10000 else 2
    s = format(abs(v), ",.%df" % nd)
    sg = "-" if v < 0 and round(abs(v), nd) != 0 else ("+" if sign and v != 0 else "")
    return "%s%s%s" % (sg, CUR_SYMBOL.get(cur, ""), s)


def fmt_pct(v, nd=2, sign=True):
    if v is None:
        return "—"
    s = format(v, "+.%df" % nd if sign else ".%df" % nd)
    return s + "%"


def trend_icon(v, scheme="red_up"):
    if v is None or abs(v) < 1e-12:
        return "⚪"
    up = v > 0
    if scheme == "green_up":
        return "🟢" if up else "🔴"
    return "🔴" if up else "🟢"


def user_label(tzname):
    return "北京" if tzname == "Asia/Shanghai" else tzname.split("/")[-1]


def fmt_time(market, d, user_tz="Asia/Shanghai"):
    """如：16:42 ET（北京 05:42）；与用户时区偏移相同则只写一个。"""
    if d is None:
        return "时间未知"
    loc = d.astimezone(TZ[market])
    s = "%s %s" % (loc.strftime("%H:%M"), TZ_ABBR[market])
    try:
        u = d.astimezone(ZoneInfo(user_tz))
    except Exception:
        return s
    if u.utcoffset() == loc.utcoffset():
        return s
    return "%s（%s %s）" % (s, user_label(user_tz), u.strftime("%H:%M"))


def fmt_user_time(d, user_tz="Asia/Shanghai"):
    try:
        u = d.astimezone(ZoneInfo(user_tz))
    except Exception:
        u = d
    return "%s %s（%s）" % (u.strftime("%m-%d"), u.strftime("%H:%M"), user_label(user_tz))
