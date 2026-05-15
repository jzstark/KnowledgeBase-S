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

    def test_lookback_hours_uses_datetime_since_filter(self):
        since, until = kb_tools._resolve_time_filters(
            {"lookback_hours": 24},
            now=datetime(2026, 5, 15, 11, 26, tzinfo=timezone.utc),
        )

        self.assertEqual(since.isoformat(), "2026-05-14T11:26:00+00:00")
        self.assertIsNone(until)


if __name__ == "__main__":
    unittest.main()
