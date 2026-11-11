from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Evidence:
    google_drive_file_id: str
    file_name: str
    file_hash: str
    exists: bool = True
    attachable: bool = True
    parent_folder_id: str | None = None
    parent_folder_name: str | None = None
    local_file_path: str | None = None


@dataclass(frozen=True)
class JournalCandidate:
    journal_date: str
    vendor_name: str
    amount: int
    tax_amount: int
    tax_rate: float
    debit_account: str
    debit_sub_account: str | None
    credit_account: str
    credit_sub_account: str | None
    tax_category: str
    description: str
    invoice_number: str | None = None
    trade_partner_code: str | None = None
    trade_partner_name: str | None = None
    source_type: str = "receipt"
    risk_flags: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "journal_date": self.journal_date,
            "vendor_name": self.vendor_name,
            "amount": self.amount,
            "tax_amount": self.tax_amount,
            "tax_rate": self.tax_rate,
            "debit_account": self.debit_account,
            "debit_sub_account": self.debit_sub_account,
            "credit_account": self.credit_account,
            "credit_sub_account": self.credit_sub_account,
            "tax_category": self.tax_category,
            "description": self.description,
            "invoice_number": self.invoice_number,
            "trade_partner_code": self.trade_partner_code,
            "trade_partner_name": self.trade_partner_name,
            "source_type": self.source_type,
            "risk_flags": list(self.risk_flags),
        }
