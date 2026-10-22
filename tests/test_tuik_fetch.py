"""scripts/tuik_fetch.py için birim testleri. Gerçek ağa çıkmaz."""

import contextlib
import io
import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import tuik_fetch as tf  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
NAMES_TEST = FIXTURES / "names_test.json"
DATA = FIXTURES / "tt09_data.json"
STRUCTURE = FIXTURES / "tt09_structure.json"


def load_fixture():
    return json.loads(DATA.read_text(encoding="utf-8"))


def categories(data, dim):
    index = data["dimension"][dim]["category"]["index"]
    if isinstance(index, dict):
        return [c for c, _ in sorted(index.items(), key=lambda kv: kv[1])]
    return list(index)


def flat_key(data, code, month, level=None):
    """(kod, ay) çiftinin value anahtarını bulur.

    level verilirse SINIFLAMA_DUZEYI o kategoriye ayarlanmış anahtar hesaplanır.
    """
    ids, sizes = data["id"], data["size"]
    if level is None:
        for key in data["value"]:
            coords = decode(int(key), sizes)
            if (categories(data, "COICOP_2018")[coords[ids.index("COICOP_2018")]] == code
                    and categories(data, "TIME_PERIOD")[coords[ids.index("TIME_PERIOD")]] == month):
                return key
        raise KeyError((code, month))
    coords = [0] * len(ids)
    coords[ids.index("COICOP_2018")] = categories(data, "COICOP_2018").index(code)
    coords[ids.index("TIME_PERIOD")] = categories(data, "TIME_PERIOD").index(month)
    coords[ids.index("SINIFLAMA_DUZEYI")] = categories(data, "SINIFLAMA_DUZEYI").index(level)
    flat = 0
    for c, s in zip(coords, sizes):
        flat = flat * s + c
    return str(flat)


def decode(flat, sizes):
    coords = [0] * len(sizes)
    for d in range(len(sizes) - 1, -1, -1):
        flat, coords[d] = divmod(flat, sizes[d])
    return coords


def keys_for_code(data, code):
    ids, sizes = data["id"], data["size"]
    pos = ids.index("COICOP_2018")
    cats = categories(data, "COICOP_2018")
    return [k for k in data["value"] if cats[decode(int(k), sizes)[pos]] == code]


class CliTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.out = self.dir / "v1" / "tufe.json"
        self.data = load_fixture()

        env = {k: v for k, v in os.environ.items()
               if k not in ("GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY")}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

        # Varsayılan olarak ağ yasak; ağ testleri bunu kendi sahteleriyle değiştirir.
        self.http = mock.Mock(side_effect=AssertionError("gerçek ağ çağrısı yapıldı"))
        self.sleep = mock.Mock()
        for name, fake in (("http_request", self.http), ("sleep", self.sleep)):
            p = mock.patch.object(tf, name, fake)
            p.start()
            self.addCleanup(p.stop)

    def run_cli(self, *extra, data=None, use_input=True, today="2026-10-22"):
        argv = ["--out", str(self.out), "--first-month", "2025-01",
                "--today", today, "--names", str(NAMES_TEST)]
        if use_input:
            path = self.dir / "input.json"
            path.write_text(json.dumps(self.data if data is None else data), encoding="utf-8")
            argv += ["--input", str(path)]
        argv += list(extra)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = tf.main(argv)
        self.output = buf.getvalue()
        return code

    def written(self):
        return json.loads(self.out.read_text(encoding="utf-8"))

    def month_index(self, month):
        return tf.month_range("2025-01", "2026-09").index(month)


class ParseTests(CliTestCase):
    def assert_fixture_series(self, doc):
        s = doc["series"]
        self.assertEqual(doc["latestMonth"], "2026-09")
        self.assertEqual(len(s["0"]), 21)
        self.assertEqual(s["0"][0], 88.578291)
        self.assertEqual(s["0"][11], 110.386963)
        self.assertEqual(s["0"][-1], 137.23)
        self.assertEqual(s["01"][0], 89.040253)
        self.assertEqual(s["01"][-1], 134.04)
        self.assertEqual(s["01113"][-1], 133.96)
        self.assertEqual(s["01143"][:11], [None] * 11)
        self.assertEqual(s["01143"][11], 100.0)
        self.assertEqual(s["01143"][-1], 131.17)
        self.assertEqual(s["01172"][0], 124.53371)
        self.assertEqual(s["01172"][self.month_index("2026-02")], 214.11)
        self.assertEqual(s["01172"][-1], 116.63)

    def test_fixture(self):
        self.assertEqual(self.run_cli(), 0, self.output)
        doc = self.written()
        self.assert_fixture_series(doc)
        self.assertEqual(doc["firstMonth"], "2025-01")
        self.assertEqual(list(doc["names"]), ["0", "01", "011", "01113", "01143", "01172"])
        self.assertEqual(list(doc["series"]), list(doc["names"]))
        self.assertNotIn("::warning", self.output)

    def test_empty_string_is_missing_not_zero(self):
        self.data["value"][flat_key(self.data, "01113", "2025-06")] = ""
        self.assertEqual(self.run_cli(), 0, self.output)
        values = self.written()["series"]["01113"]
        self.assertIsNone(values[self.month_index("2025-06")])
        self.assertNotIn(0.0, values)
        self.assertIn("::warning title=TÜFE::", self.output)

    def test_value_as_list(self):
        total = math.prod(self.data["size"])
        values = [None] * total
        for key, value in self.data["value"].items():
            values[int(key)] = value
        self.data["value"] = values
        self.assertEqual(self.run_cli(), 0, self.output)
        self.assert_fixture_series(self.written())

    def test_index_as_dict(self):
        for dim in self.data["id"]:
            cat = self.data["dimension"][dim]["category"]
            # Sözlük sırası bilerek ters: değere göre sıralanmalı.
            cat["index"] = {c: i for i, c in reversed(list(enumerate(cat["index"])))}
        self.assertEqual(self.run_cli(), 0, self.output)
        self.assert_fixture_series(self.written())

    def test_missing_required_dimension(self):
        data = tf.parse_jsonstat(self.data)
        self.assertIn("0", data)
        broken = load_fixture()
        broken["id"][broken["id"].index("TIME_PERIOD")] = "ZAMAN"
        with self.assertRaises(tf.ValidationError):
            tf.parse_jsonstat(broken)


class HardErrorTests(CliTestCase):
    def assert_fails(self, code=1, **kwargs):
        self.assertEqual(self.run_cli(**kwargs), code, self.output)
        self.assertIn("::error title=TÜFE::", self.output)
        self.assertFalse(self.out.exists())

    def test_code_removed(self):
        for key in keys_for_code(self.data, "01113"):
            del self.data["value"][key]
        self.assert_fails()

    def test_headline_latest_missing(self):
        self.data["value"][flat_key(self.data, "0", "2026-09")] = ""
        self.assert_fails()
        self.assertIn('"0" serisinin 2026-09 değeri yok', self.output)

    def test_negative_value(self):
        self.data["value"][flat_key(self.data, "01172", "2025-06")] = "-5"
        self.assert_fails()

    def test_ratio(self):
        key = flat_key(self.data, "01172", "2026-03")
        self.data["value"][key] = str(float(self.data["value"][key]) * 3)
        self.assert_fails()
        self.assertIn("oranı", self.output)

    def test_base(self):
        for key in keys_for_code(self.data, "01113"):
            self.data["value"][key] = str(float(self.data["value"][key]) * 1.2)
        self.assert_fails()
        self.assertIn("ortalaması", self.output)

    def test_latest_behind_old_file(self):
        self.assertEqual(self.run_cli(), 0, self.output)
        old = self.written()
        old["latestMonth"] = "2026-10"
        for code in old["series"]:
            old["series"][code].append(None)
        self.out.write_text(tf.dumps_document(old), encoding="utf-8")
        before = self.out.read_text(encoding="utf-8")
        self.assertEqual(self.run_cli(), 1, self.output)
        self.assertEqual(self.out.read_text(encoding="utf-8"), before)

    def test_revision(self):
        self.assertEqual(self.run_cli(), 0, self.output)
        old = self.written()
        old["series"]["0"][self.month_index("2026-08")] = 130.0
        self.out.write_text(tf.dumps_document(old), encoding="utf-8")
        before = self.out.read_text(encoding="utf-8")
        self.assertEqual(self.run_cli(), 1, self.output)
        self.assertIn("geçmiş değerleri değişmiş", self.output)
        self.assertEqual(self.out.read_text(encoding="utf-8"), before)

    def test_duplicate_with_different_value(self):
        self.data["value"][flat_key(self.data, "0", "2026-09", level="2")] = "137.5"
        self.assert_fails()
        self.assertIn("iki kez", self.output)

    def test_duplicate_with_same_value_is_fine(self):
        self.data["value"][flat_key(self.data, "0", "2026-09", level="2")] = "137.23"
        self.assertEqual(self.run_cli(), 0, self.output)

    def test_latest_in_future(self):
        self.assert_fails(today="2026-09-15")
        self.assertIn("en fazla 2026-08", self.output)


class BehaviourTests(CliTestCase):
    def test_warning_latest_missing(self):
        self.data["value"][flat_key(self.data, "01113", "2026-09")] = ""
        self.assertEqual(self.run_cli(), 0, self.output)
        self.assertIsNone(self.written()["series"]["01113"][-1])
        self.assertIn('::warning title=TÜFE::"01113" kodunun 2026-09 değeri yok', self.output)

    def test_unchanged(self):
        gh_out = self.dir / "gh_output"
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(gh_out)}):
            self.assertEqual(self.run_cli(), 0, self.output)
            first = self.out.read_text(encoding="utf-8")
            self.assertIn("changed=true", gh_out.read_text())
            gh_out.write_text("")
            with mock.patch.object(tf, "write_atomic", side_effect=AssertionError("yazıldı")):
                self.assertEqual(self.run_cli(), 0, self.output)
        self.assertEqual(self.out.read_text(encoding="utf-8"), first)
        lines = gh_out.read_text().splitlines()
        self.assertEqual(lines, ["changed=false", "latest=2026-09", "skipped=false"])

    def test_skip_when_current(self):
        self.assertEqual(self.run_cli(), 0, self.output)
        gh_out = self.dir / "gh_output"
        with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(gh_out)}):
            self.assertEqual(self.run_cli(use_input=False), 0, self.output)
        self.http.assert_not_called()
        self.assertIn("Güncel, istek yapılmadı", self.output)
        self.assertEqual(gh_out.read_text().splitlines(),
                         ["changed=false", "latest=2026-09", "skipped=true"])

    def test_delay(self):
        self.assertEqual(self.run_cli(today="2026-11-12"), 3, self.output)
        self.assertIn("TÜİK verisi gecikti: beklenen 2026-10, gelen 2026-09", self.output)

    def test_delay_with_force(self):
        self.assertEqual(self.run_cli("--force", today="2026-11-12"), 0, self.output)
        self.assertNotIn("gecikti", self.output)

    def test_no_delay_before_day_10(self):
        self.assertEqual(self.run_cli(today="2026-11-09"), 0, self.output)

    def test_format(self):
        self.assertEqual(self.run_cli(), 0, self.output)
        text = self.out.read_text(encoding="utf-8")
        self.assertTrue(text.endswith("}\n"))
        self.assertIn("TÜFE", text)  # ensure_ascii=False
        doc = json.loads(text)
        self.assertEqual(list(doc), ["schema", "source", "sourceUrl", "base", "firstMonth",
                                     "latestMonth", "generatedAt", "names", "series"])
        lines = text.splitlines()
        for code in doc["series"]:
            matching = [l for l in lines if l.startswith(f'    "{code}": [')]
            self.assertEqual(len(matching), 1, code)
            self.assertTrue(matching[0].rstrip(",").endswith("]"))
        for code in doc["names"]:
            self.assertEqual(sum(l.startswith(f'    "{code}": "') for l in lines), 1)
        top = [l for l in lines if l.startswith('  "')]
        self.assertEqual(len(top), 9)
        self.assertEqual(json.loads(tf.dumps_document(doc)), doc)
        self.assertEqual(tf.dumps_document(doc), text)

    def test_dry_run(self):
        self.assertEqual(self.run_cli("--dry-run"), 0, self.output)
        self.assertFalse(self.out.exists())
        self.assertIn("::notice title=TÜFE::", self.output)

    def test_step_summary(self):
        summary = self.dir / "summary.md"
        with mock.patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary)}):
            self.assertEqual(self.run_cli(), 0, self.output)
        text = summary.read_text(encoding="utf-8")
        self.assertIn("Son ay: 2026-09", text)
        self.assertIn("Seri sayısı: 6", text)


class NetworkTests(CliTestCase):
    def test_fetch_from_network(self):
        structure = STRUCTURE.read_bytes()
        data = DATA.read_bytes()
        self.http.side_effect = [(200, structure), (200, data)]
        self.assertEqual(self.run_cli(use_input=False), 0, self.output)
        self.assertEqual(self.http.call_count, 2)
        get, post = self.http.call_args_list
        self.assertEqual(get.args[:2], ("GET", tf.STRUCTURE_URL))
        self.assertEqual(post.args[:2], ("POST", tf.DATA_URL))
        criteria = json.loads(structure)["template"]["criteria"]
        self.assertEqual(json.loads(post.args[2]), criteria)
        self.assertEqual(self.written()["latestMonth"], "2026-09")

    def test_local_structure(self):
        self.http.side_effect = [(200, DATA.read_bytes())]
        self.assertEqual(self.run_cli("--structure", str(STRUCTURE), use_input=False), 0,
                         self.output)
        self.assertEqual(self.http.call_count, 1)
        self.assertEqual(self.http.call_args.args[0], "POST")

    def test_4xx_not_retried(self):
        self.http.side_effect = None
        self.http.return_value = (403, b"Forbidden")
        self.assertEqual(self.run_cli(use_input=False), 2, self.output)
        self.assertEqual(self.http.call_count, 1)
        self.sleep.assert_not_called()
        self.assertIn("HTTP 403", self.output)

    def test_5xx_retried_three_times(self):
        self.http.side_effect = None
        self.http.return_value = (503, b"")
        self.assertEqual(self.run_cli(use_input=False), 2, self.output)
        self.assertEqual(self.http.call_count, 3)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [30, 90])

    def test_network_error_retried(self):
        self.http.side_effect = [OSError("bağlantı kesildi"), (200, STRUCTURE.read_bytes()),
                                 (200, DATA.read_bytes())]
        self.assertEqual(self.run_cli(use_input=False), 0, self.output)
        self.assertEqual(self.http.call_count, 3)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [30])

    def test_network_error_persistent(self):
        self.http.side_effect = OSError("zaman aşımı")
        self.assertEqual(self.run_cli(use_input=False), 2, self.output)
        self.assertEqual(self.http.call_count, 3)


if __name__ == "__main__":
    unittest.main()
