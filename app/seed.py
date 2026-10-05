ORDERS = [
    {"order_id": "ORD-1001", "phone": "919999900001", "status": "shipped", "carrier": "BlueDart",
     "tracking_id": "BD123456", "eta": "2026-10-08", "items": ["Acme Buds Pro"], "total_inr": 2499},
    {"order_id": "ORD-1002", "phone": "919999900002", "status": "processing", "carrier": None,
     "tracking_id": None, "eta": "2026-10-11", "items": ["Acme Charger 65W", "USB-C Cable"], "total_inr": 1798},
    {"order_id": "ORD-1003", "phone": "919999900001", "status": "delivered", "carrier": "Delhivery",
     "tracking_id": "DL998877", "eta": "2026-09-28", "items": ["Acme Power Bank"], "total_inr": 1599},
]


def seed_orders(store) -> None:
    for o in ORDERS:
        store.put_order(o)
