import csv
import io
import tempfile
import unittest
from pathlib import Path

from app import spreadsheet_safe_csv


class SpreadsheetSafeCsvTests(unittest.TestCase):
    def test_converts_all_configured_phone_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "call_log.csv"
            with path.open("w", encoding="cp932", newline="") as output:
                csv.writer(output).writerows([
                    [],
                    ["ID", "開始", "通話開始", "終了", "発信", "受信", "秒", "代表"],
                    ["1", "", "", "", "0358493767", "9012345678.0", "30", "05012345678"],
                ])

            result = spreadsheet_safe_csv(path, (4, 5, 7))

        rows = list(csv.reader(io.StringIO(result.decode("utf-8-sig"))))
        self.assertEqual(rows[1][4], "発信")
        self.assertEqual(rows[2][4], '=\"0358493767\"')
        self.assertEqual(rows[2][5], '=\"09012345678\"')
        self.assertEqual(rows[2][7], '=\"05012345678\"')


if __name__ == "__main__":
    unittest.main()
