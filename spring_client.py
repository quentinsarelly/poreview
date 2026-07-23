"""Thin wrapper around the Spring Systems Web API for pulling purchase orders.

Docs: https://springsystems.readme.io/reference/web-api-endpoint-reference

Only a single po.filter segment has been confirmed against the docs (attr/op/value).
Chaining multiple filters in one URL isn't documented, so callers needing multiple
conditions (e.g. retailer_id + a date cutoff) should filter client-side on the
result of a single server-side filter rather than assuming multi-filter URLs work.
"""

from dataclasses import dataclass, field
from typing import Any

import requests


@dataclass
class SpringSystemsClient:
    base_url: str
    api_user: str
    api_key: str
    session: requests.Session = field(default_factory=requests.Session)

    def get_pos(self, attr: str, op: str, value: str) -> list[dict[str, Any]]:
        """Fetch POs matching a single filter condition, e.g. attr="retailer_id", op="eq"."""
        url = (
            f"{self.base_url.rstrip('/')}/po-outgoing/export/"
            f"po.filter.{op}.{attr}/{value}/"
            f"api_user/{self.api_user}/api_key/{self.api_key}"
        )
        response = self.session.get(url, timeout=30)
        response.raise_for_status()
        return _extract_pos(response.json())

    def get_pos_for_retailer(self, retailer_id: str) -> list[dict[str, Any]]:
        return self.get_pos("retailer_id", "eq", retailer_id)


def _extract_pos(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize the {"pos": {"po": [...]}} response, handling the single-result
    case where the API may return a dict instead of a one-item list."""
    po = payload.get("pos", {}).get("po", [])
    if isinstance(po, dict):
        return [po]
    return po or []


def get_line_items(po: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize a PO's nested po_items.po_item into a list."""
    item = po.get("po_items", {}).get("po_item", [])
    if isinstance(item, dict):
        return [item]
    return item or []
