"""
Polymarket BTC Up/Down Bot — Main Trading Engine
Uses official py-clob-client (github.com/Polymarket/py-clob-client)

Supports: 5-minute and 15-minute BTC Up/Down markets
Entry timing: T-10s before window close (sweet spot for accuracy vs price)
"""

import os
import sys
import time
import asyncio
import argparse
import logging
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

import strategy
import market_finder

load_dotenv()

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log"),
    ],
)
log = logging.getLogger("bot")

# ─── Constants ────────────────────────────────────────────────────────────────
HOST         = "https://clob.polymarket.com"
GAMMA_HOST   = "https://gamma-api.polymarket.com"
CHAIN_ID     = 137          # Polygon mainnet
MIN_SHARES   = 5            # Polymarket minimum order size
ENTRY_OFFSET = 10           # seconds before window close to enter
SNIPE_LOOP_INTERVAL = 2     # seconds between TA re-checks in snipe window

# Drawdown thresholds
DRAWDOWN_PAUSE_PCT = 0.30   # 30% drop from peak → pause 1 hour
DRAWDOWN_STOP_PCT  = 0.50   # 50% drop from peak → stop entirely
DRAWDOWN_PAUSE_SECS = 3600  # 1 hour pause

# Cooldown after consecutive losses
COOLDOWN_LOSS_COUNT = 3     # pause one window after this many consecutive losses

# Order book spread: skip if best price > this
MAX_TOKEN_PRICE = 0.96


class BotConfig:
    def __init__(self, args):
        self.window_minutes = args.window          # 5 or 15
        self.window_seconds = args.window * 60
        self.mode           = args.mode            # safe | aggressive | degen
        self.dry_run        = args.dry_run
        self.max_trades     = args.max_trades      # None = infinite
        self.once           = args.once
        self.telegram       = args.telegram        # bool

        # Bankroll from env / defaults
        self.bankroll     = float(os.getenv("STARTING_BANKROLL", "10.0"))
        self.min_bet      = float(os.getenv("MIN_BET",           "5.0"))
        self.max_bet      = float(os.getenv("MAX_BET",           "25.0"))

        # Bet-sizing per mode (flat fractions — used in safe/degen)
        self.bet_fractions = {
            "safe":       0.25,
            "aggressive": None,   # handled separately (profits only)
            "degen":      1.0,
        }
        self.min_confidence = {
            "safe":       0.40,
            "aggressive": 0.30,
            "degen":      0.00,
        }

    def bet_size(self, profits_only: float = 0.0) -> float:
        """Flat bet sizing (legacy). Used as fallback; prefer dynamic_bet_size."""
        if self.mode == "aggressive":
            return max(profits_only, self.min_bet)
        return max(self.bankroll * self.bet_fractions[self.mode], self.min_bet)

    def dynamic_bet_size(self, confidence: float, profits: float = 0.0) -> float:
        """
        Confidence-based dynamic bet sizing.

        Safe mode:
          0.3–0.5 confidence → 10% of bankroll
          0.5–0.7 confidence → 20% of bankroll
          0.7–1.0 confidence → 30% of bankroll

        Aggressive mode:
          Same ratios but applied to profits above original bankroll.

        Degen mode:
          Always 100% of bankroll (no scaling).
        """
        if self.mode == "degen":
            return max(self.bankroll, self.min_bet)

        # Determine fraction by confidence tier
        if confidence >= 0.70:
            fraction = 0.30
        elif confidence >= 0.50:
            fraction = 0.20
        else:
            fraction = 0.10  # 0.3–0.5 range

        if self.mode == "aggressive":
            # Apply fraction to profits above original; fall back to min_bet
            base = max(profits, 0.0)
            return max(base * fraction, self.min_bet)

        # Safe mode: apply to full bankroll
        return max(self.bankroll * fraction, self.min_bet)

    def confidence_threshold(self) -> float:
        return self.min_confidence[self.mode]


# ─── Telegram helpers ─────────────────────────────────────────────────────────
def _tg_send(token: str, chat_id: str, text: str):
    """Fire-and-forget Telegram message. Silently swallows errors."""
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception as e:
        log.debug(f"Telegram send failed: {e}")


class PolymarketBot:
    def __init__(self, config: BotConfig):
        self.cfg    = config
        self.client = self._init_client()
        self.trades = 0
        self.wins   = 0
        self.losses = 0
        self.original_bankroll      = config.bankroll
        self.profits_above_original = 0.0

        # Drawdown circuit breaker
        self.peak_bankroll    = config.bankroll

        # Cooldown tracking
        self.consecutive_losses = 0
        self.cooldown_next      = False   # skip next window flag

        # Win rate by confidence bucket: {bucket: [wins, total]}
        self.conf_buckets = {
            "low":    [0, 0],   # confidence 0.0–0.4
            "medium": [0, 0],   # confidence 0.4–0.7
            "high":   [0, 0],   # confidence 0.7–1.0
        }

        # Telegram
        self._tg_token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._tg_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    def _notify(self, text: str):
        """Send Telegram notification if --telegram flag is set."""
        if self.cfg.telegram:
            _tg_send(self._tg_token, self._tg_chat_id, text)

    def _conf_bucket(self, confidence: float) -> str:
        if confidence < 0.4:
            return "low"
        elif confidence < 0.7:
            return "medium"
        return "high"

    def _record_bucket(self, confidence: float, won: bool):
        bucket = self._conf_bucket(confidence)
        self.conf_buckets[bucket][1] += 1
        if won:
            self.conf_buckets[bucket][0] += 1

    # ─── Client init ──────────────────────────────────────────────────────────
    def _init_client(self) -> ClobClient:
        pk    = os.getenv("POLY_PRIVATE_KEY")
        funder = os.getenv("POLY_FUNDER_ADDRESS")

        if not pk:
            log.error("POLY_PRIVATE_KEY not set in .env")
            sys.exit(1)

        if funder:
            # Email / Magic / proxy wallet
            client = ClobClient(
                HOST,
                key=pk,
                chain_id=CHAIN_ID,
                signature_type=1,
                funder=funder,
            )
        else:
            # EOA / MetaMask
            client = ClobClient(HOST, key=pk, chain_id=CHAIN_ID)

        if not self.cfg.dry_run:
            client.set_api_creds(client.create_or_derive_api_creds())
            log.info("API credentials initialised ✓")

        return client

    # ─── Bankroll helpers ─────────────────────────────────────────────────────
    def _update_bankroll_from_chain(self):
        """Sync bankroll from actual on-chain balance (skip in dry-run)."""
        if self.cfg.dry_run:
            return
        try:
            raw = self.client.get_balance()
            self.cfg.bankroll = int(raw) / 1e6
            log.info(f"On-chain balance: ${self.cfg.bankroll:.2f} USDC")
        except Exception as e:
            log.warning(f"Could not fetch balance: {e}")

    def _update_peak_bankroll(self):
        """Keep peak bankroll up to date."""
        if self.cfg.bankroll > self.peak_bankroll:
            self.peak_bankroll = self.cfg.bankroll

    def _check_drawdown(self) -> str:
        """
        Check drawdown against peak bankroll.
        Returns 'ok', 'pause', or 'stop'.
        """
        if self.peak_bankroll <= 0:
            return "ok"
        drawdown = (self.peak_bankroll - self.cfg.bankroll) / self.peak_bankroll
        if drawdown >= DRAWDOWN_STOP_PCT:
            return "stop"
        if drawdown >= DRAWDOWN_PAUSE_PCT:
            return "pause"
        return "ok"

    # ─── Order book depth check ───────────────────────────────────────────────
    def _get_best_price(self, token_id: str) -> float:
        """
        Fetch the best available ask price for a token.
        Returns the price or 0.0 on failure.
        """
        try:
            book = self.client.get_order_book(token_id)
            # book is typically a dict with 'asks': [{'price': ..., 'size': ...}, ...]
            asks = book.get("asks") if isinstance(book, dict) else None
            if asks:
                # Best ask is the lowest ask price
                best_ask = min(float(a["price"]) for a in asks if "price" in a)
                return best_ask
        except Exception as e:
            log.debug(f"Order book fetch failed for {token_id}: {e}")
        return 0.0

    # ─── Main loop ────────────────────────────────────────────────────────────
    def run(self):
        log.info("=" * 60)
        log.info(f"  Polymarket BTC Bot  |  {self.cfg.window_minutes}min window  |  mode={self.cfg.mode}")
        log.info(f"  dry_run={self.cfg.dry_run}  bankroll=${self.cfg.bankroll:.2f}")
        log.info("=" * 60)

        self._update_bankroll_from_chain()
        self.peak_bankroll = self.cfg.bankroll  # set peak after syncing on-chain

        self._notify(
            f"🤖 <b>Bot started</b>\n"
            f"Mode: {self.cfg.mode} | Window: {self.cfg.window_minutes}min\n"
            f"Bankroll: ${self.cfg.bankroll:.2f} | dry_run={self.cfg.dry_run}"
        )

        while True:
            if self.cfg.max_trades and self.trades >= self.cfg.max_trades:
                log.info(f"Reached max trades ({self.cfg.max_trades}). Stopping.")
                break

            if self.cfg.bankroll < self.cfg.min_bet:
                log.warning(f"Bankroll ${self.cfg.bankroll:.2f} below min bet ${self.cfg.min_bet:.2f}. Pausing 60s.")
                time.sleep(60)
                self._update_bankroll_from_chain()
                continue

            # ── Drawdown circuit breaker ──────────────────────────────────────
            self._update_peak_bankroll()
            dd_status = self._check_drawdown()

            if dd_status == "stop":
                msg = (
                    f"🛑 DRAWDOWN STOP: bankroll ${self.cfg.bankroll:.2f} is "
                    f"≥50% below peak ${self.peak_bankroll:.2f}. Stopping bot."
                )
                log.error(msg)
                self._notify(f"🛑 <b>Circuit Breaker — STOPPED</b>\n{msg}")
                break

            if dd_status == "pause":
                drawdown_pct = (self.peak_bankroll - self.cfg.bankroll) / self.peak_bankroll
                msg = (
                    f"⚠️  DRAWDOWN PAUSE: bankroll ${self.cfg.bankroll:.2f} is "
                    f"{drawdown_pct:.0%} below peak ${self.peak_bankroll:.2f}. "
                    f"Pausing {DRAWDOWN_PAUSE_SECS // 60} minutes."
                )
                log.warning(msg)
                self._notify(f"⚠️ <b>Circuit Breaker — PAUSED 1h</b>\n{msg}")
                time.sleep(DRAWDOWN_PAUSE_SECS)
                self._update_bankroll_from_chain()
                self._update_peak_bankroll()
                log.info("Resuming after drawdown pause.")
                continue

            self._trade_one_cycle()

            if self.cfg.once:
                log.info("--once flag set, exiting after one cycle.")
                break

        self._print_summary()

    def _print_summary(self):
        wr = self.wins / self.trades if self.trades else 0
        summary_lines = [
            f"\n{'=' * 60}",
            f"  SESSION SUMMARY",
            f"  Trades: {self.trades}  |  Wins: {self.wins}  |  Losses: {self.losses}  |  Win rate: {wr:.0%}",
            f"  Final bankroll: ${self.cfg.bankroll:.2f}",
            f"  Peak bankroll:  ${self.peak_bankroll:.2f}",
            f"\n  Win Rate by Confidence Bucket:",
        ]
        for bucket, (wins, total) in self.conf_buckets.items():
            bucket_wr = wins / total if total else 0
            summary_lines.append(
                f"    {bucket.capitalize():8s}  {wins:3d}W / {total:3d}T  ({bucket_wr:.0%})"
            )
        summary_lines.append("=" * 60)
        summary = "\n".join(summary_lines)
        log.info(summary)

        # Telegram summary
        bucket_lines = "\n".join(
            f"  {b.capitalize()}: {v[0]}W/{v[1]}T ({v[0]/v[1]:.0%})" if v[1] else f"  {b.capitalize()}: no trades"
            for b, v in self.conf_buckets.items()
        )
        self._notify(
            f"📊 <b>Session ended</b>\n"
            f"Trades: {self.trades} | W/L: {self.wins}/{self.losses} | WR: {wr:.0%}\n"
            f"Bankroll: ${self.cfg.bankroll:.2f}\n\n"
            f"<b>Win rate by confidence:</b>\n{bucket_lines}"
        )

    def _trade_one_cycle(self):
        now  = int(time.time())
        ws   = self.cfg.window_seconds
        window_ts  = now - (now % ws)
        close_time = window_ts + ws
        wait_until = close_time - ENTRY_OFFSET

        # ── Cooldown check ────────────────────────────────────────────────────
        if self.cooldown_next:
            self.cooldown_next = False
            log.info(f"  Cooling down after {COOLDOWN_LOSS_COUNT} losses — skipping this window")
            sleep_secs = close_time - time.time() + 2
            if sleep_secs > 0:
                time.sleep(sleep_secs)
            return

        # ── Wait until T-10s ──────────────────────────────────────────────────
        sleep_secs = wait_until - time.time()
        if sleep_secs > 0:
            log.info(
                f"Window {self.cfg.window_minutes}min | opens {datetime.fromtimestamp(window_ts, tz=timezone.utc).strftime('%H:%M:%S')} UTC "
                f"| closes {datetime.fromtimestamp(close_time, tz=timezone.utc).strftime('%H:%M:%S')} UTC "
                f"| sleeping {sleep_secs:.0f}s"
            )
            time.sleep(sleep_secs)

        # ── Snipe loop ────────────────────────────────────────────────────────
        best_signal = None
        best_score  = 0.0

        while time.time() < close_time - 2:
            signal = strategy.analyze(
                window_ts=window_ts,
                window_minutes=self.cfg.window_minutes,
            )
            log.info(
                f"  TA @ T-{close_time - time.time():.0f}s | "
                f"dir={signal.direction} score={signal.score:.2f} "
                f"conf={signal.confidence:.0%} | "
                f"win_delta={signal.window_delta_pct:+.4f}%"
            )

            if abs(signal.score) > abs(best_score):
                best_score  = signal.score
                best_signal = signal

            # Spike detection: sudden jump → fire now
            if best_signal and (abs(signal.score) - abs(best_score) >= 1.5):
                log.info("  ⚡ Spike detected — firing early!")
                break

            # Confidence threshold met
            if signal.confidence >= self.cfg.confidence_threshold():
                log.info(f"  OK Confidence {signal.confidence:.0%} >= threshold {self.cfg.confidence_threshold():.0%}")
                break

            time.sleep(SNIPE_LOOP_INTERVAL)

        # T-5s hard deadline: use best seen
        if best_signal is None:
            log.warning("  No signal generated — skipping cycle")
            time.sleep(5)
            return

        final_signal = best_signal

        # ── Skip sideways markets ─────────────────────────────────────────────
        if final_signal.skip_flat:
            log.info("  Sideways market — skipping")
            time.sleep(max(0, close_time - time.time() + 2))
            return

        log.info(
            f"  FINAL SIGNAL: {final_signal.direction.upper()} | "
            f"score={final_signal.score:.2f} | conf={final_signal.confidence:.0%}"
        )

        # ── Fetch market ──────────────────────────────────────────────────────
        if self.cfg.dry_run:
            # In dry-run, simulate token price from the backtest pricing model
            from backtest import simulated_token_price
            token_price = simulated_token_price(final_signal.window_delta_pct)
            token_id = None
            log.info(f"  [DRY RUN] Simulated token_price=${token_price:.3f} (delta={final_signal.window_delta_pct:+.4f}%)")
            if token_price > MAX_TOKEN_PRICE:
                log.info(f"  [DRY RUN] Spread too wide -- skipping (price ${token_price:.3f} > ${MAX_TOKEN_PRICE:.2f})")
                return
        else:
            market = market_finder.get_market(
                window_ts=window_ts,
                window_minutes=self.cfg.window_minutes,
            )
            if not market:
                log.error(f"  Market not found for window_ts={window_ts}")
                return

            token_id = market["up_token"] if final_signal.direction == "up" else market["down_token"]
            token_price = market["up_price"] if final_signal.direction == "up" else market["down_price"]
            log.info(f"  Market: {market['slug']} | token_price=${token_price:.3f}")

            # Order book depth check
            best_price = self._get_best_price(token_id)
            if best_price > MAX_TOKEN_PRICE:
                log.info(f"  Spread too wide -- skipping (best ask ${best_price:.3f} > ${MAX_TOKEN_PRICE:.2f})")
                return

        # ── Bet sizing ────────────────────────────────────────────────────────
        bet_usdc = self.cfg.dynamic_bet_size(final_signal.confidence, self.profits_above_original)
        bet_usdc = min(bet_usdc, self.cfg.bankroll, self.cfg.max_bet)
        shares   = bet_usdc / token_price if token_price > 0 else 0

        if shares < MIN_SHARES:
            log.warning(f"  Shares {shares:.1f} < minimum {MIN_SHARES}. Skipping.")
            return

        # ── Confidence bucket logging ─────────────────────────────────────────
        bucket = self._conf_bucket(final_signal.confidence)
        log.info(f"  Confidence bucket: {bucket} | bet=${bet_usdc:.2f}")

        # ── Execute ───────────────────────────────────────────────────────────
        if self.cfg.dry_run:
            self._dry_run_trade(
                window_ts, close_time, final_signal, token_price, bet_usdc, shares, bucket
            )
        else:
            self._live_trade(token_id, token_price, bet_usdc, shares, close_time, final_signal, bucket)

    # ─── Live trading ─────────────────────────────────────────────────────────
    def _live_trade(self, token_id, token_price, bet_usdc, shares, close_time, signal, bucket):
        log.info(f"  💰 PLACING ORDER: {signal.direction.upper()} | ${bet_usdc:.2f} | {shares:.1f} shares @ ${token_price:.3f}")
        self._notify(
            f"💰 <b>Trade placed</b>\n"
            f"Direction: {signal.direction.upper()} | Bet: ${bet_usdc:.2f}\n"
            f"Price: ${token_price:.3f} | Conf: {signal.confidence:.0%} ({bucket})"
        )

        # Primary: FOK market buy
        success = self._try_market_order(token_id, bet_usdc)

        if not success and token_price <= 0.96:
            # Fallback: GTC limit buy at $0.95
            log.info("  Falling back to GTC limit buy @ $0.95")
            success = self._try_limit_order(token_id, shares)

        if success:
            log.info("  ✅ Order placed successfully")
            self.trades += 1
            self.cfg.bankroll -= bet_usdc
        else:
            log.error("  ❌ Order failed")

        # Wait for resolution then check outcome
        wait = max(0, close_time - time.time() + 5)
        log.info(f"  Waiting {wait:.0f}s for resolution…")
        time.sleep(wait)
        self._check_and_record_result(token_id, bet_usdc, signal, token_price, bucket)

    def _try_market_order(self, token_id: str, amount_usdc: float) -> bool:
        """FOK market buy — retry up to 3 times."""
        for attempt in range(3):
            try:
                mo = MarketOrderArgs(
                    token_id=token_id,
                    amount=amount_usdc,
                    side=BUY,
                    order_type=OrderType.FOK,
                )
                signed = self.client.create_market_order(mo)
                resp   = self.client.post_order(signed, OrderType.FOK)
                if resp and resp.get("status") != "error":
                    return True
                log.warning(f"  FOK attempt {attempt+1} failed: {resp}")
            except Exception as e:
                log.warning(f"  FOK attempt {attempt+1} exception: {e}")
            time.sleep(3)
        return False

    def _try_limit_order(self, token_id: str, shares: float) -> bool:
        """GTC limit buy at $0.95."""
        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=0.95,
                size=max(shares, MIN_SHARES),
                side=BUY,
            )
            signed = self.client.create_order(order_args)
            resp   = self.client.post_order(signed, OrderType.GTC)
            return bool(resp and resp.get("status") != "error")
        except Exception as e:
            log.error(f"  GTC limit order failed: {e}")
            return False

    def _check_and_record_result(self, token_id, bet_usdc, signal, entry_price, bucket):
        """Poll Polymarket after resolution to determine win/loss."""
        try:
            # Get price of the token we bought post-resolution
            price = float(self.client.get_price(token_id, "SELL") or 0)
            won = price >= 0.99

            if won:
                payout = bet_usdc / entry_price  # shares × $1.00
                profit = payout - bet_usdc
                self.cfg.bankroll += payout
                self.profits_above_original = max(
                    0, self.cfg.bankroll - self.original_bankroll
                )
                self.wins += 1
                self.consecutive_losses = 0
                self._update_peak_bankroll()
                log.info(f"  🏆 WIN! profit=+${profit:.2f} | new bankroll=${self.cfg.bankroll:.2f}")
                self._notify(
                    f"🏆 <b>WIN</b> | +${profit:.2f}\n"
                    f"Bankroll: ${self.cfg.bankroll:.2f}"
                )
            else:
                self.losses += 1
                self.consecutive_losses += 1
                log.info(f"  💀 LOSS | bankroll=${self.cfg.bankroll:.2f} | consecutive_losses={self.consecutive_losses}")
                self._notify(
                    f"💀 <b>LOSS</b>\n"
                    f"Bankroll: ${self.cfg.bankroll:.2f}"
                )
                if self.consecutive_losses >= COOLDOWN_LOSS_COUNT:
                    log.info(f"  Cooling down after {COOLDOWN_LOSS_COUNT} losses — will skip next window")
                    self.cooldown_next = True

            self._record_bucket(signal.confidence, won)
            self._update_bankroll_from_chain()
        except Exception as e:
            log.error(f"  Result check failed: {e}")

    # ─── Dry run ──────────────────────────────────────────────────────────────
    def _dry_run_trade(self, window_ts, close_time, signal, token_price, bet_usdc, shares, bucket):
        log.info(f"  [DRY RUN] Would BUY {signal.direction.upper()} | ${bet_usdc:.2f} | {shares:.1f} shares @ ${token_price:.3f}")
        self._notify(
            f"💰 <b>[DRY RUN] Trade</b>\n"
            f"Direction: {signal.direction.upper()} | Bet: ${bet_usdc:.2f}\n"
            f"Price: ${token_price:.3f} | Conf: {signal.confidence:.0%} ({bucket})"
        )

        wait = max(0, close_time - time.time() + 3)
        log.info(f"  [DRY RUN] Waiting {wait:.0f}s for window to close…")
        time.sleep(wait)

        # Check actual BTC outcome via Binance
        actual_direction = strategy.check_actual_outcome(
            window_ts=window_ts,
            window_minutes=self.cfg.window_minutes,
        )
        won = actual_direction == signal.direction

        if won:
            payout = bet_usdc / token_price
            profit = payout - bet_usdc
            self.cfg.bankroll += profit
            self.profits_above_original = max(0, self.cfg.bankroll - self.original_bankroll)
            self.wins += 1
            self.consecutive_losses = 0
            self._update_peak_bankroll()
            log.info(f"  [DRY RUN] 🏆 WIN! profit=+${profit:.2f} | bankroll=${self.cfg.bankroll:.2f}")
            self._notify(
                f"🏆 <b>[DRY RUN] WIN</b> | +${profit:.2f}\n"
                f"Bankroll: ${self.cfg.bankroll:.2f}"
            )
        else:
            self.cfg.bankroll -= bet_usdc
            self.losses += 1
            self.consecutive_losses += 1
            log.info(f"  [DRY RUN] 💀 LOSS | bankroll=${self.cfg.bankroll:.2f} | consecutive_losses={self.consecutive_losses}")
            self._notify(
                f"💀 <b>[DRY RUN] LOSS</b>\n"
                f"Bankroll: ${self.cfg.bankroll:.2f}"
            )
            if self.consecutive_losses >= COOLDOWN_LOSS_COUNT:
                log.info(f"  Cooling down after {COOLDOWN_LOSS_COUNT} losses — will skip next window")
                self.cooldown_next = True

        self._record_bucket(signal.confidence, won)
        self.trades += 1
        wr = self.wins / self.trades if self.trades else 0
        log.info(f"  [DRY RUN] Stats: {self.wins}W/{self.losses}L  win_rate={wr:.0%}")


# ─── Entry point ──────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Polymarket BTC Up/Down Bot")
    parser.add_argument("--window",     type=int,   default=5,      choices=[5, 15], help="5 or 15 minute window")
    parser.add_argument("--mode",       type=str,   default="safe",  choices=["safe", "aggressive", "degen"])
    parser.add_argument("--dry-run",    action="store_true",          help="Paper trade without placing real orders")
    parser.add_argument("--once",       action="store_true",          help="Run a single trade cycle then exit")
    parser.add_argument("--max-trades", type=int,   default=None,    help="Stop after N trades")
    parser.add_argument("--telegram",   action="store_true",          help="Enable Telegram notifications (requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env)")
    args = parser.parse_args()

    cfg = BotConfig(args)
    bot = PolymarketBot(cfg)
    bot.run()


if __name__ == "__main__":
    main()
