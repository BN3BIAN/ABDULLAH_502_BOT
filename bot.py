import requests
import time

# ====== الإعدادات ======
TELEGRAM_TOKEN = "8727048281:AAHj5QrnJtkp84g1JwzhtWwNiB0_EleqWcY"
CHAT_ID = "718432991"
FINNHUB_API_KEY = "d6s44g9r01qrb5i8hvegd6s44g9r01qrb5i8hvf0"

# ====== إرسال تيليجرام ======
def send_alert(symbol, price, change, volume):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    
    text = f"""🚀 دخول مبكر!

السهم: {symbol}
السعر: {price}
التغير: {change}%
الحجم: {volume}

⚡ ممكن انفجار قريب"""

    data = {
        "chat_id": CHAT_ID,
        "text": text
    }

    try:
        requests.post(url, data=data)
    except:
        pass

# ====== فلتر الرموز ======
def is_valid_symbol(symbol):
    return symbol.isalpha() and 3 <= len(symbol) <= 4

# ====== جلب الأسهم ======
def get_symbols():
    url = f"https://finnhub.io/api/v1/stock/symbol?exchange=US&token={FINNHUB_API_KEY}"
    
    try:
        data = requests.get(url).json()
        return [x["symbol"] for x in data if is_valid_symbol(x["symbol"])]
    except:
        return []

# ====== بيانات سهم ======
def get_data(symbol):
    try:
        quote_url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={FINNHUB_API_KEY}"
        profile_url = f"https://finnhub.io/api/v1/stock/profile2?symbol={symbol}&token={FINNHUB_API_KEY}"

        quote = requests.get(quote_url).json()
        profile = requests.get(profile_url).json()

        price = quote.get("c", 0)
        prev = quote.get("pc", 0)
        open_price = quote.get("o", 0)

        volume = profile.get("shareOutstanding", 0)  # تقريب للسيولة

        if price == 0 or prev == 0 or open_price == 0:
            return None

        change = ((price - prev) / prev) * 100

        return price, change, open_price, volume

    except:
        return None

# ====== فلتر قبل الانفجار ======
def is_early_move(price, change, open_price, volume):
    
    if price < 2:  # استبعاد الرخيص جدًا
        return False

    if change < 1 or change > 5:  # حركة مبكرة
        return False

    if price < open_price:  # لازم فوق الافتتاح
        return False

    if volume < 10000000:  # سيولة قوية (تقدير)
        return False

    return True

# ====== التشغيل ======
def run_bot():
    sent = set()
    symbols = get_symbols()

    print(f"📊 عدد الأسهم: {len(symbols)}")

    while True:
        for symbol in symbols[:80]:

            if symbol in sent:
                continue

            data = get_data(symbol)

            if not data:
                continue

            price, change, open_price, volume = data

            if is_early_move(price, change, open_price, volume):
                print(f"⚡ دخول مبكر: {symbol}")
                send_alert(symbol, round(price,2), round(change,2), volume)
                sent.add(symbol)

        time.sleep(60)

# ====== تشغيل ======
run_bot()
