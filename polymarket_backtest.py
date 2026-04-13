#!/usr/bin/env python3
"""
polymarket_backtest.py — 5-minute Polymarket crypto pattern backtest.
"""

from __future__ import annotations

import asyncio
import csv
import json
import math
import os
import random
import statistics
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

GAMMA_API    = "https://gamma-api.polymarket.com"
CLOB_API     = "https://clob.polymarket.com"
COINS        = ["BTC", "ETH", "SOL", "XRP"]
DAYS_BACK    = 5
CANDLE_MIN   = 5
POSITION_USD = 10.0
FEE_RATE     = 0.02
OUTPUT_DIR   = "data"
SYNTHETIC_MODE = True

@dataclass
class Market:
    condition_id:    str
    coin:            str
    question:        str
    start_ts:        int
    end_ts:          int
    duration_min:    float
    yes_token:       str
    no_token:        str
    resolution:      str
    final_yes_price: float
    volume:          float

@dataclass
class Candle:
    condition_id:     str
    coin:             str
    ts:               int
    dt:               str
    price:            float
    minutes_to_close: float

@dataclass
class Trade:
    pattern:          str
    condition_id:     str
    coin:             str
    question:         str
    entry_ts:         int
    entry_dt:         str
    minutes_to_close: float
    side:             str
    entry_price:      float
    resolution:       str
    win:              bool
    gross_pnl:        float
    net_pnl:          float
    roi_pct:          float

@dataclass
class PatternSummary:
    pattern:       str
    coin:          str
    trades:        int
    wins:          int
    losses:        int
    win_rate:      float
    total_net_pnl: float
    avg_net_pnl:   float
    max_drawdown:  float
    profit_factor: float
    sharpe:        float

def _now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())

def _days_ago_ts(n: int) -> int:
    return _now_ts() - n * 86_400

def _fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

async def _get(client: httpx.AsyncClient, url: str, params: dict | None = None) -> Any:
    for attempt in range(4):
        try:
            r = await client.get(url, params=params, timeout=20.0)
            r.raise_for_status()
            return r.json()
        except Exception:
            if attempt == 3:
                raise
            await asyncio.sleep(2 ** attempt)

def simulate_trade(side: str, entry_price: float, resolution: str) -> Tuple[bool, float, float, float]:
    cost = POSITION_USD
    if side == "YES":
        if entry_price <= 0:
            return False, 0.0, 0.0, 0.0
        win = resolution == "YES"
        gross_pnl = cost * (1.0 / entry_price - 1.0) if win else -cost
    else:
        no_price = 1.0 - entry_price
        if no_price <= 0:
            return False, 0.0, 0.0, 0.0
        win = resolution == "NO"
        gross_pnl = cost * (1.0 / no_price - 1.0) if win else -cost
    fee = FEE_RATE * cost
    net_pnl = gross_pnl - fee
    roi_pct = (net_pnl / cost) * 100.0
    return win, round(gross_pnl, 4), round(net_pnl, 4), round(roi_pct, 4)

PATTERNS: Dict[str, dict] = {
    "P1_LATE_CONF_YES": {
        "desc": "YES > 0.70 within last 10 min -> buy YES",
        "min_mtc": 1, "max_mtc": 10, "side": "YES",
        "condition": lambda p, _: p > 0.70,
    },
    "P2_FADE_EXTREME_YES": {
        "desc": "YES > 0.85 within last 10 min -> buy NO (contrarian)",
        "min_mtc": 1, "max_mtc": 10, "side": "NO",
        "condition": lambda p, _: p > 0.85,
    },
    "P3_FADE_EXTREME_NO": {
        "desc": "YES < 0.15 within last 10 min -> buy YES (contrarian)",
        "min_mtc": 1, "max_mtc": 10, "side": "YES",
        "condition": lambda p, _: p < 0.15,
    },
    "P4_MOM_UP": {
        "desc": "3 consecutive rising 5-min candles (10-30 min window) -> buy YES",
        "min_mtc": 10, "max_mtc": 30, "side": "YES", "condition": None,
    },
    "P5_MOM_DOWN": {
        "desc": "3 consecutive falling 5-min candles (10-30 min window) -> buy NO",
        "min_mtc": 10, "max_mtc": 30, "side": "NO", "condition": None,
    },
    "P6_NEAR_EVEN": {
        "desc": "YES in [0.45, 0.55] (5-20 min window) -> buy YES (UP bias)",
        "min_mtc": 5, "max_mtc": 20, "side": "YES",
        "condition": lambda p, _: 0.45 <= p <= 0.55,
    },
    "P7_EARLY_CHEAP_YES": {
        "desc": "YES < 0.35 (20-60 min window) -> buy YES (cheap asymmetric)",
        "min_mtc": 20, "max_mtc": 60, "side": "YES",
        "condition": lambda p, _: p < 0.35,
    },
    "P8_FADE_ANY_EXTREME": {
        "desc": "YES > 0.85 -> buy NO  |  YES < 0.15 -> buy YES  (last 10 min)",
        "min_mtc": 1, "max_mtc": 10, "side": "DYNAMIC",
        "side_fn": lambda p: "NO" if p > 0.85 else "YES",
        "condition": lambda p, _: p > 0.85 or p < 0.15,
    },
}

def run_pattern(key, cfg, markets, candles_map):
    trades = []
    for mkt in markets:
        if mkt.resolution not in ("YES", "NO"):
            continue
        candles = candles_map.get(mkt.condition_id, [])
        if len(candles) < 3:
            continue
        window = [c for c in candles if cfg["min_mtc"] <= c.minutes_to_close <= cfg["max_mtc"]]
        if not window:
            continue
        entry = None
        if cfg["condition"] is not None:
            for c in window:
                if cfg["condition"](c.price, c):
                    entry = c
                    break
        elif "MOM_UP" in key:
            for i in range(2, len(window)):
                if window[i].price > window[i-1].price > window[i-2].price:
                    entry = window[i]
                    break
        elif "MOM_DOWN" in key:
            for i in range(2, len(window)):
                if window[i].price < window[i-1].price < window[i-2].price:
                    entry = window[i]
                    break
        if entry is None:
            continue
        side = cfg["side_fn"](entry.price) if cfg["side"] == "DYNAMIC" else cfg["side"]
        win, gross, net, roi = simulate_trade(side, entry.price, mkt.resolution)
        trades.append(Trade(
            pattern=key, condition_id=mkt.condition_id, coin=mkt.coin,
            question=mkt.question, entry_ts=entry.ts, entry_dt=entry.dt,
            minutes_to_close=entry.minutes_to_close, side=side,
            entry_price=entry.price, resolution=mkt.resolution,
            win=win, gross_pnl=gross, net_pnl=net, roi_pct=roi,
        ))
    return trades

def summarise(pattern, coin, trades):
    if not trades:
        return PatternSummary(pattern, coin, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    wins = sum(1 for t in trades if t.win)
    pnls = [t.net_pnl for t in trades]
    equity = peak = max_dd = 0.0
    for p in pnls:
        equity += p
        if equity > peak: peak = equity
        dd = peak - equity
        if dd > max_dd: max_dd = dd
    g_wins = sum(t.gross_pnl for t in trades if t.win)
    g_losses = abs(sum(t.gross_pnl for t in trades if not t.win))
    pf = g_wins / g_losses if g_losses > 0 else float("inf")
    sharpe = 0.0
    if len(pnls) > 1:
        mu = statistics.mean(pnls)
        sig = statistics.stdev(pnls)
        if sig > 0:
            sharpe = (mu / sig) * math.sqrt(len(pnls))
    return PatternSummary(
        pattern=pattern, coin=coin, trades=len(trades), wins=wins,
        losses=len(trades)-wins, win_rate=round(wins/len(trades), 4),
        total_net_pnl=round(sum(pnls), 4), avg_net_pnl=round(statistics.mean(pnls), 4),
        max_drawdown=round(max_dd, 4), profit_factor=round(pf, 4), sharpe=round(sharpe, 4),
    )

def generate_synthetic_data() -> Tuple[List[Market], Dict[str, List[Candle]]]:
    rng = random.Random(42)
    MARKET_DURATION_MIN = 60
    INTERVAL_MIN = 60
    coin_params = {
        "BTC": {"yes_rate": 0.52, "vol": 0.060},
        "ETH": {"yes_rate": 0.50, "vol": 0.070},
        "SOL": {"yes_rate": 0.48, "vol": 0.080},
        "XRP": {"yes_rate": 0.51, "vol": 0.065},
    }
    now = _now_ts()
    start_ref = _days_ago_ts(DAYS_BACK)
    n_per_mkt = MARKET_DURATION_MIN // CANDLE_MIN
    all_markets: List[Market] = []
    candles_map: Dict[str, List[Candle]] = {}
    for coin in COINS:
        p = coin_params[coin]
        t = start_ref
        while t + MARKET_DURATION_MIN * 60 <= now:
            mkt_start = t
            mkt_end = t + MARKET_DURATION_MIN * 60
            cid = f"syn_{coin}_{mkt_start}"
            price = rng.uniform(0.28, 0.72)
            prices = []
            for _ in range(n_per_mkt):
                drift = 0.05 * (0.5 - price)
                shock = rng.gauss(0.0, p["vol"])
                price = max(0.02, min(0.98, price + drift + shock))
                prices.append(round(price, 6))
            adjusted_yes = p["yes_rate"] * 0.4 + prices[-1] * 0.6
            if rng.random() < adjusted_yes:
                resolution = "YES"
                final_yes_price = round(rng.uniform(0.91, 0.99), 4)
            else:
                resolution = "NO"
                final_yes_price = round(rng.uniform(0.01, 0.09), 4)
            all_markets.append(Market(
                condition_id=cid, coin=coin,
                question=f"Will {coin} price go up in the next {MARKET_DURATION_MIN} minutes?",
                start_ts=mkt_start, end_ts=mkt_end,
                duration_min=float(MARKET_DURATION_MIN),
                yes_token=f"tok_yes_{mkt_start}_{coin}",
                no_token=f"tok_no_{mkt_start}_{coin}",
                resolution=resolution, final_yes_price=final_yes_price,
                volume=round(rng.uniform(250, 9000), 2),
            ))
            candle_list = []
            for i, prc in enumerate(prices):
                ts = mkt_start + i * CANDLE_MIN * 60
                mtc = (mkt_end - ts) / 60.0
                candle_list.append(Candle(
                    condition_id=cid, coin=coin, ts=ts, dt=_fmt(ts),
                    price=prc, minutes_to_close=round(mtc, 2),
                ))
            candles_map[cid] = candle_list
            t += INTERVAL_MIN * 60
    all_markets.sort(key=lambda m: m.end_ts, reverse=True)
    return all_markets, candles_map

def save_csv(path, rows, fields):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(asdict(row) if hasattr(row, "__dataclass_fields__") else dict(row))

def save_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)

def analyse_streaks(markets):
    result = {}
    for coin in COINS:
        coin_markets = sorted(
            [m for m in markets if m.coin == coin and m.resolution in ("YES", "NO")],
            key=lambda m: m.end_ts,
        )
        resolutions = [m.resolution for m in coin_markets]
        n = len(resolutions)
        if n < 4:
            result[coin] = {"insufficient_data": True, "markets": n}
            continue
        streaks = {}
        for length in range(1, 5):
            for anchor in ("YES", "NO"):
                key = f"after_{length}x_{anchor}"
                total = yes_next = 0
                for i in range(length, n):
                    if all(resolutions[i-j-1] == anchor for j in range(length)):
                        total += 1
                        if resolutions[i] == "YES":
                            yes_next += 1
                if total > 0:
                    streaks[key] = {
                        "occurrences": total,
                        "next_YES_rate": round(yes_next/total, 4),
                        "next_NO_rate": round(1 - yes_next/total, 4),
                        "edge_if_YES": round(yes_next/total - 0.5, 4),
                    }
        result[coin] = {"streaks": streaks, "total_markets": n,
                        "overall_yes_rate": round(resolutions.count("YES")/n, 4)}
    return result

async def main() -> None:
    print("=" * 72)
    print("  Polymarket 5-min Crypto Pattern Backtest")
    print(f"  Mode: {'SYNTHETIC' if SYNTHETIC_MODE else 'LIVE'}")
    print("=" * 72)
    all_markets: List[Market] = []
    all_candles: List[Candle] = []
    candles_map: Dict[str, List[Candle]] = {}
    if SYNTHETIC_MODE:
        print("\n[1/5]  Generating synthetic markets ...")
        all_markets, candles_map = generate_synthetic_data()
        all_candles = [c for v in candles_map.values() for c in v]
        for coin in COINS:
            n = sum(1 for m in all_markets if m.coin == coin)
            print(f"       {coin} : {n} markets")
        resolved = [m for m in all_markets if m.resolution in ("YES", "NO")]
        unknown  = [m for m in all_markets if m.resolution == "UNKNOWN"]
        print(f"       Total : {len(all_markets)}  |  Resolved : {len(resolved)}  |  Unknown : {len(unknown)}")
        print(f"\n[2/5]  Price data ready — {len(all_candles):,} candles")
    else:
        async with httpx.AsyncClient() as client:
            print("\n[1/5]  Fetching markets from Gamma API ...")
            for coin in COINS:
                pass
    print("\n[3/5]  Streak analysis ...")
    resolved = [m for m in all_markets if m.resolution in ("YES", "NO")]
    streak_data = analyse_streaks(resolved)
    print("\n[4/5]  Pattern backtest ...")
    all_trades = []
    all_summaries = []
    best_by_sharpe = None
    for pat_key, pat_cfg in PATTERNS.items():
        pat_trades = run_pattern(pat_key, pat_cfg, resolved, candles_map)
        all_trades.extend(pat_trades)
        for coin in COINS + ["ALL"]:
            subset = [t for t in pat_trades if coin == "ALL" or t.coin == coin]
            if not subset:
                continue
            s = summarise(pat_key, coin, subset)
            all_summaries.append(s)
            if coin == "ALL" and s.trades >= 5:
                if best_by_sharpe is None or s.sharpe > best_by_sharpe.sharpe:
                    best_by_sharpe = s
    print("\n[5/5]  Saving output files ...")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    save_csv(f"{OUTPUT_DIR}/raw_markets.csv", all_markets,
        ["condition_id","coin","question","start_ts","end_ts","duration_min",
         "yes_token","no_token","resolution","final_yes_price","volume"])
    save_csv(f"{OUTPUT_DIR}/price_history.csv", all_candles,
        ["condition_id","coin","ts","dt","price","minutes_to_close"])
    save_csv(f"{OUTPUT_DIR}/backtest_trades.csv", all_trades,
        ["pattern","condition_id","coin","question","entry_ts","entry_dt",
         "minutes_to_close","side","entry_price","resolution","win","gross_pnl","net_pnl","roi_pct"])
    save_csv(f"{OUTPUT_DIR}/pattern_summary.csv", all_summaries,
        ["pattern","coin","trades","wins","losses","win_rate","total_net_pnl",
         "avg_net_pnl","max_drawdown","profit_factor","sharpe"])
    save_json(f"{OUTPUT_DIR}/summary_report.json", {
        "generated_at": _fmt(_now_ts()),
        "mode": "synthetic" if SYNTHETIC_MODE else "live",
        "best_pattern": asdict(best_by_sharpe) if best_by_sharpe else None,
        "all_summaries": [asdict(s) for s in all_summaries],
        "streak_analysis": streak_data,
    })
    print(f"  Done — {len(all_markets)} markets, {len(all_candles):,} candles, {len(all_trades)} trades")
    print("=" * 72)

if __name__ == "__main__":
    asyncio.run(main())
