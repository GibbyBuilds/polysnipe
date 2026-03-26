"""
market_finder.py  —  Locate the active BTC Up/Down market on Polymarket

Strategy: construct the deterministic slug from the window timestamp,
then hit Polymarket's Gamma API (no auth required) to get token IDs and
current prices. No scanning or pagination needed.

Slug patterns:
  5-minute:  btc-up-or-down-5-minute-windows-{window_ts}
  15-minute: btc-up-or-down-15-minute-windows-{window_ts}

(The exact slugs may vary — we try both the official pattern and common
 variations, then fall back to a keyword search.)
"""

import time
import logging
import requests
from typing import Optional, Dict

log = logging.getLogger("market_finder")

GAMMA_HOST = "https://gamma-api.polymarket.com"


def _slug_candidates(window_ts: int, window_minutes: int):
    """Yield slug patterns to try, most-likely first."""
    w = window_minutes
    yield f"btc-up-or-down-{w}-minute-windows-{window_ts}"
    yield f"btc-up-or-down-{w}-minute-{window_ts}"
    yield f"btc-updown-{w}m-{window_ts}"
    yield f"btc-up-down-{w}m-{window_ts}"
    yield f"btc-{w}m-updown-{window_ts}"


def _parse_market(event: dict, window_minutes: int) -> Optional[Dict]:
    """
    Extract Up/Down token IDs and prices from a Gamma event response.
    Returns a dict with keys: slug, up_token, down_token, up_price, down_price.
    """
    markets = event.get("markets") or []
    if not markets:
        return None

    up_token   = None
    down_token = None
    up_price   = 0.50
    down_price = 0.50

    for m in markets:
        title   = (m.get("groupItemTitle") or m.get("question") or "").lower()
        outcomes = m.get("outcomes") or []
        tokens   = m.get("clobTokenIds") or m.get("outcomePrices") or []
        prices   = m.get("outcomePrices") or []

        # Polymarket usually surfaces 2 outcomes: ["Up","Down"] or ["Yes","No"]
        if len(outcomes) == 2:
            if "up" in outcomes[0].lower():
                up_token   = tokens[0] if tokens else None
                down_token = tokens[1] if len(tokens) > 1 else None
                up_price   = float(prices[0]) if prices else 0.50
                down_price = float(prices[1]) if len(prices) > 1 else 0.50
            elif "down" in outcomes[0].lower():
                down_token = tokens[0] if tokens else None
                up_token   = tokens[1] if len(tokens) > 1 else None
                down_price = float(prices[0]) if prices else 0.50
                up_price   = float(prices[1]) if len(prices) > 1 else 0.50
            break

    if not up_token or not down_token:
        # Fallback: try clobTokenIds on the event level
        clob = event.get("clobTokenIds") or []
        if len(clob) >= 2:
            up_token   = clob[0]
            down_token = clob[1]

    if not up_token or not down_token:
        return None

    return {
        "slug":       event.get("slug", ""),
        "up_token":   up_token,
        "down_token": down_token,
        "up_price":   up_price,
        "down_price": down_price,
    }


def _fetch_by_slug(slug: str, window_minutes: int) -> Optional[Dict]:
    try:
        resp = requests.get(
            f"{GAMMA_HOST}/events",
            params={"slug": slug},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        events = data if isinstance(data, list) else data.get("events", [])
        if events:
            return _parse_market(events[0], window_minutes)
    except Exception as e:
        log.debug(f"Slug fetch failed ({slug}): {e}")
    return None


def _fetch_by_keyword(window_ts: int, window_minutes: int) -> Optional[Dict]:
    """Fallback: search by keyword."""
    try:
        resp = requests.get(
            f"{GAMMA_HOST}/events",
            params={
                "tag":    "crypto",
                "q":      f"BTC {window_minutes} minute",
                "active": "true",
                "limit":  20,
            },
            timeout=8,
        )
        resp.raise_for_status()
        data   = resp.json()
        events = data if isinstance(data, list) else data.get("events", [])

        for event in events:
            slug = event.get("slug", "")
            # Match window timestamp in the slug
            if str(window_ts) in slug:
                result = _parse_market(event, window_minutes)
                if result:
                    log.info(f"  Found market via keyword search: {slug}")
                    return result
    except Exception as e:
        log.warning(f"Keyword search failed: {e}")
    return None


def get_market(window_ts: int, window_minutes: int) -> Optional[Dict]:
    """
    Main entry point.  Returns market info dict or None.
    Tries deterministic slug candidates first, then keyword fallback.
    """
    for slug in _slug_candidates(window_ts, window_minutes):
        result = _fetch_by_slug(slug, window_minutes)
        if result:
            log.info(f"  Found market: {result['slug']}")
            return result

    log.info("  Slug lookup failed — trying keyword search…")
    result = _fetch_by_keyword(window_ts, window_minutes)
    if result:
        return result

    log.warning(f"  No market found for window_ts={window_ts} ({window_minutes}min)")
    return None
