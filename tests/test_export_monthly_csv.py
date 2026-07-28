import importlib.util
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

if __name__ == "__main__":
    unittest.main()
