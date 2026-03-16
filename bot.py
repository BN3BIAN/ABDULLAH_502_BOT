import os
import time
import math
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import pandas as pd


# =========================
# Environment Variables
# =========================
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "120"))
TOP_ALERTS_PER_SCAN = int(os.getenv("TOP_ALERTS_PER_SCAN", "4"))

MARKET_SYMBOL_LIMIT = int(os.getenv("MARKET_SYMBOL_LIMIT", "60"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "0.8"))

MIN_PRICE = float(os.getenv("MIN_PRICE", "0.30"))
MAX_PRICE = float(os.getenv("MAX_PRICE", "30"))

# دخول سريع
FAST_MIN_DAY_CHANGE = float(os.getenv("FAST_MIN_DAY_CHANGE", "3.5"))
FAST_MIN_MINUTE_CHANGE = float(os.getenv("FAST_MIN_MINUTE_CHANGE", "0.25"))
FAST_MIN_RVOL = float(os.getenv("FAST_MIN_RVOL", "1.3"))

# ترند اليوم
TREND_MIN_DAY_CHANGE = float(os.getenv("TREND_MIN_DAY_CHANGE", "5.0"))
TREND_MIN_MINUTE_CHANGE = float(os.getenv("TREND_MIN_MINUTE_CHANGE", "0.05"))
TREND_MIN_RVOL = float(os.getenv("TREND_MIN_RVOL", "1.0"))

# فلتر إضافي
MIN_LAST_1M_VOL = int(os.getenv("MIN_LAST_1M_VOL", "5000"))
MIN_DAY_VOLUME = int(os.getenv("MIN_DAY_VOLUME", "100000"))

FINNHUB_URL = "https://finnhub.io/api/v1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

last_sent = {}


# =========================
# Helpers
# =========================
def normalize_symbols(raw: str):
    if not raw:
        return set()
    out = set()
    for x in raw.split(","):
        s = x.strip().upper()
        if s:
            out.add(s)
    return out


def parse_float_map(raw: str):
    result = {}
    if not raw:
        return result

    for item in raw.split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue

        sym, val = item.split(":", 1)
        sym = sym.strip().upper()

        try:
            result[sym] = float(val.strip())
        except Exception:
            continue

    return result


SHARIAH_COMPLIANT = normalize_symbols(os.getenv("SHARIAH_COMPLIANT", ""))
SHARIAH_NON_COMPLIANT = normalize_symbols(os.getenv("SHARIAH_NON_COMPLIANT", ""))
FLOAT_SHARES_MAP = parse_float_map(os.getenv("FLOAT_SHARES_MAP", ""))


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


def get_shariah_status(symbol: str) -> str:
    s = symbol.upper().strip()
    if s in SHARIAH_COMPLIANT:
        return "شرعي"
    if s in SHARIAH_NON_COMPLIANT:
        return "غير شرعي"
    return "غير محدد"


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


def send_telegram_message(text: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text
    }

    try:
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code == 200:
            return True

        logging.error("Telegram send failed: %s | %s", r.status_code, r.text)
        return False

    except Exception as e:
        logging.exception("Telegram exception: %s", e)
        return False


def should_send(symbol: str, price: float) -> bool:
    old = last_sent.get(symbol)
    if old is None:
        return True

    old_price = safe_float(old.get("price"), 0.0)
    if old_price <= 0:
        return True

    change_pct = ((price - old_price) / old_price) * 100
    return abs(change_pct) >= 0.5


def mark_sent(symbol: str, price: float):
    last_sent[symbol] = {"price": price}


# =========================
# Finnhub API
# =========================
def get_market_symbols():
    url = f"{FINNHUB_URL}/stock/symbol?exchange=US&token={FINNHUB_API_KEY}"

    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        data = r.json()

        symbols = []

        for item in data:
            symbol = str(item.get("symbol", "")).strip().upper()
            typ = str(item.get("type", "")).strip().upper()

            if not symbol:
                continue
            if "." in symbol or "^" in symbol:
                continue

            # نركز فقط على الأسهم العادية و ADR
            if typ and typ not in {"COMMON STOCK", "ADR"}:
                continue

            symbols.append(symbol)

            if len(symbols) >= MARKET_SYMBOL_LIMIT:
                break

        return symbols

    except Exception as e:
        logging.exception("get_market_symbols failed: %s", e)
        return []


def get_quote(symbol: str):
    url = f"{FINNHUB_URL}/quote?symbol={symbol}&token={FINNHUB_API_KEY}"

    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        data = r.json()

        price = safe_float(data.get("c"), 0)
        prev_close = safe_float(data.get("pc"), 0)

        if price <= 0 or prev_close <= 0:
            return None

        day_change_pct = ((price - prev_close) / prev_close) * 100

        return {
            "price": round(price, 4),
            "day_change_pct": round(day_change_pct, 2),
        }

    except Exception as e:
        logging.exception("get_quote failed for %s: %s", symbol, e)
        return None


def get_profile(symbol: str):
    url = f"{FINNHUB_URL}/stock/profile2?symbol={symbol}&token={FINNHUB_API_KEY}"

    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        data = r.json()

        market_cap = safe_float(data.get("marketCapitalization"), 0)
        shares_outstanding = safe_float(data.get("shareOutstanding"), 0)
        float_shares = FLOAT_SHARES_MAP.get(symbol.upper(), 0)

        return {
            "name": str(data.get("name", "")).strip(),
            "market_cap_m": market_cap,
            "shares_outstanding_m": shares_outstanding,
            "float_shares": float_shares,
        }

    except Exception as e:
        logging.exception("get_profile failed for %s: %s", symbol, e)
        return {
            "name": "",
            "market_cap_m": 0,
            "shares_outstanding_m": 0,
            "float_shares": 0,
        }


def get_candles(symbol: str):
    now_ts = int(time.time())
    start_ts = now_ts - (60 * 90)

    url = (
        f"{FINNHUB_URL}/stock/candle"
        f"?symbol={symbol}"
        f"&resolution=1"
        f"&from={start_ts}"
        f"&to={now_ts}"
        f"&token={FINNHUB_API_KEY}"
    )

    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        data = r.json()

        if data.get("s") != "ok":
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

        typical = (df["high"] + df["low"] + df["close"]) / 3.0
        tpv = typical * df["volume"]
        vol_cum = df["volume"].cumsum()

        vwap = 0.0
        if not vol_cum.empty and safe_float(vol_cum.iloc[-1], 0) > 0:
            vwap = safe_float((tpv.cumsum() / vol_cum).iloc[-1], 0)

        day_volume = safe_float(df["volume"].sum(), 0)
        above_vwap = price > vwap if vwap > 0 else False

        return {
            "minute_change_pct": round(minute_change_pct, 2),
            "last_1m_vol": int(last_1m_vol),
            "avg_vol20": round(avg_vol20, 2),
            "rvol": round(rvol, 2),
            "vwap": round(vwap, 4),
            "day_volume": int(day_volume),
            "above_vwap": above_vwap,
        }

    except Exception as e:
        logging.exception("get_candles failed for %s: %s", symbol, e)
        return None


# =========================
# Signal Logic
# =========================
def classify_signal(day_change_pct, minute_change_pct, rvol, above_vwap):
    if (
        day_change_pct >= FAST_MIN_DAY_CHANGE
        and minute_change_pct >= FAST_MIN_MINUTE_CHANGE
        and rvol >= FAST_MIN_RVOL
        and above_vwap
    ):
        return "دخول سريع"

    if (
        day_change_pct >= TREND_MIN_DAY_CHANGE
        and minute_change_pct >= TREND_MIN_MINUTE_CHANGE
        and rvol >= TREND_MIN_RVOL
        and above_vwap
    ):
        return "ترند اليوم"

    return None


def detect_state(day_change_pct, minute_change_pct, rvol, above_vwap):
    if day_change_pct >= 5 and minute_change_pct < -0.20 and rvol >= 1.2 and not above_vwap:
        return "تصريف"

    if day_change_pct >= 5 and minute_change_pct < 0 and above_vwap:
        return "تصحيح"

    return "طبيعي"


def decide_action(signal_type, state):
    if state == "تصريف":
        return "انتظار"
    if signal_type in {"دخول سريع", "ترند اليوم"} and state != "تصريف":
        return "دخول"
    return "انتظار"


def calc_score(signal_type, day_change_pct, minute_change_pct, rvol, above_vwap):
    score = (
        max(0, day_change_pct) * 3
        + max(0, minute_change_pct) * 20
        + max(0, rvol) * 12
        + (10 if above_vwap else 0)
    )

    if signal_type == "دخول سريع":
        score += 18
    elif signal_type == "ترند اليوم":
        score += 10

    return round(score, 2)


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


def build_alert_text(m: dict) -> str:
    title = "تنبيه سهم"
    if m["signal_type"] == "دخول سريع":
        title = "تنبيه دخول سريع"
    elif m["signal_type"] == "ترند اليوم":
        title = "تنبيه ترند اليوم"

    shares_outstanding_text = "غير متاح"
    if m["shares_outstanding"] > 0:
        shares_outstanding_text = fmt_num(m["shares_outstanding"])

    float_shares_text = "غير متاح"
    if m["float_shares"] > 0:
        float_shares_text = fmt_num(m["float_shares"])

    market_cap_text = "غير متاح"
    if m["market_cap"] > 0:
        market_cap_text = fmt_num(m["market_cap"])

    lines = [
        f"📢 {title}",
        "",
        f"السهم: {m['symbol']}",
        f"النوع: {m['signal_type']}",
        f"القرار: {m['decision']}",
        f"الحالة: {m['state']}",
        f"الشرعية: {m['shariah']}",
        f"الجلسة: {get_session_label()}",
        f"السعر: {m['price']}",
        f"التغير اليومي: {pct_str(m['day_change_pct'])}",
        f"تغير آخر دقيقة: {pct_str(m['minute_change_pct'])}",
        f"حجم التداول: {fmt_num(m['day_volume'])}",
        f"فوليوم آخر دقيقة: {fmt_num(m['last_1m_vol'])}",
        f"متوسط 20 دقيقة: {fmt_num(m['avg_vol20'])}",
        f"RVOL: {m['rvol']}",
        f"VWAP: {m['vwap']}",
        f"فوق VWAP: {'نعم' if m['above_vwap'] else 'لا'}",
        f"القيمة السوقية: {market_cap_text}",
        f"حجم الشركة: {m['company_size']}",
        f"عدد أسهم الشركة: {shares_outstanding_text}",
        f"الأسهم المطروحة: {float_shares_text}",
        f"السكور: {m['score']}",
    ]

    return "\n".join(lines)


# =========================
# Main Scan
# =========================
def scan_once():
    symbols = get_market_symbols()
    logging.info("Scanning %s symbols...", len(symbols))

    ranked = []

    for symbol in symbols:
        quote = get_quote(symbol)
        time.sleep(REQUEST_DELAY)

        if not quote:
            continue

        price = safe_float(quote["price"], 0)
        day_change_pct = safe_float(quote["day_change_pct"], 0)

        if not (MIN_PRICE <= price <= MAX_PRICE):
            continue

        candles = get_candles(symbol)
        time.sleep(REQUEST_DELAY)

        if not candles:
            continue

        minute_change_pct = safe_float(candles["minute_change_pct"], 0)
        rvol = safe_float(candles["rvol"], 0)
        above_vwap = bool(candles["above_vwap"])
        last_1m_vol = int(candles["last_1m_vol"])
        day_volume = int(candles["day_volume"])

        if last_1m_vol < MIN_LAST_1M_VOL:
            continue
        if day_volume < MIN_DAY_VOLUME:
            continue

        # يمنع الأسهم الرخيصة الضعيفة إلا لو الزخم قوي جدًا
        if price < 1 and day_change_pct < 8:
            continue

        signal_type = classify_signal(day_change_pct, minute_change_pct, rvol, above_vwap)
        if not signal_type:
            continue

        profile = get_profile(symbol)
        time.sleep(REQUEST_DELAY)

        state = detect_state(
            day_change_pct=day_change_pct,
            minute_change_pct=minute_change_pct,
            rvol=rvol,
            above_vwap=above_vwap,
        )
        decision = decide_action(signal_type, state)

        score = calc_score(
            signal_type=signal_type,
            day_change_pct=day_change_pct,
            minute_change_pct=minute_change_pct,
            rvol=rvol,
            above_vwap=above_vwap,
        )

        market_cap_value = safe_float(profile.get("market_cap_m", 0), 0) * 1_000_000
        shares_outstanding_value = safe_float(profile.get("shares_outstanding_m", 0), 0) * 1_000_000
        float_shares_value = safe_float(profile.get("float_shares", 0), 0)

        ranked.append({
            "symbol": symbol,
            "signal_type": signal_type,
            "decision": decision,
            "state": state,
            "shariah": get_shariah_status(symbol),
            "price": round(price, 4),
            "day_change_pct": round(day_change_pct, 2),
            "minute_change_pct": round(minute_change_pct, 2),
            "day_volume": day_volume,
            "last_1m_vol": last_1m_vol,
            "avg_vol20": candles["avg_vol20"],
            "rvol": round(rvol, 2),
            "vwap": candles["vwap"],
            "above_vwap": above_vwap,
            "market_cap": market_cap_value,
            "company_size": size_label(profile.get("market_cap_m", 0)),
            "shares_outstanding": shares_outstanding_value,
            "float_shares": float_shares_value,
            "score": score,
        })

        logging.info(
            "%s | type=%s | decision=%s | state=%s | score=%s",
            symbol, signal_type, decision, state, score
        )

    ranked.sort(key=lambda x: x["score"], reverse=True)

    if not ranked:
        logging.info("No signals found.")
        return

    sent_count = 0

    for m in ranked[:TOP_ALERTS_PER_SCAN]:
        if not should_send(m["symbol"], m["price"]):
            continue

        text = build_alert_text(m)

        if send_telegram_message(text):
            mark_sent(m["symbol"], m["price"])
            sent_count += 1
            logging.info("Alert sent for %s", m["symbol"])

    if sent_count == 0:
        logging.info("No alerts sent this round.")


def main():
    ok, msg = require_env()
    if not ok:
        logging.error(msg)
        return

    startup = (
        "✅ البوت اشتغل بنسخة احترافية على Finnhub فقط\n"
        "لا يستخدم yfinance\n"
        "يرسل:\n"
        "1) دخول سريع\n"
        "2) ترند اليوم\n"
        "ويعرض القرار والحالة والشرعية وحجم الشركة"
    )
    send_telegram_message(startup)

    while True:
        try:
            scan_once()
        except Exception as e:
            logging.exception("Main loop error: %s", e)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
