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

CHECK_INTERVAL = 180
REQUEST_DELAY = 0.80
BATCH_SIZE = 20

MIN_PRICE = 0.30
MAX_PRICE = 50.0
MIN_CHANGE_PCT = 1.20
MIN_OPEN_CHANGE_PCT = 0.80
RESEND_MOVE_PCT = 0.50
MAX_ALERTS_PER_SYMBOL = 20

FINNHUB_URL = "https://finnhub.io/api/v1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

session = requests.Session()
symbols_cache = []
symbols_cache_ts = 0.0
profile_cache = {}
news_cache = {}
last_sent = {}
alert_counter = {}
scan_offset = 0


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
        logging.error("Missing required variables: %s", ", ".join(missing))
        return False
    return True


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
    try:
        r = session.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=20
        )
        return r.status_code == 200
    except Exception as e:
        logging.exception("Telegram send failed: %s", e)
        return False


def should_send(symbol: str, price: float) -> bool:
    old = last_sent.get(symbol)
    if old is None:
        return True

    old_price = safe_float(old.get("price"), 0)
    if old_price <= 0:
        return True

    move_pct = ((price - old_price) / old_price) * 100
    return abs(move_pct) >= RESEND_MOVE_PCT


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


def get_market_symbols():
    global symbols_cache, symbols_cache_ts

    now_ts = time.time()
    if symbols_cache and (now_ts - symbols_cache_ts) < 43200:
        return symbols_cache

    data = api_get("/stock/symbol", {"exchange": "US"}, timeout=40)
    if not data:
        return symbols_cache if symbols_cache else []

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
    return symbols


def get_next_batch(symbols):
    global scan_offset

    if not symbols:
        return []

    start = scan_offset
    end = start + BATCH_SIZE
    batch = symbols[start:end]

    if len(batch) < BATCH_SIZE:
        batch += symbols[: max(0, BATCH_SIZE - len(batch))]

    scan_offset = (scan_offset + BATCH_SIZE) % len(symbols)
    return batch


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

    near_day_high = False
    if high_price > 0:
        near_day_high = price >= (high_price * 0.992)

    return {
        "price": round(price, 4),
        "day_change_pct": round(day_change_pct, 2),
        "open_change_pct": round(open_change_pct, 2),
        "open": round(open_price, 4) if open_price > 0 else 0,
        "high": round(high_price, 4) if high_price > 0 else 0,
        "low": round(low_price, 4) if low_price > 0 else 0,
        "near_day_high": near_day_high,
    }


def get_profile(symbol: str):
    cached = profile_cache.get(symbol)
    if cached:
        return cached

    data = api_get("/stock/profile2", {"symbol": symbol}, timeout=15)
    if not data:
        profile = {
            "industry": "غير محدد",
            "market_cap_m": 0,
            "shares_outstanding_m": 0,
        }
        profile_cache[symbol] = profile
        return profile

    profile = {
        "industry": str(data.get("finnhubIndustry", "")).strip() or "غير محدد",
        "market_cap_m": safe_float(data.get("marketCapitalization"), 0),
        "shares_outstanding_m": safe_float(data.get("shareOutstanding"), 0),
    }
    profile_cache[symbol] = profile
    return profile


def get_latest_news_status(symbol: str):
    cached = news_cache.get(symbol)
    now_ts = time.time()

    if cached and (now_ts - cached["ts"]) < 3600:
        return cached["value"]

    try:
        today = datetime.utcnow().date()
        from_date = (today - timedelta(days=3)).isoformat()
        to_date = today.isoformat()

        data = api_get(
            "/company-news",
            {"symbol": symbol, "from": from_date, "to": to_date},
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


def classify_alert(q):
    is_rise = q["day_change_pct"] >= MIN_CHANGE_PCT
    is_activity = q["open_change_pct"] >= MIN_OPEN_CHANGE_PCT or q["near_day_high"]

    if is_rise and is_activity:
        return "تنبيه ارتفاع + نشاط"
    if is_activity:
        return "تنبيه نشاط"
    if is_rise:
        return "تنبيه ارتفاع"
    return None


def detect_state(q):
    if q["near_day_high"] and q["day_change_pct"] >= MIN_CHANGE_PCT:
        return "قريب من قمة اليوم"
    if q["day_change_pct"] >= MIN_CHANGE_PCT and q["open_change_pct"] >= 0:
        return "صعود"
    if q["day_change_pct"] >= MIN_CHANGE_PCT and q["open_change_pct"] < 0:
        return "احتمال وهمي"
    return "مراقبة"


def build_alert_text(row):
    alert_no = get_alert_number(row["symbol"])

    lines = [
        f"📢 {row['alert_type']} | رقم {alert_no}",
        "",
        tag_line("🏷️", "السهم", row["symbol"]),
        tag_line("💵", "السعر", row["price"]),
        tag_line("📈", "نسبة الارتفاع", pct_str(row["day_change_pct"])),
        tag_line("⚡", "التغير من الافتتاح", pct_str(row["open_change_pct"])),
        tag_line("🧠", "الحالة", row["state"]),
        tag_line("🎯", "قريب من أعلى اليوم", "نعم" if row["near_day_high"] else "لا"),
        tag_line("🧮", "عدد أسهم الشركة", fmt_num(row["shares_outstanding"]) if row["shares_outstanding"] > 0 else "غير متاح"),
        tag_line("📦", "الأسهم المتاحة", "غير متاح"),
        tag_line("🏭", "نشاط الشركة", row["industry"]),
        tag_line("🏢", "حجم الشركة", row["company_size"]),
        tag_line("🏛️", "القيمة السوقية", fmt_num(row["market_cap"]) if row["market_cap"] > 0 else "غير متاح"),
        tag_line("📰", "الخبر", row["news"]),
        tag_line("🕒", "الجلسة", get_session_label()),
    ]
    return "\n".join(lines)


def scan_market():
    symbols = get_market_symbols()
    if not symbols:
        logging.warning("لم يتم جلب أي رموز.")
        return []

    batch = get_next_batch(symbols)
    logging.info("🔍 بدأ الفحص...")
    logging.info("عدد الأسهم المفحوصة هذه الدورة: %s", len(batch))

    rows = []
    stats = {
        "total": 0,
        "quote_fail": 0,
        "price_filtered": 0,
        "no_alert": 0,
        "accepted": 0,
    }

    for symbol in batch:
        stats["total"] += 1

        q = get_quote(symbol)
        time.sleep(REQUEST_DELAY)

        if not q:
            stats["quote_fail"] += 1
            continue

        price = safe_float(q["price"], 0)
        if not (MIN_PRICE <= price <= MAX_PRICE):
            stats["price_filtered"] += 1
            continue

        alert_type = classify_alert(q)
        if not alert_type:
            stats["no_alert"] += 1
            continue

        profile = get_profile(symbol)
        time.sleep(0.20)

        shares_outstanding = safe_float(profile.get("shares_outstanding_m", 0), 0) * 1_000_000
        market_cap = safe_float(profile.get("market_cap_m", 0), 0) * 1_000_000

        row = {
            "symbol": symbol,
            "price": q["price"],
            "day_change_pct": q["day_change_pct"],
            "open_change_pct": q["open_change_pct"],
            "near_day_high": q["near_day_high"],
            "alert_type": alert_type,
            "state": detect_state(q),
            "industry": profile.get("industry", "غير محدد"),
            "shares_outstanding": shares_outstanding,
            "market_cap": market_cap,
            "company_size": size_label(profile.get("market_cap_m", 0)),
            "news": "",
        }

        rows.append(row)
        stats["accepted"] += 1

    logging.info("Scan summary: %s", stats)
    return rows


def send_alerts(rows):
    if not rows:
        logging.info("لا توجد إشارات هذه الدورة.")
        return

    rows = sorted(
        rows,
        key=lambda x: (
            x["day_change_pct"],
            x["open_change_pct"],
            1 if x["near_day_high"] else 0
        ),
        reverse=True
    )

    sent = 0

    for row in rows:
        symbol = row["symbol"]
        price = row["price"]

        if alert_counter.get(symbol, 0) >= MAX_ALERTS_PER_SYMBOL:
            continue

        if not should_send(symbol, price):
            continue

        row["news"] = get_latest_news_status(symbol)
        time.sleep(0.20)

        if send_telegram_message(build_alert_text(row)):
            mark_sent(symbol, price)
            sent += 1
            logging.info("Alert sent for %s | %s", symbol, row["alert_type"])

    if sent == 0:
        logging.info("لم يتم إرسال أي تنبيه هذه الدورة.")


def main():
    if not require_env():
        return

    startup = (
        "✅ اشتغل البوت بنسخة Finnhub الخفيفة الحقيقية\n"
        "بدون candle وبدون قائمة ثابتة\n"
        f"تنبيه الارتفاع من: {MIN_CHANGE_PCT}%\n"
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
