"""
strategy.py  —  Composite technical-analysis signal for BTC Up/Down markets

Indicators and weights (tuned for short windows):
  1. Window Delta      weight 5-7  — THE dominant signal
  2. Micro Momentum    weight 2    — last 2 candles direction
  3. Acceleration      weight 1.5  — is momentum building or fading?
  4. EMA 9/21          weight 1    — short-term trend
  5. RSI 14            weight 1-2  — overbought / oversold extremes
  6. Volume Surge      weight 1    — confirms direction
  7. Tick Trend        weight 2    — 2-sec real-time tick micro-trend
"""

import time
import logging
import requests
from dataclasses import dataclass, field
from typing import List, Optional

log = logging.getLogger("strategy")

BINANCE_API   = "https://api.binance.com/api/v3"
# Accumulated real-time ticks between calls
_tick_buffer: List[float] = []


@dataclass
class Signal:
    direction:        str    = "up"    # "up" | "down"
    score:            float  = 0.0
    confidence:       float  = 0.0
    window_delta_pct: float  = 0.0
    indicators:       dict   = field(default_factory=dict)
    skip_flat:        bool   = False   # True when market is moving < 0.005% (sideways)


# ─── Real-time tick accumulator ───────────────────────────────────────────────
def record_tick(price: float):
    """Call every 2s during the snipe window to build a micro-trend."""
    _tick_buffer.append(price)
    if len(_tick_buffer) > 30:
        _tick_buffer.pop(0)


def _tick_trend() -> float:
    """Return a score for recent tick momentum (+ve = up, -ve = down)."""
    if len(_tick_buffer) < 4:
        return 0.0
    ups   = sum(1 for i in range(1, len(_tick_buffer)) if _tick_buffer[i] > _tick_buffer[i - 1])
    downs = sum(1 for i in range(1, len(_tick_buffer)) if _tick_buffer[i] < _tick_buffer[i - 1])
    total = ups + downs
    if total == 0:
        return 0.0
    ratio = (ups - downs) / total
    move  = (_tick_buffer[-1] - _tick_buffer[0]) / _tick_buffer[0] * 100
    if abs(ratio) < 0.60 or abs(move) < 0.005:
        return 0.0
    return 2.0 if ratio > 0 else -2.0


# ─── Binance helpers ──────────────────────────────────────────────────────────
def _fetch_candles(symbol: str = "BTCUSDT", interval: str = "1m", limit: int = 30) -> List[dict]:
    try:
        resp = requests.get(
            f"{BINANCE_API}/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=5,
        )
        resp.raise_for_status()
        raw = resp.json()
        return [
            {
                "open":   float(c[1]),
                "high":   float(c[2]),
                "low":    float(c[3]),
                "close":  float(c[4]),
                "volume": float(c[5]),
            }
            for c in raw
        ]
    except Exception as e:
        log.warning(f"Binance candle fetch failed: {e}")
        return []


def _fetch_btc_price() -> Optional[float]:
    try:
        resp = requests.get(
            f"{BINANCE_API}/ticker/price",
            params={"symbol": "BTCUSDT"},
            timeout=3,
        )
        resp.raise_for_status()
        price = float(resp.json()["price"])
        record_tick(price)
        return price
    except Exception as e:
        log.warning(f"BTC price fetch failed: {e}")
        return None


def _fetch_window_open_price(window_ts: int, window_minutes: int) -> Optional[float]:
    """Fetch the BTC open price at the exact window start via Binance klines."""
    interval = "5m" if window_minutes == 5 else "15m"
    try:
        resp = requests.get(
            f"{BINANCE_API}/klines",
            params={
                "symbol":    "BTCUSDT",
                "interval":  interval,
                "startTime": window_ts * 1000,
                "limit":     1,
            },
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        if data:
            return float(data[0][1])  # open price
    except Exception as e:
        log.warning(f"Window open price fetch failed: {e}")
    return None


# ─── Indicator functions ──────────────────────────────────────────────────────
def _ema(prices: List[float], period: int) -> float:
    if len(prices) < period:
        return prices[-1] if prices else 0.0
    k   = 2 / (period + 1)
    ema = prices[0]
    for p in prices[1:]:
        ema = p * k + ema * (1 - k)
    return ema


def _rsi(closes: List[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [d for d in deltas if d > 0]
    losses = [-d for d in deltas if d < 0]
    avg_gain = sum(gains[-period:]) / period if gains else 0
    avg_loss = sum(losses[-period:]) / period if losses else 1e-9
    rs       = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# ─── Main analysis entry point ────────────────────────────────────────────────
def analyze(window_ts: int, window_minutes: int) -> Signal:
    """
    Run all 7 indicators and return a composite Signal.
    Called every 2 seconds during the T-10s snipe window.
    """
    candles = _fetch_candles(limit=30)
    current = _fetch_btc_price()
    win_open = _fetch_window_open_price(window_ts, window_minutes)

    if not candles or current is None:
        log.warning("Insufficient data — returning neutral signal")
        return Signal()

    closes  = [c["close"]  for c in candles]
    volumes = [c["volume"] for c in candles]

    score        = 0.0
    indicators   = {}

    # ── 1. Window Delta (weight 5-7) ──────────────────────────────────────────
    win_delta_pct = 0.0
    win_weight    = 0
    if win_open:
        win_delta_pct = (current - win_open) / win_open * 100
        if   abs(win_delta_pct) > 0.10:  win_weight = 7
        elif abs(win_delta_pct) > 0.02:  win_weight = 5
        elif abs(win_delta_pct) > 0.005: win_weight = 3
        else:                             win_weight = 1
        score += win_weight if win_delta_pct > 0 else -win_weight
    indicators["window_delta"] = {"pct": win_delta_pct, "weight": win_weight}

    # ── 2. Micro Momentum (weight 2) ──────────────────────────────────────────
    if len(closes) >= 3:
        micro = 2.0 if closes[-1] > closes[-2] > closes[-3] else (
               -2.0 if closes[-1] < closes[-2] < closes[-3] else 0.0)
        score += micro
        indicators["micro_momentum"] = micro

    # ── 3. Acceleration (weight 1.5) ──────────────────────────────────────────
    if len(closes) >= 4:
        move_latest = closes[-1] - closes[-2]
        move_prev   = closes[-2] - closes[-3]
        if move_latest * move_prev > 0:                        # same direction
            if abs(move_latest) > abs(move_prev):              # accelerating
                score += 1.5 if move_latest > 0 else -1.5
            else:                                               # decelerating
                score += 0.5 if move_latest > 0 else -0.5
        indicators["acceleration"] = move_latest - move_prev

    # ── 4. EMA 9/21 Crossover (weight 1) ──────────────────────────────────────
    ema9  = _ema(closes, 9)
    ema21 = _ema(closes, 21)
    ema_cross = 1.0 if ema9 > ema21 else -1.0
    score    += ema_cross
    indicators["ema_cross"] = {"ema9": ema9, "ema21": ema21}

    # ── 5. RSI 14 (weight 1-2) ────────────────────────────────────────────────
    rsi = _rsi(closes)
    rsi_score = 0.0
    if   rsi > 75: rsi_score =  2.0   # overbought, likely Up win if already moving up
    elif rsi < 25: rsi_score = -2.0   # oversold
    elif rsi > 55: rsi_score =  1.0
    elif rsi < 45: rsi_score = -1.0
    score += rsi_score
    indicators["rsi"] = {"value": rsi, "score": rsi_score}

    # ── 6. Volume Surge (weight 1) ────────────────────────────────────────────
    if len(volumes) >= 6:
        recent_vol = sum(volumes[-3:]) / 3
        prior_vol  = sum(volumes[-6:-3]) / 3
        if prior_vol > 0 and recent_vol / prior_vol >= 1.5:
            # Volume surge confirms the current directional move
            dir_score  = 1.0 if closes[-1] >= closes[-4] else -1.0
            score     += dir_score
            indicators["volume_surge"] = True
        else:
            indicators["volume_surge"] = False

    # ── 7. Tick Trend (weight 2) ──────────────────────────────────────────────
    tick_score = _tick_trend()
    score     += tick_score
    indicators["tick_trend"] = tick_score

    # ── Final signal ──────────────────────────────────────────────────────────
    direction  = "up" if score >= 0 else "down"
    # Divide by 7 (window_delta max weight) so high-delta = 1.0 confidence quickly
    confidence = min(abs(score) / 7.0, 1.0)

    # Flag sideways markets: abs(window_delta_pct) < 0.01%
    skip_flat = abs(win_delta_pct) < 0.01

    return Signal(
        direction=direction,
        score=score,
        confidence=confidence,
        window_delta_pct=win_delta_pct,
        indicators=indicators,
        skip_flat=skip_flat,
    )


# ─── Outcome checker (dry-run / backtesting) ──────────────────────────────────
def check_actual_outcome(window_ts: int, window_minutes: int) -> str:
    """
    After the window closes, check what BTC actually did via Binance.
    Returns "up" or "down".
    """
    interval = "5m" if window_minutes == 5 else "15m"
    try:
        resp = requests.get(
            f"{BINANCE_API}/klines",
            params={
                "symbol":    "BTCUSDT",
                "interval":  interval,
                "startTime": window_ts * 1000,
                "limit":     1,
            },
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        if data:
            open_  = float(data[0][1])
            close_ = float(data[0][4])
            return "up" if close_ >= open_ else "down"
    except Exception as e:
        log.warning(f"Outcome check failed: {e}")
    return "unknown"
