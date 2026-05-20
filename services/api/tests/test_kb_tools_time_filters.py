import unittest
from datetime import datetime, timezone

import kb_tools


class TimeFilterTests(unittest.TestCase):
    def test_date_string_filters_are_bound_as_datetimes(self):
        params = ["default"]

        clause = kb_tools._time_filter_clause(
            params,
            since="2026-05-13",
            until=None,
            time_basis="published",
        )

        self.assertEqual(clause, "n.source_published_at >= $2::timestamptz")
        self.assertIsInstance(params[1], datetime)
        self.assertEqual(params[1].date().isoformat(), "2026-05-13")

    def test_captured_time_basis_filters_by_captured_at(self):
        params = ["default"]

        clause = kb_tools._time_filter_clause(
            params,
            since="2026-05-14T16:00:00Z",
            until=None,
            time_basis="captured",
        )

        self.assertEqual(clause, "n.captured_at >= $2::timestamptz")
        self.assertIsInstance(params[1], datetime)
        self.assertEqual(params[1].isoformat(), "2026-05-14T16:00:00+00:00")

    def test_lookback_hours_uses_datetime_since_filter(self):
        since, until = kb_tools._resolve_time_filters(
            {"lookback_hours": 24},
            now=datetime(2026, 5, 15, 11, 26, tzinfo=timezone.utc),
        )

        self.assertEqual(since.isoformat(), "2026-05-14T11:26:00+00:00")
        self.assertEqual(until.isoformat(), "2026-05-15T11:26:00+00:00")


if __name__ == "__main__":
    unittest.main()
