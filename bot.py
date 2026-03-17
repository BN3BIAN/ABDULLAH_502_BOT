import os
import time
import math
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests


FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "150"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "0.60"))

SYMBOL_LIMIT = int(os.getenv("SYMBOL_LIMIT", "40"))
QUOTE_CANDIDATES_LIMIT = int(os.getenv("QUOTE_CANDIDATES_LIMIT", "8"))

MIN_PRICE = float(os.getenv("MIN_PRICE", "0.30"))
MAX_PRICE = float(os.getenv("MAX_PRICE", "50.00"))

MIN_CHANGE = float(os.getenv("MIN_CHANGE", "1.20"))

MIN_MINUTE_CHANGE = float(os.getenv("MIN_MINUTE_CHANGE", "0.35"))
MIN_LAST_1M_VOL = int(os.getenv("MIN_LAST_1M_VOL", "1200"))
MIN_DAY_VOLUME = int(os.getenv("MIN_DAY_VOLUME", "15000"))

RESEND_MOVE_PCT = float(os.getenv("RESEND_MOVE_PCT", "0.40"))
ALERT_MAX_PER_SYMBOL = int(os.getenv("ALERT_MAX_PER_SYMBOL", "20"))

FINNHUB_URL = "https://finnhub.io/api/v1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

session = requests.Session()
last_sent = {}
alert_counter = {}
profile_cache = {}
news_cache = {}
symbols_cache = []
symbols_cache_ts = 0.0


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


def tag_line(icon, label, value):
    return f"{icon} {label}: {value}"


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

        if r.status_code == 429:
            logging.warning("429 too many requests on %s", path)
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


def should_send(symbol: str, price: float) -> bool:
    old = last_sent.get(symbol)
    if old is None:
        return True

    old_price = safe_float(old.get("price"), 0.0)
    if old_price <= 0:
        return True

    move_pct = ((price - old_price) / old_price) * 100
    return abs(move_pct) >= RESEND_MOVE_PCT


def mark_sent(symbol: str, price: float):
    last_sent[symbol] = {"price": price}
    alert_counter[symbol] = alert_counter.get(symbol, 0) + 1


def get_alert_number(symbol: str) -> int:
    return alert_counter.get(symbol, 0) + 1


def get_market_symbols():
    global symbols_cache, symbols_cache_ts

    now_ts = time.time()
    if symbols_cache and (now_ts - symbols_cache_ts) < 43200:
        return symbols_cache[:SYMBOL_LIMIT]

    data = api_get("/stock/symbol", {"exchange": "US"}, timeout=40)

    if not data:
        return symbols_cache[:SYMBOL_LIMIT] if symbols_cache else []

    symbols = []

    for item in data:
        try:
            if isinstance(item, dict):
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

            elif isinstance(item, str):
                symbol = item.strip().upper()
                if symbol.isalpha() and len(symbol) <= 5:
                    symbols.append(symbol)

        except Exception:
            continue

    symbols = sorted(set(symbols))
    symbols_cache = symbols
    symbols_cache_ts = now_ts
    return symbols[:SYMBOL_LIMIT]


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

    open_change_pct = 0.0
    if open_price > 0:
        open_change_pct = ((price - open_price) / open_price) * 100

    return {
        "price": round(price, 4),
        "day_change_pct": round(day_change_pct, 2),
        "open_change_pct": round(open_change_pct, 2),
        "open": round(open_price, 4) if open_price > 0 else 0,
        "high": round(high_price, 4) if high_price > 0 else 0,
        "low": round(low_price, 4) if low_price > 0 else 0,
    }


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

    closes = data.get("c", [])
    highs = data.get("h", [])
    volumes = data.get("v", [])

    if len(closes) < 25 or len(highs) < 25 or len(volumes) < 25:
        return None

    price = safe_float(closes[-1], 0)
    prev_close = safe_float(closes[-2], 0)
    last_1m_vol = int(safe_float(volumes[-1], 0))
    day_volume = int(sum(safe_float(v, 0) for v in volumes))

    if price <= 0 or prev_close <= 0:
        return None

    minute_change_pct = ((price - prev_close) / prev_close) * 100

    last3_first = safe_float(closes[-3], 0)
    momentum_3m_pct = 0.0
    if last3_first > 0:
        momentum_3m_pct = ((price - last3_first) / last3_first) * 100

    prev_20_high = max(safe_float(x, 0) for x in highs[-21:-1]) if len(highs) >= 21 else 0
    current_liquidity = price * last_1m_vol
    total_liquidity = price * day_volume

    return {
        "minute_change_pct": round(minute_change_pct, 2),
        "momentum_3m_pct": round(momentum_3m_pct, 2),
        "last_1m_vol": last_1m_vol,
        "day_volume": day_volume,
        "current_liquidity": current_liquidity,
        "total_liquidity": total_liquidity,
        "prev_20_high": round(prev_20_high, 4),
    }


def get_profile(symbol: str):
    cached = profile_cache.get(symbol)
    if cached:
        return cached

    data = api_get("/stock/profile2", {"symbol": symbol}, timeout=15)
    if not data:
        profile = {
            "industry": "غير محدد",
            "shares_outstanding_m": 0,
        }
        profile_cache[symbol] = profile
        return profile

    profile = {
        "industry": str(data.get("finnhubIndustry", "")).strip() or "غير محدد",
        "shares_outstanding_m": safe_float(data.get("shareOutstanding"), 0),
    }
    profile_cache[symbol] = profile
    return profile


def get_latest_news_status(symbol: str):
    cached = news_cache.get(symbol)
    now_ts = time.time()

    if cached and (now_ts - cached["ts"]) < 1800:
        return cached["value"]

    try:
        today = datetime.utcnow().date()
        from_date = (today - timedelta(days=3)).isoformat()
        to_date = today.isoformat()

        data = api_get(
            "/company-news",
            {
                "symbol": symbol,
                "from": from_date,
                "to": to_date,
            },
            timeout=15
        )

        if isinstance(data, list) and len(data) > 0:
            headline = str(data[0].get("headline", "")).strip()
            value = f"يوجد خبر: {headline[:90]}" if headline else "يوجد خبر"
        else:
            value = "لا يوجد خبر"

        news_cache[symbol] = {"value": value, "ts": now_ts}
        return value

    except Exception:
        return "لا يوجد خبر"


def choose_quote_candidates(symbols):
    logging.info("🔍 بدأ الفحص...")
    logging.info("عدد الأسهم المفحوصة في مرحلة quote: %s", len(symbols))

    candidates = []
    stats = {
        "total": 0,
        "quote_fail": 0,
        "price_filtered": 0,
        "weak_filtered": 0,
        "accepted": 0,
    }

    for symbol in symbols:
        stats["total"] += 1

        quote = get_quote(symbol)
        time.sleep(REQUEST_DELAY + 0.2)

        if not quote:
            stats["quote_fail"] += 1
            continue

        price = safe_float(quote["price"], 0)
        day_change_pct = safe_float(quote["day_change_pct"], 0)
        open_change_pct = safe_float(quote["open_change_pct"], 0)

        if not (MIN_PRICE <= price <= MAX_PRICE):
            stats["price_filtered"] += 1
            continue

        if day_change_pct < MIN_CHANGE and open_change_pct < 0.80:
            stats["weak_filtered"] += 1
            continue

        quote_score = (
            max(0, day_change_pct) * 4
            + max(0, open_change_pct) * 2
        )

        candidates.append({
            "symbol": symbol,
            "quote": quote,
            "quote_score": round(quote_score, 2),
        })
        stats["accepted"] += 1

    candidates.sort(key=lambda x: x["quote_score"], reverse=True)
    logging.info("Quote summary: %s", stats)
    return candidates[:QUOTE_CANDIDATES_LIMIT]


def classify_alert(day_change_pct, minute_change_pct, momentum_3m_pct, last_1m_vol):
    is_rise = day_change_pct >= MIN_CHANGE
    is_momentum = (
        minute_change_pct >= MIN_MINUTE_CHANGE
        and momentum_3m_pct >= 0.40
        and last_1m_vol >= MIN_LAST_1M_VOL
    )

    if is_rise and is_momentum:
        return "تنبيه زخم + ارتفاع"
    if is_momentum:
        return "تنبيه زخم"
    if is_rise:
        return "تنبيه ارتفاع"
    return None


def detect_state(price, prev_20_high, day_change_pct, minute_change_pct):
    if prev_20_high > 0 and price > prev_20_high:
        return "اختراق قمة سابقة"

    if day_change_pct >= MIN_CHANGE and minute_change_pct >= 0:
        return "صعود"

    if day_change_pct >= MIN_CHANGE and minute_change_pct < 0:
        return "احتمال وهمي"

    return "مراقبة"


def build_alert_text(m: dict) -> str:
    alert_no = get_alert_number(m["symbol"])

    lines = [
        f"📢 {m['alert_type']} | رقم {alert_no}",
        "",
        tag_line("🏷️", "السهم", m["symbol"]),
        tag_line("💵", "السعر", m["price"]),
        tag_line("📈", "نسبة الارتفاع", pct_str(m["day_change_pct"])),
        tag_line("⚡", "زخم آخر دقيقة", pct_str(m["minute_change_pct"])),
        tag_line("🚀", "زخم 3 دقائق", pct_str(m["momentum_3m_pct"])),
        tag_line("🧠", "الحالة", m["state"]),
        tag_line("📊", "حجم التداول", fmt_num(m["day_volume"])),
        tag_line("🪙", "سيولة آخر دقيقة", fmt_num(m["current_liquidity"])),
        tag_line("💰", "إجمالي السيولة", fmt_num(m["total_liquidity"])),
        tag_line("🧮", "عدد أسهم الشركة", fmt_num(m["shares_outstanding"]) if m["shares_outstanding"] > 0 else "غير متاح"),
        tag_line("📦", "الأسهم المتاحة", m["float_text"]),
        tag_line("🏭", "نشاط الشركة", m["industry"]),
        tag_line("📰", "الخبر", m["news"]),
        tag_line("🕒", "الجلسة", get_session_label()),
    ]
    return "\n".join(lines)


def scan_market():
    symbols = get_market_symbols()
    if not symbols:
        logging.warning("لم يتم جلب أي رموز.")
        return []

    quote_candidates = choose_quote_candidates(symbols)
    logging.info("Quote candidates selected: %s", len(quote_candidates))

    results = []
    stats = {
        "candle_fail": 0,
        "volume_filtered": 0,
        "no_alert": 0,
        "accepted": 0,
    }

    for item in quote_candidates:
        symbol = item["symbol"]
        quote = item["quote"]

        candles = get_candles(symbol)
        time.sleep(REQUEST_DELAY)

        if not candles:
            stats["candle_fail"] += 1
            continue

        day_volume = int(candles["day_volume"])
        last_1m_vol = int(candles["last_1m_vol"])

        if day_volume < MIN_DAY_VOLUME:
            stats["volume_filtered"] += 1
            continue

        price = safe_float(quote["price"], 0)
        day_change_pct = safe_float(quote["day_change_pct"], 0)
        minute_change_pct = safe_float(candles["minute_change_pct"], 0)
        momentum_3m_pct = safe_float(candles["momentum_3m_pct"], 0)

        alert_type = classify_alert(
            day_change_pct=day_change_pct,
            minute_change_pct=minute_change_pct,
            momentum_3m_pct=momentum_3m_pct,
            last_1m_vol=last_1m_vol,
        )

        if not alert_type:
            stats["no_alert"] += 1
            continue

        profile = get_profile(symbol)
        time.sleep(0.15)

        shares_outstanding = safe_float(profile.get("shares_outstanding_m", 0), 0) * 1_000_000
        industry = profile.get("industry", "غير محدد")

        state = detect_state(
            price=price,
            prev_20_high=safe_float(candles["prev_20_high"], 0),
            day_change_pct=day_change_pct,
            minute_change_pct=minute_change_pct,
        )

        results.append({
            "symbol": symbol,
            "price": round(price, 4),
            "day_change_pct": round(day_change_pct, 2),
            "minute_change_pct": round(minute_change_pct, 2),
            "momentum_3m_pct": round(momentum_3m_pct, 2),
            "alert_type": alert_type,
            "state": state,
            "day_volume": day_volume,
            "current_liquidity": candles["current_liquidity"],
            "total_liquidity": candles["total_liquidity"],
            "shares_outstanding": shares_outstanding,
            "float_text": "غير متاح",
            "industry": industry,
            "news": "",
        })
        stats["accepted"] += 1

    logging.info("Candle summary: %s", stats)
    return results


def send_alerts(rows):
    if not rows:
        logging.info("لا توجد إشارات هذه الدورة.")
        return

    rows = sorted(
        rows,
        key=lambda x: (
            x["day_change_pct"],
            x["minute_change_pct"],
            x["day_volume"]
        ),
        reverse=True
    )

    sent = 0

    for row in rows:
        symbol = row["symbol"]
        price = row["price"]

        if alert_counter.get(symbol, 0) >= ALERT_MAX_PER_SYMBOL:
            continue

        if not should_send(symbol, price):
            continue

        row["news"] = get_latest_news_status(symbol)
        time.sleep(0.15)

        text = build_alert_text(row)

        if send_telegram_message(text):
            mark_sent(symbol, price)
            sent += 1
            logging.info("Alert sent for %s | %s", symbol, row["alert_type"])

    if sent == 0:
        logging.info("لم يتم إرسال أي تنبيه هذه الدورة.")


def main():
    ok, msg = require_env()
    if not ok:
        logging.error(msg)
        return

    startup = (
        "✅ اشتغل البوت بنسخة Finnhub المخففة\n"
        "تم تقليل الفحص لتخفيف 429\n"
        f"تنبيه الارتفاع من: {MIN_CHANGE}%\n"
        f"تنبيه الزخم من: {MIN_MINUTE_CHANGE}% آخر دقيقة\n"
        f"الجلسة الحالية: {get_session_label()}"
    )
    send_telegram_message(startup)

    while True:
        try:
            rows = scan_market()
            send_alerts(rows)
        except Exception as e:
            logging.exception("Main loop error: %s", e)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
