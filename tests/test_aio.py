import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import aio  # noqa: E402

CATALOG = Path(__file__).resolve().parent / "catalog-sample.json"


class ParseWhen(unittest.TestCase):
    def test_interval_forms(self):
        for text, minutes in [("90", 90), ("45m", 45), ("1h30m", 90), ("1ч30м", 90), ("2h", 120)]:
            with self.subTest(text=text):
                delta = aio.parse_when(text) - datetime.now()
                self.assertAlmostEqual(delta.total_seconds(), minutes * 60, delta=5)

    def test_clock_time_is_next_occurrence(self):
        at = aio.parse_when("23:59")
        self.assertEqual((at.hour, at.minute), (23, 59))
        self.assertGreater(at, datetime.now())
        self.assertLess(at - datetime.now(), timedelta(days=1))

    def test_rejects_garbage(self):
        for text in ("", "abc", "25:00", "0", "12:75"):
            with self.subTest(text=text), self.assertRaises(aio.AioError):
                aio.parse_when(text)


class Assets(unittest.TestCase):
    release = {"tag_name": "v1.3.3", "assets": [
        {"name": "TryToCatchMe_1.3.3_x64-setup.exe"},
        {"name": "TryToCatchMe_1.3.3_amd64.AppImage"},
    ]}

    def test_glob(self):
        self.assertEqual(aio.pick_asset(self.release, "TryToCatchMe_*_amd64.AppImage")["name"],
                         "TryToCatchMe_1.3.3_amd64.AppImage")

    def test_missing(self):
        with self.assertRaises(aio.AioError):
            aio.pick_asset(self.release, "*.deb")


class Catalog(unittest.TestCase):
    def setUp(self):
        aio._catalog = None

    def test_real_catalog_merges_with_builtin(self):
        data = CATALOG.read_bytes()
        with mock.patch.object(aio, "http_get", return_value=data), \
                mock.patch.object(aio, "save_json"):
            mods = aio.fetch_catalog()
        ids = [m["id"] for m in mods]
        self.assertEqual(len(ids), len(set(ids)))
        for mid in ("zapret", "tg-ws-proxy", "trytocatchme", "shutdown-timer"):
            self.assertIn(mid, ids)
            self.assertIsNotNone(aio.linux_spec(next(m for m in mods if m["id"] == mid)))
        for m in mods:
            spec = aio.linux_spec(m)
            if spec and not spec.get("kind", "").startswith("builtin:"):
                src = aio.source_of(m)
                self.assertTrue(src["repo"] and src["asset"], m["id"])

    def test_offline_uses_builtin(self):
        with mock.patch.object(aio, "http_get", side_effect=OSError("offline")), \
                mock.patch.object(aio, "CATALOG_CACHE", Path("/nonexistent/catalog.json")):
            mods = aio.fetch_catalog()
        self.assertEqual([m["id"] for m in mods], [m["id"] for m in aio.BUILTIN_CATALOG])

    def test_module_paths(self):
        aio._catalog = aio.BUILTIN_CATALOG
        self.assertEqual(aio.module_file(aio.find_module("zapret")), aio.ZAPRET_BIN)
        self.assertEqual(aio.module_file(aio.find_module("tg-ws-proxy")).name, "TgWsProxy")
        self.assertEqual(aio.module_file(aio.find_module("TryToCatchMe")).name, "TryToCatchMe.AppImage")
        with self.assertRaises(aio.AioError):
            aio.find_module("nope")


class Desktop(unittest.TestCase):
    def test_entry(self):
        e = aio.desktop_entry("X", '"/a b/x"', "icon", terminal=True)
        self.assertIn('Exec="/a b/x"\n', e)
        self.assertIn("Terminal=true\n", e)
        self.assertTrue(e.startswith("[Desktop Entry]\n"))


if __name__ == "__main__":
    unittest.main()
