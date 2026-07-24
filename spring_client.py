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
