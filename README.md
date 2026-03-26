# PolySnipe ⚡

**Automated BTC prediction market bot for Polymarket.**

Snipes BTC Up/Down binary markets at T-10 seconds before window close — when direction is locked in but tokens aren't fully priced. Built with Python and the official [Polymarket CLOB SDK](https://github.com/Polymarket/py-clob-client).

## How It Works

Every 5 or 15 minutes, Polymarket opens a market: *"Will BTC close higher or lower than the opening price?"*

PolySnipe:
1. Waits until **10 seconds before the window closes** (the sweet spot)
2. Runs a **7-indicator composite signal** on real-time Binance data
3. Buys the winning token via FOK market order (with GTC limit fallback)
4. Tracks results, manages bankroll, and adapts

```
Window open                              Window close
    │────────────────────────────────────────│
                                  │     │
                               T-10s  T-0s
                              (snipe)
```

## Strategy: 7-Indicator Composite Signal

| # | Indicator | Weight | Purpose |
|---|-----------|--------|---------|
| 1 | **Window Delta** | 5–7 | Is BTC up/down vs window open? Dominant signal |
| 2 | Micro Momentum | 2 | Last 2 candles direction |
| 3 | Acceleration | 1.5 | Is momentum building or fading? |
| 4 | EMA 9/21 | 1 | Short-term trend |
| 5 | RSI 14 | 1–2 | Overbought/oversold extremes |
| 6 | Volume Surge | 1 | Confirms direction |
| 7 | Tick Trend | 2 | Real-time 2-second micro-trend |

## Risk Management

- **Drawdown circuit breaker** — 30% drop from peak pauses trading for 1 hour, 50% stops the bot
- **Confidence-based bet sizing** — Higher confidence = larger position (10/20/30% tiers)
- **Sideways market filter** — Skips windows where BTC delta < 0.005%
- **Spread filter** — Skips if best token price > $0.96
- **Cooldown** — Pauses after 3 consecutive losses
- **Win rate tracking** — Tracks performance by confidence bucket (low/medium/high)

## Trading Modes

| Mode | Bet Size | Min Confidence | Philosophy |
|------|----------|----------------|------------|
| `safe` | 10-30% (confidence-scaled) | 30% | Survive losing streaks, compound slowly |
| `aggressive` | Profits only (scaled) | 20% | Compound fast, protect principal |
| `degen` | 100% | 0% | All-in. Not recommended. |

## Backtesting

```bash
# Flat $5 bets, 7-day backtest
python backtest.py --window 5 --hours 168 --mode flat

# Compounding mode
python backtest.py --window 5 --hours 72 --mode safe --bankroll 50

# Export results
python backtest.py --window 15 --hours 168 --output results.xlsx
```

**Sample results (7 days, flat $5 bets):**

| Confidence | Trades | Win Rate | $50 → |
|---|---|---|---|
| ≥0% | 1,923 | 97.9% | $3,046 |
| ≥30% | 1,777 | 99.6% | $2,828 |
| ≥40% | 1,741 | 100% | $2,799 |

## Quick Start

```bash
# Clone & setup
git clone https://github.com/thegizzybot/polysnipe.git
cd polysnipe
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt

# Configure credentials
python setup_creds.py

# Dry run (no real money)
python bot.py --dry-run --mode safe --window 5

# Live trading
python bot.py --mode safe --window 5

# With Telegram notifications
python bot.py --mode safe --window 5 --telegram
```

## Telegram Notifications

Get real-time trade alerts on your phone. Add to `.env`:

```
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

Then run with `--telegram`.

## Project Structure

```
polysnipe/
├── bot.py           — Trading engine, loop, order execution, risk management
├── strategy.py      — 7-indicator TA signal engine
├── market_finder.py — Polymarket Gamma API market discovery
├── backtest.py      — Historical backtesting with multiple modes
├── setup_creds.py   — One-time credential setup
├── requirements.txt
└── README.md
```

## CLI Reference

```
python bot.py [--window {5,15}] [--mode {safe,aggressive,degen}]
              [--dry-run] [--once] [--max-trades N] [--telegram]
```

## ⚠️ Disclaimer

This bot trades real money on prediction markets. You can lose your entire bankroll.
- Always test with `--dry-run` before using real funds
- Start small (< $20 USDC)
- Past backtesting performance does not guarantee future results
- Check Polymarket's geographic restrictions

## Built With

- [py-clob-client](https://github.com/Polymarket/py-clob-client) — Official Polymarket SDK
- [Binance API](https://binance-docs.github.io/apidocs/) — Real-time BTC price data
- Python 3.9+

---

**License:** MIT
