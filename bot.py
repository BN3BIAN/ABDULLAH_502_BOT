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
MIN_PRICE = float(os.getenv("MIN_PRICE", "0.30"))
MAX_PRICE = float(os.getenv("MAX_PRICE", "20"))
MAX_CANDIDATES = int(os.getenv("MAX_CANDIDATES", "80"))
TOP_ALERTS_PER_SCAN = int(os.getenv("TOP_ALERTS_PER_SCAN", "5"))
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "20"))

MIN_RVOL = float(os.getenv("MIN_RVOL", "1.2"))
MIN_DAY_CHANGE_PCT = float(os.getenv("MIN_DAY_CHANGE_PCT", "3.0"))
MIN_LAST_MIN_DOLLAR_VOL = float(os.getenv("MIN_LAST_MIN_DOLLAR_VOL", "15000"))
MIN_DAY_VOLUME = int(os.getenv("MIN_DAY_VOLUME", "50000"))

HALAL_SYMBOLS = {x.strip().upper() for x in os.getenv("HALAL_SYMBOLS", "").split(",") if x.strip()}
HARAM_SYMBOLS = {x.strip().upper() for x in os.getenv("HARAM_SYMBOLS", "").split(",") if x.strip()}

FMP_API_KEY = os.getenv("FMP_API_KEY", "demo").strip()

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

last_alert_time = {}


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
        x = float(x)
    except Exception:
        return "0%"
    s = f"{x:+.2f}%"
    return s.replace("+0.00%", "0%")


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
        return "خارج الجلسات الرئيسية"
    except Exception:
        return "غير محدد"


def can_send_alert(symbol: str) -> bool:
    now = datetime.now(timezone.utc)
    last_time = last_alert_time.get(symbol)
    if last_time is None:
        return True
    minutes_passed = (now - last_time).total_seconds() / 60
    return minutes_passed >= COOLDOWN_MINUTES


def mark_alert_sent(symbol: str):
    last_alert_time[symbol] = datetime.now(timezone.utc)


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
        col = possible_cols[0]
        vals = df[col].astype(str).tolist()
    else:
        vals = []
        for col in df.columns:
            vals.extend(df[col].astype(str).tolist())

    for v in vals:
        m = re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", v.strip().upper())
        if m:
            candidates.append(v.strip().upper())

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
                    syms = _extract_symbols_from_table(df)
                    collected.extend(syms)

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
        return fetch_fmp_candidates()

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
        t = yf.Ticker(symbol)
        info = t.info or {}
        return info
    except Exception:
        return {}


def get_news_headline(symbol: str) -> str:
    try:
        t = yf.Ticker(symbol)
        items = t.news or []
        if not items:
            return "لا يوجد خبر"
        title = items[0].get("title") or ""
        if not title:
            return "لا يوجد خبر"
        return title[:180]
    except Exception:
        return "لا يوجد خبر"


def compute_metrics(symbol: str, df: pd.DataFrame, info: dict) -> dict | None:
    try:
        df = df.copy()

        typical = (df["High"] + df["Low"] + df["Close"]) / 3
        df["TPV"] = typical * df["Volume"]
        vol_cum = df["Volume"].replace(0, pd.NA).cumsum()
        df["VWAP"] = df["TPV"].cumsum() / vol_cum

        df["AvgVol20"] = df["Volume"].rolling(20).mean()
        df["RVOL"] = df["Volume"] / df["AvgVol20"]

        latest = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else latest

        price = safe_float(latest["Close"])
        vwap = safe_float(latest["VWAP"])
        last_min_vol = safe_int(latest["Volume"])
        avg_vol20 = safe_float(latest["AvgVol20"])
        rvol = safe_float(latest["RVOL"])
        minute_change_pct = ((price - safe_float(prev["Close"])) / safe_float(prev["Close"], 1)) * 100

        today_open = safe_float(df.iloc[0]["Open"], price)
        day_change_pct = ((price - today_open) / (today_open if today_open else 1)) * 100

        day_volume = safe_int(info.get("regularMarketVolume", 0))
        if day_volume == 0:
            day_volume = safe_int(df["Volume"].sum())

        shares_outstanding = safe_int(info.get("sharesOutstanding", 0))
        float_shares = safe_int(info.get("floatShares", 0))
        company_name = info.get("shortName") or info.get("longName") or symbol
        industry = info.get("industry") or info.get("sector") or "غير متوفر"
        summary = info.get("longBusinessSummary") or ""
        market_cap = safe_int(info.get("marketCap", 0))

        last_min_dollar_vol = price * last_min_vol
        liquidity_power = (last_min_vol / avg_vol20) if avg_vol20 > 0 else 0.0

        if day_change_pct >= 8 and rvol >= 2:
            momentum = "عالي جدًا"
        elif day_change_pct >= 4 and rvol >= 1.5:
            momentum = "عالي"
        elif day_change_pct >= 2:
            momentum = "متوسط"
        else:
            momentum = "ضعيف"

        if minute_change_pct >= 0.7 and last_min_dollar_vol >= MIN_LAST_MIN_DOLLAR_VOL:
            whale = "نعم 🐳"
        elif rvol >= 3 and last_min_dollar_vol >= (MIN_LAST_MIN_DOLLAR_VOL * 2):
            whale = "محتمل 🐳"
        else:
            whale = "لا"

        if price > vwap and day_change_pct > 0 and rvol >= 1.3:
            trend = "صعود"
        elif price < vwap and day_change_pct < 0:
            trend = "هبوط"
        else:
            trend = "انتظار"

        recent_high = safe_float(df["High"].tail(15).max(), price)
        trigger = round(recent_high + 0.01, 4)

        if trend == "صعود" and momentum in {"عالي", "عالي جدًا"} and whale != "لا":
            entry = "مناسب لدخول سريع بحذر"
        elif price < recent_high:
            entry = f"انتظار اختراق {trigger}"
        else:
            entry = "انتظار"

        if symbol in HALAL_SYMBOLS:
            sharia = "شرعي"
        elif symbol in HARAM_SYMBOLS:
            sharia = "غير شرعي"
        else:
            sharia = "غير محدد"

        return {
            "symbol": symbol,
            "company_name": company_name,
            "industry": industry,
            "business_summary": summary[:200],
            "price": round(price, 4),
            "vwap": round(vwap, 4),
            "day_change_pct": round(day_change_pct, 2),
            "minute_change_pct": round(minute_change_pct, 2),
            "last_min_vol": last_min_vol,
            "day_volume": day_volume,
            "avg_vol20": avg_vol20,
            "rvol": round(rvol, 2),
            "liquidity_power": round(liquidity_power, 2),
            "last_min_dollar_vol": round(last_min_dollar_vol, 2),
            "shares_outstanding": shares_outstanding,
            "float_shares": float_shares,
            "market_cap": market_cap,
            "momentum": momentum,
            "whale": whale,
            "trend": trend,
            "entry": entry,
            "trigger": trigger,
            "sharia": sharia,
        }
    except Exception:
        return None


def is_candidate(metrics: dict) -> bool:
    price = metrics["price"]
    day_change = metrics["day_change_pct"]
    rvol = metrics["rvol"]
    day_vol = metrics["day_volume"]
    last_min_dollar_vol = metrics["last_min_dollar_vol"]

    if not (MIN_PRICE <= price <= MAX_PRICE):
        return False

    if day_vol < MIN_DAY_VOLUME:
        return False

    if (
        day_change >= MIN_DAY_CHANGE_PCT
        or rvol >= MIN_RVOL
        or last_min_dollar_vol >= MIN_LAST_MIN_DOLLAR_VOL
    ):
        return True

    return False


def score_stock(metrics: dict, is_gainer: bool, is_active: bool) -> float:
    score = 0.0
    score += max(0, metrics["day_change_pct"]) * 2.0
    score += max(0, metrics["rvol"]) * 8.0
    score += min(metrics["liquidity_power"], 10) * 6.0
    score += min(metrics["last_min_dollar_vol"] / 10000.0, 20) * 2.5

    if metrics["trend"] == "صعود":
        score += 12
    if metrics["whale"] != "لا":
        score += 10
    if is_gainer:
        score += 8
    if is_active:
        score += 8

    return round(score, 2)


def build_alert_text(metrics: dict, is_gainer: bool, is_active: bool, news_title: str) -> str:
    labels = []
    if is_gainer:
        labels.append("📈 الأكثر ارتفاعًا")
    if is_active:
        labels.append("⚡ الأكثر تداولًا")
    if metrics["momentum"] in {"عالي", "عالي جدًا"}:
        labels.append("🔥 زخم قوي")
    if metrics["whale"] != "لا":
        labels.append("🐳 دخول حوت")
    if metrics["trend"] == "انتظار":
        labels.append("⏳ انتظار")
    elif metrics["trend"] == "هبوط":
        labels.append("📉 هبوط")
    else:
        labels.append("✅ صعود")

    labels_line = " | ".join(labels) if labels else "📊 مراقبة"

    return (
        f"{labels_line}\n\n"
        f"السهم: {metrics['symbol']}\n"
        f"الجلسة: {get_session_label()}\n"
        f"السعر: {metrics['price']}\n"
        f"الحالة: {metrics['trend']}\n"
        f"القرار: {metrics['entry']}\n\n"
        f"1- الزخم: {metrics['momentum']}\n"
        f"2- عدد أسهم الشركة: {fmt_num(metrics['shares_outstanding'])}\n"
        f"3- عدد الأسهم المطروحة: {fmt_num(metrics['float_shares'])}\n"
        f"4- عدد الأسهم المتداولة: {fmt_num(metrics['day_volume'])}\n"
        f"5- حجم التداول الآن: {fmt_num(metrics['day_volume'])}\n"
        f"6- التغير اليومي: {pct_str(metrics['day_change_pct'])}\n"
        f"7- قوة السيولة: {metrics['liquidity_power']}x\n"
        f"8- سيولة آخر دقيقة: {fmt_num(metrics['last_min_vol'])}\n"
        f"9- RVOL: {metrics['rvol']}\n"
        f"10- الشرعية: {metrics['sharia']}\n"
        f"11- مناسب للدخول: {metrics['entry']}\n"
        f"12- دخول حوت: {metrics['whale']}\n"
        f"13- عمل الشركة: {metrics['industry']}\n"
        f"14- الخبر: {news_title if news_title else 'لا يوجد خبر'}\n"
        f"15- الاتجاه: {metrics['trend']}\n\n"
        f"VWAP: {metrics['vwap']}\n"
    )


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
                "%s | score=%s | change=%s | rvol=%s | trend=%s",
                symbol,
                score,
                metrics["day_change_pct"],
                metrics["rvol"],
                metrics["trend"]
            )

        except Exception as e:
            logging.exception("Scan error for %s: %s", symbol, e)

    ranked.sort(key=lambda x: x["score"], reverse=True)

    sent_count = 0
    for item in ranked:
        if sent_count >= TOP_ALERTS_PER_SCAN:
            break

        symbol = item["symbol"]
        if not can_send_alert(symbol):
            continue

        news_title = get_news_headline(symbol)
        text = build_alert_text(
            item["metrics"],
            item["is_gainer"],
            item["is_active"],
            news_title
        )

        if send_telegram_message(text):
            mark_alert_sent(symbol)
            sent_count += 1
            logging.info("Alert sent for %s", symbol)

    if sent_count == 0:
        logging.info("No alerts sent this round.")


def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("أضف TELEGRAM_BOT_TOKEN و TELEGRAM_CHAT_ID")
        return

    startup = (
        "✅ البوت اشتغل وبدأ فحص السوق الأمريكي\n"
        "يشمل: Top Gainers + Most Active\n"
        f"نطاق السعر: {MIN_PRICE} إلى {MAX_PRICE}"
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
