"""检查交换资料的标识、文本和跨表引用。"""

import json
from pathlib import Path
import unittest


DATA_DIR = Path(__file__).resolve().parents[1] / "data"


class SourceDataTest(unittest.TestCase):
    def setUp(self):
        self.tables = {}
        for path in sorted(DATA_DIR.glob("*.json")):
            rows = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsInstance(rows, list)
            self.assertTrue(rows, path.name)
            by_id = {}
            for row in rows:
                identifier = row["id"]
                self.assertIsInstance(identifier, str, path.name)
                self.assertNotIn(identifier, by_id, path.name)
                if "text" in row:
                    self.assertIsInstance(row["text"], str, path.name)
                by_id[identifier] = row
            self.tables[path.stem] = by_id

    def test_exchange_tables_present(self):
        self.assertEqual(set(self.tables), {'classes', 'copybook', 'screens'})

    def test_related_source_records(self):
        data = self.tables
        screens = data["screens"]
        message_ids = {entry["message_id"] for entry in data["copybook"].values()}
        cases = data["classes"]
        for case in cases.values():
            screen = screens[case["device"]]
            self.assertTrue(set(screen["required_keys"]) <= case["data"].keys())
            self.assertIn(screen["waiting_ref"], message_ids)
        self.assertEqual(cases["R1"]["data"]["group_count"], 0)
        self.assertNotEqual(cases["R3"]["data"]["permitted_public_name"], cases["R3"]["data"]["primary"])
