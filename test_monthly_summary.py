import unittest
from datetime import date
from unittest.mock import patch

import app as app_module
from app import future_monthly_summary_rows, get_user_workbook


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


class UserWorkbookTests(unittest.TestCase):
    def test_resolves_distinct_kitagawa_and_mori_workbooks(self):
        kitagawa = get_user_workbook("kitagawa")
        mori = get_user_workbook("mori")

        self.assertEqual(kitagawa["label"], "北川班")
        self.assertEqual(mori["label"], "森班")
        self.assertNotEqual(kitagawa["spreadsheetId"], mori["spreadsheetId"])

    def test_user_management_list_uses_selected_workbook(self):
        with patch.object(app_module, "read_managed_users", return_value=[]) as reader:
            response = app_module.app.test_client().get(
                "/api/user-management?workbook=mori"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["workbook"], "mori")
        reader.assert_called_once_with("mori")

    def test_user_management_rejects_unknown_workbook(self):
        response = app_module.app.test_client().get(
            "/api/user-management?workbook=unknown"
        )

        self.assertEqual(response.status_code, 400)

    def test_user_management_save_writes_to_selected_workbook(self):
        saved_users = [
            {
                "row": 2,
                "order": "1",
                "name": "森テスト",
                "enabled": True,
                "createdAt": "2026-11-06",
                "memo": "",
            }
        ]
        with (
            patch.object(
                app_module,
                "read_managed_users",
                side_effect=[([], [2]), saved_users],
            ) as reader,
            patch.object(app_module, "update_user_sheet_row") as updater,
        ):
            response = app_module.app.test_client().post(
                "/api/user-management",
                json={"workbook": "mori", "name": "森テスト", "enabled": True},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["workbook"], "mori")
        self.assertEqual(reader.call_args_list[0].args, ("mori",))
        self.assertTrue(reader.call_args_list[0].kwargs["include_blanks"])
        self.assertEqual(updater.call_args.args[0:2], ("mori", 2))

    def test_mori_refresh_does_not_run_kitagawa_gas(self):
        with (
            patch.object(app_module.requests, "post") as post,
            patch.object(
                app_module,
                "reset_future_monthly_summary",
                return_value=[],
            ) as reset,
        ):
            result = app_module.refresh_monthly_summary("mori")

        self.assertTrue(result["ok"])
        self.assertTrue(result["monthlySummarySkipped"])
        post.assert_not_called()
        reset.assert_called_once_with(
            app_module.get_user_workbook("mori")["spreadsheetId"]
        )


if __name__ == "__main__":
    unittest.main()
