import datetime
import hashlib
import hmac
import json
import os
import sys
import time
import requests
import pandas as pd
import logging
from typing import Dict, List, Optional
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed

from eth_account import Account
from eth_account.messages import encode_defunct

# --- LOGGING CONFIG ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Redirect all print() statements and uncaught errors to bot.log
class Tee:
    def __init__(self, *files):
        self.files = files
    def write(self, obj):
        for f in self.files:
            try:
                f.write(obj)
                f.flush()
            except:
                pass
    def flush(self):
        for f in self.files:
            try:
                f.flush()
            except:
                pass

log_f = open("bot.log", "a", encoding="utf-8")
sys.stdout = Tee(sys.stdout, log_f)
sys.stderr = Tee(sys.stderr, log_f)

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

_PROXY_URL = os.getenv("PROXY_URL", "").strip()
PROXIES = {"http": _PROXY_URL, "https": _PROXY_URL} if _PROXY_URL else None

def send_telegram_alert(message: str):
    """Sends a markdown/HTML formatted message to Telegram if credentials exist."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
        
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    
    try:
        r = requests.post(url, json=payload, timeout=5)
        if r.status_code != 200:
            print(f'[-] Telegram API {r.status_code}: {r.text}')
    except Exception as e:
        print(f"[Telegram Error] Could not send message: {e}")



class PaperTrader:
    def __init__(self, size_percentage: float = 0.05, max_trade_usd: float = 5.0,
                 db_file="paper_portfolio.json", session=None,
                 live_mode: bool = False, wallet_address: str = None,
                 settings: dict = None):
        self.db_file = db_file
        self.session = session or requests.Session()
        self.live_mode = live_mode
        self.wallet_address = wallet_address
        # Shared settings dict — reads live so changes take effect immediately
        self._settings = settings
        # Fallback values used when no shared settings dict is provided
        self.size_percentage = size_percentage
        self.max_trade_usd = max_trade_usd

        # Set up live-trading signer if needed
        self._live_account = None
        self._api_key = os.getenv("POLYMARKET_API_KEY", "").strip()
        self._api_secret = os.getenv("POLYMARKET_API_SECRET", "").strip()
        self._api_passphrase = os.getenv("POLYMARKET_API_PASSPHRASE", "").strip()
        self._api_address = os.getenv("POLYMARKET_ADDRESS", "").strip()
        if live_mode:
            pk = os.getenv("POLYMARKET_PRIVATE_KEY", "").strip()
            if pk:
                self._live_account = Account.from_key(pk)
            elif self._api_key and self._api_address:
                pass  # use API key auth
            else:
                raise ValueError(
                    "No Polymarket credentials found.\n"
                    "Option 1 — Private key:  set POLYMARKET_PRIVATE_KEY in .env\n"
                    "Option 2 — API key:      set POLYMARKET_API_KEY + POLYMARKET_API_SECRET + "
                    "POLYMARKET_API_PASSPHRASE + POLYMARKET_ADDRESS in .env"
                )

        self.portfolio = self.load_portfolio()

    def load_portfolio(self):
        if os.path.exists(self.db_file):
            with open(self.db_file, 'r') as f:
                data = json.load(f)
            # Back-fill wallet_address if missing
            if self.wallet_address and "wallet_address" not in data:
                data["wallet_address"] = self.wallet_address
            return data
        return {
            "wallet_address": self.wallet_address,
            "virtual_usdc": 50.0,
            "open_positions": [],
            "total_trades_taken": 0,
            "realized_pnl": 0.0,
            "start_pnl": 0.0
        }

    # ------------------------------------------------------------------ #
    # Live order placement                                                 #
    # ------------------------------------------------------------------ #

    def _live_auth_headers(self, method: str, path: str, body: str = "") -> dict:
        ts = str(int(time.time() * 1000))
        if self._live_account:
            # Private key auth (EIP-191)
            msg = ts + method.upper() + path + body
            signed = self._live_account.sign_message(encode_defunct(text=msg))
            return {
                "POLY-ADDRESS": self._live_account.address,
                "POLY-SIGNATURE": signed.signature.hex(),
                "POLY-TIMESTAMP": ts,
                "Content-Type": "application/json",
            }
        elif self._api_secret:
            # Full API key auth (HMAC-SHA256) — key + secret + passphrase
            msg = ts + method.upper() + path + body
            sig = hmac.new(
                self._api_secret.encode("utf-8"),
                msg.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            return {
                "POLY-API-KEY": self._api_key,
                "POLY-SIGNATURE": sig,
                "POLY-TIMESTAMP": ts,
                "POLY-PASSPHRASE": self._api_passphrase,
                "POLY-ADDRESS": self._api_address,
                "Content-Type": "application/json",
            }
        else:
            # API key only (Google/social accounts — no secret/passphrase)
            return {
                "POLY-API-KEY": self._api_key,
                "POLY-ADDRESS": self._api_address,
                "POLY-TIMESTAMP": ts,
                "Content-Type": "application/json",
            }

    def _place_real_order(self, token_id: str, side: str, size: float, price: float) -> dict:
        """Places a real LIMIT/FOK order on the Polymarket CLOB."""
        path = "/order"
        body = {
            "token_id": token_id,
            "side": side,
            "type": "LIMIT",
            "size": str(round(size, 4)),
            "price": str(round(price, 4)),
            "time_in_force": "FOK",
        }
        body_str = json.dumps(body, separators=(",", ":"))
        headers = self._live_auth_headers("POST", path, body_str)
        try:
            r = self.session.post(f"https://clob.polymarket.com{path}",
                                  headers=headers, data=body_str, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error(f"[LIVE ORDER FAILED] {side} {size:.4f} @ {price:.4f} token={token_id[:12]}: {e}")
            return {"error": str(e)}

    def _cfg(self, key: str, fallback):
        """Read from shared settings dict if available, else use fallback."""
        if self._settings is None:
            return fallback
        mode = "live" if self.live_mode else "paper"
        return self._settings.get(mode, {}).get(key, fallback)

    @property
    def _size_pct(self): return self._cfg("size_pct", self.size_percentage)
    @property
    def _max_usd(self): return self._cfg("max_usd", self.max_trade_usd)
    @property
    def _min_whale_usd(self): return self._cfg("min_whale_usd", 0.0)
    @property
    def _max_slippage(self): return self._cfg("max_slippage_pct", 5.0) / 100.0

    def is_underperforming(self, threshold: float = -5.0) -> bool:
        """Returns True if realized PnL is below the trash threshold."""
        return self.portfolio.get("realized_pnl", 0.0) <= threshold

    def is_elite(self, threshold: float = 20.0) -> bool:
        """Returns True if realized PnL is above the hall of fame threshold."""
        return self.portfolio.get("realized_pnl", 0.0) >= threshold

    def save_portfolio(self):
        with open(self.db_file, 'w') as f:
            json.dump(self.portfolio, f, indent=4)

    def _fetch_orderbook(self, asset_id: str) -> dict:
        """Fetch the full orderbook for an asset. Returns empty dict on failure."""
        try:
            url = f"https://clob.polymarket.com/book?token_id={asset_id}"
            res = self.session.get(url, timeout=3)
            if res.status_code != 200:
                return {}
            return res.json()
        except:
            return {}

    def _fetch_live_best_ask(self, asset_id: str) -> tuple:
        """Fetch (price, size) of best ask (lowest sell) from Polymarket CLOB. Returns (0.0, 0.0) on failure."""
        book = self._fetch_orderbook(asset_id)
        asks = book.get("asks", [])
        if not asks:
            return 0.0, 0.0
        best = min(asks, key=lambda x: float(x.get("price", 999)))
        return float(best["price"]), float(best["size"])

    def _fetch_live_best_bid(self, asset_id: str) -> tuple:
        """Fetch (price, size) of best bid (highest buy) from Polymarket CLOB. Returns (0.0, 0.0) on failure."""
        book = self._fetch_orderbook(asset_id)
        bids = book.get("bids", [])
        if not bids:
            return 0.0, 0.0
        best = max(bids, key=lambda x: float(x.get("price", 0)))
        return float(best["price"]), float(best["size"])

    def process_new_trade(self, target_trade: dict):
        """
        Receives a new trade event from the monitored wallet and executes a demo trade.
        Includes 5% slippage check against live Polymarket orderbook.
        """
        tx_hash = target_trade.get("transactionHash", "")
        market_id = target_trade.get("conditionId", "Unknown")
        trade_id = f"{tx_hash}_{market_id}"
        side = target_trade.get("side") # BUY or SELL
        whale_price = float(target_trade.get("price", 0))
        size = float(target_trade.get("size", 0))
        asset_id = target_trade.get("asset", "")
        
        target_usdc_value = whale_price * size
        simulated_usdc_size = min(target_usdc_value * self._size_pct, self._max_usd)

        # --- MIN WHALE TRADE FILTER ---
        if target_usdc_value < self._min_whale_usd:
            print(f"\n[MIN FILTER] Whale trade ${target_usdc_value:.2f} < min ${self._min_whale_usd:.2f}. Skipping.")
            return

        if side == "BUY":
            # Prevent negative balance!
            if self.portfolio["virtual_usdc"] <= 0:
                print(f"\n[DEMO WALLET OUT OF FUNDS] Tried to buy {market_id} but balance is 0!")
                return

            # --- SLIPPAGE & LIQUIDITY CHECK ---
            live_price, live_size = self._fetch_live_best_ask(asset_id)
            exec_price = whale_price  # Default to whale

            if live_price > 0 and whale_price > 0:
                slippage = (live_price - whale_price) / whale_price

                # 1. Price is worse (Positive Slippage)
                if slippage > self._max_slippage:
                    print(f"\n[SLIPPAGE AVOIDED] Price moved {slippage*100:.1f}% higher from {whale_price:.4f} to {live_price:.4f}. Skipping!")
                    msg = f"⚠️ <b>SLIPPAGE AVOIDED</b>\n\nMarket <code>{market_id[:12]}...</code>\nWhale price: {whale_price:.4f}\nLive price: {live_price:.4f}\nDrift: <b>{slippage*100:.1f}%</b> — Too risky, skipped entry."
                    send_telegram_alert(msg)
                    return
                
                # 2. Price is 'Too Good' (Negative Slippage > 15%) 
                # Likely stale or tiny orderbook. We clamp to whale price for realism.
                elif slippage < -0.15:
                    print(f"[*] Live price {live_price:.4f} is >15% below whale {whale_price:.4f}. Using whale price for realism.")
                    exec_price = whale_price
                
                # 3. Check Liquidity
                else:
                    required_tokens = simulated_usdc_size / live_price
                    if live_size < required_tokens:
                        print(f"[*] Insufficient liquidity at {live_price:.4f} (Size: {live_size:.1f} < Required: {required_tokens:.1f}). Using whale price.")
                        exec_price = whale_price
                    else:
                        exec_price = live_price  # Perfect, use live price
            else:
                exec_price = whale_price  # Fallback to whale price if CLOB unavailable

            # If not enough for the full simulation, buy what we can (partial)
            if simulated_usdc_size > self.portfolio["virtual_usdc"]:
                simulated_usdc_size = self.portfolio["virtual_usdc"]
                print(f"[*] Partial buy due to low balance!")

            simulated_token_size = simulated_usdc_size / exec_price if exec_price > 0 else 0

            # --- LIVE ORDER PLACEMENT ---
            if self.live_mode:
                result = self._place_real_order(asset_id, "BUY", simulated_token_size, exec_price)
                if "error" in result:
                    msg = f"⚠️ <b>LIVE ORDER FAILED</b>\n\nMarket <code>{market_id[:12]}...</code>\nError: {result['error']}"
                    send_telegram_alert(msg)
                    return  # Don't update portfolio if the real order failed
                logger.info(f"[LIVE ORDER] BUY placed: {result}")

            self.portfolio["virtual_usdc"] -= simulated_usdc_size
            
            # MERGE: If we already hold this market/asset, add to existing position instead of duplicating
            existing = next((p for p in self.portfolio["open_positions"] if p["market_id"] == market_id and p["asset_id"] == asset_id), None)
            if existing:
                total_tokens = existing["amount_tokens"] + simulated_token_size
                total_invested = existing["amount_usdc_invested"] + simulated_usdc_size
                existing["price_bought"] = total_invested / total_tokens if total_tokens > 0 else exec_price
                existing["amount_tokens"] = total_tokens
                existing["amount_usdc_invested"] = total_invested
            else:
                self.portfolio["open_positions"].append({
                    "market_id": market_id,
                    "asset_id": asset_id,
                    "price_bought": exec_price,
                    "amount_usdc_invested": simulated_usdc_size,
                    "amount_tokens": simulated_token_size,
                    "timestamp": datetime.datetime.now().isoformat()
                })
            self.portfolio["total_trades_taken"] += 1

            slippage_note = ""
            if live_price > 0 and live_price != whale_price:
                diff = (live_price - whale_price) / whale_price * 100
                slippage_note = f" (Live price: {exec_price:.4f}, drift: {diff:+.1f}%)"

            tag = "LIVE BUY" if self.live_mode else "DEMO BUY"
            print(f"\n[{tag}] Target bought ${target_usdc_value:,.2f}. We bought ${simulated_usdc_size:.2f} at {exec_price:.4f}¢{slippage_note}")
            print(f"[{'LIVE' if self.live_mode else 'DEMO'} WALLET] Remaining USDC: ${self.portfolio['virtual_usdc']:,.2f}")
            self.save_portfolio()

            emoji = "🟢" if self.live_mode else "📋"
            msg = f"{emoji} <b>{tag}</b>\n\nTarget bought ${target_usdc_value:,.2f} on <code>{market_id[:12]}...</code>\nWe bought <b>${simulated_usdc_size:.2f}</b> at {exec_price:.4f}¢{slippage_note}\n\n💵 <b>Balance:</b> ${self.portfolio['virtual_usdc']:,.2f}"
            send_telegram_alert(msg)
                
        elif side == "SELL":
            sold_something = False
            for pos in list(self.portfolio["open_positions"]):
                if pos["market_id"] == market_id:
                    asset_id_pos = pos.get("asset_id", "")
                    
                    # Fetch live best bid for our specific asset — this is what we'd actually receive
                    live_sell_price, live_sell_size = self._fetch_live_best_bid(asset_id_pos)
                    
                    # Fallback Logic:
                    exit_price = whale_price
                    if live_sell_price > 0:
                        # Safety: If live bid is suspiciously higher than whale's sell price, fallback 
                        # to ensure we don't 'ghost' profit.
                        if live_sell_price > whale_price * 1.15:
                            exit_price = whale_price
                        # Liquidity: If we can't 'dump' our whole position at this price, use whale price
                        elif live_sell_size < pos["amount_tokens"]:
                            exit_price = whale_price
                        else:
                            exit_price = live_sell_price
                    
                    # Revenue = our tokens * the price we'd get on the market NOW
                    revenue = pos["amount_tokens"] * exit_price
                    profit = revenue - pos["amount_usdc_invested"]
                    
                    self.portfolio["virtual_usdc"] += revenue
                    self.portfolio["realized_pnl"] += profit
                    self.portfolio["open_positions"].remove(pos)
                    print(f"\n[DEMO SELL] Target sold at {whale_price:.4f}. We closed at live bid {exit_price:.4f}. Revenue: ${revenue:.2f} (Profit: ${profit:+.2f})!")
                    print(f"[DEMO WALLET] PnL: ${self.portfolio['realized_pnl']:+.2f} | Balance: ${self.portfolio['virtual_usdc']:,.2f}")
                    sold_something = True
                    self.save_portfolio()
                    
                    msg = f" <b>DEMO SELL</b>\n\nTarget sold. We closed position for ${revenue:.2f} (exit: {exit_price:.4f}).\n <b>Profit:</b> {profit:+.2f}\n\n <b>Total PnL:</b> ${self.portfolio['realized_pnl']:+.2f}\n <b>Balance:</b> ${self.portfolio['virtual_usdc']:,.2f}"
                    send_telegram_alert(msg)
                    break
            if not sold_something:
                print(f"\n[DEMO IGNORE] Target sold {market_id}, but no open position.")

    def check_auto_close_targets(self):
        """
        Polls the CLOB API for the current price of all open positions.
        If price >= 0.99 (Win) or <= 0.01 (Loss), automatically closes the position.
        """
        if not self.portfolio["open_positions"]:
            return
            
        for pos in list(self.portfolio["open_positions"]):
            asset_id = pos.get("asset_id")
            if not asset_id:
                continue

            try:
                url = f"https://clob.polymarket.com/book?token_id={asset_id}"
                res = self.session.get(url, timeout=5)
                if res.status_code != 200:
                    continue

                data = res.json()
                bids = data.get("bids", [])
                
                # If no bids left, the market might be dead or resolved to a loss.
                if not bids:
                    # If empty orderbook, don't assume the market is dead, just wait
                    continue
                    
                highest_bid, bid_size = 0.0, 0.0
                if bids:
                    best_bid_item = max(bids, key=lambda x: float(x.get("price", 0)))
                    highest_bid = float(best_bid_item["price"])
                    bid_size = float(best_bid_item["size"])
                
                # Use a dynamic loss threshold: the position entry price determines the floor
                entry_price = pos.get("price_bought", 0.05)
                loss_threshold = max(0.003, entry_price * 0.2)  # 20% of entry, minimum 0.003
                
                if highest_bid >= 0.99:
                    # We won! Market practically resolved to Yes.
                    revenue = pos["amount_tokens"] * 1.0
                    profit = revenue - pos["amount_usdc_invested"]
                    print(f"\n[DEMO SWEEPER WIN] Market practically at $1.00! We auto-cashed out for ${revenue:.2f} (Profit: ${profit:+.2f}).")
                    self.portfolio["virtual_usdc"] += revenue
                    self.portfolio["realized_pnl"] += profit
                    self.portfolio["open_positions"].remove(pos)
                    self.save_portfolio()
                    
                    msg = f" <b>SWEEPER WIN!</b>\n\nMarket practically at $1.00! Auto-cashed out for ${revenue:.2f}.\n <b>Profit:</b> ${profit:+.2f}\n\n <b>Total PnL:</b> ${self.portfolio['realized_pnl']:+.2f}"
                    send_telegram_alert(msg)
                
                elif highest_bid <= loss_threshold:
                    # We lost. Market basically resolved to No.
                    profit = -pos["amount_usdc_invested"]
                    print(f"\n[DEMO SWEEPER LOSS] Market dropped to $0.01 or less! Position wiped (Profit: ${profit:+.2f}).")
                    self.portfolio["realized_pnl"] += profit
                    self.portfolio["open_positions"].remove(pos)
                    self.save_portfolio()
                    
                    msg = f"💀 <b>SWEEPER LOSS</b>\n\nMarket dropped to $0.01! Position wiped.\n💸 <b>Profit:</b> ${profit:+.2f}\n\n <b>Total PnL:</b> ${self.portfolio['realized_pnl']:+.2f}"
                    send_telegram_alert(msg)
                    
            except Exception as e:
                pass

    def process_target_activity(self, activity: dict):
        """Processes REDEEM and MERGE activity events from the target wallet."""
        act_type = activity.get("type", "")
        if act_type not in ("REDEEM", "MERGE"):
            return

        market_id = activity.get("conditionId", "Unknown")

        for pos in list(self.portfolio["open_positions"]):
            if pos["market_id"] != market_id:
                continue

            if act_type == "REDEEM":
                # Winning redeem pays $1 per token; losing redeem pays $0
                target_usdc_earned = float(activity.get("usdcSize", 0))
                if target_usdc_earned > 0:
                    revenue = pos["amount_tokens"] * 1.0
                    profit = revenue - pos["amount_usdc_invested"]
                    tag, emoji = "REDEEM WIN", "🎉"
                    print(f"\n[{tag}] Market resolved! Cashed out ${revenue:.2f} (Profit: ${profit:+.2f})")
                else:
                    revenue = 0.0
                    profit = -pos["amount_usdc_invested"]
                    tag, emoji = "REDEEM LOSS", "📉"
                    print(f"\n[{tag}] Market resolved to a loss (Profit: ${profit:+.2f})")

            else:  # MERGE — whale exited via merge, treat as sell at current live price
                exit_price, _ = self._fetch_live_best_bid(pos.get("asset_id", ""))
                if exit_price <= 0:
                    exit_price = pos.get("price_bought", 0)  # fallback to entry price
                revenue = pos["amount_tokens"] * exit_price
                profit = revenue - pos["amount_usdc_invested"]
                tag, emoji = "MERGE EXIT", "🔀"
                print(f"\n[{tag}] Whale merged out. We exit at live bid {exit_price:.4f}. Revenue: ${revenue:.2f} (Profit: ${profit:+.2f})")

                # Live mode: place a real sell order
                if self.live_mode and self._live_account:
                    result = self._place_real_order(pos.get("asset_id", ""), "SELL", pos["amount_tokens"], exit_price)
                    if "error" in result:
                        send_telegram_alert(f"⚠️ <b>LIVE MERGE SELL FAILED</b>\n{result['error']}")
                        break

            self.portfolio["virtual_usdc"] += revenue
            self.portfolio["realized_pnl"] += profit
            self.portfolio["open_positions"].remove(pos)
            self.save_portfolio()

            print(f"[WALLET] PnL: ${self.portfolio['realized_pnl']:+.2f} | Balance: ${self.portfolio['virtual_usdc']:,.2f}")
            send_telegram_alert(
                f"{emoji} <b>TARGET {tag}</b>\n\n"
                f"Market: <code>{market_id[:12]}...</code>\n"
                f"Revenue: ${revenue:.2f} | <b>Profit: ${profit:+.2f}</b>\n\n"
                f"Total PnL: <b>${self.portfolio['realized_pnl']:+.2f}</b>"
            )
            break



class LiveMonitor:
    def __init__(self, target_wallets: list, live_mode: bool = False, settings: dict = None):
        # Deduplication to prevent race conditions on portfolio files
        self.target_wallets = sorted(list(set([w.strip() for w in target_wallets if w.strip()])))
        self.base_url = "https://data-api.polymarket.com"
        self.live_mode = live_mode
        self.settings = settings  # shared reference — mutations propagate instantly

        self.session = requests.Session()
        # Increase pool size to match thread count
        adapter = requests.adapters.HTTPAdapter(pool_connections=25, pool_maxsize=25)
        self.session.mount('https://', adapter)
        self.session.mount('http://', adapter)
        if PROXIES:
            self.session.proxies.update(PROXIES)

        # Shared executor for all polling and sweeper tasks
        self.executor = ThreadPoolExecutor(max_workers=min(25, (len(self.target_wallets) * 2) + 1))

        # State per wallet
        self.seen_trade_ids = {wallet: set() for wallet in self.target_wallets}
        self.seen_activity_hashes = {wallet: set() for wallet in self.target_wallets}
        self.traders = {
            wallet: PaperTrader(
                db_file=f"portfolio_{wallet[:8]}.json",
                session=self.session,
                live_mode=live_mode,
                wallet_address=wallet,
                settings=settings,
            )
            for wallet in self.target_wallets
        }
        self.hall_of_fame_file = "memory_hall_of_fame.json"
        self.last_update_id = 0
        self._inline_last_update_id = 0

    def save_to_hall_of_fame(self, wallet: str):
        """Saves a high-performing wallet to the persistent memory file."""
        hall = []
        if os.path.exists(self.hall_of_fame_file):
            with open(self.hall_of_fame_file, 'r') as f:
                hall = json.load(f)
        
        if wallet not in hall:
            hall.append(wallet)
            with open(self.hall_of_fame_file, 'w') as f:
                json.dump(hall, f, indent=4)
            print(f"\n🏆 [HALL OF FAME] Wallet {wallet[:12]} promoted to permanent memory!")
            send_telegram_alert(f"🏆 <b>HALL OF FAME PROMOTION</b>\nWallet <code>{wallet[:12]}...</code> has been saved as a top performer!")

    def perform_maintenance(self, scanner):
        """Checks for underperformers, trashes them, and replaces them with fresh leaderboard winners."""
        trashed_count = 0
        to_trash = []

        for w in list(self.target_wallets):
            t = self.traders[w]

            # 1. Check for Elite Performance (Save to Memory)
            if t.is_elite(threshold=20.0):
                self.save_to_hall_of_fame(w)

            # 2. Collect underperformers — don't mutate while iterating
            if t.is_underperforming(threshold=-5.0):
                to_trash.append(w)

        for w in to_trash:
            t = self.traders[w]
            print(f"\n🗑️ [TRASHING] Wallet {w[:12]} underperformed (PnL: ${t.portfolio['realized_pnl']:.2f}). Replacing...")
            send_telegram_alert(f"🗑️ <b>WALLET TRASHED</b>\nPruning underperformer <code>{w[:12]}...</code> (PnL: ${t.portfolio['realized_pnl']:.2f})")

            # Cleanup — guard against double-removal if maintenance fires concurrently
            if w in self.target_wallets:
                self.target_wallets.remove(w)
            self.traders.pop(w, None)
            self.seen_trade_ids.pop(w, None)
            self.seen_activity_hashes.pop(w, None)
            trashed_count += 1
                
        if trashed_count > 0:
            print(f"[*] Searching for {trashed_count} new replacements...")
            new_winners = []
            offset = 0
            while len(new_winners) < trashed_count:
                candidates = scanner.get_top_traders_24h(limit=20, offset=offset)
                if not candidates: break
                for c in candidates:
                    wallet = c['wallet']
                    if wallet not in self.target_wallets:
                        new_winners.append(wallet)
                        if len(new_winners) >= trashed_count: break
                offset += 20
            
            for new_w in new_winners:
                self.target_wallets.append(new_w)
                self.seen_trade_ids[new_w] = set()
                self.seen_activity_hashes[new_w] = set()
                self.traders[new_w] = PaperTrader(db_file=f"portfolio_{new_w[:8]}.json", session=self.session, live_mode=self.live_mode, wallet_address=new_w, settings=self.settings)
                print(f"✅ [REPLACED] New wallet added: {new_w[:12]}")
                send_telegram_alert(f"✅ <b>NEW WHALE ADDED</b>\nReplaced trashed slot with <code>{new_w[:12]}...</code>")
                # Pre-sync
                self._fetch_latest_trades(new_w, initial_sync=True)
                self._fetch_latest_activity(new_w, initial_sync=True)

    def check_telegram_commands(self, controller: "TelegramController"):
        """Delegate command polling to the shared TelegramController."""
        controller.poll()
            
    def start_monitoring(self, poll_interval_seconds: int = 5,
                         controller: "TelegramController" = None,
                         stop_event=None):
        print("="*50)
        print(f" LIVE {'REAL' if self.live_mode else 'PAPER'}-TRADING MONITOR ")
        print(f" Tracking {len(self.target_wallets)} Wallets Simultaneously (Multi-threaded)")
        print(f" Polling API every {poll_interval_seconds} seconds...")
        print("="*50)

        def sync_wallet(w):
            print(f"[*] Syncing historical trades for {w[:8]}...")
            self._fetch_latest_trades(w, initial_sync=True)
            self._fetch_clob_trades(w, initial_sync=True)
            self._fetch_latest_activity(w, initial_sync=True)

        with ThreadPoolExecutor(max_workers=10) as executor:
            executor.map(sync_wallet, self.target_wallets)

        print(f"\n[!] Sync complete. Watching for new LIVE trades now.\n")

        mode_tag = "🟢 LIVE TRADING" if self.live_mode else "📋 PAPER RACE"
        start_msg = f"{mode_tag} <b>STARTED!</b>\n\nTracking these whales:\n"
        for w in self.target_wallets:
            start_msg += f"• <a href='https://polymarket.com/profile/{w}'>{w[:10]}...</a>\n"
        start_msg += "\n<i>/status · /pnl · /stop</i>"
        send_telegram_alert(start_msg)

        last_sweeper_check = time.time()
        last_maintenance_check = time.time()

        # Instantiate a scanner for the maintenance loop to use
        maint_scanner = Scanner(session=self.session)

        import threading
        _stop = stop_event or threading.Event()

        while not _stop.is_set():
            try:
                # Telegram polling is handled by TelegramController in its own loop;
                # only fall back to inline polling when running without a controller.
                if controller is None:
                    self._inline_poll_commands()

                    
                def poll_wallet(w):
                    self._fetch_latest_trades(w)
                    self._fetch_clob_trades(w)
                    self._fetch_latest_activity(w)

                # Use persistent executor mapping
                list(self.executor.map(poll_wallet, list(self.target_wallets)))

                # Run the auto-close sweeper every 15 seconds to avoid API ratelimits
                if time.time() - last_sweeper_check > 15:
                    list(self.executor.map(lambda w: self.traders[w].check_auto_close_targets(), list(self.target_wallets)))
                    last_sweeper_check = time.time()

                # Run maintenance (Trash & Replace) every 30 minutes
                if time.time() - last_maintenance_check > 1800:
                    self.perform_maintenance(maint_scanner)
                    last_maintenance_check = time.time()

                _stop.wait(poll_interval_seconds)
            except KeyboardInterrupt:
                logger.info("Stopping monitor.")
                self.print_closing_leaderboard()
                self.shutdown()
                break
            except Exception as e:
                logger.error(f"Error during polling loop: {e}")
                _stop.wait(poll_interval_seconds)

    def _inline_poll_commands(self):
        """Minimal inline Telegram poll used when running without TelegramController."""
        if not TELEGRAM_BOT_TOKEN:
            return
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
        try:
            res = requests.get(url, params={"offset": self._inline_last_update_id}, timeout=3)
            data = res.json()
            if not data.get("ok"):
                return
            for update in data.get("result", []):
                self._inline_last_update_id = update["update_id"] + 1
                text = update.get("message", {}).get("text", "").strip()
                if text == "/status":
                    reply = "<b>STATUS</b>\n\n"
                    for w in self.target_wallets:
                        t = self.traders[w]
                        reply += (f"<a href='https://polymarket.com/profile/{w}'>{w[:10]}...</a>\n"
                                  f"  Bal: ${t.portfolio['virtual_usdc']:.2f} | "
                                  f"PnL: <b>${t.portfolio['realized_pnl']:+.2f}</b> | "
                                  f"Open: {len(t.portfolio.get('open_positions', []))}\n\n")
                    send_telegram_alert(reply)
        except Exception:
            pass

    def shutdown(self):
        """Cleanly close session and executor."""
        self.executor.shutdown(wait=False)
        self.session.close()

    def print_closing_leaderboard(self):
        print("\n" + "="*70)
        print(" 🏁 DEMO RACE CONCLUDED: FINAL LEADERBOARD 🏁 ")
        print("="*70)
        
        results = []
        for w in self.target_wallets:
            t = self.traders[w]
            usdc = t.portfolio.get("virtual_usdc", 0.0)
            realized_pnl = t.portfolio.get("realized_pnl", 0.0)
            
            unrealized_pnl = 0.0
            open_count = len(t.portfolio.get("open_positions", []))
            
            if open_count > 0:
                print(f"[*] Calculating live value for {open_count} open positions of {w[:8]}...")
                for pos in t.portfolio["open_positions"]:
                    asset_id = pos.get("asset_id")
                    if not asset_id: continue
                    highest_bid = 0.0
                    try:
                        res = requests.get(f"https://clob.polymarket.com/book?token_id={asset_id}", timeout=3)
                        if res.status_code == 200:
                            bids = res.json().get("bids", [])
                            if bids:
                                highest_bid = max([float(b["price"]) for b in bids])
                    except:
                        pass
                        
                    if highest_bid > 0:
                        current_value = pos["amount_tokens"] * highest_bid
                        unrealized_pnl += (current_value - pos["amount_usdc_invested"])

            total_net_pnl = realized_pnl + unrealized_pnl
            results.append({
                "wallet": w, 
                "usdc": usdc, 
                "realized": realized_pnl, 
                "unrealized": unrealized_pnl, 
                "net": total_net_pnl
            })
            
        # Sort by best Total Net PnL
        results.sort(key=lambda x: x["net"], reverse=True)
        
        for idx, res in enumerate(results):
            medal = "🥇" if idx == 0 else "🥈" if idx == 1 else "🥉" if idx == 2 else "  "
            print(f"{medal} [{idx+1}] Wallet: {res['wallet'][:15]}...")
            print(f"      Balance: ${res['usdc']:,.2f} | Realized PnL: ${res['realized']:+.2f}")
            if res['unrealized'] != 0.0:
                print(f"      Unrealized PnL (Open Pos): ${res['unrealized']:+.2f}")
            print(f"      => Total Net PnL: ${res['net']:+.2f}\n")
            
        print("="*70 + "\n")

    def _fetch_latest_activity(self, wallet: str, initial_sync=False):
        url = f"{self.base_url}/activity"
        params = {"user": wallet, "limit": 50, "_t": int(time.time() * 1000)}

        try:
            response = self.session.get(url, params=params, timeout=20)
            if response.status_code == 429:
                print(f"[!] Rate Limited (429) fetching activity for {wallet[:8]}! Slowing down...")
                return
            if response.status_code != 200:
                print(f'[-] API Error {response.status_code} fetching activity for {wallet[:8]}')
                return
            activities = response.json()
            if not activities:
                return

            seen = self.seen_activity_hashes[wallet]
            for act in reversed(activities):
                act_hash = act.get("transactionHash")
                if act_hash and act_hash not in seen:
                    seen.add(act_hash)
                    if not initial_sync and act.get("type") in ("REDEEM", "MERGE"):
                        self.traders[wallet].process_target_activity(act)
            # Cap to last 2000 hashes to prevent unbounded growth
            if len(seen) > 2000:
                self.seen_activity_hashes[wallet] = set(list(seen)[-2000:])
        except Exception as e:
            print(f"[-] Activity Fetch Error: {e}")

    def _fetch_latest_trades(self, wallet: str, initial_sync=False):
        url = f"{self.base_url}/trades"
        params = {"user": wallet, "limit": 50, "_t": int(time.time() * 1000)}

        for attempt in range(3):
            try:
                response = self.session.get(url, params=params, timeout=20)
                break
            except Exception as e:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                print(f"[-] Trade Fetch Error after 3 attempts: {e}")
                return
        try:
            if response.status_code == 429:
                print(f"[!] Rate Limited (429) fetching trades for {wallet[:8]}! We might miss fast trades.")
                return
            if response.status_code != 200:
                print(f'[-] API Error {response.status_code} fetching trades for {wallet[:8]}')
                return
            trades = response.json()
            if not trades:
                return

            seen = self.seen_trade_ids[wallet]
            backfill_cutoff = time.time() - 1800  # backfill trades from last 30 min on initial sync
            for trade in reversed(trades):
                tx_hash = trade.get("transactionHash", "")
                market_id = trade.get("conditionId", "")
                trade_id = f"{tx_hash}_{market_id}"

                if tx_hash and trade_id not in seen:
                    seen.add(trade_id)
                    trade_ts = int(trade.get("timestamp", 0))
                    if not initial_sync or trade_ts >= backfill_cutoff:
                        print(f"[*] {'[Backfill] ' if initial_sync else ''}New Trade by {wallet[:8]}: {trade.get('side')} ${float(trade.get('price', 0)) * float(trade.get('size', 0)):.2f}")
                        self.traders[wallet].process_new_trade(trade)
            # Cap to last 2000 IDs to prevent unbounded growth
            if len(seen) > 2000:
                self.seen_trade_ids[wallet] = set(list(seen)[-2000:])
        except Exception as e:
            print(f"[-] Trade Fetch Error: {e}")

    def _fetch_clob_trades(self, wallet: str, initial_sync=False):
        """Poll CLOB API directly for faster trade detection (less indexing lag)."""
        try:
            url = "https://clob.polymarket.com/trades"
            params = {"maker_address": wallet, "limit": 50}
            response = self.session.get(url, params=params, timeout=20)
            if response.status_code != 200:
                return
            data = response.json()
            trades = data if isinstance(data, list) else data.get("data", [])
            if not trades:
                return

            seen = self.seen_trade_ids[wallet]
            backfill_cutoff = time.time() - 1800
            for trade in reversed(trades):
                tx_hash = trade.get("transaction_hash", trade.get("transactionHash", ""))
                market_id = trade.get("market", trade.get("conditionId", ""))
                trade_id = f"{tx_hash}_{market_id}_clob"
                if tx_hash and trade_id not in seen:
                    seen.add(trade_id)
                    trade_ts = int(trade.get("timestamp", 0))
                    if initial_sync and trade_ts < backfill_cutoff:
                        continue
                    # Normalize to data-api format
                    side = trade.get("side", "BUY").upper()
                    price = float(trade.get("price", 0))
                    size = float(trade.get("size", trade.get("original_size", 0)))
                    asset = trade.get("asset_id", trade.get("outcome_index", ""))
                    normalized = {
                        "transactionHash": tx_hash,
                        "conditionId": market_id,
                        "side": side,
                        "price": price,
                        "size": size,
                        "asset": str(asset),
                        "timestamp": trade_ts,
                    }
                    print(f"[*] [CLOB] {'[Backfill] ' if initial_sync else ''}New Trade by {wallet[:8]}: {side} ${price * size:.2f}")
                    self.traders[wallet].process_new_trade(normalized)
        except Exception as e:
            print(f"[-] CLOB Trade Fetch Error: {e}")




class Scanner:
    def __init__(self, session=None):
        self.base_url = "https://data-api.polymarket.com/v1"
        self.session = session or requests.Session()

    def get_top_traders_24h(self, limit: int = 20, offset: int = 0, category: str = "OVERALL") -> List[Dict]:
        """
        Fetches the top traders from the Polymarket leaderboard for the past 24 hours.
        """
        url = f"{self.base_url}/leaderboard"
        params = {
            "window": "1d", # Sometimes timePeriod=1d or DAY
            "limit": limit,
            "offset": offset
        }
        
        try:
            response = self.session.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            
            traders = []
            for item in data:
                proxy_wallet = item.get("proxyWallet")
                if proxy_wallet:
                    traders.append({
                        "name": item.get("userName", "Unknown"),
                        "wallet": proxy_wallet, # Useful for fetching individual trades later
                        "pnl": item.get("pnl", 0),
                        "volume": item.get("vol", 0)
                    })
            
            print(f"[+] Found {len(traders)} top traders in the past 24h.")
            return traders
            
        except requests.exceptions.RequestException as e:
            print(f"[-] Error fetching leaderboard: {e}")
            return []




class Backtester:
    def __init__(self, size_percentage: float = 0.05, max_trade_usd: float = 5.0, session=None):
        self.size_percentage = size_percentage
        self.max_trade_usd = max_trade_usd
        self.base_url = "https://data-api.polymarket.com"
        self.session = session or requests.Session()

    def fetch_user_trades(self, wallet: str) -> pd.DataFrame:
        """
        Fetches the recent trades of a given wallet.
        """
        # print(f"    [*] Fetching recent trades for {wallet}...")
        url = f"{self.base_url}/trades"
        params = {"user": wallet, "limit": 100} # Get last 100 trades for preview
        
        try:
            response = self.session.get(url, params=params)
            if response.status_code != 200:
                return pd.DataFrame()
                
            trades = response.json()
            if not trades:
                return pd.DataFrame()
            
            # Convert to Pandas DataFrame
            df = pd.DataFrame(trades)
            # Make sure price and size columns are numeric
            df['size'] = pd.to_numeric(df['size'], errors='coerce')
            df['price'] = pd.to_numeric(df['price'], errors='coerce')
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s') if 'timestamp' in df.columns else pd.Timestamp.now()
            
            # calculate the USD value of the trade (size = number of outcome tokens)
            df['usdc_value'] = df['size'] * df['price']
            
            return df
            
        except Exception as e:
            # print(f"    [-] Error fetching trades: {e}")
            return pd.DataFrame()

    def simulate_copy_trading(self, wallet: str) -> Dict:
        """
        Runs the max $5, 5% strategy on the user's historical trades.
        Returns a summary dictionary with advanced analytics.
        """
        df = self.fetch_user_trades(wallet)
        if df.empty:
            return {"simulated_volume_usd": 0, "trades_analyzed": 0, "details": "No data"}
            
        # Filter only BUY trades to simulate our bot 'entering' positions
        buys = df[df['side'] == 'BUY'].copy()
        
        if buys.empty:
             return {"simulated_volume_usd": 0, "trades_analyzed": 0, "details": "No BUYs found"}
        

        # Calculate time span
        now_time = pd.Timestamp.now(tz='UTC').replace(tzinfo=None)
        min_time = buys['timestamp'].min()
        max_time = buys['timestamp'].max()
        time_diff = max_time - min_time
        hours_diff = time_diff.total_seconds() / 3600.0 if pd.notnull(time_diff) else 1
        
        # STRICT 24 HOUR FILTER
        time_limit = pd.Timestamp.now(tz='UTC').replace(tzinfo=None) - pd.Timedelta(hours=24)
        buys = buys[buys['timestamp'] >= time_limit].copy()
        
        if buys.empty:
             return {"simulated_volume_usd": 0, "trades_analyzed": 0, "details": "No BUYs in last 24h"}
             
        # --- Advanced Analytics for the Whale ---
        avg_buy_size = buys['usdc_value'].mean()
        max_buy_size = buys['usdc_value'].max()
        
        if hours_diff > 0:
            trades_per_hour = len(buys) / hours_diff
        else:
            trades_per_hour = len(buys)
            
        # --- Our Simulation Logic ---
        # Rule: 5% of target's USDC value, capped at Target Max
        buys['simulated_usd_size'] = buys['usdc_value'] * self.size_percentage
        buys['simulated_usd_size'] = buys['simulated_usd_size'].clip(upper=self.max_trade_usd)
        
        total_sim_volume = buys['simulated_usd_size'].sum()
        trades_analyzed = len(buys)
        
        stats_msg = (
            f"Analyzed {trades_analyzed} recent buys over ~{hours_diff:.1f} hours.\n"
            f"       Whale Avg Buy: ${avg_buy_size:,.2f} | Max Buy: ${max_buy_size:,.2f}\n"
            f"       Trading Freq: ~{trades_per_hour:.1f} trades/hour (1 trade every ~{60/trades_per_hour if trades_per_hour>0 else 0:.1f} mins)\n"
            f"       => With our rules ($5 max), we would have invested ${total_sim_volume:,.2f}."
        )
        
        return {
            "trades_analyzed": trades_analyzed,
            "simulated_volume_usd": total_sim_volume,
            "details": stats_msg
        }



class LedgerBacktester:
    def __init__(self, size_percentage: float = 0.05, max_trade_usd: float = 5.0, session=None):
        self.size_percentage = size_percentage
        self.max_trade_usd = max_trade_usd
        self.base_url = "https://data-api.polymarket.com"
        self.session = session or requests.Session()

    def get_recent_active_conditions(self, wallet: str) -> tuple:
        """Fetch trades from last 24 hours. Returns (active_conditions_set, trades_per_hour)"""
        url = f"{self.base_url}/trades"
        try:
            res = self.session.get(url, params={"user": wallet, "limit": 300})
            if res.status_code != 200: return set(), 0.0
            trades = res.json()
            if not trades: return set(), 0.0
            
            active_conditions = set()
            now = datetime.datetime.now(datetime.timezone.utc)
            # Count UNIQUE transactions only (not Polymarket bundle sub-fills)
            unique_tx_hashes = set()
            unique_tx_timestamps = []

            for t in trades:
                tx_hash = t.get('transactionHash', '')
                ts = int(t.get('timestamp', 0))
                trade_time = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
                if (now - trade_time).total_seconds() <= 86400: # 24h
                    if tx_hash and tx_hash not in unique_tx_hashes:
                        unique_tx_hashes.add(tx_hash)
                        unique_tx_timestamps.append(ts)
                    if t.get('conditionId'):
                        active_conditions.add(t.get('conditionId'))

            trades_per_hour = 0.0
            n = len(unique_tx_timestamps)
            if n >= 2:
                # To prevent artificially inflating frequency for accounts that did 3 trades in 1 minute,
                # we calculate their TRUE daily average (divided by flat 24 hours).
                trades_per_hour = n / 24.0

            return active_conditions, trades_per_hour
        except:
            return set(), 0.0

    def analyze_roi(self, wallet: str) -> Dict:
        """
        Calculates exact Simulated PnL and ROI using the positions API.
        Only considers positions that had trade activity in the last 24 hours.
        """
        active_conditions, trades_per_hour = self.get_recent_active_conditions(wallet)
        if not active_conditions:
            return {}
            
        url = f"{self.base_url}/positions?user={wallet}"
        try:
            response = self.session.get(url)
            if response.status_code != 200:
                return {}
                
            positions = response.json()
            if not positions:
                return {}
                
            total_invested = 0.0
            total_pnl = 0.0
            valid_positions = 0
            
            for pos in positions:
                condition_id = pos.get('conditionId')
                if condition_id not in active_conditions:
                    continue  # SKIP POSITIONS NOT TRADED IN LAST 24H
                    
                initial_value = float(pos.get("initialValue", 0))
                percent_pnl = float(pos.get("percentPnl", 0) or 0)
                
                if initial_value <= 0:
                    continue
                    
                valid_positions += 1
                
                # Rule: 5% of target's USDC value, capped at Target Max
                simulated_investment = min(initial_value * self.size_percentage, self.max_trade_usd)
                simulated_profit = simulated_investment * (percent_pnl / 100.0)
                
                total_invested += simulated_investment
                total_pnl += simulated_profit

            if total_invested == 0:
                return {}
                
            roi_percentage = (total_pnl / total_invested) * 100
            
            return {
                "positions_analyzed": valid_positions,
                "simulated_invested": total_invested,
                "simulated_pnl": total_pnl,
                "roi_percentage": roi_percentage,
                "trades_per_hour": trades_per_hour
            }
            
        except Exception as e:
            return {}



def get_category_leaderboard(category="SPORTS", limit=5):
    url = "https://data-api.polymarket.com/v1/leaderboard"
    params = {"limit": limit, "timePeriod": "DAY", "category": category.upper(), "orderBy": "PNL"}
    try:
        return requests.get(url, params=params).json()
    except:
        return []

# ======================================================================
# TELEGRAM CONTROLLER — full phone control
# ======================================================================

class TelegramController:
    """
    Runs as the main loop. The bot is controlled entirely via Telegram commands.

    Commands
    --------
    /help               — show this list
    /status             — wallet stats for all tracked wallets
    /pnl                — live unrealized + realized PnL breakdown
    /portfolios         — rank all saved portfolio files by PnL
    /autopilot          — hunt top leaderboard whales (>=20% ROI, >=2/hr) and paper-trade them
    /track <addr,...>   — paper-trade one or more specific wallet addresses
    /promote <n>        — promote wallet #n from /portfolios into paper monitoring
    /golive <n>         — promote wallet #n from /portfolios to REAL order execution
    /confirmgolive      — confirm go-live within 60 s after /golive (safety gate)
    /stop               — stop current monitor and print leaderboard
    /positions          — list every open paper/live position across all wallets
    """

    HELP = (
        "<b>📱 BOT COMMANDS</b>\n\n"
        "<b>Monitor</b>\n"
        "/autopilot — hunt &amp; paper-trade top whales\n"
        "/paperall — paper-trade all promoted wallets (≥50%)\n"
        "/track &lt;addr&gt; — paper-trade specific wallet(s)\n"
        "/stop — stop current monitor\n\n"
        "<b>Stats</b>\n"
        "/status — wallet stats\n"
        "/pnl — live PnL breakdown\n"
        "/portfolios — promoted wallets (≥50% return)\n"
        "/positions — open positions with live price &amp; % change\n\n"
        "<b>Actions</b>\n"
        "/sell &lt;n&gt; — manually close position #n\n"
        "/promote &lt;n&gt; — paper monitor portfolio #n\n"
        "/golive &lt;n&gt; — real-trade portfolio #n\n"
        "/confirmgolive — confirm go-live (60 s window)\n"
        "/name &lt;n&gt; &lt;nickname&gt; — name a promoted wallet\n\n"
        "<b>Settings</b>\n"
        "/settings — view current settings\n"
        "/set paper|live size|max|min|slippage &lt;value&gt;\n"
        "  size = % of whale trade to copy\n"
        "  max = max USDC per trade\n"
        "  min = min whale trade size to copy\n"
        "  slippage = max slippage % before skipping\n"
    )

    SETTINGS_FILE = "bot_settings.json"
    NAMES_FILE = "wallet_names.json"

    DEFAULT_SETTINGS = {
        "paper": {"size_pct": 0.05, "max_usd": 10.0, "min_whale_usd": 0.0, "max_slippage_pct": 5.0},
        "live":  {"size_pct": 0.05, "max_usd": 10.0, "min_whale_usd": 0.0, "max_slippage_pct": 5.0},
    }

    def __init__(self):
        if not TELEGRAM_BOT_TOKEN:
            raise ValueError("TELEGRAM_BOT_TOKEN not set — cannot run in Telegram mode.")

        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=10, pool_maxsize=10)
        self.session.mount("https://", adapter)
        if PROXIES:
            self.session.proxies.update(PROXIES)

        self.last_update_id = 0
        self._monitor: Optional[LiveMonitor] = None
        self._monitor_thread = None
        self._stop_event = None

        # Pending go-live confirmation state
        self._pending_golive_wallet: Optional[str] = None
        self._pending_golive_expires: float = 0.0

        # Persistent settings & names (mutable dicts — shared by reference with traders)
        self.settings = self._load_settings()
        self.wallet_names = self._load_names()

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Settings & names persistence                                         #
    # ------------------------------------------------------------------ #

    def _load_settings(self) -> dict:
        if os.path.exists(self.SETTINGS_FILE):
            try:
                with open(self.SETTINGS_FILE) as f:
                    data = json.load(f)
                # Merge with defaults so new keys are always present
                for mode in ("paper", "live"):
                    for k, v in self.DEFAULT_SETTINGS[mode].items():
                        data.setdefault(mode, {})[k] = data.get(mode, {}).get(k, v)
                return data
            except Exception:
                pass
        return json.loads(json.dumps(self.DEFAULT_SETTINGS))  # deep copy

    def _save_settings(self):
        with open(self.SETTINGS_FILE, "w") as f:
            json.dump(self.settings, f, indent=2)

    def _load_names(self) -> dict:
        if os.path.exists(self.NAMES_FILE):
            try:
                with open(self.NAMES_FILE) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_names(self):
        with open(self.NAMES_FILE, "w") as f:
            json.dump(self.wallet_names, f, indent=2)

    def _wallet_label(self, wallet: str) -> str:
        """Returns an HTML clickable link using nickname if set, else short address."""
        name = self.wallet_names.get(wallet, wallet[:14] + "...")
        return f"<a href='https://polymarket.com/profile/{wallet}'>{name}</a>"

    # ------------------------------------------------------------------ #
    # Telegram API helpers                                                 #
    # ------------------------------------------------------------------ #

    def send(self, text: str):
        send_telegram_alert(text)

    def _send_with_keyboard(self, text: str, keyboard: list) -> Optional[int]:
        """Send a message with inline keyboard. Returns message_id."""
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        try:
            r = self.session.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": keyboard},
            }, timeout=5)
            return r.json().get("result", {}).get("message_id")
        except Exception:
            return None

    def _edit_message(self, message_id: int, text: str, keyboard: list = None):
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
        }
        if keyboard is not None:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        try:
            self.session.post(url, json=payload, timeout=5)
        except Exception:
            pass

    def _answer_callback(self, callback_query_id: str, text: str = ""):
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
        try:
            self.session.post(url, json={"callback_query_id": callback_query_id, "text": text}, timeout=5)
        except Exception:
            pass

    def _monitor_running(self) -> bool:
        return self._monitor_thread is not None and self._monitor_thread.is_alive()

    SESSION_FILE = "active_session.json"

    def _save_session(self, wallets: list, live_mode: bool):
        try:
            with open(self.SESSION_FILE, "w") as f:
                json.dump({"wallets": wallets, "live_mode": live_mode}, f, indent=2)
        except Exception as e:
            logger.warning(f"Could not save session: {e}")

    def _clear_session(self):
        try:
            if os.path.exists(self.SESSION_FILE):
                os.remove(self.SESSION_FILE)
        except Exception:
            pass

    def _load_session(self) -> Optional[dict]:
        if not os.path.exists(self.SESSION_FILE):
            return None
        try:
            with open(self.SESSION_FILE) as f:
                data = json.load(f)
            if data.get("wallets"):
                return data
        except Exception:
            pass
        return None

    def _start_monitor(self, wallets: list, live_mode: bool = False):
        """Spin up a LiveMonitor in a background thread and persist the session."""
        if self._monitor_running():
            self._stop_monitor()

        import threading
        self._stop_event = threading.Event()
        self._monitor = LiveMonitor(wallets, live_mode=live_mode, settings=self.settings)

        # Full reset on new session — clean $50 slate, no carry-over from previous runs
        for trader in self._monitor.traders.values():
            trader.portfolio["open_positions"] = []
            trader.portfolio["virtual_usdc"] = 50.0
            trader.portfolio["realized_pnl"] = 0.0
            trader.portfolio["total_trades_taken"] = 0
            trader.save_portfolio()

        self._save_session(wallets, live_mode)

        def _run():
            self._monitor.start_monitoring(
                poll_interval_seconds=5,
                controller=self,
                stop_event=self._stop_event,
            )

        self._monitor_thread = threading.Thread(target=_run, daemon=True)
        self._monitor_thread.start()

    def _stop_monitor(self, clear_session: bool = True):
        if self._monitor:
            if self._stop_event:
                self._stop_event.set()
            self._monitor.print_closing_leaderboard()
            self._monitor.shutdown()
            self._monitor = None
            self._monitor_thread = None
            self._stop_event = None
        if clear_session:
            self._clear_session()

    def _sorted_portfolios(self) -> list:
        entries = []
        for fname in os.listdir("."):
            if not (fname.startswith("portfolio_") and fname.endswith(".json")):
                continue
            try:
                with open(fname) as f:
                    data = json.load(f)
                wallet = data.get("wallet_address") or fname.replace("portfolio_", "").replace(".json", "")
                entries.append({
                    "wallet": wallet,
                    "pnl": data.get("realized_pnl", 0.0),
                    "balance": data.get("virtual_usdc", 0.0),
                    "open": len(data.get("open_positions", [])),
                    "trades": data.get("total_trades_taken", 0),
                })
            except Exception:
                continue
        entries.sort(key=lambda x: x["pnl"], reverse=True)
        return entries

    def _calc_pnl_report(self) -> str:
        if not self._monitor:
            return "No monitor running."
        reply = "<b>LIVE PnL REPORT</b>\n\n"
        for w in self._monitor.target_wallets:
            t = self._monitor.traders[w]
            realized = t.portfolio.get("realized_pnl", 0.0)
            balance = t.portfolio.get("virtual_usdc", 0.0)
            positions = t.portfolio.get("open_positions", [])
            unrealized = 0.0
            priced = 0
            for pos in positions:
                asset_id = pos.get("asset_id")
                if not asset_id:
                    continue
                try:
                    r = self.session.get(
                        f"https://clob.polymarket.com/book?token_id={asset_id}", timeout=3
                    )
                    if r.status_code == 200:
                        bids = r.json().get("bids", [])
                        if bids:
                            best_bid = max(float(b["price"]) for b in bids)
                            unrealized += pos["amount_tokens"] * best_bid - pos["amount_usdc_invested"]
                            priced += 1
                except Exception:
                    pass
            open_cost = sum(p["amount_usdc_invested"] for p in positions)
            total = balance + open_cost + unrealized
            pct = ((total - 50.0) / 50.0) * 100
            reply += (
                f"<a href='https://polymarket.com/profile/{w}'>{w[:10]}...</a>\n"
                f"  Realized: <b>${realized:+.2f}</b>\n"
                f"  Unrealized: <b>${unrealized:+.2f}</b> ({priced}/{len(positions)})\n"
                f"  Cash: ${balance:.2f} | Total: <b>${total:.2f}</b> ({pct:+.1f}%)\n\n"
            )
        return reply

    # ------------------------------------------------------------------ #
    # Command handlers                                                     #
    # ------------------------------------------------------------------ #

    def _cmd_status(self):
        if not self._monitor_running():
            self.send("⚪ No monitor running. Use /autopilot or /track.")
            return
        reply = f"<b>{'🟢 LIVE' if self._monitor.live_mode else '📋 PAPER'} STATUS</b>\n\n"
        for w in self._monitor.target_wallets:
            t = self._monitor.traders[w]
            reply += (
                f"<a href='https://polymarket.com/profile/{w}'>{w[:10]}...</a>\n"
                f"  Bal: ${t.portfolio['virtual_usdc']:.2f} | "
                f"PnL: <b>${t.portfolio['realized_pnl']:+.2f}</b> | "
                f"Open: {len(t.portfolio.get('open_positions', []))}\n\n"
            )
        self.send(reply)

    def _cmd_pnl(self):
        if not self._monitor_running():
            self.send("⚪ No monitor running.")
            return
        self.send("⏳ Fetching orderbook prices...")
        self.send(self._calc_pnl_report())

    STARTING_BALANCE = 50.0
    PROMOTE_THRESHOLD = 0.50  # 50% return on starting balance

    def _promoted_entries(self) -> list:
        return [
            e for e in self._sorted_portfolios()
            if e["pnl"] / self.STARTING_BALANCE >= self.PROMOTE_THRESHOLD
        ]

    def _cmd_portfolios(self):
        all_entries = self._sorted_portfolios()
        entries = self._promoted_entries()

        if not entries:
            self.send(
                f"🏆 <b>PROMOTED LIST</b>\n\n"
                f"No wallets have hit the <b>+50% return</b> threshold yet.\n"
                f"({len(all_entries)} wallets tracked total — keep watching!)"
            )
            return

        reply = f"🏆 <b>PROMOTED LIST</b> (≥50% return)\n\n"
        for i, e in enumerate(entries):
            w = e["wallet"]
            pct = (e["pnl"] / self.STARTING_BALANCE) * 100
            reply += (
                f"<b>{i+1}.</b> <a href='https://polymarket.com/profile/{w}'>{w[:14]}...</a>\n"
                f"   Return: <b>+{pct:.1f}%</b> (${e['pnl']:+.2f}) | "
                f"Trades: {e['trades']} | Open: {e['open']}\n\n"
            )
        reply += "<i>/paperall · /promote &lt;n&gt; · /golive &lt;n&gt;</i>"
        self.send(reply)

    def _cmd_paperall(self):
        entries = self._promoted_entries()
        if not entries:
            self.send("🏆 No promoted wallets yet (none above +50%). Keep running /autopilot!")
            return
        if self._monitor_running():
            self.send("⚠️ Monitor already running. /stop first.")
            return
        wallets = [e["wallet"] for e in entries if len(e["wallet"]) >= 20]
        if not wallets:
            self.send("⚠️ Promoted wallets have incomplete addresses. Run the bot with the latest code to fix.")
            return
        self.send(
            f"📋 <b>PAPER TRADING ALL {len(wallets)} PROMOTED WALLETS</b>\n\n" +
            "\n".join(f"• <a href='https://polymarket.com/profile/{w}'>{w[:14]}...</a>" for w in wallets)
        )
        self._start_monitor(wallets, live_mode=False)

    def _cmd_autopilot(self):
        if self._monitor_running():
            self.send("⚠️ Monitor already running. /stop first.")
            return
        self.send("🔍 <b>AUTO-PILOT STARTING</b>\n\nHunting top whales (>=8% ROI, >=0.3 trades/hr)...\nThis may take up to 5 minutes.")

        import threading

        def _hunt():
            session = self.session
            scanner = Scanner(session=session)
            ledger = LedgerBacktester(size_percentage=0.05, max_trade_usd=5.0, session=session)
            winners = []

            hof_file = "memory_hall_of_fame.json"
            if os.path.exists(hof_file):
                with open(hof_file) as f:
                    hall_wallets = json.load(f)
                for hw in hall_wallets:
                    winners.append({"wallet": hw})

            offset = 0
            start = time.time()
            while len(winners) < 15 and time.time() - start < 300:
                traders = scanner.get_top_traders_24h(limit=50, offset=offset)
                if not traders:
                    break

                def _eval(t):
                    res = ledger.analyze_roi(t["wallet"])
                    return t, res

                with ThreadPoolExecutor(max_workers=20) as ex:
                    results = list(ex.map(_eval, traders))

                for t, res in results:
                    roi = res.get("roi_percentage", 0)
                    freq = res.get("trades_per_hour", 0)
                    if roi >= 8.0 and freq >= 0.3 and t["wallet"] not in [w["wallet"] for w in winners]:
                        winners.append({"wallet": t["wallet"], "roi": roi, "freq": freq})
                        if len(winners) >= 15:
                            break
                offset += 50

            if not winners:
                self.send("❌ No qualifying whales found. Try again later.")
                return

            wallets = [w["wallet"] for w in winners]
            self.send(f"✅ Found <b>{len(wallets)}</b> whales. Starting paper monitor...")
            self._start_monitor(wallets, live_mode=False)

        threading.Thread(target=_hunt, daemon=True).start()

    def _cmd_track(self, args: str):
        wallets = [w.strip() for w in args.split(",") if w.strip()]
        if not wallets:
            self.send("Usage: /track &lt;wallet1&gt;,&lt;wallet2&gt;,...")
            return
        if self._monitor_running():
            self.send("⚠️ Monitor already running. /stop first.")
            return
        self.send(f"📋 Starting paper monitor for {len(wallets)} wallet(s)...")
        self._start_monitor(wallets, live_mode=False)

    def _cmd_promote(self, args: str):
        try:
            n = int(args.strip()) - 1
        except ValueError:
            self.send("Usage: /promote &lt;number&gt;  (use /portfolios to see the list)")
            return
        entries = self._sorted_portfolios()
        if n < 0 or n >= len(entries):
            self.send(f"❌ Invalid number. Use /portfolios to see available entries.")
            return
        wallet = entries[n]["wallet"]
        if len(wallet) < 20:
            self.send(f"⚠️ Wallet <code>{wallet}</code> has an incomplete address (old portfolio). Cannot promote.")
            return
        if self._monitor_running():
            self.send("⚠️ Monitor already running. /stop first.")
            return
        self.send(f"📋 Promoting <code>{wallet[:14]}...</code> to paper monitor...")
        self._start_monitor([wallet], live_mode=False)

    def _cmd_golive(self, args: str):
        try:
            n = int(args.strip()) - 1
        except ValueError:
            self.send("Usage: /golive &lt;number&gt;  (use /portfolios to see the list)")
            return
        entries = self._sorted_portfolios()
        if n < 0 or n >= len(entries):
            self.send("❌ Invalid number. Use /portfolios to see available entries.")
            return
        wallet = entries[n]["wallet"]
        if len(wallet) < 20:
            self.send(f"⚠️ Wallet <code>{wallet}</code> has an incomplete address. Cannot go live.")
            return

        self._pending_golive_wallet = wallet
        self._pending_golive_expires = time.time() + 60

        self.send(
            f"⚠️ <b>GO LIVE CONFIRMATION REQUIRED</b>\n\n"
            f"Wallet: <code>{wallet[:14]}...</code>\n"
            f"PnL: <b>${entries[n]['pnl']:+.2f}</b>\n\n"
            f"This will place <b>REAL orders</b> on Polymarket.\n"
            f"Type /confirmgolive within 60 seconds to proceed."
        )

    def _cmd_confirmgolive(self):
        if not self._pending_golive_wallet or time.time() > self._pending_golive_expires:
            self._pending_golive_wallet = None
            self.send("❌ No pending go-live request (or it expired). Use /golive &lt;n&gt; first.")
            return

        wallet = self._pending_golive_wallet
        self._pending_golive_wallet = None

        has_pk = os.getenv("POLYMARKET_PRIVATE_KEY", "").strip()
        has_api = os.getenv("POLYMARKET_API_KEY", "").strip() and os.getenv("POLYMARKET_ADDRESS", "").strip()
        if not has_pk and not has_api:
            self.send(
                "❌ No Polymarket credentials in .env\n"
                "Option 1: set POLYMARKET_PRIVATE_KEY\n"
                "Option 2: set POLYMARKET_API_KEY + POLYMARKET_API_SECRET + POLYMARKET_API_PASSPHRASE + POLYMARKET_ADDRESS"
            )
            return

        if self._monitor_running():
            self.send("⚠️ Monitor already running. /stop first.")
            return

        self.send(f"🚀 <b>GOING LIVE</b> with <code>{wallet[:14]}...</code>...")
        try:
            self._start_monitor([wallet], live_mode=True)
        except ValueError as e:
            self.send(f"❌ Failed to start: {e}")

    def _cmd_stop(self):
        if not self._monitor_running():
            self.send("⚪ No monitor is running.")
            return
        self.send("🛑 Stopping monitor and generating leaderboard...")
        self._stop_monitor()
        self.send("✅ Monitor stopped.")

    def _cmd_settings(self):
        s = self.settings
        self.send(
            "<b>⚙️ CURRENT SETTINGS</b>\n\n"
            "<b>📋 Paper</b>\n"
            f"  Size: {s['paper']['size_pct']*100:.1f}% of whale trade\n"
            f"  Max per trade: ${s['paper']['max_usd']:.2f}\n"
            f"  Min whale trade: ${s['paper']['min_whale_usd']:.2f}\n"
            f"  Max slippage: {s['paper']['max_slippage_pct']:.1f}%\n\n"
            "<b>🟢 Live</b>\n"
            f"  Size: {s['live']['size_pct']*100:.1f}% of whale trade\n"
            f"  Max per trade: ${s['live']['max_usd']:.2f}\n"
            f"  Min whale trade: ${s['live']['min_whale_usd']:.2f}\n"
            f"  Max slippage: {s['live']['max_slippage_pct']:.1f}%\n\n"
            "<i>Change with: /set paper|live size|max|min|slippage &lt;value&gt;\n"
            "Example: /set live max 20</i>"
        )

    def _cmd_set(self, args: str):
        # /set paper|live size|max|min|slippage <value>
        parts = args.strip().split()
        if len(parts) != 3:
            self.send("Usage: /set paper|live size|max|min|slippage &lt;value&gt;\nExample: /set live max 20")
            return
        mode, key_alias, raw_val = parts[0].lower(), parts[1].lower(), parts[2]
        if mode not in ("paper", "live"):
            self.send("Mode must be <b>paper</b> or <b>live</b>.")
            return
        key_map = {"size": "size_pct", "max": "max_usd", "min": "min_whale_usd", "slippage": "max_slippage_pct"}
        key = key_map.get(key_alias)
        if not key:
            self.send("Key must be one of: <b>size</b>, <b>max</b>, <b>min</b>, <b>slippage</b>.")
            return
        try:
            val = float(raw_val)
        except ValueError:
            self.send(f"Value must be a number, got: <code>{raw_val}</code>")
            return
        # size is stored as decimal (5% → 0.05)
        stored_val = val / 100.0 if key == "size_pct" else val
        self.settings[mode][key] = stored_val
        self._save_settings()
        display = f"{val:.1f}%" if key == "size_pct" else f"${val:.2f}" if key != "max_slippage_pct" else f"{val:.1f}%"
        self.send(f"✅ <b>{mode.capitalize()} {key_alias}</b> set to <b>{display}</b>")

    def _cmd_name(self, args: str):
        # /name <n> <nickname>
        parts = args.strip().split(None, 1)
        if len(parts) < 2:
            self.send("Usage: /name &lt;number&gt; &lt;nickname&gt;\nExample: /name 1 TheGoat")
            return
        try:
            n = int(parts[0]) - 1
        except ValueError:
            self.send("First argument must be a number.")
            return
        nickname = parts[1].strip()
        entries = self._promoted_entries()
        if n < 0 or n >= len(entries):
            self.send(f"❌ Invalid number. Use /portfolios to see the list.")
            return
        wallet = entries[n]["wallet"]
        if len(wallet) < 20:
            self.send("⚠️ This wallet has an incomplete address stored.")
            return
        self.wallet_names[wallet] = nickname
        self._save_names()
        self.send(f"✅ Wallet <a href='https://polymarket.com/profile/{wallet}'>{nickname}</a> saved!")

    def _build_positions_message(self) -> tuple:
        """Returns (text, flat_position_list) for the positions view."""
        if not self._monitor_running():
            return "⚪ No monitor running.", []

        flat = []  # list of (wallet, pos_dict, trader)
        reply = "<b>📂 OPEN POSITIONS</b>\n\n"
        idx = 1

        for w in self._monitor.target_wallets:
            trader = self._monitor.traders[w]
            positions = trader.portfolio.get("open_positions", [])
            if not positions:
                continue
            reply += f"{self._wallet_label(w)}\n"
            for pos in positions:
                asset_id = pos.get("asset_id", "")
                entry = pos.get("price_bought", 0)
                invested = pos.get("amount_usdc_invested", 0)
                tokens = pos.get("amount_tokens", 0)

                # Fetch live best bid for current price
                current = 0.0
                try:
                    book = trader._fetch_orderbook(asset_id)
                    bids = book.get("bids", [])
                    if bids:
                        current = max(float(b["price"]) for b in bids)
                except Exception:
                    pass

                current_val = tokens * current if current > 0 else invested
                pct = ((current - entry) / entry * 100) if entry > 0 and current > 0 else 0.0
                pct_str = f"{pct:+.1f}%" if current > 0 else "n/a"
                arrow = "📈" if pct > 0 else "📉" if pct < 0 else "➖"

                mid = pos.get("market_id", "?")[:10]
                reply += (
                    f"  <b>{idx}.</b> <code>{mid}...</code>\n"
                    f"      Entry: {entry:.4f} → Now: {current:.4f} {arrow} <b>{pct_str}</b>\n"
                    f"      Invested: ${invested:.2f} | Value: ${current_val:.2f}\n"
                )
                flat.append((w, pos, trader))
                idx += 1
            reply += "\n"

        if not flat:
            return "No open positions.", []

        return reply, flat

    def _cmd_positions(self, edit_message_id: int = None):
        text, flat = self._build_positions_message()

        # Build inline keyboard: sell buttons 3 per row, then refresh
        all_sells = [{"text": f"💰 #{i+1}", "callback_data": f"sell_{i}"} for i in range(len(flat))]
        sell_rows = [all_sells[i:i+3] for i in range(0, len(all_sells), 3)]
        keyboard = sell_rows + [[{"text": "🔄 Refresh", "callback_data": "refresh_positions"}]]

        if edit_message_id:
            self._edit_message(edit_message_id, text, keyboard)
        else:
            self._send_with_keyboard(text, keyboard)

    def _do_sell(self, flat_index: int, message_id: int = None):
        """Close position at index flat_index from the current positions list."""
        _, flat = self._build_positions_message()
        if flat_index < 0 or flat_index >= len(flat):
            self.send(f"❌ Position #{flat_index+1} not found. Use /positions to refresh.")
            return

        wallet, pos, trader = flat[flat_index]
        market_id = pos.get("market_id", "?")
        asset_id = pos.get("asset_id", "")
        tokens = pos.get("amount_tokens", 0)
        invested = pos.get("amount_usdc_invested", 0)

        # Get current best bid
        exit_price = 0.0
        try:
            book = trader._fetch_orderbook(asset_id)
            bids = book.get("bids", [])
            if bids:
                exit_price = max(float(b["price"]) for b in bids)
        except Exception:
            pass

        if exit_price <= 0:
            self.send(f"⚠️ Could not fetch live price for position #{flat_index+1}. Try again.")
            return

        # Live mode: place a real sell order
        if trader.live_mode:
            result = trader._place_real_order(asset_id, "SELL", tokens, exit_price)
            if "error" in result:
                self.send(f"❌ Live sell failed: {result['error']}")
                return

        revenue = tokens * exit_price
        profit = revenue - invested
        trader.portfolio["virtual_usdc"] += revenue
        trader.portfolio["realized_pnl"] += profit
        trader.portfolio["open_positions"].remove(pos)
        trader.save_portfolio()

        tag = "🟢 LIVE SELL" if trader.live_mode else "📋 MANUAL SELL"
        self.send(
            f"{tag}\n\n"
            f"Wallet: {self._wallet_label(wallet)}\n"
            f"Market: <code>{market_id[:12]}...</code>\n"
            f"Exit price: {exit_price:.4f}\n"
            f"Revenue: ${revenue:.2f} | Profit: <b>${profit:+.2f}</b>\n\n"
            f"Total PnL: <b>${trader.portfolio['realized_pnl']:+.2f}</b>"
        )
        # Refresh the positions message if we know its ID
        if message_id:
            self._cmd_positions(edit_message_id=message_id)

    # ------------------------------------------------------------------ #
    # Polling loop                                                         #
    # ------------------------------------------------------------------ #

    def poll(self):
        """Called once per tick to fetch and dispatch Telegram updates."""
        if not TELEGRAM_BOT_TOKEN:
            return
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
        try:
            res = self.session.get(url, params={"offset": self.last_update_id}, timeout=4)
            data = res.json()
            if not data.get("ok"):
                return
            for update in data.get("result", []):
                self.last_update_id = update["update_id"] + 1

                # ── Inline button callbacks ──────────────────────────────
                cb = update.get("callback_query")
                if cb:
                    cb_id = cb["id"]
                    cb_data = cb.get("data", "")
                    msg_id = cb.get("message", {}).get("message_id")
                    if cb_data == "refresh_positions":
                        self._answer_callback(cb_id, "Refreshing...")
                        self._cmd_positions(edit_message_id=msg_id)
                    elif cb_data.startswith("sell_"):
                        try:
                            idx = int(cb_data.split("_")[1])
                        except ValueError:
                            idx = -1
                        self._answer_callback(cb_id, f"Selling position #{idx+1}...")
                        self._do_sell(idx, message_id=msg_id)
                    else:
                        self._answer_callback(cb_id)
                    continue

                # ── Text commands ────────────────────────────────────────
                text = update.get("message", {}).get("text", "").strip()
                if not text:
                    continue
                parts = text.split(None, 1)
                cmd = parts[0].lower().split("@")[0]  # strip bot username if present
                args = parts[1] if len(parts) > 1 else ""

                if cmd == "/help":
                    self.send(self.HELP)
                elif cmd == "/status":
                    self._cmd_status()
                elif cmd == "/pnl":
                    self._cmd_pnl()
                elif cmd == "/portfolios":
                    self._cmd_portfolios()
                elif cmd == "/paperall":
                    self._cmd_paperall()
                elif cmd == "/autopilot":
                    self._cmd_autopilot()
                elif cmd == "/track":
                    self._cmd_track(args)
                elif cmd == "/promote":
                    self._cmd_promote(args)
                elif cmd == "/golive":
                    self._cmd_golive(args)
                elif cmd == "/confirmgolive":
                    self._cmd_confirmgolive()
                elif cmd == "/stop":
                    self._cmd_stop()
                elif cmd == "/positions":
                    self._cmd_positions()
                elif cmd == "/sell":
                    try:
                        self._do_sell(int(args.strip()) - 1)
                    except ValueError:
                        self.send("Usage: /sell &lt;position number&gt;")
                elif cmd == "/settings":
                    self._cmd_settings()
                elif cmd == "/set":
                    self._cmd_set(args)
                elif cmd == "/name":
                    self._cmd_name(args)
                else:
                    self.send(f"Unknown command: <code>{cmd}</code>\n\n{self.HELP}")
        except Exception as e:
            logger.debug(f"[TelegramController] poll error: {e}")

    def run(self):
        """Main loop — polls Telegram every 2 seconds indefinitely."""
        logger.info("TelegramController started.")

        # --- Auto-resume last session if one exists ---
        session = self._load_session()
        if session:
            wallets = session["wallets"]
            live_mode = session.get("live_mode", False)
            mode_tag = "🟢 LIVE" if live_mode else "📋 PAPER"
            self.send(
                f"🤖 <b>BOT ONLINE — RESUMING LAST SESSION</b>\n\n"
                f"Mode: {mode_tag}\n"
                f"Wallets: {len(wallets)}\n\n" +
                "\n".join(f"• <code>{w[:14]}...</code>" for w in wallets) +
                "\n\n<i>Send /stop to stop, /status for stats.</i>"
            )
            self._start_monitor(wallets, live_mode=live_mode)
        else:
            self.send(
                "🤖 <b>BOT ONLINE</b>\n\nNo previous session found.\nSend /help to see all commands."
            )

        while True:
            try:
                self.poll()
                time.sleep(2)
            except KeyboardInterrupt:
                logger.info("TelegramController shutting down.")
                # On crash/ctrl-c keep the session file so next start resumes
                self._stop_monitor(clear_session=False)
                break


def run_telegram_mode():
    try:
        controller = TelegramController()
        controller.run()
    except ValueError as e:
        print(f"[-] Cannot start Telegram mode: {e}")
        print("    Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in your .env file.")


def run_live_monitor():
    wallets_input = input("\nEnter wallet addresses to live track (comma separated): ").strip()
    if not wallets_input:
        print("Invalid wallet.")
        return
    wallets = [w.strip() for w in wallets_input.split(',')]
    monitor = LiveMonitor(wallets)
    monitor.start_monitoring(poll_interval_seconds=5)


def run_promote_to_live():
    """
    Scans all portfolio_*.json files, ranks them by realized PnL,
    lets the user pick one (or more), then starts a LIVE copy-trade monitor
    that places real orders on Polymarket.
    """
    print("\n" + "="*60)
    print("  PROMOTE WALLET → GO LIVE  ")
    print("  Scans paper-trade results and deploys the best performer  ")
    print("="*60)

    # --- Collect all portfolio files ---
    portfolio_files = [f for f in os.listdir(".") if f.startswith("portfolio_") and f.endswith(".json")]
    if not portfolio_files:
        print("[-] No portfolio files found. Run AUTO-PILOT or LIVE COPY first to generate results.")
        return

    entries = []
    for fname in portfolio_files:
        try:
            with open(fname, "r") as f:
                data = json.load(f)
            wallet = data.get("wallet_address") or fname.replace("portfolio_", "").replace(".json", "")
            pnl = data.get("realized_pnl", 0.0)
            balance = data.get("virtual_usdc", 0.0)
            open_pos = len(data.get("open_positions", []))
            trades = data.get("total_trades_taken", 0)
            entries.append({
                "wallet": wallet,
                "pnl": pnl,
                "balance": balance,
                "open": open_pos,
                "trades": trades,
                "file": fname,
            })
        except Exception:
            continue

    if not entries:
        print("[-] Could not read any portfolio files.")
        return

    # Sort best → worst
    entries.sort(key=lambda x: x["pnl"], reverse=True)

    print(f"\n{'#':<4} {'Wallet':<20} {'Realized PnL':>14} {'Balance':>10} {'Trades':>7} {'Open':>5}")
    print("-" * 62)
    for i, e in enumerate(entries):
        wallet_display = e["wallet"][:18] + "..." if len(e["wallet"]) > 18 else e["wallet"]
        pnl_str = f"${e['pnl']:+.2f}"
        print(f"{i+1:<4} {wallet_display:<20} {pnl_str:>14} ${e['balance']:>8.2f} {e['trades']:>7} {e['open']:>5}")

    print("\nEnter the number(s) of the wallet(s) to promote (e.g. 1  or  1,3):")
    raw = input("> ").strip()
    if not raw:
        print("Cancelled.")
        return

    try:
        indices = [int(x.strip()) - 1 for x in raw.split(",")]
    except ValueError:
        print("[-] Invalid input.")
        return

    selected = []
    for idx in indices:
        if 0 <= idx < len(entries):
            selected.append(entries[idx])
        else:
            print(f"[-] Index {idx+1} out of range, skipping.")

    if not selected:
        print("[-] No valid wallets selected.")
        return

    print("\nSelected wallets:")
    for e in selected:
        print(f"  • {e['wallet']}  (PnL: ${e['pnl']:+.2f})")

    # Warn if wallet address is a short prefix (no full address stored yet)
    short_wallets = [e for e in selected if len(e["wallet"]) < 20]
    if short_wallets:
        print("\n⚠️  Some wallets only have a partial address stored (bot wasn't tracking them with the latest code).")
        print("   Enter the full wallet address for each, or press Enter to skip:\n")
        for e in short_wallets:
            full = input(f"  Full address for '{e['wallet']}': ").strip()
            if full:
                e["wallet"] = full

    # Confirm live mode
    print("\n⚠️  WARNING: LIVE MODE will place REAL orders on Polymarket.")
    print("   Make sure your .env file has either POLYMARKET_PRIVATE_KEY")
    print("   or POLYMARKET_API_KEY + POLYMARKET_API_SECRET + POLYMARKET_API_PASSPHRASE + POLYMARKET_ADDRESS set.")
    confirm = input("\nType 'GO LIVE' to confirm, or anything else to cancel: ").strip()
    if confirm != "GO LIVE":
        print("Cancelled. No real orders will be placed.")
        return

    wallets_to_track = [e["wallet"] for e in selected]
    print(f"\n🚀 Launching LIVE monitor for {len(wallets_to_track)} wallet(s)...")
    send_telegram_alert(
        f"🚀 <b>GOING LIVE!</b>\n\nDeploying real copy-trading on:\n" +
        "\n".join(f"• <code>{w[:14]}...</code>" for w in wallets_to_track)
    )

    try:
        monitor = LiveMonitor(wallets_to_track, live_mode=True)
        monitor.start_monitoring(poll_interval_seconds=5)
    except ValueError as e:
        print(f"\n[-] Could not start live mode: {e}")
        print("    Set POLYMARKET_PRIVATE_KEY or POLYMARKET_API_KEY credentials in your .env file and try again.")

def run_auto_pilot():
    import time
    print("\n" + "="*50)
    print(" AUTO-PILOT: Hunting Highest Quality Whales (>=20% ROI & >=2 Real Trades/hr) ")
    print("  5-Minute Timeout Enforced. ")
    print("="*50)
    
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
    session.mount('https://', adapter)
    
    scanner = Scanner(session=session)
    ledger = LedgerBacktester(size_percentage=0.05, max_trade_usd=5.0, session=session)
    
    winners = []
    
    # --- LOAD FROM HALL OF FAME FIRST ---
    hof_file = "memory_hall_of_fame.json"
    if os.path.exists(hof_file):
        with open(hof_file, 'r') as f:
            hall_wallets = json.load(f)
            print(f"[*] Loading {len(hall_wallets)} elite wallets from Hall of Fame...")
            for hw in hall_wallets:
                winners.append({"name": f"Elite_{hw[:6]}", "wallet": hw, "roi": 999.0, "freq": 999.0})
    
    offset = 0
    batch_size = 50
    total_scanned = 0
    start_time = time.time()
    
    while len(winners) < 15:
        elapsed = time.time() - start_time
        if elapsed > 300: # 5 minutes
            print("\n[] 5 minutes elapsed! Stopping hunt early.")
            break
            
        print(f"\n[*] Fetching Leaderboard batch (offset {offset})...")
        traders = scanner.get_top_traders_24h(limit=batch_size, offset=offset)
        if not traders:
            print("[-] No more traders found from API! Stopping hunt.")
            break
            
        def evaluate_whale(t):
            res = ledger.analyze_roi(t['wallet'])
            roi = res.get('roi_percentage', 0)
            freq = res.get('trades_per_hour', 0.0)
            return {"t": t, "res": res, "roi": roi, "freq": freq}

        with ThreadPoolExecutor(max_workers=20) as executor:
            batch_results = list(executor.map(evaluate_whale, traders))

        for out in batch_results:
            if time.time() - start_time > 300: break
            if not out: continue
            total_scanned += 1
            roi = out['roi']
            freq = out['freq']
            t = out['t']
            print(f"  [{total_scanned}] {t['name'][:15]:15}  ROI:{roi:7.1f}%  Freq:{freq:7.2f}/hr", end="")
            if roi >= 20.0 and freq >= 2.0:
                print("  => FOUND!")
                winners.append({
                    "name": t["name"], "wallet": t["wallet"],
                    "roi": roi, "freq": freq,
                    "sim_pnl": out['res'].get("simulated_pnl", 0),
                    "sim_invested": out['res'].get("simulated_invested", 0)
                })
                if len(winners) >= 15: break
            else:
                reason = []
                if roi < 20.0: reason.append(f"ROI {roi:.1f}%<20%")
                if freq < 2.0: reason.append(f"Freq {freq:.1f}<2.0/hr")
                print(f"  => skip ({', '.join(reason)})")

        offset += batch_size

    print("\n" + "="*50)
    print(f"  AUTO-PILOT HUNT COMPLETE! Found {len(winners)} Whales  ")
    print("="*50)
    
    if len(winners) == 0:
        print("[-] No whales found matching the criteria. Returning to menu.")
        return
        
    for w in winners:
        profile_url = f"https://polymarket.com/profile/{w['wallet']}"
        print(f"Bot: {w['name']:<15} | ROI: {w['roi']:>5.1f}% | Trades/hr: {w['freq']:.1f} | Bot PnL: ${w['sim_pnl']:>5.2f} | Link: {profile_url}")
        
    print(f"\n PASSING {len(winners)} FOUND BOTS DIRECTLY TO LIVE MONITOR. BUCKLE UP! ")
    time.sleep(3)
    
    wallets_to_track = [w['wallet'] for w in winners]
    monitor = LiveMonitor(wallets_to_track)
    monitor.start_monitoring(poll_interval_seconds=5)


def main():
    while True:
        print("\n" + "="*50)
        print(" Polymarket Analytics & Copy-Trading Bot ")
        print("="*50)
        print("1.  AUTO-PILOT (Hunt Bots: >=20% ROI & >=2 Trades/hr -> Paper Trade)")
        print("2.  LIVE COPY (Enter Specific Wallet Addresses)")
        print("3.  PROMOTE WALLET → GO LIVE (Pick best paper-trade result & trade for real)")
        print("4.  📱 TELEGRAM MODE (Control everything from your phone)")
        print("5.  Exit")

        choice = input("\nSelect an option (1-5): ").strip()

        if choice == '1':
            run_auto_pilot()
        elif choice == '2':
            run_live_monitor()
        elif choice == '3':
            run_promote_to_live()
        elif choice == '4':
            run_telegram_mode()
        elif choice == '5':
            print("Exiting...")
            break
        else:
            print("Invalid choice.")

if __name__ == '__main__':
    if len(sys.argv) > 1:
        if sys.argv[1] == '--auto':
            run_auto_pilot()
        elif sys.argv[1] == '--telegram':
            run_telegram_mode()
        else:
            main()
    else:
        main()


