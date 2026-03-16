import os
import time
import math
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf


TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))
TOP_ALERTS_PER_SCAN = int(os.getenv("TOP_ALERTS_PER_SCAN", "3"))
MIN_PRICE = float(os.getenv("MIN_PRICE", "0.20"))
MAX_PRICE = float(os.getenv("MAX_PRICE", "20"))

SYMBOLS = [
    "BBAI", "SOUN", "PLTR", "SOFI", "MARA", "RIOT", "LCID", "NIO",
    "ACHR", "QBTS", "RGTI", "IONQ", "HIMS", "OPEN", "RUN",
    "FUBO", "ASTS", "DNA", "RXRX", "BMEA", "ALT", "TNXP", "SLS",
    "UEC", "DNN", "AAPL", "TSLA", "AMD", "NVDA", "SMCI",
    "INTC", "ABEV", "GRAB", "WULF", "CLS", "KULR", "SERV"
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

last_sent = {}


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


def fmt_num(n) -> str:
    try:
        n = float(n)
    except Exception:
        return "0"

    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.2f}".rstrip("0").rstrip(".") + "B"
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}".rstrip("0").rstrip(".") + "M"
    if n >= 1_000:
        return f"{n/1_000:.2f}".rstrip("0").rstrip(".") + "K"
    if float(n).is_integer():
        return str(int(n))
    return f"{n:.2f}".rstrip("0").rstrip(".")


def pct_str(x) -> str:
    try:
        return f"{float(x):+.2f}%"
    except Exception:
        return "0%"


def send_telegram_message(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}

    try:
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code == 200:
            return True
        logging.error("Telegram send failed: %s | %s", r.status_code, r.text)
        return False
    except Exception as e:
        logging.exception("Telegram exception: %s", e)
        return False


def get_session_label() -> str:
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


def should_send(symbol: str, price: float) -> bool:
    old = last_sent.get(symbol)
    if old is None:
        return True

    old_price = safe_float(old.get("price"), 0.0)
    if old_price <= 0:
        return True

    change_pct = ((price - old_price) / old_price) * 100
    return abs(change_pct) >= 0.3


def mark_sent(symbol: str, price: float):
    old = last_sent.get(symbol, {})
    count = safe_int(old.get("count"), 0) + 1
    last_sent[symbol] = {
        "price": price,
        "count": count,
    }


def get_repeat_count(symbol: str) -> int:
    return safe_int(last_sent.get(symbol, {}).get("count"), 0) + 1


def clean_downloaded_df(df: pd.DataFrame) -> pd.DataFrame | None:
    try:
        if df is None or df.empty:
            return None

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]

        keep = ["Open", "High", "Low", "Close", "Volume"]
        available = [c for c in keep if c in df.columns]
        if len(available) < 5:
            return None

        df = df[keep].copy()

        for col in keep:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df.dropna(subset=["Open", "High", "Low", "Close", "Volume"]).copy()
        if df.empty:
            return None

        return df
    except Exception:
        return None


def fetch_symbol_metrics(symbol: str):
    try:
        raw = yf.download(
            tickers=symbol,
            period="2d",
            interval="1m",
            prepost=True,
            progress=False,
            auto_adjust=False,
            threads=False,
        )

        df = clean_downloaded_df(raw)
        if df is None or df.empty:
            logging.info("%s | no clean data", symbol)
            return None

        latest = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else latest

        price = safe_float(latest["Close"])
        if not (MIN_PRICE <= price <= MAX_PRICE):
            return None

        day_open = safe_float(df.iloc[0]["Open"], price)
        day_change_pct = ((price - day_open) / (day_open if day_open else 1)) * 100
        minute_change_pct = ((price - safe_float(prev["Close"], price)) / (safe_float(prev["Close"], 1))) * 100

        day_volume = safe_int(df["Volume"].sum())
        last_1m_vol = safe_int(latest["Volume"])

        typical = (df["High"] + df["Low"] + df["Close"]) / 3.0
        tpv = typical * df["Volume"]
        vol_cum = df["Volume"].replace(0, pd.NA).cumsum()
        vwap_series = tpv.cumsum() / vol_cum
        vwap = safe_float(vwap_series.iloc[-1], 0)

        avg_vol20_series = df["Volume"].replace(0, pd.NA).rolling(20, min_periods=1).mean()
        avg_vol20 = safe_float(avg_vol20_series.iloc[-1], 0)
        rvol = (last_1m_vol / avg_vol20) if avg_vol20 > 0 else 0.0

        trend = "صعود" if price > vwap and day_change_pct > 0 else "انتظار"

        score = (
            max(0, day_change_pct) * 2
            + max(0, rvol) * 8
            + (5 if trend == "صعود" else 0)
            + min(last_1m_vol / 1000, 20)
        )

        return {
            "symbol": symbol,
            "price": round(price, 4),
            "day_change_pct": round(day_change_pct, 2),
            "minute_change_pct": round(minute_change_pct, 2),
            "day_volume": day_volume,
            "last_1m_vol": last_1m_vol,
            "rvol": round(rvol, 2),
            "vwap": round(vwap, 4),
            "trend": trend,
            "score": round(score, 2),
        }
    except Exception as e:
        logging.exception("fetch_symbol_metrics failed for %s: %s", symbol, e)
        return None


def build_alert_text(m: dict) -> str:
    repeat = get_repeat_count(m["symbol"])
    badges = [f"🚨 تنبيه {repeat}"]

    if m["day_change_pct"] >= 5:
        badges.append("🔥 ارتفاع قوي")
    if m["rvol"] >= 1:
        badges.append("💧 سيولة")
    if m["trend"] == "صعود":
        badges.append("✅ صعود")
    else:
        badges.append("⏳ مراقبة")

    return "\n".join([
        " | ".join(badges),
        "",
        f"📈 السهم: {m['symbol']}",
        f"🕒 الجلسة: {get_session_label()}",
        f"💰 السعر: {m['price']}",
        f"📊 التغير اليومي: {pct_str(m['day_change_pct'])}",
        f"⚡ تغير آخر دقيقة: {pct_str(m['minute_change_pct'])}",
        f"🔁 حجم التداول اليوم: {fmt_num(m['day_volume'])}",
        f"💧 سيولة آخر دقيقة: {fmt_num(m['last_1m_vol'])}",
        f"📏 RVOL: {m['rvol']}",
        f"📌 VWAP: {m['vwap']}",
        f"📍 الاتجاه: {m['trend']}",
        f"🏁 السكور: {m['score']}",
    ])


def scan_once():
    logging.info("Scanning %s symbols...", len(SYMBOLS))

    ranked = []
    for symbol in SYMBOLS:
        metrics = fetch_symbol_metrics(symbol)
        if metrics:
            ranked.append(metrics)
            logging.info(
                "%s | score=%s | change=%s | rvol=%s | trend=%s",
                metrics["symbol"],
                metrics["score"],
                metrics["day_change_pct"],
                metrics["rvol"],
                metrics["trend"],
            )

    ranked.sort(key=lambda x: x["score"], reverse=True)

    if not ranked:
        logging.info("No ranked symbols found.")
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
    startup = (
        "✅ البوت اشتغل بنسخة مبسطة ومضمونة\n"
        "🔎 يفحص قائمة أسهم متحركة كل 60 ثانية\n"
        "📩 سيرسل أفضل الأسهم مباشرة"
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
