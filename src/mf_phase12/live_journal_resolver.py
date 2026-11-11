from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .accounting_api import MONEY_FORWARD_JOURNAL_TYPE, normalize_invoice_kind
from .hash_utils import stable_hash
from .local_artifacts import require_ignored_untracked_data_path
from .office_connection_probe import run_office_connection_probe


_TAX_NAME_ALIASES = {
    "課税仕入 10%": "課仕 10%",
    "課仕 10%": "課税仕入 10%",
    "課税仕入 (軽)8%": "課仕 (軽)8%",
    "課仕 (軽)8%": "課税仕入 (軽)8%",
}


class JournalMasterReadClient(Protocol):
    def offices(self) -> dict[str, Any]: ...
    def accounts(self, office_id: str | None = None) -> dict[str, Any]: ...
    def sub_accounts(self, office_id: str | None = None) -> dict[str, Any]: ...
    def taxes(self, office_id: str | None = None) -> dict[str, Any]: ...
    def trade_partners(self, office_id: str | None = None) -> dict[str, Any]: ...


@dataclass(frozen=True)
class JournalMasterResolutionSummary:
    status: str
    input_count: int
    ready_count: int
    blocked_count: int
    output_json: Path
    result_hash: str

    def to_redacted_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "input_count": self.input_count,
            "ready_count": self.ready_count,
            "blocked_count": self.blocked_count,
            "output_json": str(self.output_json),
            "result_hash": self.result_hash,
            "target_office_verified": True,
            "moneyforward_write_called": False,
            "raw_master_ids_printed": False,
            "raw_journal_bodies_printed": False,
            "raw_api_response_stored": False,
        }


def resolve_live_journal_specs_from_file(
    *,
    client: JournalMasterReadClient,
    input_json: Path,
    output_json: Path,
    office_id: str | None = None,
) -> JournalMasterResolutionSummary:
    input_path = require_ignored_untracked_data_path(input_json, purpose="live journal master input")
    output_path = require_ignored_untracked_data_path(output_json, purpose="live journal master output")
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("live journal master input must be an object")
    expected_office_name = _required_string(payload, "expected_office_name")
    office_probe = run_office_connection_probe(
        accounting_client=client,
        expected_office_name=expected_office_name,
    )
    if not office_probe.target_office_verified:
        raise ValueError("target Money Forward office was not uniquely verified")
    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("live journal master input requires items[]")

    masters = {
        "accounts": _extract_items(client.accounts(office_id)),
        "sub_accounts": _extract_items(client.sub_accounts(office_id)),
        "taxes": _extract_items(client.taxes(office_id)),
        "trade_partners": _extract_items(client.trade_partners(office_id)),
    }
    resolved_items = [_resolve_item(item, masters) for item in raw_items if isinstance(item, dict)]
    ready_count = sum(item["status"] == "ready" for item in resolved_items)
    blocked_count = len(resolved_items) - ready_count
    result = {
        "status": "ready" if blocked_count == 0 else "blocked",
        "expected_office_hash": office_probe.expected_office_hash,
        "items": resolved_items,
        "moneyforward_write_called": False,
        "raw_api_response_stored": False,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return JournalMasterResolutionSummary(
        status=result["status"],
        input_count=len(resolved_items),
        ready_count=ready_count,
        blocked_count=blocked_count,
        output_json=output_path,
        result_hash=stable_hash(result),
    )


def _resolve_item(item: dict[str, Any], masters: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    source_file_hash = _required_string(item, "source_file_hash")
    if not _is_sha256_marker(source_file_hash):
        raise ValueError("source_file_hash must be sha256:<64 hex>")
    spec = item.get("journal")
    if not isinstance(spec, dict):
        raise ValueError("journal spec must be an object")
    transaction_date = _required_string(spec, "transaction_date")[:10]
    branches = spec.get("branches")
    if not isinstance(branches, list) or not branches:
        raise ValueError("journal spec requires branches[]")

    missing: list[dict[str, str]] = []
    resolved_branches: list[dict[str, Any]] = []
    for index, branch in enumerate(branches):
        if not isinstance(branch, dict):
            raise ValueError(f"branch {index} must be an object")
        debitor = _resolve_side(branch.get("debitor"), masters, missing, f"branches[{index}].debitor")
        creditor = _resolve_side(branch.get("creditor"), masters, missing, f"branches[{index}].creditor")
        if debitor.get("value") != creditor.get("value"):
            raise ValueError(f"branch {index} is unbalanced")
        resolved_branch = {"debitor": debitor, "creditor": creditor}
        remark = str(branch.get("remark") or "").strip()
        if remark:
            resolved_branch["remark"] = remark
        resolved_branches.append(resolved_branch)

    journal: dict[str, Any] = {
        "transaction_date": transaction_date,
        "journal_type": MONEY_FORWARD_JOURNAL_TYPE,
        "branches": resolved_branches,
    }
    for field in ("memo", "tags"):
        if spec.get(field) not in (None, "", []):
            journal[field] = spec[field]
    return {
        "source_file_hash": source_file_hash,
        "status": "blocked" if missing else "ready",
        "missing_masters": missing,
        "journal": journal,
        "journal_hash": stable_hash(journal),
    }


def _resolve_side(
    side: Any,
    masters: dict[str, list[dict[str, Any]]],
    missing: list[dict[str, str]],
    path: str,
) -> dict[str, Any]:
    if not isinstance(side, dict):
        raise ValueError(f"{path} must be an object")
    value = int(side.get("value") or 0)
    if value <= 0:
        raise ValueError(f"{path}.value must be positive")
    account_name = _required_string(side, "account_name")
    account = _unique_named(masters["accounts"], account_name)
    resolved: dict[str, Any] = {"value": value}
    if account is None:
        missing.append(_missing(path, "account", account_name))
    else:
        resolved["account_id"] = _external_id(account)

    sub_account_name = _optional_string(side.get("sub_account_name"))
    if sub_account_name:
        sub_account = _unique_sub_account(
            masters["sub_accounts"],
            sub_account_name=sub_account_name,
            account_name=account_name,
            account_id=_external_id(account) if account else None,
        )
        if sub_account is None:
            missing.append(_missing(path, "sub_account", f"{account_name}/{sub_account_name}"))
        else:
            resolved["sub_account_id"] = _external_id(sub_account)

    tax_name = _optional_string(side.get("tax_name"))
    if tax_name:
        tax = _unique_named(masters["taxes"], tax_name, aliases=_tax_name_aliases(tax_name))
        if tax is None:
            missing.append(_missing(path, "tax", tax_name))
        else:
            resolved["tax_id"] = _external_id(tax)

    trade_partner_name = _optional_string(side.get("trade_partner_name"))
    if trade_partner_name:
        partner = _unique_named(masters["trade_partners"], trade_partner_name)
        code = str(partner.get("code") or "").strip() if partner else ""
        if not code:
            missing.append(_missing(path, "trade_partner", trade_partner_name))
        else:
            resolved["trade_partner_code"] = code

    invoice_kind = _optional_string(side.get("invoice_kind"))
    if invoice_kind:
        resolved["invoice_kind"] = normalize_invoice_kind(invoice_kind)
    return resolved


def _unique_named(
    items: list[dict[str, Any]],
    expected_name: str,
    *,
    aliases: set[str] | None = None,
) -> dict[str, Any] | None:
    exact = _normalize(expected_name)
    matches = [item for item in items if _normalize(_name(item)) == exact and item.get("available") is not False]
    if matches or not aliases:
        return matches[0] if len(matches) == 1 else None
    expected = {_normalize(value) for value in aliases} - {exact}
    matches = [item for item in items if _normalize(_name(item)) in expected and item.get("available") is not False]
    return matches[0] if len(matches) == 1 else None


def _tax_name_aliases(name: str) -> set[str]:
    return {name, _TAX_NAME_ALIASES[name]} if name in _TAX_NAME_ALIASES else {name}


def _unique_sub_account(
    items: list[dict[str, Any]],
    *,
    sub_account_name: str,
    account_name: str,
    account_id: str | None,
) -> dict[str, Any] | None:
    expected_sub = _normalize(sub_account_name)
    expected_account = _normalize(account_name)
    candidates = [
        item
        for item in items
        if _normalize(_name(item)) == expected_sub and item.get("available") is not False
    ]
    parent_filtered = [
        item
        for item in candidates
        if (account_id and _parent_account_id(item) == account_id)
        or (_parent_account_name(item) and _normalize(_parent_account_name(item) or "") == expected_account)
    ]
    if len(parent_filtered) == 1:
        return parent_filtered[0]
    return candidates[0] if len(candidates) == 1 else None


def _extract_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if _name(payload) or payload.get("id"):
            return [payload]
    return []


def _name(item: dict[str, Any]) -> str:
    return str(item.get("name") or item.get("display_name") or item.get("title") or "").strip()


def _external_id(item: dict[str, Any] | None) -> str:
    if not item:
        return ""
    return str(item.get("id") or item.get("account_id") or item.get("sub_account_id") or "").strip()


def _parent_account_id(item: dict[str, Any]) -> str:
    return str(item.get("account_id") or item.get("account_item_id") or item.get("parent_account_id") or "").strip()


def _parent_account_name(item: dict[str, Any]) -> str | None:
    nested = item.get("account") or item.get("account_item") or item.get("parent_account")
    if isinstance(nested, dict) and _name(nested):
        return _name(nested)
    for key in ("account_name", "account_item_name", "parent_account_name"):
        if item.get(key):
            return str(item[key]).strip()
    return None


def _missing(path: str, master_type: str, name: str) -> dict[str, str]:
    return {"path": path, "master_type": master_type, "name_hash": stable_hash(_normalize(name))}


def _normalize(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def _required_string(payload: dict[str, Any], field: str) -> str:
    value = str(payload.get(field) or "").strip()
    if not value:
        raise ValueError(f"{field} is required")
    return value


def _optional_string(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _is_sha256_marker(value: str) -> bool:
    if not value.startswith("sha256:"):
        return False
    digest = value.removeprefix("sha256:")
    return len(digest) == 64 and all(ch in "0123456789abcdef" for ch in digest.lower())
