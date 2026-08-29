import json
import time

import requests

from compare import LineStatus, POResult, POStatus
from compare_shipment import ShipmentLineStatus, ShipmentResult, ShipmentStatus

# action_id of the /po-invoice confirmation button, shared with slack_listener.
INVOICE_CONFIRM_ACTION = "po_invoice_confirm"

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


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def format_invoice_preparation(prep) -> tuple[str, list[dict]]:
    """Renders a workflow.InvoicePreparation as (fallback_text, blocks).

    The confirm button is only attached when nothing is blocking. Its value
    carries the derived number and date, but the handler re-derives them from
    scratch rather than trusting the payload -- see slack_listener.
    """
    ready = prep.ready
    header = (
        f":white_check_mark: *PO {prep.po_num}* passed all checks — ready to invoice"
        if ready
        else f":rotating_light: *PO {prep.po_num}* is not ready to invoice"
    )
    blocks = [
        _section(header),
        _section(format_summary(prep.pricing)),
        _section(format_shipment_summary(prep.shipment)),
    ]

    if prep.invoice_num and prep.ship_date:
        detail = [
            f"*Invoice number* `{prep.invoice_num}`  _(derived)_",
            f"*Invoice date* `{prep.invoice_date}`  _(Camelot ship date)_",
            f"*Total* {prep.total:,.2f}",
        ]
        skipped = prep.number_resolution.skipped if prep.number_resolution else []
        if skipped:
            detail.append(
                ":information_source: _"
                + "; ".join(skipped)
                + f" — using `{prep.invoice_num}` instead. Another PO shipped to the "
                "same DC on the same day._"
            )
        check = prep.ship_date_check
        if check and check.spring_asn_date:
            detail.append(
                f"_Ship date cross-checked against Spring's ASN "
                f"({check.spring_asn_raw}) — agrees._"
            )
        else:
            detail.append(
                "_Spring has no ASN date to cross-check against; using Camelot's "
                "ship date alone._"
            )
        blocks.append(_section("\n".join(detail)))

    if prep.blockers:
        blocks.append(
            _section(
                "*Blocking:*\n" + "\n".join(f"• {b}" for b in prep.blockers)
            )
        )

    # invoice_num is always set when nothing blocks, but attaching a confirm
    # button with a null number would be worse than showing none at all.
    if ready and prep.invoice_num:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": INVOICE_CONFIRM_ACTION,
                        "style": "primary",
                        "text": {"type": "plain_text", "text": "Create invoices"},
                        "value": json.dumps(
                            {
                                "po_num": prep.po_num,
                                "shipment_id": prep.shipment_id,
                                "invoice_num": prep.invoice_num,
                                "invoice_date": prep.invoice_date,
                            }
                        ),
                        "confirm": {
                            "title": {"type": "plain_text", "text": "Create invoices?"},
                            "text": {
                                "type": "mrkdwn",
                                "text": (
                                    f"Creates a draft invoice in Odoo for *{prep.po_num}* "
                                    f"as `{prep.invoice_num}` dated `{prep.invoice_date}`, "
                                    "and submits it to Spring if that leg is enabled. "
                                    "The Spring step may transmit an EDI 810 to Target "
                                    "and cannot be undone."
                                ),
                            },
                            "confirm": {"type": "plain_text", "text": "Create"},
                            "deny": {"type": "plain_text", "text": "Cancel"},
                        },
                    }
                ],
            }
        )

    fallback = f"PO {prep.po_num} — {'ready to invoice' if ready else 'not ready to invoice'}"
    return fallback, blocks


def format_invoicing_outcome(outcome) -> str:
    """Renders a workflow.InvoicingOutcome."""
    lines = [
        f":white_check_mark: *{outcome.invoice_num}* dated `{outcome.invoice_date}`",
        f":page_facing_up: Odoo DRAFT invoice created (account.move `{outcome.odoo_move_id}`) "
        "— not posted, review and post it in Odoo.",
    ]
    if outcome.spring_skipped:
        lines.append(f":pause_button: Spring skipped — {outcome.spring_skipped}")
    elif outcome.spring_error:
        lines.append(
            f":rotating_light: Spring invoice FAILED — {outcome.spring_error}\n"
            "The Odoo draft above was still created; the Spring invoice was not."
        )
    elif outcome.spring_result:
        result = outcome.spring_result
        lines.append(
            f":outbox_tray: Spring invoice created — id `{result.get('invoice_id')}`, "
            f"status `{result.get('invoice_status')}`, amount {result.get('invoice_amount')}"
        )
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
