import csv
import gzip
import io
import tempfile
import unittest
from pathlib import Path

from phone_history_converter import ConversionError, convert_customer_csv


def make_history_row(phone: str, date: str, time: str, store: str = "店舗") -> list[str]:
    row = [""] * 18
    row[1] = date
    row[2] = time
    row[8] = phone
    row[11] = "担当"
    row[13] = "カテゴリ"
    row[14] = store
    row[16] = "ステータス"
    row[17] = "メモ"
    return row


class PhoneHistoryConverterTests(unittest.TestCase):
    def convert(self, customer_csv: bytes, history_rows: list[list[str]], applicant_only: bool = False):
        with tempfile.TemporaryDirectory() as directory:
            history_path = Path(directory) / "30秒省き.csv"
            with history_path.open("w", encoding="utf-8-sig", newline="") as history_file:
                csv.writer(history_file).writerows(history_rows)
            return convert_customer_csv(customer_csv, history_path, applicant_only)

    def test_converts_formula_phone_and_adds_call_time_marks(self):
        customer_csv = (
            "名前,代表名,tel番号\n"
            "店舗A,申込者A,=\"0358493767\"\n"
            "店舗B,,=\"0360000000\"\n"
        ).encode("cp932")
        history_rows = [
            make_history_row("358493767.0", "2026-10-12", "09:15:00"),
            make_history_row("358493767", "2026-10-12", "17:30:00", "最新店舗"),
        ]

        result, summary = self.convert(customer_csv, history_rows)
        rows = list(csv.reader(io.StringIO(result.decode("utf-8-sig"))))

        self.assertEqual(summary, {"processed": 2, "matched": 1})
        self.assertEqual(rows[1][0], "0358493767")
        self.assertEqual(rows[1][1], "最新店舗")
        self.assertEqual(rows[1][6], "2")
        self.assertEqual(rows[1][7], "○")
        self.assertEqual(rows[1][15], "○")
        self.assertEqual(rows[1][17], "○")
        self.assertEqual(rows[1][18], "×")

    def test_can_filter_to_rows_with_representative_name(self):
        customer_csv = (
            "名前,代表名,tel番号\n"
            "店舗A,申込者A,=\"0358493767\"\n"
            "店舗B,,=\"0360000000\"\n"
        ).encode("cp932")
        history_rows = [
            make_history_row("358493767", "2026-10-12", "10:00:00"),
            make_history_row("360000000", "2026-10-12", "10:00:00"),
        ]

        _, summary = self.convert(customer_csv, history_rows, applicant_only=True)

        self.assertEqual(summary, {"processed": 1, "matched": 1})

    def test_filter_requires_representative_column(self):
        customer_csv = "名前,tel番号\n店舗A,0358493767\n".encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            history_path = Path(directory) / "30秒省き.csv"
            history_path.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ConversionError, "代表名"):
                convert_customer_csv(customer_csv, history_path, include_representative_only=True)

    def test_missing_phone_column_is_reported(self):
        customer_csv = "名前,住所\n店舗A,東京都\n".encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            history_path = Path(directory) / "30秒省き.csv"
            history_path.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ConversionError, "電話番号"):
                convert_customer_csv(customer_csv, history_path)

    def test_reads_gzip_compressed_history(self):
        customer_csv = "名前,tel番号\n店舗A,0358493767\n".encode("utf-8")
        history_rows = [make_history_row("358493767", "2026-10-12", "11:00:00")]
        with tempfile.TemporaryDirectory() as directory:
            history_path = Path(directory) / "30秒省き.csv.gz"
            history_buffer = io.StringIO(newline="")
            csv.writer(history_buffer).writerows(history_rows)
            with gzip.open(history_path, "wb") as history_file:
                history_file.write(history_buffer.getvalue().encode("utf-8-sig"))

            _, summary = convert_customer_csv(customer_csv, history_path)

        self.assertEqual(summary, {"processed": 1, "matched": 1})


if __name__ == "__main__":
    unittest.main()
