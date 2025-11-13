import os
import json
import logging
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# ----------------------------
# Logging setup
# ----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)


# ----------------------------
# Helpers
# ----------------------------
def safe_float(value: str) -> Optional[float]:
    if value is None:
        return None
    value = str(value).strip()
    if value == "":
        return None
    # Strip any % signs
    value = value.replace("%", "")
    try:
        return float(value)
    except ValueError:
        return None


def get_env_var(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Environment variable '{name}' is required but not set.")
    return val


# ----------------------------
# Google Sheets connection
# ----------------------------
def get_worksheet() -> gspread.Worksheet:
    """
    Connect to Google Sheets using service account JSON provided in env
    variable GOOGLE_CREDS_JSON. Opens spreadsheet 'Active-Investing'
    and worksheet 'Alpaca-Screener'.
    """
    google_creds_json = get_env_var("GOOGLE_CREDS_JSON")

    creds_info = json.loads(google_creds_json)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    credentials = Credentials.from_service_account_info(creds_info, scopes=scopes)
    client = gspread.authorize(credentials)

    spreadsheet_name = "Active-Investing"
    worksheet_name = "Alpaca-Screener"

    logging.info(f"Opening spreadsheet '{spreadsheet_name}', worksheet '{worksheet_name}'")
    sh = client.open(spreadsheet_name)
    ws = sh.worksheet(worksheet_name)
    return ws


# ----------------------------
# Alpaca connection
# ----------------------------
def get_alpaca_client() -> TradingClient:
    """
    Connects to Alpaca live account.
    Assumes you set:
      - ALPACA_API_KEY
      - ALPACA_API_SECRET
      - (optional) ALPACA_BASE_URL, e.g. https://api.alpaca.markets
    """
    api_key = get_env_var("ALPACA_API_KEY")
    api_secret = get_env_var("ALPACA_API_SECRET")
    base_url = os.getenv("ALPACA_BASE_URL")  # optional; alpaca-py can infer

    trading_client = TradingClient(
        api_key,
        api_secret,
        paper=False,  # LIVE trading
        url_override=base_url if base_url else None
    )

    account = trading_client.get_account()
    buying_power = float(account.buying_power)
    logging.info(f"Connected to Alpaca. Buying power: {buying_power:.2f}")
    return trading_client


# ----------------------------
# Core allocation logic
# ----------------------------
ICON_MULTIPLIERS = {
    "💎": 1.0,
    "💥": 0.9,
    "🚀": 0.8,
    "✨": 0.7,
    "📊": 0.6,
}


def get_bracket_pct(percent_down: float) -> Optional[float]:
    """
    C column: % down from all-time high.
    Brackets:
      0–25   -> 5% of available funds
      26–50  -> 10%
      51–75  -> 15%
      76–99.9 (or more) -> 20%
    """
    if percent_down < 0:
        return None

    if 0 <= percent_down <= 25:
        return 0.05
    elif 25 < percent_down <= 50:
        return 0.10
    elif 50 < percent_down <= 75:
        return 0.15
    else:
        # 76–99.9+ treated as top bracket
        return 0.20


def compute_order_notional(
    buying_power: float,
    percent_down: float,
    icon: str,
    long_ma: float,
    price: float,
    sentiment_raw: Optional[str]
) -> Optional[float]:
    """
    Compute the notional order size given all inputs.
    Returns None if the row should be skipped.
    """

    # Bracket based on percent down
    bracket_pct = get_bracket_pct(percent_down)
    if bracket_pct is None:
        logging.info(f"Skipping: invalid percent down {percent_down}")
        return None

    # Icon multiplier (must exist, else skip).
    icon = (icon or "").strip()
    if icon not in ICON_MULTIPLIERS:
        logging.info(f"Skipping: icon '{icon}' not recognized or missing in P column.")
        return None
    icon_mult = ICON_MULTIPLIERS[icon]

    if price <= 0:
        logging.info("Skipping: non-positive price.")
        return None

    # Long MA / Price multiplier
    ma_price_factor = long_ma / price

    # Sentiment multiplier
    sentiment_mult = 0.1  # default when no entry or non-positive
    if sentiment_raw is not None and str(sentiment_raw).strip() != "":
        s_val = safe_float(sentiment_raw)
        if s_val is not None and s_val > 0:
            sentiment_mult = s_val
        # else keep default 0.1 (assumption for <=0 or bad parse)

    base_alloc = buying_power * bracket_pct
    notional = base_alloc * icon_mult * ma_price_factor * sentiment_mult

    return notional


# ----------------------------
# Main bot logic
# ----------------------------
def run_bot() -> None:
    # Connect to external services
    ws = get_worksheet()
    trading_client = get_alpaca_client()
    account = trading_client.get_account()
    buying_power = float(account.buying_power)

    logging.info("Fetching sheet data...")
    rows = ws.get_all_values()

    if not rows or len(rows) < 2:
        logging.info("No data rows found (only header or empty sheet). Exiting.")
        return

    header = rows[0]
    logging.info(f"Header row: {header}")

    # Column indices (0-based)
    COL_TICKER = 0   # A
    COL_PRICE = 1    # B
    COL_PCT_DOWN = 2 # C
    COL_LONG_MA = 9  # J
    COL_ICON = 15    # P
    COL_SENTIMENT = 16  # Q

    MIN_NOTIONAL = 1.0  # Do not place orders below $1

    seen_symbols = set()
    orders_submitted = 0

    for row_idx, row in enumerate(rows[1:], start=2):  # start=2 to reflect real row number in sheet
        # Safely get each cell with default empty string
        def cell(idx: int) -> str:
            return row[idx] if idx < len(row) else ""

        symbol = cell(COL_TICKER).strip().upper()
        price_str = cell(COL_PRICE)
        pct_down_str = cell(COL_PCT_DOWN)
        long_ma_str = cell(COL_LONG_MA)
        icon = cell(COL_ICON)
        sentiment_str = cell(COL_SENTIMENT)

        if not symbol:
            logging.info(f"Row {row_idx}: No symbol in column A, skipping.")
            continue

        if symbol in seen_symbols:
            logging.info(f"Row {row_idx}: Symbol {symbol} already processed, skipping duplicate.")
            continue

        price = safe_float(price_str)
        pct_down = safe_float(pct_down_str)
        long_ma = safe_float(long_ma_str)

        # Required data check (A, B, C, J)
        if price is None or pct_down is None or long_ma is None:
            logging.info(
                f"Row {row_idx}, {symbol}: Missing required numeric data "
                "(price, % down, or long MA). Skipping."
            )
            continue

        # Compute order notional
        notional = compute_order_notional(
            buying_power=buying_power,
            percent_down=pct_down,
            icon=icon,
            long_ma=long_ma,
            price=price,
            sentiment_raw=sentiment_str
        )

        if notional is None:
            logging.info(f"Row {row_idx}, {symbol}: Notional is None, skipping.")
            continue

        if notional < MIN_NOTIONAL:
            logging.info(
                f"Row {row_idx}, {symbol}: Computed notional {notional:.2f} < {MIN_NOTIONAL}, skipping."
            )
            continue

        notional_rounded = round(notional, 2)
        logging.info(
            f"Row {row_idx}, {symbol}: placing BUY market order with notional ${notional_rounded:.2f}"
        )

        # Submit the order to Alpaca
        try:
            order_req = MarketOrderRequest(
                symbol=symbol,
                notional=notional_rounded,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            )
            order = trading_client.submit_order(order_req)
            logging.info(f"Order submitted for {symbol}: id={order.id}, notional={notional_rounded:.2f}")
            orders_submitted += 1
            seen_symbols.add(symbol)
        except Exception as e:
            logging.error(f"Failed to submit order for {symbol}: {e}")

    logging.info(f"Finished run. Orders submitted: {orders_submitted}")


if __name__ == "__main__":
    try:
        run_bot()
    except Exception as e:
        logging.exception(f"Unhandled exception in bot: {e}")
