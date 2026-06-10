import threading
import uuid
import json
import urllib.parse
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_file
import io

from carrier_lookup import FixedLineCarrierLookup
from vos import VOSClient


carrier_lookup = FixedLineCarrierLookup()


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


def new_job() -> str:
    jid = str(uuid.uuid4())
    jobs[jid] = {"logs": [], "done": False, "ok": 0, "ng": 0}
    return jid


def job_log(jid: str, msg: str):
    jobs[jid]["logs"].append(msg)


# ─────────────────────────────────────────────
# ページ
# ─────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


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
    login_id         = data.get("loginId", "SDBDY07009")
    password         = data.get("password", "buddy1999")
    login_user_id    = data.get("loginUserId", "381")
    second_user_id   = data.get("secondUserId", "443")
    check_carrier    = data.get("checkCarrier", True) is True

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
    login_id   = data.get("loginId", "SDBDY07009")
    password   = data.get("password", "buddy1999")
    start_date = data.get("startDate", "")
    end_date   = data.get("endDate", "")
    in_out     = data.get("inOutFlag", "2")

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
                jobs[jid]["ok"] = 1
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
    login_id = data.get("loginId", "SDBDY07009")
    password = data.get("password", "buddy1999")

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


# ─────────────────────────────────────────────
def _now():
    return datetime.now().strftime("%H:%M:%S")


if __name__ == "__main__":
    app.run(debug=True, port=5000, use_reloader=False)
