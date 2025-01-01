from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from remitextract.extract import MODEL, ExtractionError, extract, validate
from remitextract.pipeline import run
from remitextract.schema import DeductionReason, Remittance, RemitLine

ADVICE = """REMITTANCE ADVICE - Vandelay Industries AP
Payment ref: ACH 0918273   Date: 2024-02-14   Amount: USD 18,410.50

Invoice      Inv Amount   Paid        Deduction  Note
INV-104233   10,000.00    9,800.00    200.00     2% 10 net 30
INV-104251    6,000.00    5,610.50    389.50     2 cases damaged on arrival
INV-104260    3,000.00    3,000.00
"""

OPEN = {"104233": Decimal("10000.00"), "104251": Decimal("6000.00"), "104260": Decimal("3000.00"), "104300": Decimal("500.00")}


def good() -> Remittance:
    return Remittance(
        payer_name="Vandelay Industries", payment_reference="ACH 0918273", payment_total=Decimal("18410.50"),
        lines=[
            RemitLine(invoice_number="INV-104233", amount_paid=Decimal("9800.00"), invoice_amount=Decimal("10000.00"),
                      deduction_amount=Decimal("200.00"), deduction_reason=DeductionReason.early_payment_discount,
                      deduction_note="2% 10 net 30", source_quote="INV-104233   10,000.00    9,800.00    200.00     2% 10 net 30"),
            RemitLine(invoice_number="INV-104251", amount_paid=Decimal("5610.50"), deduction_amount=Decimal("389.50"),
                      deduction_reason=DeductionReason.damaged_goods, source_quote="INV-104251    6,000.00    5,610.50    389.50     2 cases damaged on arrival"),
            RemitLine(invoice_number="INV-104260", amount_paid=Decimal("3000.00"), source_quote="INV-104260    3,000.00    3,000.00"),
        ])


class FakeClient:
    def __init__(self, parsed=None, stop_reason="end_turn"):
        self.calls = []
        self.parsed, self.stop_reason = parsed, stop_reason
        self.beta = NS(messages=NS(parse=self._parse))

    def _parse(self, **kw):
        self.calls.append(kw)
        return NS(parsed_output=self.parsed, stop_reason=self.stop_reason, model=MODEL)


def test_request_uses_structured_output_and_fallbacks():
    c = FakeClient(good())
    r = extract(c, ADVICE.encode(), "text/plain")
    kw = c.calls[0]
    assert kw["model"] == "claude-opus-5-5" and kw["output_format"] is Remittance
    assert kw["fallbacks"] == "default" and kw["output_config"] == {"effort": "medium"}
    doc = kw["messages"][0]["content"][0]
    assert doc["type"] == "document" and doc["source"]["type"] == "text"
    assert r.payment_total == Decimal("18410.50")


def test_pdf_is_sent_as_base64_document():
    c = FakeClient(good())
    extract(c, b"%PDF-1.4 fake", "application/pdf")
    src = c.calls[0]["messages"][0]["content"][0]["source"]
    assert src["type"] == "base64" and src["media_type"] == "application/pdf"


@pytest.mark.parametrize("stop", ["refusal", "max_tokens"])
def test_bad_stop_reasons_raise(stop):
    with pytest.raises(ExtractionError):
        extract(FakeClient(good(), stop), b"x", "text/plain")


def test_valid_remittance_passes():
    v = validate(good(), document_text=ADVICE, open_invoices=OPEN)
    assert v.ok, v.issues


def test_hallucinated_line_is_caught_by_grounding_and_ar_checks():
    r = good()
    r.lines.append(RemitLine(invoice_number="INV-104300", amount_paid=Decimal("500.00"), source_quote="INV-104300   500.00   500.00"))
    v = validate(r, document_text=ADVICE, open_invoices=OPEN)
    assert not v.ok
    assert any("source row not found" in i for i in v.issues)
    assert any("lines sum to" in i for i in v.issues)


def test_business_rules():
    r = good()
    r.lines[2] = RemitLine(invoice_number="999", amount_paid=Decimal("3000.00"), source_quote="INV-104260    3,000.00    3,000.00")
    r.lines[1].deduction_reason = None
    r.lines[0].amount_paid = Decimal("10000.00")  # +200 deduction exceeds open amount
    r.payment_total = Decimal("18610.50")
    issues = validate(r, document_text=ADVICE, open_invoices=OPEN).issues
    assert any("not an open invoice" in i for i in issues)
    assert any("deduction without a reason" in i for i in issues)
    assert any("exceeds open" in i for i in issues)


class MemStore:
    def __init__(self, blobs):
        self.blobs = dict(blobs)

    def list(self, prefix):
        return sorted(k for k in self.blobs if k.startswith(prefix))

    def read(self, name):
        return self.blobs[name]

    def write(self, name, data, content_type):
        self.blobs[name] = data

    def move(self, src, dst):
        self.blobs[dst] = self.blobs.pop(src)


def test_pipeline_routes_and_never_loses_documents():
    store = MemStore({"incoming/C107/advice-0214.txt": ADVICE.encode(), "incoming/C107/bad.txt": b"illegible scan"})
    bad = good()
    bad.unreadable, bad.notes = True, "second page blurred"

    class Routed(FakeClient):
        def _parse(self, **kw):
            text = kw["messages"][0]["content"][0]["source"].get("data", "")
            return NS(parsed_output=good() if "Vandelay" in text else bad, stop_reason="end_turn", model=MODEL)

    results = {r.blob: r for r in run(Routed(), store, lambda cust: OPEN)}
    assert results["incoming/C107/advice-0214.txt"].outcome == "extracted"
    assert results["incoming/C107/bad.txt"].outcome == "review"
    assert "extracted/C107/advice-0214.txt.json" in store.blobs and "review/C107/bad.txt.json" in store.blobs
    assert store.list("incoming/") == []
    assert "processed/C107/advice-0214.txt" in store.blobs


def test_pipeline_leaves_blob_in_place_on_unexpected_error():
    store = MemStore({"incoming/C1/a.txt": b"x"})

    class Boom(FakeClient):
        def _parse(self, **kw):
            raise ConnectionError("network")

    r = run(Boom(), store, lambda c: {})
    assert r[0].outcome == "failed" and store.list("incoming/") == ["incoming/C1/a.txt"]
