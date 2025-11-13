# Alpaca Equity Buying Bot (Google Sheets + alpaca-py)

This script performs **automated stock purchasing via Alpaca**, using inputs from a Google Sheet. It reads per‑ticker signals (price, % down from ATH, moving average, icon flag, sentiment), computes a dollar notional for each, and places **live market buy orders** using the official `alpaca-py` trading client.

It is designed for **one-shot runs** (cron, scheduler, GitHub Action, etc.).

> ⚠️ **Warning:** This script places **real trades** (`paper=False`). Use at your own risk and test thoroughly with paper trading or tiny amounts first.

---

## 🧾 High-Level Flow

1. Connects to Google Sheets using a service account.
2. Opens spreadsheet **`Active-Investing`**, worksheet **`Alpaca-Screener`**.
3. Reads all rows (header + data rows).
4. For each unique symbol row:

   * Parses price, % down from ATH, long MA, icon, sentiment.
   * Computes an order notional using:

     * Bracket % (based on % down from ATH)
     * Icon multiplier
     * Moving-average factor (long MA ÷ price)
     * Sentiment multiplier
   * Skips rows that don’t meet minimum data/size criteria.
   * Submits a **market buy** order to Alpaca.
5. Logs all steps and reports how many orders were submitted.

---

## 📊 Google Sheet Layout

Spreadsheet: **`Active-Investing`**
Worksheet: **`Alpaca-Screener`**

Expected columns:

| Column | Index | Meaning                                       |
| ------ | ----- | --------------------------------------------- |
| A      | 0     | **Ticker** (e.g. `AAPL`, `MSFT`)              |
| B      | 1     | **Price** (current price)                     |
| C      | 2     | **% Down From ATH** (0–100, non-negative)     |
| J      | 9     | **Long Moving Average**                       |
| P      | 15    | **Icon** (one of `💎`, `💥`, `🚀`, `✨`, `📊`) |
| Q      | 16    | **Sentiment** (optional, positive numeric)    |

Other columns may exist; they are ignored by the bot.

Each non-empty symbol in column A is treated as a candidate asset. Duplicate symbols are only processed **once** per run.

---

## 🔐 Environment Variables

The script is configured via environment variables:

| Variable            | Required | Description                                                                                      |
| ------------------- | -------- | ------------------------------------------------------------------------------------------------ |
| `GOOGLE_CREDS_JSON` | Yes      | Full Google service account JSON as a **single-line string**.                                    |
| `ALPACA_API_KEY`    | Yes      | Alpaca trading API key.                                                                          |
| `ALPACA_API_SECRET` | Yes      | Alpaca trading API secret.                                                                       |
| `ALPACA_BASE_URL`   | No       | Optional base URL (e.g. `https://api.alpaca.markets`). If omitted, `alpaca-py` uses its default. |

The script always connects with `paper=False`, meaning **live** account mode.

---

## 🧮 Allocation & Order Sizing

### 1. Percent-Down Brackets (Column C)

`get_bracket_pct(percent_down)` uses the **non-negative** `% down from ATH` to determine the base allocation fraction:

| % Down Range | Bracket % of Buying Power |
| ------------ | ------------------------- |
| 0–25         | 5%  (0.05)                |
| 25–50        | 10% (0.10)                |
| 50–75        | 15% (0.15)                |
| >75          | 20% (0.20)                |

If `percent_down < 0`, the row is skipped as invalid.

### 2. Icon Multipliers (Column P)

Icons weight conviction/priority:

```text
💎 → 1.0
💥 → 0.9
🚀 → 0.8
✨ → 0.7
📊 → 0.6
```

Any icon not in this set causes the row to be skipped.

### 3. Moving Average Factor (Column J vs B)

```text
MA / Price = long_ma / price
```

This ratio boosts or reduces allocation depending on how the long MA compares to current price.

### 4. Sentiment Multiplier (Column Q)

* Default sentiment multiplier = **0.1**.
* If column Q contains a parseable **positive** float, that value is used.
* If Q is blank, non-numeric, or ≤ 0, the multiplier remains 0.1.

### 5. Notional Calculation

Given:

* `buying_power` from Alpaca account
* `bracket_pct` from % down
* `icon_mult` from P
* `ma_price_factor` from J / B
* `sentiment_mult` from Q

The notional is:

```text
base_alloc = buying_power × bracket_pct
notional  = base_alloc × icon_mult × ma_price_factor × sentiment_mult
```

Rows are skipped if:

* Any required numeric field (price, % down, long MA) is missing/invalid
* `notional` is `None`
* `notional < MIN_NOTIONAL` (hard-coded to **$1.00**)

Final `notional` is **rounded to 2 decimals** and used in a dollar-based market order.

---

## 💱 Alpaca Integration

The script uses `alpaca-py`:

```python
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
```

Connection:

```python
trading_client = TradingClient(
    api_key,
    api_secret,
    paper=False,  # LIVE trading
    url_override=base_url or None,
)
```

For each valid row, an order is submitted:

```python
order_req = MarketOrderRequest(
    symbol=symbol,
    notional=notional_rounded,
    side=OrderSide.BUY,
    time_in_force=TimeInForce.DAY,
)
order = trading_client.submit_order(order_req)
```

On success, the script logs the order ID and notional.

---

## 📦 Installation

Install required dependencies:

```bash
pip install gspread google-auth alpaca-py
```

You may also need:

```bash
pip install google-auth-oauthlib
```

> Ensure that your Google service account has **Sheets** and **Drive** API access enabled and is shared as **Editor** on the target spreadsheet.

---

## ▶️ Running the Bot

Set the necessary environment variables, then run:

```bash
python alpaca_buy_bot.py
```

You’ll see logs like:

```text
2025-01-01 12:00:00 [INFO] Connected to Alpaca. Buying power: 12345.67
2025-01-01 12:00:01 [INFO] Header row: ['Ticker', 'Price', 'PctDownATH', ...]
2025-01-01 12:00:02 [INFO] Row 2, AAPL: placing BUY market order with notional $27.43
2025-01-01 12:00:02 [INFO] Order submitted for AAPL: id=abc123, notional=27.43
2025-01-01 12:00:03 [INFO] Finished run. Orders submitted: 3
```

If the sheet is empty or only contains a header row, it simply exits.

---

## ⚠️ Safety & Best Practices

* Start with **paper trading** (adapt the script to use Alpaca’s paper endpoint and `paper=True`).
* Use **very small notional values** when first moving to live mode.
* Double‑check sheet formulas, icons, and sentiment entries before each run.
* Consider adding:

  * Position size caps per symbol
  * Daily/weekly max spend
  * Logging to a separate Sheet or database

---

## 📄 License

Add your preferred license here (MIT, Apache 2.0, etc.).
