import os
import time
import requests
import pandas as pd

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

CHECK_INTERVAL = 60
SYMBOL_LIMIT = 40
MIN_PRICE = 0.3
MIN_VOLUME = 20000
MIN_CHANGE = 5.0

last_sent = {}

def send(msg):
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": msg}
    )

def get_symbols():
    url = f"https://finnhub.io/api/v1/stock/symbol?exchange=US&token={FINNHUB_API_KEY}"
    data = requests.get(url).json()
    out = []
    for s in data:
        sym = s.get("symbol", "")
        if sym.isalpha() and len(sym) <= 5:
            out.append(sym)
        if len(out) >= SYMBOL_LIMIT:
            break
    return out

def get_quote(sym):
    url = f"https://finnhub.io/api/v1/quote?symbol={sym}&token={FINNHUB_API_KEY}"
    d = requests.get(url).json()
    if not d or d.get("c", 0) == 0:
        return None
    change = ((d["c"] - d["pc"]) / d["pc"]) * 100
    return d["c"], change

def get_candle(sym):
    now = int(time.time())
    url = f"https://finnhub.io/api/v1/stock/candle?symbol={sym}&resolution=1&from={now-3600}&to={now}&token={FINNHUB_API_KEY}"
    d = requests.get(url).json()
    if d.get("s") != "ok":
        return None

    df = pd.DataFrame({
        "c": d["c"],
        "v": d["v"]
    })

    if len(df) < 5:
        return None

    last_vol = df.iloc[-1]["v"]
    total_vol = df["v"].sum()
    last_price = df.iloc[-1]["c"]
    prev_high = df["c"].iloc[:-1].max()

    return last_vol, total_vol, last_price, prev_high

def get_profile(sym):
    url = f"https://finnhub.io/api/v1/stock/profile2?symbol={sym}&token={FINNHUB_API_KEY}"
    d = requests.get(url).json()
    return {
        "industry": d.get("finnhubIndustry", "غير معروف"),
        "shares": d.get("shareOutstanding", 0)
    }

def get_news(sym):
    url = f"https://finnhub.io/api/v1/company-news?symbol={sym}&from=2025-01-01&to=2026-12-31&token={FINNHUB_API_KEY}"
    d = requests.get(url).json()
    return "يوجد خبر" if d else "لا يوجد خبر"

def should_send(sym, price):
    old = last_sent.get(sym)
    if not old:
        return True
    return abs(price - old) / old * 100 > 0.5

def scan():
    symbols = get_symbols()

    for sym in symbols:
        q = get_quote(sym)
        if not q:
            continue

        price, change = q

        if price < MIN_PRICE or change < MIN_CHANGE:
            continue

        candle = get_candle(sym)
        if not candle:
            continue

        last_vol, total_vol, last_price, prev_high = candle

        if total_vol < MIN_VOLUME:
            continue

        profile = get_profile(sym)
        news = get_news(sym)

        if last_price > prev_high:
            state = "اختراق"
        elif change > 8:
            state = "صعود قوي"
        else:
            state = "صعود"

        msg = f"""
🚀 تنبيه سهم

السهم: {sym}
السعر: {price}
الارتفاع: {change:.2f}%

الحالة: {state}

حجم التداول: {int(total_vol)}
سيولة آخر دقيقة: {int(last_vol)}

عدد الأسهم: {profile['shares']}
النشاط: {profile['industry']}

الخبر: {news}
"""

        if should_send(sym, price):
            send(msg)
            last_sent[sym] = price

        time.sleep(0.3)

while True:
    scan()
    time.sleep(CHECK_INTERVAL)
