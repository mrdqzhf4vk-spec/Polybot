"""
╔══════════════════════════════════════════════════════════════╗
║   POLYMARKET BTC 5-MIN LAG ARBITRAGE — PAPER TRADER         ║
║   Monitors Binance vs Polymarket odds in real-time           ║
║   Auto-enters paper positions when LAG is detected           ║
╚══════════════════════════════════════════════════════════════╝

Strategy:
  - Every 5 min, Polymarket opens a BTC "Up or Down" market
  - Binance price moves FASTER than Polymarket odds update
  - When Binance says BTC is clearly Up/Down but odds still show 50-65¢,
    there's a 15-30 second lag window → enter that side
  - Paper sell at 80¢+ or at -40¢ (stop loss)

How to run:
    pip install requests colorama
    python bot.py
"""

import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
print("--- [DEBUG] Script started... ---")

import requests
import time
import json
import csv
import os, sys, math, threading
from datetime import datetime, timezone
try:
    import winsound
    HAS_SOUND = True
except ImportError:
    HAS_SOUND = False
from collections import deque

# ─────────────────────────────────────────────────────────────
# CONFIG — Tweak these to adjust sensitivity
# ─────────────────────────────────────────────────────────────
PAPER_STAKE          = 100.0    # Virtual dollars per trade
# ALERTS & NOTIFICATIONS
ENABLE_SOUND         = False    # Set to False to disable terminal beeps
TELEGRAM_BOT_TOKEN   = "8689564877:AAGuk6WVY0hrH0vwj9uNUir3GJ4okKZDnd8"
TELEGRAM_CHAT_ID      = "5997475720"

LAG_THRESHOLD_CENTS  = 4        # More aggressive (was 12)
MIN_TIME_REMAINING   = 40       # Don't enter with < 60 seconds left
MAX_TIME_REMAINING   = 290    # Don't enter with > 290 seconds left (too early)
TARGET_EXIT_CENTS    = 101      # DISABLED: hold until expiry
STOP_LOSS_CENTS      = 0        # DISABLED: hold until expiry
BTC_MIN_DISPLACEMENT = 0.5      # More aggressive (was 15)
POLL_INTERVAL        = 3        # Seconds between data polls
MAX_ALLOWED_SPREAD_CENTS = 20.0 # Relaxed spread (was 10.0)

TRADE_LOG_FILE  = "trades.csv"
GAMMA_API       = "https://gamma-api.polymarket.com"
CLOB_API        = "https://clob.polymarket.network"
BINANCE_API     = "https://api.binance.com"
SERIES_SLUG     = "btc-updown-5m"

# ─────────────────────────────────────────────────────────────
# TERMINAL COLORS
# ─────────────────────────────────────────────────────────────
try:
    from colorama import init, Fore, Back, Style
    init(autoreset=True)
    GREEN  = Fore.GREEN
    RED    = Fore.RED
    YELLOW = Fore.YELLOW
    CYAN   = Fore.CYAN
    WHITE  = Fore.WHITE
    BOLD   = Style.BRIGHT
    RESET  = Style.RESET_ALL
    MAGENTA = Fore.MAGENTA
except ImportError:
    GREEN = RED = YELLOW = CYAN = WHITE = BOLD = RESET = MAGENTA = ""

def clr(text, color): return f"{color}{text}{RESET}" if RESET else text

# ─────────────────────────────────────────────────────────────
# DATA FETCHERS
# ─────────────────────────────────────────────────────────────

def get_binance_btc_price():
    """Get current BTC/USDT price from Binance with fallback."""
    for api in [BINANCE_API, "https://api1.binance.com", "https://api2.binance.com", "https://api.binance.us"]:
        try:
            r = requests.get(f"{api}/api/v3/ticker/price", params={"symbol": "BTCUSDT"}, timeout=3)
            if r.status_code == 200:
                return float(r.json()["price"])
        except: continue
    return None


def get_binance_sentiment():
    """
    Get BTC/USDT orderbook depth and calculate sentiment.
    Returns (sentiment_ratio, total_depth_usd, whale_alert)
    """
    try:
        r = requests.get(f"{BINANCE_API}/api/v3/depth",
                         params={"symbol": "BTCUSDT", "limit": 100}, timeout=5)
        data = r.json()
        
        bids = data.get("bids", []) # [[price, qty], ...]
        asks = data.get("asks", [])
        
        buy_vol = sum(float(b[0]) * float(b[1]) for b in bids)
        sell_vol = sum(float(a[0]) * float(a[1]) for a in asks)
        
        if buy_vol + sell_vol == 0:
            return 0.0, 0.0, False, False
            
        ratio = (buy_vol - sell_vol) / (buy_vol + sell_vol)
        w_buy  = any(float(b[0]) * float(b[1]) > 500_000 for b in bids)
        w_sell = any(float(a[0]) * float(a[1]) > 500_000 for a in asks)
                    
        return ratio, buy_vol + sell_vol, w_buy, w_sell
    except:
        return 0.0, 0.0, False, False


def get_polymarket_strike(coin, block_start_ts_sec):
    """Fetch official Strike Price from Polymarket price API."""
    try:
        start_dt = datetime.fromtimestamp(block_start_ts_sec, tz=timezone.utc)
        end_dt   = datetime.fromtimestamp(block_start_ts_sec + 300, tz=timezone.utc)
        start_iso = start_dt.isoformat().replace("+00:00", "Z")
        end_iso   = end_dt.isoformat().replace("+00:00", "Z")
        
        url = "https://polymarket.com/api/crypto/crypto-price"
        r = requests.get(url, params={
            "symbol": coin, "eventStartTime": start_iso,
            "variant": "fiveminute", "endDate": end_iso
        }, timeout=8)
        if r.status_code == 200:
            raw_p = r.json().get("openPrice")
            if raw_p: return float(raw_p), None
        return None, "Awaiting Official Strike..."
    except:
        return None, "Connection Error"


def fetch_active_market():
    """Two-Tier Discovery: Event Slug -> Market Extraction."""
    try:
        now = int(time.time())
        block_base = (now // 300) * 300
        candidates = [block_base + (i*300) for i in range(-3, 4)]
        
        for block in candidates:
            slug = f"btc-updown-5m-{block}"
            # Tier 1: Find Event ID
            url_ev = "https://gamma-api.polymarket.com/events"
            r_ev = requests.get(url_ev, params={"slug": slug}, timeout=3)
            data_ev = r_ev.json()
            if not data_ev or not isinstance(data_ev, list): continue
            
            event_id = data_ev[0].get("id")
            if not event_id: continue
            
            # Tier 2: Fetch Live Markets for this Event
            url_mkt = "https://gamma-api.polymarket.com/markets"
            r_mkt = requests.get(url_mkt, params={"event_id": event_id}, timeout=3)
            mkts = r_mkt.json()
            if not mkts: continue
            
            m = mkts[0]
            end_s = m.get("endDate", "")
            if not end_s: continue
            
            end_ts = int(datetime.fromisoformat(end_s.replace("Z", "+00:00")).timestamp())
            duration_left = end_ts - now
            
            if duration_left > 0:
                title  = m.get("question", "BTC 5m Prediction")
                mkt_id = m.get("id")
                tok_raw = m.get("clobTokenIds", "[]")
                tokens = json.loads(tok_raw) if isinstance(tok_raw, str) else tok_raw
                outcomes_raw = m.get("outcomes", "[]")
                outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw

                if len(tokens) >= 2 and len(outcomes) >= 2:
                    up_idx   = next((i for i, o in enumerate(outcomes) if "up" in str(o).lower()), 0)
                    down_idx = 1 - up_idx
                    up_p     = m.get("outcomePrices", [0.5, 0.5])[up_idx]
                    dn_p     = m.get("outcomePrices", [0.5, 0.5])[down_idx]
                    
                    return tokens[up_idx], tokens[down_idx], mkt_id, title, end_ts, slug, up_idx, float(up_p), float(dn_p)

    except Exception: pass
    return None, None, None, None, None, None, None, 0.5, 0.5


def get_clob_price(token_id):
    """Fetch live orderbook from CLOB API (True Prices)."""
    try:
        # Use the standard CLOB REST endpoint
        url = "https://clob.polymarket.com/book"
        params = {"token_id": token_id}
        r = requests.get(url, params=params, timeout=3)
        if r.status_code == 200:
            data = r.json()
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            
            # Use the top of the book
            best_bid = float(bids[0]["price"]) if bids else 0.5
            best_ask = float(asks[0]["price"]) if asks else 0.5
            return best_bid, best_ask
    except: pass
    return 0.5, 0.5


def get_historical_streak():
    """Read recent streak from analysis results to determine Market Mood."""
    try:
        path = os.path.join("..", "polymarket_5min_analysis", "results.json")
        if not os.path.exists(path): return 0
        with open(path, "r") as f:
            data = json.load(f)
            seq = data.get("analysis", {}).get("outcome_sequence", [])
            if not seq: return 0
            last_5 = seq[-5:]
            # Calculate score: +1 for Up, -1 for Down
            score = sum(1 if x == "Up" else -1 for x in last_5)
            return score
    except: return 0


def send_telegram_notification(message):
    """Send alert to your phone via Telegram API."""
    if not TELEGRAM_BOT_TOKEN or "PASTE" in TELEGRAM_BOT_TOKEN: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown"
        }
        requests.post(url, json=payload, timeout=5)
    except: pass

def telegram_command_agent(trader):
    """Background listener for Telegram commands like /wr, /stats."""
    if not TELEGRAM_BOT_TOKEN or "PASTE" in TELEGRAM_BOT_TOKEN: return
    last_update_id = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {"offset": last_update_id + 1, "timeout": 30}
            r = requests.get(url, params=params, timeout=35)
            if r.status_code == 200:
                data = r.json()
                for update in data.get("result", []):
                    last_update_id = update["update_id"]
                    msg = update.get("message", {})
                    text = msg.get("text", "").lower()
                    sender_id = str(msg.get("chat", {}).get("id", ""))
                    
                    # Security: only reply to YOU
                    if sender_id != str(TELEGRAM_CHAT_ID): continue
                    
                    if text in ["/wr", "/stats", "/pnl"]:
                        resp = (
                            "📊 *LIVE SNIPER STATS* 📊\n\n"
                            f"✅ *Official Wins*: `{trader.total_wins}`\n"
                            f"❌ *Official Losses*: `{trader.total_losses}`\n"
                            f"🎯 *Win Rate*: `{trader.win_rate:.1f}%`\n"
                            f"💰 *Total PnL*: `${trader.total_pnl:+.2f}`\n\n"
                            f"⏳ *Pending Resolution*: `{len(trader.pending_resolution)}` trade(s)"
                        )
                        send_telegram_notification(resp)
                    elif text == "/start" or text == "/help":
                        send_telegram_notification("🤖 *Sniper Bot Active*\nCommands: `/wr`, `/stats`")
        except: pass
        time.sleep(2)


def compute_fair_value(btc_price, price_to_beat, seconds_remaining):
    """Compute the 'fair value' probability that BTC ends Up."""
    if not price_to_beat or not btc_price: return 0.5
    displacement = btc_price - price_to_beat
    BTC_VOL_5MIN = 80  
    time_fraction  = max(seconds_remaining / 300, 0.05)
    adjusted_vol   = BTC_VOL_5MIN * math.sqrt(time_fraction)
    z_score = displacement / adjusted_vol
    k = 1.7
    prob_up = 1.0 / (1.0 + math.exp(-k * z_score))
    return round(prob_up, 4)

# ─────────────────────────────────────────────────────────────
# PAPER TRADING ENGINE
# ─────────────────────────────────────────────────────────────

class PaperTrade:
    def __init__(self, slug, direction, entry_p, strike, btc_at_entry, mkt_id, up_idx=0):
        self.slug = slug
        self.direction = direction # "Up" or "Down"
        self.entry_price = entry_p
        self.strike = strike
        self.btc_at_entry = btc_at_entry
        self.market_id = mkt_id
        self.up_index  = up_idx # Polymarket winnerIndex mapping
        self.entry_time = time.time()
        self.status = "OPEN"
        self.exit_price = None
        self.pnl = 0.0
        self.is_resolved = False
        self.official_winner = None # "Up", "Down" or None

    def current_pnl_pct(self, current_price):
        if not current_price: return 0.0
        return (current_price - self.entry_price) / self.entry_price * 100

class PaperTrader:
    def __init__(self):
        self.active_trade = None
        self.history = []
        self.pending_resolution = deque(maxlen=50) 
        self.total_wins = 0
        self.total_losses = 0
        self.total_pnl = 0.0
        self.last_settle_check = 0
        
        # --- Persistent Memory: Load stats from CSV ---
        if os.path.exists(TRADE_LOG_FILE):
            try:
                with open(TRADE_LOG_FILE, "r", newline="", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        status = row.get("Status", "")
                        pnl = float(row.get("PnL", 0) or 0)
                        if status == "WIN": self.total_wins += 1
                        elif status == "LOSS": self.total_losses += 1
                        self.total_pnl += pnl
                print(clr(f"  [MEMORY LOADED] Wins: {self.total_wins} | Losses: {self.total_losses} | PnL: ${self.total_pnl:.2f}", CYAN))
            except: pass
        else:
            # Create header if new
            with open(TRADE_LOG_FILE, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(["Time", "Slug", "Dir", "Entry", "Strike", "Result", "PnL", "Status"])

    def enter(self, slug, direction, price, strike, btc_p, mkt_id, up_idx=0):
        self.active_trade = PaperTrade(slug, direction, price, strike, btc_p, mkt_id, up_idx)
        print(clr(f"  >>> PAPER ENTRY: {direction} @ {price*100:.1f}¢", GREEN + BOLD))

    def update(self, current_price, market_expired=False):
        if not self.active_trade: return
        
        if market_expired:
            self.active_trade.status = "PENDING_RESOLUTION"
            self.pending_resolution.append(self.active_trade)
            print(clr(f"  >>> MARKET ENDED: Awaiting official resolution for {self.active_trade.slug}", YELLOW))
            self.active_trade = None
            return

    def poll_resolutions(self):
        """Poll Gamma API for official results of pending trades."""
        now = time.time()
        if now - self.last_settle_check < 45: return # Check every 45s
        self.last_settle_check = now
        
        if not self.pending_resolution: return
        
        # We only check the oldest few to stay efficient
        for _ in range(min(5, len(self.pending_resolution))):
            trade = self.pending_resolution[0]
            try:
                url = f"{GAMMA_API}/markets/{trade.market_id}"
                r = requests.get(url, timeout=5)
                if r.status_code == 200:
                    data = r.json()
                    if data.get("resolved") == True:
                        self.pending_resolution.popleft() # Resolved!
                        winner_idx = data.get("winnerIndex")
                        
                        # Compare to our 'Up' index mapping
                        winner_dir = "Up" if winner_idx == trade.up_index else "Down"
                        trade.official_winner = winner_dir
                        trade.is_resolved = True
                        
                        if trade.direction == winner_dir:
                            self.total_wins += 1
                            trade.pnl = 1.0 - trade.entry_price
                        else:
                            self.total_losses += 1
                            trade.pnl = -trade.entry_price
                            
                        self.total_pnl += trade.pnl
                        self.history.append(trade)
                        
                        # Write to Persistent Memory (CSV)
                        try:
                            with open(TRADE_LOG_FILE, "a", newline="", encoding="utf-8") as f:
                                csv.writer(f).writerow([
                                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    trade.slug, trade.direction, trade.entry_price, 
                                    trade.strike, winner_dir, f"{trade.pnl:.4f}", 
                                    "WIN" if trade.direction == winner_dir else "LOSS"
                                ])
                        except: pass

                        print(clr(f"  [OFFICIAL SETTLEMENT] {trade.slug}: {winner_dir.upper()} wins. Result: {'WIN' if trade.direction == winner_dir else 'LOSS'}", CYAN + BOLD))
                else:
                    # Move to tail of queue to try later
                    self.pending_resolution.rotate(-1)
            except:
                self.pending_resolution.rotate(-1)

    @property
    def total_trades(self): return self.total_wins + self.total_losses
    @property
    def win_rate(self):
        return (self.total_wins / self.total_trades * 100) if self.total_trades > 0 else 0.0

# ─────────────────────────────────────────────────────────────
# DASHBOARD
# ─────────────────────────────────────────────────────────────

def clear_screen(): os.system("cls" if os.name == "nt" else "clear")

def render_dashboard(state):
    try:
        clear_screen()
        W = 65
        def line(t="", p=True): print(f"  {t}" if p else t)
        
        # --- SIGNAL HUD SECTION ---
        sig_type = state.get("active_signal") # "Up", "Down" or None
        if sig_type:
            col = GREEN if sig_type == "Up" else RED
            mode = state.get("sig_mode", "LAG")
            print(clr("=" * W, col + BOLD))
            print(clr(f"  [!] {mode.upper()} ALERT: BUY {sig_type.upper()} NOW! [!]", col + BOLD).center(W))
            print(clr("=" * W, col + BOLD))
        else:
            print(clr("=" * W, CYAN + BOLD))
            print(clr(f"  POLYMARKET SIGNAL SNIPER v3 — SCANNING...", CYAN + BOLD).center(W))
            print(clr("=" * W, CYAN + BOLD))

        print(clr(f"  {datetime.now().strftime('%Y-%m-%d  %H:%M:%S')}", CYAN).center(W))
        print(clr("-" * W, CYAN))

        sec = state.get("seconds_remaining", 0)
        line(f"[MARKET]  {state.get('market_title', 'Scanning...'):.50s}")
        line(f"  Time remaining:   " + clr(f"{sec//60}:{sec%60:02d}", YELLOW + BOLD))
        
        mood = state.get("lag_debug", {}).get("mood", 0)
        mood_str = "🔥 UP TREND" if mood >= 2 else ("❄️ DOWN TREND" if mood <= -2 else "⚖️ NEUTRAL")
        line(f"  Market Mood:      " + clr(f"{mood_str} ({mood:+})", CYAN))

        ptb = state.get("price_to_beat"); btc = state.get("btc_price")
        line()
        line(clr("[PRICES]", WHITE + BOLD))
        line(f"  Price to Beat:    " + clr(f"${ptb:,.2f}" if ptb else "Fetching...", WHITE))
        if btc and ptb:
            d = btc - ptb
            line(f"  Binance BTC:      " + clr(f"${btc:,.2f} ({'+' if d>=0 else ''}{d:,.2f})", GREEN if d>=0 else RED))

        ratio = state.get("sentiment_ratio", 0.0)
        line()
        line(clr("[AI SENTIMENT ANALYZER]", WHITE + BOLD))
        filled = int((ratio + 1) / 2 * 20); bar = "|" * filled + "-" * (20 - filled)
        col = GREEN if ratio > 0.1 else (RED if ratio < -0.1 else YELLOW)
        line(f"  Sentiment:   " + clr("BULLISH" if ratio>0.1 else ("BEARISH" if ratio<-0.1 else "NEUTRAL"), col + BOLD))
        line(f"  [POLYMARKET PRICES] [{state.get('lag_debug',{}).get('source','SCAN')}]")
        line(f"    UP Tokens:   {state.get('up_price', 0)*100:.1f}¢")
        line(f"    DOWN Tokens: {state.get('dn_price', 0)*100:.1f}¢")
        line()
        line(clr("[POLYMARKET ORDERBOOK]", WHITE + BOLD) + clr(" [TRUE CLOB]", GREEN))
        
        def price_line(lbl, b, a, col):
            if b is None or a is None: return f"  {lbl}: Fetching..."
            return f"  {lbl}:   {clr(f'{b*100:2.1f}¢', WHITE)} bid / {clr(f'{a*100:2.1f}¢', col)} ask"

        line(price_line("UP Tokens", state.get("up_bid"), state.get("up_ask"), GREEN))
        line(price_line("DOWN Tokens", state.get("dn_bid"), state.get("dn_ask"), RED))

        lag_data = state.get("lag_debug", {})
        up_gap = lag_data.get("up_gap"); dn_gap = lag_data.get("dn_gap")
        line(); line(clr("[SIGNAL SNIPER METER]", WHITE + BOLD))
        
        def bar_gap(g, lbl):
            if g is None: return f"  {lbl}: Scanning..."
            pct = min(1.0, max(0.0, abs(g) / LAG_THRESHOLD_CENTS))
            b = "|" * int(pct*15) + "-" * (15 - int(pct*15))
            return f"  {lbl}: [{clr(b, GREEN if pct>=1.0 else CYAN)}] {g:+.1f}\u00a2 / {LAG_THRESHOLD_CENTS}\u00a2"

        line(bar_gap(up_gap, "UP Opportunity  "))
        line(bar_gap(dn_gap, "DOWN Opportunity"))

        print(clr("=" * W, CYAN + BOLD))
    except Exception as e:
        print(f"\r[Dashboard Render Error] {e}", end="")

# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

def main():
    print(clr("--- Initializing Paper Trader ---", CYAN + BOLD))
    print(f"Trade log: {TRADE_LOG_FILE}")
    
    print(clr("Testing connections...", WHITE))
    try:
        requests.get(GAMMA_API, timeout=5)
        print(clr("  - Gamma API: OK", GREEN))
    except:
        print(clr("  - Gamma API: FAILED (Check internet)", RED))
    
    trader = PaperTrader()
    # Start the Telegram Interactive Listener in the background
    threading.Thread(target=telegram_command_agent, args=(trader,), daemon=True).start()
    
    last_alert_slug = None # Anti-spam: one alert per market
    
    while True:
        try:
            # 1. Discover active market
            res = fetch_active_market()
            up_tok, dn_tok, mkt_id, title, end_ts, slug, up_idx, amm_up, amm_dn = res
            
            if not mkt_id:
                trader.poll_resolutions()
                render_dashboard({
                    "market_title": "Waiting for next 5-min block...",
                    "seconds_remaining": 0,
                    "stats": {"total": trader.total_trades, "win_rate": trader.win_rate, "total_pnl": trader.total_pnl},
                    "pending_count": len(trader.pending_resolution)
                })
                time.sleep(1); continue

            # 2. Main Loop for one market
            while True:
                trader.poll_resolutions()
                now = int(time.time())
                sec_rem = end_ts - now
                if sec_rem < -2: break # Time up!
                
                # Fetch prices
                btc_price = get_binance_btc_price()
                strike, _ = get_polymarket_strike("BTC", end_ts - 300)
                price_to_beat = strike or btc_price
                
                # 2. Get Prices (CLOB with Gamma Fallback)
                ub, ua = get_clob_price(up_tok)
                db, da = get_clob_price(dn_tok)
                price_source = "CLOB"
                
                # Fallback to Gamma AMM prices if CLOB is empty
                if ua is None:
                    ua = amm_up
                    price_source = "AMM"
                if da is None:
                    da = amm_dn
                    price_source = "AMM"

                up_true = (ua is not None)
                dn_true = (da is not None)
                
                # 3. Decision Logic
                fv = compute_fair_value(btc_price, price_to_beat, sec_rem)
                sent_ratio, _, w_buy, w_sell = get_binance_sentiment()
                mood = get_historical_streak() # -5 to +5
                
                lag_debug = {}
                active_sig = None
                sig_mode = "LAG" # "LAG" or "TREND"
                PRICE_CAP = 0.85
                
                if up_true and dn_true and fv is not None:
                    gap_up = (fv - ua) * 100
                    gap_dn = ((1.0 - fv) - da) * 100
                    lag_debug = {"up_gap": gap_up, "dn_gap": gap_dn, "mood": mood, "source": price_source}
                    
                    if sec_rem > 35 and abs(btc_price - (price_to_beat or 0)) > BTC_MIN_DISPLACEMENT:
                        # UP Alert: Must have gap, be below 85c, and have BULLISH sentiment
                        if gap_up >= LAG_THRESHOLD_CENTS and ua < PRICE_CAP and sent_ratio > 0.05:
                            active_sig = "Up"
                        # DOWN Alert: Must have gap, be below 85c, and have BEARISH sentiment
                        elif gap_dn >= LAG_THRESHOLD_CENTS and da < PRICE_CAP and sent_ratio < -0.05:
                            active_sig = "Down"

                    if active_sig:
                        if HAS_SOUND and ENABLE_SOUND: winsound.Beep(800, 200)
                        
                        # Send Telegram Alert (Once per market block)
                        if last_alert_slug != slug:
                            last_alert_slug = slug
                            msg = (
                                f"🚨 *BTC SNIPER ALERT: {active_sig.upper()}* 🚀\n\n"
                                f"💰 Profit Gap: `{lag_debug.get('up_gap' if active_sig=='Up' else 'dn_gap',0):+.1f}¢` / {LAG_THRESHOLD_CENTS}¢\n"
                                f"⏱ Time Left: `{sec_rem//60}:{sec_rem%60:02d}`\n"
                                f"🎯 Price: `${btc_price:,.2f}`\n\n"
                                f"🔗 [Trade on Polymarket](https://polymarket.com/event/{slug})"
                            )
                            send_telegram_notification(msg)

                    if not trader.active_trade and active_sig:
                        if active_sig == "Up":
                            trader.enter(slug, "Up", ua, price_to_beat, btc_price, mkt_id, up_idx)
                        else:
                            trader.enter(slug, "Down", da, price_to_beat, btc_price, mkt_id, up_idx)

                if trader.active_trade:
                    cur_mid = ua if trader.active_trade.direction == "Up" else da
                    trader.update(cur_mid, market_expired=(sec_rem < 2))
                    cur_pnl_pct = trader.active_trade.current_pnl_pct(cur_mid) if cur_mid else 0.0
                else:
                    cur_pnl_pct = 0.0

                render_dashboard({
                    "active_signal": active_sig, "sig_mode": sig_mode,
                    "market_title": title, "slug": slug, "seconds_remaining": sec_rem,
                    "price_to_beat": price_to_beat, "btc_price": btc_price,
                    "sentiment_ratio": sent_ratio, "up_price": ua, "dn_price": da,
                    "up_bid": ub, "up_ask": ua, "dn_bid": db, "dn_ask": da,
                    "lag_debug": lag_debug, "active_trade": trader.active_trade.__dict__ if trader.active_trade else None,
                    "cur_pnl_pct": cur_pnl_pct,
                    "stats": {"total": trader.total_trades, "win_rate": trader.win_rate, "total_pnl": trader.total_pnl},
                    "pending_count": len(trader.pending_resolution)
                })
                time.sleep(1)
            
        except KeyboardInterrupt: break
        except Exception as e:
            print(f"Error in loop: {e}"); time.sleep(POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__": 
    print("--- [HEARTBEAT] Bot starting... ---")
    main()
