"""Convert a customer CSV into phone-number call-history search results."""

from __future__ import annotations

import csv
import gzip
import io
import re
import unicodedata
from datetime import datetime
from pathlib import Path


OUTPUT_HEADERS = [
    "検索番号",
    "店舗名",
    "カテゴリ",
    "担当者名",
    "ステータス",
    "メモ",
    "通話件数",
] + [f"{hour}時" for hour in range(9, 19)] + [
    "月曜",
    "火曜",
    "水曜",
    "木曜",
    "金曜",
]

PHONE_COLUMN_NAMES = {"tel番号", "電話番号", "電話"}
REPRESENTATIVE_COLUMN_NAME = "代表名"


class ConversionError(ValueError):
    """An input file or required source file cannot be converted."""


def _decode_csv(data: bytes, filename: str) -> str:
    for encoding in ("utf-8-sig", "cp932"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ConversionError(f"{filename}を読み込めません。UTF-8またはShift-JISのCSVを指定してください。")


def _normalized_header(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()


def _clean_phone(value: str) -> str:
    phone = unicodedata.normalize("NFKC", str(value or "")).strip()
    if phone.startswith("="):
        phone = phone[1:].strip()
    if len(phone) >= 2 and phone[0] == phone[-1] == '"':
        phone = phone[1:-1]
    if phone.endswith(".0"):
        phone = phone[:-2]

    phone = re.sub(r"[\s\-()]", "", phone)
    return phone if phone.isdigit() else ""


def normalize_phone(value: str) -> str:
    """Normalize CSV phone values, including Excel's ="0123..." format."""
    return _clean_phone(value).lstrip("0")


def spreadsheet_phone(value: str) -> str:
    """Keep a phone number as text when the CSV is imported into a spreadsheet."""
    phone = _clean_phone(value)
    return f'=\"{phone}\"' if phone else ""


def _find_column(headers: list[str], names: set[str]) -> int | None:
    normalized_names = {_normalized_header(name) for name in names}
    for index, header in enumerate(headers):
        if _normalized_header(header) in normalized_names:
            return index
    return None


def _read_customer_phones(
    customer_csv: bytes,
    include_representative_only: bool,
) -> list[tuple[str, str]]:
    text = _decode_csv(customer_csv, "アップロードしたファイル")
    try:
        rows = iter(csv.reader(io.StringIO(text)))
        headers = next(rows, None)
    except csv.Error as exc:
        raise ConversionError(f"顧客CSVを解析できません: {exc}") from exc

    if not headers:
        raise ConversionError("顧客CSVに見出し行がありません。")

    phone_column = _find_column(headers, PHONE_COLUMN_NAMES)
    if phone_column is None:
        raise ConversionError("「tel番号」または「電話番号」列が見つかりません。")

    representative_column = _find_column(headers, {REPRESENTATIVE_COLUMN_NAME})
    if include_representative_only and representative_column is None:
        raise ConversionError("「代表名」列が見つからないため、代表名による絞り込みができません。")

    phones: list[tuple[str, str]] = []
    seen: set[str] = set()
    try:
        for row in rows:
            if phone_column >= len(row):
                continue
            representative = (
                row[representative_column].strip()
                if representative_column is not None and representative_column < len(row)
                else ""
            )
            if include_representative_only and not representative:
                continue

            original_phone = _clean_phone(row[phone_column])
            normalized_phone = normalize_phone(original_phone)
            if normalized_phone and normalized_phone not in seen:
                phones.append((original_phone, normalized_phone))
                seen.add(normalized_phone)
    except csv.Error as exc:
        raise ConversionError(f"顧客CSVを解析できません: {exc}") from exc

    if not phones:
        raise ConversionError("検索対象の電話番号が見つかりませんでした。")
    return phones


def _read_call_history(history_csv: bytes, phones: set[str]) -> dict[str, list[list[str]]]:
    text = _decode_csv(history_csv, "通話履歴CSV")
    matches = {phone: [] for phone in phones}
    try:
        for row in csv.reader(io.StringIO(text)):
            if len(row) <= 8:
                continue
            phone = normalize_phone(row[8])
            if phone in matches:
                matches[phone].append(row)
    except csv.Error as exc:
        raise ConversionError(f"通話履歴CSVを解析できません: {exc}") from exc
    return matches


def _build_output_rows(
    phones: list[tuple[str, str]],
    matches: dict[str, list[list[str]]],
) -> list[list[str]]:
    output_rows = []
    for original_phone, normalized_phone in phones:
        history_rows = matches[normalized_phone]
        if not history_rows:
            continue

        hit_hours: set[int] = set()
        hit_weekdays: set[int] = set()
        for row in history_rows:
            if len(row) > 2:
                try:
                    hour = int(row[2].strip().split(":", 1)[0])
                    if 9 <= hour <= 18:
                        hit_hours.add(hour)
                except ValueError:
                    pass
            if len(row) > 1:
                try:
                    weekday = datetime.strptime(row[1].strip(), "%Y-%m-%d").weekday()
                    if weekday <= 4:
                        hit_weekdays.add(weekday)
                except ValueError:
                    pass

        latest = history_rows[-1]
        padded_latest = latest + [""] * max(0, 18 - len(latest))
        output_rows.append(
            [
                spreadsheet_phone(original_phone),
                padded_latest[14].strip(),
                padded_latest[13].strip(),
                padded_latest[11].strip(),
                padded_latest[16].strip(),
                padded_latest[17].strip(),
                str(len(history_rows)),
            ]
            + ["○" if hour in hit_hours else "×" for hour in range(9, 19)]
            + ["○" if weekday in hit_weekdays else "×" for weekday in range(5)]
        )
    return output_rows


def convert_customer_csv(
    customer_csv: bytes,
    history_csv_path: str | Path,
    include_representative_only: bool = False,
) -> tuple[bytes, dict[str, int]]:
    """Search customer phone numbers in call history and return a UTF-8 CSV."""
    phones = _read_customer_phones(customer_csv, include_representative_only)
    history_path = Path(history_csv_path)
    try:
        if history_path.suffix.casefold() == ".gz":
            with gzip.open(history_path, "rb") as history_file:
                history_csv = history_file.read()
        else:
            history_csv = history_path.read_bytes()
    except OSError as exc:
        raise ConversionError(f"通話履歴ファイルを読み込めません: {history_path.name}") from exc

    matches = _read_call_history(history_csv, {phone for _, phone in phones})
    output_rows = _build_output_rows(phones, matches)

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(OUTPUT_HEADERS)
    writer.writerows(output_rows)

    return output.getvalue().encode("utf-8-sig"), {
        "processed": len(phones),
        "matched": len(output_rows),
    }
