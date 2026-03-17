import os
import time
import math
import json
import hashlib
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import pandas as pd


# =========================
# ENV
# =========================
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "0.10"))
SCAN_SYMBOL_LIMIT = int(os.getenv("SCAN_SYMBOL_LIMIT", "300"))

TOP_GAINERS_COUNT = int(os.getenv("TOP_GAINERS_COUNT", "5"))
TOP_POWER_COUNT = int(os.getenv("TOP_POWER_COUNT", "5"))

MIN_PRICE = float(os.getenv("MIN_PRICE", "0.30"))
MAX_PRICE = float(os.getenv("MAX_PRICE", "30.00"))

MIN_DAY_VOLUME = int(os.getenv("MIN_DAY_VOLUME", "50000"))
MIN_LAST_1M_VOL = int(os.getenv("MIN_LAST_1M_VOL", "1200"))

FAST_MIN_DAY_CHANGE = float(os.getenv("FAST_MIN_DAY_CHANGE", "2.0"))
FAST_MIN_MINUTE_CHANGE = float(os.getenv("FAST_MIN_MINUTE_CHANGE", "0.10"))
FAST_MIN_RVOL = float(os.getenv("FAST_MIN_RVOL", "1.05"))

TREND_MIN_DAY_CHANGE = float(os.getenv("TREND_MIN_DAY_CHANGE", "3.0"))
TREND_MIN_MINUTE_CHANGE = float(os.getenv("TREND_MIN_MINUTE_CHANGE", "0.00"))
TREND_MIN_RVOL = float(os.getenv("TREND_MIN_RVOL", "0.90"))

RESEND_MOVE_PCT = float(os.getenv("RESEND_MOVE_PCT", "0.35"))
TOP_LIST_MIN_CHANGE_PCT = float(os.getenv("TOP_LIST_MIN_CHANGE_PCT", "0.20"))
ALERT_MAX_PER_SYMBOL = int(os.getenv("ALERT_MAX_PER_SYMBOL", "20"))
DEBUG_MODE = os.getenv("DEBUG_MODE", "true").strip().lower() == "true"

FINNHUB_URL = "https://finnhub.io/api/v1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

session = requests.Session()

last_sent = {}
alert_counter = {}
profile_cache = {}
last_top_gainers_hash = None
last_top_power_hash = None


# =========================
# HELPERS
# =========================
def safe_float(x, default=0.0):
    try:
        if x is None:
            return default
        v = float(x)
        if math.isnan(v):
            return default
        return v
    except Exception:
        return default


def fmt_num(n):
    n = safe_float(n, 0)

    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}".rstrip("0").rstrip(".") + "B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M"
    if n >= 1_000:
        return f"{n / 1_000:.2f}".rstrip("0").rstrip(".") + "K"
    if float(n).is_integer():
        return str(int(n))
    return f"{n:.2f}".rstrip("0").rstrip(".")


def pct_str(x):
    try:
        return f"{float(x):+.2f}%"
    except Exception:
        return "0%"


def get_session_label():
    try:
        now_et = datetime.now(ZoneInfo("America/New_York"))
        hm = now_et.hour * 60 + now_et.minute

        if 4 * 60 <= hm < 9 * 60 + 30:
            return "قبل الافتتاح"
        if 9 * 60 + 30 <= hm < 16 * 60:
            return "وقت السوق"
        if 16 * 60 <= hm < 20 * 60:
            return "بعد الإغلاق"
        return "خارج الجلسة"
    except Exception:
        return "غير محدد"


def require_env():
    missing = []
    if not FINNHUB_API_KEY:
        missing.append("FINNHUB_API_KEY")
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")

    if missing:
        msg = "Missing required variables: " + ", ".join(missing)
        logging.error(msg)
        return False, msg
    return True, ""


def api_get(path: str, params=None, timeout=20):
    params = params or {}
    params["token"] = FINNHUB_API_KEY

    try:
        r = session.get(f"{FINNHUB_URL}{path}", params=params, timeout=timeout)
        if r.status_code == 403:
            logging.warning("403 forbidden on %s", path)
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logging.exception("api_get error | %s | %s", path, e)
        return None


def send_telegram_message(text: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text
    }

    try:
        r = session.post(url, json=payload, timeout=20)
        if r.status_code == 200:
            return True

        logging.error("Telegram send failed: %s | %s", r.status_code, r.text)
        return False
    except Exception as e:
        logging.exception("Telegram exception: %s", e)
        return False


def make_rows_hash(rows, keys):
    compact = []
    for r in rows:
        compact.append({k: r.get(k) for k in keys})
    raw = json.dumps(compact, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def detect_material_top_change(rows, previous_rows_map):
    if not previous_rows_map:
        return True

    for row in rows:
        old = previous_rows_map.get(row["symbol"])
        if old is None:
            return True

        old_day = safe_float(old.get("day_change_pct"), 0)
        new_day = safe_float(row.get("day_change_pct"), 0)

        if abs(new_day - old_day) >= TOP_LIST_MIN_CHANGE_PCT:
            return True

    return False


def should_send(symbol: str, price: float) -> bool:
    old = last_sent.get(symbol)
    if old is None:
        return True

    old_price = safe_float(old.get("price"), 0.0)
    if old_price <= 0:
        return True

    change_pct = ((price - old_price) / old_price) * 100
    return abs(change_pct) >= RESEND_MOVE_PCT


def mark_sent(symbol: str, price: float):
    last_sent[symbol] = {"price": price}
    alert_counter[symbol] = alert_counter.get(symbol, 0) + 1


def get_alert_number(symbol: str) -> int:
    return alert_counter.get(symbol, 0) + 1


def size_label(market_cap_m):
    mc = safe_float(market_cap_m, 0)
    if mc <= 0:
        return "غير محدد"
    if mc < 300:
        return "مايكرو"
    if mc < 2_000:
        return "صغيرة"
    if mc < 10_000:
        return "متوسطة"
    if mc < 200_000:
        return "كبيرة"
    return "عملاقة"


def label_bool(v, yes_text="نعم", no_text="لا"):
    return yes_text if v else no_text


def tag_line(icon, label, value):
    return f"{icon} {label}: {value}"


# =========================
# FINNHUB DATA
# =========================
def get_market_symbols():
    data = api_get("/stock/symbol", {"exchange": "US"}, timeout=40)
    if not data:
        return []

    symbols = []

    for item in data:
        symbol = str(item.get("symbol", "")).strip().upper()
        typ = str(item.get("type", "")).strip().upper()

        if not symbol:
            continue
        if "." in symbol or "^" in symbol:
            continue
        if not symbol.isalpha():
            continue
        if len(symbol) > 5:
            continue
        if typ and typ not in {"COMMON STOCK", "ADR"}:
            continue

        symbols.append(symbol)

    symbols = sorted(set(symbols), key=lambda x: (len(x), x))
    return symbols[:SCAN_SYMBOL_LIMIT]


def get_quote(symbol: str):
    data = api_get("/quote", {"symbol": symbol}, timeout=15)
    if not data:
        return None

    price = safe_float(data.get("c"), 0)
    prev_close = safe_float(data.get("pc"), 0)
    open_price = safe_float(data.get("o"), 0)
    high_price = safe_float(data.get("h"), 0)
    low_price = safe_float(data.get("l"), 0)

    if price <= 0 or prev_close <= 0:
        return None

    day_change_pct = ((price - prev_close) / prev_close) * 100

    return {
        "price": round(price, 4),
        "prev_close": round(prev_close, 4),
        "day_change_pct": round(day_change_pct, 2),
        "open": round(open_price, 4) if open_price > 0 else 0,
        "high": round(high_price, 4) if high_price > 0 else 0,
        "low": round(low_price, 4) if low_price > 0 else 0,
    }


def get_profile(symbol: str):
    cached = profile_cache.get(symbol)
    if cached:
        return cached

    data = api_get("/stock/profile2", {"symbol": symbol}, timeout=15)

    if not data:
        profile = {
            "name": "",
            "industry": "غير محدد",
            "market_cap_m": 0,
            "shares_outstanding_m": 0,
        }
        profile_cache[symbol] = profile
        return profile

    profile = {
        "name": str(data.get("name", "")).strip(),
        "industry": str(data.get("finnhubIndustry", "")).strip() or "غير محدد",
        "market_cap_m": safe_float(data.get("marketCapitalization"), 0),
        "shares_outstanding_m": safe_float(data.get("shareOutstanding"), 0),
    }
    profile_cache[symbol] = profile
    return profile


def get_candles(symbol: str):
    now_ts = int(time.time())
    start_ts = now_ts - (60 * 90)

    data = api_get(
        "/stock/candle",
        {
            "symbol": symbol,
            "resolution": "1",
            "from": start_ts,
            "to": now_ts,
        },
        timeout=20
    )

    if not data or data.get("s") != "ok":
        return None

    df = pd.DataFrame({
        "open": data.get("o", []),
        "high": data.get("h", []),
        "low": data.get("l", []),
        "close": data.get("c", []),
        "volume": data.get("v", []),
    })

    if df.empty or len(df) < 25:
        return None

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna().copy()
    if df.empty or len(df) < 25:
        return None

    latest = df.iloc[-1]
    prev = df.iloc[-2]

    price = safe_float(latest["close"], 0)
    prev_close = safe_float(prev["close"], 0)
    last_1m_vol = safe_float(latest["volume"], 0)

    if price <= 0 or prev_close <= 0:
        return None

    minute_change_pct = ((price - prev_close) / prev_close) * 100

    avg_vol20 = safe_float(df["volume"].rolling(20, min_periods=20).mean().iloc[-1], 0)
    rvol = (last_1m_vol / avg_vol20) if avg_vol20 > 0 else 0.0

    day_volume = safe_float(df["volume"].sum(), 0)

    typical = (df["high"] + df["low"] + df["close"]) / 3.0
    tpv = typical * df["volume"]
    vol_cum = df["volume"].cumsum()

    vwap = 0.0
    if safe_float(vol_cum.iloc[-1], 0) > 0:
        vwap = safe_float((tpv.cumsum() / vol_cum).iloc[-1], 0)

    above_vwap = price > vwap if vwap > 0 else False

    last3 = df.tail(3).copy()
    first3_close = safe_float(last3.iloc[0]["close"], 0)
    last3_close = safe_float(last3.iloc[-1]["close"], 0)
    momentum_3m_pct = ((last3_close - first3_close) / first3_close) * 100 if first3_close > 0 else 0.0

    whale_in = (
        last_1m_vol >= max(5000, avg_vol20 * 2.2)
        and minute_change_pct > 0
        and above_vwap
    )

    current_liquidity_value = price * last_1m_vol
    daily_liquidity_value = price * day_volume

    return {
        "minute_change_pct": round(minute_change_pct, 2),
        "momentum_3m_pct": round(momentum_3m_pct, 2),
        "last_1m_vol": int(last_1m_vol),
        "avg_vol20": round(avg_vol20, 2),
        "rvol": round(rvol, 2),
        "vwap_side": "فوق" if above_vwap else "تحت",
        "above_vwap": above_vwap,
        "day_volume": int(day_volume),
        "whale_in": whale_in,
        "current_liquidity": current_liquidity_value,
        "daily_liquidity": daily_liquidity_value,
    }


# =========================
# SIGNAL ENGINE
# =========================
def classify_signal(day_change_pct, minute_change_pct, momentum_3m_pct, rvol, above_vwap):
    if (
        day_change_pct >= FAST_MIN_DAY_CHANGE
        and minute_change_pct >= FAST_MIN_MINUTE_CHANGE
        and momentum_3m_pct >= 0.15
        and rvol >= FAST_MIN_RVOL
        and above_vwap
    ):
        return "تنبيه دخول سريع"

    if (
        day_change_pct >= TREND_MIN_DAY_CHANGE
        and minute_change_pct >= TREND_MIN_MINUTE_CHANGE
        and rvol >= TREND_MIN_RVOL
        and above_vwap
    ):
        return "تنبيه ترند"

    return None


def detect_state(day_change_pct, minute_change_pct, rvol, above_vwap, whale_in):
    if day_change_pct >= 5 and minute_change_pct < -0.20 and rvol >= 1.2 and not above_vwap:
        return "تصريف"
    if whale_in:
        return "دخول حوت"
    if day_change_pct > 0 and minute_change_pct >= 0 and above_vwap:
        return "زخم إيجابي"
    return "طبيعي"


def decide_action(signal_type, state, above_vwap, minute_change_pct):
    if state == "تصريف":
        return "انتظار"
    if signal_type and above_vwap and minute_change_pct >= 0:
        return "دخول"
    return "مراقبة"


def breakout_text(above_vwap, minute_change_pct):
    if above_vwap and minute_change_pct > 0:
        return "اختراق إيجابي"
    if above_vwap:
        return "فوق الفواب"
    return "تحت الفواب"


def calc_power_score(day_change_pct, minute_change_pct, momentum_3m_pct, rvol, above_vwap, whale_in, day_volume):
    score = (
        max(0, day_change_pct) * 3
        + max(0, minute_change_pct) * 18
        + max(0, momentum_3m_pct) * 10
        + max(0, rvol) * 12
        + (12 if above_vwap else 0)
        + (15 if whale_in else 0)
        + min(day_volume / 500000, 15)
    )
    return round(score, 2)


# =========================
# FORMAT
# =========================
def build_stock_alert_text(m: dict) -> str:
    alert_no = get_alert_number(m["symbol"])

    lines = [
        f"📢 {m['signal_type']} | رقم {alert_no}",
        "",
        tag_line("🏷️", "السهم", m["symbol"]),
        tag_line("💵", "السعر", m["price"]),
        tag_line("📈", "التغير اليومي", pct_str(m["day_change_pct"])),
        tag_line("⏱️", "آخر دقيقة", pct_str(m["minute_change_pct"])),
        tag_line("🚀", "زخم 3 دقائق", pct_str(m["momentum_3m_pct"])),
        tag_line("🧠", "الحالة", m["state"]),
        tag_line("✅", "القرار", m["decision"]),
        tag_line("🧩", "الاختراق", m["breakout_text"]),
        tag_line("🏭", "نشاط الشركة", m["industry"]),
        tag_line("🕒", "الجلسة", get_session_label()),
        tag_line("📍", "VWAP", m["vwap_side"]),
        tag_line("🐋", "دخول حوت", label_bool(m["whale_in"])),
        tag_line("💰", "السيولة الآن", fmt_num(m["current_liquidity"])),
        tag_line("🪙", "سيولة آخر دقيقة", fmt_num(m["last_1m_vol"])),
        tag_line("📊", "حجم التداول", fmt_num(m["day_volume"])),
        tag_line("📶", "RVOL", m["rvol"]),
        tag_line("🏢", "حجم الشركة", m["company_size"]),
        tag_line("🧮", "عدد الأسهم", fmt_num(m["shares_outstanding"]) if m["shares_outstanding"] > 0 else "غير متاح"),
        tag_line("📦", "الأسهم المطروحة", fmt_num(m["float_shares"]) if m["float_shares"] > 0 else "غير متاح"),
        tag_line("🏛️", "القيمة السوقية", fmt_num(m["market_cap"]) if m["market_cap"] > 0 else "غير متاح"),
        tag_line("⭐", "السكور", m["power_score"]),
    ]
    return "\n".join(lines)


def build_top_list_text(title: str, rows: list, rank_key: str) -> str:
    lines = [f"🏆 {title}", f"🕒 الجلسة: {get_session_label()}", ""]

    for i, row in enumerate(rows, start=1):
        whale = "🐋" if row["whale_in"] else "•"
        lines.append(
            f"{i}) {row['symbol']} | {row['price']} | {pct_str(row['day_change_pct'])} | "
            f"1m {pct_str(row['minute_change_pct'])} | RVOL {row['rvol']} | VWAP {row['vwap_side']} | {whale} | {row['industry']}"
        )

    lines.append("")
    lines.append(f"📌 الترتيب: {rank_key}")
    return "\n".join(lines)


# =========================
# CORE SCAN
# =========================
def scan_market():
    symbols = get_market_symbols()
    logging.info("Scanning %s symbols...", len(symbols))

    collected = []
    stats = {
        "total": 0,
        "quote_fail": 0,
        "price_filtered": 0,
        "candle_fail": 0,
        "volume_filtered": 0,
        "accepted": 0,
    }

    for symbol in symbols:
        stats["total"] += 1

        quote = get_quote(symbol)
        time.sleep(REQUEST_DELAY)

        if not quote:
            stats["quote_fail"] += 1
            continue

        price = safe_float(quote["price"], 0)
        day_change_pct = safe_float(quote["day_change_pct"], 0)

        if not (MIN_PRICE <= price <= MAX_PRICE):
            stats["price_filtered"] += 1
            continue

        candles = get_candles(symbol)
        time.sleep(REQUEST_DELAY)

        if not candles:
            stats["candle_fail"] += 1
            continue

        day_volume = int(candles["day_volume"])
        last_1m_vol = int(candles["last_1m_vol"])

        if day_volume < MIN_DAY_VOLUME or last_1m_vol < MIN_LAST_1M_VOL:
            stats["volume_filtered"] += 1
            continue

        minute_change_pct = safe_float(candles["minute_change_pct"], 0)
        momentum_3m_pct = safe_float(candles["momentum_3m_pct"], 0)
        rvol = safe_float(candles["rvol"], 0)
        above_vwap = bool(candles["above_vwap"])
        whale_in = bool(candles["whale_in"])

        signal_type = classify_signal(
            day_change_pct=day_change_pct,
            minute_change_pct=minute_change_pct,
            momentum_3m_pct=momentum_3m_pct,
            rvol=rvol,
            above_vwap=above_vwap,
        )

        state = detect_state(
            day_change_pct=day_change_pct,
            minute_change_pct=minute_change_pct,
            rvol=rvol,
            above_vwap=above_vwap,
            whale_in=whale_in,
        )

        decision = decide_action(
            signal_type=signal_type,
            state=state,
            above_vwap=above_vwap,
            minute_change_pct=minute_change_pct,
        )

        power_score = calc_power_score(
            day_change_pct=day_change_pct,
            minute_change_pct=minute_change_pct,
            momentum_3m_pct=momentum_3m_pct,
            rvol=rvol,
            above_vwap=above_vwap,
            whale_in=whale_in,
            day_volume=day_volume,
        )

        profile = get_profile(symbol)

        market_cap_value = safe_float(profile.get("market_cap_m", 0), 0) * 1_000_000
        shares_outstanding_value = safe_float(profile.get("shares_outstanding_m", 0), 0) * 1_000_000

        # مبدئيًا خلي الأسهم المطروحة = غير متاح
        # نقدر نضيفها لاحقًا بمصدر آخر أو متغير خارجي
        float_shares_value = 0

        row = {
            "symbol": symbol,
            "price": round(price, 4),
            "day_change_pct": round(day_change_pct, 2),
            "minute_change_pct": round(minute_change_pct, 2),
            "momentum_3m_pct": round(momentum_3m_pct, 2),
            "signal_type": signal_type or "تنبيه متابعة",
            "decision": decision,
            "state": state,
            "breakout_text": breakout_text(above_vwap, minute_change_pct),
            "industry": profile.get("industry", "غير محدد"),
            "vwap_side": candles["vwap_side"],
            "above_vwap": above_vwap,
            "whale_in": whale_in,
            "current_liquidity": candles["current_liquidity"],
            "daily_liquidity": candles["daily_liquidity"],
            "last_1m_vol": last_1m_vol,
            "day_volume": day_volume,
            "avg_vol20": candles["avg_vol20"],
            "rvol": round(rvol, 2),
            "market_cap": market_cap_value,
            "company_size": size_label(profile.get("market_cap_m", 0)),
            "shares_outstanding": shares_outstanding_value,
            "float_shares": float_shares_value,
            "power_score": power_score,
        }

        collected.append(row)
        stats["accepted"] += 1

    logging.info("Scan summary: %s", stats)
    return collected


def send_top_lists_if_changed(collected: list):
    global last_top_gainers_hash, last_top_power_hash

    if not collected:
        return

    top_gainers = sorted(
        collected,
        key=lambda x: (x["day_change_pct"], x["minute_change_pct"], x["rvol"]),
        reverse=True
    )[:TOP_GAINERS_COUNT]

    top_power = sorted(
        collected,
        key=lambda x: x["power_score"],
        reverse=True
    )[:TOP_POWER_COUNT]

    gainers_hash = make_rows_hash(top_gainers, ["symbol", "day_change_pct", "minute_change_pct", "rvol"])
    power_hash = make_rows_hash(top_power, ["symbol", "power_score", "day_change_pct", "minute_change_pct"])

    if gainers_hash != last_top_gainers_hash:
        if send_telegram_message(build_top_list_text("أفضل 5 الأكثر ارتفاعًا الآن", top_gainers, "التغير اليومي")):
            last_top_gainers_hash = gainers_hash
            logging.info("Top gainers list sent.")

    if power_hash != last_top_power_hash:
        if send_telegram_message(build_top_list_text("أفضل 5 الأقوى الآن", top_power, "السكور")):
            last_top_power_hash = power_hash
            logging.info("Top power list sent.")


def send_dynamic_stock_alerts(collected: list):
    if not collected:
        return

    alert_candidates = [
        x for x in collected
        if x["signal_type"] in {"تنبيه دخول سريع", "تنبيه ترند"} or x["whale_in"]
    ]

    alert_candidates = sorted(
        alert_candidates,
        key=lambda x: (x["power_score"], x["day_change_pct"], x["minute_change_pct"]),
        reverse=True
    )

    sent = 0
    for row in alert_candidates:
        symbol = row["symbol"]
        price = row["price"]

        if alert_counter.get(symbol, 0) >= ALERT_MAX_PER_SYMBOL:
            continue

        if not should_send(symbol, price):
            continue

        text = build_stock_alert_text(row)
        if send_telegram_message(text):
            mark_sent(symbol, price)
            sent += 1
            logging.info("Dynamic alert sent for %s", symbol)

    if sent == 0:
        logging.info("No dynamic alerts sent this round.")


def main():
    ok, msg = require_env()
    if not ok:
        logging.error(msg)
        return

    startup = (
        "✅ البوت اشتغل بنسخة v2 الديناميكية\n"
        "يفحص السوق الأمريكي ويستخرج الأسهم الصاعدة والترند تلقائيًا\n"
        "يرسل قائمة التوب فقط إذا تغيرت فعلاً\n"
        "ويرسل تنبيهات متتابعة للسهم إذا تغير السعر وتحسنت الإشارة\n"
        f"🕒 الجلسة الحالية: {get_session_label()}\n"
        f"📦 SCAN_SYMBOL_LIMIT: {SCAN_SYMBOL_LIMIT}"
    )
    send_telegram_message(startup)

    while True:
        try:
            collected = scan_market()
            send_top_lists_if_changed(collected)
            time.sleep(1)
            send_dynamic_stock_alerts(collected)
        except Exception as e:
            logging.exception("Main loop error: %s", e)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
