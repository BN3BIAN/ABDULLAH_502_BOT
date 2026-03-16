import os
import time
import logging
from datetime import datetime

import requests
import pandas as pd
import yfinance as yf


# =========================
# الإعدادات
# =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# ضع الأسهم هنا أو من متغير Railway باسم STOCK_SYMBOLS
DEFAULT_SYMBOLS = ["BNAI", "ANPA", "ASNS", "DXST"]
STOCK_SYMBOLS = [
    s.strip().upper()
    for s in os.getenv("STOCK_SYMBOLS", ",".join(DEFAULT_SYMBOLS)).split(",")
    if s.strip()
]

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "60"))  # بالثواني
MIN_PRICE = float(os.getenv("MIN_PRICE", "0.2"))
MAX_PRICE = float(os.getenv("MAX_PRICE", "20"))
MIN_RVOL = float(os.getenv("MIN_RVOL", "2.0"))
MIN_CHANGE_PCT = float(os.getenv("MIN_CHANGE_PCT", "2.0"))
MIN_VOLUME_SPIKE = float(os.getenv("MIN_VOLUME_SPIKE", "1.8"))

# منع التكرار لنفس السهم خلال مدة قصيرة
COOLDOWN_MINUTES = int(os.getenv("COOLDOWN_MINUTES", "15"))


# =========================
# اللوق
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)


# =========================
# تخزين آخر تنبيه
# =========================
last_alert_time = {}


# =========================
# تيليجرام
# =========================
def send_telegram_message(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("TELEGRAM_BOT_TOKEN أو TELEGRAM_CHAT_ID غير موجود.")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
    }

    try:
        response = requests.post(url, json=payload, timeout=20)
        if response.status_code == 200:
            logging.info("تم إرسال رسالة تيليجرام بنجاح.")
            return True
        logging.error("فشل إرسال الرسالة: %s | %s", response.status_code, response.text)
        return False
    except Exception as e:
        logging.exception("خطأ أثناء إرسال تيليجرام: %s", e)
        return False


# =========================
# تحميل البيانات
# =========================
def get_stock_data(symbol: str) -> pd.DataFrame | None:
    try:
        df = yf.download(
            tickers=symbol,
            period="1d",
            interval="1m",
            progress=False,
            auto_adjust=False,
            threads=False
        )

        if df is None or df.empty:
            logging.warning("%s | لا توجد بيانات.", symbol)
            return None

        # إذا رجعت MultiIndex
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]

        required_cols = ["Open", "High", "Low", "Close", "Volume"]
        missing = [c for c in required_cols if c not in df.columns]
        if missing:
            logging.warning("%s | أعمدة ناقصة: %s", symbol, missing)
            return None

        df = df.dropna().copy()
        if df.empty:
            logging.warning("%s | البيانات بعد التنظيف فارغة.", symbol)
            return None

        return df

    except Exception as e:
        logging.exception("%s | خطأ أثناء جلب البيانات: %s", symbol, e)
        return None


# =========================
# حساب المؤشرات
# =========================
def compute_metrics(df: pd.DataFrame) -> dict | None:
    try:
        df = df.copy()

        typical_price = (df["High"] + df["Low"] + df["Close"]) / 3
        df["TPV"] = typical_price * df["Volume"]
        df["VWAP"] = df["TPV"].cumsum() / df["Volume"].replace(0, pd.NA).cumsum()

        df["AvgVol20"] = df["Volume"].rolling(20).mean()
        df["RVOL"] = df["Volume"] / df["AvgVol20"]

        latest = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else latest
        first = df.iloc[0]

        price = float(latest["Close"])
        vwap = float(latest["VWAP"]) if pd.notna(latest["VWAP"]) else 0.0
        volume = float(latest["Volume"])
        avg_vol20 = float(latest["AvgVol20"]) if pd.notna(latest["AvgVol20"]) else 0.0
        rvol = float(latest["RVOL"]) if pd.notna(latest["RVOL"]) else 0.0

        day_change_pct = ((price - float(first["Open"])) / float(first["Open"])) * 100 if float(first["Open"]) != 0 else 0.0
        minute_change_pct = ((price - float(prev["Close"])) / float(prev["Close"])) * 100 if float(prev["Close"]) != 0 else 0.0
        volume_spike = (volume / avg_vol20) if avg_vol20 > 0 else 0.0

        return {
            "price": round(price, 4),
            "vwap": round(vwap, 4),
            "volume": int(volume),
            "avg_vol20": int(avg_vol20) if avg_vol20 > 0 else 0,
            "rvol": round(rvol, 2),
            "day_change_pct": round(day_change_pct, 2),
            "minute_change_pct": round(minute_change_pct, 2),
            "volume_spike": round(volume_spike, 2),
        }

    except Exception as e:
        logging.exception("خطأ أثناء حساب المؤشرات: %s", e)
        return None


# =========================
# فلترة الإشارة
# =========================
def evaluate_signal(symbol: str, metrics: dict) -> tuple[bool, str]:
    price = metrics["price"]
    vwap = metrics["vwap"]
    rvol = metrics["rvol"]
    day_change_pct = metrics["day_change_pct"]
    volume_spike = metrics["volume_spike"]

    reasons = []

    if not (MIN_PRICE <= price <= MAX_PRICE):
        reasons.append(f"السعر خارج النطاق ({price})")
    else:
        reasons.append(f"السعر مناسب ({price})")

    if price > vwap:
        reasons.append(f"فوق VWAP ({vwap})")
    else:
        reasons.append(f"تحت VWAP ({vwap})")

    if rvol >= MIN_RVOL:
        reasons.append(f"RVOL قوي ({rvol})")
    else:
        reasons.append(f"RVOL ضعيف ({rvol})")

    if day_change_pct >= MIN_CHANGE_PCT:
        reasons.append(f"زخم يومي جيد ({day_change_pct}%)")
    else:
        reasons.append(f"زخم يومي ضعيف ({day_change_pct}%)")

    if volume_spike >= MIN_VOLUME_SPIKE:
        reasons.append(f"فوليوم قوي ({volume_spike}x)")
    else:
        reasons.append(f"فوليوم ضعيف ({volume_spike}x)")

    passed = (
        MIN_PRICE <= price <= MAX_PRICE
        and price > vwap
        and rvol >= MIN_RVOL
        and day_change_pct >= MIN_CHANGE_PCT
        and volume_spike >= MIN_VOLUME_SPIKE
    )

    return passed, " | ".join(reasons)


# =========================
# منع تكرار التنبيه
# =========================
def can_send_alert(symbol: str) -> bool:
    now = datetime.utcnow()
    last_time = last_alert_time.get(symbol)

    if last_time is None:
        return True

    minutes_passed = (now - last_time).total_seconds() / 60
    return minutes_passed >= COOLDOWN_MINUTES


def mark_alert_sent(symbol: str):
    last_alert_time[symbol] = datetime.utcnow()


# =========================
# رسالة التنبيه
# =========================
def build_alert_message(symbol: str, metrics: dict) -> str:
    return (
        f"🚨 إشارة قوية\n\n"
        f"السهم: {symbol}\n"
        f"السعر: {metrics['price']}\n"
        f"VWAP: {metrics['vwap']}\n"
        f"RVOL: {metrics['rvol']}\n"
        f"التغير اليومي: {metrics['day_change_pct']}%\n"
        f"فوليوم آخر دقيقة: {metrics['volume']}\n"
        f"قوة الفوليوم: {metrics['volume_spike']}x\n"
    )


# =========================
# الفحص
# =========================
def scan_once():
    logging.info("جار فحص الأسهم...")

    for symbol in STOCK_SYMBOLS:
        df = get_stock_data(symbol)
        if df is None:
            logging.info("%s | لا توجد بيانات كافية.", symbol)
            continue

        metrics = compute_metrics(df)
        if metrics is None:
            logging.info("%s | تعذر حساب المؤشرات.", symbol)
            continue

        passed, details = evaluate_signal(symbol, metrics)
        logging.info("%s | %s", symbol, details)

        if passed:
            if can_send_alert(symbol):
                message = build_alert_message(symbol, metrics)
                sent = send_telegram_message(message)
                if sent:
                    mark_alert_sent(symbol)
                    logging.info("%s | تم إرسال تنبيه.", symbol)
            else:
                logging.info("%s | الإشارة موجودة لكن داخل فترة التبريد.", symbol)


# =========================
# التشغيل الرئيسي
# =========================
def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("أضف TELEGRAM_BOT_TOKEN و TELEGRAM_CHAT_ID في Railway Variables.")
        return

    logging.info("تم تشغيل البوت.")
    logging.info("الأسهم الحالية: %s", ", ".join(STOCK_SYMBOLS))
    logging.info("مدة الفحص: %s ثانية", CHECK_INTERVAL)

    # رسالة بدء اختيارية
    send_telegram_message("✅ البوت اشتغل وبدأ فحص الأسهم.")

    while True:
        try:
            scan_once()
        except Exception as e:
            logging.exception("خطأ في الحلقة الرئيسية: %s", e)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
