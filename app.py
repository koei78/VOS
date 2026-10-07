import threading
import uuid
import json
import os
import urllib.parse
import csv
import sqlite3
import tempfile
import zipfile
import base64
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timedelta
from flask import Flask, render_template, request, jsonify, send_file
import io

from carrier_lookup import FixedLineCarrierLookup
from vos import VOSClient


carrier_lookup = FixedLineCarrierLookup()
DEFAULT_LOGIN_ID = os.getenv("VOS_DEFAULT_LOGIN_ID", "SDBDY07016")
DEFAULT_PASSWORD = os.getenv("VOS_DEFAULT_PASSWORD", "")
DB_PATH = os.path.join(os.path.dirname(__file__), "call_history.sqlite3")
DOWNLOAD_OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "download_outputs")
USER_SHEET_ID = os.getenv("USER_SHEET_ID", "1sMrQ5FgLbo_AQxXnhLTZIEY36vu6_bSgq9R8s6J3KeU")
USER_SHEET_NAME = os.getenv("USER_SHEET_NAME", "管理")
USER_SHEET_MAX_ROWS = int(os.getenv("USER_SHEET_MAX_ROWS", "1000"))
USER_SHEET_HEADERS = ["表示順", "ユーザー名", "有効", "登録日時", "メモ"]

CALL_HISTORY_COLUMNS = [
    "log_id",
    "phone_start_date",
    "phone_start_time",
    "call_start_date",
    "call_start_time",
    "call_end_date",
    "call_end_time",
    "caller_number",
    "receiver_number",
    "duration_seconds",
    "representative_number",
    "user_name",
    "customer_id",
    "project_list",
    "customer_name",
    "representative_name",
    "applicant_name",
    "applicant_info_json",
    "call_type",
    "call_flag",
    "call_history",
]

CALL_HISTORY_LABELS = {
    "log_id": "ログID",
    "phone_start_date": "電話開始日",
    "phone_start_time": "電話開始時刻",
    "call_start_date": "通話開始日",
    "call_start_time": "通話開始時刻",
    "call_end_date": "通話終了日",
    "call_end_time": "通話終了時刻",
    "caller_number": "発信者番号",
    "receiver_number": "受信者番号",
    "duration_seconds": "電話時間(秒)",
    "representative_number": "代表番号",
    "user_name": "ユーザー",
    "customer_id": "顧客ID",
    "project_list": "プロジェクトリスト",
    "customer_name": "顧客名",
    "representative_name": "代表者名",
    "applicant_name": "申込者名",
    "applicant_info_json": "申込者情報",
    "call_type": "タイプ",
    "call_flag": "通話フラグ",
    "call_history": "通話履歴",
}


def _safe_cookies(cookies: dict) -> dict:
    """クッキー値にlatin-1で表せない文字が含まれる場合にURLエンコードする"""
    safe = {}
    for k, v in cookies.items():
        try:
            str(v).encode("latin-1")
            safe[k] = v
        except UnicodeEncodeError:
            safe[k] = urllib.parse.quote(str(v), safe="")
    return safe

app = Flask(__name__)
app.secret_key = "vos-flask-secret-key-2026"

# ジョブ管理（メモリ内）
jobs: dict = {}
_sheets_service = None


def new_job() -> str:
    jid = str(uuid.uuid4())
    jobs[jid] = {"logs": [], "done": False, "ok": 0, "ng": 0}
    return jid


def job_log(jid: str, msg: str):
    jobs[jid]["logs"].append(msg)


def init_call_history_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS call_history (
                log_id TEXT PRIMARY KEY,
                phone_start_date TEXT,
                phone_start_time TEXT,
                call_start_date TEXT,
                call_start_time TEXT,
                call_end_date TEXT,
                call_end_time TEXT,
                caller_number TEXT,
                receiver_number TEXT,
                duration_seconds INTEGER,
                representative_number TEXT,
                user_name TEXT,
                customer_id TEXT,
                project_list TEXT,
                customer_name TEXT,
                representative_name TEXT,
                applicant_name TEXT,
                applicant_info_json TEXT,
                call_type TEXT,
                call_flag TEXT,
                call_history TEXT,
                raw_json TEXT,
                imported_at TEXT NOT NULL
            )
            """
        )
        existing_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(call_history)").fetchall()
        }
        if "representative_name" not in existing_columns:
            conn.execute("ALTER TABLE call_history ADD COLUMN representative_name TEXT")
        if "applicant_name" not in existing_columns:
            conn.execute("ALTER TABLE call_history ADD COLUMN applicant_name TEXT")
        if "applicant_info_json" not in existing_columns:
            conn.execute("ALTER TABLE call_history ADD COLUMN applicant_info_json TEXT")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS customer_representatives (
                customer_id TEXT PRIMARY KEY,
                representative_name TEXT,
                applicant_name TEXT,
                applicant_info_json TEXT,
                status TEXT NOT NULL DEFAULT '',
                error TEXT,
                checked_at TEXT NOT NULL
            )
            """
        )
        representative_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(customer_representatives)").fetchall()
        }
        if "applicant_name" not in representative_columns:
            conn.execute("ALTER TABLE customer_representatives ADD COLUMN applicant_name TEXT")
        if "applicant_info_json" not in representative_columns:
            conn.execute("ALTER TABLE customer_representatives ADD COLUMN applicant_info_json TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_call_history_call_start_date ON call_history(call_start_date)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_call_history_latest "
            "ON call_history(call_start_date DESC, call_start_time DESC, log_id DESC)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_call_history_customer_id ON call_history(customer_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_call_history_receiver_number ON call_history(receiver_number)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_call_history_representative_name ON call_history(representative_name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_call_history_applicant_name ON call_history(applicant_name)")


def split_datetime(dt_str):
    if not dt_str:
        return "", ""
    parts = dt_str.split(" ", 1)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 else (dt_str.strip(), "")


def clean_cell(value):
    return (value or "").replace("\r\n", " ").replace("\r", " ").replace("\n", " ").strip()


def normalize_number(value):
    value = clean_cell(value)
    if not value:
        return ""
    try:
        return str(int(float(value)))
    except ValueError:
        return value


def parse_call_log_csv(path):
    encoding = "utf-8-sig"
    try:
        with open(path, encoding=encoding) as f:
            f.read()
    except UnicodeDecodeError:
        encoding = "cp932"

    rows = []
    with open(path, "r", encoding=encoding, newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            if row:
                break

        for row in reader:
            if not row or len(row) < 15:
                continue
            tel_start_date, tel_start_time = split_datetime(row[1])
            call_start_date, call_start_time = split_datetime(row[2])
            call_end_date, call_end_time = split_datetime(row[3])
            record = {
                "log_id": clean_cell(row[0]),
                "phone_start_date": tel_start_date,
                "phone_start_time": tel_start_time,
                "call_start_date": call_start_date,
                "call_start_time": call_start_time,
                "call_end_date": call_end_date,
                "call_end_time": call_end_time,
                "caller_number": normalize_number(row[4]),
                "receiver_number": normalize_number(row[5]),
                "duration_seconds": int(row[6]) if clean_cell(row[6]).isdigit() else None,
                "representative_number": normalize_number(row[7]),
                "user_name": clean_cell(row[8]),
                "customer_id": clean_cell(row[9]),
                "project_list": clean_cell(row[10]),
                "customer_name": clean_cell(row[11]),
                "representative_name": "",
                "applicant_name": "",
                "applicant_info_json": "",
                "call_type": clean_cell(row[12]),
                "call_flag": clean_cell(row[13]),
                "call_history": clean_cell(row[14]),
                "raw_json": json.dumps(row, ensure_ascii=False),
                "imported_at": datetime.now().isoformat(timespec="seconds"),
            }
            if record["log_id"]:
                rows.append(record)
    return rows


def phone_for_vos(value):
    raw = str(value or "").strip()
    if raw.endswith(".0"):
        raw = raw[:-2]
    tel = "".join(ch for ch in raw if ch.isdigit())
    if not tel:
        return ""
    if not tel.startswith("0") and len(tel) == 9:
        tel = "0" + tel
    return tel


def usable_vos_phone(value):
    tel = phone_for_vos(value)
    if len(tel) not in {10, 11}:
        return ""
    if not tel.startswith("0"):
        return ""
    if set(tel) == {"0"}:
        return ""
    return tel


def chunked(values, size=800):
    for index in range(0, len(values), size):
        yield values[index:index + size]


def row_matches_download_filters(row, filters):
    user = (filters.get("user") or "").strip()
    if user and user not in row.get("user_name", ""):
        return False

    keyword = (filters.get("keyword") or "").strip()
    if keyword:
        haystack = " ".join([
            row.get("project_list", ""),
            row.get("customer_name", ""),
            row.get("call_flag", ""),
            row.get("call_history", ""),
        ])
        if keyword not in haystack:
            return False

    min_seconds = filters.get("min_seconds")
    if min_seconds is not None:
        duration = row.get("duration_seconds")
        if duration is None or duration < min_seconds:
            return False

    hour = (filters.get("hour") or "").strip()
    if hour:
        try:
            if int((row.get("call_start_time") or "0").split(":", 1)[0]) != int(hour):
                return False
        except ValueError:
            return False

    return True


def row_output_values(row, representative_name):
    return [
        row.get("receiver_number", ""),
        representative_name,
        row.get("customer_id", ""),
        row.get("customer_name", ""),
        row.get("project_list", ""),
        row.get("user_name", ""),
        row.get("call_start_date", ""),
        row.get("call_start_time", ""),
        row.get("duration_seconds", ""),
        row.get("call_flag", ""),
        row.get("call_history", ""),
    ]


def write_representative_outputs(rows, output_prefix):
    os.makedirs(DOWNLOAD_OUTPUT_DIR, exist_ok=True)
    header = [
        "電話番号",
        "代表者名",
        "顧客ID",
        "顧客名",
        "プロジェクトリスト",
        "担当者",
        "通話日",
        "通話時刻",
        "通話秒数",
        "通話フラグ",
        "通話履歴",
    ]
    all_path = os.path.join(DOWNLOAD_OUTPUT_DIR, f"{output_prefix}_代表者名あり.csv")
    with open(all_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for row, representative_name in rows:
            writer.writerow(row_output_values(row, representative_name))

    by_hour = {}
    for row, representative_name in rows:
        hour = (row.get("call_start_time") or "不明").split(":", 1)[0] or "不明"
        by_hour.setdefault(hour, []).append((row, representative_name))

    hour_paths = []
    for hour, hour_rows in sorted(by_hour.items()):
        path = os.path.join(DOWNLOAD_OUTPUT_DIR, f"{output_prefix}_{hour}時_代表者名あり.csv")
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            for row, representative_name in hour_rows:
                writer.writerow(row_output_values(row, representative_name))
        hour_paths.append(path)

    zip_path = os.path.join(DOWNLOAD_OUTPUT_DIR, f"{output_prefix}_時間別_代表者名あり.zip")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.write(all_path, os.path.basename(all_path))
        for path in hour_paths:
            z.write(path, os.path.basename(path))

    return all_path, zip_path, hour_paths


def insert_call_history_rows(rows):
    if not rows:
        return 0, 0
    init_call_history_db()
    fields = CALL_HISTORY_COLUMNS + ["raw_json", "imported_at"]
    placeholders = ",".join("?" for _ in fields)
    sql = f"""
        INSERT OR IGNORE INTO call_history ({",".join(fields)})
        VALUES ({placeholders})
    """
    with sqlite3.connect(DB_PATH) as conn:
        before = conn.total_changes
        conn.executemany(sql, [[row.get(field) for field in fields] for row in rows])
        added = conn.total_changes - before
        conn.execute(
            """
            UPDATE call_history
            SET representative_name = (
                SELECT representative_name
                FROM customer_representatives
                WHERE customer_representatives.customer_id = call_history.customer_id
            ),
            applicant_name = (
                SELECT applicant_name
                FROM customer_representatives
                WHERE customer_representatives.customer_id = call_history.customer_id
            ),
            applicant_info_json = (
                SELECT applicant_info_json
                FROM customer_representatives
                WHERE customer_representatives.customer_id = call_history.customer_id
            )
            WHERE customer_id IN (
                SELECT customer_id FROM customer_representatives
            )
              AND (representative_name IS NULL OR representative_name = '')
            """
        )
    return added, len(rows) - added


def representative_stats():
    init_call_history_db()
    with sqlite3.connect(DB_PATH) as conn:
        total_ids = conn.execute(
            "SELECT COUNT(DISTINCT customer_id) FROM call_history WHERE customer_id IS NOT NULL AND customer_id != ''"
        ).fetchone()[0]
        checked_ids = conn.execute("SELECT COUNT(*) FROM customer_representatives").fetchone()[0]
        named_ids = conn.execute(
            "SELECT COUNT(*) FROM customer_representatives WHERE representative_name IS NOT NULL AND representative_name != ''"
        ).fetchone()[0]
    return {
        "totalIds": total_ids,
        "checkedIds": checked_ids,
        "namedIds": named_ids,
        "remainingIds": max(total_ids - checked_ids, 0),
    }


def missing_representative_ids(limit=None):
    init_call_history_db()
    sql = """
        SELECT DISTINCT h.customer_id
        FROM call_history h
        LEFT JOIN customer_representatives r ON r.customer_id = h.customer_id
        WHERE h.customer_id IS NOT NULL
          AND h.customer_id != ''
          AND r.customer_id IS NULL
        ORDER BY h.customer_id
    """
    params = []
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    with sqlite3.connect(DB_PATH) as conn:
        return [row[0] for row in conn.execute(sql, params).fetchall()]


def uncached_representative_ids(customer_ids):
    ids = sorted({str(customer_id) for customer_id in customer_ids if str(customer_id or "").strip()})
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    with sqlite3.connect(DB_PATH) as conn:
        cached = {
            row[0]
            for row in conn.execute(
                f"SELECT customer_id FROM customer_representatives WHERE customer_id IN ({placeholders})",
                ids,
            ).fetchall()
        }
    return [customer_id for customer_id in ids if customer_id not in cached]


def representative_name_map(customer_ids):
    ids = sorted({str(customer_id) for customer_id in customer_ids if str(customer_id or "").strip()})
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    with sqlite3.connect(DB_PATH) as conn:
        return {
            row[0]: row[1] or ""
            for row in conn.execute(
                f"SELECT customer_id, representative_name FROM customer_representatives WHERE customer_id IN ({placeholders})",
                ids,
            ).fetchall()
        }


def customer_phone_map(customer_ids):
    ids = sorted({str(customer_id) for customer_id in customer_ids if str(customer_id or "").strip()})
    if not ids:
        return {}
    phones = {}

    def consider(customer_id, value, base_score):
        tel = usable_vos_phone(value)
        if not tel:
            return
        score = base_score + (2 if len(tel) == 10 else 1)
        current = phones.get(customer_id)
        if not current or score > current[1]:
            phones[customer_id] = (tel, score)

    with sqlite3.connect(DB_PATH) as conn:
        for group in chunked(ids):
            placeholders = ",".join("?" for _ in group)
            rows = conn.execute(
                f"""
                SELECT customer_id, receiver_number, caller_number, representative_number
                FROM call_history
                WHERE customer_id IN ({placeholders})
                """,
                group,
            ).fetchall()
            for customer_id, receiver_number, caller_number, representative_number in rows:
                consider(customer_id, receiver_number, 30)
                consider(customer_id, caller_number, 20)
                consider(customer_id, representative_number, 10)
    return {customer_id: tel for customer_id, (tel, _score) in phones.items()}


def pick_export_value(row, include_keywords, exclude_keywords=()):
    for key, value in row.items():
        normalized_key = str(key or "").replace(" ", "").replace("　", "")
        if all(keyword in normalized_key for keyword in include_keywords) and not any(keyword in normalized_key for keyword in exclude_keywords):
            cleaned = clean_cell(value)
            if cleaned:
                return cleaned
    return ""


def extract_customer_export_info(export_rows, customer_id="", tel=""):
    if not export_rows:
        return "", "", {}
    selected = export_rows[0] if len(export_rows) == 1 else {}
    if customer_id:
        for row in export_rows:
            if any(clean_cell(value) == str(customer_id) for value in row.values()):
                selected = row
                break
    if tel and not selected:
        target_tel = usable_vos_phone(tel)
        for row in export_rows:
            if any(usable_vos_phone(value) == target_tel for value in row.values()):
                selected = row
                break
    if not selected:
        return "", "", {}
    representative_name = (
        pick_export_value(selected, ("代表", "名"))
        or pick_export_value(selected, ("代表者",))
        or pick_export_value(selected, ("代表",))
    )
    applicant_name = (
        pick_export_value(selected, ("申込", "名"))
        or pick_export_value(selected, ("申込者",))
        or pick_export_value(selected, ("申込",))
    )
    applicant_info = {
        key: clean_cell(value)
        for key, value in selected.items()
        if "申込" in str(key or "") and clean_cell(value)
    }
    return representative_name, applicant_name, applicant_info


def save_representative_result(customer_id, representative_name, status="", error="", applicant_name="", applicant_info_json=""):
    now = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO customer_representatives
                (customer_id, representative_name, applicant_name, applicant_info_json, status, error, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(customer_id) DO UPDATE SET
                representative_name=excluded.representative_name,
                applicant_name=excluded.applicant_name,
                applicant_info_json=excluded.applicant_info_json,
                status=excluded.status,
                error=excluded.error,
                checked_at=excluded.checked_at
            """,
            (customer_id, representative_name, applicant_name, applicant_info_json, status, error, now),
        )
        conn.execute(
            """
            UPDATE call_history
            SET representative_name = ?,
                applicant_name = ?,
                applicant_info_json = ?
            WHERE customer_id = ?
            """,
            (representative_name, applicant_name, applicant_info_json, customer_id),
        )


def sync_known_representatives_to_history():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE call_history
            SET representative_name = (
                SELECT representative_name
                FROM customer_representatives
                WHERE customer_representatives.customer_id = call_history.customer_id
            ),
            applicant_name = (
                SELECT applicant_name
                FROM customer_representatives
                WHERE customer_representatives.customer_id = call_history.customer_id
            ),
            applicant_info_json = (
                SELECT applicant_info_json
                FROM customer_representatives
                WHERE customer_representatives.customer_id = call_history.customer_id
            )
            WHERE customer_id IN (
                SELECT customer_id FROM customer_representatives
            )
              AND (representative_name IS NULL OR representative_name = '')
            """
        )


REPRESENTATIVE_PHONE_BATCH_SIZE = 200


def fetch_customer_export_batch_with_cookies(tels, cookies):
    session = VOSClient.create_session()
    session.cookies.update(_safe_cookies(cookies))
    tmp = VOSClient.__new__(VOSClient)
    tmp.session = session
    tels = [tel for tel in tels if tel]
    try:
        return tels, tmp.customer_data_export_by_tels(tels), ""
    except Exception as exc:
        return tels, [], str(exc)


def fetch_representative_name_with_cookies(customer_id, cookies):
    session = VOSClient.create_session()
    session.cookies.update(_safe_cookies(cookies))
    tmp = VOSClient.__new__(VOSClient)
    tmp.session = session
    try:
        name = tmp.topname(customer_id) or ""
        status = "ok" if name else "empty"
        return customer_id, name, "", "", status, ""
    except Exception as exc:
        return customer_id, "", "", "", "error", str(exc)


def backfill_representatives(vos, customer_ids, workers, log=None, progress_every=50):
    if not customer_ids:
        return 0, 0
    cookies = vos.session.cookies.get_dict()
    phones = customer_phone_map(customer_ids)
    phone_to_customer_ids = defaultdict(list)
    no_phone_customer_ids = []
    for customer_id in customer_ids:
        tel = phones.get(customer_id, "")
        if tel:
            phone_to_customer_ids[tel].append(customer_id)
        else:
            no_phone_customer_ids.append(customer_id)

    named = 0
    errors = 0
    workers = max(1, min(int(workers or 1), 16))
    total_phone_count = len(phone_to_customer_ids)
    phone_batches = list(chunked(list(phone_to_customer_ids), REPRESENTATIVE_PHONE_BATCH_SIZE))
    if log:
        duplicate_skips = max(0, len(customer_ids) - total_phone_count - len(no_phone_customer_ids))
        log(f"[{_now()}] 電話番号を重複排除: 顧客ID={len(customer_ids):,}件 / ユニーク電話番号={total_phone_count:,}件 / 重複スキップ={duplicate_skips:,}件 / 電話番号なし={len(no_phone_customer_ids):,}件")
        log(f"[{_now()}] 顧客データ出力を一括実行: {len(phone_batches):,}回 / 1回最大{REPRESENTATIVE_PHONE_BATCH_SIZE:,}番号 / 並列={workers}")

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(fetch_customer_export_batch_with_cookies, batch, cookies)
            for batch in phone_batches
        ]
        for index, future in enumerate(as_completed(futures), start=1):
            batch_tels, export_rows, error = future.result()
            customer_ids_for_phone = []
            for tel in batch_tels:
                customer_ids_for_phone.extend(phone_to_customer_ids.get(tel, []))
            if error:
                errors += len(customer_ids_for_phone)
            for tel in batch_tels:
                for customer_id in phone_to_customer_ids.get(tel, []):
                    name, applicant_name, applicant_info = extract_customer_export_info(export_rows, customer_id, tel)
                    applicant_info_json = json.dumps(applicant_info, ensure_ascii=False) if applicant_info else ""
                    status = "ok" if (name or applicant_name) else "empty"
                    save_representative_result(customer_id, name, status, f"export:{error}" if error else "", applicant_name, applicant_info_json)
                    if name:
                        named += 1
            if log and (index % 10 == 0 or index == len(phone_batches)):
                checked_phone_count = min(index * REPRESENTATIVE_PHONE_BATCH_SIZE, total_phone_count)
                log(f"[{_now()}] 一括電話番号検索: {index:,}/{len(phone_batches):,}回 ({checked_phone_count:,}/{total_phone_count:,}番号) / 代表者あり={named:,}件 / エラー={errors:,}件")

        fallback_futures = [
            executor.submit(fetch_representative_name_with_cookies, customer_id, cookies)
            for customer_id in no_phone_customer_ids
        ]
        for index, future in enumerate(as_completed(fallback_futures), start=1):
            customer_id, name, applicant_name, applicant_info_json, status, error = future.result()
            save_representative_result(customer_id, name, status, error, applicant_name, applicant_info_json)
            if name:
                named += 1
            if error:
                errors += 1
            if log and (index % progress_every == 0 or index == len(no_phone_customer_ids)):
                log(f"[{_now()}] 電話番号なし顧客ID検索: {index:,}/{len(no_phone_customer_ids):,}件 / 代表者あり={named:,}件 / エラー={errors:,}件")
    return named, errors


def get_latest_call_history_date():
    init_call_history_db()
    with sqlite3.connect(DB_PATH) as conn:
        latest = conn.execute(
            "SELECT MAX(call_start_date) FROM call_history WHERE call_start_date IS NOT NULL AND call_start_date != ''"
        ).fetchone()[0]
    if not latest:
        return None
    try:
        return datetime.strptime(latest, "%Y-%m-%d").date()
    except ValueError:
        return None


def month_ranges(start: date, end: date):
    current = start
    while current <= end:
        if current.month == 12:
            next_month = date(current.year + 1, 1, 1)
        else:
            next_month = date(current.year, current.month + 1, 1)
        chunk_end = min(end, next_month - timedelta(days=1))
        yield current, chunk_end
        current = chunk_end + timedelta(days=1)


def _sheet_range(a1_range):
    safe_name = USER_SHEET_NAME.replace("'", "''")
    return f"'{safe_name}'!{a1_range}"


def get_sheets_service():
    global _sheets_service
    if _sheets_service:
        return _sheets_service

    credentials_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    credentials_json_base64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_BASE64", "").strip()
    credentials_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    if not credentials_json and credentials_json_base64:
        credentials_json = base64.b64decode(credentials_json_base64).decode("utf-8")
    if not credentials_json and not credentials_file:
        raise RuntimeError(
            "Google Sheetsの認証が未設定です。GOOGLE_SERVICE_ACCOUNT_FILE / "
            "GOOGLE_SERVICE_ACCOUNT_JSON / GOOGLE_SERVICE_ACCOUNT_JSON_BASE64 のいずれかを設定して、"
            "対象スプレッドシートをサービスアカウントに共有してください。"
        )

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError(
            "Google Sheets APIライブラリが未インストールです。pip install -r requirements.txt を実行してください。"
        ) from exc

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    if credentials_json:
        info = json.loads(credentials_json)
        credentials = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    else:
        credentials = service_account.Credentials.from_service_account_file(credentials_file, scopes=scopes)

    _sheets_service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
    return _sheets_service


def user_sheet_values_api():
    return get_sheets_service().spreadsheets().values()


def ensure_user_sheet_headers():
    values_api = user_sheet_values_api()
    result = values_api.get(
        spreadsheetId=USER_SHEET_ID,
        range=_sheet_range("A1:E1"),
        valueRenderOption="FORMATTED_VALUE",
    ).execute()
    headers = (result.get("values") or [[]])[0]
    if headers[: len(USER_SHEET_HEADERS)] != USER_SHEET_HEADERS:
        values_api.update(
            spreadsheetId=USER_SHEET_ID,
            range=_sheet_range("A1:E1"),
            valueInputOption="USER_ENTERED",
            body={"values": [USER_SHEET_HEADERS]},
        ).execute()


def _sheet_bool(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().upper() in {"TRUE", "1", "YES", "ON", "有効"}


def _normalize_sheet_row(row, index):
    padded = list(row) + [""] * (len(USER_SHEET_HEADERS) - len(row))
    display_order, name, enabled, created_at, memo = padded[:5]
    if not str(name or "").strip():
        return None
    return {
        "row": index,
        "order": str(display_order or "").strip(),
        "name": str(name or "").strip(),
        "enabled": _sheet_bool(enabled),
        "createdAt": str(created_at or "").strip(),
        "memo": str(memo or "").strip(),
    }


def read_managed_users(include_blanks=False):
    ensure_user_sheet_headers()
    result = user_sheet_values_api().get(
        spreadsheetId=USER_SHEET_ID,
        range=_sheet_range(f"A2:E{USER_SHEET_MAX_ROWS}"),
        valueRenderOption="FORMATTED_VALUE",
    ).execute()
    raw_rows = result.get("values") or []
    users = []
    blank_rows = []
    for offset, row in enumerate(raw_rows, start=2):
        user = _normalize_sheet_row(row, offset)
        if user:
            users.append(user)
        elif include_blanks:
            blank_rows.append(offset)
    return (users, blank_rows) if include_blanks else users


def next_user_order(users):
    nums = []
    for user in users:
        try:
            nums.append(int(float(user.get("order") or 0)))
        except ValueError:
            pass
    return str((max(nums) if nums else 0) + 1)


def update_user_sheet_row(row, values):
    if row < 2 or row > USER_SHEET_MAX_ROWS:
        raise ValueError("更新対象の行が不正です")
    user_sheet_values_api().update(
        spreadsheetId=USER_SHEET_ID,
        range=_sheet_range(f"A{row}:E{row}"),
        valueInputOption="USER_ENTERED",
        body={"values": [values]},
    ).execute()


def clear_user_sheet_row(row):
    if row < 2 or row > USER_SHEET_MAX_ROWS:
        raise ValueError("削除対象の行が不正です")
    user_sheet_values_api().clear(
        spreadsheetId=USER_SHEET_ID,
        range=_sheet_range(f"A{row}:E{row}"),
        body={},
    ).execute()


# ─────────────────────────────────────────────
# ページ
# ─────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/users", methods=["POST"])
def api_users():
    data = request.json or {}
    login_id = data.get("loginId") or DEFAULT_LOGIN_ID
    password = data.get("password") or DEFAULT_PASSWORD
    if not login_id or not password:
        return jsonify({"error": "ログインIDとパスワードを入力してください"}), 400
    try:
        vos = VOSClient(login_id, password)
        users = vos.get_user_options()
        return jsonify({"users": users})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/user-management", methods=["GET"])
def api_user_management_list():
    try:
        users = read_managed_users()
        return jsonify({
            "users": users,
            "sheetId": USER_SHEET_ID,
            "sheetName": USER_SHEET_NAME,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/user-management", methods=["POST"])
def api_user_management_save():
    data = request.json or {}
    name = str(data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "ユーザー名を入力してください"}), 400

    memo = str(data.get("memo") or "").strip()
    enabled = data.get("enabled", True) is True
    row = data.get("row")

    try:
        users, blank_rows = read_managed_users(include_blanks=True)
        row = int(row) if row else None
        duplicate = next((u for u in users if u["name"] == name and u["row"] != row), None)
        if duplicate:
            return jsonify({"error": f"同じユーザー名が既にあります: {name}"}), 400

        if row:
            current = next((u for u in users if u["row"] == row), None)
            order = str(data.get("order") or (current or {}).get("order") or next_user_order(users)).strip()
            created_at = str(data.get("createdAt") or (current or {}).get("createdAt") or date.today().isoformat()).strip()
        else:
            row = blank_rows[0] if blank_rows else max([u["row"] for u in users], default=1) + 1
            order = str(data.get("order") or next_user_order(users)).strip()
            created_at = str(data.get("createdAt") or date.today().isoformat()).strip()

        update_user_sheet_row(row, [order, name, "TRUE" if enabled else "FALSE", created_at, memo])
        users = read_managed_users()
        return jsonify({"ok": True, "row": row, "users": users})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/user-management/<int:row>/status", methods=["PATCH"])
def api_user_management_status(row):
    data = request.json or {}
    enabled = data.get("enabled") is True
    try:
        users = read_managed_users()
        current = next((u for u in users if u["row"] == row), None)
        if not current:
            return jsonify({"error": "対象ユーザーが見つかりません"}), 404
        update_user_sheet_row(
            row,
            [
                current.get("order", ""),
                current.get("name", ""),
                "TRUE" if enabled else "FALSE",
                current.get("createdAt", ""),
                current.get("memo", ""),
            ],
        )
        return jsonify({"ok": True, "users": read_managed_users()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/user-management/<int:row>", methods=["DELETE"])
def api_user_management_delete(row):
    try:
        users = read_managed_users()
        if not any(u["row"] == row for u in users):
            return jsonify({"error": "対象ユーザーが見つかりません"}), 404
        clear_user_sheet_row(row)
        return jsonify({"ok": True, "users": read_managed_users()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────
# API: 見込み一括更新
# ─────────────────────────────────────────────
@app.route("/api/update", methods=["POST"])
def api_update():
    data = request.json or {}
    tel_list = [
        t.strip().replace("-", "").replace(" ", "").replace("\u3000", "")
        for t in (data.get("telList") or "").splitlines()
        if t.strip()
    ]
    if not tel_list:
        return jsonify({"error": "電話番号が入力されていません"}), 400

    call_date        = data.get("callDate", "")
    call_time        = data.get("callTime", "09:00")
    rank             = data.get("rank", "D")
    login_id         = data.get("loginId") or DEFAULT_LOGIN_ID
    password         = data.get("password") or DEFAULT_PASSWORD
    login_user_id    = data.get("loginUserId", "381")
    second_user_id   = data.get("secondUserId", "443")
    check_carrier    = data.get("checkCarrier", True) is True
    if not login_id or not password:
        return jsonify({"error": "ログインIDとパスワードを入力してください"}), 400

    jid = new_job()

    def run():
        def log(msg):
            job_log(jid, msg)

        try:
            log(f"[{_now()}] ログイン中... ({login_id})")
            vos = VOSClient(login_id, password)
            cookies = vos.session.cookies.get_dict()
            log(f"[{_now()}] ログイン成功")
            log(f"[{_now()}] 開始: {len(tel_list)}件  日付={call_date}  時間={call_time}  ランク={rank}")
            log(f"[{_now()}] 発番確認: {'ON（ＮＴＴ東日本のみ更新）' if check_carrier else 'OFF'}")
            ok = ng = 0

            def process_one(tel):
                if check_carrier:
                    try:
                        carrier_result = carrier_lookup.lookup(tel)
                    except Exception as exc:
                        return tel, None, None, "", (), str(exc)
                    if carrier_result.error:
                        return tel, None, None, "", (), carrier_result.error
                    if "ＮＴＴ東日本" not in carrier_result.carriers:
                        return tel, None, None, "", carrier_result.carriers, None

                s = VOSClient.create_session()
                s.cookies.update(_safe_cookies(cookies))
                tmp = VOSClient.__new__(VOSClient)
                tmp.session = s
                results = tmp.get_id_by_tel(tel)
                if not results:
                    return tel, None, None, "", ("ＮＴＴ東日本",) if check_carrier else (), None
                first = results[0]
                cid, flag = first["id"], first["flag"]
                if flag == "見込み":
                    return tel, cid, None, flag, ("ＮＴＴ東日本",) if check_carrier else (), None
                status = tmp.update_next(cid, call_date, call_time, rank,
                                        login_user_id=login_user_id,
                                        cust_second_user_id=second_user_id)
                return tel, cid, status, flag, ("ＮＴＴ東日本",) if check_carrier else (), None

            for position, tel in enumerate(tel_list, start=1):
                log(f"[{_now()}] [{position}/{len(tel_list)}] 処理中  tel={tel}")
                try:
                    tel, cid, status, flag, carriers, carrier_error = process_one(tel)
                    carrier_name = " / ".join(carriers) if carriers else "判定なし"

                    if carrier_error:
                        log(f"[{_now()}] ⏭ 発番判定エラーのためスキップ  tel={tel}  理由={carrier_error}")
                        ng += 1
                    elif check_carrier and "ＮＴＴ東日本" not in carriers:
                        log(f"[{_now()}] ⏭ 発番={carrier_name} のためスキップ  tel={tel}")
                        ng += 1
                    elif cid is None:
                        log(f"[{_now()}] ⚠ 発番={carrier_name} / 顧客なし  tel={tel}")
                        ng += 1
                    elif flag == "見込み":
                        log(f"[{_now()}] ⏭ スキップ（見込み）  tel={tel} → id={cid}")
                        ng += 1
                    elif status in (200, 302):
                        carrier_log = f"  発番={carrier_name}" if check_carrier else ""
                        log(f"[{_now()}] ✓ OK  tel={tel} → id={cid}  ({status}){carrier_log}")
                        ok += 1
                    else:
                        log(f"[{_now()}] ✗ NG  tel={tel} → id={cid}  ({status})")
                        ng += 1
                except Exception as e:
                    log(f"[{_now()}] ✗ ERR  tel={tel}  {e}")
                    ng += 1

            jobs[jid]["ok"] = ok
            jobs[jid]["ng"] = ng
            log(f"[{_now()}] ━━ 完了: 成功={ok}件 / 失敗={ng}件 ━━")
        except Exception as e:
            log(f"[{_now()}] ✗ エラー: {e}")
        finally:
            jobs[jid]["done"] = True

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"jobId": jid})


# ─────────────────────────────────────────────
# API: 通話履歴ダウンロード
# ─────────────────────────────────────────────
@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.json or {}
    login_id   = data.get("loginId") or DEFAULT_LOGIN_ID
    password   = data.get("password") or DEFAULT_PASSWORD
    start_date = data.get("startDate", "")
    end_date   = data.get("endDate", "")
    in_out     = data.get("inOutFlag", "2")
    user_filter = data.get("userFilter", "")
    keyword_filter = data.get("keywordFilter", "")
    hour_filter = data.get("hourFilter", "")
    phone_field = data.get("phoneField", "receiver_number")
    try:
        min_seconds = int(data.get("minSeconds")) if str(data.get("minSeconds", "")).strip() else None
    except ValueError:
        return jsonify({"error": "最小通話秒数は数値で入力してください"}), 400
    if not login_id or not password:
        return jsonify({"error": "ログインIDとパスワードを入力してください"}), 400

    jid = new_job()

    def run():
        def log(msg):
            job_log(jid, msg)

        try:
            log(f"[{_now()}] ログイン中... ({login_id})")
            vos = VOSClient(login_id, password)
            log(f"[{_now()}] ダウンロード中...  {start_date} ～ {end_date}")
            result = vos.data(
                starttime=start_date,
                endtime=end_date,
                in_out_flag=in_out,
                output_file="call_log.csv",
            )
            if result:
                log(f"[{_now()}] ✓ call_log.csv 保存完了")
                rows = parse_call_log_csv("call_log.csv")
                filters = {
                    "user": user_filter,
                    "keyword": keyword_filter,
                    "min_seconds": min_seconds,
                    "hour": hour_filter,
                }
                filtered_rows = [row for row in rows if row_matches_download_filters(row, filters)]
                log(f"[{_now()}] 絞り込み後: {len(filtered_rows):,}件")

                customer_ids = [row.get("customer_id") for row in filtered_rows]
                missing_ids = uncached_representative_ids(customer_ids)
                if missing_ids:
                    log(f"[{_now()}] 未確認の顧客IDを代表者確認: {len(missing_ids):,}件")
                    backfill_representatives(vos, missing_ids, 8, log=log)
                representative_by_id = representative_name_map(customer_ids)

                representative_rows = []
                phone_cache = {}
                for index, row in enumerate(filtered_rows, start=1):
                    customer_id = row.get("customer_id", "")
                    cached_name = representative_by_id.get(customer_id, "")
                    if cached_name:
                        row["representative_name"] = cached_name
                        representative_rows.append((row, cached_name))
                        continue

                    tel = phone_for_vos(row.get(phone_field) or row.get("receiver_number"))
                    if not tel:
                        continue
                    if tel not in phone_cache:
                        representative_name = ""
                        try:
                            results = vos.get_id_by_tel(tel)
                            for result_item in results:
                                cid = result_item.get("id")
                                if not cid:
                                    continue
                                name = vos.topname(cid)
                                if name:
                                    representative_name = name
                                    save_representative_result(cid, name, "ok", "")
                                    break
                        except Exception as exc:
                            log(f"[{_now()}] VOS検索エラー tel={tel}: {exc}")
                        phone_cache[tel] = representative_name

                    if phone_cache[tel]:
                        representative_rows.append((row, phone_cache[tel]))
                    if index % 20 == 0:
                        log(f"[{_now()}] VOS検索中: {index:,}/{len(filtered_rows):,}件 / 代表者あり={len(representative_rows):,}件")

                prefix = f"call_history_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                all_path, zip_path, hour_paths = write_representative_outputs(representative_rows, prefix)
                jobs[jid]["ok"] = len(representative_rows)
                jobs[jid]["ng"] = len(filtered_rows) - len(representative_rows)
                jobs[jid]["downloadCsv"] = os.path.basename(all_path)
                jobs[jid]["downloadZip"] = os.path.basename(zip_path)
                log(f"[{_now()}] ✓ 代表者名あり: {len(representative_rows):,}件")
                log(f"[{_now()}] ✓ 時間別ファイル: {len(hour_paths):,}個")
            else:
                log(f"[{_now()}] ✗ ダウンロード失敗")
                jobs[jid]["ng"] = 1
        except Exception as e:
            log(f"[{_now()}] ✗ エラー: {e}")
        finally:
            jobs[jid]["done"] = True

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"jobId": jid})


# ─────────────────────────────────────────────
# API: CSVマージ
# ─────────────────────────────────────────────
@app.route("/api/merge", methods=["POST"])
def api_merge():
    data = request.json or {}
    login_id = data.get("loginId") or DEFAULT_LOGIN_ID
    password = data.get("password") or DEFAULT_PASSWORD
    if not login_id or not password:
        return jsonify({"error": "ログインIDとパスワードを入力してください"}), 400

    jid = new_job()

    def run():
        def log(msg):
            job_log(jid, msg)

        try:
            log(f"[{_now()}] ログイン中... ({login_id})")
            vos = VOSClient(login_id, password)
            log(f"[{_now()}] CSVマージ開始...")
            vos.merge_to_cleaned_split()
            log(f"[{_now()}] ✓ cleaned_split.csv 更新完了")
            jobs[jid]["ok"] = 1
        except Exception as e:
            log(f"[{_now()}] ✗ エラー: {e}")
        finally:
            jobs[jid]["done"] = True

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"jobId": jid})


# ─────────────────────────────────────────────
# API: 全通話履歴DB同期
# ─────────────────────────────────────────────
@app.route("/api/history/sync", methods=["POST"])
def api_history_sync():
    data = request.json or {}
    login_id = data.get("loginId") or DEFAULT_LOGIN_ID
    password = data.get("password") or DEFAULT_PASSWORD
    start_date = data.get("startDate") or "2022-01-01"
    end_date = data.get("endDate") or date.today().isoformat()
    in_out = data.get("inOutFlag", "")
    incremental = data.get("incremental", True) is True
    representative_workers = int(data.get("representativeWorkers") or 6)
    if not login_id or not password:
        return jsonify({"error": "ログインIDとパスワードを入力してください"}), 400

    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "日付は YYYY-MM-DD で入力してください"}), 400
    if incremental:
        latest = get_latest_call_history_date()
        if latest:
            start = latest
    if start > end:
        return jsonify({"error": "開始日は終了日以前にしてください"}), 400

    jid = new_job()

    def run():
        def log(msg):
            job_log(jid, msg)

        total_added = 0
        total_skipped = 0
        temp_paths = []
        try:
            init_call_history_db()
            log(f"[{_now()}] ログイン中... ({login_id})")
            vos = VOSClient(login_id, password)
            if incremental:
                log(f"[{_now()}] 未取得分だけ更新: {start.isoformat()} ～ {end.isoformat()}（最大1か月ずつ）")
            else:
                log(f"[{_now()}] 全履歴同期開始: {start.isoformat()} ～ {end.isoformat()}（最大1か月ずつ）")

            for chunk_start, chunk_end in month_ranges(start, end):
                fd, temp_path = tempfile.mkstemp(prefix="vos_call_log_", suffix=".csv")
                os.close(fd)
                temp_paths.append(temp_path)
                log(f"[{_now()}] 取得中: {chunk_start.isoformat()} ～ {chunk_end.isoformat()}")
                result = vos.data(
                    starttime=chunk_start.isoformat(),
                    endtime=chunk_end.isoformat(),
                    in_out_flag=in_out,
                    output_file=temp_path,
                )
                if not result:
                    log(f"[{_now()}] 取得失敗: {chunk_start.isoformat()} ～ {chunk_end.isoformat()}")
                    continue

                rows = parse_call_log_csv(temp_path)
                added, skipped = insert_call_history_rows(rows)
                total_added += added
                total_skipped += skipped
                log(f"[{_now()}] DB投入: 追加={added:,}件 / 重複={skipped:,}件")
                missing_ids = uncached_representative_ids(row.get("customer_id") for row in rows)
                if missing_ids:
                    log(f"[{_now()}] 新規顧客IDの代表者確認: {len(missing_ids):,}件")
                    backfill_representatives(vos, missing_ids, representative_workers, log=log)

            jobs[jid]["ok"] = total_added
            jobs[jid]["ng"] = total_skipped
            log(f"[{_now()}] ━━ 完了: 新規追加={total_added:,}件 / 重複スキップ={total_skipped:,}件 ━━")
        except Exception as e:
            jobs[jid]["ng"] = total_skipped
            log(f"[{_now()}] ✗ エラー: {e}")
        finally:
            for temp_path in temp_paths:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            jobs[jid]["done"] = True

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"jobId": jid})


@app.route("/api/history")
def api_history():
    init_call_history_db()
    q = (request.args.get("q") or "").strip()
    page = max(int(request.args.get("page", 1) or 1), 1)
    limit = min(max(int(request.args.get("limit", 50) or 50), 10), 200)
    offset = (page - 1) * limit

    where = ""
    params = []
    if q:
        like = f"%{q}%"
        where = """
            WHERE log_id LIKE ?
               OR caller_number LIKE ?
               OR receiver_number LIKE ?
               OR customer_id LIKE ?
               OR customer_name LIKE ?
               OR representative_name LIKE ?
               OR applicant_name LIKE ?
               OR user_name LIKE ?
               OR project_list LIKE ?
               OR call_flag LIKE ?
        """
        params = [like] * 10

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        total = conn.execute(f"SELECT COUNT(*) FROM call_history {where}", params).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT {",".join(CALL_HISTORY_COLUMNS)}
            FROM call_history
            {where}
            ORDER BY call_start_date DESC, call_start_time DESC, log_id DESC
            LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        ).fetchall()

    return jsonify({
        "columns": [{"key": key, "label": CALL_HISTORY_LABELS[key]} for key in CALL_HISTORY_COLUMNS],
        "rows": [dict(row) for row in rows],
        "total": total,
        "page": page,
        "limit": limit,
    })


@app.route("/api/history/stats")
def api_history_stats():
    init_call_history_db()
    with sqlite3.connect(DB_PATH) as conn:
        total = conn.execute("SELECT COUNT(*) FROM call_history").fetchone()[0]
        span = conn.execute(
            "SELECT MIN(call_start_date), MAX(call_start_date) FROM call_history"
        ).fetchone()
    return jsonify({
        "total": total,
        "minDate": span[0],
        "maxDate": span[1],
        "dbPath": DB_PATH,
    })


@app.route("/api/history/chart")
def api_history_chart():
    init_call_history_db()
    with sqlite3.connect(DB_PATH) as conn:
        span = conn.execute(
            "SELECT MIN(call_start_date), MAX(call_start_date) FROM call_history"
        ).fetchone()

        if not span[1]:
            return jsonify({"points": []})

        end_text = request.args.get("end") or span[1]
        try:
            end = datetime.strptime(end_text, "%Y-%m-%d").date()
        except ValueError:
            end = datetime.strptime(span[1], "%Y-%m-%d").date()

        start_text = request.args.get("start")
        if start_text:
            try:
                start = datetime.strptime(start_text, "%Y-%m-%d").date()
            except ValueError:
                start = end - timedelta(days=13)
        else:
            start = end - timedelta(days=13)

        rows = conn.execute(
            """
            SELECT call_start_date, COUNT(*)
            FROM call_history
            WHERE call_start_date BETWEEN ? AND ?
            GROUP BY call_start_date
            ORDER BY call_start_date
            """,
            (start.isoformat(), end.isoformat()),
        ).fetchall()

    counts = {row[0]: row[1] for row in rows}
    points = []
    current = start
    while current <= end:
        key = current.isoformat()
        points.append({"date": key, "count": counts.get(key, 0)})
        current += timedelta(days=1)
    return jsonify({"points": points})


@app.route("/api/representatives/stats")
def api_representatives_stats():
    return jsonify(representative_stats())


@app.route("/api/representatives/backfill", methods=["POST"])
def api_representatives_backfill():
    data = request.json or {}
    login_id = data.get("loginId") or DEFAULT_LOGIN_ID
    password = data.get("password") or DEFAULT_PASSWORD
    if not login_id or not password:
        return jsonify({"error": "ログインIDとパスワードを入力してください"}), 400
    try:
        workers = max(1, min(int(data.get("workers") or 8), 16))
        limit = int(data.get("limit")) if str(data.get("limit", "")).strip() else None
    except ValueError:
        return jsonify({"error": "並列数と上限件数は数値で入力してください"}), 400

    jid = new_job()

    def run():
        def log(msg):
            job_log(jid, msg)

        try:
            init_call_history_db()
            stats = representative_stats()
            log(f"[{_now()}] 顧客ID={stats['totalIds']:,}件 / 確認済み={stats['checkedIds']:,}件 / 未確認={stats['remainingIds']:,}件")
            ids = missing_representative_ids(limit=limit)
            if not ids:
                log(f"[{_now()}] 未確認の顧客IDはありません")
                jobs[jid]["ok"] = 0
                return
            log(f"[{_now()}] ログイン中... ({login_id})")
            vos = VOSClient(login_id, password)
            log(f"[{_now()}] 代表者名一括確認開始: {len(ids):,}件 / 並列={workers}")
            named, errors = backfill_representatives(vos, ids, workers, log=log)
            jobs[jid]["ok"] = named
            jobs[jid]["ng"] = errors
            stats_after = representative_stats()
            log(f"[{_now()}] ━━ 完了: 代表者あり={named:,}件 / エラー={errors:,}件 / 残り={stats_after['remainingIds']:,}件 ━━")
        except Exception as e:
            log(f"[{_now()}] ✗ エラー: {e}")
        finally:
            jobs[jid]["done"] = True

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"jobId": jid})


# ─────────────────────────────────────────────
# API: ジョブ状態ポーリング
# ─────────────────────────────────────────────
@app.route("/api/job/<jid>")
def api_job(jid):
    job = jobs.get(jid)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify(job)


# ─────────────────────────────────────────────
# CSV ダウンロード
# ─────────────────────────────────────────────
@app.route("/api/csv/<filename>")
def api_csv(filename):
    import os
    safe = {"call_log.csv", "cleaned_split.csv"}
    if filename not in safe:
        return "Not found", 404
    path = os.path.join(os.path.dirname(__file__), filename)
    if not os.path.exists(path):
        return "ファイルがありません", 404
    return send_file(path, as_attachment=True, download_name=filename)


@app.route("/api/download-output/<filename>")
def api_download_output(filename):
    safe_name = os.path.basename(filename)
    path = os.path.join(DOWNLOAD_OUTPUT_DIR, safe_name)
    if safe_name != filename or not os.path.exists(path):
        return "Not found", 404
    return send_file(path, as_attachment=True, download_name=safe_name)


# ─────────────────────────────────────────────
def _now():
    return datetime.now().strftime("%H:%M:%S")


if __name__ == "__main__":
    app.run(debug=True, port=5000, use_reloader=False)
