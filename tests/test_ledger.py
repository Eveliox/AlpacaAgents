from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path
import sqlite3
import tempfile
import unittest

from alpaca_agents.executor.ledger import ClosedTrade, LedgerError, TradeLedger

DAY = date(2026, 9, 15)
CLOSE = datetime(2026, 9, 15, 18, tzinfo=timezone.utc)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "ledger.sqlite3"
        self.ledger = TradeLedger(self.path)
        self.trade = ClosedTrade("close-1", "position-1", DAY, CLOSE - timedelta(days=1), CLOSE,
                                 Decimal("90"), Decimal("51"), Decimal("1"), "stop_hit")

    def record(self, trade=None):
        return self.ledger.record_closed_trade(trade or self.trade, recorded_at=CLOSE)

    def test_pnl_fees_and_exact_breaker_boundary_survive_restart(self):
        self.assertTrue(self.record())
        summary = TradeLedger(self.path).daily_summary(DAY)
        self.assertEqual(summary.realized_pnl, Decimal("-40"))
        self.assertEqual(summary.realized_loss, Decimal("40"))
        self.assertTrue(summary.breaker_tripped)
        self.assertEqual(summary.closed_trades, 1)
        self.assertEqual([e["event"] for e in self.ledger.events()],
                         ["position_closed", "circuit_breaker_triggered"])

    def test_under_boundary_then_winner_does_not_offset_loss(self):
        self.record(replace(self.trade, exit_credit=Decimal("51.01")))
        self.assertFalse(self.ledger.daily_summary(DAY).breaker_tripped)
        self.record(replace(self.trade, close_id="win", position_id="win", exit_credit=Decimal("191")))
        self.record(replace(self.trade, close_id="loss", position_id="loss", exit_credit=Decimal("90.99")))
        summary = self.ledger.daily_summary(DAY)
        self.assertEqual(summary.realized_pnl, Decimal("60"))
        self.assertEqual(summary.realized_loss, Decimal("40"))
        self.assertTrue(summary.breaker_tripped)
        self.record(replace(self.trade, close_id="win2", position_id="win2", exit_credit=Decimal("191")))
        self.assertTrue(self.ledger.daily_summary(DAY).breaker_tripped)
        self.assertEqual(sum(e["event"] == "circuit_breaker_triggered" for e in self.ledger.events()), 1)

    def test_duplicate_replay_and_conflict(self):
        self.record()
        self.assertFalse(self.record(replace(self.trade, fees=Decimal("1.00"))))
        for change in ({"exit_credit": Decimal("50")}, {"close_id": "another"}, {"position_id": "another"}):
            with self.subTest(change=change), self.assertRaises(LedgerError):
                self.record(replace(self.trade, **change))
        self.assertEqual(self.ledger.daily_summary(DAY).closed_trades, 1)
        self.assertEqual(len(self.ledger.events()), 2)

    def test_late_old_day_record_does_not_change_new_day(self):
        old_day = DAY - timedelta(days=1)
        old = replace(self.trade, trading_day=old_day, opened_at=CLOSE - timedelta(days=2),
                      closed_at=CLOSE - timedelta(days=1))
        self.record(old)
        self.assertTrue(self.ledger.daily_summary(old_day).breaker_tripped)
        self.assertFalse(self.ledger.daily_summary(DAY).breaker_tripped)
        self.assertEqual(self.ledger.daily_summary(DAY).closed_trades, 0)
        # Looking up another day never clears the old latch.
        self.assertTrue(self.ledger.daily_summary(old_day).breaker_tripped)

    def test_bad_input_never_writes(self):
        for change in (
            {"entry_debit": Decimal("NaN")}, {"fees": Decimal("-1")},
            {"exit_credit": Decimal("Infinity")}, {"fees": 1.0},
            {"entry_debit": Decimal(0)}, {"fees": Decimal("0.0000001")},
            {"closed_at": CLOSE.replace(tzinfo=None)},
            {"opened_at": CLOSE + timedelta(seconds=1)},
            {"trading_day": CLOSE}, {"reason": "unknown"}, {"close_id": "bad\nvalue"},
        ):
            with self.subTest(change=change), self.assertRaises(LedgerError):
                self.record(replace(self.trade, **change))
        with self.assertRaises(LedgerError):
            self.ledger.record_closed_trade(self.trade, recorded_at=CLOSE - timedelta(seconds=1))
        self.assertEqual(self.ledger.events(), [])

    def test_accounting_does_not_depend_on_global_decimal_precision(self):
        with localcontext() as context:
            context.prec = 2
            self.record(replace(self.trade, exit_credit=Decimal("51.01")))
            self.assertEqual(self.ledger.daily_summary(DAY).realized_loss, Decimal("39.99"))

    def test_audit_failure_rolls_back_close_and_breaker(self):
        db = sqlite3.connect(self.path)
        try:
            db.execute("""CREATE TRIGGER reject_event BEFORE INSERT ON ledger_events
                          WHEN NEW.event = 'circuit_breaker_triggered'
                          BEGIN SELECT RAISE(ABORT, 'simulated audit failure'); END""")
            db.commit()
        finally:
            db.close()
        with self.assertRaises(sqlite3.IntegrityError):
            self.record()
        self.assertEqual(self.ledger.daily_summary(DAY).closed_trades, 0)
        self.assertFalse(self.ledger.daily_summary(DAY).breaker_tripped)
        self.assertEqual(self.ledger.events(), [])

    def test_concurrent_replay_records_once(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.record(), range(2)))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(self.ledger.daily_summary(DAY).closed_trades, 1)

    def test_concurrent_losses_trip_once(self):
        trades = [replace(self.trade, close_id=f"close-{i}", position_id=f"pos-{i}",
                          exit_credit=Decimal("69")) for i in range(2)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(self.record, trades)), [True, True])
        self.assertEqual(self.ledger.daily_summary(DAY).realized_loss, Decimal("44"))
        self.assertEqual(sum(e["event"] == "circuit_breaker_triggered" for e in self.ledger.events()), 1)


if __name__ == "__main__":
    unittest.main()
