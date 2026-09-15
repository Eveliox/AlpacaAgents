from datetime import date
import unittest

from alpaca_agents.calendar import easter, holidays, is_session, previous_session, sessions_between


class CalendarTests(unittest.TestCase):
    def test_known_holidays_2024_2026(self):
        expected = {
            2024: {date(2024, 1, 1), date(2024, 1, 15), date(2024, 2, 19), date(2024, 3, 29), date(2024, 5, 27),
                   date(2024, 6, 19), date(2024, 7, 4), date(2024, 9, 2), date(2024, 11, 28), date(2024, 12, 25)},
            2025: {date(2025, 1, 1), date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18), date(2025, 5, 26),
                   date(2025, 6, 19), date(2025, 7, 4), date(2025, 9, 1), date(2025, 11, 27), date(2025, 12, 25)},
            2026: {date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
                   date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25)},
        }
        for year, days in expected.items():
            with self.subTest(year=year):
                self.assertEqual(holidays(year), frozenset(days))

    def test_weekend_observance_rules(self):
        self.assertIn(date(2021, 7, 5), holidays(2021))      # July 4 on Sunday -> Monday
        self.assertIn(date(2020, 7, 3), holidays(2020))      # July 4 on Saturday -> Friday
        self.assertNotIn(date(2021, 12, 31), holidays(2021)) # Jan 1 2022 is Saturday: not observed on Dec 31
        self.assertNotIn(date(2022, 1, 1), holidays(2022))
        self.assertIn(date(2022, 12, 26), holidays(2022))    # Christmas on Sunday -> Monday
        self.assertNotIn(date(2021, 6, 19), holidays(2021))  # Juneteenth not observed by NYSE before 2022

    def test_easter(self):
        self.assertEqual(easter(2024), date(2024, 3, 31))
        self.assertEqual(easter(2025), date(2025, 4, 20))
        self.assertEqual(easter(2026), date(2026, 4, 5))

    def test_sessions(self):
        self.assertTrue(is_session(date(2026, 9, 15)))
        self.assertFalse(is_session(date(2026, 9, 12)))       # Saturday
        self.assertFalse(is_session(date(2026, 9, 7)))        # Labor Day
        self.assertEqual(previous_session(date(2026, 9, 8)), date(2026, 9, 4))   # skips Labor Day + weekend
        self.assertEqual(previous_session(date(2026, 9, 15)), date(2026, 9, 14))
        self.assertEqual(sessions_between(date(2026, 9, 4), date(2026, 9, 11)), 4)  # 8,9,10,11 (7th is holiday)
        self.assertEqual(sessions_between(date(2026, 9, 15), date(2026, 9, 15)), 0)
        with self.assertRaises(TypeError):
            is_session("2026-09-15")


if __name__ == "__main__":
    unittest.main()
