import json
import tempfile
import unittest
from pathlib import Path

from carrier_lookup import FixedLineCarrierLookup


class FakeResponse:
    apparent_encoding = "utf-8"
    encoding = "utf-8"

    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.calls = []

    def get(self, url, timeout):
        self.calls.append((url, timeout))
        digit = url.removesuffix(".txt").rsplit("/", 1)[-1]
        if digit == "3":
            return FakeResponse("031234,テスト通信\n031234,第二通信\n")
        if digit == "6":
            return FakeResponse("061234,西日本通信\n")
        return FakeResponse("")


class FixedLineCarrierLookupTest(unittest.TestCase):
    def test_lookup_many_preserves_order_and_loads_shards_sequentially(self):
        with tempfile.TemporaryDirectory() as directory:
            session = FakeSession()
            lookup = FixedLineCarrierLookup(
                cache_file=Path(directory) / "cache.json",
                session=session,
            )

            results = lookup.lookup_many(
                ["03-1234-5678", "+81 6 1234 5678", "050-1234-5678", "abc"]
            )

            self.assertEqual(results[0].carriers, ("テスト通信", "第二通信"))
            self.assertEqual(results[1].carriers, ("西日本通信",))
            self.assertEqual(results[2].error, "050 IP電話はこの検索の対象外です")
            self.assertIsNotNone(results[3].error)
            self.assertEqual(len(session.calls), 2)
            self.assertTrue(session.calls[0][0].endswith("/3.txt"))
            self.assertTrue(session.calls[1][0].endswith("/6.txt"))

            lookup.lookup("0312349999")
            self.assertEqual(len(session.calls), 2)

    def test_valid_cache_avoids_network(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_file = Path(directory) / "cache.json"
            cache_file.write_text(
                json.dumps(
                    {
                        "created_at": 9999999999,
                        "loaded_shards": ["3"],
                        "index": {"031234": ["キャッシュ通信"]},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            session = FakeSession()
            lookup = FixedLineCarrierLookup(cache_file=cache_file, session=session)

            result = lookup.lookup("0312345678")

            self.assertEqual(result.carriers, ("キャッシュ通信",))
            self.assertEqual(session.calls, [])


if __name__ == "__main__":
    unittest.main()
