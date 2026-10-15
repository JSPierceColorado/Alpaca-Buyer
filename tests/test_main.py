"""Run with `python -m unittest discover -s tests -v` after pip installing requirements."""
from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase, mock

import main
from alpaca.trading.enums import AssetClass, TimeInForce


def asset(symbol, kind=AssetClass.US_EQUITY, fractionable=True):
    return SimpleNamespace(symbol=symbol, asset_class=kind, tradable=True, fractionable=fractionable)


class CoreTests(TestCase):
    def test_column_e_order_dedupes_and_ignores_empty(self):
        self.assertEqual(main.clean_symbols([["SPGI"], [" BTC "], ["BTC"], [], ["brk.b"], ["garbage symbol"]]),
                         ["SPGI", "BTC", "BRK.B"])

    def test_two_percent_of_current_buying_power(self):
        self.assertEqual(main.two_percent("10000.99"), Decimal("200.01"))
        self.assertEqual(main.two_percent("0"), Decimal("0.00"))
        with self.assertRaises(ValueError):
            main.two_percent(None)

    def test_order_id_stable_and_short(self):
        a = main.order_id("2026-10-14", "BTC/USD")
        self.assertEqual(a, main.order_id("2026-10-14", "btc/usd"))
        self.assertNotEqual(a, main.order_id("2026-10-15", "BTC/USD"))
        self.assertLessEqual(len(a), 48)

    def test_crypto_aliases(self):
        indexed = main.crypto_aliases([asset("BTC/USD", AssetClass.CRYPTO)])
        for symbol in ("BTC", "BTC/USD", "BTCUSD"):
            self.assertEqual(indexed[symbol].symbol, "BTC/USD")

    def test_dry_run_never_submits_even_with_margin(self):
        client = mock.MagicMock()
        client.get_account.return_value = SimpleNamespace(
            trading_blocked=False, account_blocked=False, status="ACTIVE",
            buying_power="10000", non_marginable_buying_power="1000")
        client.get_clock.return_value.is_open = True
        client.get_all_positions.return_value = []
        client.get_all_assets.return_value = [asset("BTC/USD", AssetClass.CRYPTO)]
        client.get_asset.return_value = asset("SPGI")
        bought, skipped, errors = main.trade_once(client, ["SPGI", "BTC"], True, "2026-10-14")
        self.assertEqual((bought, skipped, errors), (0, 0, 0))
        client.submit_order.assert_not_called()
        self.assertEqual(client.get_account.call_count, 3)
        self.assertEqual(client.get_all_positions.call_count, 2)

    def test_live_paper_orders_use_separate_buying_powers(self):
        client = mock.MagicMock()
        client.get_account.return_value = SimpleNamespace(
            trading_blocked=False, account_blocked=False, status="ACTIVE",
            buying_power="10000", non_marginable_buying_power="1000")
        client.get_clock.return_value.is_open = True
        client.get_all_positions.return_value = []
        client.get_all_assets.return_value = [asset("BTC/USD", AssetClass.CRYPTO)]
        client.get_asset.return_value = asset("SPGI")
        client.submit_order.return_value.id = "test-id"
        with mock.patch.object(main, "existing_order", return_value=False):
            bought, skipped, errors = main.trade_once(client, ["SPGI", "BTC", "BTC"], False, "2026-10-14")
        self.assertEqual((bought, skipped, errors), (2, 1, 0))
        first, second = [call.kwargs["order_data"] for call in client.submit_order.call_args_list]
        self.assertEqual(first.symbol, "SPGI")
        self.assertEqual(first.notional, 200.0)
        self.assertEqual(first.time_in_force, TimeInForce.DAY)
        self.assertEqual(second.symbol, "BTC/USD")
        self.assertEqual(second.notional, 20.0)
        self.assertEqual(second.time_in_force, TimeInForce.GTC)

    def test_closed_equity_market_skips_stocks_but_not_crypto(self):
        client = mock.MagicMock()
        client.get_account.return_value = SimpleNamespace(
            trading_blocked=False, account_blocked=False, status="ACTIVE",
            buying_power="10000", non_marginable_buying_power="1000")
        client.get_clock.return_value.is_open = False
        client.get_all_positions.return_value = []
        client.get_all_assets.return_value = [asset("BTC/USD", AssetClass.CRYPTO)]
        client.get_asset.return_value = asset("SPGI")
        with mock.patch.object(main, "existing_order", return_value=False):
            bought, skipped, errors = main.trade_once(client, ["SPGI", "BTC"], False, "2026-10-14")
        self.assertEqual((bought, skipped, errors), (1, 1, 0))
        self.assertEqual(client.submit_order.call_args.kwargs["order_data"].symbol, "BTC/USD")

    def test_existing_stock_and_crypto_positions_are_skipped(self):
        client = mock.MagicMock()
        client.get_account.return_value = SimpleNamespace(
            trading_blocked=False, account_blocked=False, status="ACTIVE",
            buying_power="10000", non_marginable_buying_power="1000")
        client.get_clock.return_value.is_open = True
        client.get_all_assets.return_value = [asset("BTC/USD", AssetClass.CRYPTO)]
        client.get_asset.side_effect = lambda ticker: asset(ticker)
        client.get_all_positions.return_value = [
            SimpleNamespace(symbol="SPGI", qty="12"),
            SimpleNamespace(symbol="BTCUSD", qty="0.05"),
        ]
        client.submit_order.return_value.id = "test-order"
        with mock.patch.object(main, "existing_order", return_value=False):
            bought, skipped, errors = main.trade_once(client, ["SPGI", "BTC", "ANET"], False, "2026-10-14")
        self.assertEqual((bought, skipped, errors), (1, 2, 0))
        self.assertEqual(client.submit_order.call_args.kwargs["order_data"].symbol, "ANET")
        self.assertEqual(client.get_all_positions.call_count, 3)

    def test_position_check_repeated_before_each_candidate(self):
        client = mock.MagicMock()
        client.get_account.return_value = SimpleNamespace(
            trading_blocked=False, account_blocked=False, status="ACTIVE",
            buying_power="10000", non_marginable_buying_power="1000")
        client.get_clock.return_value.is_open = True
        client.get_all_assets.return_value = []
        client.get_asset.side_effect = lambda ticker: asset(ticker)
        # A position appears externally after the first symbol is considered.
        client.get_all_positions.side_effect = [[], [SimpleNamespace(symbol="ASML", qty="1")]]
        client.submit_order.return_value.id = "test-order"
        with mock.patch.object(main, "existing_order", return_value=False):
            bought, skipped, errors = main.trade_once(client, ["SPGI", "ASML"], False, "2026-10-14")
        self.assertEqual((bought, skipped, errors), (1, 1, 0))
        self.assertEqual(client.submit_order.call_count, 1)
        self.assertEqual(client.submit_order.call_args.kwargs["order_data"].symbol, "SPGI")

    def test_short_position_is_already_held(self):
        client = mock.MagicMock()
        client.get_account.return_value = SimpleNamespace(
            trading_blocked=False, account_blocked=False, status="ACTIVE",
            buying_power="10000", non_marginable_buying_power="1000")
        client.get_clock.return_value.is_open = True
        client.get_all_assets.return_value = []
        client.get_asset.return_value = asset("ANET")
        client.get_all_positions.return_value = [SimpleNamespace(symbol="anet", qty="-5")]
        self.assertEqual(main.trade_once(client, ["ANET"], False, "2026-10-14"), (0, 1, 0))
        client.submit_order.assert_not_called()

    def test_position_lookup_failure_stops_run_before_any_order(self):
        client = mock.MagicMock()
        client.get_account.return_value = SimpleNamespace(
            trading_blocked=False, account_blocked=False, status="ACTIVE",
            buying_power="10000", non_marginable_buying_power="1000")
        client.get_clock.return_value.is_open = True
        client.get_all_assets.return_value = []
        client.get_asset.return_value = asset("SPGI")
        client.get_all_positions.side_effect = RuntimeError("positions API unavailable")
        with self.assertRaises(main.PositionLookupError):
            main.trade_once(client, ["SPGI", "ASML"], False, "2026-10-14")
        client.submit_order.assert_not_called()
        self.assertEqual(client.get_all_positions.call_count, 1)

    def test_held_alias_normalization(self):
        self.assertEqual(main.position_key(" btc/usd "), main.position_key("BTCUSD"))
        self.assertEqual(main.position_key(" spgi "), "SPGI")


if __name__ == "__main__":
    import unittest
    unittest.main()
