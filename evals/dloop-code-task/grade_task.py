"""独立业务断言；评测者在编码结束后运行，不把答案交给实施者。"""

import argparse
import json
from pathlib import Path
import sys
import unittest


class AcceptanceTests(unittest.TestCase):
    def store(self):
        return InventoryStore({"a": {"locked": False, "value": 3}, "b": {"locked": False, "value": 5},
                               "locked": {"locked": True, "value": 9}}, materials=2)

    def test_confirm_aggregates_once(self):
        store = self.store()
        items_reference = store.items
        self.assertEqual(8, salvage_many(store, ["a", "b", "a"], confirmed=True))
        self.assertEqual({"locked"}, set(items_reference))
        self.assertIs(items_reference, store.items)
        self.assertEqual(10, store.materials)

    def test_cancel_leaves_state(self):
        store = self.store()
        before = dict(store.items)
        self.assertEqual(0, salvage_many(store, ["missing", "locked"], confirmed=False))
        self.assertEqual(before, store.items)
        self.assertEqual(2, store.materials)

    def test_empty_selection(self):
        store = self.store()
        self.assertEqual(0, salvage_many(store, [], confirmed=True))
        self.assertEqual(2, store.materials)

    def test_locked_selection_is_atomic(self):
        store = self.store()
        before = dict(store.items)
        with self.assertRaises(ValueError):
            salvage_many(store, ["a", "locked"], confirmed=True)
        self.assertEqual(before, store.items)
        self.assertEqual(2, store.materials)

    def test_missing_selection_is_atomic(self):
        store = self.store()
        before = dict(store.items)
        with self.assertRaises((KeyError, ValueError)):
            salvage_many(store, ["a", "missing"], confirmed=True)
        self.assertEqual(before, store.items)
        self.assertEqual(2, store.materials)

    def test_credit_failure_restores_existing_reference(self):
        store = self.store()
        items_reference = store.items
        before = dict(store.items)
        store.fail_credit = True
        with self.assertRaises(RuntimeError):
            salvage_many(store, ["a", "b"], confirmed=True)
        self.assertIs(items_reference, store.items)
        self.assertEqual(before, items_reference)
        self.assertEqual(2, store.materials)

    def test_repeated_confirmation_does_not_credit_twice(self):
        store = self.store()
        salvage_many(store, ["a"], confirmed=True)
        with self.assertRaises((KeyError, ValueError)):
            salvage_many(store, ["a"], confirmed=True)
        self.assertEqual(5, store.materials)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True, type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.project.resolve()))
    from inventory import salvage_many
    from shared_store import InventoryStore
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(AcceptanceTests))
    print(json.dumps({"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors)}))
    sys.exit(0 if result.wasSuccessful() else 1)
