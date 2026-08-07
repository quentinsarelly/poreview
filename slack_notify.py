import time

import requests

from compare import LineStatus, POResult, POStatus
from compare_shipment import ShipmentLineStatus, ShipmentResult, ShipmentStatus

_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 2

_STATUS_EMOJI = {
    LineStatus.OK: ":white_check_mark:",
    LineStatus.PRICE_MISMATCH: ":warning:",
    LineStatus.SKU_NOT_FOUND: ":question:",
}

_SHIPMENT_STATUS_EMOJI = {
    ShipmentStatus.ALL_MATCH: ":white_check_mark:",
    ShipmentStatus.NEEDS_REVIEW: ":rotating_light:",
    ShipmentStatus.NOT_YET_SHIPPED: ":hourglass_flowing_sand:",
    ShipmentStatus.AWAITING_CONFIRMATION: ":package:",
}

_SHIPMENT_LINE_EMOJI = {
    ShipmentLineStatus.OK: ":white_check_mark:",
    ShipmentLineStatus.QTY_MISMATCH: ":warning:",
    ShipmentLineStatus.NOT_SHIPPED: ":x:",
    ShipmentLineStatus.UNEXPECTED_ITEM: ":question:",
}


def format_summary(result: POResult) -> str:
    header_emoji = ":white_check_mark:" if result.status == POStatus.ALL_MATCH else ":rotating_light:"
    lines = [
        f"{header_emoji} *PO {result.po_num}* ({result.retailer_name}) — {result.status.value}",
    ]
    if result.status == POStatus.NO_LINE_ITEMS:
        lines.append(
            "_No line item data synced from Spring Systems yet — nothing to compare. "
            "Re-run once the PO has line items._"
        )
        return "\n".join(lines)
    for line in result.lines:
        emoji = _STATUS_EMOJI[line.status]
        if line.status == LineStatus.SKU_NOT_FOUND:
            lines.append(
                f"{emoji} SKU `{line.sku or '(blank)'}` (retailer item# `{line.retailer_item_num}`) "
                f"— not found in price list (PO price: {line.price_ordered:.2f}, qty {line.qty_ordered})"
            )
        elif line.status == LineStatus.PRICE_MISMATCH:
            lines.append(
                f"{emoji} SKU `{line.sku}` — PO price {line.price_ordered:.2f} "
                f"vs expected {line.price_expected:.2f} (delta {line.delta:+.2f}), "
                f"qty {line.qty_ordered}"
            )
        else:
            lines.append(
                f"{emoji} SKU `{line.sku}` — {line.price_ordered:.2f}, qty {line.qty_ordered}"
            )
    return "\n".join(lines)


def format_shipment_summary(result: ShipmentResult) -> str:
    header_emoji = _SHIPMENT_STATUS_EMOJI[result.status]
    lines = [f"{header_emoji} *PO {result.po_num}* shipment check — {result.status.value}"]

    if result.status == ShipmentStatus.NOT_YET_SHIPPED:
        lines.append("_No Camelot shipment found for that shipment ID._")
        return "\n".join(lines)

    if result.status == ShipmentStatus.AWAITING_CONFIRMATION:
        lines.append(
            f"Shipment `{result.shipment_id}` exists in Camelot (status "
            f"`{result.order_status}`) but has no line-level quantities yet — "
            "the warehouse hasn't run ship-confirm. Nothing to compare until "
            "that's done. :point_right: ping the warehouse to confirm this "
            "shipment in Camelot."
        )
        return "\n".join(lines)

    lines.append(
        f"Shipment `{result.shipment_id}` — status `{result.order_status}`, "
        f"shipped {result.ship_date or '(no ship date)'}"
    )
    for line in result.lines:
        emoji = _SHIPMENT_LINE_EMOJI[line.status]
        if line.status == ShipmentLineStatus.NOT_SHIPPED:
            lines.append(
                f"{emoji} SKU `{line.sku}` — ordered {line.qty_ordered:g}, "
                f"not found in the shipment"
            )
        elif line.status == ShipmentLineStatus.UNEXPECTED_ITEM:
            lines.append(
                f"{emoji} SKU `{line.sku}` — shipped {line.qty_shipped:g}, not on the PO"
            )
        elif line.status == ShipmentLineStatus.QTY_MISMATCH:
            lines.append(
                f"{emoji} SKU `{line.sku}` — ordered {line.qty_ordered:g}, "
                f"shipped {line.qty_shipped:g} (delta {line.delta:+g})"
            )
        else:
            lines.append(f"{emoji} SKU `{line.sku}` — qty {line.qty_shipped:g}")
    return "\n".join(lines)


def post_text(text: str, webhook_url: str) -> None:
    last_error: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            response = requests.post(webhook_url, json={"text": text}, timeout=15)
            response.raise_for_status()
            return
        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt < _MAX_ATTEMPTS:
                time.sleep(_RETRY_BACKOFF_SECONDS * attempt)
    raise last_error


def post_summary(result: POResult, webhook_url: str) -> None:
    post_text(format_summary(result), webhook_url)


def post_shipment_summary(result: ShipmentResult, webhook_url: str) -> None:
    post_text(format_shipment_summary(result), webhook_url)
