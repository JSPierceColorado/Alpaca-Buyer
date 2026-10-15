# Alpaca Auto-Buyer

Reads **AI Investment Opportunity Tracker — Personal** → **Recommendations!E2:E**, removes duplicate tickers, skips assets already held in the Alpaca account, and buys **2% of current eligible buying power per remaining Alpaca-tradable asset** at **12 noon America/Denver** daily.

- Stocks/ETFs: 2% of Alpaca `buying_power` (including any permitted margin); requires market open, tradable + fractionable security; market DAY order.
- Crypto: 2% of Alpaca `non_marginable_buying_power` (no margin); market GTC order; USD pairs only (e.g. sheet `BTC` resolves to `BTC/USD`).
- Order size recalculated *sequentially* just before each symbol, so later orders can be smaller as buying power decreases. This is **not** 2% total split across all symbols.
- **Before each buy, check `TradingClient.get_all_positions()` and skip any asset with an existing open position, long or short.** For crypto, `BTC/USD` and `BTCUSD` are treated as the same holding. If Alpaca's positions endpoint fails, **abort all remaining buys** rather than assume the account owns nothing. An asset becomes eligible again after its entire position is closed.
- Duplicate rows and symbol aliases are deduplicated. A deterministic daily `client_order_id` prevents repeat purchases on restart or retrigger after successful placement. If Alpaca order lookup fails, **no order is sent**. Ambiguous submission failures are not auto-retried.
- Never automatically sells. Does not read column H (broker) because the requirement is **any unique asset in column E**, provided Alpaca offers it.
- Only US equities and USD-quoted cryptocurrencies are supported. Unsupported symbols, nonfractionable equities, and market-closed equities are skipped.
- **Defaults: `DRY_RUN=true`, `ALPACA_PAPER=true`, `ENABLE_LIVE_TRADING=false`**. No live purchases happen until explicitly changed.

## Connection steps

1. In Google Cloud, create a project, enable **Google Sheets API**, create a **service account**, generate a JSON key, and store its **entire JSON document** as the `GOOGLE_SERVICE_ACCOUNT_JSON` Railway variable. Do **not** commit the JSON key to GitHub.
2. In the Google Sheet, click **Share** and grant the service account's `client_email` **Viewer** access (read-only is sufficient). The connected Google Drive app in ChatGPT does not automatically authorize a separate Railway container.
3. In the new Railway service, set `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` from **your Alpaca paper account** to start, plus `GOOGLE_SERVICE_ACCOUNT_JSON`. Defaults and the specific sheet ID are in `.env.example`.
4. Connect a GitHub repository holding these files. Railway should build the Dockerfile and pick up `railway.toml`; confirm its Cron Schedule reads `0 18,19 * * *` and restart policy is NEVER. Disable any public domain; none is needed.
5. Run `python main.py --test-now` from a configured shell for a harmless test that **never places orders**. In Railway, scheduled executions start at 18:00 and 19:00 UTC; the code executes purchase logic only if local Mountain hour is 12.
6. After confirming logs, set `DRY_RUN=false` while **ALPACA_PAPER=true** to test actual paper orders. For real-money trades, use **live Alpaca keys**, `ALPACA_PAPER=false`, `ENABLE_LIVE_TRADING=true`, and `DRY_RUN=false`, only after reviewing risk and the output.

## Railway environment variables

| Name | Default | Notes |
| --- | --- | --- |
| `GOOGLE_SHEET_ID` | `16yYo9fju59Kc_CfuqF45EvKACWfbS1NEb74OhwiaaI4` | Spreadsheet ID |
| `GOOGLE_SHEET_TAB` | `Recommendations` | Tab to read E2:E |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Required | Full JSON, kept secret |
| `ALPACA_API_KEY` | Required | Paper or live trading key |
| `ALPACA_SECRET_KEY` | Required | Corresponding secret |
| `ALPACA_PAPER` | `true` | Use Alpaca paper endpoint |
| `DRY_RUN` | `true` | Preview purchases; no order submission |
| `ENABLE_LIVE_TRADING` | `false` | Second explicit live-trading gate |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, etc. |

## Margin / risk disclosures

This bot **does not intentionally add to an existing open position**: held assets are skipped each run. It can buy an asset again after the position is fully closed. This rule checks holdings, not every possible unfilled buy order or concurrent external trade; another system acting at the same moment could still change the account between a check and submission. With margin enabled, `buying_power` is *not cash*: borrowing can create interest costs and margin calls, and forced liquidation may occur. The 2% purchase limit is **per ticker** rather than per day; many rows can collectively create substantial exposure. Fractional market orders have no guaranteed fill price. Alpaca crypto fees and eligibility rules apply. Order logs mean submission, **not guaranteed execution**. Do not enable real-money mode without first validating a paper run and understanding these exposures.

## Local dry-run

```sh
pip install -r requirements.txt
export ALPACA_API_KEY='YOUR_PAPER_KEY'
export ALPACA_SECRET_KEY='YOUR_PAPER_SECRET'
export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat path/to/service-account.json)"
python main.py --test-now
```

## Notes

- Railway Cron is **UTC** and does not support automatic daylight-saving time changes. The double UTC schedule plus local timezone guard covers both MDT and MST. Railway may start a cron a few minutes after the nominal time; the local guard accepts the entire 12 PM hour.
- For equities, Alpaca's market clock is consulted (weekends and exchange holidays skip stocks). Crypto can run seven days a week.
- The bot does not guarantee fills, available prices, or an execution precisely at noon. It submits market orders when the conditions above are met.
- Only this worker should use its daily order-ID namespace to retain exact one-buy-per-symbol/day idempotency.
