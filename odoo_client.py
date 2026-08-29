"""Thin wrapper around Odoo's external XML-RPC API for pushing invoices.

Docs: https://www.odoo.com/documentation/19.0/developer/reference/external_api.html

Odoo Online (SaaS, this account included) blocks password auth on this API --
it requires a dedicated API key (avatar -> My Profile -> Account Security ->
New API Key), used as the password in both `authenticate` and `execute_kw`.

Field mapping (company/journal/partner IDs, invoice ref/payment_reference
layout, the payment_term line's custom label) was reverse-engineered from a
real existing Target invoice created via the current manual/CSV process --
see the poreview project memory for the full writeup.
"""

import xmlrpc.client
from dataclasses import dataclass, field
from typing import Any


@dataclass
class OdooClient:
    url: str
    db: str
    login: str
    api_key: str
    uid: int = field(init=False, repr=False)
    _models: xmlrpc.client.ServerProxy = field(init=False, repr=False)

    def __post_init__(self) -> None:
        base = self.url.rstrip("/")
        common = xmlrpc.client.ServerProxy(f"{base}/xmlrpc/2/common")
        uid = common.authenticate(self.db, self.login, self.api_key, {})
        if not uid:
            raise RuntimeError(
                "Odoo authentication failed -- check ODOO_DB_NAME/ODOO_USER/ODOO_API_KEY."
            )
        self.uid = uid
        self._models = xmlrpc.client.ServerProxy(f"{base}/xmlrpc/2/object")

    def execute(self, model: str, method: str, *args: Any, **kwargs: Any) -> Any:
        return self._models.execute_kw(self.db, self.uid, self.api_key, model, method, list(args), kwargs)

    def find_product_id_by_sku(self, sku: str) -> int | None:
        """Exact match on default_code -- must be exact, not ilike/prefix. There are
        duplicate product.product records for the same physical item, one keyed on
        the SCL-XXXX SKU (correct) and a stale one keyed on the barcode as
        default_code -- an exact match on the SCL-style SKU string naturally avoids
        the stale duplicate, since its default_code is a 13-digit barcode instead."""
        ids = self.execute("product.product", "search", [["default_code", "=", sku]], limit=1)
        return ids[0] if ids else None

    def find_invoice_by_reference(self, invoice_num: str) -> dict[str, Any] | None:
        """Existing customer invoice carrying this invoice number, in any state
        (draft included), or None.

        Returns `ref` (which holds the PO number) alongside the id, because the
        caller has to tell "this PO is already invoiced" apart from "a different
        PO to the same DC already took this number" -- those need opposite
        responses. Also guards against a double-click on the Slack confirm
        button, since Odoo has no uniqueness constraint on payment_reference.
        """
        ids = self.execute(
            "account.move",
            "search",
            [["payment_reference", "=", invoice_num], ["move_type", "=", "out_invoice"]],
            limit=1,
        )
        if not ids:
            return None
        rows = self.execute(
            "account.move",
            "read",
            ids,
            fields=["id", "ref", "payment_reference", "state", "invoice_date", "amount_total"],
        )
        return rows[0] if rows else None

    def create_draft_invoice(
        self,
        *,
        company_id: int,
        journal_id: int,
        partner_id: int,
        po_num: str,
        invoice_num: str,
        invoice_date: str,
        lines: list[dict[str, Any]],
    ) -> int:
        """Creates an account.move (move_type=out_invoice). Lands in state='draft'
        by default -- this never calls action_post(), so nothing here posts to the
        ledger or becomes visible outside a draft-invoice view in Odoo.

        lines: [{"product_id": int, "quantity": float, "price_unit": float}, ...]
        """
        move_id = self.execute(
            "account.move",
            "create",
            {
                "move_type": "out_invoice",
                "company_id": company_id,
                "journal_id": journal_id,
                "partner_id": partner_id,
                "ref": po_num,
                "payment_reference": invoice_num,
                "invoice_date": invoice_date,
                "invoice_line_ids": [
                    (0, 0, {"product_id": line["product_id"], "quantity": line["quantity"], "price_unit": line["price_unit"]})
                    for line in lines
                ],
            },
        )
        self._label_receivable_line(move_id, po_num, invoice_num)
        return move_id

    def _label_receivable_line(self, move_id: int, po_num: str, invoice_num: str) -> None:
        """Matches the existing manual-process convention: the payment_term
        (receivable) line's name is "{po_num} - {invoice_num}" instead of Odoo's
        default "Payment terms: ..." label, so AR entries stay searchable by PO/
        invoice number the same way the existing (manually created) ones are."""
        line_ids = self.execute(
            "account.move.line",
            "search",
            [["move_id", "=", move_id], ["display_type", "=", "payment_term"]],
        )
        if line_ids:
            self.execute("account.move.line", "write", line_ids, {"name": f"{po_num} - {invoice_num}"})
