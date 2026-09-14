import unittest
from unittest.mock import patch

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
        self.items = {
            "a": {"locked": False, "value": 3},
            "b": {"locked": False, "value": 5},
            "locked": {"locked": True, "value": 9},
        }
        self.store = InventoryStore(self.items, materials=10)
        self.items_reference = self.store.items

    def assert_unchanged(self):
        self.assertEqual(self.items, self.store.items)
        self.assertEqual(10, self.store.materials)
        self.assertIs(self.items_reference, self.store.items)

    def test_confirmed_selection_is_removed_and_credited_once(self):
        selection = ["a", "b", "a"]
        with patch.object(self.store, "add_materials", wraps=self.store.add_materials) as credit:
            self.assertEqual(8, salvage_many(self.store, selection, confirmed=True))
            credit.assert_called_once_with(8)
        self.assertEqual({"locked": self.items["locked"]}, self.store.items)
        self.assertEqual(18, self.store.materials)
        self.assertIs(self.items_reference, self.store.items)
        self.assertEqual(["a", "b", "a"], selection)

    def test_cancelled_selection_does_not_validate_or_change_data(self):
        with patch.object(self.store, "add_materials") as credit:
            self.assertEqual(0, salvage_many(self.store, ["a", "locked", "missing"], confirmed=False))
            credit.assert_not_called()
        self.assert_unchanged()

    def test_empty_selection_does_not_issue_materials(self):
        self.store.fail_credit = True
        self.assertEqual(0, salvage_many(self.store, [], confirmed=True))
        self.assert_unchanged()

    def test_locked_selection_preserves_entire_inventory(self):
        with self.assertRaises(ValueError):
            salvage_many(self.store, ["a", "locked"], confirmed=True)
        self.assert_unchanged()

    def test_missing_selection_preserves_entire_inventory(self):
        with self.assertRaises(KeyError):
            salvage_many(self.store, ["a", "missing"], confirmed=True)
        self.assert_unchanged()

    def test_credit_failure_restores_items_and_materials(self):
        self.store.fail_credit = True
        with self.assertRaises(RuntimeError):
            salvage_many(self.store, ["a", "b"], confirmed=True)
        self.assert_unchanged()

    def test_failure_after_credit_restores_items_and_materials(self):
        def credit_then_fail(amount):
            self.store.materials += amount
            raise RuntimeError("credit failed after mutation")

        with patch.object(self.store, "add_materials", side_effect=credit_then_fail):
            with self.assertRaises(RuntimeError):
                salvage_many(self.store, ["a", "b"], confirmed=True)
        self.assert_unchanged()
