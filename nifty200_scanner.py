"""
==========================================================================
NIFTY 200 INTRADAY SCANNER — Angel One (SmartAPI) + Telegram Alerts
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
from zoneinfo import ZoneInfo
try:
    from SmartApi import SmartConnect
except ImportError:
    from SmartApi.smartConnect import SmartConnect
INDIA_TZ = ZoneInfo("Asia/Kolkata")

# ==========================================================================
# STEP 1: CONFIGURATION
# ==========================================================================
API_KEY = os.getenv("ANGEL_API_KEY")
CLIENT_ID = os.getenv("ANGEL_CLIENT_ID")
PASSWORD = os.getenv("ANGEL_PASSWORD")
TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ---- TIME RULES ----
ENTRY_START_TIME = "09:30"
ENTRY_CUTOFF_TIME = "14:00"
EOD_ALERT_TIME = "15:00"
MARKET_OPEN_HOUR, MARKET_OPEN_MIN = 9, 15
MARKET_CLOSE_HOUR, MARKET_CLOSE_MIN = 15, 30
TOTAL_MARKET_MINUTES = (MARKET_CLOSE_HOUR * 60 + MARKET_CLOSE_MIN) - (MARKET_OPEN_HOUR * 60 + MARKET_OPEN_MIN)  # 375

# ---- SCORING (spec table, max 100) ----
MIN_SCORE = 80
MAX_POSSIBLE_SCORE = 100

# ---- INTRADAY TECHNICAL FILTERS ----
MAX_VCP = 0.60
GAP_FILTER_PCT = 5.0            # ATR-based નહીં — તમારો explicit નિર્ણય, revert નથી કર્યો (નીચે chat note જુઓ)
RSI_LOW, RSI_HIGH = 50, 80
MIN_BUYER_SIDE_PCT = 54
# FIX: RS vs Nifty filter સંપૂર્ણપણે કાઢી નાખ્યું (તમારો explicit નિર્ણય)

# Corporate Action / Event — manual daily list (FIX #5)
TODAY_EVENT_SYMBOLS = []   # <-- દરરોજ સવારે અહીં update કરો, દા.ત. ["INFY-EQ", "TCS-EQ"]

# ---- TRADE MANAGEMENT ----
INITIAL_SL_PCT = 1.0
DOWNSIDE_ALERT_PCT = -0.7
TARGET_PCT = 1.0
BREAKEVEN_TRIGGER = 0.7

NIFTY200_LIST_FILE = "nifty200_symbols.json"
SECTOR_INDEX_FILE = "sector_indices.json"
STOCK_SECTOR_FILE = "stock_sector_map.json"

API_CALL_DELAY = 0.35
NIFTY_TOKEN = "99926000"

TICK_SEC = 15
ENTRY_SCAN_INTERVAL_SEC = 60
INTRADAY_CACHE_REFRESH_SEC = 300
QUOTE_BATCH_CHUNK_SIZE = 50

INDIA_TZ = ZoneInfo("Asia/Kolkata")

# ==========================================================================
# STEP 2: HELPER FUNCTIONS
# ==========================================================================

def get_ist_now():
    return datetime.now(INDIA_TZ)


def send_telegram_msg(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram configuration missing!")
        return
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
            raise SystemExit("Login failed")
        send_telegram_msg("✅ *Angel One Login Successful.* Scanner starting...")
        return smart_api
    except Exception as e:
        send_telegram_msg(f"❌ *Login Error:* {e}")
        raise


def load_nifty200_list():
    try:
        with open(NIFTY200_LIST_FILE, "r") as f:
            data = json.load(f)
    except Exception as e:
        send_telegram_msg(f"⚠️ *Nifty200 list load error:* {e}")
        return []
    return [s for s in data if isinstance(s, dict) and "symbol" in s and "token" in s]


def load_sector_indices():
    try:
        with open(SECTOR_INDEX_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}   # sector data ના મળે તો soft-filter apply નહીં થાય (નીચે જુઓ)


def load_stock_sector_map():
    try:
        with open(STOCK_SECTOR_FILE, "r") as f:
            data = json.load(f)
        return {s["symbol"]: s["sector"] for s in data if "symbol" in s and "sector" in s}
    except Exception:
        return {}


def get_volume_threshold(current_time: datetime):
    hour, minute = current_time.hour, current_time.minute
    if (hour, minute) < (11, 0):
        return 0.3
    elif (hour, minute) < (13, 0):
        return 0.5
    else:
        return 1.0


# FIX (તમારો re-decision): Raw (actual) volume પાછું — projection હટાવ્યું.
# ટ્રેડ-ઓફ યાદ રાખજો: સવારે 09:45 એ પણ raw sum ઓછો જ હશે, પણ threshold પણ
# એ સમયે loose (0.3x) છે એટલે balance થાય છે.
def get_raw_volume_multiplier(df_today: pd.DataFrame, avg_vol_10d: float):
    if avg_vol_10d <= 0 or df_today.empty:
        return 0
    today_vol_so_far = df_today["volume"].sum()
    return today_vol_so_far / avg_vol_10d


def get_market_quotes_batch(smart_api, token_list: list):
    quotes = {}
    unique_tokens = list({str(t) for t in token_list})
    for i in range(0, len(unique_tokens), QUOTE_BATCH_CHUNK_SIZE):
        chunk = unique_tokens[i:i + QUOTE_BATCH_CHUNK_SIZE]
        try:
            params = {"mode": "FULL", "exchangeTokens": {"NSE": chunk}}
            data = smart_api.getMarketData(**params) if hasattr(smart_api, "getMarketData") else None
            time.sleep(API_CALL_DELAY)
            if data and data.get("status") and data.get("data", {}).get("fetched"):
                for row in data["data"]["fetched"]:
                    token = str(row.get("symbolToken") or row.get("token") or "")
                    if not token:
                        continue
                    buy_qty = float(row.get("totBuyQuan", 0))
                    sell_qty = float(row.get("totSellQuan", 0))
                    total_qty = buy_qty + sell_qty
                    buyer_pct = (buy_qty / total_qty * 100) if total_qty > 0 else None
                    quotes[token] = {
                        "ltp": float(row.get("ltp", 0)),
                        "upper_circuit": float(row.get("upperCircuit", 0)),
                        "lower_circuit": float(row.get("lowerCircuit", 0)),
                        "buyer_pct": buyer_pct,
                    }
        except Exception as e:
            print(f"Batch quote error: {e}")
    return quotes


def is_in_circuit(quote: dict, buffer_pct: float = 0.15):
    if not quote or quote.get("upper_circuit", 0) <= 0:
        return False
    ltp, uc, lc = quote["ltp"], quote["upper_circuit"], quote["lower_circuit"]
    near_upper = ltp >= uc * (1 - buffer_pct / 100)
    near_lower = ltp <= lc * (1 + buffer_pct / 100)
    return near_upper or near_lower


def get_historical_data(smart_api, symbol_token, interval="ONE_DAY", from_date=None, to_date=None, days=365):
    now = get_ist_now()
    if to_date is None:
        to_date = now
    if from_date is None:
        from_date = to_date - timedelta(days=days)
    params = {
        "exchange": "NSE",
        "symboltoken": str(symbol_token),
        "interval": interval,
        "fromdate": from_date.strftime("%Y-%m-%d %H:%M"),
        "todate": to_date.strftime("%Y-%m-%d %H:%M"),
    }
    try:
        data = smart_api.getCandleData(params)
        time.sleep(API_CALL_DELAY)
        if not data.get("status") or not data.get("data"):
            return None
        df = pd.DataFrame(data["data"], columns=["timestamp", "open", "high", "low", "close", "volume"])
        df[["open", "high", "low", "close", "volume"]] = df[["open", "high", "low", "close", "volume"]].astype(float)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df
    except Exception as e:
        print(f"Historical data error ({symbol_token}): {e}")
        return None


def get_today_intraday_data(smart_api, symbol_token, interval="FIVE_MINUTE"):
    now = get_ist_now()
    market_open_today = now.replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MIN, second=0, microsecond=0)
    df = get_historical_data(smart_api, symbol_token, interval=interval,
                              from_date=market_open_today, to_date=now, days=1)
    if df is None or df.empty:
        return df
    df = df[df["timestamp"].dt.date == now.date()].reset_index(drop=True)
    return df


def get_today_intraday_cached(smart_api, token, cache: dict, now: datetime):
    entry = cache.get(token)
    if entry and (now - entry["fetched_at"]).total_seconds() < INTRADAY_CACHE_REFRESH_SEC:
        return entry["df"]
    df = get_today_intraday_data(smart_api, token)
    cache[token] = {"df": df, "fetched_at": now}
    return df


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


def calc_vwap(df_today: pd.DataFrame):
    vol_sum = df_today["volume"].cumsum()
    if vol_sum.empty or vol_sum.iloc[-1] == 0:
        return df_today["close"]
    typical_price = (df_today["high"] + df_today["low"] + df_today["close"]) / 3
    return (typical_price * df_today["volume"]).cumsum() / vol_sum


def is_index_positive(smart_api, token, cache, now):
    try:
        df_today = get_today_intraday_cached(smart_api, token, cache, now)
        if df_today is None or df_today.empty or len(df_today) < 3:
            return False
        open_price = df_today["open"].iloc[0]
        ltp = df_today["close"].iloc[-1]
        vwap = calc_vwap(df_today).iloc[-1]
        return (ltp >= open_price) and (ltp > vwap)
    except Exception as e:
        print(f"Index trend check error: {e}")
        return False


def has_corporate_action(symbol: str):
    """
    FIX #5: TODO_EVENT_SYMBOLS (config ટોચ પર) — મેન્યુઅલી દરરોજ update
    કરવાનું list. Filter નથી (exclude નથી કરતું), ફક્ત message માં ⚠️ tag.
    """
    return symbol in TODAY_EVENT_SYMBOLS


def score_sheet1(df: pd.DataFrame, live_ltp: float = None):
    if df is None or len(df) < 200:
        return 0
    ltp = live_ltp if live_ltp is not None else df["close"].iloc[-1]
    dma10 = df["close"].rolling(10).mean().iloc[-1]
    dma20 = df["close"].rolling(20).mean().iloc[-1]
    dma50 = df["close"].rolling(50).mean().iloc[-1]
    dma100 = df["close"].rolling(100).mean().iloc[-1]
    dma200 = df["close"].rolling(200).mean().iloc[-1]
    low10 = df["low"].rolling(10).min().iloc[-1]

    score = 0
    score += 25 if ltp > dma10 else 0
    score += 25 if ltp > low10 else 0
    score += 10 if ltp > dma20 else 0
    score += 10 if ltp > dma50 else 0
    score += 10 if ltp > dma100 else 0
    score += 10 if ltp > dma200 else 0
    score += 10 if (dma20 > dma50 > dma100 > dma200) else 0
    return score   # max = 100


def calc_volatility_contraction(df: pd.DataFrame):
    """
    FIX (loosened): "contracting" tolerance 1.15 → 1.35 કરી છે, જેથી
    સાચા breakout સ્ટોક (જ્યાં stage1 stage2 કરતાં થોડો વધારે હોવા છતાં
    overall multi-week ratio સારો હોય) reject ન થાય. Formula same (multi-
    stage daily-range contraction) — તમારો સૂચવેલો intraday-range formula
    ("Today High-Low / Open") ના વાપર્યો, કારણ કે એ true VCP (multi-day
    pattern) ને બદલે એક-દિવસનું અલગ જ measure બની જાય.
    """
    if len(df) < 30:
        return None
    daily_range_pct = (df["high"] - df["low"]) / df["close"]
    stage1 = daily_range_pct.tail(10).mean()
    stage2 = daily_range_pct.tail(20).head(10).mean()
    stage3 = daily_range_pct.tail(30).head(10).mean()
    if stage3 <= 0:
        return None
    contracting = stage1 <= stage2 * 1.35 and stage2 <= stage3 * 1.35
    ratio = stage1 / stage3
    return ratio if contracting else 999


def deep_filters_pass(df, df_today, prev_close, quote, now, avg_vol_10d, sector_positive):
    """
    FIX (diagnostics): હવે આ function ત્રીજું value પણ return કરે છે —
    `metrics` dict — જેમાં LTP/RSI/VWAP/VCP/Gap/Volume/Buyer% ના actual
    computed values હોય છે, ભલે condition pass થાય કે fail. આનાથી
    caller ને diagnostic message અને EOD summary બંને માટે data મળે છે.
    Pass/Fail logic બિલકુલ same રાખ્યું છે, ફક્ત values ને capture કરી છે.
    """
    ltp = df_today["close"].iloc[-1]
    metrics = {"ltp": round(float(ltp), 2)}

    if is_in_circuit(quote):
        return False, "Stock in upper/lower circuit", metrics

    buyer_pct = quote.get("buyer_pct") if quote else None
    metrics["buyer_pct"] = round(buyer_pct, 1) if buyer_pct is not None else None
    if buyer_pct is not None and buyer_pct < MIN_BUYER_SIDE_PCT:
        return False, f"Buyer side only {buyer_pct:.1f}%", metrics

    # Gap filter (simple %, ATR હટાવ્યું)
    gap_pct = abs((df_today["open"].iloc[0] - prev_close) / prev_close) * 100
    metrics["gap_pct"] = round(float(gap_pct), 2)
    if gap_pct > GAP_FILTER_PCT:
        return False, f"Gap too large ({metrics['gap_pct']}%)", metrics

    # RSI (50-80)
    rsi = calc_rsi(df["close"]).iloc[-1]
    metrics["rsi"] = round(float(rsi), 2) if pd.notna(rsi) else None
    if pd.isna(rsi) or not (RSI_LOW <= rsi <= RSI_HIGH):
        return False, f"RSI out of range ({metrics['rsi']})", metrics

    # Volume (raw actual, vs 10-Day Avg)
    vol_mult = get_raw_volume_multiplier(df_today, avg_vol_10d)
    metrics["vol_mult"] = round(float(vol_mult), 2)
    if vol_mult < get_volume_threshold(now):
        return False, f"Volume too low ({vol_mult:.2f}x)", metrics

    # VWAP
    vwap = calc_vwap(df_today).iloc[-1]
    metrics["vwap"] = round(float(vwap), 2)
    if ltp <= vwap:
        return False, f"LTP below VWAP (LTP {ltp:.2f} <= VWAP {vwap:.2f})", metrics

    # VCP
    vcf = calc_volatility_contraction(df)
    metrics["vcp"] = round(float(vcf), 3) if (vcf is not None and vcf != 999) else vcf
    if vcf is None or vcf >= MAX_VCP:
        return False, f"VCP too high / not contracting ({metrics['vcp']})", metrics

    # Sector — SOFT check only (block નથી કરતું, contradiction resolve — નીચે chat note જુઓ)
    # sector_positive param અહીં ફક્ત message-tagging માટે વપરાય છે, caller માં.

    return True, "OK", metrics


def format_diag_message(symbol: str, metrics: dict):
    """
    NEW: સ્ટોક પહેલીવાર entry-scan filter-check માંથી પસાર થાય ત્યારે (pass
    થાય કે fail — બંને case માં) એક વખત આ diagnostic message મોકલાય છે,
    જેમાં LTP/RSI/VWAP/VCP/Gap/Buyer% ના actual values + કઈ condition
    ✅ satisfy થાય છે અને કઈ ❌ નથી થતી એ દેખાય છે.
    """
    def flag(ok):
        return "✅" if ok else ("❌" if ok is False else "➖")

    ltp = metrics.get("ltp")
    rsi = metrics.get("rsi")
    vwap = metrics.get("vwap")
    vcp = metrics.get("vcp")
    gap = metrics.get("gap_pct")
    vol_mult = metrics.get("vol_mult")
    buyer = metrics.get("buyer_pct")

    rsi_ok = (RSI_LOW <= rsi <= RSI_HIGH) if rsi is not None else None
    vwap_ok = (ltp is not None and vwap is not None and ltp > vwap) if vwap is not None else None
    vcp_ok = (vcp is not None and vcp != 999 and vcp < MAX_VCP) if vcp is not None else None
    gap_ok = (gap <= GAP_FILTER_PCT) if gap is not None else None
    vol_ok = (vol_mult is not None) if vol_mult is None else None  # threshold time-dependent, info only
    buyer_ok = (buyer >= MIN_BUYER_SIDE_PCT) if buyer is not None else None

    lines = [f"🆕 *Shortlist Check:* {symbol}", "", f"💰 LTP: ₹{ltp}"]
    lines.append(f"{flag(rsi_ok)} RSI: {rsi} (જોઈએ {RSI_LOW}-{RSI_HIGH})")
    lines.append(f"{flag(vwap_ok)} VWAP: ₹{vwap} (LTP {'>' if vwap_ok else '<='} VWAP જોઈએ)")
    lines.append(f"{flag(vcp_ok)} VCP: {vcp} (જોઈએ < {MAX_VCP})")
    lines.append(f"{flag(gap_ok)} Gap: {gap}% (જોઈએ <= {GAP_FILTER_PCT}%)")
    if buyer is not None:
        lines.append(f"{flag(buyer_ok)} Buyer side: {buyer}% (જોઈએ >= {MIN_BUYER_SIDE_PCT}%)")
    if vol_mult is not None:
        lines.append(f"➖ Volume: {vol_mult}x avg (time-based threshold)")
    lines.append("")
    lines.append("ℹ️ *આ ફક્ત diagnostic info છે.* Actual BUY signal બધી condition પાસ થાય ત્યારે જ અલગથી મોકલાશે.")
    return "\n".join(lines)


def format_eod_rejection_summary(shortlist, rejection_reasons: dict, active_trades):
    """
    NEW: EOD_ALERT_TIME (3:00 PM) આસપાસ એક વખત મોકલાય છે — આજે shortlist
    થયેલા સ્ટોકમાંથી કેટલા entry લેવાયા, કેટલા reject થયા અને કયા કારણથી
    (reason પ્રમાણે group કરીને), અને કેટલા સ્ટોકનું filter-check જ ના
    થયું (દા.ત. Nifty આખો દિવસ negative રહ્યું હોય તો).
    """
    active_symbols = {t["symbol"] for t in active_trades}
    total = len(shortlist)
    entered = len(active_symbols)

    rejected = {
        s["symbol"]: rejection_reasons.get(s["symbol"])
        for s in shortlist
        if s["symbol"] not in active_symbols and s["symbol"] in rejection_reasons
    }
    not_evaluated = [
        s["symbol"] for s in shortlist
        if s["symbol"] not in active_symbols and s["symbol"] not in rejection_reasons
    ]

    by_reason = {}
    for sym, reason in rejected.items():
        key = (reason or "Unknown").split(" (")[0]   # similar reasons group કરવા માટે prefix વાપર્યો
        by_reason.setdefault(key, []).append(sym)

    lines = [
        "📊 *End-of-Day Shortlist Summary*",
        "",
        f"કુલ Shortlisted: {total}",
        f"✅ Entry લેવાયું: {entered}",
        f"❌ Reject થયા: {len(rejected)}",
    ]
    if not_evaluated:
        lines.append(f"⏳ ક્યારેય filter-check જ ના થયું: {len(not_evaluated)} ({', '.join(not_evaluated)})")

    if by_reason:
        lines.append("")
        lines.append("*Reject Reasons:*")
        for reason, syms in sorted(by_reason.items(), key=lambda x: -len(x[1])):
            lines.append(f"• {reason} — {len(syms)} સ્ટોક: {', '.join(syms)}")

    return "\n".join(lines)


def build_daily_shortlist(smart_api, stocks, stock_sector_map):
    shortlist = []
    for stock in stocks:
        symbol, token = stock["symbol"], stock["token"]
        df = get_historical_data(smart_api, token)
        if df is None or len(df) < 200:
            continue
        quotes = get_market_quotes_batch(smart_api, [token])
        live_ltp = quotes.get(str(token), {}).get("ltp")
        score = score_sheet1(df, live_ltp=live_ltp)
        if score >= MIN_SCORE:
            avg_vol_10d = df["volume"].tail(10).mean()
            has_event = has_corporate_action(symbol)
            shortlist.append({
                "symbol": symbol, "token": str(token), "daily_df": df,
                "score": score, "avg_vol_10d": avg_vol_10d,
                "sector": stock_sector_map.get(symbol),
                "has_event": has_event,
            })
    return shortlist


def monitor_active_trades_batch(active_trades, quotes: dict, now: datetime):
    still_active = []
    for trade in active_trades:
        quote = quotes.get(trade["token"])
        if not quote or not quote.get("ltp"):
            still_active.append(trade)
            continue

        ltp_now = quote["ltp"]
        profit_pct = ((ltp_now - trade["entry"]) / trade["entry"]) * 100

        if profit_pct >= BREAKEVEN_TRIGGER and not trade.get("be_alert_sent"):
            send_telegram_msg(
                f"🟢 *{trade['symbol']}*: પ્રાઇસ +{profit_pct:.2f}% વધી ગઈ છે. "
                f"તમારો Stop-Loss તમારી Buying Price (₹{trade['entry']}) પર શિફ્ટ કરી દો (Risk-Free Trade)."
            )
            trade["be_alert_sent"] = True
            trade["sl"] = trade["entry"]

        if profit_pct <= DOWNSIDE_ALERT_PCT and not trade.get("downside_alert_sent"):
            send_telegram_msg(f"⚠️ *{trade['symbol']}*: પ્રાઇસ {profit_pct:.2f}% — ધ્યાન આપો.")
            trade["downside_alert_sent"] = True

        if ltp_now >= trade["target"]:
            send_telegram_msg(
                f"🎯 *{trade['symbol']}*: Target Hit (+{TARGET_PCT:.1f}%)! "
                f"પ્રોફિટ બુક કરો અથવા બાકીની ક્વોન્ટિટી ટ્રેઇલ કરો."
            )
            continue

        if ltp_now <= trade["sl"]:
            send_telegram_msg(
                f"🛑 *{trade['symbol']}*: Stop-Loss Hit! પોઝિશનમાંથી એક્ઝિટ કરી લો.\n"
                f"Entry: ₹{trade['entry']} → LTP: ₹{ltp_now} | P&L: {profit_pct:+.2f}%"
            )
            continue

        current_time_str = now.strftime("%H:%M")
        if current_time_str >= EOD_ALERT_TIME and not trade.get("eod_alert_sent"):
            send_telegram_msg(
                f"⏰ *{trade['symbol']}*: સમય ૩:૦૦ થઈ ગયો છે. ટાર્ગેટ કે સ્ટોપલોસ હિટ થયો નથી. "
                f"વર્તમાન P&L {profit_pct:+.2f}% છે. યોગ્ય લાગે તો પોઝિશન સ્ક્વેર-ઓફ (Exit) કરી લો."
            )
            trade["eod_alert_sent"] = True

        still_active.append(trade)

    return still_active


# ==========================================================================
# STEP 3: MAIN SCANNER LOOP
# ==========================================================================
def run_scanner():
    smart_api = angel_login()
    stocks = load_nifty200_list()
    sector_indices = load_sector_indices()
    stock_sector_map = load_stock_sector_map()
    if not stocks:
        send_telegram_msg("❌ *Nifty200 list ખાલી છે — સ્ક્રિપ્ટ બંધ.*")
        return

    last_heartbeat = get_ist_now()
    active_trades = []   # multiple simultaneous trades allowed
    shortlist = None
    session_date = get_ist_now().date()
    consecutive_errors = 0
    intraday_cache = {}
    last_entry_scan_at = None

    # NEW: diagnostics/rejection tracking (દરરોજ reset થાય છે)
    rejection_reasons = {}   # symbol -> latest reject reason (આજનું)
    diag_sent = set()        # જે સ્ટોકનો diagnostic message આજે એકવાર મોકલાઈ ગયો
    eod_summary_sent = False

    send_telegram_msg("🚀 *Nifty 200 Intraday Scanner Started!*")

    while True:
        try:
            now = get_ist_now()
            current_time_str = now.strftime("%H:%M")
            if current_time_str < "09:15" or current_time_str >= "15:30":
                send_telegram_msg("ℹ️ *માર્કેટ બંધ છે. સ્કેનર બંધ થઈ રહ્યું છે.*")
                break
            if now.date() != session_date:
                send_telegram_msg("🔄 *નવો ટ્રેડિંગ દિવસ — state reset.*")
                session_date = now.date()
                active_trades = []
                shortlist = None
                consecutive_errors = 0
                intraday_cache = {}
                last_entry_scan_at = None
                rejection_reasons = {}
                diag_sent = set()
                eod_summary_sent = False

            if (now - last_heartbeat).total_seconds() >= 1800:
                send_telegram_msg(f"🟢 *System OK* | {current_time_str} IST")
                last_heartbeat = now

            allow_new_entry = current_time_str < ENTRY_CUTOFF_TIME

            if current_time_str >= f"{MARKET_CLOSE_HOUR:02d}:{MARKET_CLOSE_MIN:02d}":
                send_telegram_msg("🏁 *Market Close. Scanner stopping.*")
                break

            if shortlist is None:
                send_telegram_msg(f"🔍 *Building shortlist from {len(stocks)} stocks...*")
                shortlist = build_daily_shortlist(smart_api, stocks, stock_sector_map)
                send_telegram_msg(f"✅ *Shortlist ready:* {len(shortlist)} stocks (score {MIN_SCORE}+/{MAX_POSSIBLE_SCORE}).")

            # NEW: EOD Rejection Summary — 3:00 PM પછી, દિવસમાં એક જ વાર
            if shortlist is not None and current_time_str >= EOD_ALERT_TIME and not eod_summary_sent:
                send_telegram_msg(format_eod_rejection_summary(shortlist, rejection_reasons, active_trades))
                eod_summary_sent = True

            # ---- STEP A: Monitor Active Trades (every tick, batched) ----
            if active_trades:
                tokens = [t["token"] for t in active_trades]
                quotes = get_market_quotes_batch(smart_api, tokens)
                active_trades = monitor_active_trades_batch(active_trades, quotes, now)

            # ---- STEP B: Entry-Scan (throttled; multiple positions allowed) ----
            due_for_scan = (last_entry_scan_at is None or
                             (now - last_entry_scan_at).total_seconds() >= ENTRY_SCAN_INTERVAL_SEC)

            if allow_new_entry and due_for_scan:
                last_entry_scan_at = now
                nifty_ok = is_index_positive(smart_api, NIFTY_TOKEN, intraday_cache, now)
                if nifty_ok:
                    active_symbols = {t["symbol"] for t in active_trades}
                    candidates = [s for s in shortlist if s["symbol"] not in active_symbols]
                    cand_tokens = [c["token"] for c in candidates]
                    quotes = get_market_quotes_batch(smart_api, cand_tokens)

                    # FIX #1: multiple trades allowed હોવાથી, જે પણ સ્ટોક
                    # filters pass કરે એ DEરેકને signal મોકલવો — ફક્ત
                    # single top-score ને નહીં (પહેલાં આ bug હતું).
                    for stock in candidates:
                        symbol, token, df = stock["symbol"], stock["token"], stock["daily_df"]
                        avg_vol_10d = stock["avg_vol_10d"]

                        df_today = get_today_intraday_cached(smart_api, token, intraday_cache, now)
                        if df_today is None or df_today.empty:
                            continue

                        prev_close = get_previous_close(df, now.date())
                        if prev_close is None:
                            continue

                        sector_name = stock.get("sector")
                        sector_token = sector_indices.get(sector_name) if sector_name else None
                        sector_positive = is_index_positive(smart_api, sector_token, intraday_cache, now) \
                            if sector_token else True

                        quote = quotes.get(token)
                        ok, reason, metrics = deep_filters_pass(df, df_today, prev_close, quote, now,
                                                                 avg_vol_10d, sector_positive)

                        # NEW: સ્ટોક પહેલીવાર ચેક થાય ત્યારે diagnostic message (એક જ વાર/દિવસ)
                        if symbol not in diag_sent:
                            diag_sent.add(symbol)
                            send_telegram_msg(format_diag_message(symbol, metrics))

                        if not ok:
                            rejection_reasons[symbol] = reason   # NEW: reject reason log (latest)
                            continue

                        rejection_reasons.pop(symbol, None)      # entry લેવાય તો reject log માંથી કાઢી નાખો

                        ltp = quote["ltp"] if quote and quote.get("ltp") else df_today["close"].iloc[-1]
                        sl = round(ltp * (1 - INITIAL_SL_PCT / 100), 2)
                        target = round(ltp * (1 + TARGET_PCT / 100), 2)

                        msg = (
                            f"🔥 *INTRADAY BUY SIGNAL* 🔥\n\n"
                            f"📌 *Stock:* {symbol}\n"
                            f"💰 *Entry:* ₹{ltp}\n"
                            f"🎯 *Target ({TARGET_PCT}%):* ₹{target}\n"
                            f"🛑 *SL ({INITIAL_SL_PCT}%):* ₹{sl}\n"
                            f"📊 *Score:* {stock['score']}/{MAX_POSSIBLE_SCORE}\n"
                        )
                        if stock.get("has_event"):
                            msg += (
                                "\n⚠️ *સિલેક્શન કન્ડિશન બની છે, પણ આજે સ્ટોકમાં EVENT "
                                "(Result/Dividend) છે. ધ્યાનપૂર્વક ટ્રેડ કરવો.*\n"
                            )
                        if not sector_positive:
                            msg += (
                                "\nℹ️ *Nifty પોઝિટિવ છે એટલે Opportunity છે, પરંતુ સેક્ટર "
                                "નેગેટિવ છે. રિસ્ક મેનેજમેન્ટ સાથે આગળ વધવું.*\n"
                            )
                        send_telegram_msg(msg)

                        active_trades.append({
                            "symbol": symbol, "token": token,
                            "entry": ltp, "sl": sl, "target": target,
                            "be_alert_sent": False, "downside_alert_sent": False,
                            "eod_alert_sent": False,
                        })

            consecutive_errors = 0
            time.sleep(TICK_SEC)

        except Exception as e:
            consecutive_errors += 1
            error_msg = str(e).lower()
            send_telegram_msg(f"⚠️ *Script Error:* {e}")

            if any(x in error_msg for x in ["session", "token", "auth", "login", "unauthorized", "401", "invalid"]):
                try:
                    smart_api = angel_login()
                except Exception:
                    pass

            if consecutive_errors >= 5:
                send_telegram_msg("🔴 *વારંવાર Error. 5 મિનિટ રાહ જોઈ રહ્યા છીએ...*")
                time.sleep(300)
                consecutive_errors = 0

            time.sleep(60)


if __name__ == "__main__":
    run_scanner()
