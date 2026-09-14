import unittest
from copy import deepcopy

from inventory import salvage_many, salvage_one
from shared_store import InventoryStore


class ExistingInventoryTests(unittest.TestCase):
    def test_single_salvage(self):
        store = InventoryStore({"a": {"locked": False, "value": 3}})
        self.assertEqual(3, salvage_one(store, "a"))
        self.assertEqual({}, store.items)
        self.assertEqual(3, store.materials)

    def test_locked_item_is_preserved(self):
        store = InventoryStore({"a": {"locked": True, "value": 3}})
        with self.assertRaises(ValueError):
            salvage_one(store, "a")
        self.assertIn("a", store.items)


class BulkSalvageTests(unittest.TestCase):
    def setUp(self):
        self.store = InventoryStore({
            "a": {"locked": False, "value": 3},
            "b": {"locked": False, "value": 5},
            "locked": {"locked": True, "value": 9},
        }, materials=11)
        self.items_reference = self.store.items
        self.before_items = deepcopy(self.store.items)

    def assert_unchanged(self):
        self.assertIs(self.items_reference, self.store.items)
        self.assertEqual(self.before_items, self.store.items)
        self.assertEqual(11, self.store.materials)

    def test_confirmed_selection_credits_total_and_preserves_collection(self):
        selection = ["a", "b"]
        self.assertEqual(8, salvage_many(self.store, selection, confirmed=True))
        self.assertEqual({"locked": self.before_items["locked"]}, self.store.items)
        self.assertEqual(19, self.store.materials)
        self.assertIs(self.items_reference, self.store.items)
        self.assertEqual(["a", "b"], selection)

    def test_cancel_does_not_validate_or_change_data(self):
        self.store.fail_credit = True
        self.assertEqual(0, salvage_many(
            self.store, ["a", "locked", "missing"], confirmed=False))
        self.assert_unchanged()

    def test_empty_selection_does_not_credit(self):
        self.store.fail_credit = True
        self.assertEqual(0, salvage_many(self.store, [], confirmed=True))
        self.assert_unchanged()

    def test_duplicate_selection_is_processed_once(self):
        self.assertEqual(8, salvage_many(
            self.store, ["a", "b", "a", "b"], confirmed=True))
        self.assertEqual(19, self.store.materials)
        self.assertEqual({"locked": self.before_items["locked"]}, self.store.items)

    def test_locked_selection_preserves_everything(self):
        with self.assertRaises(ValueError):
            salvage_many(self.store, ["a", "locked", "b"], confirmed=True)
        self.assert_unchanged()

    def test_missing_selection_preserves_everything(self):
        with self.assertRaises(KeyError):
            salvage_many(self.store, ["a", "missing", "b"], confirmed=True)
        self.assert_unchanged()

    def test_failed_credit_restores_everything(self):
        self.store.fail_credit = True
        with self.assertRaises(RuntimeError):
            salvage_many(self.store, ["a", "b"], confirmed=True)
        self.assert_unchanged()

    def test_credit_failure_after_balance_change_restores_everything(self):
        def failing_credit(amount):
            self.store.materials += amount
            raise RuntimeError("credit failed after balance change")

        self.store.add_materials = failing_credit
        with self.assertRaises(RuntimeError):
            salvage_many(self.store, ["a", "b"], confirmed=True)
        self.assert_unchanged()

    def test_existing_single_salvage_credit_failure_still_restores_data(self):
        self.store.fail_credit = True
        with self.assertRaises(RuntimeError):
            salvage_one(self.store, "a")
        self.assert_unchanged()
