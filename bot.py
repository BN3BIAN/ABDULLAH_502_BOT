import os
import yfinance as yf
import pandas as pd
import requests

from ta.momentum import RSIIndicator
from ta.trend import MACD, SMAIndicator

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

# =========================
# التوكن من Railway
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")

# =========================
# جلب بيانات السهم
# =========================
def get_data(symbol):
    ticker = yf.Ticker(symbol)
    info = ticker.info
    hist = ticker.history(period="6mo", interval="1d")

    if hist.empty:
        return None, None

    return info, hist


# =========================
# التحليل الفني المتقدم
# =========================
def technical_analysis(hist):
    df = hist.copy()

    close = df["Close"]

    # RSI
    rsi = RSIIndicator(close).rsi().iloc[-1]

    # MACD
    macd = MACD(close)
    macd_line = macd.macd().iloc[-1]
    macd_signal = macd.macd_signal().iloc[-1]

    # Moving Averages
    ma20 = SMAIndicator(close, window=20).sma_indicator().iloc[-1]
    ma50 = SMAIndicator(close, window=50).sma_indicator().iloc[-1]

    signals = []
    score = 0

    # RSI
    if rsi < 30:
        signals.append("RSI: تشبع بيع 🔥")
        score += 2
    elif rsi > 70:
        signals.append("RSI: تشبع شراء ⚠️")
        score -= 2
    else:
        signals.append("RSI: طبيعي")

    # MACD
    if macd_line > macd_signal:
        signals.append("MACD: صاعد 📈")
        score += 1
    else:
        signals.append("MACD: هابط 📉")
        score -= 1

    # MA
    last_price = close.iloc[-1]

    if last_price > ma20:
        signals.append("فوق MA20 ✅")
        score += 1
    else:
        signals.append("تحت MA20 ❌")
        score -= 1

    if last_price > ma50:
        signals.append("فوق MA50 ✅")
        score += 1
    else:
        signals.append("تحت MA50 ❌")
        score -= 1

    return signals, score, last_price, df


# =========================
# دعم ومقاومة
# =========================
def support_resistance(df):
    support = df["Close"].min()
    resistance = df["Close"].max()
    return support, resistance


# =========================
# الأخبار
# =========================
def get_news(symbol):
    try:
        url = f"https://query1.finance.yahoo.com/v1/finance/search?q={symbol}"
        res = requests.get(url).json()
        news = res.get("news", [])[:3]

        result = ""
        for n in news:
            result += f"- {n.get('title')}\n"

        return result if result else "لا توجد أخبار"
    except:
        return "تعذر جلب الأخبار"


# =========================
# الشرعية (مبدئي)
# =========================
def sharia_check(info):
    sector = info.get("sector", "").lower()

    if "financial" in sector or "bank" in sector:
        return "❌ غير متوافق (قطاع مالي)"
    return "⚖️ يحتاج مراجعة إضافية"


# =========================
# تقييم نهائي
# =========================
def get_signal(score):
    if score >= 3:
        return "🟢 Strong Buy"
    elif score == 2:
        return "🟢 Buy"
    elif score == 1 or score == 0:
        return "🟡 Neutral"
    else:
        return "🔴 Sell"


# =========================
# المعالج الرئيسي
# =========================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol = update.message.text.strip().upper()

    try:
        info, hist = get_data(symbol)

        if hist is None:
            await update.message.reply_text("❌ السهم غير موجود")
            return

        tech_signals, score, price, df = technical_analysis(hist)
        support, resistance = support_resistance(df)

        news = get_news(symbol)
        sharia = sharia_check(info)

        signal = get_signal(score)

        response = f"""
📊 {symbol}

💰 السعر: {price:.2f}

📈 التحليل الفني:
{chr(10).join(tech_signals)}

🎯 التقييم:
Score: {score}
Signal: {signal}

📉 الدعم: {support:.2f}
📈 المقاومة: {resistance:.2f}

📰 الأخبار:
{news}

⚖️ الشرعية:
{sharia}

🏢 الشركة:
{info.get("longBusinessSummary", "لا يوجد وصف")[:400]}...
"""

        await update.message.reply_text(response)

    except Exception as e:
        await update.message.reply_text("⚠️ حدث خطأ في التحليل")


# =========================
# تشغيل البوت
# =========================
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🚀 Bot Running...")
    app.run_polling()


if __name__ == "__main__":
    main()
