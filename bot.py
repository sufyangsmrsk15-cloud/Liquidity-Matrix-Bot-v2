# ===== Whale Footprint Telegram Bot Files (Auto-Hybrid) =====
# This document contains three files you can copy to your project:
# 1) bot.py  (main program)
# 2) requirements.txt
# 3) Procfile

# ===== FILE: bot.py =====
"""
Whale Footprint Auto-Hybrid Telegram Bot (Python 3.10)
- Uses Bitget public endpoints to fetch SPOT + FUTURES data (passphrase optional)
- Detects footprint (stop-hunt) candles, checks Volume Delta approximation, OI spike,
  CVD confirmation and CoinGlass public liquidation heatmap as an extra filter
- Sends alerts to Telegram (no live order placement)

Deploy: Render.com (Procfile provided). Set environment variables in Render secrets.
"""

from __future__ import annotations
import os
import time
import math
import requests
import json
from collections import defaultdict
from typing import List, Dict, Tuple, Optional
from telegram import Bot
from dotenv import load_dotenv

# Load env (for local testing)
load_dotenv()

# ---------------------- CONFIG / ENV ----------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BITGET_API_KEY = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET = os.getenv("BITGET_API_SECRET", "")
# passphrase optional - if not provided we still use public futures endpoints
BITGET_PASSPHRASE = os.getenv("BITGET_PASSPHRASE", "")

# Symbols: allow separate spot/futures symbols if needed
SPOT_SYMBOL = os.getenv("SPOT_SYMBOL", "BTCUSDT")
FUTURES_SYMBOL = os.getenv("FUTURES_SYMBOL", "BTCUSDT")
INTERVAL = os.getenv("INTERVAL", "15m")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "60"))

# Strategy params (adjustable via env too)
RR = int(os.getenv("RR", "4"))
VOL_SMA_LEN = int(os.getenv("VOL_SMA_LEN", "20"))
VOL_MULT = float(os.getenv("VOL_MULT", "2.2"))
WICK_RATIO = float(os.getenv("WICK_RATIO", "0.35"))
USE_NY_SESSION = os.getenv("USE_NY_SESSION", "true").lower() in ("1","true","yes")
NY_START_UTC = int(os.getenv("NY_START_UTC", "12"))
NY_END_UTC = int(os.getenv("NY_END_UTC", "17"))
LIQ_THRESHOLD = float(os.getenv("LIQ_THRESHOLD", "10000"))
MIN_RANGE = float(os.getenv("MIN_RANGE", "0"))  # optional

BITGET_PUBLIC_REST = "https://api.bitget.com"
COINGLASS_PUBLIC = "https://open-api.coinglass.com"
COINGLASS_PUBLIC_V2 = "https://open-api.coinglass.com/public/v2"

bot = Bot(token=TELEGRAM_TOKEN) if TELEGRAM_TOKEN else None

# ---------------------- UTILITIES ----------------------

def _interval_to_seconds(interval: str) -> int:
    if interval.endswith("m"):
        return int(interval[:-1]) * 60
    if interval.endswith("h"):
        return int(interval[:-1]) * 3600
    if interval.endswith("d"):
        return int(interval[:-1]) * 86400
    return 900


def sma(values: List[float], length: int) -> List[float]:
    out: List[float] = []
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= length:
            s -= values[i - length]
            out.append(s / length)
        else:
            out.append(s / (i + 1))
    return out


def compute_cvd(candles: List[Dict]) -> List[float]:
    cvd: List[float] = []
    s = 0.0
    for c in candles:
        delta = 0.0
        if c["close"] > c["open"]:
            delta = c["volume"]
        elif c["close"] < c["open"]:
            delta = -c["volume"]
        s += delta
        cvd.append(s)
    return cvd

# ---------------------- BITGET ENDPOINTS (public) ----------------------

def fetch_futures_klines(symbol: str, interval: str, limit: int = 200) -> List[Dict]:
    """Fetch futures (mix) klines from Bitget public mix endpoint. Returns ascending list."""
    url = f"{BITGET_PUBLIC_REST}/api/mix/v1/market/candles"
    params = {"symbol": symbol, "granularity": _interval_to_seconds(interval), "limit": limit}
    try:
        r = requests.get(url, params=params, timeout=12)
        if r.status_code == 200:
            data = r.json().get("data") or []
            parsed: List[Dict] = []
            for it in data:
                if isinstance(it, list) and len(it) >= 6:
                    parsed.append({
                        "time": int(float(it[0])),
                        "open": float(it[1]),
                        "high": float(it[2]),
                        "low": float(it[3]),
                        "close": float(it[4]),
                        "volume": float(it[5]),
                    })
            return list(reversed(parsed))
        else:
            print("futures klines status", r.status_code, r.text[:200])
    except Exception as e:
        print("fetch_futures_klines err", e)
    return []


def fetch_spot_klines(symbol: str, interval: str, limit: int = 200) -> List[Dict]:
    """Fetch spot klines. Tries common Bitget spot endpoint format."""
    # Bitget spot endpoint sometimes uses instrument id format, try a couple methods
    url1 = f"{BITGET_PUBLIC_REST}/api/spot/v3/instruments/{symbol}/candles"
    params = {"granularity": _interval_to_seconds(interval), "limit": limit}
    try:
        r = requests.get(url1, params=params, timeout=10)
        if r.status_code == 200:
            data = r.json() or []
            parsed = []
            for it in data:
                if isinstance(it, list) and len(it) >= 6:
                    parsed.append({
                        "time": int(float(it[0])),
                        "open": float(it[1]),
                        "high": float(it[2]),
                        "low": float(it[3]),
                        "close": float(it[4]),
                        "volume": float(it[5]),
                    })
            return list(reversed(parsed))
        else:
            # fallback: try mix endpoint (sometimes works for symbols)
            return fetch_futures_klines(symbol, interval, limit)
    except Exception as e:
        print("fetch_spot_klines err", e)
    return []


def fetch_futures_open_interest(symbol: str) -> float:
    try:
        url = f"{BITGET_PUBLIC_REST}/api/mix/v1/market/openInterest?symbol={symbol}"
        r = requests.get(url, timeout=8)
        if r.status_code == 200:
            j = r.json()
            data = j.get("data") or {}
            for k in ("openInterest", "open_interest", "oi"):
                if k in data:
                    return float(data[k])
    except Exception as e:
        print("fetch_futures_open_interest err", e)
    return 0.0

# ---------------------- FOOTPRINT DETECTION ----------------------

def detect_stop_hunt_and_footprint(candles: List[Dict]) -> Tuple[List[int], List[int], List[float]]:
    volumes = [c["volume"] for c in candles]
    vol_sma = sma(volumes, VOL_SMA_LEN)
    cvd = compute_cvd(candles)

    buy_idxs: List[int] = []
    sell_idxs: List[int] = []
    for i, c in enumerate(candles):
        high, low, op, cl = c["high"], c["low"], c["open"], c["close"]
        rng = high - low
        if rng <= 0:
            continue
        lower_wick = min(op, cl) - low
        upper_wick = high - max(op, cl)
        lower_ratio = lower_wick / rng
        upper_ratio = upper_wick / rng
        vol_ok = True
        if i < len(vol_sma):
            vol_ok = c["volume"] > vol_sma[i] * VOL_MULT
        delta = c["volume"] if cl > op else (-c["volume"] if cl < op else 0)

        if vol_ok and lower_ratio >= WICK_RATIO and delta > 0 and (rng >= MIN_RANGE or MIN_RANGE == 0):
            buy_idxs.append(i)
        if vol_ok and upper_ratio >= WICK_RATIO and delta < 0 and (rng >= MIN_RANGE or MIN_RANGE == 0):
            sell_idxs.append(i)
    return buy_idxs, sell_idxs, cvd


def is_in_ny_session(ts: int) -> bool:
    utc_hour = (ts // 1000) // 3600 % 24
    return NY_START_UTC <= utc_hour < NY_END_UTC


def check_oi_spike(oi_hist: List[float], threshold_percent: float = 1.5) -> bool:
    if len(oi_hist) < 12:
        return False
    sma10 = sum(oi_hist[-12:-2]) / 10.0
    last = oi_hist[-1]
    if sma10 <= 0:
        return False
    return (last - sma10) / sma10 * 100.0 >= threshold_percent

# ---------------------- COINGLASS (public) ----------------------

def fetch_coinglass_liquidation_info(time_type: str = "h1", symbol: str = "BTC") -> Optional[dict]:
    url = f"{COINGLASS_PUBLIC_V2}/liquidation_info"
    params = {"time_type": time_type, "symbol": symbol}
    try:
        r = requests.get(url, params=params, timeout=8)
        if r.status_code == 200:
            return r.json()
        print("coinglass liq status", r.status_code)
    except Exception as e:
        print("fetch_coinglass_liquidation_info err", e)
    return None


def parse_coinglass_into_heatmap(liq_json: Optional[dict]) -> Dict[float, float]:
    buckets: Dict[float, float] = defaultdict(float)
    if not liq_json:
        return buckets
    try:
        data = liq_json.get("data") if isinstance(liq_json, dict) else None
        if not data:
            return buckets
        items = data.get("items") or data.get("list") or []
        for it in items:
            if not isinstance(it, dict):
                continue
            price = float(it.get("price", 0) or 0)
            amount = float(it.get("liquidation", 0) or 0)
            if price and amount:
                buckets[price] += amount
    except Exception as e:
        print("parse_coinglass_into_heatmap err", e)
    return dict(buckets)


def get_liquidation_heatmap(symbol: str = "BTC") -> Dict[float, float]:
    sym = symbol.replace("USDT", "")[:6]
    j = fetch_coinglass_liquidation_info(time_type="h1", symbol=sym)
    return parse_coinglass_into_heatmap(j)


def liquidation_mass_near(price: float, heatmap: Dict[float, float], window: float = 50.0) -> float:
    total = 0.0
    for p, amt in heatmap.items():
        if abs(p - price) <= window:
            total += amt
    return total

# ---------------------- TELEGRAM ALERT ----------------------

def send_alert(text: str) -> None:
    try:
        if bot and TELEGRAM_CHAT_ID:
            bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
            print("Alert sent:", text)
        else:
            print("Alert (no telegram configured):", text)
    except Exception as e:
        print("send_alert err", e)

# ---------------------- MAIN LOOP ----------------------

def main_loop() -> None:
    print("Starting Whale Footprint Auto-Hybrid Bot — polling", POLL_SECONDS, "seconds")
    oi_hist: List[float] = []
    recent_signals = set()

    while True:
        try:
            # fetch futures + spot candles
            fut_candles = fetch_futures_klines(FUTURES_SYMBOL, INTERVAL, limit=300)
            spot_candles = fetch_spot_klines(SPOT_SYMBOL, INTERVAL, limit=300)

            # prefer futures candles for footprint detection (richer for derivatives)
            candles = fut_candles or spot_candles
            if not candles:
                print("No candles available; sleeping")
                time.sleep(POLL_SECONDS)
                continue

            buy_idxs, sell_idxs, cvd = detect_stop_hunt_and_footprint(candles)

            # fetch open interest from futures (if available)
            oi = fetch_futures_open_interest(FUTURES_SYMBOL)
            if oi and oi > 0:
                oi_hist.append(oi)
                if len(oi_hist) > 500:
                    oi_hist = oi_hist[-500:]

            # get coinglass heatmap (best-effort public)
            heatmap = get_liquidation_heatmap(FUTURES_SYMBOL)

            # evaluate recent footprint candidates
            for idx in buy_idxs + sell_idxs:
                c = candles[idx]
                sig_key = (FUTURES_SYMBOL, c["time"])
                if sig_key in recent_signals:
                    continue
                # require candidate to be among last 3 candles
                if idx < len(candles) - 4:
                    continue

                # session filter
                if USE_NY_SESSION and not is_in_ny_session(c["time"] * 1000):
                    continue

                side = "BUY" if idx in buy_idxs else "SELL"
                oi_spike = check_oi_spike(oi_hist)
                cvd_after = cvd[idx:]
                cvd_rising = len(cvd_after) >= 2 and cvd_after[-1] > cvd_after[0]
                liq_mass = liquidation_mass_near(c["close"], heatmap, window=max(50.0, c["close"]*0.005))

                # confirmations per your strategy
                buy_ok = side == "BUY" and oi_spike and cvd_rising and liq_mass > LIQ_THRESHOLD
                sell_ok = side == "SELL" and oi_spike and (not cvd_rising) and liq_mass > LIQ_THRESHOLD

                if buy_ok or sell_ok:
                    side_str = "BUY" if buy_ok else "SELL"
                    sl = round(c["low"], 2) if buy_ok else round(c["high"], 2)
                    # compute a simple TP using RR and current price
                    entry = round(c["close"], 2)
                    tp = round(entry + (entry - sl) * RR, 2) if buy_ok else round(entry - (sl - entry) * RR, 2)

                    msg = (
                        f"🐋 WHALE FOOTPRINT {side_str} - {FUTURES_SYMBOL}
"
                        f"Entry: {entry}  SL: {sl}  TP: {tp}  RR:1:{RR}
"
                        f"OI_spike:{oi_spike}  CVD_rising:{cvd_rising}  LiqMass:{round(liq_mass,2)}"
                    )
                    send_alert(msg)
                    recent_signals.add(sig_key)

            # prune recent_signals to avoid memory growth
            if len(recent_signals) > 2000:
                recent_signals = set(list(recent_signals)[-1000:])

        except Exception as e:
            print("main loop err", e)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main_loop()

# ===== FILE: requirements.txt =====
# Keep versions compatible with Python 3.10
python-telegram-bot==13.15
requests==2.31.0
python-dotenv==1.0.0

# ===== FILE: Procfile =====
worker: python bot.py

# ===== FILE: .env.example =====
# Copy to .env locally or set Render environment variables
# TELEGRAM_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
# TELEGRAM_CHAT_ID=@yourchannel or 123456789
# BITGET_API_KEY=your_api_key
# BITGET_API_SECRET=your_secret
# BITGET_PASSPHRASE=your_passphrase (optional)
# SPOT_SYMBOL=BTCUSDT
# FUTURES_SYMBOL=BTCUSDT
# INTERVAL=15m
# POLL_SECONDS=60
# VOL_SMA_LEN=20
# VOL_MULT=2.2
# WICK_RATIO=0.35
# LIQ_THRESHOLD=10000

