#!/usr/bin/env python3
"""
telegram_signals.py — Hardened P8_FADE_ANY_EXTREME signal bot for Telegram.

Setup:
1. Create .env file with:
   TELEGRAM_BOT_TOKEN=123456:ABC-your-token
   TELEGRAM_CHAT_ID=987654321

2. Run:
   python telegram_signals.py --sim    # simulation
   python telegram_signals.py --live   # live Polymarket API
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Load .env before anything else
def _load_dotenv() -> None:
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    with open(env_path) as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val

_load_dotenv()

BOT_TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID       = os.environ.get("TELEGRAM_CHAT_ID",  "").strip()

POSITION_USD  = 10.0
FEE_RATE      = 0.02
SIGNAL_HIGH   = 0.85
SIGNAL_LOW    = 0.15
MIN_MTC       = 1.0
MAX_MTC       = 10.0
MIN_PRICE     = 0.01
MAX_PRICE     = 0.99
POLL_SEC      = 30
SIM_SPEED     = 0.10
HEARTBEAT_SEC = 3600

GAMMA_API    = "https://gamma-api.polymarket.com"
CLOB_API     = "https://clob.polymarket.com"
TG_API       = "https://api.telegram.org"
COINS        = ["BTC", "ETH", "SOL", "XRP"]

HTTP_RETRIES  = 3
HTTP_BACKOFF  = [1.0, 2.0, 4.0]
TG_RETRIES    = 3
TG_BACKOFF    = [2.0, 4.0, 8.0]

try:
    import httpx
except ImportError:
    print("ERROR: httpx is not installed.  Run:  pip install httpx")
    sys.exit(1)

@dataclass
class Signal:
    condition_id:     str
    coin:             str
    side:             str
    entry_price:      float
    minutes_to_close: float
    dt:               str
    ts:               int

@dataclass
class ClosedTrade:
    signal:     Signal
    resolution: str
    win:        bool
    gross_pnl:  float
    net_pnl:    float
    roi_pct:    float

def compute_pnl(side, entry_price, resolution):
    cost = POSITION_USD
    if side == "YES":
        win   = resolution == "YES"
        gross = cost * (1.0 / entry_price - 1.0) if win else -cost
    else:
        no_px = 1.0 - entry_price
        win   = resolution == "NO"
        gross = cost * (1.0 / no_px - 1.0) if win else -cost
    net = gross - FEE_RATE * cost
    roi = (net / cost) * 100.0
    return win, round(gross, 4), round(net, 4), round(roi, 2)

def _http_get(url, params=None, timeout=6.0):
    for attempt, wait in enumerate([0.0] + HTTP_BACKOFF):
        if wait:
            time.sleep(wait)
        try:
            r = httpx.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as exc:
            if attempt < HTTP_RETRIES:
                print(f"  [HTTP] retry {attempt+1}: {exc}")
            else:
                print(f"  [HTTP] failed after {HTTP_RETRIES+1} attempts: {exc}")
    return None

def _tg_send(text, retries=TG_RETRIES):
    if not BOT_TOKEN or not CHAT_ID:
        return False
    for attempt, wait in enumerate([0.0] + TG_BACKOFF[:retries]):
        if wait:
            time.sleep(wait)
        try:
            r = httpx.post(
                f"{TG_API}/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
                timeout=10.0,
            )
            if r.status_code == 200:
                return True
            print(f"  [Telegram] HTTP {r.status_code}: {r.text[:120]}")
        except Exception as exc:
            print(f"  [Telegram] send error (attempt {attempt+1}): {exc}")
    return False

def _tg_validate():
    if not BOT_TOKEN or not CHAT_ID:
        return False
    try:
        r = httpx.get(f"{TG_API}/bot{BOT_TOKEN}/getMe", timeout=8.0)
        if r.status_code != 200:
            print(f"  [Telegram] Token validation failed: HTTP {r.status_code}")
            return False
        bot_name = r.json().get("result", {}).get("username", "?")
        print(f"  [Telegram] Token valid — bot @{bot_name}")
        return True
    except Exception as exc:
        print(f"  [Telegram] Validation error: {exc}")
        return False

def _fmt_signal(sig):
    trade_px   = (1.0 - sig.entry_price) if sig.side == "NO" else sig.entry_price
    trade_px   = max(trade_px, MIN_PRICE)
    max_payout = POSITION_USD / trade_px
    multiplier = max_payout / POSITION_USD
    return (
        f"\u26a1 <b>SIGNAL — BUY {sig.side} on {sig.coin}</b>\n"
        f"YES price : {sig.entry_price:.3f}  \u2192  "
        f"{'NO' if sig.side == 'NO' else 'YES'} price : {trade_px:.3f}\n"
        f"Max payout: <b>${max_payout:.2f}</b>  ({multiplier:.1f}\u00d7 your stake)\n"
        f"Time left : {sig.minutes_to_close:.1f} min\n"
        f"Stake     : ${POSITION_USD:.2f}\n"
        f"<i>{sig.dt}</i>"
    )

def _fmt_resolution(trade, running_total, n_trades):
    icon = "\u2705" if trade.win else "\u274c"
    tag  = "WIN" if trade.win else "LOSS"
    return (
        f"{icon} <b>{tag} — {trade.signal.coin} resolved {trade.resolution}</b>\n"
        f"Net PnL  : <b>${trade.net_pnl:>+.2f}</b>  (roi {trade.roi_pct:>+.1f}%)\n"
        f"Running  : ${running_total:>+.2f}  across {n_trades} trade(s)"
    )

def _fmt_session_end(trades):
    if not trades:
        return "\U0001f4ca <b>Session ended — no signals fired.</b>"
    wins  = sum(1 for t in trades if t.win)
    total = sum(t.net_pnl for t in trades)
    wr    = wins / len(trades) * 100
    pf_raw = (
        sum(t.gross_pnl for t in trades if t.win) /
        abs(sum(t.gross_pnl for t in trades if not t.win) or 1)
    )
    pf = f"{pf_raw:.2f}" if math.isfinite(pf_raw) else "\u221e"
    return (
        f"\U0001f4ca <b>Session complete — P8_FADE_ANY_EXTREME</b>\n"
        f"Trades : {len(trades)}  (wins {wins} / losses {len(trades)-wins})\n"
        f"Win %  : {wr:.1f}%\n"
        f"Net PnL: <b>${total:>+.2f}</b>\n"
        f"Profit factor: {pf}"
    )

class _Board:
    def __init__(self):
        self.trades: List[ClosedTrade] = []
    def add(self, t):
        self.trades.append(t)
    @property
    def n(self): return len(self.trades)
    @property
    def total(self): return sum(t.net_pnl for t in self.trades)

class TelegramSignalBot:
    def __init__(self, mode="live", speed=SIM_SPEED):
        self.mode  = mode
        self.speed = speed
        self.board = _Board()

    def _pause(self):
        if self.speed > 0:
            time.sleep(self.speed)

    def _on_signal(self, sig):
        trade_px = max((1 - sig.entry_price) if sig.side == "NO" else sig.entry_price, MIN_PRICE)
        mp = POSITION_USD / trade_px
        print(f"  \u26a1  {sig.dt}  {sig.coin:<4s}  YES={sig.entry_price:.3f}  →  BUY {sig.side} @ {trade_px:.3f}  (max ${mp:.2f})")
        if _tg_send(_fmt_signal(sig)):
            print("       \U0001f4f1 Telegram notification sent")
        self._pause()

    def _on_close(self, trade):
        self.board.add(trade)
        icon = "\u2713" if trade.win else "\u2717"
        tag  = "WIN " if trade.win else "LOSS"
        print(f"     {icon} {tag}  {trade.signal.coin:<4s}  → {trade.resolution}  net ${trade.net_pnl:>+.2f}  (roi {trade.roi_pct:>+.1f}%)")
        if _tg_send(_fmt_resolution(trade, self.board.total, self.board.n)):
            print("       \U0001f4f1 Telegram notification sent")
        self._pause()

    def _header(self):
        tg_ok = bool(BOT_TOKEN and CHAT_ID)
        print("=" * 72)
        print("  P8_FADE_ANY_EXTREME  \u00b7  Telegram Signal Bot  [HARDENED]")
        print(f"  Signal  : YES > {SIGNAL_HIGH} → BUY NO  |  YES < {SIGNAL_LOW} → BUY YES")
        print(f"  Window  : {MIN_MTC:.0f}–{MAX_MTC:.0f} min before close")
        print(f"  Stake   : ${POSITION_USD:.2f}  |  Fee: {FEE_RATE*100:.0f}%")
        print(f"  Mode    : {self.mode.upper()}")
        if tg_ok:
            print(f"  Telegram: \u2713 configured  (chat_id={CHAT_ID})")
        else:
            print("  Telegram: \u2717 NOT configured — signals printed to terminal only")
            print("            Create .env with TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        print("=" * 72)

    def run(self):
        self._header()
        if BOT_TOKEN and CHAT_ID:
            if not _tg_validate():
                print("\n  ERROR: Telegram credentials are invalid. Check .env and try again.")
                sys.exit(1)
            _tg_send(
                f"\U0001f680 <b>P8_FADE_ANY_EXTREME bot started</b>\n"
                f"Monitoring BTC/ETH/SOL/XRP — "
                f"signal window: last {MIN_MTC:.0f}–{MAX_MTC:.0f} min before close\n"
                f"Stake: ${POSITION_USD:.2f}  |  Mode: {self.mode.upper()}"
            )
        try:
            if self.mode == "sim":
                self._run_sim()
            else:
                self._run_live()
        except KeyboardInterrupt:
            print("\n  Stopped by user (Ctrl-C).")
            if BOT_TOKEN and CHAT_ID:
                _tg_send(_fmt_session_end(self.board.trades))
        except Exception as exc:
            print(f"\n  FATAL ERROR: {exc}")
            print(traceback.format_exc())
            if BOT_TOKEN and CHAT_ID:
                _tg_send(
                    f"\U0001f534 <b>Bot crashed!</b>\n"
                    f"<code>{str(exc)[:300]}</code>\n"
                    f"Restart with: systemctl restart polybot"
                )
            sys.exit(1)

    def _run_sim(self):
        print("\n  Loading synthetic dataset ... ", end="", flush=True)
        sys.path.insert(0, str(Path(__file__).parent))
        from polymarket_backtest import generate_synthetic_data
        markets, candles_map = generate_synthetic_data()
        markets.sort(key=lambda m: m.start_ts)
        print(f"done  ({len(markets)} markets)\n")
        open_pos: Dict[str, Signal] = {}
        for mkt in markets:
            for c in (candles_map.get(mkt.condition_id) or []):
                if not (MIN_MTC <= c.minutes_to_close <= MAX_MTC):
                    continue
                if not (MIN_PRICE <= c.price <= MAX_PRICE):
                    continue
                if (c.price > SIGNAL_HIGH or c.price < SIGNAL_LOW) and mkt.condition_id not in open_pos:
                    side = "NO" if c.price > SIGNAL_HIGH else "YES"
                    sig  = Signal(condition_id=mkt.condition_id, coin=mkt.coin,
                                  side=side, entry_price=c.price,
                                  minutes_to_close=c.minutes_to_close, dt=c.dt, ts=c.ts)
                    open_pos[mkt.condition_id] = sig
                    self._on_signal(sig)
            if mkt.condition_id in open_pos and mkt.resolution in ("YES", "NO"):
                sig = open_pos.pop(mkt.condition_id)
                win, gross, net, roi = compute_pnl(sig.side, sig.entry_price, mkt.resolution)
                self._on_close(ClosedTrade(sig, mkt.resolution, win, gross, net, roi))
        if BOT_TOKEN and CHAT_ID:
            _tg_send(_fmt_session_end(self.board.trades))
        print(f"\n  Session complete — {self.board.n} trades  |  net ${self.board.total:>+.2f}")

    def _run_live(self):
        print(f"\n  Polling every {POLL_SEC}s — Ctrl-C to stop\n")
        open_sigs: Dict[str, Signal] = {}
        sig_entry_ts: Dict[str, int] = {}
        last_heartbeat = time.time()
        scan = 0

        def _active(coin):
            r = _http_get(f"{GAMMA_API}/markets", params={
                "active": "true", "closed": "false",
                "search": f"{coin} minute", "limit": 5,
            })
            if not r: return None
            try:
                mkts = [m for m in r.json() if isinstance(m, dict)]
                if mkts:
                    mkts.sort(key=lambda m: m.get("endDate") or "")
                    return mkts[0]
            except Exception: pass
            return None

        def _midprice(tok):
            r = _http_get(f"{CLOB_API}/book", params={"token_id": tok})
            if not r: return None
            try:
                book = r.json()
                bids = book.get("bids") or []
                asks = book.get("asks") or []
                if bids and asks:
                    return (float(bids[0]["price"]) + float(asks[0]["price"])) / 2
                return float((bids or asks)[0]["price"]) if (bids or asks) else None
            except Exception: return None

        def _market_resolved(cid):
            r = _http_get(f"{GAMMA_API}/markets/{cid}")
            if not r: return None
            try:
                data = r.json()
                outcome = (data.get("outcome") or data.get("resolution") or data.get("winner") or "")
                outcome = str(outcome).upper().strip()
                if outcome in ("YES", "NO"): return outcome
                if data.get("closed") or data.get("resolved"):
                    for tok in (data.get("tokens") or []):
                        o = str(tok.get("outcome") or "").upper().strip()
                        if (tok.get("winner") or tok.get("winning")) and o in ("YES", "NO"):
                            return o
            except Exception: pass
            return None

        def _fmt_ts(ts):
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        while True:
            scan += 1
            now_ts = int(datetime.now(timezone.utc).timestamp())
            print(f"  [{_fmt_ts(now_ts)}]  scan #{scan}  (open: {len(open_sigs)})")

            if time.time() - last_heartbeat >= HEARTBEAT_SEC:
                _tg_send(
                    f"\U0001f493 <b>Heartbeat</b> — bot is alive\n"
                    f"Scan #{scan}  |  Open signals: {len(open_sigs)}\n"
                    f"Net P&L so far: ${self.board.total:>+.2f}  ({self.board.n} closed trades)"
                )
                last_heartbeat = time.time()

            stale_cids = []
            for cid, sig in list(open_sigs.items()):
                age_min = (now_ts - sig.ts) / 60.0
                if age_min > MAX_MTC + 5:
                    outcome = _market_resolved(cid)
                    if outcome:
                        open_sigs.pop(cid, None)
                        sig_entry_ts.pop(cid, None)
                        win, gross, net, roi = compute_pnl(sig.side, sig.entry_price, outcome)
                        self._on_close(ClosedTrade(sig, outcome, win, gross, net, roi))
                        print(f"    [resolved] {sig.coin} → {outcome}")
                    elif age_min > MAX_MTC + 30:
                        stale_cids.append(cid)

            for cid in stale_cids:
                sig = open_sigs.pop(cid, None)
                sig_entry_ts.pop(cid, None)
                if sig:
                    print(f"    [stale] {sig.coin} removed after 30+ min")
                    _tg_send(f"\u26a0\ufe0f <b>Stale signal removed</b> — {sig.coin}\n"
                             f"Could not resolve after 30+ min.\n"
                             f"Signal was: BUY {sig.side} @ {sig.entry_price:.3f}")

            for coin in COINS:
                mkt = _active(coin)
                if not mkt:
                    print(f"    {coin}: no active market")
                    continue
                tokens = mkt.get("tokens") or []
                yes_tok = str(tokens[0].get("token_id") or tokens[0].get("tokenId") or "") if tokens else ""
                if not yes_tok: continue
                end_str = mkt.get("endDate") or mkt.get("end_date_iso") or ""
                try:
                    end_ts = int(datetime.fromisoformat(end_str.replace("Z", "+00:00")).timestamp())
                except Exception: continue
                mtc = (end_ts - now_ts) / 60.0
                if mtc <= 0:
                    print(f"    {coin}: expired (mtc={mtc:.1f})")
                    continue
                if not (MIN_MTC <= mtc <= MAX_MTC):
                    print(f"    {coin}: mtc={mtc:.1f} — outside window")
                    continue
                price = _midprice(yes_tok)
                if price is None:
                    print(f"    {coin}: no price")
                    continue
                if not (MIN_PRICE <= price <= MAX_PRICE):
                    print(f"    {coin}: price {price:.4f} out of bounds")
                    continue
                cid = (mkt.get("conditionId") or mkt.get("condition_id") or mkt.get("id") or yes_tok)
                print(f"    {coin}: YES={price:.3f}  mtc={mtc:.1f} min", end="")
                if price > SIGNAL_HIGH or price < SIGNAL_LOW:
                    if cid not in open_sigs:
                        side = "NO" if price > SIGNAL_HIGH else "YES"
                        sig  = Signal(condition_id=cid, coin=coin, side=side,
                                      entry_price=price, minutes_to_close=round(mtc, 2),
                                      dt=_fmt_ts(now_ts), ts=now_ts)
                        open_sigs[cid] = sig
                        sig_entry_ts[cid] = now_ts
                        print(f"  \u26a1 SIGNAL")
                        self._on_signal(sig)
                    else:
                        print("  (open)")
                else:
                    print()

            print(f"  Next poll in {POLL_SEC}s ...\n")
            time.sleep(POLL_SEC)

def main():
    ap = argparse.ArgumentParser(description="P8_FADE_ANY_EXTREME — Hardened Telegram signal bot")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--sim",  action="store_true", help="Simulation mode")
    grp.add_argument("--live", action="store_true", help="Live Polymarket API")
    ap.add_argument("--speed", type=float, default=SIM_SPEED)
    args = ap.parse_args()
    TelegramSignalBot(mode="sim" if args.sim else "live", speed=args.speed).run()

if __name__ == "__main__":
    main()
