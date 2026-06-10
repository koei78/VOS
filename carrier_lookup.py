from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import requests


@dataclass(frozen=True)
class CarrierResult:
    phone_number: str
    prefix: str | None
    carriers: tuple[str, ...]
    found: bool
    error: str | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["carriers"] = list(self.carriers)
        data["carrier"] = self.carriers[0] if self.carriers else None
        return data


class FixedLineCarrierLookup:
    """固定電話番号の初期割当キャリアをメモリ上の辞書から検索する。"""

    SOURCE_URL = "http://denwa-bangou.com/zipcode2/{digit}.txt"
    SOURCE_PAGE = "https://denwa-bangou.com/number-carrier"
    SOURCE_AS_OF = "2025-01-01"
    SHARD_DIGITS = tuple("123456789")

    def __init__(
        self,
        cache_file: str | Path = ".cache/fixed_line_carriers.json",
        cache_ttl: int = 7 * 24 * 60 * 60,
        timeout: float = 20.0,
        request_interval: float = 0.3,
        session: requests.Session | None = None,
    ):
        self.cache_file = Path(cache_file)
        self.cache_ttl = cache_ttl
        self.timeout = timeout
        self.request_interval = max(0.0, request_interval)
        self.session = session or requests.Session()
        self.session.headers.setdefault(
            "User-Agent",
            "VOSapp FixedLineCarrierLookup/1.0 "
            "(source: https://denwa-bangou.com/number-carrier)",
        )
        self._index: dict[str, tuple[str, ...]] = {}
        self._loaded_shards: set[str] = set()
        self._loaded = False
        self._load_lock = threading.Lock()
        self._last_request_at = 0.0

    def load(self, force_refresh: bool = False) -> int:
        """全割当一覧を1ファイルずつ順番に読み込む。"""
        if self._loaded and len(self._loaded_shards) == len(self.SHARD_DIGITS) and not force_refresh:
            return len(self._index)

        with self._load_lock:
            if force_refresh:
                self._index = {}
                self._loaded_shards = set()
                self._loaded = True
            elif not self._loaded:
                self._load_cache()

            if len(self._loaded_shards) == len(self.SHARD_DIGITS):
                return len(self._index)

            for digit in self.SHARD_DIGITS:
                if digit not in self._loaded_shards:
                    self._load_shard(digit)
            self._loaded = True
            self._save_cache()
            return len(self._index)

    def lookup(self, phone_number: object) -> CarrierResult:
        """電話番号1件を検索する。初回だけ割当一覧を自動読み込みする。"""
        original = "" if phone_number is None else str(phone_number)
        try:
            normalized = self.normalize_phone_number(original)
        except ValueError as exc:
            return CarrierResult(original, None, (), False, str(exc))

        prefix = normalized[:6]
        self._ensure_shard(prefix[1])
        carriers = self._index.get(prefix, ())
        return CarrierResult(normalized, prefix, carriers, bool(carriers))

    def lookup_many(self, phone_numbers: Iterable[object]) -> list[CarrierResult]:
        """配列を入力順のまま、先頭から1件ずつ検索する。"""
        results = []
        for value in phone_numbers:
            results.append(self.lookup(value))
        return results

    def lookup_many_dicts(self, phone_numbers: Iterable[object]) -> list[dict]:
        return [result.to_dict() for result in self.lookup_many(phone_numbers)]

    @staticmethod
    def normalize_phone_number(phone_number: str) -> str:
        value = phone_number.strip()
        if value.startswith("+81"):
            value = "0" + value[3:]
        digits = re.sub(r"[\s()（）-]", "", value)

        if not digits.isdigit():
            raise ValueError("電話番号は数字、ハイフン、空白、括弧で入力してください")
        if len(digits) < 6:
            raise ValueError("固定電話番号は先頭6桁以上必要です")
        if not digits.startswith("0"):
            raise ValueError("日本国内の0から始まる電話番号を指定してください")
        if digits.startswith("050"):
            raise ValueError("050 IP電話はこの検索の対象外です")
        return digits

    def _ensure_shard(self, digit: str) -> None:
        with self._load_lock:
            if not self._loaded:
                self._load_cache()
                self._loaded = True
            if digit in self._loaded_shards:
                return
            if digit not in self.SHARD_DIGITS:
                return
            self._load_shard(digit)
            self._save_cache()

    def _load_shard(self, digit: str) -> None:
        rows: dict[str, list[str]] = {}
        try:
            shard_rows = self._download_shard(digit)
        except Exception as exc:
            raise RuntimeError(f"キャリア割当データ({digit})の取得に失敗: {exc}") from exc

        for prefix, carrier in shard_rows:
            carriers = rows.setdefault(prefix, [])
            if carrier not in carriers:
                carriers.append(carrier)
        self._index.update({prefix: tuple(carriers) for prefix, carriers in rows.items()})
        self._loaded_shards.add(digit)

    def _download_shard(self, digit: str) -> list[tuple[str, str]]:
        wait = self.request_interval - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        response = self.session.get(
            self.SOURCE_URL.format(digit=digit),
            timeout=self.timeout,
        )
        self._last_request_at = time.monotonic()
        response.raise_for_status()
        response.encoding = response.apparent_encoding or "utf-8"

        result = []
        for line in response.text.splitlines():
            prefix, separator, carrier = line.strip().partition(",")
            if separator and len(prefix) == 6 and prefix.isdigit() and carrier.strip():
                result.append((prefix, carrier.strip()))
        return result

    def _load_cache(self) -> bool:
        try:
            if not self.cache_file.exists():
                return False
            payload = json.loads(self.cache_file.read_text(encoding="utf-8"))
            created_at = float(payload["created_at"])
            if self.cache_ttl >= 0 and time.time() - created_at > self.cache_ttl:
                return False

            raw_index = payload["index"]
            self._index = {
                prefix: tuple(carriers)
                for prefix, carriers in raw_index.items()
                if len(prefix) == 6 and prefix.isdigit()
            }
            self._loaded_shards = set(payload.get("loaded_shards", self.SHARD_DIGITS))
            self._loaded = bool(self._index)
            return True
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return False

    def _save_cache(self) -> None:
        payload = {
            "created_at": time.time(),
            "source": self.SOURCE_PAGE,
            "source_as_of": self.SOURCE_AS_OF,
            "loaded_shards": sorted(self._loaded_shards),
            "index": {key: list(value) for key, value in self._index.items()},
        }
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.cache_file.with_suffix(self.cache_file.suffix + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary.replace(self.cache_file)
        except OSError:
            # Read-only environments can still use the in-memory index.
            pass


if __name__ == "__main__":
    lookup = FixedLineCarrierLookup()
    sample_numbers = ["03-1234-5678", "0364275970", "050-1234-5678"]
    for item in lookup.lookup_many_dicts(sample_numbers):
        print(item)
