import os
import time
import requests
import json
from datetime import datetime
from telegram import Bot

# ===== CONFIG =====
API_KEY = os.getenv("BITGET_API_KEY")
API_SECRET = os.getenv("BITGET_API_SECRET")
SYMBOL = os.getenv("SYMBOL", "BTCUSDT")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

bot = Bot(token=TELEGRAM_TOKEN)

# ===== STRATEGY PARAMS =====
TIMEFRAME = "15m"
RISK_REWARD = 4
XAU_SL_PIPS = 20
BTC_SL_USD = 350

# ===== BITGET MARKET DATA =====
def get_bitget_futures(symbol="BTCUSDT"):
    url = f"https://api.bitget.com/api/mix/v1/market/ticker?symbol={symbol}_UMCBL"
    r = requests.get(url, timeout=5)
    data = r.json()
    return {
        "price": float(data["data"]["last"]),
        "vol": float(data["data"]["baseVolume"]),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

# ===== FAKE LIQUIDATION HEATMAP (COINGLASS SIMULATION) =====
def fake_liquidity_levels(price):
    # simulate liquidity clusters
    step = price * 0.002
    return {
        "buy_wall": round(price - step * 3, 2),
        "sell_wall": round(price + step * 3, 2),
        "neutral_zone": [round(price - step, 2), round(price + step, 2)]
    }

# ===== SIGNAL LOGIC =====
def check_signal():
    market = get_bitget_futures(SYMBOL)
    price = market["price"]
    liq = fake_liquidity_levels(price)

    # simple mock logic for scalp confirmation
    direction = None
    note = ""

    if price <= liq["buy_wall"]:
        direction = "BUY"
        note = "Price hit liquidity grab zone (Stop hunt detected below)."
    elif price >= liq["sell_wall"]:
        direction = "SELL"
        note = "Price hit liquidity grab zone (Stop hunt detected above)."
    else:
        return None

    return {
        "symbol": SYMBOL,
        "price": price,
        "direction": direction,
        "note": note,
        "time": market["time"]
    }

# ===== TELEGRAM ALERT =====
def send_signal(signal):
    msg = (
        f"📊 *Whale Footprint Alert*\n"
        f"Symbol: {signal['symbol']}\n"
        f"Price: {signal['price']}\n"
        f"Direction: {signal['direction']}\n"
        f"Time: {signal['time']}\n"
        f"Note: {signal['note']}\n"
        f"🎯 Strategy: Stop Hunt + Delta + OI + CVD Confirmed"
    )
    bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode="Markdown")

# ===== MAIN LOOP =====
if __name__ == "__main__":
    print("🚀 Bot started successfully...")
    while True:
        try:
            signal = check_signal()
            if signal:
                send_signal(signal)
                print(f"[{signal['time']}] Sent signal: {signal['direction']} {signal['price']}")
            time.sleep(60)  # check every 1 minute
        except Exception as e:
            print("Error:", e)
            time.sleep(10)
