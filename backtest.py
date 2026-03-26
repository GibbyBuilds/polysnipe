"""
backtest.py  —  Historical backtesting for the BTC strategy

Downloads Binance candles and simulates the bot's TA logic across past windows.
Outputs a summary table and optionally an Excel workbook.

Usage:
    python backtest.py --window 5 --hours 72
    python backtest.py --window 15 --hours 168 --output results.xlsx --mode safe
"""

import argparse
import time
import logging
from typing import List, Dict

import requests

try:
    import openpyxl
    EXCEL_AVAILABLE = True
except ImportError:
    EXCEL_AVAILABLE = False

log = logging.getLogger("backtest")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")

BINANCE_API = "https://api.binance.com/api/v3"


# ─── Token price model ────────────────────────────────────────────────────────
def simulated_token_price(delta_pct: float) -> float:
    """
    Piecewise linear model: as delta grows, the winning token costs more
    because market makers see the same signal.
    Prevents the backtest from assuming cheap tokens.
    """
    d = abs(delta_pct)
    if   d < 0.005: return 0.50
    elif d < 0.01:  return 0.52
    elif d < 0.02:  return 0.55
    elif d < 0.05:  return 0.65
    elif d < 0.10:  return 0.80
    elif d < 0.15:  return 0.90
    else:           return 0.95


# ─── Candle fetcher ───────────────────────────────────────────────────────────
def fetch_candles(interval: str, start_ms: int, end_ms: int) -> List[dict]:
    candles = []
    limit   = 1000
    current = start_ms
    while current < end_ms:
        try:
            resp = requests.get(
                f"{BINANCE_API}/klines",
                params={
                    "symbol":    "BTCUSDT",
                    "interval":  interval,
                    "startTime": current,
                    "endTime":   end_ms,
                    "limit":     limit,
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data:
                break
            for c in data:
                candles.append({
                    "open_time": int(c[0]) // 1000,
                    "open":      float(c[1]),
                    "high":      float(c[2]),
                    "low":       float(c[3]),
                    "close":     float(c[4]),
                    "volume":    float(c[5]),
                })
            current = int(data[-1][6]) + 1  # next candle after last close time
        except Exception as e:
            log.error(f"Fetch error: {e}")
            break
    return candles


# ─── Strategy replay ──────────────────────────────────────────────────────────
def _ema(prices, period):
    if len(prices) < period:
        return prices[-1] if prices else 0.0
    k = 2 / (period + 1)
    e = prices[0]
    for p in prices[1:]:
        e = p * k + e * (1 - k)
    return e


def _rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [d for d in deltas if d > 0]
    losses = [-d for d in deltas if d < 0]
    ag = sum(gains[-period:]) / period if gains else 0
    al = sum(losses[-period:]) / period if losses else 1e-9
    return 100 - 100 / (1 + ag / al)


def compute_signal(window_candle: dict, prior_1m_candles: List[dict]) -> dict:
    """Replay composite signal on a single window candle + surrounding 1m data."""
    score = 0.0

    closes  = [c["close"]  for c in prior_1m_candles[-30:]]
    volumes = [c["volume"] for c in prior_1m_candles[-30:]]
    current = window_candle["close"]   # T-10s approximation
    win_open = window_candle["open"]

    # 1. Window delta
    delta = (current - win_open) / win_open * 100
    if   abs(delta) > 0.10: w = 7
    elif abs(delta) > 0.02: w = 5
    elif abs(delta) > 0.005: w = 3
    else:                   w = 1
    score += w if delta > 0 else -w

    # 2. Micro momentum
    if len(closes) >= 3:
        mm = 2.0 if closes[-1] > closes[-2] > closes[-3] else (
            -2.0 if closes[-1] < closes[-2] < closes[-3] else 0.0)
        score += mm

    # 3. Acceleration
    if len(closes) >= 4:
        m1 = closes[-1] - closes[-2]
        m2 = closes[-2] - closes[-3]
        if m1 * m2 > 0:
            score += (1.5 if abs(m1) > abs(m2) else 0.5) * (1 if m1 > 0 else -1)

    # 4. EMA 9/21
    if len(closes) >= 21:
        score += 1.0 if _ema(closes, 9) > _ema(closes, 21) else -1.0

    # 5. RSI
    rsi = _rsi(closes)
    if   rsi > 75: score += 2.0
    elif rsi < 25: score -= 2.0
    elif rsi > 55: score += 1.0
    elif rsi < 45: score -= 1.0

    # 6. Volume surge
    if len(volumes) >= 6:
        rv = sum(volumes[-3:]) / 3
        pv = sum(volumes[-6:-3]) / 3
        if pv > 0 and rv / pv >= 1.5:
            score += 1.0 if closes[-1] >= (closes[-4] if len(closes) >= 4 else closes[-1]) else -1.0

    direction  = "up" if score >= 0 else "down"
    confidence = min(abs(score) / 7.0, 1.0)
    return {
        "direction":  direction,
        "score":      score,
        "confidence": confidence,
        "delta":      delta,
    }


# ─── Simulation ───────────────────────────────────────────────────────────────
BET_MODES = {
    "safe":  0.25,
    "degen": 1.0,
    "flat":  None,  # fixed bet size, no compounding
}

def simulate(window_candles, all_1m_candles, window_seconds, mode, conf_thresh, bankroll=50.0, flat_bet=5.0):
    min_bet = 5.0
    trades  = []
    br      = bankroll
    is_flat = (mode == "flat")

    # Build a lookup: open_time -> [1m candles up to that point]
    one_m_by_time = {c["open_time"]: c for c in all_1m_candles}

    for wc in window_candles:
        if br < min_bet:
            break

        # Gather prior 1m candles (up to T-10s in the window)
        prior = [c for c in all_1m_candles if c["open_time"] < wc["open_time"] + window_seconds - 10][-30:]

        sig = compute_signal(wc, prior)
        if sig["confidence"] < conf_thresh:
            continue

        # Skip sideways markets
        if abs(sig["delta"]) < 0.005:
            continue

        token_price = simulated_token_price(sig["delta"])

        # Skip if spread too wide
        if token_price > 0.96:
            continue

        if is_flat:
            bet = flat_bet
        else:
            bet = max(br * BET_MODES.get(mode, 0.25), min_bet)
        bet = min(bet, br)
        shares = bet / token_price

        actual = "up" if wc["close"] >= wc["open"] else "down"
        won = actual == sig["direction"]

        if won:
            payout = shares * 1.0
            profit = payout - bet
            br    += profit
        else:
            br -= bet

        trades.append({
            "window_ts":   wc["open_time"],
            "direction":   sig["direction"],
            "actual":      actual,
            "confidence":  sig["confidence"],
            "token_price": token_price,
            "bet":         bet,
            "won":         won,
            "bankroll":    br,
        })

    wins   = sum(1 for t in trades if t["won"])
    losses = len(trades) - wins
    return {
        "trades":   trades,
        "wins":     wins,
        "losses":   losses,
        "win_rate": wins / len(trades) if trades else 0,
        "final_br": br,
        "roi":      (br - bankroll) / bankroll,
    }


# ─── Excel output ─────────────────────────────────────────────────────────────
def write_excel(results_by_conf, output_path: str):
    if not EXCEL_AVAILABLE:
        log.warning("openpyxl not installed — skipping Excel output")
        return

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Summary"

    headers = ["Conf Thresh", "Mode", "Trades", "Wins", "Losses", "Win Rate", "Final BR", "ROI"]
    ws.append(headers)
    for row in results_by_conf:
        ws.append([
            f"{row['conf']:.0%}",
            row["mode"],
            row["trades"],
            row["wins"],
            row["losses"],
            f"{row['win_rate']:.1%}",
            f"${row['final_br']:.2f}",
            f"{row['roi']:+.1%}",
        ])

    wb.save(output_path)
    log.info(f"Results written to {output_path}")


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Polymarket BTC Bot Backtester")
    parser.add_argument("--window",  type=int, default=5,   choices=[5, 15])
    parser.add_argument("--hours",   type=int, default=72,  help="How many hours to backtest")
    parser.add_argument("--output",  type=str, default=None, help="Path to output Excel file")
    parser.add_argument("--mode",    type=str, default="flat", choices=["safe", "degen", "flat"])
    parser.add_argument("--bankroll",type=float, default=50.0)
    args = parser.parse_args()

    ws  = args.window * 60
    now = int(time.time())
    end_ms   = now * 1000
    start_ms = (now - args.hours * 3600) * 1000

    interval = "5m" if args.window == 5 else "15m"
    log.info(f"Fetching {args.window}min candles for last {args.hours}h…")
    wc = fetch_candles(interval, start_ms, end_ms)
    log.info(f"Fetching 1m candles for indicators…")
    mc = fetch_candles("1m", start_ms, end_ms)

    if not wc:
        log.error("No candles fetched. Exiting.")
        return

    log.info(f"Backtesting {len(wc)} windows…\n")

    results = []
    conf_thresholds = [0.0, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60]
    for ct in conf_thresholds:
        r = simulate(wc, mc, ws, args.mode, ct, args.bankroll, flat_bet=5.0)
        results.append({**r, "conf": ct, "mode": args.mode})
        log.info(
            f"  conf≥{ct:.0%}  trades={len(r['trades']):3d}  "
            f"W/L={r['wins']}/{r['losses']}  "
            f"wr={r['win_rate']:.1%}  "
            f"final=${r['final_br']:.2f}  "
            f"ROI={r['roi']:+.1%}"
        )

    if args.output:
        write_excel(results, args.output)


if __name__ == "__main__":
    main()
