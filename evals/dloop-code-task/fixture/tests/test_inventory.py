import unittest

from inventory import salvage_one
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
