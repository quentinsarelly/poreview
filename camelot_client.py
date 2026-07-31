"""Thin wrapper around the Camelot 3PL SOAP API (Dynamics NAV/Excalibur) for
pulling shipment quantities, so a PO can be checked against what was actually
shipped before invoicing.

Docs: WP0726 - Camelot 3PL Software API Documentation (Excalibur SOAP service,
single Codeunit endpoint TPLWebServiceInt).

Confirmed against the live API (2026-07-30):
- The shipment-detail SOAP action is `GetOrderStatusDetail` -- NOT
  `GetOrderStatusDetailed`, which every other Camelot client wrapper in other
  Sarelly repos uses and which the server rejects outright
  (`Method "GetOrderStatusDetailed" is invalid!`). Confirmed via the live
  WSDL (`?wsdl` on the SOAP URL) -- check the WSDL directly if a method call
  ever comes back "invalid", rather than trusting other repos' wrappers.
- Which XMLPort/data shape a call returns is controlled by
  `pInterfaceProfile`, not by the SOAP action name. The inventory-only
  profile used elsewhere in other repos (SAR_ITEM_E) either errors or
  silently returns inventory data for shipment calls -- shipment calls need
  CAMELOT_SHIPMENT_PROFILE (`SAR_SHP_E` on this account).
- A retailer PO # is not a valid `pDocument` value -- Camelot has its own
  shipment ID (e.g. "S0459771"). The PO # is stored in the shipment's
  `OrderRefNumber` field, not `PurchOrderNumber` (confirmed empty on a real
  Target shipment). So looking up a shipment by PO # means pulling
  GetOrderStatusDateRange over a window and matching OrderRefNumber --
  there's no direct "look up by PO #" call.

NOT yet confirmed: whether Camelot's ItemNumber (the SKU on a shipped line)
matches product_vendor_item_num, the SKU convention compare.py already uses
to join Spring PO lines against the price list. See compare_shipment.py.
"""

from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree

import requests

_NS = "urn:microsoft-dynamics-schemas/codeunit/TPLWebServiceInt"
_NS_SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
_NS_SHIPMENT = "urn:microsoft-dynamics-nav/xmlports/x37036604"

_ENVELOPE = """\
<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <{action} xmlns="{ns}">
      {params}
    </{action}>
  </soap:Body>
</soap:Envelope>"""


class CamelotError(Exception):
    pass


@dataclass
class CamelotClient:
    soap_url: str
    username: str
    password: str
    client_code: str
    trading_partner: str
    shipment_profile: str
    session: requests.Session = field(default_factory=requests.Session)

    def _call(self, action: str, params: dict[str, Any]) -> ElementTree.Element:
        param_xml = "\n      ".join(f"<{k}>{v}</{k}>" for k, v in params.items())
        envelope = _ENVELOPE.format(action=action, ns=_NS, params=param_xml)
        response = self.session.post(
            self.soap_url,
            data=envelope.encode("utf-8"),
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": f'"{_NS}"',
            },
            auth=(self.username, self.password),
            timeout=45,
        )
        root = ElementTree.fromstring(response.content)
        body = root.find(f"{{{_NS_SOAP}}}Body")
        fault = body.find(f"{{{_NS_SOAP}}}Fault")
        if fault is not None:
            msg = fault.findtext("faultstring") or ElementTree.tostring(
                fault, encoding="unicode"
            )
            raise CamelotError(msg)
        response.raise_for_status()
        return list(body)[0]

    def _base_params(self) -> dict[str, str]:
        return {
            "pInterfaceProfile": self.shipment_profile,
            "pClient": self.client_code,
            "pTradingPartner": self.trading_partner,
            "pXMLDoc": "",
        }

    def get_order_status(self, document: str, doc_type: int = 0) -> dict[str, str]:
        """Profile-less tracking/status lookup by Camelot's own document number
        (not usable with a retailer PO # directly -- see module docstring)."""
        resp = self._call(
            "GetOrderStatus",
            {
                "pDocType": doc_type,
                "pDocument": document,
                "pTrackingNumber": "",
                "pStatus": "",
            },
        )
        return {
            "tracking_number": _text(resp, "pTrackingNumber"),
            "status": _text(resp, "pStatus"),
        }

    def find_shipment_id_for_po(
        self, po_num: str, begin_date: str, end_date: str
    ) -> str | None:
        """Search shipments in a date range (YYYY-MM-DD) and return the Camelot
        ShipmentID whose OrderRefNumber matches the given PO #, or None if no
        shipment in that window matches (including if it hasn't shipped yet)."""
        resp = self._call(
            "GetOrderStatusDateRange",
            {**self._base_params(), "pBeginDate": begin_date, "pEndDate": end_date},
        )
        xml_doc = _text(resp, "pXMLDoc")
        if not xml_doc:
            return None
        doc = ElementTree.fromstring(xml_doc)
        for advice in doc.findall(f"{{{_NS_SHIPMENT}}}ShipmentConfirmationAdvice"):
            ref = (advice.findtext(f"{{{_NS_SHIPMENT}}}OrderRefNumber") or "").strip()
            if ref == po_num:
                return advice.findtext(f"{{{_NS_SHIPMENT}}}ShipmentID")
        return None

    def get_shipment_detail(
        self, shipment_id: str, doc_type: int = 0
    ) -> dict[str, Any] | None:
        """Full shipment detail (status, ship date, one line per item shipped)
        for a Camelot shipment ID, as returned by find_shipment_id_for_po."""
        resp = self._call(
            "GetOrderStatusDetail",
            {**self._base_params(), "pDocType": doc_type, "pDocument": shipment_id},
        )
        xml_doc = _text(resp, "pXMLDoc")
        if not xml_doc:
            return None
        doc = ElementTree.fromstring(xml_doc)
        advice = doc.find(f"{{{_NS_SHIPMENT}}}ShipmentConfirmationAdvice")
        if advice is None:
            return None
        return _parse_shipment_advice(advice)

    def get_shipment_for_po(
        self, po_num: str, begin_date: str, end_date: str
    ) -> dict[str, Any] | None:
        """PO # -> Camelot ShipmentID -> full shipment detail, in one call."""
        shipment_id = self.find_shipment_id_for_po(po_num, begin_date, end_date)
        if shipment_id is None:
            return None
        return self.get_shipment_detail(shipment_id)


def _text(element: ElementTree.Element, tag: str) -> str:
    return (
        element.findtext(f".//{{{_NS}}}{tag}") or element.findtext(f".//{tag}") or ""
    )


def _parse_shipment_advice(advice: ElementTree.Element) -> dict[str, Any]:
    ns = _NS_SHIPMENT
    lines = []
    for line_el in advice.findall(f"{{{ns}}}ShipLine"):
        lines.append(
            {
                "item_number": (line_el.findtext(f"{{{ns}}}ItemNumber") or "").strip(),
                "qty_ordered": _to_float(line_el.findtext(f"{{{ns}}}QtyOrdered")),
                "qty_shipped": _to_float(line_el.findtext(f"{{{ns}}}QtyShipped")),
            }
        )
    return {
        "shipment_id": advice.findtext(f"{{{ns}}}ShipmentID") or "",
        "order_ref_number": (advice.findtext(f"{{{ns}}}OrderRefNumber") or "").strip(),
        "order_status": advice.findtext(f"{{{ns}}}OrderStatus") or "",
        "ship_date": advice.findtext(f"{{{ns}}}ShipDate") or "",
        "lines": lines,
    }


def _to_float(value: str | None) -> float:
    try:
        return float(value) if value else 0.0
    except ValueError:
        return 0.0
