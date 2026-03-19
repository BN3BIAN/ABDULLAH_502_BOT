import requests
import time
import telegram

# ====== الإعدادات ======
TELEGRAM_TOKEN = "8727048281:AAHj5QrnJtkp84g1JwzhtWwNiB0_EleqWcY"
CHAT_ID = 718432991
FINNHUB_API_KEY = "d6s44g9r01qrb5i8hvegd6s44g9r01qrb5i8hvf0"

bot = telegram.Bot(token=TELEGRAM_TOKEN)

# ====== فلتر الرموز ======
def is_valid_symbol(symbol):
    if not symbol:
        return False
    
    if not symbol.isalpha():
        return False
    
    if len(symbol) < 3 or len(symbol) > 4:
        return False

    return True

# ====== جلب الأسهم ======
def get_stocks():
    url = f"https://finnhub.io/api/v1/stock/symbol?exchange=US&token={FINNHUB_API_KEY}"
    
    try:
        response = requests.get(url)

        if response.status_code != 200:
            print("❌ فشل الاتصال:", response.status_code)
            return []

        try:
            data = response.json()
        except:
            print("❌ الرد مو JSON")
            return []

        if not isinstance(data, list):
            print("❌ البيانات مو قائمة")
            return []

        return data

    except Exception as e:
        print("❌ خطأ:", e)
        return []

# ====== فلترة ======
def filter_stocks(stocks):
    filtered = []

    for stock in stocks:
        try:
            symbol = stock.get("symbol", "")
            desc = stock.get("description", "").lower()

            if not is_valid_symbol(symbol):
                continue

            if any(x in desc for x in ["acquisition", "holdings", "adr", "capital"]):
                continue

            filtered.append(symbol)

        except:
            continue

    return filtered

# ====== إرسال ======
def send_alert(symbol):
    try:
        bot.send_message(chat_id=CHAT_ID, text=f"🚀 فرصة محتملة: {symbol}")
    except Exception as e:
        print("❌ خطأ إرسال:", e)

# ====== تشغيل ======
def run_bot():
    sent = set()

    while True:
        stocks = get_stocks()
        filtered = filter_stocks(stocks)

        print(f"📊 بعد الفلترة: {len(filtered)}")

        for symbol in filtered:
            if symbol not in sent:
                send_alert(symbol)
                sent.add(symbol)

        time.sleep(60)

run_bot()
