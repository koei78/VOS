import csv
import os
import requests

from bs4 import BeautifulSoup
import re


class VOSClient:

    BASE = "https://line005.socio-vos.com"
    LOGIN_URL = f"{BASE}/vos/login/"
    SEARCH_PAGE = f"{BASE}/vos/customComplexSearch/"
    SEARCH_POST = f"{BASE}/vos/customComplexSearch/select/"

    @staticmethod
    def create_session():
        """VOS向けセッションを作成する。

        既定では環境変数のHTTP(S)プロキシを使わない。
        必要な場合のみ VOS_USE_ENV_PROXY=1 で有効化する。
        """
        session = requests.Session()
        use_env_proxy = os.getenv("VOS_USE_ENV_PROXY", "0").lower() in {"1", "true", "yes", "on"}
        if not use_env_proxy:
            session.trust_env = False
            session.proxies.update({"http": None, "https": None})
        return session

    def __init__(self, login_id="SDBDY07009", password="buddy1999"):

        self.session = self.create_session()

        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

        try:
            r0 = self.session.get(self.LOGIN_URL, headers={"User-Agent": ua})
            print("GET login:", r0.status_code, "cookies:", self.session.cookies.get_dict())

            payload = {
                "userLoginId": login_id,
                "userPassword": password,
                "applePcMode": "yes",
                "goOnLogin": "はい",
            }

            r1 = self.session.post(
                self.LOGIN_URL,
                data=payload,
                headers={
                    "User-Agent": ua,
                    "Origin": self.BASE,
                    "Referer": self.LOGIN_URL,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                allow_redirects=True,
            )
        except requests.exceptions.ProxyError as e:
            raise RuntimeError(
                "プロキシ経由でVOSに接続できませんでした。"
                "既定では環境プロキシを無効化しています。"
                "プロキシが必要な環境では環境変数 VOS_USE_ENV_PROXY=1 を設定してください。"
            ) from e

        print("POST login:", r1.status_code)
        print("final url:", r1.url)
    def update_next(self, cust_base_id, call_date, call_time, rank,
                    login_user_id="381", login_group_id="7",
                    cust_second_user_id="381", cust_second_group_id="7"):
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0"

        # ① 編集ページをGETして既存フォーム値を全取得
        edit_url = f"{self.BASE}/vos//custom/edit/{cust_base_id}"
        r = self.session.get(edit_url, params={"backURI": "prospectManagement"},
                             headers={"User-Agent": ua})
        soup = BeautifulSoup(r.text, "html.parser")

        # フォームの全input/select/textareaを収集
        form = []
        skip_names = {"custNextCallDateTime", "custNextCallTime", "custNextCallTime1",
                      "custBaseStatus", "custBaseSituationRank",
                      "loginUserId", "loginGroupId",
                      "custSecondGroupId", "custSecondUserId",
                      "callLog", "callPartnerId", "memo",
                      "createCallLogShow", "latestCallLogShow", "nothingCallLogShow",
                      "maxCallLogUniqueid", "callLogFlag1", "callLogPage"}

        seen = set()
        for tag in soup.find_all(["input", "select", "textarea"]):
            name = tag.get("name")
            if not name or name in skip_names:
                continue
            if tag.name == "input":
                t = tag.get("type", "text").lower()
                if t in ("submit", "button", "image", "reset"):
                    continue
                if t == "checkbox":
                    if tag.get("checked"):
                        form.append((name, tag.get("value", "on")))
                    continue
                if t == "radio":
                    if tag.get("checked"):
                        form.append((name, tag.get("value", "")))
                    continue
                form.append((name, tag.get("value", "")))
            elif tag.name == "select":
                selected = tag.find("option", selected=True)
                form.append((name, selected["value"] if selected else ""))
            elif tag.name == "textarea":
                form.append((name, tag.get_text()))

        # ② 更新したいフィールドを上書き追加
        form += [
            ("custBaseStatus",       "23"),
            ("custBaseSituationRank", rank),
            ("createCallLogShow",    "true"),
            ("latestCallLogShow",    "false"),
            ("nothingCallLogShow",   "false"),
            ("maxCallLogUniqueid",   ""),
            ("callLogFlag1",         "4"),
            ("callLogPage",          ""),
            ("loginUserId",          login_user_id),
            ("loginGroupId",         login_group_id),
            ("custSecondGroupId",    cust_second_group_id),
            ("custSecondUserId",     cust_second_user_id),
            ("callLog",              "1"),
            ("callPartnerId",        "2"),
            ("memo",                 ""),
            ("custNextCallDateTime", call_date),
            ("custNextCallTime",     call_time),
            ("custNextCallDateTime", call_date),   # サーバーが読む2回目
            ("custNextCallTime1",    call_time),
        ]

        resp = self.session.post(
            f"{self.BASE}/vos//custom/updateNext",
            params={"listId": "", "custBaseStatusVal": "23"},
            data=form,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ja,en;q=0.9",
                "Origin": self.BASE,
                "Referer": edit_url,
                "User-Agent": ua,
                "Upgrade-Insecure-Requests": "1",
            },
            timeout=30,
            allow_redirects=True,
        )
        return resp.status_code

    def topname(self, id):
        url = f"https://line005.socio-vos.com/vos/custom/edit/{id}"
        params = {
            "backURI": "prospectManagement"
        }


        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36 Edg/145.0.0.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja,en;q=0.9",
        }

        r = self.session.get(url, params=params, headers=headers)
        with open("response.html", "w", encoding="utf-8") as f:
            f.write(r.text)

        print("HTML保存完了")
        with open("response.html", "r", encoding="utf-8") as f:
            html = f.read()

        soup = BeautifulSoup(html, "html.parser")

        # idで取得（最も安全）
        input_tag = soup.find("input", id="custBaseApplyName")

        if input_tag:
            value = input_tag.get("value")
            print("取得成功:", value)
            return value            
        else:
            print("見つからない")
    def data(self, starttime="2026-03-01", endtime="2026-03-31",
             group_id="7", in_out_flag="2",
             output_file="call_log.csv"):
        """
        通話履歴CSVをダウンロードして保存する

        Parameters
        ----------
        starttime    : 取得開始日 (YYYY-MM-DD)
        endtime      : 取得終了日 (YYYY-MM-DD)
        group_id     : グループID
        in_out_flag  : 発着信フラグ (2=発信, 1=着信, ""=両方)
        jsessionid   : セッションID（ブラウザのCookieからコピー）
        output_file  : 保存するCSVファイル名
        """

        url = "https://line005.socio-vos.com/vos//callLog/csvDownload"

        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "ja,en;q=0.9,en-GB;q=0.8,en-US;q=0.7",
            "Cache-Control": "max-age=0",
            "Connection": "keep-alive",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://line005.socio-vos.com",
            "Referer": "https://line005.socio-vos.com/vos//callLog/select/",
            "Sec-Fetch-Dest": "frame",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Upgrade-Insecure-Requests": "1",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0",
        }

        payload = {
            "backURI": "callLog",
            "historyURI": "",
            "encPassword": "buddy0708",
            "starttime": starttime,
            "beginHour": "",
            "beginMinute": "",
            "endtime": endtime,
            "endHour": "",
            "endMinute": "",
            "groupId": group_id,
            "userId": "",
            "telNum": "",
            "inOutFlag": in_out_flag,
            "callLogFlag1": "",
            "callLogFlag2": "",
            "callLogFlag3": "",
            "callPartnerId": "",
            "notViewAddedLog": "1",
            "accountcode": "",
            "custBaseListId": "",
            "curentPage": "1",
            "movePage": "",
        }

        r = self.session.post(url, headers=headers, data=payload)
        print(f"ステータス: {r.status_code}")

        if r.status_code == 200 and len(r.content) > 0:
            # HTMLが返ってきた場合はエラー（ログイン失敗 or 不正なパラメータ）
            if r.content.lstrip()[:5].lower() in (b'<?xml', b'<!doc', b'<html'):
                print("[エラー] CSVではなくHTMLが返されました（ログイン失敗または日付が不正の可能性）")
                print(r.text[r.text.find('<title'):r.text.find('<title')+200] or r.text[:300])
                return None
            # ZIPの場合は中のCSVを取り出す（パスワード付き）
            if r.content[:2] == b'PK':
                import zipfile, io
                password = payload["encPassword"].encode()
                with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                    csv_name = next(n for n in z.namelist() if n.endswith('.csv'))
                    with z.open(csv_name, pwd=password) as zf, open(output_file, 'wb') as out:
                        out.write(zf.read())
                print(f"ZIP解凍・CSV保存完了: {output_file}")
                return True
            else:
                with open(output_file, "wb") as f:
                    f.write(r.content)
                print(f"CSV保存完了: {output_file} ({len(r.content):,} bytes)")
                return True
        else:
            print("取得失敗またはレスポンスが空です")
            print(r.text[:500])
            return None

    def merge_to_cleaned_split(self, call_log_file="call_log.csv",
                                cleaned_split_file="cleaned_split.csv"):
        """
        call_log.csv の内容を cleaned_split.csv の形式に変換して追記する。
        既に存在するログID（列0）は追加しない。
        """

        def to_float_str(s):
            if not s:
                return ''
            try:
                return str(float(s))
            except ValueError:
                return s

        def split_datetime(dt_str):
            # '2026-03-18  17:41:06' → ('2026-03-18', ' 17:41:06')
            # '2026-03-18 17:41:06'  → ('2026-03-18', '17:41:06')
            if not dt_str:
                return '', ''
            parts = dt_str.split(' ', 1)
            return (parts[0], parts[1]) if len(parts) == 2 else (dt_str, '')

        # 既存の全列をタプルとして収集（ファイルがなければ空）
        existing_keys = set()
        if os.path.exists(cleaned_split_file):
            with open(cleaned_split_file, 'r', encoding='utf-8-sig') as f:
                reader = csv.reader(f)
                for row in reader:
                    if row:
                        existing_keys.add(tuple(row))
        print(f"既存レコード数: {len(existing_keys):,}")

        # call_log.csv を読み込み変換して追記
        # エンコード自動判定（utf-8-sig → cp932 の順で試す）
        encoding = 'utf-8-sig'
        try:
            with open(call_log_file, encoding='utf-8-sig') as f:
                f.read()
        except UnicodeDecodeError:
            encoding = 'cp932'
        print(f"call_log エンコード: {encoding}")

        added = 0
        skipped = 0
        with open(call_log_file, 'r', encoding=encoding) as f_in, \
             open(cleaned_split_file, 'a', encoding='utf-8-sig', newline='') as f_out:

            reader = csv.reader(f_in)
            writer = csv.writer(f_out)

            # 空行を飛ばしてヘッダーを探す
            for row in reader:
                if row:
                    break  # ヘッダー行を読み飛ばした

            for row in reader:
                if not row or len(row) < 15:
                    continue
                log_id = row[0]
                tel_start_date, tel_start_time = split_datetime(row[1])
                call_start_date, call_start_time = split_datetime(row[2])
                call_end_date, call_end_time = split_datetime(row[3])

                def clean(s):
                    return s.replace('\r\n', ' ').replace('\r', ' ').replace('\n', ' ').strip()

                new_row = [
                    log_id,                          # [0]  ログID
                    tel_start_date,                  # [1]  電話開始日
                    tel_start_time,                  # [2]  電話開始時刻
                    call_start_date,                 # [3]  通話開始日
                    call_start_time,                 # [4]  通話開始時刻
                    call_end_date,                   # [5]  通話終了日
                    call_end_time,                   # [6]  通話終了時刻
                    to_float_str(row[4]),            # [7]  発信者番号
                    to_float_str(row[5]),            # [8]  受信者番号
                    row[6],                          # [9]  電話時間（秒）
                    to_float_str(row[7]),            # [10] 代表番号
                    clean(row[8]),                   # [11] ユーザー
                    row[9],                          # [12] 顧客ID
                    clean(row[10]),                  # [13] プロジェクトリスト
                    clean(row[11]),                  # [14] 顧客名
                    row[12],                         # [15] タイプ
                    clean(row[13]),                  # [16] 通話フラグ
                    clean(row[14]),                  # [17] 通話履歴
                ]
                key = tuple(new_row)
                if key in existing_keys:
                    skipped += 1
                    continue

                writer.writerow(new_row)
                existing_keys.add(key)
                added += 1

        print(f"追加: {added:,} 件 / スキップ（重複）: {skipped:,} 件")
        print(f"cleaned_split.csv に追記完了")

    # ======================
    # selectだけ何度でも取得
    # ======================
    def select(self, tel):
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0"

        # 検索ページをGETしてからPOST
        self.session.get(self.SEARCH_PAGE, headers={"User-Agent": ua})

        search_payload = {
            "encPassword": "",
            "isOnlyContractInfo": "",
            "lastCalledId": "",
            "firstOnload": "false",
            "formatId": "0",
            "nonFormatConfirm": "false",
            "defaultFormatId": "",
            "custTelNum": tel,
            "custBaseName1": "",
            "custBaseKana1": "",
            "listName": "",
            "flag_btn_cust_base": "0",
            "multipleGroupId": "",
            "custBaseZipCode": "",
            "custBaseInvoiceNo": "",
            "custBaseAddr2": "",
            "custBaseAddrKana": "",
            "custBaseProvideClassify": "",
            "custBasePhoneBook": "",
            "custBaseJudge": "",
            "custBaseDepartId": "7",
            "custBaseLastCallLogDepartId": "7",
            "projectUserGroupId": "7",
            "pagingAction": "customComplexSearch",
            "sreachOptions": "01",
            "sreachOptionsList": "01",
        }

        res = self.session.post(
            self.SEARCH_POST,
            data=search_payload,
            headers={
                "User-Agent": ua,
                "Origin": self.BASE,
                "Referer": self.SEARCH_PAGE,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )

        return res.text

    def get_id_by_tel(self, tel):
        """電話番号→顧客ID一覧を返す（見つからなければ空リスト）"""
        html = self.select(tel)
        # デバッグ用：検索結果HTMLを保存
        with open("debug_search.html", "w", encoding="utf-8") as f:
            f.write(html)
        soup = BeautifulSoup(html, "html.parser")
        ids = []
        for cb in soup.find_all("input", {"name": "chk_id"}):
            val = cb.get("value", "")
            if val and val not in ids:
                ids.append(val)
        print(f"tel={tel} → ids={ids}  (HTML長={len(html)})")
        return ids

    # ===== GUI =====
    @staticmethod
    def run_gui():
        import tkinter as tk
        from tkinter import ttk, scrolledtext, messagebox
        import threading
        import time
        from datetime import datetime

        WAIT_SEC = 1.5
        root = tk.Tk()
        root.title("見込み一括更新ツール")
        root.resizable(False, False)
        root.configure(bg="#f0f4f8")

        # --- UI構築 ---
        tk.Label(root, text="🎯 見込み一括更新ツール", font=("", 14, "bold"), bg="#f0f4f8").pack(padx=12, pady=6, anchor="w")

        frm = tk.LabelFrame(root, text="設定", bg="#f0f4f8", font=("", 9))
        frm.pack(fill="x", padx=12, pady=4)

        today = datetime.today()
        year_var  = tk.StringVar(value=str(today.year))
        month_var = tk.StringVar(value=f"{today.month:02d}")
        day_var   = tk.StringVar(value=f"{today.day:02d}")
        tk.Label(frm, text="📅 次回コール日", bg="#f0f4f8").grid(row=0, column=0, sticky="w", padx=8, pady=4)
        date_frm = tk.Frame(frm, bg="#f0f4f8")
        date_frm.grid(row=0, column=1, sticky="w", padx=4, pady=4)
        ttk.Combobox(date_frm, textvariable=year_var,  values=[str(y) for y in range(today.year, today.year+3)], width=6,  state="readonly").pack(side="left")
        tk.Label(date_frm, text="年", bg="#f0f4f8").pack(side="left")
        ttk.Combobox(date_frm, textvariable=month_var, values=[f"{m:02d}" for m in range(1,13)], width=4, state="readonly").pack(side="left")
        tk.Label(date_frm, text="月", bg="#f0f4f8").pack(side="left")
        ttk.Combobox(date_frm, textvariable=day_var,   values=[f"{d:02d}" for d in range(1,32)], width=4, state="readonly").pack(side="left")
        tk.Label(date_frm, text="日", bg="#f0f4f8").pack(side="left")

        time_var = tk.StringVar(value="09:00")
        tk.Label(frm, text="🕐 時間", bg="#f0f4f8").grid(row=1, column=0, sticky="w", padx=8, pady=4)
        ttk.Combobox(frm, textvariable=time_var, values=[f"{h:02d}:{m:02d}" for h in range(8,21) for m in (0,30)], width=8, state="readonly").grid(row=1, column=1, sticky="w", padx=4, pady=4)

        rank_var = tk.StringVar(value="D")
        tk.Label(frm, text="🏷️ ランク", bg="#f0f4f8").grid(row=2, column=0, sticky="w", padx=8, pady=4)
        rank_frm = tk.Frame(frm, bg="#f0f4f8")
        rank_frm.grid(row=2, column=1, sticky="w", padx=4, pady=4)
        for r in ["A", "B", "C", "D", "E"]:
            tk.Radiobutton(rank_frm, text=r, variable=rank_var, value=r, bg="#f0f4f8", font=("", 10, "bold")).pack(side="left", padx=4)

        login_id_var = tk.StringVar(value="SDBDY07009")
        tk.Label(frm, text="🔑 ログインID", bg="#f0f4f8").grid(row=3, column=0, sticky="w", padx=8, pady=4)
        tk.Entry(frm, textvariable=login_id_var, width=20).grid(row=3, column=1, sticky="w", padx=4, pady=4)

        password_var = tk.StringVar(value="buddy1999")
        tk.Label(frm, text="🔒 パスワード", bg="#f0f4f8").grid(row=4, column=0, sticky="w", padx=8, pady=4)
        tk.Entry(frm, textvariable=password_var, width=20, show="*").grid(row=4, column=1, sticky="w", padx=4, pady=4)

        tk.Label(root, text="📋 電話番号リスト（1行1件）", bg="#f0f4f8", font=("", 9)).pack(anchor="w", padx=12)
        id_text = scrolledtext.ScrolledText(root, height=8, width=50, font=("Courier", 10))
        id_text.pack(padx=12, pady=4, fill="x")

        run_btn = tk.Button(root, text="▶ 実行", font=("", 12, "bold"), bg="#2563eb", fg="white",
                            relief="flat", padx=20, pady=8, cursor="hand2")
        run_btn.pack(pady=8)

        tk.Label(root, text="ログ", bg="#f0f4f8", font=("", 9)).pack(anchor="w", padx=12)
        log_box = scrolledtext.ScrolledText(root, height=10, width=50, font=("Courier", 9),
                                            state="disabled", bg="#1e1e2e", fg="#cdd6f4")
        log_box.pack(padx=12, pady=(0, 12), fill="x")

        def log(msg):
            log_box.configure(state="normal")
            log_box.insert("end", msg + "\n")
            log_box.see("end")
            log_box.configure(state="disabled")

        PARALLEL = 5  # 同時実行数

        def process_one(tel, cookies, call_date, call_time, rank):
            """1件処理（独立セッション）"""
            ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36 Edg/146.0.0.0"
            s = VOSClient.create_session()
            s.cookies.update(cookies)
            tmp = VOSClient.__new__(VOSClient)
            tmp.session = s
            ids = tmp.get_id_by_tel(tel)
            if not ids:
                return tel, None, None
            cid = ids[0]
            status = tmp.update_next(cid, call_date, call_time, rank)
            return tel, cid, status

        def run_task(tel_list, call_date, call_time, rank, login_id, password):
            from concurrent.futures import ThreadPoolExecutor, as_completed
            log(f"🔐 ログイン中... ({login_id})")
            try:
                vos = VOSClient(login_id, password)
            except Exception as e:
                log(f"💥 ログイン失敗: {e}")
                run_btn.configure(state="normal", text="▶ 実行")
                return
            cookies = vos.session.cookies.get_dict()
            log(f"🚀 開始: {len(tel_list)}件  日付={call_date} 時間={call_time} ランク={rank}  並列数={PARALLEL}")
            ok = ng = 0
            with ThreadPoolExecutor(max_workers=PARALLEL) as executor:
                futures = {executor.submit(process_one, tel, cookies, call_date, call_time, rank): tel
                           for tel in tel_list}
                for future in as_completed(futures):
                    try:
                        tel, cid, status = future.result()
                        if cid is None:
                            log(f"  ⚠️ 顧客なし  tel={tel}")
                            ng += 1
                        elif status in (200, 302):
                            log(f"  ✅ OK  tel={tel} → custBaseId={cid}  ({status})")
                            ok += 1
                        else:
                            log(f"  ❌ NG  tel={tel} → custBaseId={cid}  ({status})")
                            ng += 1
                    except Exception as e:
                        log(f"  💥 ERR tel={futures[future]}  {e}")
                        ng += 1
            log(f"\n📊 完了: 成功={ok}件 / 失敗={ng}件")
            run_btn.configure(state="normal", text="▶ 実行")

        def start():
            tel_list = []
            for line in id_text.get("1.0", "end").strip().splitlines():
                tel = line.strip().replace("-", "").replace(" ", "").replace("　", "")
                if tel:
                    tel_list.append(tel)
            if not tel_list:
                messagebox.showwarning("エラー", "電話番号が入力されていません")
                return
            call_date = f"{year_var.get()}-{month_var.get()}-{day_var.get()}"
            run_btn.configure(state="disabled", text="⏳ 実行中...")
            threading.Thread(target=run_task,
                             args=(tel_list, call_date, time_var.get(), rank_var.get(),
                                   login_id_var.get().strip(), password_var.get().strip()),
                             daemon=True).start()

        run_btn.configure(command=start)
        root.mainloop()

if __name__ == "__main__":
    VOSClient.run_gui()
