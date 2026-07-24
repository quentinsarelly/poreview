from dataclasses import dataclass
from enum import Enum
from typing import Any

from spring_client import get_line_items


class LineStatus(str, Enum):
    OK = "OK"
    PRICE_MISMATCH = "PRICE_MISMATCH"
    SKU_NOT_FOUND = "SKU_NOT_FOUND"


class POStatus(str, Enum):
    ALL_MATCH = "ALL_MATCH"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    NO_LINE_ITEMS = "NO_LINE_ITEMS"


@dataclass
class LineResult:
    sku: str
    retailer_item_num: str
    qty_ordered: float
    price_ordered: float
    price_expected: float | None
    status: LineStatus

    @property
    def delta(self) -> float | None:
        if self.price_expected is None:
            return None
        return round(self.price_ordered - self.price_expected, 4)


@dataclass
class POResult:
    po_id: str
    po_num: str
    retailer_name: str
    lines: list[LineResult]

    @property
    def status(self) -> POStatus:
        if not self.lines:
            return POStatus.NO_LINE_ITEMS
        if any(line.status != LineStatus.OK for line in self.lines):
            return POStatus.NEEDS_REVIEW
        return POStatus.ALL_MATCH


def evaluate_po(po: dict[str, Any], price_map: dict[str, float]) -> POResult:
    lines: list[LineResult] = []
    for item in get_line_items(po):
        # Our own SKU lives under the nested `product` object (product_vendor_item_num),
        # matching the price sheet. po_item_buyer_item_num is the *retailer's* internal
        # item number and is not a usable join key against our price list.
        sku = str(item.get("product", {}).get("product_vendor_item_num", "")).strip()
        retailer_item_num = str(item.get("po_item_buyer_item_num", "")).strip()
        qty_ordered = float(item.get("po_item_qty_ordered", 0) or 0)
        price_ordered = float(item.get("po_item_unit_price", 0) or 0)
        price_expected = price_map.get(sku)

        if price_expected is None:
            status = LineStatus.SKU_NOT_FOUND
        elif round(price_ordered, 4) != round(price_expected, 4):
            status = LineStatus.PRICE_MISMATCH
        else:
            status = LineStatus.OK

        lines.append(
            LineResult(
                sku=sku,
                retailer_item_num=retailer_item_num,
                qty_ordered=qty_ordered,
                price_ordered=price_ordered,
                price_expected=price_expected,
                status=status,
            )
        )

    retailer_name = po.get("retailer", {}).get("retailer_name") or po.get(
        "retailer_id", "Unknown retailer"
    )
    return POResult(
        po_id=str(po.get("po_id", "")),
        po_num=str(po.get("po_num", "")),
        retailer_name=str(retailer_name),
        lines=lines,
    )
