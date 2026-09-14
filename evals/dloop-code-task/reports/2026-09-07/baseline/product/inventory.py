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
    if not confirmed:
        return 0
    selected_ids = list(dict.fromkeys(item_ids))
    if not selected_ids:
        return 0
    total = 0
    for item_id in selected_ids:
        item = store.items[item_id]
        if item["locked"]:
            raise ValueError("locked item")
        total += item["value"]
    with store.transaction():
        for item_id in selected_ids:
            del store.items[item_id]
        store.add_materials(total)
    return total
