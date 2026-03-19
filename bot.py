import requests
import time

API_KEY = "YOUR_API_KEY"

TELEGRAM_TOKEN = "YOUR_BOT_TOKEN"
CHAT_ID = "YOUR_CHAT_ID"

# إعدادات النخبة
MIN_VOLUME = 1000000
MIN_PRICE = 1
MAX_PRICE = 100
RELATIVE_VOLUME = 2


def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.get(url, params={"chat_id": CHAT_ID, "text": msg})


def is_valid_symbol(symbol):
    if len(symbol) > 4:
        return False
    if symbol.endswith(('W', 'U', 'R', 'Q', 'P')):
        return False
    return True


def get_stocks():
    url = f"https://finnhub.io/api/v1/stock/symbol?exchange=US&token={API_KEY}"
    return requests.get(url).json()


def get_quote(symbol):
    url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={API_KEY}"
    return requests.get(url).json()


def elite_filter(stock):
    symbol = stock['symbol']

    # فلترة الرمز
    if not is_valid_symbol(symbol):
        return False

    # فلترة السوق
    if stock['exchange'] not in ['NASDAQ', 'NYSE', 'AMEX']:
        return False

    data = get_quote(symbol)

    price = data.get('c', 0)
    volume = data.get('v', 0)

    if price < MIN_PRICE or price > MAX_PRICE:
        return False

    if volume < MIN_VOLUME:
        return False

    return True


def check_breakout(symbol):
    url = f"https://finnhub.io/api/v1/stock/candle?symbol={symbol}&resolution=1&count=20&token={API_KEY}"
    data = requests.get(url).json()

    if data['s'] != 'ok':
        return False

    highs = data['h']
    last_price = highs[-1]
    prev_high = max(highs[:-1])

    if last_price > prev_high:
        return True

    return False


def run_bot():
    print("🚀 Elite Bot v2 Started...")

    while True:
        try:
            stocks = get_stocks()

            for stock in stocks:
                if elite_filter(stock):
                    symbol = stock['symbol']

                    if check_breakout(symbol):
                        msg = f"🔥 دخول قوي: {symbol}"
                        print(msg)
                        send_telegram(msg)

            time.sleep(60)

        except Exception as e:
            print("Error:", e)
            time.sleep(30)


run_bot()
