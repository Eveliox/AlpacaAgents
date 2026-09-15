from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path
import sqlite3
import tempfile
import unittest

from alpaca_agents.executor.fills import FillLedger, OptionFill
from alpaca_agents.executor.ledger import LedgerError

DAY = date(2026, 9, 15)
OPEN = datetime(2026, 9, 14, 15, tzinfo=timezone.utc)
CLOSE = OPEN + timedelta(days=1)


class FillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "fills.sqlite3"
        self.ledger = FillLedger(self.path)
        # Historical accounting can include multi-contract positions. This is
        # not permission to bypass the entry engine's one-contract cap.
        self.buy = OptionFill("buy-1", "position-1", "IWM261016C00205000", "buy_to_open", 2,
                              Decimal("180"), Decimal("2"), OPEN, OPEN.date())
        self.sell = replace(self.buy, execution_id="sell-1", side="sell_to_close", quantity=1,
                            premium=Decimal("52"), fees=Decimal("1"), occurred_at=CLOSE, trading_day=DAY)

    def record(self, fill):
        return self.ledger.record(fill, recorded_at=CLOSE + timedelta(days=2))

    def test_partial_close_trips_before_position_fully_closed_and_survives_restart(self):
        self.record(self.buy)
        self.record(self.sell)
        restarted = FillLedger(self.path)
        summary = restarted.daily_summary(DAY)
        self.assertEqual(summary["realized_pnl"], Decimal("-40"))
        self.assertTrue(summary["breaker_tripped"])
        self.assertFalse(summary["reconciled"])
        self.assertEqual(restarted.inventory(), [{"position_id": "position-1", "quantity": 1,
                                                "remaining_basis": Decimal("91"),
                                                "contract": "IWM261016C00205000"}])
        self.assertEqual(restarted.open_lifecycle("IWM261016C00205000"), "position-1")
        self.assertIsNone(restarted.open_lifecycle("IWM261016P00205000"))

    def test_fifo_and_entry_exit_fees(self):
        self.record(replace(self.buy, quantity=1, premium=Decimal("80"), fees=Decimal("1")))
        self.record(replace(self.buy, execution_id="buy-2", quantity=1, premium=Decimal("100"), fees=Decimal("2"),
                            occurred_at=OPEN + timedelta(seconds=1)))
        self.record(self.sell)  # 52 - 1 - oldest basis 81 = -30
        self.assertEqual(self.ledger.daily_summary(DAY)["realized_pnl"], Decimal("-30"))
        self.assertEqual(self.ledger.inventory()[0]["remaining_basis"], Decimal("102"))
        self.record(replace(self.sell, execution_id="sell-2", occurred_at=CLOSE + timedelta(seconds=1),
                            premium=Decimal("112")))  # +9, cannot offset loss counter
        summary = self.ledger.daily_summary(DAY)
        self.assertEqual(summary["realized_pnl"], Decimal("-21"))
        self.assertEqual(summary["realized_loss"], Decimal("30"))
        self.assertEqual(self.ledger.inventory(), [])

    def test_realization_days_and_latch_not_reset_by_wins(self):
        self.record(self.buy)
        self.record(self.sell)
        self.record(replace(self.sell, execution_id="sell-2", occurred_at=CLOSE + timedelta(days=1),
                            trading_day=DAY + timedelta(days=1), premium=Decimal("120")))
        self.assertTrue(self.ledger.daily_summary(DAY)["breaker_tripped"])
        tomorrow = self.ledger.daily_summary(DAY + timedelta(days=1))
        self.assertEqual(tomorrow["realized_pnl"], Decimal("28"))
        self.assertEqual(tomorrow["realized_loss"], 0)
        self.assertFalse(tomorrow["breaker_tripped"])

    def test_replay_conflicts_and_closed_lifecycle(self):
        self.record(self.buy)
        self.record(replace(self.sell, quantity=2))
        self.assertFalse(self.record(replace(self.buy, fees=Decimal("2.000"))))
        with self.assertRaises(LedgerError):
            self.record(replace(self.sell, premium=Decimal("1")))
        with self.assertRaises(LedgerError):
            self.record(replace(self.buy, execution_id="reopen", occurred_at=CLOSE + timedelta(seconds=1)))
        self.assertEqual(self.ledger.inventory(), [])

    def test_unmatched_oversell_mismatched_and_out_of_order(self):
        with self.assertRaises(LedgerError):
            self.record(self.sell)
        self.record(self.buy)
        for fill in (
            replace(self.sell, quantity=3),
            replace(self.sell, contract="IWM261016P00205000"),
            replace(self.sell, occurred_at=OPEN - timedelta(seconds=1)),
        ):
            with self.subTest(fill=fill), self.assertRaises(LedgerError):
                self.record(fill)
        self.assertEqual(self.ledger.inventory()[0]["quantity"], 2)
        self.assertEqual(self.ledger.daily_summary(DAY)["realized_loss"], 0)

    def test_invalid_inputs_are_rejected(self):
        for change in (
            {"quantity": True}, {"quantity": 0}, {"quantity": 1.5},
            {"premium": Decimal("NaN")}, {"fees": Decimal("-1")}, {"fees": 0.0},
            {"strategy": "debit_spread"}, {"side": "sell_to_open"}, {"multiplier": 10},
            {"contract": "IWM261332C00205000"}, {"contract": "IWM261016C00000000"},
            {"occurred_at": OPEN.replace(tzinfo=None)}, {"trading_day": OPEN},
        ):
            with self.subTest(change=change), self.assertRaises(LedgerError):
                self.record(replace(self.buy, **change))
        with self.assertRaises(LedgerError):
            self.ledger.record(self.buy, recorded_at=OPEN - timedelta(seconds=1))
        self.assertEqual(self.ledger.inventory(), [])

    def test_fractional_fee_allocation_conserves_total_basis(self):
        with localcontext() as context:
            context.prec = 2  # Caller precision must not affect accounting.
            self.record(replace(self.buy, quantity=3, premium=Decimal("3"), fees=Decimal("0.01")))
            for i in range(3):
                self.record(replace(self.sell, execution_id=f"sell-{i}", premium=Decimal("1"),
                                    fees=Decimal(0), occurred_at=CLOSE + timedelta(seconds=i)))
            self.assertEqual(self.ledger.daily_summary(DAY)["realized_pnl"], Decimal("-0.01"))
            self.assertEqual(self.ledger.inventory(), [])

    def test_audit_failure_rolls_back_inventory_fill_and_latch(self):
        self.record(self.buy)
        db = sqlite3.connect(self.path)
        try:
            db.execute("""CREATE TRIGGER fail_audit BEFORE INSERT ON fill_events
                          WHEN NEW.event='circuit_breaker_triggered'
                          BEGIN SELECT RAISE(ABORT, 'simulated failure'); END""")
            db.commit()
        finally:
            db.close()
        with self.assertRaises(sqlite3.IntegrityError):
            self.record(self.sell)
        self.assertEqual(self.ledger.inventory()[0]["quantity"], 2)
        self.assertEqual(self.ledger.daily_summary(DAY)["realized_loss"], 0)
        self.assertFalse(self.ledger.daily_summary(DAY)["breaker_tripped"])

    def test_concurrent_duplicate_does_not_double_count(self):
        self.record(self.buy)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.record, [self.sell, self.sell]))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(self.ledger.daily_summary(DAY)["realized_loss"], Decimal("40"))

    def test_breaker_event_once_and_same_day_win_does_not_clear_it(self):
        self.record(self.buy)
        self.record(self.sell)
        self.record(replace(self.sell, execution_id="winner", premium=Decimal("200"),
                            occurred_at=CLOSE + timedelta(seconds=1)))
        summary = self.ledger.daily_summary(DAY)
        self.assertTrue(summary["breaker_tripped"])
        self.assertEqual(summary["realized_loss"], Decimal("40"))
        self.assertEqual(summary["realized_pnl"], Decimal("68"))
        self.assertEqual(sum(e["event"] == "circuit_breaker_triggered" for e in self.ledger.events()), 1)
        self.assertEqual(len(self.ledger.events(limit=1)), 1)
        with self.assertRaises(LedgerError):
            self.ledger.events(limit=0)

    def test_equal_timestamp_partial_fills_allowed_in_id_order(self):
        self.record(self.buy)
        self.record(replace(self.sell, execution_id="sell-a"))
        self.assertTrue(self.record(replace(self.sell, execution_id="sell-b")))
        self.assertEqual(self.ledger.inventory(), [])

    def test_fees_count_toward_loss_and_latch_and_replay_safely(self):
        self.assertTrue(self.ledger.record_fee("fee-1", trading_day=DAY, amount=Decimal("0.04"), recorded_at=CLOSE))
        self.assertFalse(self.ledger.record_fee("fee-1", trading_day=DAY, amount=Decimal("0.040"), recorded_at=CLOSE))
        with self.assertRaises(LedgerError):
            self.ledger.record_fee("fee-1", trading_day=DAY, amount=Decimal("0.05"), recorded_at=CLOSE)
        with self.assertRaises(LedgerError):
            self.ledger.record_fee("fee-neg", trading_day=DAY, amount=Decimal("-1"), recorded_at=CLOSE)
        summary = self.ledger.daily_summary(DAY)
        self.assertEqual(summary["realized_loss"], Decimal("0.04"))
        self.assertEqual(summary["realized_pnl"], Decimal("-0.04"))
        self.record(self.buy)
        self.record(replace(self.sell, premium=Decimal("52.04")))  # -39.96 + 0.04 fee = 40 exactly
        self.assertTrue(self.ledger.daily_summary(DAY)["breaker_tripped"])
        self.assertEqual(sum(e["event"] == "fee_accounted" for e in self.ledger.events()), 1)

    def test_sell_consumes_multiple_fifo_lots(self):
        self.record(replace(self.buy, quantity=1, premium=Decimal("80"), fees=Decimal("1")))
        self.record(replace(self.buy, execution_id="buy-2", quantity=1, premium=Decimal("100"), fees=Decimal("2"),
                            occurred_at=OPEN + timedelta(seconds=1)))
        self.record(replace(self.sell, quantity=2, premium=Decimal("180"), fees=Decimal("2")))
        self.assertEqual(self.ledger.daily_summary(DAY)["realized_pnl"], Decimal("-5"))
        self.assertEqual(self.ledger.inventory(), [])


if __name__ == "__main__":
    unittest.main()
