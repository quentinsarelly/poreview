"""Compares a PO's ordered quantities (Spring Systems) against what Camelot's
WMS actually shipped, mirroring compare.py's pricing-check pattern but for
quantities -- meant to run after a shipment posts and before invoicing.

The SKU join (Camelot ItemNumber <-> Spring product_vendor_item_num) is
confirmed to match exactly, validated end-to-end against a real Target PO
(2026-08-04: PO 10001964460-3841 / Camelot shipment S0461276, 12/12 lines
matched on both SKU and quantity).

Caller's responsibility: get the shipment dict from
camelot_client.get_shipment_detail(shipment_id), not get_shipment_for_po --
there's currently no reliable way to look up a Target shipment by PO # alone
(see camelot_client.py's module docstring), so the Camelot shipment ID has
to be supplied manually for now.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any

from spring_client import get_line_items


class ShipmentLineStatus(str, Enum):
    OK = "OK"
    QTY_MISMATCH = "QTY_MISMATCH"
    NOT_SHIPPED = "NOT_SHIPPED"  # ordered on the PO, no matching line in the shipment
    UNEXPECTED_ITEM = "UNEXPECTED_ITEM"  # shipped, but not found on the PO


class ShipmentStatus(str, Enum):
    ALL_MATCH = "ALL_MATCH"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    NOT_YET_SHIPPED = "NOT_YET_SHIPPED"  # no Camelot shipment found for this PO yet


@dataclass
class ShipmentLineResult:
    sku: str
    qty_ordered: float
    qty_shipped: float
    status: ShipmentLineStatus

    @property
    def delta(self) -> float:
        return round(self.qty_shipped - self.qty_ordered, 4)


@dataclass
class ShipmentResult:
    po_num: str
    shipment_id: str | None
    order_status: str | None
    ship_date: str | None
    lines: list[ShipmentLineResult]

    @property
    def status(self) -> ShipmentStatus:
        if self.shipment_id is None:
            return ShipmentStatus.NOT_YET_SHIPPED
        if any(line.status != ShipmentLineStatus.OK for line in self.lines):
            return ShipmentStatus.NEEDS_REVIEW
        return ShipmentStatus.ALL_MATCH


def evaluate_shipment(
    po: dict[str, Any], shipment: dict[str, Any] | None
) -> ShipmentResult:
    po_num = str(po.get("po_num", ""))

    if shipment is None:
        return ShipmentResult(
            po_num=po_num, shipment_id=None, order_status=None, ship_date=None, lines=[]
        )

    ordered: dict[str, float] = {}
    for item in get_line_items(po):
        sku = str(item.get("product", {}).get("product_vendor_item_num", "")).strip()
        if sku:
            ordered[sku] = ordered.get(sku, 0.0) + float(
                item.get("po_item_qty_ordered", 0) or 0
            )

    shipped: dict[str, float] = {}
    for line in shipment.get("lines", []):
        sku = str(line.get("item_number", "")).strip()
        if sku:
            shipped[sku] = shipped.get(sku, 0.0) + float(line.get("qty_shipped", 0) or 0)

    lines: list[ShipmentLineResult] = []
    for sku in sorted(set(ordered) | set(shipped)):
        qty_ordered = ordered.get(sku, 0.0)
        qty_shipped = shipped.get(sku, 0.0)

        if sku not in shipped:
            status = ShipmentLineStatus.NOT_SHIPPED
        elif sku not in ordered:
            status = ShipmentLineStatus.UNEXPECTED_ITEM
        elif round(qty_ordered, 4) != round(qty_shipped, 4):
            status = ShipmentLineStatus.QTY_MISMATCH
        else:
            status = ShipmentLineStatus.OK

        lines.append(
            ShipmentLineResult(
                sku=sku, qty_ordered=qty_ordered, qty_shipped=qty_shipped, status=status
            )
        )

    return ShipmentResult(
        po_num=po_num,
        shipment_id=shipment.get("shipment_id"),
        order_status=shipment.get("order_status"),
        ship_date=shipment.get("ship_date"),
        lines=lines,
    )
