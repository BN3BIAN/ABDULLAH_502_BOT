import requests
import time

# ====== الإعدادات ======
TELEGRAM_TOKEN = "8727048281:AAHj5QrnJtkp84g1JwzhtWwNiB0_EleqWcY"
CHAT_ID = "718432991"
FINNHUB_API_KEY = "d6s44g9r01qrb5i8hvegd6s44g9r01qrb5i8hvf0"

tracked = {}  # الأسهم اللي نراقبها
sent = set()  # الأسهم اللي أرسلناها

# ====== إرسال ======
def send_alert(symbol, price, change):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    
    text = f"""🚀 تم الصيد!

السهم: {symbol}
السعر: {price}
التغير: {change}%

🔥 انفجار مؤكد"""

    try:
        requests.post(url, data={"chat_id": CHAT_ID, "text": text})
    except:
        pass

# ====== فلتر رموز ======
def is_valid_symbol(symbol):
    return symbol.isalpha() and 3 <= len(symbol) <= 4

# ====== جلب الأسهم (فلترة قوية) ======
def get_symbols():
    url = f"https://finnhub.io/api/v1/stock/symbol?exchange=US&token={FINNHUB_API_KEY}"
    
    try:
        data = requests.get(url).json()

        symbols = []

        for x in data:
            s = x.get("symbol", "")
            d = x.get("description", "").lower()

            if not is_valid_symbol(s):
                continue

            if any(w in d for w in ["adr", "holding", "acquisition", "capital", "fund"]):
                continue

            symbols.append(s)

        return symbols[:250]  # 👈 نخليها خفيفة وسريعة

    except:
        return []

# ====== جلب السعر ======
def get_price(symbol):
    try:
        url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={FINNHUB_API_KEY}"
        data = requests.get(url).json()

        price = data.get("c", 0)
        prev = data.get("pc", 0)

        if price == 0 or prev == 0:
            return None

        change = ((price - prev) / prev) * 100

        return price, change

    except:
        return None

# ====== منطق الصيد ======
def hunter_logic(symbol, price, change):
    
    # المرحلة 1: رصد مبكر
    if symbol not in tracked:
        if 1 <= change <= 3:
            tracked[symbol] = price
            print(f"👀 تم رصد: {symbol}")
        return

    # المرحلة 2: تأكيد الانفجار
    start_price = tracked[symbol]

    move = ((price - start_price) / start_price) * 100

    if move >= 2 and symbol not in sent:
        print(f"🔥 تم الصيد: {symbol}")
        send_alert(symbol, round(price,2), round(change,2))
        sent.add(symbol)

# ====== التشغيل ======
def run_bot():
    symbols = get_symbols()

    print(f"🎯 عدد الأسهم بعد الفلترة: {len(symbols)}")

    while True:
        for symbol in symbols:

            data = get_price(symbol)

            if not data:
                continue

            price, change = data

            if price < 2:  # استبعاد الرخيص
                continue

            hunter_logic(symbol, price, change)

        time.sleep(30)  # أسرع = أدق

# ====== تشغيل ======
run_bot()
