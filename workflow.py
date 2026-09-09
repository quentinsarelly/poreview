"""Business logic shared by the CLI (main.py) and the Slack listener.

Both are thin adapters over this module: everything here returns a value or
raises WorkflowError, and nothing prints or calls sys.exit, so the same call
sequence can be rendered to a terminal or to a Slack message.

WorkflowError messages are written to be safe to show to an end user as-is
(no credentials, no tracebacks) -- callers should render str(e) directly
rather than reformatting it.

load_price_map() always re-reads the Google Sheet; Clients.price_map() is the
cached accessor (PRICE_CACHE_SECONDS, default 300s) and is what long-lived
callers should use. Callers evaluating many POs in one pass -- the batch run --
still load once and pass the map in explicitly.
"""

import threading
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from xml.etree import ElementTree

import csv_po
import invoicing
import price_list
from camelot_client import CamelotClient, CamelotError
from compare import POResult, POStatus, evaluate_po
from compare_shipment import ShipmentResult, ShipmentStatus, evaluate_shipment
from config import Config
from odoo_client import OdooClient
from spring_client import SpringSystemsClient, build_invoice_request_xml, get_line_items


class WorkflowError(Exception):
    """A condition the caller should report to the user -- not a bug."""


class PONotFound(WorkflowError):
    """Raised when a PO number doesn't resolve, so callers can render it
    differently from a genuine failure (Slack uses :question: not :warning:)."""


# (env var name, Config attribute) pairs, grouped by the integration that needs them.
_SPRING_ENV = (
    ("SPRING_API_BASE_URL", "spring_base_url"),
    ("SPRING_API_USER", "spring_api_user"),
    ("SPRING_API_KEY", "spring_api_key"),
)
_CAMELOT_ENV = (
    ("CAMELOT_SOAP_URL", "camelot_soap_url"),
    ("CAMELOT_USERNAME", "camelot_username"),
    ("CAMELOT_PASSWORD", "camelot_password"),
    ("CAMELOT_CLIENT", "camelot_client_code"),
    ("CAMELOT_TRADING_PARTNER", "camelot_trading_partner"),
    ("CAMELOT_SHIPMENT_PROFILE", "camelot_shipment_profile"),
)
_RETAILER_ENV = (("SPRING_RETAILER_ID", "spring_retailer_id"),)
_SLACK_BOT_ENV = (
    ("SLACK_BOT_TOKEN", "slack_bot_token"),
    ("SLACK_APP_TOKEN", "slack_app_token"),
)
_ODOO_ENV = (
    ("ODOO_DB_URL", "odoo_db_url"),
    ("ODOO_DB_NAME", "odoo_db_name"),
    ("ODOO_USER", "odoo_user"),
    ("ODOO_API_KEY", "odoo_api_key"),
    ("ODOO_COMPANY_ID", "odoo_company_id"),
    ("ODOO_JOURNAL_ID", "odoo_journal_id"),
    ("ODOO_TARGET_PARTNER_ID", "odoo_target_partner_id"),
)


def _require(config: Config, group: tuple[tuple[str, str], ...], hint: str = "") -> None:
    missing = [name for name, attr in group if not getattr(config, attr)]
    if missing:
        message = f"Missing required environment variable(s): {', '.join(missing)}."
        raise WorkflowError(f"{message} {hint}" if hint else message)


def require_spring_env(config: Config, hint: str = "") -> None:
    _require(config, _SPRING_ENV, hint)


def require_camelot_env(config: Config, hint: str = "") -> None:
    _require(config, _CAMELOT_ENV, hint)


def require_odoo_env(config: Config, hint: str = "") -> None:
    _require(config, _ODOO_ENV, hint)


def require_batch_env(config: Config, hint: str = "") -> None:
    """The batch "review new POs" run. SPRING_RETAILER_ID is required even when
    --retailer-id overrides it -- preserved from the pre-refactor behavior; it
    looks like an oversight, but changing it is a behavior change, not a move."""
    _require(config, _SPRING_ENV + _RETAILER_ENV, hint)


def require_listener_env(config: Config, hint: str = "") -> None:
    """Everything slack_listener.py needs to start, reported in one message so
    a fresh deployment sees every missing var at once instead of one per restart."""
    _require(config, _SPRING_ENV + _SLACK_BOT_ENV + _CAMELOT_ENV, hint)


def require_odoo_push_env(config: Config, hint: str = "") -> None:
    """Both groups at once, so a caller missing vars from each is told about
    all of them in one message rather than one group at a time."""
    _require(config, _SPRING_ENV + _ODOO_ENV, hint)


@dataclass
class Clients:
    """Lazily-built API clients, so a caller that only needs Spring never
    authenticates against Odoo (OdooClient authenticates on construction).

    Build one per CLI run; the Slack listener builds one at startup and reuses
    it, which keeps the underlying requests.Session connection pools warm.
    """

    config: Config
    _spring: SpringSystemsClient | None = field(default=None, repr=False)
    _camelot: CamelotClient | None = field(default=None, repr=False)
    _odoo: OdooClient | None = field(default=None, repr=False)
    _price_map: dict[str, float] | None = field(default=None, repr=False)
    _price_map_loaded_at: float = field(default=0.0, repr=False)
    _price_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def price_map(self) -> dict[str, float]:
        """Price list, re-read at most once per PRICE_CACHE_SECONDS.

        The listener handles commands on several threads and previously hit the
        Sheets API once per command; the lock means a burst of commands triggers
        one fetch rather than one each. A CLI run loads it at most once anyway,
        so this only matters for the long-lived process.
        """
        with self._price_lock:
            age = time.monotonic() - self._price_map_loaded_at
            if self._price_map is None or age > self.config.price_cache_seconds:
                self._price_map = load_price_map(self.config)
                self._price_map_loaded_at = time.monotonic()
            return self._price_map

    @property
    def spring(self) -> SpringSystemsClient:
        if self._spring is None:
            require_spring_env(self.config)
            self._spring = SpringSystemsClient(
                base_url=self.config.spring_base_url,
                api_user=self.config.spring_api_user,
                api_key=self.config.spring_api_key,
            )
        return self._spring

    @property
    def camelot(self) -> CamelotClient:
        if self._camelot is None:
            require_camelot_env(self.config)
            self._camelot = CamelotClient(
                soap_url=self.config.camelot_soap_url,
                username=self.config.camelot_username,
                password=self.config.camelot_password,
                client_code=self.config.camelot_client_code,
                trading_partner=self.config.camelot_trading_partner,
                shipment_profile=self.config.camelot_shipment_profile,
            )
        return self._camelot

    @property
    def odoo(self) -> OdooClient:
        if self._odoo is None:
            require_odoo_env(self.config)
            self._odoo = OdooClient(
                url=self.config.odoo_db_url,
                db=self.config.odoo_db_name,
                login=self.config.odoo_user,
                api_key=self.config.odoo_api_key,
            )
        return self._odoo


# --- PO lookup and review -------------------------------------------------


def fetch_po(clients: Clients, po_num: str, *, from_csv: str | None = None) -> dict[str, Any]:
    """Resolve a single PO by number, from the live API or a local CSV export."""
    if from_csv:
        po = next(
            (p for p in csv_po.load_pos_from_csv(from_csv) if str(p.get("po_num")) == po_num),
            None,
        )
    else:
        po = clients.spring.get_po_by_num(po_num)
    if po is None:
        raise PONotFound(f"PO {po_num} not found.")
    return po


def fetch_retailer_pos(clients: Clients, retailer_id: str) -> list[dict[str, Any]]:
    return clients.spring.get_pos_for_retailer(retailer_id)


def load_price_map(config: Config) -> dict[str, float]:
    return price_list.load_prices(
        sheet_id=config.google_sheet_id,
        credentials_path=config.google_credentials_path,
        token_path=config.google_token_path,
        worksheet_name=config.price_sheet_worksheet,
        sku_column=config.price_sheet_sku_column,
        price_column=config.price_sheet_price_column,
    )


def review_pricing(po: dict[str, Any], price_map: dict[str, float]) -> POResult:
    return evaluate_po(po, price_map)


def check_shipment_quantities(
    clients: Clients, po: dict[str, Any], shipment_id: str
) -> ShipmentResult:
    try:
        shipment = clients.camelot.get_shipment_detail(shipment_id)
    except CamelotError as e:
        # Usually a shipment ID that doesn't exist ("Document S9999999 not
        # found."), which is a typo in the Slack command, not a bug -- report it
        # as a message instead of a traceback.
        raise WorkflowError(f"Camelot rejected shipment ID {shipment_id!r}: {e}") from e
    return evaluate_shipment(po, shipment)


# --- Odoo draft invoices --------------------------------------------------


@dataclass
class OdooInvoiceLine:
    sku: str
    product_id: int
    quantity: float
    price_unit: float


@dataclass
class OdooInvoicePlan:
    """Everything needed to create the Odoo invoice, resolved but not yet sent
    -- so a dry run can render exactly what a real run would create."""

    po_num: str
    invoice_num: str
    invoice_date: str | None
    company_id: int
    journal_id: int
    partner_id: int
    lines: list[OdooInvoiceLine]

    @property
    def total(self) -> float:
        return sum(line.quantity * line.price_unit for line in self.lines)


def plan_odoo_invoice(
    clients: Clients,
    po: dict[str, Any],
    invoice_num: str,
    invoice_date: str | None,
    *,
    qty_overrides: dict[str, float] | None = None,
) -> OdooInvoicePlan:
    """Resolve each PO line's SKU to an Odoo product id. Raises rather than
    creating a partial invoice if any SKU is unknown to Odoo.

    qty_overrides: if provided, use these quantities instead of po_item_qty_ordered.
    Lines whose SKU is not in qty_overrides are skipped (partial invoice).
    """
    line_items = get_line_items(po)
    po_num = str(po.get("po_num", ""))
    if not line_items:
        raise WorkflowError(f"PO {po_num} has no line items to invoice.")

    odoo = clients.odoo
    lines: list[OdooInvoiceLine] = []
    missing_skus: list[str] = []
    for item in line_items:
        sku = str(item.get("product", {}).get("product_vendor_item_num", "")).strip()
        # If qty_overrides is set, skip lines not in the override map
        if qty_overrides is not None and sku not in qty_overrides:
            continue
        product_id = odoo.find_product_id_by_sku(sku)
        if product_id is None:
            missing_skus.append(sku)
            continue
        quantity = (
            qty_overrides[sku]
            if qty_overrides is not None
            else float(item.get("po_item_qty_ordered", 0) or 0)
        )
        lines.append(
            OdooInvoiceLine(
                sku=sku,
                product_id=product_id,
                quantity=quantity,
                price_unit=float(item.get("po_item_unit_price", 0) or 0),
            )
        )
    if missing_skus:
        raise WorkflowError(
            f"SKU(s) not found in Odoo (no product.product with that exact default_code): "
            f"{missing_skus}. Aborting -- fix the product records first."
        )

    config = clients.config
    return OdooInvoicePlan(
        po_num=po_num,
        invoice_num=invoice_num,
        invoice_date=invoice_date,
        company_id=int(config.odoo_company_id),
        journal_id=int(config.odoo_journal_id),
        partner_id=int(config.odoo_target_partner_id),
        lines=lines,
    )


def create_odoo_invoice(clients: Clients, plan: OdooInvoicePlan) -> int:
    """Creates the DRAFT invoice and returns its account.move id. Never posts."""
    return clients.odoo.create_draft_invoice(
        company_id=plan.company_id,
        journal_id=plan.journal_id,
        partner_id=plan.partner_id,
        po_num=plan.po_num,
        invoice_num=plan.invoice_num,
        invoice_date=plan.invoice_date,
        lines=[
            {"product_id": line.product_id, "quantity": line.quantity, "price_unit": line.price_unit}
            for line in plan.lines
        ],
    )


# --- Spring invoices ------------------------------------------------------


def build_spring_invoice_xml(
    config: Config,
    po: dict[str, Any],
    invoice_num: str,
    invoice_date: str | None,
    *,
    qty_overrides: dict[str, float] | None = None,
) -> ElementTree.Element:
    """The request body send_spring_invoice would POST. SPRING_VENDOR_ID isn't
    required just to preview it, so an unset one is rendered as a placeholder."""
    vendor_id = config.spring_vendor_id or "<SPRING_VENDOR_ID not set>"
    return build_invoice_request_xml(
        po, invoice_num, vendor_id, invoice_date, qty_overrides=qty_overrides
    )


def send_spring_invoice(
    clients: Clients,
    po: dict[str, Any],
    invoice_num: str,
    invoice_date: str | None,
    *,
    qty_overrides: dict[str, float] | None = None,
) -> dict[str, Any]:
    """WARNING: may transmit an EDI 810 to the retailer -- see
    SpringSystemsClient.create_invoice. Callers must confirm before calling.

    qty_overrides: if provided, invoice only items in this map using these
    quantities (SKU → qty). Items not in the map are skipped.
    """
    if not clients.config.spring_vendor_id:
        raise WorkflowError("SPRING_VENDOR_ID is not set. Set it in .env or use --dry-run.")
    return clients.spring.create_invoice(
        po,
        invoice_num,
        clients.config.spring_vendor_id,
        invoice_date=invoice_date,
        qty_overrides=qty_overrides,
    )


def list_invoices_since(clients: Clients, date_str: str) -> list[dict[str, Any]]:
    return clients.spring.get_invoices_created_since(date_str)


def find_spring_invoice(clients: Clients, invoice_num: str) -> dict[str, Any] | None:
    """Look an invoice up by number.

    Only invoice_num is usable as a filter here: the invoice endpoint accepts
    a po_num/po_id filter but silently answers HTTP 200 with an empty list
    instead of erroring, so filtering by PO would always look like "no invoice
    exists". Confirmed live 2026-08-29. The owning PO is read off the returned
    invoice (po_num) instead.
    """
    matches = clients.spring.get_invoices("invoice_num", "eq", invoice_num)
    return matches[0] if matches else None


@dataclass
class InvoiceNumberResolution:
    """Which invoice number a PO should use, or why it can't have one."""

    invoice_num: str | None = None
    # Set when this PO already has an invoice -- a re-run, not a collision.
    already_invoiced_as: str | None = None
    already_invoiced_where: list[str] = field(default_factory=list)
    # Numbers skipped because a *different* PO owns them.
    skipped: list[str] = field(default_factory=list)


def resolve_invoice_num(
    clients: Clients, po_num: str, base: str, *, check_odoo: bool = True
) -> InvoiceNumberResolution:
    """Walk base, base+B, base+C ... and return the first number free in both
    Spring and Odoo -- unless this PO already owns one, which stops the walk.

    That ordering is what makes re-runs safe. Allocating "the next free number"
    without first checking whether this PO already has one would hand a second
    number to a PO that was already invoiced, every single re-run.
    """
    resolution = InvoiceNumberResolution()

    for candidate in invoicing.invoice_num_candidates(base):
        owners: list[tuple[str, str, str]] = []  # (system, owning po_num, detail)

        spring_invoice = find_spring_invoice(clients, candidate)
        if spring_invoice:
            owners.append(
                (
                    "Spring",
                    str(spring_invoice.get("po_num") or ""),
                    f"Spring invoice_id={spring_invoice.get('invoice_id')} "
                    f"created {spring_invoice.get('invoice_created')}",
                )
            )

        if check_odoo:
            odoo_invoice = clients.odoo.find_invoice_by_reference(candidate)
            if odoo_invoice:
                owners.append(
                    (
                        "Odoo",
                        str(odoo_invoice.get("ref") or ""),
                        f"Odoo account.move={odoo_invoice.get('id')} "
                        f"({odoo_invoice.get('state')})",
                    )
                )

        if not owners:
            resolution.invoice_num = candidate
            return resolution

        mine = [o for o in owners if o[1] == po_num]
        if mine:
            resolution.already_invoiced_as = candidate
            resolution.already_invoiced_where = [detail for _, _, detail in mine]
            return resolution

        resolution.skipped.append(
            f"{candidate} (taken by PO {owners[0][1] or 'unknown'})"
        )

    raise WorkflowError(
        f"Every invoice number from {base} through {base}Z is already in use. "
        "That's 26 invoices to the same DC on the same ship date -- check for a "
        "derivation problem before forcing this through."
    )


# --- /po-invoice: check everything, then derive ---------------------------


@dataclass
class InvoicePreparation:
    """The full result of checking a PO+shipment: what was verified, what it
    would invoice as, and everything standing in the way. Never invoices
    anything itself -- execute_invoicing() does that, only once ready."""

    po: dict[str, Any]
    po_num: str
    shipment_id: str
    pricing: POResult
    shipment: ShipmentResult
    ship_date_check: invoicing.ShipDateCheck | None
    invoice_num: str | None
    total: float
    blockers: list[str]
    # How invoice_num was arrived at -- which numbers were skipped because
    # another PO owns them, or which number this PO already holds.
    number_resolution: "InvoiceNumberResolution | None" = None
    # Partial shipment: when some items weren't shipped, allow invoicing only
    # what was shipped. partial_total is the sum using shipped quantities.
    partial_shipment_allowed: bool = False
    partial_total: float | None = None

    @property
    def ready(self) -> bool:
        return not self.blockers

    @property
    def ship_date(self) -> date | None:
        return self.ship_date_check.ship_date if self.ship_date_check else None

    @property
    def invoice_date(self) -> str | None:
        ship_date = self.ship_date
        return ship_date.isoformat() if ship_date else None


def prepare_invoice(
    clients: Clients,
    po_num: str,
    shipment_id: str,
    *,
    price_map: dict[str, float] | None = None,
) -> InvoicePreparation:
    """Price check + shipment check + date/number derivation + duplicate checks.

    Read-only: every blocker is collected rather than raised on the first
    failure, so one Slack message can show everything that needs fixing.
    """
    po = fetch_po(clients, po_num)
    if price_map is None:
        price_map = clients.price_map()

    pricing = review_pricing(po, price_map)
    shipment = check_shipment_quantities(clients, po, shipment_id)

    blockers: list[str] = []
    if pricing.status != POStatus.ALL_MATCH:
        blockers.append(f"Pricing check is {pricing.status.value}, not ALL_MATCH.")
    if shipment.status != ShipmentStatus.ALL_MATCH:
        blockers.append(f"Shipment check is {shipment.status.value}, not ALL_MATCH.")

    ship_date_check: invoicing.ShipDateCheck | None = None
    invoice_num: str | None = None
    try:
        ship_date_check = invoicing.check_ship_date(
            shipment.ship_date, po.get("po_last_asn_date")
        )
        if ship_date_check.agrees:
            invoice_num = invoicing.derive_invoice_num(po_num, ship_date_check.ship_date)
        else:
            blockers.append(
                f"Ship date sources disagree -- {ship_date_check.disagreement}. "
                "Resolve which is right before invoicing: this date sets both the "
                "invoice date and the invoice number."
            )
    except invoicing.DerivationError as e:
        blockers.append(str(e))

    resolution: InvoiceNumberResolution | None = None
    if invoice_num:
        base = invoice_num
        try:
            require_odoo_env(clients.config)
            check_odoo = True
        except WorkflowError as e:
            # Without Odoo we can't see half the picture, so don't hand out a
            # number that might already be taken there -- block instead.
            blockers.append(str(e))
            check_odoo = False

        if check_odoo:
            resolution = resolve_invoice_num(clients, po_num, base, check_odoo=True)
            if resolution.already_invoiced_as:
                invoice_num = None
                blockers.append(
                    f"PO {po_num} is already invoiced as "
                    f"{resolution.already_invoiced_as} "
                    f"({'; '.join(resolution.already_invoiced_where)}). "
                    "Nothing to do -- reconcile manually if that's not what you expect."
                )
            else:
                invoice_num = resolution.invoice_num
        else:
            invoice_num = None

    total = sum(line.qty_ordered * line.price_ordered for line in pricing.lines)

    # Check if partial invoicing is possible: pricing passes but shipment has
    # missing items. partial_total uses shipped quantities instead of ordered.
    partial_shipment_allowed = False
    partial_total: float | None = None
    if (
        pricing.status == POStatus.ALL_MATCH
        and shipment.status == ShipmentStatus.NEEDS_REVIEW
        and shipment.partial_invoice_possible
    ):
        partial_shipment_allowed = True
        # Build a price map from pricing.lines (SKU → unit price)
        sku_prices = {line.sku: line.price_ordered for line in pricing.lines}
        shipped_items = shipment.shipped_items
        partial_total = sum(
            shipped_items.get(sku, 0.0) * price
            for sku, price in sku_prices.items()
            if sku in shipped_items
        )

    return InvoicePreparation(
        po=po,
        po_num=po_num,
        shipment_id=shipment_id,
        pricing=pricing,
        shipment=shipment,
        ship_date_check=ship_date_check,
        invoice_num=invoice_num,
        total=total,
        blockers=blockers,
        number_resolution=resolution,
        partial_shipment_allowed=partial_shipment_allowed,
        partial_total=partial_total,
    )


@dataclass
class InvoicingOutcome:
    invoice_num: str
    invoice_date: str
    odoo_move_id: int
    spring_result: dict[str, Any] | None = None
    spring_error: str | None = None
    spring_skipped: str | None = None


def execute_invoicing(clients: Clients, prep: InvoicePreparation) -> InvoicingOutcome:
    """Create the Odoo draft, then (if enabled) the Spring invoice.

    Odoo first on purpose: the draft is reversible and the Spring call may
    transmit an EDI 810 to Target, which is not. If Odoo fails this raises
    before Spring is touched. A Spring failure is captured rather than raised,
    so the caller can still report the Odoo draft that did get created.
    """
    if not prep.ready:
        raise WorkflowError(
            f"PO {prep.po_num} is not ready to invoice: {' '.join(prep.blockers)}"
        )

    plan = plan_odoo_invoice(clients, prep.po, prep.invoice_num, prep.invoice_date)
    move_id = create_odoo_invoice(clients, plan)

    outcome = InvoicingOutcome(
        invoice_num=prep.invoice_num,
        invoice_date=prep.invoice_date,
        odoo_move_id=move_id,
    )

    if not clients.config.spring_invoice_enabled:
        outcome.spring_skipped = (
            "SPRING_INVOICE_ENABLED is off -- no invoice was sent to Spring/Target."
        )
        return outcome

    try:
        outcome.spring_result = send_spring_invoice(
            clients, prep.po, prep.invoice_num, prep.invoice_date
        )
    except Exception as e:  # noqa: BLE001 -- reported to the user, not swallowed
        outcome.spring_error = f"{type(e).__name__}: {e}"
    return outcome


def execute_partial_invoicing(
    clients: Clients, prep: InvoicePreparation
) -> InvoicingOutcome:
    """Create invoices for only the items that were actually shipped.

    Like execute_invoicing, but uses shipped quantities instead of ordered
    quantities. Requires partial_shipment_allowed to be True (pricing passes,
    shipment has some items shipped).
    """
    if not prep.partial_shipment_allowed:
        raise WorkflowError(
            f"PO {prep.po_num} does not allow partial invoicing. "
            "Pricing must pass and at least some items must be shipped."
        )
    if not prep.invoice_num:
        raise WorkflowError(
            f"PO {prep.po_num} has no invoice number derived. "
            f"Blockers: {' '.join(prep.blockers)}"
        )

    # Use shipped quantities from the shipment check
    qty_overrides = prep.shipment.shipped_items

    plan = plan_odoo_invoice(
        clients, prep.po, prep.invoice_num, prep.invoice_date, qty_overrides=qty_overrides
    )
    move_id = create_odoo_invoice(clients, plan)

    outcome = InvoicingOutcome(
        invoice_num=prep.invoice_num,
        invoice_date=prep.invoice_date,
        odoo_move_id=move_id,
    )

    if not clients.config.spring_invoice_enabled:
        outcome.spring_skipped = (
            "SPRING_INVOICE_ENABLED is off -- no invoice was sent to Spring/Target."
        )
        return outcome

    try:
        outcome.spring_result = send_spring_invoice(
            clients, prep.po, prep.invoice_num, prep.invoice_date, qty_overrides=qty_overrides
        )
    except Exception as e:  # noqa: BLE001 -- reported to the user, not swallowed
        outcome.spring_error = f"{type(e).__name__}: {e}"
    return outcome
