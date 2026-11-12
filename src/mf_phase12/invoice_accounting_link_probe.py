from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .accounting_api import (
    MoneyForwardAccountingClient,
    Transport,
    UrllibTransport,
)
from .hash_utils import mask_identifier, stable_hash
from .local_artifacts import require_ignored_untracked_data_path

INVOICE_BASE_URL = "https://invoice.moneyforward.com"
MAX_ACCOUNTING_JOURNAL_WINDOW_DAYS = 90
INVOICE_EXCISE_RATES = {
    "untaxable": 0.0,
    "non_taxable": 0.0,
    "tax_exemption": 0.0,
    "five_percent": 0.05,
    "eight_percent": 0.08,
    "eight_percent_as_reduced_tax_rate": 0.08,
    "ten_percent": 0.10,
}


@dataclass(frozen=True)
class InvoiceAccountingLinkProbeResult:
    status: str
    start_date: str
    end_date: str
    accounting_start_date: str
    accounting_end_date: str
    invoice_billing_count: int
    accounting_journal_count: int
    matched_invoice_count: int
    sales_accrual_match_count: int
    bank_deposit_match_count: int
    category_candidate_count: int
    unmatched_invoice_count: int
    sales_accrual_missing_count: int
    settlement_status_counts: dict[str, int]
    batch_sample_count: int
    review_queue_count: int
    output_json: str | None = None
    moneyforward_write_called: bool = False
    pdf_reupload_called: bool = False
    google_drive_write_called: bool = False
    raw_api_response_stored: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "accounting_start_date": self.accounting_start_date,
            "accounting_end_date": self.accounting_end_date,
            "invoice_billing_count": self.invoice_billing_count,
            "accounting_journal_count": self.accounting_journal_count,
            "matched_invoice_count": self.matched_invoice_count,
            "sales_accrual_match_count": self.sales_accrual_match_count,
            "bank_deposit_match_count": self.bank_deposit_match_count,
            "category_candidate_count": self.category_candidate_count,
            "unmatched_invoice_count": self.unmatched_invoice_count,
            "sales_accrual_missing_count": self.sales_accrual_missing_count,
            "settlement_status_counts": self.settlement_status_counts,
            "batch_sample_count": self.batch_sample_count,
            "review_queue_count": self.review_queue_count,
            "output_json": self.output_json,
            "moneyforward_write_called": self.moneyforward_write_called,
            "pdf_reupload_called": self.pdf_reupload_called,
            "google_drive_write_called": self.google_drive_write_called,
            "raw_api_response_stored": self.raw_api_response_stored,
        }


class MoneyForwardInvoiceClient:
    def __init__(
        self,
        access_token: str,
        *,
        base_url: str = INVOICE_BASE_URL,
        transport: Transport | None = None,
    ):
        self.access_token = access_token
        self.base_url = base_url.rstrip("/")
        self.transport = transport or UrllibTransport()

    def partners(self, *, page: int = 1, per_page: int = 100) -> dict[str, Any]:
        return self._get_json(f"/api/v3/partners?{_page_query(page, per_page)}")

    def billings(self, *, page: int = 1, per_page: int = 100) -> dict[str, Any]:
        return self._get_json(f"/api/v3/billings?{_page_query(page, per_page)}")

    def billing(self, billing_id: str) -> dict[str, Any]:
        return self._get_json(f"/api/v3/billings/{urllib.parse.quote(billing_id)}")

    def billing_items(self, billing_id: str, *, page: int = 1, per_page: int = 100) -> dict[str, Any]:
        query = _page_query(page, per_page)
        return self._get_json(f"/api/v3/billings/{urllib.parse.quote(billing_id)}/items?{query}")

    def billing_pdf(self, billing_id: str) -> bytes:
        path = f"/api/v3/billings/{urllib.parse.quote(billing_id)}.pdf"
        status, headers, payload = self.transport.request(
            "GET",
            f"{self.base_url}{path}",
            headers={"Authorization": f"Bearer {self.access_token}", "Accept": "application/pdf"},
        )
        content_type = str(headers.get("Content-Type") or "").lower()
        if status != 200 or "pdf" not in content_type:
            raise RuntimeError(f"Money Forward Invoice API failed: GET {path} HTTP {status}")
        return payload

    def _get_json(self, path: str) -> dict[str, Any]:
        status, _headers, payload = self.transport.request(
            "GET",
            f"{self.base_url}{path}",
            headers={"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"},
        )
        if status != 200:
            raise RuntimeError(f"Money Forward Invoice API failed: GET {path} HTTP {status}")
        return json.loads(payload.decode())


def run_invoice_accounting_link_probe(
    *,
    invoice_client: MoneyForwardInvoiceClient,
    accounting_client: MoneyForwardAccountingClient,
    start_date: str,
    end_date: str,
    accounting_start_date: str | None = None,
    accounting_end_date: str | None = None,
    output_json: Path | None = None,
    max_pages: int = 5,
    per_page: int = 100,
) -> InvoiceAccountingLinkProbeResult:
    accounting_start_date = accounting_start_date or start_date
    accounting_end_date = accounting_end_date or end_date
    partners = _fetch_paged(
        lambda page: invoice_client.partners(page=page, per_page=per_page),
        max_pages=max_pages, per_page=per_page,
    )
    partner_names = {str(partner.get("id")): str(partner.get("name") or "") for partner in partners}
    billings = [
        billing
        for billing in _fetch_paged(
            lambda page: invoice_client.billings(page=page, per_page=per_page),
            max_pages=max_pages, per_page=per_page,
        )
        if _date_in_range(_parse_api_date(str(billing.get("billing_date") or "")), start_date, end_date)
    ]
    enriched_billings = []
    for billing in billings:
        billing_id = str(billing.get("id") or "")
        if not billing_id:
            continue
        detail = _first_item(invoice_client.billing(billing_id)) or billing
        items = _fetch_paged(
            lambda page, current_id=billing_id: invoice_client.billing_items(current_id, page=page, per_page=per_page),
            max_pages=max_pages, per_page=per_page, require_ids=False,
        )
        enriched = dict(billing)
        for key, value in detail.items():
            if value not in (None, "", []):
                enriched[key] = value
        enriched.setdefault("id", billing_id)
        enriched["partner_name"] = partner_names.get(str(enriched.get("partner_id") or ""), "")
        enriched["items"] = items
        enriched_billings.append(enriched)

    journals = _fetch_accounting_journals(
        accounting_client=accounting_client,
        start_date=accounting_start_date,
        end_date=accounting_end_date,
        max_pages=max_pages,
        per_page=per_page,
    )
    matches = [_match_billing_to_journal(billing, journals, peer_billings=enriched_billings) for billing in enriched_billings]
    safe_matches = [_redact_match(match) for match in matches]
    accrual_items = [_normalize_accrual_item(match) for match in matches]
    matched_count = sum(1 for match in matches if match["matched"])
    sales_accrual_match_count = sum(1 for match in matches if _has_full_sales_accrual(match))
    bank_match_count = sum(1 for match in matches if match["bank_deposit_match"])
    category_count = sum(1 for match in matches if match["category_candidates"])
    settlement_status_counts = _counts(str(match["settlement_status"]) for match in matches)
    review_queue_count = sum(1 for match in matches if match["settlement_action"] != "settled")

    status = "ok"
    if len(enriched_billings) - matched_count or len(enriched_billings) - sales_accrual_match_count or review_queue_count:
        status = "needs_action"
    result = InvoiceAccountingLinkProbeResult(
        status=status,
        start_date=start_date,
        end_date=end_date,
        accounting_start_date=accounting_start_date,
        accounting_end_date=accounting_end_date,
        invoice_billing_count=len(enriched_billings),
        accounting_journal_count=len(journals),
        matched_invoice_count=matched_count,
        sales_accrual_match_count=sales_accrual_match_count,
        bank_deposit_match_count=bank_match_count,
        category_candidate_count=category_count,
        unmatched_invoice_count=len(enriched_billings) - matched_count,
        sales_accrual_missing_count=len(enriched_billings) - sales_accrual_match_count,
        settlement_status_counts=settlement_status_counts,
        batch_sample_count=min(5, len(enriched_billings)),
        review_queue_count=review_queue_count,
        output_json=str(output_json) if output_json else None,
    )
    if output_json:
        output_json = require_ignored_untracked_data_path(output_json, purpose="invoice accounting link probe output")
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(
            json.dumps(
                {
                    "summary": result.to_dict(),
                    "matches": safe_matches,
                    "accrual_items": accrual_items,
                    "batch_sample": safe_matches[:5],
                    "source_of_truth": "existing Money Forward invoice/accounting linkage",
                    "probe_mode": "read_only_get_only",
                    "raw_billing_ids_stored": False,
                    "raw_api_response_stored": False,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    return result


def _page_query(page: int, per_page: int) -> str:
    return urllib.parse.urlencode({"page": page, "per_page": per_page})


def _fetch_paged(
    fetch_page: Any,
    *,
    max_pages: int,
    per_page: int = 100,
    require_ids: bool = True,
) -> list[dict[str, Any]]:
    """Read a complete, non-ambiguous Invoice API list or fail closed.

    Accept the API's pagination/meta wrapper where present. For legacy lists
    without metadata, only a short page proves the bounded page scan ended.
    No source IDs or payload contents are included in failure messages.
    """
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
        raise ValueError("Invoice API max_pages must be positive")
    if isinstance(per_page, bool) or not isinstance(per_page, int) or per_page < 1:
        raise ValueError("Invoice API per_page must be positive")

    items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    expected_pagination: tuple[int, int] | None = None
    metadata_mode: bool | None = None
    pagination_kind: str | None = None
    for page in range(1, max_pages + 1):
        payload = fetch_page(page)
        if isinstance(payload, list):
            rows = payload
            metadata = None
        elif isinstance(payload, dict):
            for key in ("data", "partners", "billings", "items"):
                if key in payload:
                    rows = payload[key]
                    break
            else:
                raise RuntimeError("Invoice API page missing list array")
            metadata_key = next(
                (key for key in ("pagination", "meta", "metadata") if key in payload),
                None,
            )
            metadata = payload[metadata_key] if metadata_key else None
            if metadata_key and metadata is None:
                raise RuntimeError("Invoice API pagination metadata invalid")
        else:
            raise RuntimeError("Invoice API page response invalid")

        if not isinstance(rows, list) or len(rows) > per_page:
            raise RuntimeError("Invoice API page rows/per_page invalid")
        if any(not isinstance(item, dict) for item in rows):
            raise RuntimeError("Invoice API page rows invalid")
        has_metadata = metadata is not None
        if metadata_mode is None:
            metadata_mode = has_metadata
        elif metadata_mode != has_metadata:
            raise RuntimeError("Invoice API pagination metadata changed")
        if has_metadata:
            if not isinstance(metadata, dict):
                raise RuntimeError("Invoice API pagination metadata invalid")
            if {"total_pages", "total_count"} <= metadata.keys():
                kind = "total_pages"
                total_pages, total_count = metadata["total_pages"], metadata["total_count"]
                if any(
                    isinstance(value, bool) or not isinstance(value, int) or value < 0
                    for value in (total_pages, total_count)
                ) or (total_pages == 0 and total_count != 0):
                    raise RuntimeError("Invoice API pagination metadata invalid")
                if page > max(total_pages, 1):
                    raise RuntimeError("Invoice API pagination exceeded total_pages")
                current_pagination = (total_pages, total_count)
                if expected_pagination is None:
                    expected_pagination = current_pagination
                elif current_pagination != expected_pagination:
                    raise RuntimeError("Invoice API pagination metadata changed")
            elif "next_page" in metadata:
                kind = "next_page"
                next_page = metadata["next_page"]
                if next_page is not None and (
                    isinstance(next_page, bool) or not isinstance(next_page, int)
                    or next_page != page + 1
                ):
                    raise RuntimeError("Invoice API pagination next_page invalid")
            else:
                raise RuntimeError("Invoice API pagination metadata missing")
            if pagination_kind is None:
                pagination_kind = kind
            elif pagination_kind != kind:
                raise RuntimeError("Invoice API pagination mode changed")
            if "current_page" in metadata and metadata["current_page"] != page:
                raise RuntimeError("Invoice API pagination current_page mismatch")

        if require_ids:
            for item in rows:
                identity = str(item.get("id") or "").strip()
                if not identity:
                    raise RuntimeError("Invoice API page has missing source identity")
                if identity in seen_ids:
                    raise RuntimeError("Invoice API pagination has duplicate source identity")
                seen_ids.add(identity)
        items.extend(rows)

        if has_metadata and pagination_kind == "total_pages":
            if len(items) > total_count:
                raise RuntimeError("Invoice API pagination total_count mismatch")
            if page >= total_pages:
                if len(items) != total_count:
                    raise RuntimeError("Invoice API pagination incomplete total_count")
                return items
            if not rows:
                raise RuntimeError("Invoice API pagination incomplete empty page")
        elif has_metadata:
            if next_page is None:
                return items
            if not rows:
                raise RuntimeError("Invoice API pagination incomplete empty page")
        elif len(rows) < per_page:
            return items
    raise RuntimeError("Invoice API pagination exceeded page limit; inventory incomplete")


def _fetch_accounting_journals(
    *,
    accounting_client: MoneyForwardAccountingClient,
    start_date: str,
    end_date: str,
    max_pages: int,
    per_page: int,
) -> list[dict[str, Any]]:
    """Reuse the verified journal pagination guard for each date window."""
    from .sync import _fetch_all_journals, _journal_id

    journals: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for window_start, window_end in _accounting_journal_windows(start_date, end_date):
        page_rows = _fetch_all_journals(
            accounting_client,
            None,
            start_date=window_start,
            end_date=window_end,
            per_page=per_page,
            max_pages=max_pages,
        )
        for journal in page_rows:
            identity = _journal_id(journal)
            if identity in seen_ids:
                raise RuntimeError("Accounting API pagination has duplicate journal identity")
            seen_ids.add(identity)
        journals.extend(page_rows)
    return journals


def _accounting_journal_windows(start_date: str, end_date: str) -> list[tuple[str, str]]:
    """Split reads to stay within Money Forward's accepted journal date range."""
    start = _parse_api_date(start_date)
    end = _parse_api_date(end_date)
    if not start or not end:
        raise ValueError("accounting journal dates must be ISO dates")
    if start > end:
        raise ValueError("accounting journal start_date must not be after end_date")

    windows: list[tuple[str, str]] = []
    cursor = date.fromisoformat(start)
    final_day = date.fromisoformat(end)
    while cursor <= final_day:
        window_end = min(cursor + timedelta(days=MAX_ACCOUNTING_JOURNAL_WINDOW_DAYS - 1), final_day)
        windows.append((cursor.isoformat(), window_end.isoformat()))
        cursor = window_end + timedelta(days=1)
    return windows


def _items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("data", "partners", "billings", "items", "journals"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        for value in data.values():
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _first_item(data: Any) -> dict[str, Any] | None:
    if isinstance(data, dict):
        items = _items(data)
        if items:
            return items[0]
        return data
    return None


def _match_billing_to_journal(
    billing: dict[str, Any],
    journals: list[dict[str, Any]],
    *,
    peer_billings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    billing_number = str(billing.get("billing_number") or "")
    partner_name = str(billing.get("partner_name") or "")
    amount = _amount(billing)
    same_partner_amount_numbers = _same_partner_amount_billing_numbers(billing, peer_billings or [])
    category_candidates = classify_invoice_items(billing.get("items") or [])
    best: dict[str, Any] | None = None
    best_score = -1
    best_reasons: list[str] = []
    collection_candidates: list[dict[str, Any]] = []
    sales_accrual_candidates: list[dict[str, Any]] = []
    for journal in journals:
        lines = _journal_lines(journal)
        text = _journal_text(journal, lines)
        score = 0
        reasons: list[str] = []
        if billing_number and _contains_billing_number(text, billing_number):
            score += 50
            reasons.append("billing_number_in_accounting_journal")
        if partner_name and _normalize_name(partner_name) in _normalize_name(text):
            score += 30
            reasons.append("partner_name_match")
        if amount and any(_line_amount(line) == amount for line in lines):
            score += 25
            reasons.append("amount_match")
        if score > best_score:
            best = journal
            best_score = score
            best_reasons = reasons
        collection = _collection_candidate(journal, lines, billing_number, partner_name, amount, text)
        if collection:
            collection_candidates.append(collection)
        sales_accrual = _sales_accrual_candidate(
            journal,
            lines,
            billing_number,
            partner_name,
            amount,
            text,
            same_partner_amount_numbers=same_partner_amount_numbers,
        )
        if sales_accrual:
            sales_accrual_candidates.append(sales_accrual)
    matched = bool(best and best_score >= 50)
    best_lines = _journal_lines(best or {})
    debit_line = next((line for line in best_lines if line["side"] == "debit"), {})
    credit_line = next((line for line in best_lines if line["side"] == "credit"), {})
    bank_deposit_match = bool(collection_candidates)
    settlement = determine_settlement_status(billing=billing, collection_candidates=collection_candidates, matched=matched)
    return {
        "matched": matched,
        "score": best_score if matched else 0,
        "reasons": best_reasons if matched else [],
        "billing": billing,
        "journal": best if matched else None,
        "debit_line": debit_line,
        "credit_line": credit_line,
        "sales_accrual_candidates": sales_accrual_candidates,
        "bank_deposit_match": bank_deposit_match,
        "collection_candidates": collection_candidates,
        "settlement_status": settlement["status"],
        "settlement_action": settlement["action"],
        "settlement_reasons": settlement["reasons"],
        "category_candidates": category_candidates,
    }


def _same_partner_amount_billing_numbers(
    billing: dict[str, Any],
    peer_billings: list[dict[str, Any]],
) -> list[str]:
    partner_name = _normalize_name(str(billing.get("partner_name") or ""))
    amount = _amount(billing)
    current_id = str(billing.get("id") or "")
    current_number = str(billing.get("billing_number") or "")
    if not partner_name or not amount:
        return []
    numbers: list[str] = []
    for peer in peer_billings:
        if current_id and str(peer.get("id") or "") == current_id:
            continue
        peer_number = str(peer.get("billing_number") or "")
        if peer_number and peer_number == current_number:
            continue
        if _normalize_name(str(peer.get("partner_name") or "")) == partner_name and _amount(peer) == amount:
            numbers.append(peer_number)
    return numbers


def _has_full_sales_accrual(match: dict[str, Any]) -> bool:
    candidates = match.get("sales_accrual_candidates") or []
    if any(str(candidate.get("amount_status") or "") == "full" for candidate in candidates):
        return True
    invoice_amount = _amount(match.get("billing") or {})
    partial_amount = sum(
        int(candidate.get("matched_gross_value") or 0)
        for candidate in candidates
        if str(candidate.get("amount_status") or "") == "partial"
    )
    return bool(invoice_amount and partial_amount == invoice_amount)


def determine_settlement_status(
    *,
    billing: dict[str, Any],
    collection_candidates: list[dict[str, Any]],
    matched: bool = True,
) -> dict[str, Any]:
    invoice_amount = _amount(billing)
    if not matched:
        return {"status": "相手先不明", "action": "queue_for_review", "reasons": ["invoice_not_matched_to_accounting"]}
    if not collection_candidates:
        return {"status": "未入金", "action": "queue_for_followup", "reasons": ["no_bank_deposit_collection_journal"]}
    collected = sum(int(item["collection_amount"]) for item in collection_candidates)
    if collected == invoice_amount:
        return {"status": "入金済み", "action": "settled", "reasons": ["collection_amount_equals_invoice_amount"]}
    if 0 < collected < invoice_amount:
        difference = invoice_amount - collected
        texts = " ".join(str(item.get("text") or "") for item in collection_candidates)
        if _has_transfer_fee_marker(texts):
            return {"status": "振込手数料差引", "action": "queue_for_fee_confirmation", "reasons": ["collection_short_by_fee_candidate"]}
        if _has_withholding_marker(texts):
            return {"status": "源泉所得税差引", "action": "queue_for_withholding_confirmation", "reasons": ["collection_short_by_withholding_candidate"]}
        return {
            "status": "一部入金",
            "action": "queue_for_followup",
            "reasons": [f"collection_shortfall_bucket={_amount_bucket(difference)}"],
        }
    return {"status": "金額不一致", "action": "queue_for_review", "reasons": ["collection_amount_exceeds_or_mismatches_invoice"]}


def classify_invoice_items(items: list[dict[str, Any]]) -> list[str]:
    text = " ".join(str(item.get(key) or "") for item in items for key in ("name", "detail", "description"))
    candidates: list[str] = []
    rules = [
        (r"家賃|賃料|設備利用", "売上高/賃貸"),
        (r"業務委託|事務委託|コンサル|管理費", "売上高/業務委託・事務委託"),
        (r"SNS|運用", "売上高/SNS運用"),
        (r"出張麻酔", "売上高/出張麻酔斡旋"),
        (r"工具|器材|サンダー|ドリル|洗浄機|コンプレッサー", "売上高/小売"),
        (r"社食|立替", "要確認/売上外または立替"),
    ]
    for pattern, candidate in rules:
        if re.search(pattern, text, flags=re.IGNORECASE) and candidate not in candidates:
            candidates.append(candidate)
    return candidates or ["要確認"]


def _normalize_accrual_item(match: dict[str, Any]) -> dict[str, Any]:
    """Keep only the official invoice facts needed to decide one sales accrual."""

    billing = match["billing"]
    raw_billing_ref = str(billing.get("id") or "")
    billing_ref_hash = mask_identifier(raw_billing_ref)
    billing_number = str(billing.get("billing_number") or "").strip()
    sales_date = _parse_api_date(str(billing.get("sales_date") or ""))
    partner_name = str(
        billing.get("partner_name") or billing.get("partner") or ""
    ).strip()
    total_price = _amount(billing)
    source_identity = (
        {"billing_ref_hash": billing_ref_hash}
        if raw_billing_ref
        else {
            "billing_number": billing_number,
            "sales_date": sales_date or "",
            "partner_hash": mask_identifier(partner_name),
            "total_price": total_price,
        }
    )
    source_file_hash = "sha256:" + stable_hash(
        {"source": "moneyforward_invoice_api", **source_identity}
    )
    excise_price_present = billing.get("excise_price") not in (None, "")
    excise_price = _integer_amount(billing.get("excise_price"))
    raw_items = [item for item in billing.get("items") or [] if isinstance(item, dict)]
    details = [
        {
            "name": str(item.get("name") or ""),
            "detail": str(item.get("detail") or item.get("description") or ""),
            "quantity": item.get("quantity"),
            "unit": str(item.get("unit") or ""),
            "price": item.get("price"),
            "excise": str(item.get("excise") or ""),
            "is_deduct_withholding_tax": item.get("is_deduct_withholding_tax") is True,
        }
        for item in raw_items
    ]
    excises = {str(item.get("excise") or "").strip() for item in raw_items}
    excises.discard("")
    excise = next(iter(excises)) if len(excises) == 1 else ""
    tax_rate = INVOICE_EXCISE_RATES.get(excise)
    category_candidates = [
        value
        for value in match.get("category_candidates") or []
        if str(value).startswith("売上高/")
    ]
    reasons: list[str] = []
    if not sales_date:
        reasons.append("missing_sales_date")
    if not billing_number:
        reasons.append("missing_billing_number")
    if total_price <= 0:
        reasons.append("non_positive_total_price")
    if not partner_name:
        reasons.append("missing_partner")
    # Account classification is deliberately deferred until the verified RAG
    # pass. These keyword candidates are hints, never a standalone decision.
    if not raw_items or len(excises) != 1:
        reasons.append("mixed_or_missing_excise")
    elif excise not in INVOICE_EXCISE_RATES:
        reasons.append("unsupported_excise")
    if any(item["is_deduct_withholding_tax"] for item in details):
        reasons.append("withholding_item_present")
    if not excise_price_present:
        reasons.append("missing_excise_price")
    elif tax_rate is not None and not _amount_tax_matches(
        total_price=total_price,
        excise_price=excise_price,
        tax_rate=tax_rate,
    ):
        reasons.append("amount_tax_mismatch")

    execution_status = "pending"
    accrual_candidates = match.get("sales_accrual_candidates") or []
    native_journal_ref_hashes = sorted(
        {
            str(candidate.get("journal_ref_hash") or "")
            for candidate in accrual_candidates
            if isinstance(candidate, dict)
            and candidate.get("native_source_verified") is True
            and candidate.get("journal_ref_hash")
        }
    )
    if len(accrual_candidates) > 1:
        reasons.append("multiple_existing_sales_accrual_matches")
    elif len(accrual_candidates) == 1:
        if str(accrual_candidates[0].get("amount_status") or "") != "full":
            reasons.append("partial_existing_sales_accrual")

    decision_state = "ready" if not reasons else "review"
    if decision_state == "review":
        execution_status = "review"
    return {
        "source_file_hash": source_file_hash,
        "billing_ref_hash": billing_ref_hash,
        "billing_number": billing_number,
        "sales_date": sales_date or "",
        "partner_name": partner_name,
        "total_price": total_price,
        "details": details,
        "sales_category": category_candidates[0] if len(category_candidates) == 1 else "",
        "excise": excise,
        "tax_rate": tax_rate,
        "excise_price": excise_price,
        "decision_state": decision_state,
        "review_reason_codes": reasons,
        "execution_status": execution_status,
        "native_linkage_status": (
            "registered"
            if len(native_journal_ref_hashes) == 1
            else "ambiguous"
            if len(native_journal_ref_hashes) > 1
            else "registered_journal_not_confirmed"
        ),
        "native_journal_ref_hashes": native_journal_ref_hashes,
        "native_source_verified": bool(native_journal_ref_hashes),
        "raw_billing_id_stored": False,
        "raw_api_response_stored": False,
    }


def _amount_tax_matches(*, total_price: int, excise_price: int, tax_rate: float) -> bool:
    if total_price <= 0 or excise_price < 0 or excise_price > total_price:
        return False
    if tax_rate == 0:
        return excise_price == 0
    expected = round((total_price - excise_price) * tax_rate)
    return abs(expected - excise_price) <= 2


def _integer_amount(value: Any) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return 0


def _redact_match(match: dict[str, Any]) -> dict[str, Any]:
    billing = match["billing"]
    journal = match["journal"] or {}
    return {
        "matched": match["matched"],
        "score": match["score"],
        "reasons": match["reasons"],
        "billing_ref_hash": mask_identifier(str(billing.get("id") or "")),
        "billing_number": str(billing.get("billing_number") or ""),
        "billing_date": str(billing.get("billing_date") or ""),
        "partner_hash": mask_identifier(str(billing.get("partner_name") or billing.get("partner_id") or "")),
        "amount_hash": mask_identifier(str(_amount(billing))),
        "item_count": len(billing.get("items") or []),
        "category_candidates": match["category_candidates"],
        "journal_ref_hash": mask_identifier(str(journal.get("id") or "")),
        "journal_number": str(journal.get("number") or ""),
        "journal_date": str(journal.get("transaction_date") or ""),
        "debit_account": match["debit_line"].get("account_name"),
        "debit_sub_account": match["debit_line"].get("sub_account_name"),
        "credit_account": match["credit_line"].get("account_name"),
        "credit_sub_account": match["credit_line"].get("sub_account_name"),
        "credit_tax": match["credit_line"].get("tax_name"),
        "sales_accrual_match_count": 1 if _has_full_sales_accrual(match) else 0,
        "sales_accrual_candidate_count": len(match["sales_accrual_candidates"]),
        "sales_accrual_journals": [
            {
                "journal_number": str(candidate.get("journal_number") or ""),
                "journal_date": str(candidate.get("journal_date") or ""),
                "debit_account": str(candidate.get("debit_account") or ""),
                "credit_account": str(candidate.get("credit_account") or ""),
                "amount_status": str(candidate.get("amount_status") or ""),
                "score": candidate.get("score"),
                "reasons": candidate.get("reasons") or [],
                "native_source": str(candidate.get("native_source") or ""),
                "native_source_verified": candidate.get("native_source_verified")
                is True,
            }
            for candidate in match["sales_accrual_candidates"][:3]
        ],
        "bank_deposit_match": match["bank_deposit_match"],
        "collection_match_count": len(match["collection_candidates"]),
        "settlement_status": match["settlement_status"],
        "settlement_action": match["settlement_action"],
        "settlement_reasons": match["settlement_reasons"],
    }


def _collection_candidate(
    journal: dict[str, Any],
    lines: list[dict[str, Any]],
    billing_number: str,
    partner_name: str,
    invoice_amount: int,
    text: str,
) -> dict[str, Any] | None:
    debit = next((line for line in lines if line["side"] == "debit"), {})
    credit = next((line for line in lines if line["side"] == "credit"), {})
    if not _is_bank_deposit_invoice_collection(debit, credit):
        return None
    score = 0
    reasons: list[str] = []
    if billing_number and _contains_billing_number(text, billing_number):
        score += 50
        reasons.append("billing_number_in_collection_journal")
    if partner_name and _normalize_name(partner_name) in _normalize_name(text):
        score += 30
        reasons.append("partner_name_match")
    collection_amount = _line_amount(debit) or _line_amount(credit)
    if invoice_amount and collection_amount == invoice_amount:
        score += 25
        reasons.append("collection_amount_exact")
    elif invoice_amount and 0 < collection_amount < invoice_amount:
        score += 15
        reasons.append("collection_amount_partial")
    if score < 50:
        return None
    return {
        "journal_ref_hash": mask_identifier(str(journal.get("id") or "")),
        "journal_number": str(journal.get("number") or ""),
        "collection_amount": collection_amount,
        "score": score,
        "reasons": reasons,
        "text": text,
    }


def _sales_accrual_candidate(
    journal: dict[str, Any],
    lines: list[dict[str, Any]],
    billing_number: str,
    partner_name: str,
    invoice_amount: int,
    text: str,
    *,
    same_partner_amount_numbers: list[str] | None = None,
) -> dict[str, Any] | None:
    source = str(journal.get("entered_by") or "")
    if source and source != "JOURNAL_TYPE_BILLING":
        return None
    native_source_verified = source == "JOURNAL_TYPE_BILLING"
    debit = next((line for line in lines if line["side"] == "debit"), {})
    credit = next((line for line in lines if line["side"] == "credit"), {})
    if not _is_invoice_sales_accrual(debit, credit):
        return None
    if not native_source_verified and debit.get("account_name") != "未収入金":
        return None
    score = 0
    reasons: list[str] = []
    billing_number_match = bool(billing_number and _contains_billing_number(text, billing_number))
    if not billing_number_match:
        return None
    other_same_amount_number_match = any(
        number and _contains_billing_number(text, number)
        for number in (same_partner_amount_numbers or [])
    )
    if other_same_amount_number_match and not billing_number_match:
        return None
    score += 50
    reasons.append("billing_number_in_sales_accrual_journal")
    if native_source_verified:
        reasons.append("moneyforward_native_billing_source")
    if partner_name and _normalize_name(partner_name) in _normalize_name(text):
        score += 30
        reasons.append("partner_name_match")
    accrued_receivable = sum(
        int(_line_amount(line) or 0)
        for line in lines
        if line.get("side") == "debit"
        and line.get("account_name") in {"未収入金", "売掛金"}
    )
    if invoice_amount and accrued_receivable == invoice_amount:
        score += 25
        reasons.append("amount_match")
        amount_status = "full"
    elif invoice_amount and 0 < accrued_receivable < invoice_amount:
        reasons.append("amount_partial")
        amount_status = "partial"
    else:
        amount_status = "unverified"
    if score < 50:
        return None
    return {
        "journal_ref_hash": mask_identifier(str(journal.get("id") or "")),
        "journal_number": str(journal.get("number") or ""),
        "journal_date": str(journal.get("transaction_date") or ""),
        "debit_account": debit.get("account_name"),
        "credit_account": credit.get("account_name"),
        "amount_status": amount_status,
        "matched_gross_value": accrued_receivable,
        "score": score,
        "reasons": reasons,
        "native_source": source or "unverified",
        "native_source_verified": native_source_verified,
    }


def _journal_lines(journal: dict[str, Any]) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for branch in journal.get("branches") or []:
        if not isinstance(branch, dict):
            continue
        remark = str(branch.get("remark") or "")
        for side_key, side in (("debitor", "debit"), ("creditor", "credit")):
            side_data = branch.get(side_key) or {}
            if isinstance(side_data, dict):
                lines.append(
                    {
                        "side": side,
                        "remark": remark,
                        "account_name": _nested_name(side_data, "account"),
                        "sub_account_name": _nested_name(side_data, "sub_account"),
                        "department_name": _nested_name(side_data, "department"),
                        "tax_name": _nested_name(side_data, "tax"),
                        "trade_partner_name": _nested_name(side_data, "trade_partner"),
                        "amount": _line_amount(side_data),
                    }
                )
    return lines


def _journal_text(journal: dict[str, Any], lines: list[dict[str, Any]]) -> str:
    parts = [
        str(journal.get(key) or "")
        for key in ("memo", "transaction_date", "journal_type")
    ]
    for line in lines:
        parts.extend(str(line.get(key) or "") for key in ("remark", "account_name", "sub_account_name", "trade_partner_name"))
    return " ".join(parts)


def _nested_name(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if isinstance(value, dict):
        return str(value.get("name") or value.get("code") or "") or None
    for candidate in (f"{key}_name", key, f"{key}_id"):
        raw = data.get(candidate)
        if raw:
            return str(raw)
    return None


def _amount(record: dict[str, Any]) -> int:
    for key in ("total_price", "total", "amount", "value"):
        value = record.get(key)
        if value not in (None, ""):
            return int(float(str(value).replace(",", "")))
    return 0


def _line_amount(line: dict[str, Any]) -> int:
    return _amount(line)


def _parse_api_date(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    normalized = value.replace("/", "-")[:10]
    try:
        return date.fromisoformat(normalized).isoformat()
    except ValueError:
        return None


def _date_in_range(value: str | None, start_date: str, end_date: str) -> bool:
    return bool(value and start_date <= value <= end_date)


def _contains_billing_number(text: str, billing_number: str) -> bool:
    if not billing_number:
        return False
    escaped = re.escape(billing_number)
    patterns = [rf"\b{escaped}\b", rf"no\.?\s*{escaped}", rf"No\.?\s*{escaped}"]
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _normalize_name(value: str) -> str:
    normalized = re.sub(r"\s+", "", value)
    for token in ("株式会社", "有限会社", "合同会社", "(株)", "（株）"):
        normalized = normalized.replace(token, "")
    return normalized.lower()


def _is_bank_deposit_invoice_collection(debit_line: dict[str, Any], credit_line: dict[str, Any]) -> bool:
    debit_account = str(debit_line.get("account_name") or "")
    credit_account = str(credit_line.get("account_name") or "")
    return ("預金" in debit_account or debit_account == "現金") and credit_account == "未収入金"


def _is_invoice_sales_accrual(debit_line: dict[str, Any], credit_line: dict[str, Any]) -> bool:
    debit_account = str(debit_line.get("account_name") or "")
    credit_account = str(credit_line.get("account_name") or "")
    return debit_account in {"未収入金", "売掛金"} and "売上" in credit_account


def _has_transfer_fee_marker(text: str) -> bool:
    return bool(re.search(r"振込手数料|手数料|送金手数料", text))


def _has_withholding_marker(text: str) -> bool:
    return bool(re.search(r"源泉|源泉所得税|所得税", text))


def _amount_bucket(amount: int) -> str:
    if amount <= 0:
        return "none"
    if amount < 1_000:
        return "under_1000"
    if amount < 10_000:
        return "under_10000"
    if amount < 100_000:
        return "under_100000"
    return "100000_plus"


def _counts(values: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def result_to_json(result: InvoiceAccountingLinkProbeResult) -> str:
    return json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True)
