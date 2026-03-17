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
REQUEST_DELAY = 0.90
BATCH_SIZE = 35

MIN_PRICE = 0.10
MAX_PRICE = 20.00

MIN_CHANGE_PCT = 8.0
MIN_OPEN_CHANGE_PCT = 3.0

MIN_MARKET_CAP_M = 20
MAX_MARKET_CAP_M = 3000

TOP_RESULTS_LIMIT = 2
COOLDOWN_SECONDS = 900

FINNHUB_URL = "https://finnhub.io/api/v1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

session = requests.Session()

symbols_cache = []
symbols_cache_ts = 0
scan_offset = 0
last_sent_time = {}
profile_cache = {}
news_cache = {}


def safe_float(x, d=0.0):
    try:
        if x is None:
            return d
        v = float(x)
        if math.isnan(v):
            return d
        return v
    except Exception:
        return d


def pct_str(x):
    try:
        return f"{float(x):+.2f}%"
    except Exception:
        return "0%"


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


def get_session():
    try:
        now = datetime.now(ZoneInfo("America/New_York"))
        h = now.hour * 60 + now.minute
        if 4 * 60 <= h < 9 * 60 + 30:
            return "قبل الافتتاح"
        if 9 * 60 + 30 <= h < 16 * 60:
            return "وقت السوق"
        if 16 * 60 <= h < 20 * 60:
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
        logging.error("Missing required variables: %s", ", ".join(missing))
        return False
    return True


def api(path, params=None):
    params = params or {}
    params["token"] = FINNHUB_API_KEY
    try:
        r = session.get(f"{FINNHUB_URL}{path}", params=params, timeout=20)
        if r.status_code != 200:
            logging.warning("HTTP %s on %s", r.status_code, path)
            return None
        return r.json()
    except Exception as e:
        logging.exception("API error on %s: %s", path, e)
        return None


def send(msg):
    try:
        session.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
            timeout=20
        )
    except Exception as e:
        logging.exception("Telegram error: %s", e)


def get_symbols():
    global symbols_cache, symbols_cache_ts

    if symbols_cache and time.time() - symbols_cache_ts < 36000:
        return symbols_cache

    data = api("/stock/symbol", {"exchange": "US"})
    if not data:
        return symbols_cache if symbols_cache else []

    out = []
    for item in data:
        try:
            if isinstance(item, dict):
                sym = str(item.get("symbol", "")).upper().strip()
                typ = str(item.get("type", "")).upper().strip()
                if not sym:
                    continue
                if "." in sym or "^" in sym:
                    continue
                if not sym.isalpha():
                    continue
                if len(sym) > 5:
                    continue
                if typ and typ not in {"COMMON STOCK", "ADR"}:
                    continue
                out.append(sym)
            elif isinstance(item, str):
                sym = item.upper().strip()
                if sym.isalpha() and len(sym) <= 5:
                    out.append(sym)
        except Exception:
            continue

    symbols_cache = sorted(set(out))
    symbols_cache_ts = time.time()
    return symbols_cache


def get_batch(symbols):
    global scan_offset
    if not symbols:
        return []

    batch = symbols[scan_offset: scan_offset + BATCH_SIZE]
    if len(batch) < BATCH_SIZE:
        batch += symbols[:max(0, BATCH_SIZE - len(batch))]
    scan_offset = (scan_offset + BATCH_SIZE) % len(symbols)
    return batch


def bad_company(name):
    n = (name or "").lower()
    bad = ["acquisition", "capital", "holdings", "adr"]
    return any(x in n for x in bad)


def translate_industry(industry):
    raw = (industry or "").strip().lower()
    mapping = {
        "airlines": "طيران",
        "biotechnology": "تقنية حيوية",
        "healthcare": "رعاية صحية",
        "medical devices": "أجهزة طبية",
        "pharmaceuticals": "أدوية",
        "software": "برمجيات",
        "semiconductors": "أشباه موصلات",
        "banks": "بنوك",
        "capital markets": "أسواق مالية",
        "oil & gas": "نفط وغاز",
        "oil and gas": "نفط وغاز",
        "energy": "طاقة",
        "insurance": "تأمين",
        "real estate": "عقار",
        "internet content & information": "محتوى وخدمات إنترنت",
        "consumer electronics": "إلكترونيات استهلاكية",
        "telecom services": "اتصالات",
        "auto manufacturers": "تصنيع سيارات",
        "specialty retail": "تجزئة متخصصة",
        "packaged foods": "أغذية",
        "aerospace & defense": "فضاء ودفاع",
        "industrial distribution": "توزيع صناعي",
        "electrical equipment & parts": "معدات كهربائية",
        "diagnostics & research": "تشخيص وأبحاث",
        "drug manufacturers": "تصنيع أدوية",
        "shell companies": "شركة استحواذ",
        "asset management": "إدارة أصول",
        "credit services": "خدمات ائتمانية",
        "food products": "منتجات غذائية",
        "farm products": "منتجات زراعية",
        "metal fabrication": "تصنيع معادن",
        "electronic components": "مكونات إلكترونية",
        "specialty chemicals": "كيماويات متخصصة",
    }
    if not raw:
        return "غير محدد"
    return mapping.get(raw, industry)


def translate_headline(headline):
    text = (headline or "").strip()
    if not text:
        return "لا يوجد خبر"

    replacements = {
        "shares": "الأسهم",
        "share": "السهم",
        "stock": "السهم",
        "stocks": "الأسهم",
        "expands": "توسّع",
        "reports": "تعلن",
        "earnings": "الأرباح",
        "revenue": "الإيرادات",
        "guidance": "التوقعات",
        "acquires": "تستحوذ على",
        "merger": "اندماج",
        "approval": "موافقة",
        "launches": "تطلق",
        "partnership": "شراكة",
        "upgrades": "رفع التقييم",
        "downgrades": "خفض التقييم",
        "price target": "السعر المستهدف",
        "american airlines": "أمريكان إيرلاينز",
        "inc.": "",
        "corp.": "",
        "corporation": "شركة",
    }

    out = text
    for en, ar in replacements.items():
        out = out.replace(en, ar)
        out = out.replace(en.title(), ar)
        out = out.replace(en.upper(), ar)

    if out == text:
        return f"يوجد خبر: {text[:90]}"
    return f"يوجد خبر: {out[:90]}"


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


def get_quote(sym):
    q = api("/quote", {"symbol": sym})
    if not q:
        return None

    c = safe_float(q.get("c"))
    pc = safe_float(q.get("pc"))
    o = safe_float(q.get("o"))
    h = safe_float(q.get("h"))

    if c <= 0 or pc <= 0:
        return None

    change = ((c - pc) / pc) * 100
    open_c = ((c - o) / o) * 100 if o > 0 else 0
    near_high = h > 0 and c >= h * 0.995

    return {
        "price": c,
        "change": change,
        "open": open_c,
        "near_high": near_high,
    }


def get_profile(sym):
    cached = profile_cache.get(sym)
    if cached:
        return cached

    p = api("/stock/profile2", {"symbol": sym})
    if not p:
        profile = {
            "name": "",
            "industry": "غير محدد",
            "market_cap_m": 0,
            "shares_outstanding_m": 0,
        }
        profile_cache[sym] = profile
        return profile

    profile = {
        "name": p.get("name", ""),
        "industry": translate_industry(p.get("finnhubIndustry", "")),
        "market_cap_m": safe_float(p.get("marketCapitalization"), 0),
        "shares_outstanding_m": safe_float(p.get("shareOutstanding"), 0),
    }
    profile_cache[sym] = profile
    return profile


def get_news(sym):
    cached = news_cache.get(sym)
    if cached and time.time() - cached["ts"] < 3600:
        return cached["value"]

    try:
        today = datetime.utcnow().date()
        from_date = (today - timedelta(days=3)).isoformat()
        to_date = today.isoformat()
        data = api("/company-news", {"symbol": sym, "from": from_date, "to": to_date})

        if isinstance(data, list) and len(data) > 0:
            value = translate_headline(data[0].get("headline", ""))
        else:
            value = "لا يوجد خبر"

        news_cache[sym] = {"value": value, "ts": time.time()}
        return value
    except Exception:
        return "لا يوجد خبر"


def cooldown_ok(sym):
    t = last_sent_time.get(sym)
    if not t:
        return True
    return time.time() - t > COOLDOWN_SECONDS


def liquidity_score(q, p):
    market_cap_m = safe_float(p.get("market_cap_m", 0), 0)
    score = 0

    if MIN_MARKET_CAP_M <= market_cap_m <= MAX_MARKET_CAP_M:
        score += 1
    if q["change"] >= MIN_CHANGE_PCT:
        score += 1
    if q["open"] >= MIN_OPEN_CHANGE_PCT:
        score += 1
    if q["near_high"]:
        score += 1

    return score


def eligible(q, profile):
    if not (MIN_PRICE <= q["price"] <= MAX_PRICE):
        return False
    if q["change"] < MIN_CHANGE_PCT:
        return False
    if q["open"] < MIN_OPEN_CHANGE_PCT:
        return False
    if not q["near_high"]:
        return False
    if profile and bad_company(profile["name"]):
        return False

    market_cap_m = safe_float(profile.get("market_cap_m", 0), 0) if profile else 0
    if market_cap_m < MIN_MARKET_CAP_M or market_cap_m > MAX_MARKET_CAP_M:
        return False

    if liquidity_score(q, profile) < 4:
        return False

    return True


def build(sym, q, p, news):
    return f"""🚀 سهم قوي الآن

🏷️ السهم: {sym}
🏢 اسم الشركة: {p.get('name', '') or 'غير متاح'}
💵 السعر: {q['price']:.2f}
📈 نسبة الارتفاع: {q['change']:.2f}%
⚡ من الافتتاح: {q['open']:.2f}%

🧠 الحالة: زخم قوي + قريب من القمة
🎯 قريب من أعلى اليوم: {"نعم" if q["near_high"] else "لا"}
🏭 نشاط الشركة: {p.get('industry', 'غير محدد')}
🧮 عدد أسهم الشركة: {fmt_num(safe_float(p.get("shares_outstanding_m", 0), 0) * 1_000_000)}
🏢 حجم الشركة: {size_label(p.get("market_cap_m", 0))}
🏛️ القيمة السوقية: {fmt_num(safe_float(p.get("market_cap_m", 0), 0) * 1_000_000)}
📰 الخبر: {news}

🕒 الجلسة: {get_session()}
"""


def run():
    send("🔥 Finnhub V5 شغال | فلترة صارمة + سيولة تقريبية + ترجمة عربية")

    while True:
        try:
            symbols = get_symbols()
            batch = get_batch(symbols)
            logging.info("بدأ الفحص | batch=%s", len(batch))

            candidates = []

            for s in batch:
                q = get_quote(s)
                time.sleep(REQUEST_DELAY)
                if not q:
                    continue

                p = get_profile(s)
                time.sleep(0.2)

                if eligible(q, p) and cooldown_ok(s):
                    candidates.append((s, q, p))

            candidates.sort(
                key=lambda x: (
                    liquidity_score(x[1], x[2]),
                    x[1]["change"],
                    x[1]["open"]
                ),
                reverse=True
            )

            sent = 0
            for s, q, p in candidates[:TOP_RESULTS_LIMIT]:
                news = get_news(s)
                time.sleep(0.2)
                send(build(s, q, p, news))
                last_sent_time[s] = time.time()
                sent += 1
                logging.info("SEND %s", s)

            if sent == 0:
                logging.info("لا توجد فرص قوية في هذه الدورة")

        except Exception as e:
            logging.exception("RUN ERROR: %s", e)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    if require_env():
        run()
