from shared_store import InventoryStore


def salvage_one(store: InventoryStore, item_id: str) -> int:
    item = store.items[item_id]
    if item["locked"]:
        raise ValueError("locked item")
    with store.transaction():
        del store.items[item_id]
        store.add_materials(item["value"])
    return item["value"]


def salvage_many(store: InventoryStore, item_ids: list[str], *, confirmed: bool) -> int:
    raise NotImplementedError("bulk salvage is not implemented")
