import os
import re
import time
import math
import logging
from datetime import datetime, timezone

import pandas as pd
import requests
import yfinance as yf


# =========================
# الإعدادات
# =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
MIN_PRICE = float(os.getenv("MIN_PRICE", "0.20"))
MAX_PRICE = float(os.getenv("MAX_PRICE", "20"))
MAX_CANDIDATES = int(os.getenv("MAX_CANDIDATES", "150"))
TOP_ALERTS_PER_SCAN = int(os.getenv("TOP_ALERTS_PER_SCAN", "5"))

MIN_RVOL = float(os.getenv("MIN_RVOL", "0.3"))
MIN_DAY_CHANGE_PCT = float(os.getenv("MIN_DAY_CHANGE_PCT", "0.3"))
MIN_LAST_MIN_DOLLAR_VOL = float(os.getenv("MIN_LAST_MIN_DOLLAR_VOL", "1000"))
MIN_DAY_VOLUME = int(os.getenv("MIN_DAY_VOLUME", "1000"))

# إعادة التنبيه فقط إذا تحسن فعلي
REPEAT_ALERT_MIN_PRICE_PCT = float(os.getenv("REPEAT_ALERT_MIN_PRICE_PCT", "0.5"))
REPEAT_ALERT_MIN_RVOL_DELTA = float(os.getenv("REPEAT_ALERT_MIN_RVOL_DELTA", "0.2"))
REPEAT_ALERT_MIN_VOL_DELTA_PCT = float(os.getenv("REPEAT_ALERT_MIN_VOL_DELTA_PCT", "10"))

# كشف مبكر قبل الانفجار
EARLY_BREAKOUT_DISTANCE_PCT = float(os.getenv("EARLY_BREAKOUT_DISTANCE_PCT", "2.0"))
EARLY_MIN_RVOL = float(os.getenv("EARLY_MIN_RVOL", "0.5"))
EARLY_MIN_DAY_CHANGE_PCT = float(os.getenv("EARLY_MIN_DAY_CHANGE_PCT", "0.3"))

HALAL_SYMBOLS = {x.strip().upper() for x in os.getenv("HALAL_SYMBOLS", "").split(",") if x.strip()}
HARAM_SYMBOLS = {x.strip().upper() for x in os.getenv("HARAM_SYMBOLS", "").split(",") if x.strip()}

FMP_API_KEY = os.getenv("FMP_API_KEY", "demo").strip()

FALLBACK_SYMBOLS = [
    "BBAI", "SOUN", "PLTR", "SOFI", "MARA", "RIOT", "LCID", "NIO",
    "ACHR", "QBTS", "RGTI", "IONQ", "HIMS", "OPEN", "RUN",
    "FUBO", "ASTS", "DNA", "RXRX", "BMEA", "ALT", "TNXP", "SLS",
    "UEC", "DNN", "AAPL", "TSLA", "AMD", "NVDA", "SMCI",
    "INTC", "ABEV", "GRAB", "WULF", "CLS", "KULR", "SERV"
]

HEADERS = {"User-Agent": "Mozilla/5.0"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# تخزين آخر تنبيه لكل سهم
last_alert_meta = {}


# =========================
# أدوات عامة
# =========================
def fmt_num(n) -> str:
    try:
        n = float(n)
    except Exception:
        return "0"

    sign = "-" if n < 0 else ""
    n = abs(n)

    if n >= 1_000_000_000:
        v = n / 1_000_000_000
        return f"{sign}{v:.2f}".rstrip("0").rstrip(".") + "B"
    if n >= 1_000_000:
        v = n / 1_000_000
        return f"{sign}{v:.2f}".rstrip("0").rstrip(".") + "M"
    if n >= 1_000:
        v = n / 1_000
        return f"{sign}{v:.2f}".rstrip("0").rstrip(".") + "K"
    if n.is_integer():
        return f"{sign}{int(n)}"
    return f"{sign}{n:.2f}".rstrip("0").rstrip(".")


def pct_str(x) -> str:
    try:
        return f"{float(x):+.2f}%"
    except Exception:
        return "0%"


def safe_float(x, default=0.0):
    try:
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return default
        return float(x)
    except Exception:
        return default


def safe_int(x, default=0):
    try:
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return default
        return int(float(x))
    except Exception:
        return default


def get_session_label() -> str:
    try:
        from zoneinfo import ZoneInfo
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


def send_telegram_message(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("TELEGRAM_BOT_TOKEN أو TELEGRAM_CHAT_ID غير موجود.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text
    }

    try:
        r = requests.post(url, json=payload, timeout=25)
        if r.status_code == 200:
            return True
        logging.error("Telegram error: %s | %s", r.status_code, r.text)
        return False
    except Exception as e:
        logging.exception("Telegram exception: %s", e)
        return False


# =========================
# ترجمة نشاط الشركة
# =========================
SECTOR_MAP = {
    "Technology": "تقنية",
    "Healthcare": "رعاية صحية",
    "Financial Services": "خدمات مالية",
    "Industrials": "صناعات",
    "Energy": "طاقة",
    "Consumer Cyclical": "استهلاكي دوري",
    "Consumer Defensive": "استهلاكي أساسي",
    "Communication Services": "اتصالات",
    "Basic Materials": "مواد أساسية",
    "Real Estate": "عقارات",
    "Utilities": "مرافق",
}

INDUSTRY_MAP = {
    "Capital Markets": "الاستثمار والأسواق المالية",
    "Biotechnology": "التكنولوجيا الحيوية وتطوير الأدوية",
    "Semiconductors": "الرقائق الإلكترونية وأشباه الموصلات",
    "Software - Infrastructure": "البرمجيات والبنية التحتية الرقمية",
    "Software - Application": "البرمجيات والتطبيقات",
    "Auto Manufacturers": "تصنيع السيارات",
    "Solar": "الطاقة الشمسية",
    "Oil & Gas E&P": "استكشاف وإنتاج النفط والغاز",
    "Uranium": "اليورانيوم والطاقة النووية",
    "Medical Devices": "الأجهزة الطبية",
    "Drug Manufacturers - Specialty & Generic": "تصنيع الأدوية",
}


def business_arabic(info: dict) -> str:
    sector = (info.get("sector") or "").strip()
    industry = (info.get("industry") or "").strip()

    sector_ar = SECTOR_MAP.get(sector, sector if sector else "غير متوفر")
    industry_ar = INDUSTRY_MAP.get(industry, industry if industry else "غير متوفر")

    if industry_ar != "غير متوفر":
        return f"تعمل الشركة في مجال {industry_ar} ضمن قطاع {sector_ar}"
    return f"تعمل الشركة في قطاع {sector_ar}"


# =========================
# جلب المرشحين
# =========================
def _clean_symbol_list(symbols):
    clean = []
    seen = set()

    for s in symbols:
        if not s:
            continue

        s = str(s).strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", s):
            continue

        if s not in seen:
            seen.add(s)
            clean.append(s)

    return clean


def _extract_symbols_from_table(df: pd.DataFrame) -> list[str]:
    candidates = []
    possible_cols = [c for c in df.columns if str(c).strip().lower() in {"symbol", "ticker"}]

    if possible_cols:
        vals = df[possible_cols[0]].astype(str).tolist()
    else:
        vals = []
        for col in df.columns:
            vals.extend(df[col].astype(str).tolist())

    for v in vals:
        s = v.strip().upper()
        if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", s):
            candidates.append(s)

    return candidates


def fetch_fmp_candidates() -> tuple[list[str], set[str], set[str]]:
    gainers_url = f"https://financialmodelingprep.com/api/v3/stock_market/gainers?apikey={FMP_API_KEY}"
    actives_url = f"https://financialmodelingprep.com/api/v3/stock_market/actives?apikey={FMP_API_KEY}"

    gainers = []
    actives = []

    try:
        r = requests.get(gainers_url, headers=HEADERS, timeout=20)
        data = r.json() if r.ok else []
        if isinstance(data, list):
            for item in data:
                symbol = item.get("symbol")
                if symbol:
                    gainers.append(symbol)
    except Exception as e:
        logging.warning("FMP gainers fetch failed: %s", e)

    try:
        r = requests.get(actives_url, headers=HEADERS, timeout=20)
        data = r.json() if r.ok else []
        if isinstance(data, list):
            for item in data:
                symbol = item.get("symbol")
                if symbol:
                    actives.append(symbol)
    except Exception as e:
        logging.warning("FMP actives fetch failed: %s", e)

    gainers = _clean_symbol_list(gainers)
    actives = _clean_symbol_list(actives)

    ordered = []
    seen = set()
    for s in gainers + actives:
        if s not in seen:
            seen.add(s)
            ordered.append(s)

    return ordered[:MAX_CANDIDATES], set(gainers), set(actives)


def fetch_yahoo_candidates() -> tuple[list[str], set[str], set[str]]:
    urls = {
        "gainers": [
            "https://finance.yahoo.com/markets/stocks/gainers/",
            "https://finance.yahoo.com/research-hub/screener/gainers/",
        ],
        "most_active": [
            "https://finance.yahoo.com/markets/stocks/most-active/",
            "https://finance.yahoo.com/screener/predefined/most_actives?count=100&offset=0",
            "https://finance.yahoo.com/research-hub/screener/most-active/",
        ]
    }

    all_symbols = []
    gainers_set = set()
    active_set = set()

    for kind, url_list in urls.items():
        collected = []
        for url in url_list:
            try:
                tables = pd.read_html(url)
                for df in tables:
                    collected.extend(_extract_symbols_from_table(df))
                if collected:
                    break
            except Exception:
                continue

        collected = _clean_symbol_list(collected)

        if kind == "gainers":
            gainers_set.update(collected)
        else:
            active_set.update(collected)

        all_symbols.extend(collected)

    ordered = []
    seen = set()
    for s in all_symbols:
        if s not in seen:
            seen.add(s)
            ordered.append(s)

    ordered = ordered[:MAX_CANDIDATES]

    if not ordered:
        logging.info("Yahoo returned 0 candidates, switching to FMP fallback...")
        ordered, gainers_set, active_set = fetch_fmp_candidates()

        if not ordered:
            logging.info("FMP also returned 0 candidates, switching to fallback symbol list...")
            fallback = _clean_symbol_list(FALLBACK_SYMBOLS)
            return fallback[:MAX_CANDIDATES], set(), set()

        return ordered, gainers_set, active_set

    return ordered, gainers_set, active_set


# =========================
# بيانات السهم
# =========================
def get_intraday(symbol: str) -> pd.DataFrame | None:
    try:
        df = yf.download(
            tickers=symbol,
            period="2d",
            interval="1m",
            prepost=True,
            progress=False,
            auto_adjust=False,
            threads=False
        )

        if df is None or df.empty:
            return None

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]

        required = ["Open", "High", "Low", "Close", "Volume"]
        if not all(c in df.columns for c in required):
            return None

        df = df.dropna().copy()
        if df.empty:
            return None

        return df
    except Exception:
        return None


def get_info_fast(symbol: str) -> dict:
    try:
        return yf.Ticker(symbol).info or {}
    except Exception:
        return {}


def get_news_headline(symbol: str) -> str:
    try:
        items = yf.Ticker(symbol).news or []
        if not items:
            return "لا يوجد خبر"
        title = items[0].get("title") or ""
        return title[:160] if title else "لا يوجد خبر"
    except Exception:
        return "لا يوجد خبر"


def compute_metrics(symbol: str, df: pd.DataFrame, info: dict) -> dict | None:
    try:
        df = df.copy()

        typical = (df["High"] + df["Low"] + df["Close"]) / 3
        df["TPV"] = typical * df["Volume"]
        vol_cum = df["Volume"].replace(0, pd.NA).cumsum()
        df["VWAP"] = df["TPV"].cumsum() / vol_cum

        df["AvgVol20_1m"] = df["Volume"].replace(0, pd.NA).rolling(20).mean()
        df["RVOL_1m"] = df["Volume"] / df["AvgVol20_1m"]

        latest = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else latest

        price = safe_float(latest["Close"])
        vwap = safe_float(latest["VWAP"])
        last_1m_vol = safe_int(latest["Volume"])
        rvol_1m = safe_float(latest["RVOL_1m"])
        liquidity_power = rvol_1m if rvol_1m > 0 else 0.0

        minute_change_pct = ((price - safe_float(prev["Close"])) / safe_float(prev["Close"], 1)) * 100

        today_open = safe_float(df.iloc[0]["Open"], price)
        day_change_pct = ((price - today_open) / (today_open if today_open else 1)) * 100

        day_volume = safe_int(info.get("regularMarketVolume", 0))
        if day_volume == 0:
            day_volume = safe_int(df["Volume"].sum())

        shares_outstanding = safe_int(info.get("sharesOutstanding", 0))
        float_shares = safe_int(info.get("floatShares", 0))

        last_1m_dollar_vol = price * last_1m_vol
        recent_high = safe_float(df["High"].tail(15).max(), price)
        breakout_distance_pct = ((recent_high - price) / price) * 100 if price > 0 else 999.0
        trigger = round(recent_high + 0.01, 4)

        if day_change_pct >= 8 and rvol_1m >= 2:
            momentum = "عالي جدًا"
        elif day_change_pct >= 4 and rvol_1m >= 1.2:
            momentum = "عالي"
        elif day_change_pct >= 1:
            momentum = "متوسط"
        else:
            momentum = "ضعيف"

        if minute_change_pct >= 0.5 and last_1m_dollar_vol >= MIN_LAST_MIN_DOLLAR_VOL:
            whale = "نعم 🐳"
        elif rvol_1m >= 1.8 and last_1m_dollar_vol >= (MIN_LAST_MIN_DOLLAR_VOL * 1.2):
            whale = "محتمل 🐳"
        else:
            whale = "لا"

        if price > vwap and day_change_pct > 0 and rvol_1m >= 1.0:
            trend = "صعود"
        elif price < vwap and day_change_pct < 0:
            trend = "هبوط"
        else:
            trend = "انتظار"

        if trend == "صعود" and momentum in {"عالي", "عالي جدًا"}:
            entry = "دخول سريع بحذر"
        elif breakout_distance_pct <= EARLY_BREAKOUT_DISTANCE_PCT:
            entry = f"قريب من اختراق {trigger}"
        else:
            entry = "انتظار"

        if price <= 5 and rvol_1m >= 1.0 and last_1m_dollar_vol >= MIN_LAST_MIN_DOLLAR_VOL:
            setup_type = "سكالب"
        elif trend == "صعود" and momentum in {"عالي", "عالي جدًا"} and price > 3:
            setup_type = "مومنتم"
        elif (
            breakout_distance_pct <= EARLY_BREAKOUT_DISTANCE_PCT
            and day_change_pct >= EARLY_MIN_DAY_CHANGE_PCT
            and rvol_1m >= EARLY_MIN_RVOL
        ):
            setup_type = "قبل الانفجار"
        else:
            setup_type = "مراقبة"

        if symbol in HALAL_SYMBOLS:
            sharia = "شرعي"
        elif symbol in HARAM_SYMBOLS:
            sharia = "غير شرعي"
        else:
            sharia = "غير محدد"

        return {
            "symbol": symbol,
            "price": round(price, 4),
            "vwap": round(vwap, 4),
            "day_change_pct": round(day_change_pct, 2),
            "minute_change_pct": round(minute_change_pct, 2),
            "last_1m_vol": last_1m_vol,
            "last_1m_dollar_vol": round(last_1m_dollar_vol, 2),
            "day_volume": day_volume,
            "rvol": round(rvol_1m, 2),
            "liquidity_power": round(liquidity_power, 2),
            "shares_outstanding": shares_outstanding,
            "float_shares": float_shares,
            "momentum": momentum,
            "whale": whale,
            "trend": trend,
            "entry": entry,
            "trigger": trigger,
            "breakout_distance_pct": round(breakout_distance_pct, 2),
            "setup_type": setup_type,
            "sharia": sharia,
            "business_ar": business_arabic(info),
        }
    except Exception:
        return None


# =========================
# فلترة السهم
# =========================
def is_candidate(metrics: dict) -> bool:
    price = safe_float(metrics.get("price", 0), 0)
    day_change = safe_float(metrics.get("day_change_pct", 0), 0)
    rvol = safe_float(metrics.get("rvol", 0), 0)
    last_1m_dollar_vol = safe_float(metrics.get("last_1m_dollar_vol", 0), 0)
    trend = metrics.get("trend", "")
    setup_type = metrics.get("setup_type", "")

    if not (MIN_PRICE <= price <= MAX_PRICE):
        return False

    if safe_int(metrics.get("day_volume", 0), 0) < MIN_DAY_VOLUME:
        return False

    rules_hit = 0

    if day_change >= MIN_DAY_CHANGE_PCT:
        rules_hit += 1

    if rvol >= MIN_RVOL:
        rules_hit += 1

    if last_1m_dollar_vol >= MIN_LAST_MIN_DOLLAR_VOL:
        rules_hit += 1

    if trend == "صعود":
        rules_hit += 1

    if setup_type in {"سكالب", "مومنتم", "قبل الانفجار"}:
        rules_hit += 1

    return rules_hit >= 1


def score_stock(metrics: dict, is_gainer: bool, is_active: bool) -> float:
    score = 0.0
    score += max(0, metrics["day_change_pct"]) * 2.0
    score += max(0, metrics["rvol"]) * 8.0
    score += min(metrics["liquidity_power"], 10) * 6.0
    score += min(metrics["last_1m_dollar_vol"] / 10000.0, 20) * 2.5

    if metrics["trend"] == "صعود":
        score += 12
    if metrics["whale"] != "لا":
        score += 10
    if is_gainer:
        score += 8
    if is_active:
        score += 8
    if metrics["setup_type"] == "قبل الانفجار":
        score += 10
    elif metrics["setup_type"] == "سكالب":
        score += 7
    elif metrics["setup_type"] == "مومنتم":
        score += 9

    return round(score, 2)


# =========================
# منع التكرار
# =========================
def get_repeat_number(symbol: str, metrics: dict, news_title: str) -> int | None:
    old = last_alert_meta.get(symbol)
    current_price = safe_float(metrics["price"], 0.0)
    current_rvol = safe_float(metrics["rvol"], 0.0)
    current_vol = safe_int(metrics["last_1m_vol"], 0)
    current_trend = metrics["trend"]
    current_trigger = safe_float(metrics["trigger"], 0.0)
    current_setup = metrics["setup_type"]

    if old is None:
        return 1

    old_price = safe_float(old.get("price"), 0.0)
    old_count = safe_int(old.get("count"), 1)
    old_rvol = safe_float(old.get("rvol"), 0.0)
    old_vol = safe_int(old.get("last_1m_vol"), 0)
    old_trend = old.get("trend", "")
    old_trigger = safe_float(old.get("trigger"), 0.0)
    old_news = old.get("news", "")
    old_setup = old.get("setup_type", "")

    improved = False

    if old_price > 0:
        price_change_pct = ((current_price - old_price) / old_price) * 100
        if price_change_pct >= REPEAT_ALERT_MIN_PRICE_PCT:
            improved = True

    if (current_rvol - old_rvol) >= REPEAT_ALERT_MIN_RVOL_DELTA:
        improved = True

    if old_vol > 0:
        vol_change_pct = ((current_vol - old_vol) / old_vol) * 100
        if vol_change_pct >= REPEAT_ALERT_MIN_VOL_DELTA_PCT:
            improved = True
    elif current_vol > 0 and old_vol == 0:
        improved = True

    if current_trend != old_trend:
        improved = True

    if abs(current_trigger - old_trigger) >= 0.01:
        improved = True

    if current_setup != old_setup:
        improved = True

    if news_title and news_title != "لا يوجد خبر" and news_title != old_news:
        improved = True

    if improved:
        return old_count + 1

    return None


def mark_repeat_alert(symbol: str, metrics: dict, news_title: str, repeat_number: int):
    last_alert_meta[symbol] = {
        "price": safe_float(metrics["price"], 0.0),
        "count": repeat_number,
        "rvol": safe_float(metrics["rvol"], 0.0),
        "last_1m_vol": safe_int(metrics["last_1m_vol"], 0),
        "trend": metrics["trend"],
        "trigger": safe_float(metrics["trigger"], 0.0),
        "news": news_title,
        "setup_type": metrics["setup_type"],
        "time": datetime.now(timezone.utc).isoformat(),
    }


# =========================
# بناء التنبيه
# =========================
def build_alert_text(metrics: dict, is_gainer: bool, is_active: bool, news_title: str, repeat_number: int) -> str:
    badges = []

    if repeat_number == 1:
        badges.append("🚨 تنبيه 1")
    else:
        badges.append(f"🚨 تنبيه {repeat_number}")
        badges.append(f"📈 صعود {repeat_number}")

    setup_type = metrics.get("setup_type", "مراقبة")
    if setup_type == "سكالب":
        badges.append("⚡ سكالب")
    elif setup_type == "مومنتم":
        badges.append("🚀 مومنتم")
    elif setup_type == "قبل الانفجار":
        badges.append("💥 قبل الانفجار")
    else:
        badges.append("👀 مراقبة")

    if is_gainer:
        badges.append("🔥 الأكثر ارتفاعًا")
    if is_active:
        badges.append("📊 الأكثر تداولًا")

    whale = metrics.get("whale", "لا")
    if whale != "لا":
        badges.append("🐳 سيولة قوية")

    trend = metrics.get("trend", "انتظار")
    if trend == "صعود":
        badges.append("✅ صعود")
    elif trend == "هبوط":
        badges.append("📉 هبوط")
    else:
        badges.append("⏳ انتظار")

    return "\n".join([
        " | ".join(badges),
        "",
        f"📈 السهم: {metrics.get('symbol', '-')}",
        f"🕒 الجلسة: {get_session_label()}",
        f"💰 السعر: {metrics.get('price', '-')}",
        f"📊 التغير اليومي: {pct_str(metrics.get('day_change_pct', 0))}",
        f"🎯 القرار: {metrics.get('entry', 'انتظار')}",
        "",
        f"⚡ نوع الفرصة: {setup_type}",
        f"🔥 الزخم: {metrics.get('momentum', '-')}",
        f"🏢 أسهم الشركة: {fmt_num(metrics.get('shares_outstanding', 0))}",
        f"📦 الأسهم المطروحة: {fmt_num(metrics.get('float_shares', 0))}",
        f"🔁 حجم التداول اليوم: {fmt_num(metrics.get('day_volume', 0))}",
        f"💧 سيولة آخر دقيقة: {fmt_num(metrics.get('last_1m_vol', 0))}",
        f"📏 RVOL: {metrics.get('rvol', 0)}",
        f"💦 قوة السيولة: {metrics.get('liquidity_power', 0)}x",
        f"🕌 الشرعية: {metrics.get('sharia', 'غير محدد')}",
        f"🐳 دخول حوت: {whale}",
        f"🏭 عمل الشركة: {metrics.get('business_ar', 'غير متوفر')}",
        f"📰 الخبر: {news_title if news_title else 'لا يوجد خبر'}",
        f"📍 الاتجاه: {trend}",
        f"📌 VWAP: {metrics.get('vwap', 0)}",
        f"🎯 مسافة الاختراق: {metrics.get('breakout_distance_pct', 0)}%",
        f"🚪 الاختراق عند: {metrics.get('trigger', '-')}",
    ])


# =========================
# الفحص
# =========================
def scan_once():
    symbols, gainers_set, active_set = fetch_yahoo_candidates()
    logging.info("Candidates fetched: %s", len(symbols))

    ranked = []

    for symbol in symbols:
        try:
            df = get_intraday(symbol)
            if df is None or df.empty:
                continue

            info = get_info_fast(symbol)
            metrics = compute_metrics(symbol, df, info)
            if not metrics:
                continue

            if not is_candidate(metrics):
                continue

            is_gainer = symbol in gainers_set
            is_active = symbol in active_set
            score = score_stock(metrics, is_gainer, is_active)

            ranked.append({
                "symbol": symbol,
                "score": score,
                "metrics": metrics,
                "is_gainer": is_gainer,
                "is_active": is_active,
            })

            logging.info(
                "%s | score=%s | type=%s | change=%s | rvol=%s | trend=%s",
                symbol,
                score,
                metrics.get("setup_type"),
                metrics.get("day_change_pct"),
                metrics.get("rvol"),
                metrics.get("trend")
            )

        except Exception as e:
            logging.exception("Scan error for %s: %s", symbol, e)

    ranked.sort(key=lambda x: x["score"], reverse=True)

    sent_count = 0
    seen_symbols_this_round = set()

    for item in ranked:
        if sent_count >= TOP_ALERTS_PER_SCAN:
            break

        symbol = item["symbol"]

        if symbol in seen_symbols_this_round:
            continue

        news_title = get_news_headline(symbol)
        repeat_number = get_repeat_number(symbol, item["metrics"], news_title)

        if repeat_number is None:
            old_meta = last_alert_meta.get(symbol)
            if old_meta is None:
                repeat_number = 1
            else:
                continue

        text = build_alert_text(
            item["metrics"],
            item["is_gainer"],
            item["is_active"],
            news_title,
            repeat_number
        )

        if send_telegram_message(text):
            mark_repeat_alert(symbol, item["metrics"], news_title, repeat_number)
            sent_count += 1
            seen_symbols_this_round.add(symbol)
            logging.info("Alert sent for %s | repeat=%s", symbol, repeat_number)

    if sent_count == 0:
        logging.info("No alerts sent this round.")


def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("أضف TELEGRAM_BOT_TOKEN و TELEGRAM_CHAT_ID")
        return

    startup = (
        "✅ البوت اشتغل وبدأ Smart Market Scanner\n"
        "🔎 يبحث في: Top Gainers + Most Active + Fallback\n"
        f"💰 نطاق السعر: {MIN_PRICE} إلى {MAX_PRICE}\n"
        "♻️ لا يعيد نفس السهم إلا إذا تحسن فعلاً\n"
        "💥 ويدعم كشف الأسهم قبل الانفجار"
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
