import os
import time
import math
import traceback
from datetime import datetime

import requests
import yfinance as yf

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("CHAT_ID", "718432991").strip()

# تقدر تعدل قائمة الأسهم هنا
WATCHLIST = [
    "IPW", "ANY", "TLYS", "EONR", "ACXP", "ASNS", "DXST",
    "BNAI", "ANPA", "MOBX", "PLTX", "OBAI"
]

CHECK_INTERVAL_SECONDS = 300  # كل 5 دقائق
MIN_PRICE = 0.30
MAX_PRICE = 20.0
MIN_RVOL = 2.0
MIN_CHANGE_PCT = 4.0
MIN_VOLUME = 300_000

sent_cache = {}


def send_telegram_message(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        print("BOT_TOKEN أو CHAT_ID غير موجود")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=20)
        print("Telegram:", r.status_code, r.text[:200])
    except Exception as e:
        print("Telegram send failed:", e)


def safe_float(value, default=0.0):
    try:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return default
        return float(value)
    except Exception:
        return default


def get_history(symbol: str):
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period="5d", interval="5m", auto_adjust=False, prepost=True)
        if df is None or df.empty:
            return None
        return df
    except Exception as e:
        print(f"{symbol} history error:", e)
        return None


def analyze_symbol(symbol: str):
    df = get_history(symbol)
    if df is None or len(df) < 30:
        return None

    close = safe_float(df["Close"].iloc[-1])
    prev_close = safe_float(df["Close"].iloc[-2])
    high = safe_float(df["High"].iloc[-1])
    low = safe_float(df["Low"].iloc[-1])
    volume = safe_float(df["Volume"].iloc[-1])

    avg_volume = safe_float(df["Volume"].tail(20).mean(), 1.0)
    rvol = volume / avg_volume if avg_volume > 0 else 0.0
    change_pct = ((close - prev_close) / prev_close * 100.0) if prev_close > 0 else 0.0

    # VWAP تقريبي للجلسة الحالية من الداتا المتاحة
    typical_price = (df["High"] + df["Low"] + df["Close"]) / 3.0
    cum_tpv = (typical_price * df["Volume"]).cumsum()
    cum_vol = df["Volume"].cumsum().replace(0, 1)
    vwap = safe_float((cum_tpv / cum_vol).iloc[-1])

    # زخم بسيط
    ema9 = safe_float(df["Close"].ewm(span=9, adjust=False).mean().iloc[-1])
    ema20 = safe_float(df["Close"].ewm(span=20, adjust=False).mean().iloc[-1])

    # اختراق آخر 20 شمعة
    recent_high = safe_float(df["High"].tail(20).max())
    breakout = close >= recent_high * 0.995

    # قوة الشمعة الحالية
    candle_body = abs(close - safe_float(df["Open"].iloc[-1]))
    candle_range = max(high - low, 0.0001)
    body_strength = candle_body / candle_range

    bullish = close > ema9 and ema9 > ema20 and close > vwap
    strong_move = (
        close >= MIN_PRICE and
        close <= MAX_PRICE and
        volume >= MIN_VOLUME and
        rvol >= MIN_RVOL and
        change_pct >= MIN_CHANGE_PCT and
        bullish and
        body_strength >= 0.45
    )

    if not strong_move:
        return None

    score = 0
    score += 25 if rvol >= 3 else 15
    score += 20 if change_pct >= 8 else 12
    score += 15 if close > vwap else 0
    score += 15 if ema9 > ema20 else 0
    score += 15 if breakout else 5
    score += 10 if body_strength >= 0.6 else 5

    result = {
        "symbol": symbol,
        "price": round(close, 4),
        "change_pct": round(change_pct, 2),
        "rvol": round(rvol, 2),
        "volume": int(volume),
        "vwap": round(vwap, 4),
        "ema9": round(ema9, 4),
        "ema20": round(ema20, 4),
        "breakout": breakout,
        "score": min(score, 100),
    }
    return result


def should_send(symbol: str, score: int, price: float) -> bool:
    now_bucket = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    key = f"{symbol}:{score // 10}:{round(price, 2)}"
    last_sent = sent_cache.get(symbol)

    if last_sent == key:
        return False

    sent_cache[symbol] = key
    return True


def format_alert(data: dict) -> str:
    breakout_text = "نعم" if data["breakout"] else "لا"
    return (
        f"🚨 <b>تنبيه سهم زخم</b>\n\n"
        f"• السهم: <b>{data['symbol']}</b>\n"
        f"• السعر: <b>{data['price']}</b>\n"
        f"• التغير: <b>{data['change_pct']}%</b>\n"
        f"• RVOL: <b>{data['rvol']}</b>\n"
        f"• الفوليوم: <b>{data['volume']:,}</b>\n"
        f"• VWAP: <b>{data['vwap']}</b>\n"
        f"• EMA9 / EMA20: <b>{data['ema9']} / {data['ema20']}</b>\n"
        f"• اختراق: <b>{breakout_text}</b>\n"
        f"• السكور: <b>{data['score']}/100</b>\n\n"
        f"مناسب للمراقبة السريعة، وليس دخولًا مضمونًا."
    )


def scan_market():
    found = []
    for symbol in WATCHLIST:
        try:
            data = analyze_symbol(symbol)
            if not data:
                continue
            found.append(data)
        except Exception:
            print(f"Error analyzing {symbol}")
            traceback.print_exc()

    found.sort(key=lambda x: (x["score"], x["rvol"], x["change_pct"]), reverse=True)

    for item in found[:5]:
        if should_send(item["symbol"], item["score"], item["price"]):
            send_telegram_message(format_alert(item))


def main():
    print("Stock bot started...")
    send_telegram_message("✅ البوت اشتغل على السيرفر بنجاح")

    while True:
        try:
            print("Scanning stocks...", datetime.utcnow().isoformat())
            scan_market()
        except Exception:
            traceback.print_exc()
            send_telegram_message("⚠️ صار خطأ داخل البوت، لكن جاري المحاولة من جديد.")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
