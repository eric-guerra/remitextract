"""The structured output contract: what Claude must return for one remittance advice."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, Field


class DeductionReason(str, Enum):
    early_payment_discount = "early_payment_discount"
    short_shipment = "short_shipment"
    damaged_goods = "damaged_goods"
    pricing_dispute = "pricing_dispute"
    promotional_allowance = "promotional_allowance"
    freight = "freight"
    other = "other"


class RemitLine(BaseModel):
    invoice_number: str = Field(description="Invoice number exactly as printed, e.g. 'INV-104233' or '104233'")
    amount_paid: Decimal = Field(description="Amount applied to this invoice, as a positive number")
    invoice_amount: Decimal | None = Field(default=None, description="Original invoice amount if shown")
    deduction_amount: Decimal = Field(default=Decimal("0"), description="Amount withheld from this invoice; 0 if none")
    deduction_reason: DeductionReason | None = None
    deduction_note: str | None = Field(default=None, description="Customer's own wording for the deduction, verbatim")
    source_quote: str = Field(description="The exact line or row of the document this entry was read from, copied verbatim")


class Remittance(BaseModel):
    payer_name: str
    payment_reference: str | None = Field(default=None, description="Check number, ACH trace or wire reference")
    payment_date: date | None = None
    currency: str = Field(default="USD", description="ISO 4217 code")
    payment_total: Decimal = Field(description="Total amount of the payment as stated on the advice")
    lines: list[RemitLine]
    unreadable: bool = Field(default=False, description="True if parts of the document could not be read; explain in notes")
    notes: str | None = None
