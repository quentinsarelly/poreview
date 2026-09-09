"""Thin wrapper around the Spring Systems Web API for pulling purchase orders.

Docs: https://springsystems.readme.io/reference/web-api-endpoint-reference

Only a single po.filter segment has been confirmed against the docs (attr/op/value).
Chaining multiple filters in one URL isn't documented, so callers needing multiple
conditions (e.g. retailer_id + a date cutoff) should filter client-side on the
result of a single server-side filter rather than assuming multi-filter URLs work.

Confirmed against the live production API (2026-07-24): despite no format param
being documented, responses come back as XML (Content-Type: text/xml), not JSON.
Results are paginated via an X-Pagination response header (JSON-encoded) rather
than a body field.
"""

import json
from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree

import requests


@dataclass
class SpringSystemsClient:
    base_url: str
    api_user: str
    api_key: str
    session: requests.Session = field(default_factory=requests.Session)

    def get_pos(self, attr: str, op: str, value: str) -> list[dict[str, Any]]:
        """Fetch all POs matching a single filter condition, e.g. attr="retailer_id",
        op="eq", following pagination until exhausted."""
        url: str | None = (
            f"{self.base_url.rstrip('/')}/po-outgoing/export/"
            f"po.filter.{op}.{attr}/{value}/"
            f"api_user/{self.api_user}/api_key/{self.api_key}"
        )
        pos: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        while url and url not in seen_urls:
            seen_urls.add(url)
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            pos.extend(_parse_pos_xml(response.text))
            url = _next_page_url(response.headers)
        return pos

    def get_pos_for_retailer(self, retailer_id: str) -> list[dict[str, Any]]:
        return self.get_pos("retailer_id", "eq", retailer_id)

    def get_po_by_num(self, po_num: str) -> dict[str, Any] | None:
        matches = self.get_pos("po_num", "eq", po_num)
        return matches[0] if matches else None

    def get_invoices(self, attr: str, op: str, value: str) -> list[dict[str, Any]]:
        """Fetch all invoices matching a single filter condition, e.g.
        attr="invoice_created", op="gte", value="2026-08-14", following pagination
        until exhausted. Same URL-embedded api_user/api_key auth as get_pos -- the
        export/GET endpoints use that pattern (confirmed live), distinct from the
        Basic-auth pattern the POST /send/ endpoints document."""
        url: str | None = (
            f"{self.base_url.rstrip('/')}/invoice-outgoing/export/"
            f"invoice.filter.{op}.{attr}/{value}/"
            f"api_user/{self.api_user}/api_key/{self.api_key}"
        )
        invoices: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        while url and url not in seen_urls:
            seen_urls.add(url)
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            invoices.extend(_parse_invoices_xml(response.text))
            url = _next_page_url(response.headers)
        return invoices

    def get_invoices_created_since(self, date: str) -> list[dict[str, Any]]:
        """date: YYYY-MM-DD. Returns invoices created on or after that date."""
        return self.get_invoices("invoice_created", "gte", date)

    def create_invoice(
        self,
        po: dict[str, Any],
        invoice_num: str,
        vendor_tp_id: str,
        invoice_date: str | None = None,
        *,
        qty_overrides: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """Create/send an invoice for a PO's line items.

        qty_overrides: if provided, invoice only items in this map using these
        quantities (SKU → qty). Items not in the map are skipped.

        WARNING -- unconfirmed draft-vs-send behavior: Spring's docs do not document
        any way to create a "draft" invoice distinct from one that's immediately
        transmitted (EDI 810) to the retailer. This hits the same /send/-style
        endpoint used to create/acknowledge POs, so it may transmit the moment this
        is called. Confirm actual behavior with Spring Systems support, or with a
        deliberate low-stakes real test, before relying on this for anything beyond
        --dry-run.

        invoice_date placement (<invoice_additional><attributes><invoice_date>) is
        inferred from the export/GET schema (springsystems.readme.io/reference/
        invoice-sample-data), not from a confirmed request example -- the live "Try
        It" example for this endpoint didn't include a date field at all. Verify
        this lands correctly on the created invoice before trusting it.
        """
        invoices_xml = build_invoice_request_xml(
            po, invoice_num, vendor_tp_id, invoice_date, qty_overrides=qty_overrides
        )
        url = f"{self.base_url.rstrip('/')}/invoice-incoming/send"
        response = self.session.post(
            url,
            data=ElementTree.tostring(invoices_xml, encoding="unicode"),
            auth=(self.api_user, self.api_key),
            headers={"Content-Type": "application/xml"},
            timeout=30,
        )
        response.raise_for_status()
        return _parse_invoice_send_response(response.text)


def _next_page_url(headers: dict[str, Any]) -> str | None:
    raw = headers.get("X-Pagination")
    if not raw:
        return None
    try:
        pagination = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return pagination.get("next_page") or None


def _text(el: ElementTree.Element | None, tag: str) -> str | None:
    return el.findtext(tag) if el is not None else None


def _parse_pos_xml(xml_text: str) -> list[dict[str, Any]]:
    root = ElementTree.fromstring(xml_text)
    return [_parse_po_element(po_el) for po_el in root.findall("po")]


def _parse_po_element(po_el: ElementTree.Element) -> dict[str, Any]:
    retailer_el = po_el.find("retailer")
    po_items_el = po_el.find("po_items")
    items: list[dict[str, Any]] = []
    if po_items_el is not None:
        for item_el in po_items_el.findall("po_item"):
            if len(item_el) == 0:
                # Spring sometimes returns an empty <po_item/> placeholder when line
                # item data hasn't synced yet - nothing to compare, so skip it.
                continue
            product_el = item_el.find("product")
            items.append(
                {
                    "po_item_id": _text(item_el, "po_item_id"),
                    "po_item_qty_ordered": _text(item_el, "po_item_qty_ordered"),
                    "po_item_unit_price": _text(item_el, "po_item_unit_price"),
                    "po_item_buyer_item_num": _text(item_el, "po_item_buyer_item_num"),
                    "product": {
                        "product_vendor_item_num": _text(
                            product_el, "product_vendor_item_num"
                        ),
                    },
                }
            )
    return {
        "po_id": _text(po_el, "po_id"),
        "po_num": _text(po_el, "po_num"),
        "po_acknowledge_status": _text(po_el, "po_acknowledge_status"),
        # Timestamp the ASN was transmitted ("2026-08-18 18:13:25"), not a declared
        # ship date -- Spring exposes no ASN export endpoint (asn-outgoing,
        # shipment-outgoing etc. all 404). Used only to cross-check Camelot's
        # ship_date, never as the primary source. See invoicing.py.
        "po_last_asn_date": _text(po_el, "po_last_asn_date"),
        "retailer_id": _text(po_el, "retailer_id"),
        "retailer": {"retailer_name": _text(retailer_el, "tp_name")},
        "po_items": {"po_item": items},
    }


def get_line_items(po: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize a PO's nested po_items.po_item into a list."""
    item = po.get("po_items", {}).get("po_item", [])
    if isinstance(item, dict):
        return [item]
    return item or []


def _parse_invoice_element(invoice_el: ElementTree.Element) -> dict[str, Any]:
    retailer_el = invoice_el.find("retailer")
    return {
        "invoice_id": _text(invoice_el, "invoice_id"),
        "invoice_num": _text(invoice_el, "invoice_num"),
        "invoice_amount": _text(invoice_el, "invoice_amount"),
        "invoice_status": _text(invoice_el, "invoice_status"),
        "invoice_created": _text(invoice_el, "invoice_created"),
        # Which PO this invoice was raised against. Needed to tell "this PO is
        # already invoiced" apart from "a different PO took that number" --
        # the invoice endpoint silently returns 0 results for a po_num/po_id
        # filter (HTTP 200, empty list) rather than erroring, so filtering by
        # PO is not an option and the link has to be read off the invoice.
        "po_num": _text(invoice_el, "invoice_po/po/po_num"),
        "po_id": _text(invoice_el, "invoice_po/po_id"),
        "retailer_id": _text(invoice_el, "retailer_id"),
        "retailer": {"retailer_name": _text(retailer_el, "tp_name")},
    }


def _parse_invoices_xml(xml_text: str) -> list[dict[str, Any]]:
    root = ElementTree.fromstring(xml_text)
    return [_parse_invoice_element(el) for el in root.findall("invoice")]


def build_invoice_request_xml(
    po: dict[str, Any],
    invoice_num: str,
    vendor_tp_id: str,
    invoice_date: str | None,
    *,
    qty_overrides: dict[str, float] | None = None,
) -> ElementTree.Element:
    """Build invoice XML for Spring Systems API.

    qty_overrides: if provided, invoice only items in this map using these
    quantities (SKU → qty). Items not in the map are skipped.
    """
    line_items = get_line_items(po)
    if not line_items:
        raise ValueError(f"PO {po.get('po_num')!r} has no line items to invoice.")

    # Filter items if qty_overrides is provided
    if qty_overrides is not None:
        filtered_items = []
        for item in line_items:
            sku = str(item.get("product", {}).get("product_vendor_item_num", "")).strip()
            if sku in qty_overrides:
                filtered_items.append(item)
        line_items = filtered_items
        if not line_items:
            raise ValueError(
                f"PO {po.get('po_num')!r} has no line items matching the qty_overrides."
            )

    missing_ids = [i for i, item in enumerate(line_items) if not item.get("po_item_id")]
    if missing_ids:
        raise ValueError(
            f"PO {po.get('po_num')!r} line item(s) at index {missing_ids} are missing "
            "po_item_id -- required to invoice against them. (POs loaded via --from-csv "
            "never have this; fetch the PO from the live API instead.)"
        )

    # Calculate total using overrides if provided
    total = 0.0
    for item in line_items:
        sku = str(item.get("product", {}).get("product_vendor_item_num", "")).strip()
        qty = (
            qty_overrides[sku]
            if qty_overrides is not None
            else float(item.get("po_item_qty_ordered", 0) or 0)
        )
        price = float(item.get("po_item_unit_price", 0) or 0)
        total += qty * price

    invoices_el = ElementTree.Element("invoices")
    invoice_el = ElementTree.SubElement(invoices_el, "invoice")
    ElementTree.SubElement(invoice_el, "invoice_num").text = invoice_num
    ElementTree.SubElement(invoice_el, "invoice_amount").text = f"{total:.2f}"
    ElementTree.SubElement(ElementTree.SubElement(invoice_el, "vendor"), "tp_id").text = str(vendor_tp_id)
    ElementTree.SubElement(ElementTree.SubElement(invoice_el, "retailer"), "tp_id").text = str(
        po.get("retailer_id", "")
    )
    if invoice_date:
        attrs_el = ElementTree.SubElement(ElementTree.SubElement(invoice_el, "invoice_additional"), "attributes")
        ElementTree.SubElement(attrs_el, "invoice_date").text = invoice_date

    invoice_po_el = ElementTree.SubElement(invoice_el, "invoice_po")
    ElementTree.SubElement(invoice_po_el, "po_id").text = str(po.get("po_id", ""))
    for item in line_items:
        sku = str(item.get("product", {}).get("product_vendor_item_num", "")).strip()
        if qty_overrides is not None:
            # Format as integer if whole number, otherwise keep decimals
            qty_val = qty_overrides[sku]
            qty_str = str(int(qty_val)) if qty_val == int(qty_val) else str(qty_val)
        else:
            qty_str = str(item.get("po_item_qty_ordered", ""))
        item_el = ElementTree.SubElement(invoice_po_el, "invoice_po_item")
        ElementTree.SubElement(item_el, "po_item_id").text = str(item["po_item_id"])
        ElementTree.SubElement(item_el, "invoice_po_item_qty").text = qty_str
        ElementTree.SubElement(item_el, "invoice_po_item_price").text = str(item.get("po_item_unit_price", ""))

    return invoices_el


def _parse_invoice_send_response(xml_text: str) -> dict[str, Any]:
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as e:
        # Show what Spring actually returned (truncated for readability)
        preview = xml_text[:500] if len(xml_text) > 500 else xml_text
        raise RuntimeError(f"Spring returned invalid XML: {e}\nResponse: {preview!r}") from e
    errors_el = root.find("errors")
    if errors_el is not None:
        raise RuntimeError(f"Spring invoice send failed: {(errors_el.text or '').strip()}")
    invoice_el = root.find("invoice")
    if invoice_el is None:
        raise RuntimeError(f"Spring invoice send returned no <invoice>: {xml_text}")
    return {
        "invoice_id": _text(invoice_el, "invoice_id"),
        "invoice_num": _text(invoice_el, "invoice_num"),
        "invoice_amount": _text(invoice_el, "invoice_amount"),
        "invoice_status": _text(invoice_el, "invoice_status"),
    }
