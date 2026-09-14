from contextlib import contextmanager
from copy import deepcopy


class InventoryStore:
    def __init__(self, items, materials=0):
        self.items = deepcopy(items)
        self.materials = materials
        self.fail_credit = False

    def add_materials(self, amount):
        if self.fail_credit:
            raise RuntimeError("credit service unavailable")
        self.materials += amount

    @contextmanager
    def transaction(self):
        before_items = deepcopy(self.items)
        before_materials = self.materials
        try:
            yield
        except Exception:
            self.items.clear()
            self.items.update(before_items)
            self.materials = before_materials
            raise
