"""
==========================================================================
NIFTY 200 INTRADAY SCANNER — Angel One (SmartAPI) + Telegram Alerts
==========================================================================
તમારે ફક્ત નીચે "STEP 1: CONFIGURATION" સેક્શનમાં જ ફેરફાર કરવાનો છે.
બાકીના કોડમાં કંઈ બદલવાની જરૂર નથી.
==========================================================================
"""

import os
import time
import json
import requests
import pyotp
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from SmartApi import SmartConnect

# ==========================================================================
# STEP 1: CONFIGURATION (GitHub Secrets માંથી સુરક્ષિત રીતે વાંચશે)
# ==========================================================================

# ---- (A) ANGEL ONE (SmartAPI) LOGIN DETAILS ----
API_KEY        = os.getenv("ANGEL_API_KEY")
CLIENT_ID      = os.getenv("ANGEL_CLIENT_ID")
PASSWORD       = os.getenv("ANGEL_PASSWORD")
TOTP_SECRET    = os.getenv("ANGEL_TOTP_SECRET")

# ---- (B) TELEGRAM BOT DETAILS ----
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

# ---- (C) STRATEGY SETTINGS ----
ENTRY_START_TIME   = "09:20"
ENTRY_CUTOFF_TIME  = "14:00"
GAP_FILTER_PCT     = 5.0
BIG_GAP_ALERT_PCT  = 2.0
MIN_SCORE          = 80
MIN_RS_STRENGTH    = 100
MAX_VCP            = 0.45
RSI_LOW, RSI_HIGH  = 50, 75
INITIAL_SL_PCT     = 1.0
TARGET_PCT         = 1.0
BREAKEVEN_TRIGGER  = 0.7
MAX_LOSS_TRADES    = 2
MIN_BUYER_SIDE_PCT = 54      # <-- Market Positive / Buyer Side % ચેક (તમારો મૂળ નિયમ)
NIFTY200_LIST_FILE = "nifty200_symbols.json"
API_CALL_DELAY     = 0.4     # <-- FIX #4: દરેક API કોલ વચ્ચે ગેપ (Rate Limit ટાળવા)

# ==========================================================================
# STEP 2: HELPER FUNCTIONS
# ==========================================================================

def send_telegram_msg(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            print(f"Telegram send failed: {r.text}")
    except Exception as e:
        print(f"Telegram Error: {e}")


def angel_login():
    smart_api = SmartConnect(api_key=API_KEY)
    try:
        totp = pyotp.TOTP(TOTP_SECRET).now()
        session = smart_api.generateSession(CLIENT_ID, PASSWORD, totp)
        if not session.get("status"):
            send_telegram_msg(f"❌ *Login Failed:* {session.get('message')}")
            raise SystemExit("Login failed - check credentials/TOTP secret")
        send_telegram_msg("✅ *Angel One Login Successful.* Scanner starting...")
        return smart_api
    except Exception as e:
        send_telegram_msg(f"❌ *Login Error:* {e}")
        raise


def load_nifty200_list():
    """
    FIX: JSON FORMAT — nifty200_symbols.json ફાઈલ EXACT આ ફોર્મેટમાં જ હોવી
    જોઈએ (list of objects, દરેકમાં "symbol" અને "token" key ફરજિયાત):

        [
          {"symbol": "RELIANCE-EQ", "token": "2885"},
          {"symbol": "TCS-EQ",      "token": "11536"},
          {"symbol": "HDFCBANK-EQ", "token": "1333"}
        ]

    સામાન્ય ભૂલો જે JSON Decode Error આપે:
      - છેલ્લા item પછી extra comma ",": ખોટું  ->  {...},  {...},  ]
      - Single quotes '...' વાપરવા (JSON માં double quotes " જ ચાલે)
      - Python True/False/None વાપરવા (JSON માં true/false/null)
    """
    try:
        with open(NIFTY200_LIST_FILE, "r") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        send_telegram_msg(
            f"❌ *JSON Format Error in {NIFTY200_LIST_FILE}:*\n{e}\n\n"
            f"ફોર્મેટ ચેક કરો: [{{\"symbol\": \"RELIANCE-EQ\", \"token\": \"2885\"}}, ...]"
        )
        return []
    except Exception as e:
        send_telegram_msg(f"⚠️ *Nifty200 list load error:* {e}")
        return []

    # દરેક entry માં "symbol" અને "token" બંને છે કે નહીં ચેક
    valid = [s for s in data if isinstance(s, dict) and "symbol" in s and "token" in s]
    if len(valid) != len(data):
        send_telegram_msg(
            f"⚠️ *{len(data) - len(valid)} entries માં 'symbol' અથવા 'token' key ખૂટે છે — skip કર્યા.*"
        )
    return valid


# --------------------------------------------------------------------------
# FIX #1: DYNAMIC VOLUME THRESHOLD (પહેલા આ MIN_VOLUME_MULT = 1.0 ફિક્સ હતું,
# જેના કારણે સવારે 9:20-11:00 વચ્ચે કોઈ સ્ટોક પાસ જ ન થાય, કારણ કે એટલા ઓછા
# સમયમાં "આખા દિવસ જેટલું" (100%) વોલ્યુમ કોઈ સ્ટોકમાં ન આવે.
# હવે સમય પ્રમાણે threshold બદલાય છે.
# --------------------------------------------------------------------------
def get_volume_threshold(current_time: datetime):
    hour, minute = current_time.hour, current_time.minute
    if (hour, minute) < (11, 0):
        return 0.3
    elif (hour, minute) < (12, 0):
        return 0.5
    else:
        return 1.0


# --------------------------------------------------------------------------
# FIX #2: CIRCUIT LIMIT CHECK — upper/lower circuit પર freeze થયેલો સ્ટોક
# BUY SIGNAL તરીકે ન જાય એ માટે. Angel One ના "Full" mode quote માંથી
# upperCircuitLimit / lowerCircuitLimit મળે છે.
# --------------------------------------------------------------------------
def get_market_quote(smart_api, symbol, token):
    """
    LTP + upper/lower circuit limits + Buy/Sell quantity — એક જ કોલમાં (FULL mode).
    FIX (Buyer Side %): Angel One ના FULL quote માં "totBuyQuan" અને "totSellQuan"
    (કુલ પેન્ડિંગ Buy/Sell ઓર્ડર ક્વોન્ટિટી, 5-level market depth પરથી) આવે છે —
    "Market Positive / Buyer Side %" નિયમ માટે એ જ વાપર્યું છે.
    """
    try:
        params = {"mode": "FULL", "exchangeTokens": {"NSE": [token]}}
        data = smart_api.getMarketData(**params) if hasattr(smart_api, "getMarketData") else None
        if data and data.get("status"):
            row = data["data"]["fetched"][0]
            buy_qty = float(row.get("totBuyQuan", 0))
            sell_qty = float(row.get("totSellQuan", 0))
            total_qty = buy_qty + sell_qty
            buyer_pct = (buy_qty / total_qty * 100) if total_qty > 0 else None
            return {
                "ltp": float(row.get("ltp", 0)),
                "upper_circuit": float(row.get("upperCircuit", 0)),
                "lower_circuit": float(row.get("lowerCircuit", 0)),
                "buyer_pct": buyer_pct,
            }
    except Exception as e:
        print(f"Market quote error ({symbol}): {e}")
    return None


def is_in_circuit(quote: dict, buffer_pct: float = 0.15):
    """LTP, upper circuit ની બહુ નજીક (freeze) છે કે નહીં ચેક કરે છે"""
    if not quote or quote["upper_circuit"] <= 0:
        return False
    ltp, uc, lc = quote["ltp"], quote["upper_circuit"], quote["lower_circuit"]
    near_upper = ltp >= uc * (1 - buffer_pct / 100)
    near_lower = ltp <= lc * (1 + buffer_pct / 100)
    return near_upper or near_lower


def get_historical_data(smart_api, symbol_token, interval="ONE_DAY", from_date=None, to_date=None, days=260):
    if to_date is None:
        to_date = datetime.now()
    if from_date is None:
        from_date = to_date - timedelta(days=days)
    params = {
        "exchange": "NSE",
        "symboltoken": symbol_token,
        "interval": interval,
        "fromdate": from_date.strftime("%Y-%m-%d %H:%M"),
        "todate": to_date.strftime("%Y-%m-%d %H:%M"),
    }
    try:
        data = smart_api.getCandleData(params)
        time.sleep(API_CALL_DELAY)   # FIX #4: throttle
        if not data.get("status"):
            return None
        df = pd.DataFrame(data["data"], columns=["timestamp", "open", "high", "low", "close", "volume"])
        df[["open", "high", "low", "close", "volume"]] = df[["open", "high", "low", "close", "volume"]].astype(float)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df
    except Exception as e:
        print(f"Historical data error ({symbol_token}): {e}")
        return None


# --------------------------------------------------------------------------
# FIX #3: આજના જ candles — from_date ને આજની સવારે 09:15 પર fix કરેલો છે,
# અને પછી df ને પણ आजની तारीખ પ્રમાણે ફિલ્ટર કરેલો છે (ડબલ સેફ્ટી, જેથી
# ગઈકાલના candles ક્યારેય VWAP માં ભળે નહીં).
# --------------------------------------------------------------------------
def get_today_intraday_data(smart_api, symbol_token, interval="FIVE_MINUTE"):
    now = datetime.now()
    market_open_today = now.replace(hour=9, minute=15, second=0, microsecond=0)
    df = get_historical_data(smart_api, symbol_token, interval=interval,
                              from_date=market_open_today, to_date=now)
    if df is None or df.empty:
        return df
    df = df[df["timestamp"].dt.date == now.date()].reset_index(drop=True)
    return df


# --------------------------------------------------------------------------
# FIX: PREVIOUS CLOSE — તમે સાચું પકડ્યું કે ચાલુ બજારે daily candle નો
# iloc[-1] આજનો (અધૂરો) running candle હોઈ શકે, ગઈકાલનું close નહીં.
#
# પણ ફક્ત iloc[-2] વાપરવું bhi risky છે — કારણ કે Angel API ક્યારેક ONE_DAY
# ડેટામાં આજનો candle ADD જ ન કરે (ખાસ કરીને 09:20 એ, થોડી જ મિનિટ ડેટા
# બન્યો હોય ત્યારે). એ case માં iloc[-1] પોતે જ સાચું ગઈકાલનું close છે,
# અને iloc[-2] વાપરવાથી ઊલટું ખોટું (2 દિવસ જૂનું) close મળી જશે.
#
# એટલે "index count" પર આધાર રાખવાને બદલે "તારીખ" પર આધાર રાખેલો છે:
# ફક્ત આજ પહેલાંની તારીખના rows માંથી છેલ્લું close = સાચું prev_close.
# --------------------------------------------------------------------------
def get_previous_close(daily_df: pd.DataFrame, today_date):
    hist = daily_df[daily_df["timestamp"].dt.date < today_date]
    if hist.empty:
        return None
    return hist["close"].iloc[-1]


def calc_rsi(closes: pd.Series, period=14):
    delta = closes.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def calc_atr(df: pd.DataFrame, period=14):
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def calc_vwap(df_today: pd.DataFrame):
    typical_price = (df_today["high"] + df_today["low"] + df_today["close"]) / 3
    return (typical_price * df_today["volume"]).cumsum() / df_today["volume"].cumsum()


def has_corporate_action(symbol: str):
    """
    Placeholder — NSE પાસે ફ્રી/સ્થિર public API નથી. Paid data provider
    (Kite/TrueData) ના corporate-action API સાથે અહીં જોડાણ કરવું પડશે.
    """
    # TODO: તમારા data provider નું actual API call અહીં મૂકો
    return False


def score_sheet1(df: pd.DataFrame, live_ltp: float = None):
    """
    FIX (Gap-up score miss): 'df["close"].iloc[-1]' એ ambiguous છે —
    Angel API એ સવારે 09:20 એ ONE_DAY candles માં આજનું running candle
    add કર્યું છે કે નહીં, એ guarantee નથી. જો ન કર્યું હોય, તો gap-up
    સ્ટોકનો score ગઈકાલના (જૂના) ભાવ પરથી ખોટો ગણાય અને એ સ્ટોક
    શોર્ટલિસ્ટમાંથી છૂટી જાય.

    ઉકેલ: DMA/10-day-low તો historical daily df પરથી જ ગણાય (એ સાચું છે),
    પણ compare કરવા માટેનો "current price" હંમેશા અલગથી fetched **live LTP**
    વાપરવો — daily candle ના છેલ્લા row પર આધાર રાખવો નહીં.
    જો live_ltp ન મળે (API ફેલ થાય), તો જ fallback તરીકે df["close"].iloc[-1] વપરાય.
    """
    if df is None or len(df) < 200:
        return 0
    ltp = live_ltp if live_ltp is not None else df["close"].iloc[-1]
    dma10  = df["close"].rolling(10).mean().iloc[-1]
    dma20  = df["close"].rolling(20).mean().iloc[-1]
    dma50  = df["close"].rolling(50).mean().iloc[-1]
    dma100 = df["close"].rolling(100).mean().iloc[-1]
    dma200 = df["close"].rolling(200).mean().iloc[-1]
    low10  = df["low"].rolling(10).min().iloc[-1]

    score = 0
    score += 25 if ltp > dma10 else 0
    score += 25 if ltp > low10 else 0
    score += 10 if ltp > dma20 else 0
    score += 10 if ltp > dma50 else 0
    score += 10 if ltp > dma100 else 0
    score += 10 if ltp > dma200 else 0
    return score


def deep_filters_pass(df: pd.DataFrame, df_today: pd.DataFrame, prev_close: float, quote: dict, now: datetime):
    ltp = df_today["close"].iloc[-1]

    # --- Circuit filter (FIX #2) ---
    if is_in_circuit(quote):
        return False, "Stock in upper/lower circuit"

    # --- Market Positive / Buyer Side % filter ---
    if quote and quote.get("buyer_pct") is not None and quote["buyer_pct"] < MIN_BUYER_SIDE_PCT:
        return False, f"Buyer side only {quote['buyer_pct']:.1f}% (need {MIN_BUYER_SIDE_PCT}%+)"

    # --- Gap filter ---
    gap_pct = ((df_today["open"].iloc[0] - prev_close) / prev_close) * 100
    if abs(gap_pct) > GAP_FILTER_PCT:
        return False, "Gap too large"

    # --- RSI ---
    rsi = calc_rsi(df["close"]).iloc[-1]
    if not (RSI_LOW <= rsi <= RSI_HIGH):
        return False, f"RSI {rsi:.1f} out of range"

    # --- ATR (over-extended move filter) ---
    atr = calc_atr(df).iloc[-1]
    avg_close = df["close"].tail(20).mean()
    if atr / avg_close > 0.06:
        return False, "ATR too high (overextended)"

    # --- Volume multiplier (FIX #1: dynamic threshold) ---
    avg_vol_20 = df["volume"].tail(20).mean()
    today_vol = df_today["volume"].sum()
    vol_mult = today_vol / avg_vol_20 if avg_vol_20 else 0
    min_vol_mult = get_volume_threshold(now)
    if vol_mult < min_vol_mult:
        return False, f"Volume multiplier too low (need {min_vol_mult}, got {vol_mult:.2f})"

    # --- VWAP ---
    vwap = calc_vwap(df_today).iloc[-1]
    if ltp <= vwap:
        return False, "LTP below VWAP"

    # --- RS Strength (proxy) ---
    rs_proxy = ((df["close"].iloc[-1] / df["close"].iloc[-20]) - 1) * 1000
    if rs_proxy < MIN_RS_STRENGTH:
        return False, "RS Strength too low"

    # --- VCP (proxy) ---
    vcp = df["close"].tail(10).std() / df["close"].tail(30).std()
    if vcp >= MAX_VCP:
        return False, "VCP too high"

    return True, "OK"


# ==========================================================================
# STEP 3: MAIN SCANNER LOOP
# ==========================================================================
#
# FIX #4 (RATE LIMIT): હવે 2 તબક્કામાં કામ થાય છે —
#   (a) એકવાર (09:20 પછી પહેલી વાર) બધા 200 સ્ટોકના DAILY candles લઈને
#       Sheet-1 સ્કોર ગણી, ફક્ત 80+ સ્કોર વાળા સ્ટોકનું "shortlist" મેમરીમાં
#       સ્ટોર થાય છે.
#   (b) ત્યારપછી દર મિનિટે ફક્ત એ shortlist (સામાન્ય રીતે 10-20 સ્ટોક) ના
#       જ live LTP/VWAP/circuit ચેક થાય છે — 200 નહીં.
# આનાથી historical getCandleData() કોલ આખા દિવસમાં ~200 વાર જ થાય
# (એક વાર બધા સ્ટોક માટે), 200 વાર/મિનિટ નહીં.
# ==========================================================================

def build_daily_shortlist(smart_api, stocks):
    """સવારે એક જ વાર ચાલે — daily DMA score ગણી 80+ વાળા સ્ટોક શોધે છે"""
    shortlist = []
    for stock in stocks:
        symbol, token = stock["symbol"], stock["token"]
        if has_corporate_action(symbol):
            continue
        df = get_historical_data(smart_api, token)   # daily candles, once per stock
        if df is None or len(df) < 200:
            continue

        # Live LTP fetch કરવો — gap-up સ્ટોકનો સાચો score મળે એ માટે (FIX)
        quote = get_market_quote(smart_api, symbol, token)
        time.sleep(API_CALL_DELAY)
        live_ltp = quote["ltp"] if quote and quote.get("ltp") else None

        score = score_sheet1(df, live_ltp=live_ltp)
        if score >= MIN_SCORE:
            shortlist.append({"symbol": symbol, "token": token, "daily_df": df, "score": score})
    return shortlist


def run_scanner():
    smart_api = angel_login()
    stocks = load_nifty200_list()
    if not stocks:
        send_telegram_msg("❌ *Nifty200 list ખાલી છે — સ્ક્રિપ્ટ બંધ.*")
        return

    last_heartbeat = datetime.now()
    trades_today = 0
    active_trade = None
    shortlist = None   # પહેલી વાર build ન થાય ત્યાં સુધી None
    session_date = datetime.now().date()   # FIX: રોજની state reset ટ્રેક કરવા

    send_telegram_msg("🚀 *Nifty 200 Intraday Scanner Started!*")

    while True:
        try:
            now = datetime.now()
            current_time_str = now.strftime("%H:%M")

            # --------------------------------------------------------------
            # FIX: DAILY STATE RESET — સ્ક્રિપ્ટ સામાન્ય રીતે 02:00 PM પછી
            # break થઈને process જ બંધ થઈ જાય છે (નીચે જુઓ), એટલે જો cloud
            # પર દરરોજ સવારે 09:15 એ ફ્રેશ process/cron ચાલુ થાય, તો
            # active_trade/trades_today/shortlist આપોઆપ જ fresh (None/0)
            # મળી જાય છે — એ કિસ્સામાં આ bug લાગુ નથી પડતો.
            #
            # પણ જો ક્યારેક પ્રોસેસ crash ન થાય અને midnight વટાવીને પણ
            # ચાલુ જ રહી જાય (દા.ત. supervisor એ restart ન કર્યું), તો આ
            # safety-net date-check state ને ફરજિયાત reset કરી દેશે.
            # --------------------------------------------------------------
            if now.date() != session_date:
                send_telegram_msg("🔄 *નવો ટ્રેડિંગ દિવસ — state reset (trade/shortlist).*")
                session_date = now.date()
                trades_today = 0
                active_trade = None
                shortlist = None

            if (now - last_heartbeat).seconds >= 1800:
                send_telegram_msg(f"🟢 *System OK* | {current_time_str} | Running fine.")
                last_heartbeat = now

            if current_time_str > ENTRY_CUTOFF_TIME:
                send_telegram_msg("⏰ *02:00 PM Crossed. No new entries. Scanner stopping.*")
                break

            if current_time_str < ENTRY_START_TIME:
                time.sleep(30)
                continue

            # ---- Build the daily shortlist ONCE, right after 09:20 ----
            if shortlist is None:
                send_telegram_msg(f"🔍 *Building daily shortlist from {len(stocks)} Nifty200 stocks...*")
                shortlist = build_daily_shortlist(smart_api, stocks)
                send_telegram_msg(f"✅ *Shortlist ready:* {len(shortlist)} stocks scored 80+.")
                if not shortlist:
                    send_telegram_msg("⚠️ *No stock qualified today.*")

            if trades_today >= MAX_LOSS_TRADES:
                time.sleep(60)
                continue

            best_candidate = None
            best_score = 0

            # ---- Only iterate the SHORTLIST now (not all 200) ----
            for stock in shortlist:
                symbol, token, df = stock["symbol"], stock["token"], stock["daily_df"]

                df_today = get_today_intraday_data(smart_api, token)   # FIX #3
                if df_today is None or df_today.empty:
                    continue

                prev_close = get_previous_close(df, now.date())        # FIX: prev_close
                if prev_close is None:
                    continue

                quote = get_market_quote(smart_api, symbol, token)     # FIX #2
                time.sleep(API_CALL_DELAY)

                ok, reason = deep_filters_pass(df, df_today, prev_close, quote, now)
                if not ok:
                    continue

                if stock["score"] > best_score:
                    best_score = stock["score"]
                    best_candidate = {"symbol": symbol, "token": token, "df_today": df_today}

            if best_candidate and active_trade is None:
                symbol = best_candidate["symbol"]
                ltp = best_candidate["df_today"]["close"].iloc[-1]
                sl = round(ltp * (1 - INITIAL_SL_PCT / 100), 2)
                target = round(ltp * (1 + TARGET_PCT / 100), 2)

                msg = (
                    f"🔥 *INTRADAY BUY SIGNAL* 🔥\n\n"
                    f"📌 *Stock:* {symbol}\n"
                    f"💰 *Entry Price:* ₹{ltp}\n"
                    f"🎯 *Target ({TARGET_PCT}%):* ₹{target}\n"
                    f"🛑 *Stop Loss ({INITIAL_SL_PCT}%):* ₹{sl}\n"
                    f"📊 *Score:* {best_score}/100\n\n"
                    f"⚠️ *Note:* Shift SL to Cost Price as soon as stock reaches +{BREAKEVEN_TRIGGER}% Profit!"
                )
                send_telegram_msg(msg)
                active_trade = {"symbol": symbol, "token": best_candidate["token"],
                                 "entry": ltp, "sl": sl, "target": target, "be_alert_sent": False}
                trades_today += 1

            if active_trade:
                quote = get_market_quote(smart_api, active_trade["symbol"], active_trade["token"])
                ltp_now = quote["ltp"] if quote else None
                if ltp_now:
                    profit_pct = ((ltp_now - active_trade["entry"]) / active_trade["entry"]) * 100
                    if profit_pct >= BREAKEVEN_TRIGGER and not active_trade["be_alert_sent"]:
                        send_telegram_msg(
                            f"📢 *{active_trade['symbol']}* is +{profit_pct:.2f}%!\n"
                            f"👉 *Shift Stop-loss to Cost Price (₹{active_trade['entry']}) — Break-Even now.*"
                        )
                        active_trade["be_alert_sent"] = True
                    if ltp_now <= active_trade["sl"] or ltp_now >= active_trade["target"]:
                        active_trade = None

            time.sleep(60)

        except Exception as e:
            send_telegram_msg(f"⚠️ *Script Error (auto-recovering):* {e}")
            time.sleep(60)


if __name__ == "__main__":
    run_scanner()
