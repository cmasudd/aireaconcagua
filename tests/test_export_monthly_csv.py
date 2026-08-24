import importlib.util
import csv
import json
import sys
import unittest
from datetime import date
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "export_monthly_csv.py"
)
SPEC = importlib.util.spec_from_file_location("export_monthly_csv", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class ExportHelpersTests(unittest.TestCase):
    def test_month_bounds(self):
        start, end = MODULE.month_bounds("2026-12")
        self.assertEqual(start.isoformat(), "2026-12-01T00:00:00")
        self.assertEqual(end.isoformat(), "2027-01-01T00:00:00")

    def test_iter_months(self):
        self.assertEqual(
            list(MODULE.iter_months(date(2026, 4, 22), date(2026, 7, 28))),
            ["2026-04", "2026-05", "2026-06", "2026-07"],
        )

    def test_vina_errazuriz_uses_v1_for_hourly_export(self):
        config_path = MODULE.ROOT / "config" / "stations.json"
        stations = json.loads(config_path.read_text(encoding="utf-8"))
        by_code = {station["code"]: station for station in stations}

        self.assertNotIn("HIRIPRO-V7", by_code)
        self.assertEqual(by_code["HIRIPRO-V1"]["device_id"], 224)
        self.assertEqual(by_code["HIRIPRO-V1"]["name"], "Esc. Viña Errázuriz")

    def test_published_manifest_and_csv_files_are_consistent(self):
        root = MODULE.ROOT
        config = json.loads(
            (root / "config" / "stations.json").read_text(encoding="utf-8")
        )
        manifest = json.loads(
            (root / "data" / "manifest.json").read_text(encoding="utf-8")
        )
        expected_codes = {station["code"] for station in config}
        published_codes = {station["code"] for station in manifest["stations"]}

        self.assertEqual(published_codes, expected_codes)
        self.assertNotIn("HIRIPRO-V7", published_codes)
        self.assertFalse((root / "data" / "HIRIPRO-V7").exists())

        for station in manifest["stations"]:
            for month, relative_paths in station["months"].items():
                previous_timestamp = None
                for relative_path in relative_paths:
                    path = root / relative_path
                    self.assertTrue(path.is_file(), relative_path)
                    self.assertLessEqual(
                        path.stat().st_size,
                        manifest["max_csv_bytes"],
                        relative_path,
                    )
                    with path.open(newline="", encoding="utf-8") as handle:
                        reader = csv.DictReader(handle)
                        self.assertEqual(reader.fieldnames, MODULE.CSV_HEADER)
                        for row in reader:
                            timestamp = row["fecha"]
                            self.assertTrue(timestamp.startswith(month), timestamp)
                            if previous_timestamp is not None:
                                self.assertGreaterEqual(timestamp, previous_timestamp)
                            previous_timestamp = timestamp

        with (root / "data" / "latest.csv").open(
            newline="", encoding="utf-8"
        ) as handle:
            latest_codes = {row["codigo"] for row in csv.DictReader(handle)}
        self.assertTrue(latest_codes.issubset(expected_codes))
        self.assertIn("HIRIPRO-V1", latest_codes)
        self.assertNotIn("HIRIPRO-V7", latest_codes)

if __name__ == "__main__":
    unittest.main()
