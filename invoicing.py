"""Derivation rules for Target invoice numbers and invoice dates.

Pure functions -- no API calls, no config. workflow.prepare_invoice() supplies
the raw values and turns failures here into blockers.

INVOICE NUMBER: TAR + YYMMDD(ship date) + last 2 digits of the PO number.
Confirmed against the three invoices raised manually on 2026-08-21:

    PO 10001993952-3840, shipped 2026-08-18 -> TAR26081840
    PO 10001993952-3841, shipped 2026-08-18 -> TAR26081841
    PO 10001993952-3842, shipped 2026-08-18 -> TAR26081842

The trailing 2 digits are the Target DC code (PO ...-3840 ships to TARGET DC
3840), so two POs to the SAME DC shipping the SAME day derive the same number.
That happens in practice -- seen 2026-08-27, POs 10002032881-3840 and
10002009713-3840. workflow.resolve_invoice_num walks the candidates from
invoice_num_candidates() and takes the first free in both Spring and Odoo,
giving the second PO that day TAR26082740B.

It checks whether *this* PO already owns a number before allocating a new one,
which is what keeps re-runs safe: "take the next free number" on its own would
hand a fresh suffix to an already-invoiced PO on every re-run.

SHIP DATE: Camelot's ShipDate is authoritative. Spring has no ASN export
endpoint, so its only ASN trace is po_last_asn_date -- the timestamp the ASN
was *transmitted*, which is not the same thing as the date the goods shipped
(an ASN sent after midnight, or stamped in a different timezone, would be off
by a day). A wrong date corrupts both the invoice date and the invoice number,
and the invoice number is what Target reconciles against -- so the two sources
are compared and a disagreement is a hard stop, not a warning.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime

# Camelot returns MM/DD/YY ("07/31/26", confirmed live). ISO is accepted too in
# case the WMS is ever reconfigured -- anything else is rejected rather than
# guessed at, since a misparsed date silently produces a wrong invoice number.
_CAMELOT_DATE_FORMATS = ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d")

# Suffixes for same-DC/same-day collisions. 'A' is omitted: the unsuffixed base
# number is effectively A, so the first collision gets B.
_COLLISION_SUFFIXES = "BCDEFGHIJKLMNOPQRSTUVWXYZ"


class DerivationError(Exception):
    """A ship date or PO number that can't be turned into an invoice number."""


@dataclass
class ShipDateCheck:
    """Camelot's ship date, plus whether Spring's ASN timestamp agrees with it."""

    ship_date: date
    camelot_raw: str
    spring_asn_raw: str | None
    spring_asn_date: date | None

    @property
    def agrees(self) -> bool:
        """True when Spring has no ASN date to compare (nothing contradicts
        Camelot) or its date part matches. False only on real disagreement."""
        return self.spring_asn_date is None or self.spring_asn_date == self.ship_date

    @property
    def disagreement(self) -> str | None:
        if self.agrees:
            return None
        return (
            f"Camelot shipped {self.ship_date.isoformat()} ({self.camelot_raw}) but "
            f"Spring's ASN is dated {self.spring_asn_date.isoformat()} "
            f"({self.spring_asn_raw})"
        )


def parse_camelot_ship_date(raw: str | None) -> date:
    value = (raw or "").strip()
    if not value:
        raise DerivationError(
            "Camelot returned no ship date for this shipment, so the invoice date "
            "and number can't be derived."
        )
    for fmt in _CAMELOT_DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise DerivationError(f"Unrecognized Camelot ship date format: {value!r}.")


def parse_spring_asn_date(raw: str | None) -> date | None:
    """Date part of po_last_asn_date ("2026-08-18 18:13:25"). Returns None when
    absent or unparseable -- this is only a cross-check, so it must never be the
    thing that blocks an otherwise-valid invoice."""
    value = (raw or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def check_ship_date(camelot_raw: str | None, spring_asn_raw: str | None) -> ShipDateCheck:
    ship_date = parse_camelot_ship_date(camelot_raw)
    return ShipDateCheck(
        ship_date=ship_date,
        camelot_raw=(camelot_raw or "").strip(),
        spring_asn_raw=(spring_asn_raw or "").strip() or None,
        spring_asn_date=parse_spring_asn_date(spring_asn_raw),
    )


def invoice_num_candidates(base: str) -> list[str]:
    """The base number, then base+B, base+C ... for same-DC/same-day collisions.

    Two POs to the same DC shipping the same day derive the same base number
    (the trailing digits are the DC code, so they don't distinguish the POs).
    Letters rather than "-2" so the number stays strictly alphanumeric: an EDI
    810 invoice number lands in BIG02, and punctuation risks being rejected or
    normalized by the retailer's validation.

    'A' is skipped because the unsuffixed base already plays that role.
    """
    return [base] + [f"{base}{letter}" for letter in _COLLISION_SUFFIXES]


def derive_invoice_num(po_num: str, ship_date: date) -> str:
    """TAR + YYMMDD + the PO's trailing 2 digits (its Target DC code)."""
    match = re.search(r"(\d{2})\s*$", (po_num or "").strip())
    if not match:
        raise DerivationError(
            f"PO number {po_num!r} doesn't end in two digits, so the DC suffix for "
            "the invoice number can't be determined."
        )
    return f"TAR{ship_date.strftime('%y%m%d')}{match.group(1)}"
