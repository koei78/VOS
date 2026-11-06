import unittest
from datetime import date

from app import future_monthly_summary_rows


class FutureMonthlySummaryRowsTests(unittest.TestCase):
    def test_finds_future_months_across_year_boundary(self):
        rows = [
            ["鈴木"],
            ["9月"],
            ["10月"],
            ["11月"],
            ["12月"],
            ["1月"],
            ["2月"],
            ["合計"],
            [],
            ["杉本"],
            ["9月"],
            ["10月"],
            ["11月"],
            ["12月"],
            ["1月"],
            ["2月"],
        ]

        self.assertEqual(
            future_monthly_summary_rows(rows, today=date(2026, 11, 6)),
            [5, 6, 7, 14, 15, 16],
        )

    def test_ignores_other_users(self):
        rows = [["阿部"], ["11月"], ["12月"], ["合計"]]

        self.assertEqual(
            future_monthly_summary_rows(rows, today=date(2026, 11, 6)),
            [],
        )


if __name__ == "__main__":
    unittest.main()
