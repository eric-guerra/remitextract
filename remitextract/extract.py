"""Remittance extraction with Claude structured outputs, plus deterministic validation.

The model reads; code decides. Every extracted line must carry a verbatim `source_quote` that we
find in the document, and the lines must reconcile to the stated payment total, before anything is
auto-applied. Otherwise the advice goes to a human with the reasons attached.
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from decimal import Decimal

from .schema import Remittance

MODEL = "claude-opus-5-5"

SYSTEM = """You extract payment remittance advices (customer statements of which invoices a payment \
covers) for an accounts-receivable team.

Read the whole document, including tables that continue across pages. For every invoice the payment \
covers, return one line. Copy invoice numbers and the source row exactly as printed. Amounts are \
positive numbers without currency symbols. When the customer paid less than the invoice amount, put \
the difference in deduction_amount and classify the reason from their wording; keep their wording in \
deduction_note.

Only report what the document says. If something is illegible or ambiguous, set unreadable to true \
and say what in notes rather than guessing."""


@dataclass
class Validation:
    ok: bool
    issues: list[str] = field(default_factory=list)
    lines_total: Decimal = Decimal("0")


def build_content(document: bytes, media_type: str) -> list[dict]:
    if media_type == "application/pdf":
        doc = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(document).decode()}}
    elif media_type.startswith("image/"):
        doc = {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(document).decode()}}
    else:  # email body / CSV / plain text
        doc = {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": document.decode("utf-8", "replace")}}
    return [doc, {"type": "text", "text": "Extract this remittance advice."}]


def extract(client, document: bytes, media_type: str) -> Remittance:
    response = client.beta.messages.parse(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM,
        output_format=Remittance,
        output_config={"effort": "medium"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": build_content(document, media_type)}],
    )
    if response.stop_reason == "refusal":
        raise ExtractionError("model declined to process the document")
    if response.stop_reason == "max_tokens":
        raise ExtractionError("output truncated (document too long for one pass)")
    if response.parsed_output is None:
        raise ExtractionError("no structured output returned")
    return response.parsed_output


class ExtractionError(Exception):
    pass


_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub(" ", s).strip().lower()


def validate(r: Remittance, *, document_text: str | None, open_invoices: dict[str, Decimal],
             tolerance: Decimal = Decimal("0.01")) -> Validation:
    issues: list[str] = []
    total = sum((l.amount_paid for l in r.lines), Decimal("0"))

    if r.unreadable:
        issues.append(f"model flagged unreadable content: {r.notes or 'no detail'}")
    if not r.lines:
        issues.append("no invoice lines found")
    if abs(total - r.payment_total) > tolerance:
        issues.append(f"lines sum to {total} but payment total is {r.payment_total}")

    seen = set()
    text = _norm(document_text) if document_text is not None else None
    for l in r.lines:
        key = _normalize_invoice(l.invoice_number)
        if key in seen:
            issues.append(f"invoice {l.invoice_number} listed twice")
        seen.add(key)
        if l.amount_paid <= 0:
            issues.append(f"invoice {l.invoice_number}: non-positive amount {l.amount_paid}")
        # Grounding check: the row the model says it read must actually be in the document.
        if text is not None and _norm(l.source_quote) not in text:
            issues.append(f"invoice {l.invoice_number}: source row not found in document")
        open_amt = open_invoices.get(key)
        if open_amt is None:
            issues.append(f"invoice {l.invoice_number} is not an open invoice for this customer")
        elif l.amount_paid + l.deduction_amount - open_amt > tolerance:
            issues.append(f"invoice {l.invoice_number}: paid {l.amount_paid} + deduction {l.deduction_amount} exceeds open {open_amt}")
        if l.deduction_amount > 0 and l.deduction_reason is None:
            issues.append(f"invoice {l.invoice_number}: deduction without a reason")
    return Validation(not issues, issues, total)


def _normalize_invoice(n: str) -> str:
    """'INV-104233', 'inv 104233', '104233' -> '104233' (AR stores the numeric part)."""
    digits = re.sub(r"\D", "", n)
    return digits or n.strip().upper()
