"""Loads PO(s) from a flattened CSV export (Spring Systems' export format: one
row per line item, PO-level fields repeated on every row) and reshapes them
into the same nested dict structure the Spring Systems API returns, so they
flow through evaluate_po/slack_notify exactly like a real API fetch would.

This exists to let the pricing-review logic be tested end-to-end before
Spring Systems API credentials are available.
"""

import csv
from collections import OrderedDict
from typing import Any


def load_pos_from_csv(path: str) -> list[dict[str, Any]]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [row for row in csv.DictReader(f) if row.get("po.po_num")]

    grouped: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    for row in rows:
        po_num = row["po.po_num"]
        po = grouped.setdefault(
            po_num,
            {
                "po_id": po_num,
                "po_num": po_num,
                "po_acknowledge_status": row.get("po.po_acknowledge_status"),
                "retailer": {"retailer_name": row.get("retailer.tp_name", "")},
                "po_items": {"po_item": []},
            },
        )
        po["po_items"]["po_item"].append(
            {
                "po_item_line_num": row.get("po_item.po_item_line_num"),
                "po_item_qty_ordered": row.get("po_item.po_item_qty_ordered"),
                "po_item_unit_price": row.get("po_item.po_item_unit_price"),
                "po_item_buyer_item_num": row.get("po_item.po_item_buyer_item_num"),
                "product": {
                    "product_vendor_item_num": row.get("product.product_vendor_item_num"),
                    "product_gtin": row.get("product.product_gtin"),
                },
            }
        )
    return list(grouped.values())
