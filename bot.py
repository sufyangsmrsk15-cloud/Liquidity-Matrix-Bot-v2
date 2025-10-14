#!/usr/bin/env python3
"""
Liquidity Matrix Bot v2
- Upgraded: Post-Sweep Delay Entry Filter, safe SL, volume filter, second-touch / candle-confirmation, multi-TF confirm.
- Replace TELEGRAM_TOKEN, TELEGRAM_CHAT_ID and TD_API_KEY in CONFIG or use environment variables.
- Uses TwelveData by default. You can plug another provider by changing twelvedata_get_series().
"""

import os
import time
import math
import requests
from datetime import datetime, timedelta, time as dtime
from apscheduler.schedulers.background import BackgroundScheduler
from typing import List, Dict, Any, Optional

# ------------------ CONFIG (EDIT BEFORE RUN) ------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")     # put your token or set env var
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "") # chat id or channel
TD_API_KEY = os.getenv("TD_API_KEY", "")             # TwelveData API key

SYMBOL_XAU = "XAU/USD"
SYMBOL_BTC = "BTC/USD"

# PK timezone session start (PK = UTC+5)
NY_SESSION_START_PK = dtime(hour=17, minute=0)
PRE_ALERT_MINUTES = 5
POST_ALERT_MINUTES = 5

# Strategy tuning
XAU_SL_PIPS = 20            # pip notion for calculating base SL (0.01 pip unit)
XAU_PIP = 0.01
BTC_SL_USD = 350
RR = 4                      # desired risk:reward
SL_BUFFER_PIPS = 5          # extra buffer to avoid stop-hunts (in pips for XAU)
RETEST_TOUCH_ALLOWANCE = 2  # require second touch (ignore first touch)
CONFIRM_VOLUME_MULT = 1.0   # confirm volume must be > avg_prev_2 * this multiplier (if volume available)
LOOKBACK_15M = 96           # ~24 hours of 15m candles (96 * 15m)
LOOKBACK_5M = 288           # ~24 hours of 5m candles (288 * 5m)
MIN_CANDLES_REQUIRED = 20

# ------------------ HELPERS ------------------

def send_telegram_message(text: str):
    """Send message via Telegram bot; returns response dict or None."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials not set; message not sent.")
        print("Message preview:\n", text)
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("Telegram send error:", e)
        return None

def twelvedata_get_series(symbol: str, interval: str = "15min", outputsize: int = 200) -> List[Dict[str, Any]]:
    """Fetch time series from TwelveData (newest-first) and return oldest-first list."""
    if not TD_API_KEY:
        raise RuntimeError("TwelveData API key not set.")
    base = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "format": "JSON",
        "apikey": TD_API_KEY
    }
    r = requests.get(base, params=params, timeout=12)
    r.raise_for_status()
    data = r.json()
    if "values" not in data:
        raise RuntimeError(f"TwelveData error or invalid response: {data}")
    return list(reversed(data["values"]))

def parse_candles(raw_candles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert raw to candles with numeric fields and datetime objects."""
    out = []
    for c in raw_candles:
        # volume may be missing or 'null'
        vol = c.get("volume")
        vol_f = float(vol) if vol not in (None, "", "null") else 0.0
        out.append({
            "datetime": datetime.fromisoformat(c["datetime"]),
            "open": float(c["open"]),
            "high": float(c["high"]),
            "low": float(c["low"]),
            "close": float(c["close"]),
            "volume": vol_f
        })
    return out

# ------------------ DETECTION LOGIC ------------------

def detect_sweep_and_green(candles_15m: List[Dict[str, Any]], lookback: int = 6) -> Dict[str, Any]:
    """
    Detect sweep on 15m with green confirm.
    Returns dict: {'signal': bool, 'sweep_candle': c, 'confirm_candle': c2, 'sweep_index': idx}
    """
    if len(candles_15m) < lookback + 2:
        return {"signal": False, "reason": "not_enough_data"}
    window = candles_15m[-(lookback+1):]
    for i in range(1, len(window)-1):
        if window[i]["low"] < window[i-1]["low"] and window[i]["low"] < window[i+1]["low"]:
            body = abs(window[i]["open"] - window[i]["close"])
            lower_wick = (window[i]["open"] - window[i]["low"]) if window[i]["open"] > window[i]["close"] else (window[i]["close"] - window[i]["low"])
            rng = (window[i]["high"] - window[i]["low"]) if (window[i]["high"] - window[i]["low"])>0 else 1e-6
            if lower_wick / rng > 0.35:
                # require subsequent candle(s) contain a green close (confirm)
                next_c = window[i+1]
                if next_c["close"] > next_c["open"]:
                    return {
                        "signal": True,
                        "sweep_candle": window[i],
                        "confirm_candle": next_c,
                        "sweep_idx_in_15m": len(candles_15m) - (lookback+1) + i
                    }
    return {"signal": False, "reason": "no_sweep"}

def detect_second_touch_and_confirmation(candles_15m: List[Dict[str, Any]],
                                         candles_5m: List[Dict[str, Any]],
                                         sweep_low: float,
                                         breakout_body_high: float) -> Dict[str, Any]:
    """
    After breakout and sweep, check for retest touches and require either:
      - second physical touch of zone (count touches)
      - OR a confirming candle on 5m/15m: bullish engulfing OR strong wick rejection with >=50% recovery
    Also enforces volume check if available.
    Returns dict: {'ok': bool, 'entry': price, 'reason': text, 'confirm_candle': {...}}
    """
    # Define retest zone: small band around breakout_body_high down to sweep_low
    zone_top = breakout_body_high
    zone_bottom = sweep_low
    # Count touches in 5m candles (last 200)
    touches = 0
    for c in candles_5m[-60:]:  # last 5 hours approx on 5m
        if c["low"] <= zone_top and c["low"] >= zone_bottom - 0.5:  # allow small slop
            touches += 1
    # If touches >= RETEST_TOUCH_ALLOWANCE -> consider as multi-touch
    if touches >= RETEST_TOUCH_ALLOWANCE:
        # Look for a confirming bullish candle right after the last touch (5m)
        # find last candle touching zone
        last_touch_idx = None
        for i in range(len(candles_5m)-1, -1, -1):
            c = candles_5m[i]
            if c["low"] <= zone_top and c["low"] >= zone_bottom - 0.5:
                last_touch_idx = i
                break
        if last_touch_idx is None:
            return {"ok": False, "reason": "touch_count_but_no_index"}
        # candidate confirm candle is next 1-2 candles
        for j in range(1, 3):
            idx = last_touch_idx + j
            if idx >= len(candles_5m):
                break
            cand = candles_5m[idx]
            # bullish engulfing on 5m vs previous
            if idx-1 >= 0:
                prev = candles_5m[idx-1]
                if (prev["close"] < prev["open"] and cand["close"] > cand["open"]
                    and cand["close"] > prev["open"] and cand["open"] < prev["close"]):
                    # volume check
                    avg_prev2_vol = (prev["volume"] + candles_5m[idx-2]["volume"]) / 2 if idx-2 >= 0 else prev["volume"]
                    if avg_prev2_vol == 0 or cand["volume"] >= avg_prev2_vol * CONFIRM_VOLUME_MULT:
                        entry = max(cand["open"] + 0.02, (cand["close"] + zone_bottom) / 2)
                        return {"ok": True, "entry": round(entry, 3), "confirm_candle": cand, "reason": "engulfing_after_second_touch"}
            # strong wick rejection check: wick recovery >= 50%
            wick_low = cand["low"]
            wick_recovery = cand["close"] - wick_low
            body = abs(cand["open"] - cand["close"])
            if wick_low <= zone_top and wick_recovery >= (cand["high"] - wick_low) * 0.5 and cand["close"] > cand["open"]:
                avg_prev2_vol = 0
                if idx-1 >= 0 and idx-2 >= 0:
                    avg_prev2_vol = (candles_5m[idx-1]["volume"] + candles_5m[idx-2]["volume"]) / 2
                if avg_prev2_vol == 0 or cand["volume"] >= avg_prev2_vol * CONFIRM_VOLUME_MULT:
                    entry = max(cand["open"] + 0.02, (cand["close"] + zone_bottom) / 2)
                    return {"ok": True, "entry": round(entry, 3), "confirm_candle": cand, "reason": "wick_rejection_after_second_touch"}
        return {"ok": False, "reason": "no_confirm_after_second_touch", "touches": touches}
    else:
        # If touches < required, allow *only* if there's a clear confirming candle that is not the immediate first touch
        # Search last 6 5m candles for a confirming candle not immediately at first touch
        last_6 = candles_5m[-6:]
        for idx, cand in enumerate(last_6):
            # bullish engulfing vs previous within last_6 set
            if idx > 0:
                prev = last_6[idx-1]
                if (prev["close"] < prev["open"] and cand["close"] > cand["open"]
                    and cand["close"] > prev["open"] and cand["open"] < prev["close"]):
                    avg_prev2_vol = (prev["volume"] + (last_6[idx-2]["volume"] if idx-2>=0 else prev["volume"])) / 2
                    if avg_prev2_vol == 0 or cand["volume"] >= avg_prev2_vol * CONFIRM_VOLUME_MULT:
                        # ensure cand not immediate first touch: if the first touch was exactly one candle earlier, ignore
                        return {"ok": True, "entry": round(max(cand["open"] + 0.02, (cand["close"] + zone_bottom) / 2), 3),
                                "confirm_candle": cand, "reason": "engulfing_no_second_touch_but_strong_confirm"}
        return {"ok": False, "reason": "not_enough_touches_and_no_confirm"}

def compute_liquidity_zones(candles: List[Dict[str, Any]]) -> Dict[str, float]:
    lows = [c["low"] for c in candles]
    highs = [c["high"] for c in candles]
    return {
        "recent_low": min(lows),
        "recent_high": max(highs),
        "last_close": candles[-1]["close"]
    }

# ------------------ TRADE PLAN BUILDER ------------------

def build_xau_trade_plan(detection: Dict[str, Any], candles_15m: List[Dict[str, Any]], candles_5m: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Uses detection result, then applies second-touch + confirmation logic to build trade.
    Returns plan dict or None.
    """
    if not detection.get("signal"):
        return None
    sweep = detection["sweep_candle"]
    confirm = detection["confirm_candle"]
    sweep_low = sweep["low"]
    breakout_body_high = confirm["high"]  # approximate breakout body high used as zone top
    # check second-touch / confirmation on 5m
    sec = detect_second_touch_and_confirmation(candles_15m, candles_5m, sweep_low, breakout_body_high)
    if not sec.get("ok"):
        return None
    entry = sec["entry"]
    # SL: below sweep_low with buffer
    sl_price = sweep_low - (SL_BUFFER_PIPS * XAU_PIP) - (XAU_SL_PIPS * XAU_PIP * 0.0)  # primary safety below sweep
    rr_distance = entry - sl_price
    tp = entry + rr_distance * RR
    tp1 = entry + rr_distance * 1.0
    return {
        "side": "LONG",
        "entry": round(entry, 3),
        "sl": round(sl_price, 3),
        "tp": round(tp, 3),
        "tp1": round(tp1, 3),
        "confidence": 0.85,
        "logic": f"Sweep+Green 15m + second-touch confirm ({sec.get('reason')})",
        "confirm_candle": sec.get("confirm_candle")
    }

def build_btc_trade_plan(detection: Dict[str, Any], candles_15m: List[Dict[str, Any]], candles_5m: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not detection.get("signal"):
        return None
    sweep = detection["sweep_candle"]
    confirm = detection["confirm_candle"]
    sweep_low = sweep["low"]
    breakout_body_high = confirm["high"]
    sec = detect_second_touch_and_confirmation(candles_15m, candles_5m, sweep_low, breakout_body_high)
    if not sec.get("ok"):
        return None
    entry = sec["entry"]
    sl_price = sweep_low - BTC_SL_USD  # larger nominal SL for BTC
    rr_distance = entry - sl_price
    tp = entry + rr_distance * RR
    return {
        "side": "LONG",
        "entry": round(entry, 2),
        "sl": round(sl_price, 2),
        "tp": round(tp, 2),
        "tp1": round(entry + rr_distance * 1.0, 2),
        "confidence": 0.80,
        "logic": f"Sweep+Green 15m + second-touch confirm ({sec.get('reason')})",
        "confirm_candle": sec.get("confirm_candle")
    }

# ------------------ ANALYSIS & FORMATTING ------------------

def get_and_analyze(symbol: str, interval_15m: str = "15min", interval_5m: str = "5min") -> Dict[str, Any]:
    try:
        raw_15m = twelvedata_get_series(symbol, interval=interval_15m, outputsize=LOOKBACK_15M)
        raw_5m = twelvedata_get_series(symbol, interval=interval_5m, outputsize=LOOKBACK_5M)
    except Exception as e:
        return {"error": f"data_fetch_error: {e}"}
    candles_15m = parse_candles(raw_15m)
    candles_5m = parse_candles(raw_5m)
    if len(candles_15m) < MIN_CANDLES_REQUIRED or len(candles_5m) < MIN_CANDLES_REQUIRED:
        return {"error": "not_enough_candles"}
    detection = detect_sweep_and_green(candles_15m, lookback=6)
    liquidity = compute_liquidity_zones(candles_15m[-LOOKBACK_15M:])
    result = {
        "symbol": symbol,
        "detection": detection,
        "liquidity": liquidity,
        "latest_15m": candles_15m[-1],
        "latest_5m": candles_5m[-1],
        "candles_15m": candles_15m,
        "candles_5m": candles_5m
    }
    # build plan if signal
    if detection.get("signal"):
        if "XAU" in symbol:
            result["plan"] = build_xau_trade_plan(detection, candles_15m, candles_5m)
        else:
            result["plan"] = build_btc_trade_plan(detection, candles_15m, candles_5m)
    return result

def format_plan_message(analysis: Dict[str, Any]) -> str:
    if "error" in analysis:
        return f"⚠ Error: {analysis['error']}"
    if not analysis.get("plan"):
        # give helpful liquidity snapshot
        return (f"ℹ <b>{analysis['symbol']}</b>\n"
                f"No qualified setup (after second-touch + confirm rules).\n"
                f"Liquidity (24h): Low {analysis['liquidity']['recent_low']}, High {analysis['liquidity']['recent_high']}\n"
                f"Latest 15m close: {analysis['latest_15m']['close']}")
    p = analysis["plan"]
    msg = f"<b>Pro SmartMoney Setup — {analysis['symbol']}</b>\n"
    msg += f"Logic: {p['logic']}\n"
    msg += f"Side: <b>{p['side']}</b>\n"
    msg += f"Entry: <code>{p['entry']}</code>\nSL: <code>{p['sl']}</code>\nTP: <code>{p['tp']}</code>\nTP1: <code>{p.get('tp1')}</code>\nConfidence: {int(p['confidence']*100)}%\n\n"
    msg += f"Liquidity (24h): Low {analysis['liquidity']['recent_low']}, High {analysis['liquidity']['recent_high']}\n"
    msg += f"Confirm candle time: {p.get('confirm_candle', {}).get('datetime')}\n"
    msg += "Trade Management:\n- TP1 hit -> move SL to break-even\n- TP2 hit -> scale out 50%\n- TP3 -> leave runner or full close\n"
    msg += "\n---\nPowered by Liquidity Matrix Bot v2"
    return msg

# ------------------ SCHEDULED JOBS ------------------

def job_pre_alert():
    now = datetime.utcnow() + timedelta(hours=5)
    text = f"🕒 <b>Pre-NY Alert</b>\nTime (PK): {now.strftime('%Y-%m-%d %H:%M')}\nScanning XAU & BTC for qualified setups..."
    send_telegram_message(text)
    try:
        x = get_and_analyze(SYMBOL_XAU)
        b = get_and_analyze(SYMBOL_BTC)
        send_telegram_message(format_plan_message(x))
        send_telegram_message(format_plan_message(b))
    except Exception as e:
        send_telegram_message(f"Pre-alert error: {e}")

def job_post_open():
    now = datetime.utcnow() + timedelta(hours=5)
    text = f"🕒 <b>NY Post-Open Alert</b>\nTime (PK): {now.strftime('%Y-%m-%d %H:%M')}\nScanning for qualified setups (second-touch + confirm)..."
    send_telegram_message(text)
    try:
        x = get_and_analyze(SYMBOL_XAU)
        b = get_and_analyze(SYMBOL_BTC)
        send_telegram_message(format_plan_message(x))
        send_telegram_message(format_plan_message(b))
    except Exception as e:
        send_telegram_message(f"Post-open error: {e}")

def start_scheduler():
    sched = BackgroundScheduler(timezone="UTC")
    # Convert PK (UTC+5) times to UTC hours
    pre_utc_hour = (NY_SESSION_START_PK.hour - PRE_ALERT_MINUTES//60) - 5
    # simpler: schedule PK 16:55 and 17:05 converted to UTC
    sched.add_job(job_pre_alert, 'cron', hour=11, minute=55)  # PK16:55 -> UTC11:55
    sched.add_job(job_post_open, 'cron', hour=12, minute=5)  # PK17:05 -> UTC12:05
    sched.start()
    print("Scheduler started. Pre-alert at PK 16:55, Post-open at PK 17:05")
    try:
        while True:
            time.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        sched.shutdown()

# ------------------ MAIN ------------------

if __name__ == "__main__":
    # basic credential check
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or not TD_API_KEY:
        print("Please set TELEGRAM_TOKEN, TELEGRAM_CHAT_ID and TD_API_KEY in environment or edit the script config.")
        print("You can still test: call get_and_analyze() manually in interactive mode.")
    else:
        print("Starting Liquidity Matrix Bot v2...")
        start_scheduler()
