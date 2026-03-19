import yfinance as yf
import pandas as pd

def analyze_stock(symbol):
    data = yf.download(symbol, period="5d", interval="5m")

    if data.empty:
        print("No data found")
        return

    # حساب المتوسطات
    data['MA20'] = data['Close'].rolling(window=20).mean()
    data['Volume_MA'] = data['Volume'].rolling(window=20).mean()

    latest = data.iloc[-1]

    price = latest['Close']
    ma = latest['MA20']
    volume = latest['Volume']
    volume_ma = latest['Volume_MA']

    print(f"\n📊 تحليل السهم: {symbol}")
    print(f"السعر الحالي: {price}")
    print(f"المتوسط: {ma}")
    print(f"الفوليوم: {volume}")

    # شروط بسيطة
    trend = "محايد"
    momentum = "ضعيف"
    liquidity = "ضعيفة"

    if price > ma:
        trend = "صاعد 📈"
    else:
        trend = "هابط 📉"

    if volume > volume_ma:
        liquidity = "قوية 🔥"

    if price > ma and volume > volume_ma:
        momentum = "قوي 🚀"

    print(f"الاتجاه: {trend}")
    print(f"السيولة: {liquidity}")
    print(f"الزخم: {momentum}")

    # قرار تقريبي
    if trend == "صاعد 📈" and liquidity == "قوية 🔥":
        print("👉 احتمال دخول جيد")
    else:
        print("👉 انتظر فرصة أفضل")


# مثال
analyze_stock("AAPL")
