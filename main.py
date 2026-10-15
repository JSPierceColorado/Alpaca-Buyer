"""Daily Google Sheets -> Alpaca buyer. Default: PAPER + DRY_RUN.

Railway cron: 0 18,19 * * * (UTC). The Mountain-time hour guard ensures
that exactly one of these two scheduled starts is eligible year-round.
"""

import argparse
import hashlib
import json
import logging
import os
import re
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from urllib.parse import quote
from zoneinfo import ZoneInfo

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, OrderSide, TimeInForce
from alpaca.trading.requests import GetAssetsRequest, MarketOrderRequest
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account

LOG = logging.getLogger("alpaca_auto_buyer")
TIMEZONE = ZoneInfo("America/Denver")
DEFAULT_SHEET_ID = "16yYo9fju59Kc_CfuqF45EvKACWfbS1NEb74OhwiaaI4"
SCOPE = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9./-]{0,24}$")
PERCENT = Decimal("0.02")
CENT = Decimal("0.01")


def setting_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    if raw.strip().lower() in {"1", "true", "yes", "on"}:
        return True
    if raw.strip().lower() in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def clean_symbols(rows: list[list[str]]) -> list[str]:
    """Read only first column of the returned E2:E matrix; preserve order."""
    symbols, seen = [], set()
    for index, row in enumerate(rows, start=2):
        symbol = str(row[0]).strip().upper() if row and row[0] is not None else ""
        if not symbol:
            continue
        if not SYMBOL_PATTERN.fullmatch(symbol):
            LOG.warning("Skipping invalid ticker at E%d: %r", index, symbol)
            continue
        if symbol not in seen:
            symbols.append(symbol)
            seen.add(symbol)
    return symbols


def google_credentials():
    value = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    file_name = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if value:
        return service_account.Credentials.from_service_account_info(json.loads(value), scopes=SCOPE)
    if file_name:
        return service_account.Credentials.from_service_account_file(file_name, scopes=SCOPE)
    raise RuntimeError("Set GOOGLE_SERVICE_ACCOUNT_JSON (Railway secret) and share the Sheet with that service account")


def load_symbols() -> list[str]:
    spreadsheet_id = os.environ.get("GOOGLE_SHEET_ID", DEFAULT_SHEET_ID).strip()
    tab = os.environ.get("GOOGLE_SHEET_TAB", "Recommendations")
    if not spreadsheet_id:
        raise RuntimeError("GOOGLE_SHEET_ID is empty")
    # Quote a tab's apostrophes according to Sheets A1 notation.
    sheet_range = f"'{tab.replace(chr(39), chr(39) * 2)}'!E2:E"
    url = (
        "https://sheets.googleapis.com/v4/spreadsheets/"
        + quote(spreadsheet_id, safe="")
        + "/values/"
        + quote(sheet_range, safe="")
    )
    with AuthorizedSession(google_credentials()) as session:
        response = session.get(url, timeout=30)
        response.raise_for_status()
        payload = response.json()
    symbols = clean_symbols(payload.get("values", []))
    LOG.info("Read %d unique tickers from %s!E2:E", len(symbols), tab)
    return symbols


def money(value) -> Decimal:
    """Never treat missing broker buying power as free money."""
    if value is None or value == "":
        raise ValueError("Alpaca did not provide required buying power")
    amount = Decimal(str(value))
    if not amount.is_finite():
        raise ValueError("Invalid buying power")
    return max(amount, Decimal("0"))


def two_percent(buying_power) -> Decimal:
    return (money(buying_power) * PERCENT).quantize(CENT, rounding=ROUND_DOWN)


def order_id(trade_day: str, symbol: str) -> str:
    """Stable across process restarts; Alpaca caps client_order_id at 48 chars."""
    digest = hashlib.sha256(symbol.upper().encode("utf-8")).hexdigest()[:14]
    return f"ab-{trade_day.replace('-', '')}-{digest}"


def is_not_found(error: Exception) -> bool:
    return getattr(error, "status_code", None) in {404, "404"}


def crypto_aliases(assets) -> dict[str, object]:
    """Map USD-quoted crypto into ticker, ticker/USD and tickerUSD aliases."""
    index = {}
    for asset in assets:
        name = str(asset.symbol).upper()
        if not name.endswith("/USD") or not bool(asset.tradable):
            continue
        base = name[:-4]
        for key in (base, name, base + "USD"):
            index.setdefault(key, asset)
    return index


def resolve_asset(client: TradingClient, raw: str, crypto_index: dict):
    """Use Alpaca's actual tradeable asset, not a ticker guessed from the Sheet."""
    if raw in crypto_index:
        return crypto_index[raw]
    try:
        return client.get_asset(raw)
    except APIError as exc:
        if not is_not_found(exc):
            raise  # Never hide an auth, API, or rate-limit failure.
    return None


def position_key(symbol: str) -> str:
    """Normalize Alpaca crypto pairs (BTC/USD vs BTCUSD) for holding checks."""
    return str(symbol).strip().upper().replace("/", "")


class PositionLookupError(RuntimeError):
    """Abort the whole buying run when current holdings cannot be checked."""


def held_positions(client: TradingClient) -> set[str]:
    """Return *all* open positions; never assume an API failure means no holdings."""
    try:
        positions = client.get_all_positions()
        if positions is None:
            raise ValueError("Alpaca returned no positions response")
        return {position_key(position.symbol) for position in positions}
    except Exception as exc:
        raise PositionLookupError("Alpaca positions unavailable; refusing further buy orders") from exc


def account_is_tradeable(account) -> bool:
    blocked = any(bool(getattr(account, name, False)) for name in ("trading_blocked", "account_blocked"))
    status = str(getattr(account, "status", "ACTIVE")).upper()
    return not blocked and status.endswith("ACTIVE")


def existing_order(client: TradingClient, client_order_id: str) -> bool:
    try:
        return client.get_order_by_client_id(client_order_id) is not None
    except APIError as exc:
        if is_not_found(exc):
            return False
        raise  # Fail closed on a lookup problem; no blind duplicate purchase.


def trade_once(client: TradingClient, symbols: list[str], dry_run: bool, trading_day: str) -> tuple[int, int, int]:
    if not symbols:
        LOG.info("Column E contains no tickers: nothing to buy")
        return 0, 0, 0

    account = client.get_account()
    if not account_is_tradeable(account):
        raise RuntimeError("Alpaca account is not active or is blocked from trading")

    # The clock controls equities only. Alpaca crypto trades every day.
    equities_open = bool(client.get_clock().is_open)
    crypto_assets = client.get_all_assets(GetAssetsRequest(asset_class=AssetClass.CRYPTO))
    crypto_index = crypto_aliases(crypto_assets)
    seen_canonical = set()
    bought = skipped = errors = 0

    for raw in symbols:
        try:
            asset = resolve_asset(client, raw, crypto_index)
            if asset is None:
                LOG.warning("SKIP %s: unavailable on Alpaca", raw)
                skipped += 1
                continue
            ticker = str(asset.symbol).upper()
            if ticker in seen_canonical:
                LOG.info("SKIP %s: duplicate Alpaca asset", ticker)
                skipped += 1
                continue
            seen_canonical.add(ticker)

            if not bool(asset.tradable):
                LOG.warning("SKIP %s: not tradable", ticker)
                skipped += 1
                continue
            crypto = getattr(asset, "asset_class", None) == AssetClass.CRYPTO
            if not crypto and getattr(asset, "asset_class", None) != AssetClass.US_EQUITY:
                LOG.warning("SKIP %s: unsupported asset class", ticker)
                skipped += 1
                continue
            if not crypto and not equities_open:
                LOG.info("SKIP %s: US equity market is closed", ticker)
                skipped += 1
                continue
            if not crypto and not bool(getattr(asset, "fractionable", False)):
                LOG.warning("SKIP %s: fractionable=false; notional orders unsupported", ticker)
                skipped += 1
                continue

            # Consult actual held positions for EACH candidate just before the
            # order. Even a short position counts as existing exposure. The API
            # must succeed: if holdings are unknown, stop the entire run.
            if position_key(ticker) in held_positions(client):
                LOG.info("SKIP %s: already held in Alpaca account", ticker)
                skipped += 1
                continue

            account = client.get_account()  # Refresh after each preceding order.
            power = account.non_marginable_buying_power if crypto else account.buying_power
            dollars = two_percent(power)
            if dollars < Decimal("1.00"):
                LOG.info("SKIP %s: 2%% of eligible buying power is under $1", ticker)
                skipped += 1
                continue
            # Crypto market orders max out at $200k; conservative broker-side cap.
            if crypto and dollars > Decimal("200000.00"):
                LOG.warning("SKIP %s: crypto notional exceeds broker's $200k limit", ticker)
                skipped += 1
                continue

            identity = order_id(trading_day, ticker)
            if dry_run:
                LOG.info("DRY_RUN BUY %s $%s (%s buying power) id=%s", ticker, dollars,
                         "cash/non-margin" if crypto else "margin-inclusive", identity)
                continue
            if existing_order(client, identity):
                LOG.info("SKIP %s: today's order already exists (%s)", ticker, identity)
                skipped += 1
                continue

            request = MarketOrderRequest(
                symbol=ticker,
                notional=float(dollars),
                side=OrderSide.BUY,
                time_in_force=TimeInForce.GTC if crypto else TimeInForce.DAY,
                client_order_id=identity,
            )
            # Do NOT auto-retry an ambiguous submission error: it may have reached the broker.
            created = client.submit_order(order_data=request)
            LOG.info("SUBMITTED BUY %s $%s order=%s", ticker, dollars, created.id)
            bought += 1
        except PositionLookupError:
            LOG.exception("ABORT: cannot verify Alpaca holdings; no more orders will be submitted")
            raise
        except Exception:
            errors += 1
            LOG.exception("FAILED %s; continuing with remaining tickers", raw)

    LOG.info("Daily summary: submitted=%d skipped=%d errors=%d dry_run=%s", bought, skipped, errors, dry_run)
    return bought, skipped, errors


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-now", action="store_true", help="Bypass noon guard for dry-run testing ONLY")
    args = parser.parse_args()

    paper = setting_bool("ALPACA_PAPER", True)
    dry_run = setting_bool("DRY_RUN", True)
    allow_live = setting_bool("ENABLE_LIVE_TRADING", False)
    if not paper and not allow_live:
        raise RuntimeError("Live Alpaca disabled: set ENABLE_LIVE_TRADING=true intentionally before using ALPACA_PAPER=false")
    if args.test_now and not dry_run:
        raise RuntimeError("--test-now is allowed only when DRY_RUN=true")

    now = datetime.now(TIMEZONE)
    if not args.test_now and now.hour != 12:
        LOG.info("Outside 12:00-12:59 America/Denver (%s): no trade", now.isoformat())
        return 0

    key = os.environ.get("ALPACA_API_KEY", "").strip()
    secret = os.environ.get("ALPACA_SECRET_KEY", "").strip()
    if not key or not secret:
        raise RuntimeError("Set ALPACA_API_KEY and ALPACA_SECRET_KEY in Railway")

    symbols = load_symbols()
    client = TradingClient(key, secret, paper=paper)
    LOG.info("Account mode=%s dry_run=%s day=%s", "PAPER" if paper else "LIVE", dry_run, now.date())
    _, _, errors = trade_once(client, symbols, dry_run, now.date().isoformat())
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
