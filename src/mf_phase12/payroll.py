from __future__ import annotations
from .shiwake_run import _setting

GENERIC_EMPLOYEE_SUBACCOUNTS: frozenset[str] = frozenset(
    {
        "給与",
        "賞与",
        "従業員",
        "社員",
        "全従業員",
        "全社員",
        "スタッフ",
        "役員",
        "未払給与",
        "未払賞与",
        "給与未払",
        "賞与未払",
    }
)
LABOR_INSURANCE_LABEL_FRAGMENTS: tuple[str, ...] = (
    "雇用保険",
    "労災保険",
    "一般拠出金",
)
PAYMENT_LABEL_FRAGMENTS: tuple[str, ...] = (
    "基本給",
    "役職手当",
    "職務手当",
    "通勤手当",
    "通勤費",
    "残業手当",
    "時間外手当",
    "深夜手当",
    "休日手当",
    "家族手当",
    "住宅手当",
    "資格手当",
    "皆勤手当",
    "手当",
    "賞与",
    "歩合",
    "支給",
)
ATTENDANCE_LABEL_FRAGMENTS: tuple[str, ...] = (
    "出勤日数",
    "欠勤日数",
    "有給",
    "労働時間",
    "残業時間",
    "深夜時間",
    "休日労働",
    "遅刻",
    "早退",
    "日数",
    "時間",
)
NET_LABEL_FRAGMENTS: tuple[str, ...] = (
    "差引支給額",
    "差引支給",
    "手取",
    "銀行振込額",
    "振込額",
)


class _ProfilePayrollRules:
    """Resolve accounting mappings at call time, never at import time."""

    def __getattr__(self, name):
        if name in {
            "GENERIC_EMPLOYEE_SUBACCOUNTS",
            "LABOR_INSURANCE_LABEL_FRAGMENTS",
            "PAYMENT_LABEL_FRAGMENTS",
            "ATTENDANCE_LABEL_FRAGMENTS",
            "NET_LABEL_FRAGMENTS",
        }:
            return globals()[name]
        value = _setting("payroll.rules." + name.lower())
        if isinstance(value, str) and (not value.strip()):
            raise ValueError("payroll accounting mapping is empty: " + name)
        return value

    def deduction_sub_account(self, canonical_label):
        matches = {
            str(sub)
            for fragment, sub in self.DEDUCTION_SUBACCOUNT_RULES
            if str(fragment) and str(fragment) in canonical_label
        }
        if len(matches) > 1:
            raise ValueError("payroll deduction mapping is ambiguous")
        return next(iter(matches)) if matches else None

    def is_generic_employee_sub_account(self, value):
        normalized = "".join(value.split())
        return not normalized or normalized in GENERIC_EMPLOYEE_SUBACCOUNTS


fixed_rules = _ProfilePayrollRules()
from dataclasses import dataclass, field
from typing import Any

PAYMENT = "payment"
DEDUCTION = "deduction"
ATTENDANCE = "attendance"
NET = "net"
COMPANY_SOCIAL_INSURANCE = "company_social_insurance"
UNCLASSIFIED = "unclassified"
MONEY_CATEGORIES = (PAYMENT, DEDUCTION, NET, COMPANY_SOCIAL_INSURANCE, UNCLASSIFIED)


@dataclass(frozen=True)
class PayrollLineItem:
    """One line of a payroll statement.

    ``amount`` is held only in memory for balance computation; it is never
    written to a persisted artifact. ``canonical_label`` is a normalized,
    non-personal item name (e.g. ``健康保険料``); ``raw_label`` is the source
    label and is never serialized to avoid leaking accidental personal data.
    """

    raw_label: str
    canonical_label: str
    category: str
    amount: int = 0
    sub_account: str | None = None

    @property
    def is_money(self) -> bool:
        return self.category in MONEY_CATEGORIES and self.category != ATTENDANCE


@dataclass(frozen=True)
class PayrollEmployee:
    """Employee-level v2 input kept only in memory.

    ``employee_ref`` must be an opaque caller-controlled reference. Both it and
    ``payable_sub_account`` may identify a person, so neither is serialized to
    redacted artifacts.
    """

    employee_ref: str
    payable_sub_account: str
    allocation_evidence_hash: str
    items: tuple[PayrollLineItem, ...]
    role: str = "employee"

    def items_in(self, category: str) -> tuple[PayrollLineItem, ...]:
        return tuple((item for item in self.items if item.category == category))

    def total_for(self, category: str) -> int:
        return sum((item.amount for item in self.items_in(category)))

    @property
    def payment_total(self) -> int:
        return self.total_for(PAYMENT)

    @property
    def deduction_total(self) -> int:
        return self.total_for(DEDUCTION)

    @property
    def unclassified_total(self) -> int:
        return self.total_for(UNCLASSIFIED)

    @property
    def explicit_net_total(self) -> int | None:
        net_items = self.items_in(NET)
        if not net_items:
            return None
        return sum((item.amount for item in net_items))

    @property
    def derived_net_total(self) -> int:
        return self.payment_total - self.deduction_total

    @property
    def net_total(self) -> int:
        explicit = self.explicit_net_total
        return explicit if explicit is not None else self.derived_net_total


@dataclass(frozen=True)
class PayrollStatement:
    period: str
    source_channel: str
    items: tuple[PayrollLineItem, ...]
    employee_count: int = 0
    synthetic: bool = False
    identifier_tag: str = ""
    schema_version: int = 1
    payroll_type: str = "salary"
    employees: tuple[PayrollEmployee, ...] = field(default_factory=tuple)

    def items_in(self, category: str) -> tuple[PayrollLineItem, ...]:
        return tuple((item for item in self.items if item.category == category))

    def total_for(self, category: str) -> int:
        return sum((item.amount for item in self.items_in(category)))

    @property
    def payment_total(self) -> int:
        return self.total_for(PAYMENT)

    @property
    def deduction_total(self) -> int:
        return self.total_for(DEDUCTION)

    @property
    def company_social_insurance_total(self) -> int:
        return self.total_for(COMPANY_SOCIAL_INSURANCE)

    @property
    def unclassified_total(self) -> int:
        return self.total_for(UNCLASSIFIED)

    @property
    def explicit_net_total(self) -> int | None:
        net_items = self.items_in(NET)
        if not net_items:
            return None
        return sum((item.amount for item in net_items))

    @property
    def derived_net_total(self) -> int:
        return self.payment_total - self.deduction_total

    @property
    def net_total(self) -> int:
        if self.employees:
            return sum((employee.net_total for employee in self.employees))
        explicit = self.explicit_net_total
        return explicit if explicit is not None else self.derived_net_total

    @property
    def has_unclassified_money(self) -> bool:
        return bool(self.items_in(UNCLASSIFIED))

    @property
    def company_social_insurance_present(self) -> bool:
        return bool(self.items_in(COMPANY_SOCIAL_INSURANCE))

    @property
    def employee_detail_present(self) -> bool:
        return bool(self.employees)

    @property
    def legacy_aggregate(self) -> bool:
        return not self.employee_detail_present


@dataclass(frozen=True)
class JournalLine:
    side: str
    account: str
    amount: int
    sub_account: str | None = None
    sensitive_sub_account: bool = False

    def __post_init__(self) -> None:
        if self.amount < 0:
            raise ValueError("journal line amounts must be non-negative")

    def redacted(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "account": self.account,
            "sub_account": "employee_specific_redacted"
            if self.sensitive_sub_account and self.sub_account
            else self.sub_account,
            "amount": "redacted",
        }


@dataclass(frozen=True)
class PayrollValidationResult:
    balanced: bool
    payment_equals_deduction_plus_net: bool
    required_deduction_categories_present: bool
    missing_deduction_categories: tuple[str, ...] = ()
    has_unclassified_money_items: bool = False
    company_social_insurance_present: bool = False
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "balanced": self.balanced,
            "payment_equals_deduction_plus_net": self.payment_equals_deduction_plus_net,
            "required_deduction_categories_present": self.required_deduction_categories_present,
            "missing_deduction_categories": list(self.missing_deduction_categories),
            "has_unclassified_money_items": self.has_unclassified_money_items,
            "company_social_insurance_present": self.company_social_insurance_present,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class PayrollJournalCandidate:
    period: str
    source_channel: str
    lines: tuple[JournalLine, ...]
    validation: PayrollValidationResult
    confidence: float
    requires_human_review: bool = True
    registration_allowed: bool = False
    risk_flags: tuple[str, ...] = field(default_factory=tuple)
    unresolved_points: tuple[str, ...] = field(default_factory=tuple)
    human_review_points: tuple[str, ...] = field(default_factory=tuple)
    employee_allocation_signature: tuple[
        tuple[str, str, str, int, int, int, int], ...
    ] = field(default_factory=tuple)

    @property
    def debit_total(self) -> int:
        return sum((line.amount for line in self.lines if line.side == "debit"))

    @property
    def credit_total(self) -> int:
        return sum((line.amount for line in self.lines if line.side == "credit"))

    @property
    def balanced(self) -> bool:
        return self.debit_total == self.credit_total

    def amount_signature(self) -> list[int]:
        """Sorted amounts used only for a non-reversible idempotency hash."""
        return sorted((line.amount for line in self.lines))

    def redacted_lines(self) -> list[dict[str, Any]]:
        return [line.redacted() for line in self.lines]


import json
import re
from pathlib import Path
from typing import Any

INPUT_SCHEMA_V1 = "moneyforward-ai-poc-phase3-payroll-input-v1"
INPUT_SCHEMA_V2 = "moneyforward-ai-poc-phase3-payroll-input-v2"
INPUT_SCHEMA = INPUT_SCHEMA_V1
SUPPORTED_INPUT_SCHEMAS = (INPUT_SCHEMA_V1, INPUT_SCHEMA_V2)


class PayrollInputError(ValueError):
    """Raised when a payroll statement input cannot be parsed safely."""


def _as_int_amount(value: Any, *, label: str) -> int:
    if isinstance(value, bool):
        raise PayrollInputError(f"amount for '{label}' must be a number")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise PayrollInputError(f"amount for '{label}' must be a whole yen value")
        return int(value)
    if isinstance(value, str):
        cleaned = value.replace(",", "").replace("¥", "").replace("円", "").strip()
        if re.fullmatch("-?\\d+", cleaned):
            return int(cleaned)
    raise PayrollInputError(f"amount for '{label}' is not a valid integer yen value")


def _money_item(entry: Any, category: str) -> PayrollLineItem:
    if not isinstance(entry, dict):
        raise PayrollInputError(
            f"{category} item must be an object with label and amount"
        )
    label = str(entry.get("label", "")).strip()
    if not label:
        raise PayrollInputError(f"{category} item is missing a label")
    amount = _as_int_amount(entry.get("amount", 0), label=label)
    sub_account = (
        fixed_rules.deduction_sub_account(label) if category == DEDUCTION else None
    )
    return PayrollLineItem(
        raw_label=label,
        canonical_label=label,
        category=category,
        amount=amount,
        sub_account=sub_account,
    )


def _employee_money_items(entry: dict[str, Any], index: int) -> PayrollEmployee:
    employee_ref = str(entry.get("employee_ref", entry.get("opaque_ref", ""))).strip()
    if not employee_ref:
        raise PayrollInputError(f"employees[{index}] is missing employee_ref")
    payable_sub_account = str(
        entry.get("sub_account", entry.get("payable_sub_account", ""))
    ).strip()
    if not payable_sub_account:
        raise PayrollInputError(f"employees[{index}] is missing sub_account")
    if fixed_rules.is_generic_employee_sub_account(payable_sub_account):
        raise PayrollInputError(
            f"employees[{index}] sub_account must uniquely identify one employee"
        )
    allocation_evidence_hash = str(entry.get("allocation_evidence_hash", "")).strip()
    if not re.fullmatch("sha256:[0-9a-f]{64}", allocation_evidence_hash):
        raise PayrollInputError(
            f"employees[{index}] requires allocation_evidence_hash as sha256:<64 lowercase hex>"
        )
    role = str(entry.get("role") or "employee").strip().lower()
    if role not in {"employee", "executive"}:
        raise PayrollInputError(
            f"employees[{index}] role must be employee or executive"
        )
    items: list[PayrollLineItem] = []
    for item in entry.get("payment_items", []) or []:
        items.append(_money_item(item, PAYMENT))
    for item in entry.get("deduction_items", []) or []:
        items.append(_money_item(item, DEDUCTION))
    for item in entry.get("unclassified_items", []) or []:
        items.append(_money_item(item, UNCLASSIFIED))
    net = entry.get("net_payment")
    if net is None and "net_amount" in entry:
        net = {"label": "差引支給額", "amount": entry["net_amount"]}
    elif isinstance(net, (int, float, str)) and (not isinstance(net, bool)):
        net = {"label": "差引支給額", "amount": net}
    if net is not None:
        items.append(_money_item(net, NET))
    if not any((item.category == NET for item in items)) and (
        not any((item.category == PAYMENT for item in items))
    ):
        raise PayrollInputError(
            f"employees[{index}] requires net_payment or employee-level payment_items"
        )
    return PayrollEmployee(
        employee_ref=employee_ref,
        payable_sub_account=payable_sub_account,
        allocation_evidence_hash=allocation_evidence_hash,
        items=tuple(items),
        role=role,
    )


def parse_statement_payload(payload: dict[str, Any]) -> PayrollStatement:
    """Parse a normalized payroll input payload into a PayrollStatement."""
    if not isinstance(payload, dict):
        raise PayrollInputError("payroll input must be a JSON object")
    schema = payload.get("schema")
    if schema is not None and schema not in SUPPORTED_INPUT_SCHEMAS:
        raise PayrollInputError(f"unsupported payroll input schema: {schema!r}")
    schema_version = 2 if schema == INPUT_SCHEMA_V2 else 1
    period = str(payload.get("period", "")).strip()
    if not re.fullmatch("\\d{4}-\\d{2}", period):
        raise PayrollInputError("period must be YYYY-MM")
    source_channel = str(
        payload.get("source_channel", "local_payroll_statement_fallback")
    ).strip()
    items: list[PayrollLineItem] = []
    for entry in payload.get("payment_items", []) or []:
        items.append(_money_item(entry, PAYMENT))
    for entry in payload.get("deduction_items", []) or []:
        items.append(_money_item(entry, DEDUCTION))
    for entry in payload.get("company_social_insurance", []) or []:
        items.append(_money_item(entry, COMPANY_SOCIAL_INSURANCE))
    for entry in payload.get("attendance_items", []) or []:
        if not isinstance(entry, dict):
            raise PayrollInputError("attendance item must be an object with a label")
        label = str(entry.get("label", "")).strip()
        if not label:
            raise PayrollInputError("attendance item is missing a label")
        if (
            entry.get("amount") not in (None, 0, 0.0)
            and _as_int_amount(entry.get("amount"), label=label) != 0
        ):
            items.append(_money_item(entry, UNCLASSIFIED))
        else:
            items.append(
                PayrollLineItem(
                    raw_label=label,
                    canonical_label=label,
                    category=ATTENDANCE,
                    amount=0,
                )
            )
    net = payload.get("net_payment")
    if net is not None:
        items.append(_money_item(net, NET))
    for entry in payload.get("unclassified_items", []) or []:
        items.append(_money_item(entry, UNCLASSIFIED))
    employees: tuple[PayrollEmployee, ...] = ()
    if schema_version == 2:
        raw_employees = payload.get("employees")
        if not isinstance(raw_employees, list) or not raw_employees:
            raise PayrollInputError(
                "v2 payroll input requires a non-empty employees array"
            )
        parsed_employees: list[PayrollEmployee] = []
        for index, entry in enumerate(raw_employees):
            if not isinstance(entry, dict):
                raise PayrollInputError(f"employees[{index}] must be an object")
            parsed_employees.append(_employee_money_items(entry, index))
        employees = tuple(parsed_employees)
        refs = [employee.employee_ref for employee in employees]
        if len(refs) != len(set(refs)):
            raise PayrollInputError("employee_ref values must be unique")
        sub_accounts = [employee.payable_sub_account for employee in employees]
        if len(sub_accounts) != len(set(sub_accounts)):
            raise PayrollInputError("employee sub_account values must be unique")
        if not any((item.category == PAYMENT for item in items)):
            items.extend(
                (item for employee in employees for item in employee.items_in(PAYMENT))
            )
        if not any((item.category == DEDUCTION for item in items)):
            items.extend(
                (
                    item
                    for employee in employees
                    for item in employee.items_in(DEDUCTION)
                )
            )
        if not any((item.category == NET for item in items)):
            items.extend(
                (item for employee in employees for item in employee.items_in(NET))
            )
        if not any((item.category == UNCLASSIFIED for item in items)):
            items.extend(
                (
                    item
                    for employee in employees
                    for item in employee.items_in(UNCLASSIFIED)
                )
            )
    if not any((item.category == PAYMENT for item in items)):
        raise PayrollInputError("at least one payment_items entry is required")
    if schema_version == 1 and (
        not any((item.category == DEDUCTION for item in items))
    ):
        raise PayrollInputError("at least one deduction_items entry is required")
    employee_count = int(payload.get("employee_count", 0) or 0)
    if employees:
        if employee_count not in (0, len(employees)):
            raise PayrollInputError("employee_count does not match employees array")
        employee_count = len(employees)
    synthetic = bool(payload.get("synthetic", False))
    identifier_tag = str(payload.get("identifier", "")).strip()
    payroll_type = (
        str(
            payload.get(
                "payroll_type",
                payload.get("statement_type", payload.get("payroll_kind", "salary")),
            )
        )
        .strip()
        .lower()
    )
    if payroll_type not in {"salary", "bonus"}:
        raise PayrollInputError("payroll_type must be 'salary' or 'bonus'")
    return PayrollStatement(
        period=period,
        source_channel=source_channel,
        items=tuple(items),
        employee_count=employee_count,
        synthetic=synthetic,
        identifier_tag=identifier_tag,
        schema_version=schema_version,
        payroll_type=payroll_type,
        employees=employees,
    )


def parse_statement_json(path: Path) -> PayrollStatement:
    raw = Path(path).read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PayrollInputError(f"payroll input is not valid JSON: {exc}") from exc
    return parse_statement_payload(payload)


def map_statement_text(text: str) -> dict[str, Any]:
    """Best-effort map of ``pdftotext`` output lines into an input payload.

    Money rows are routed to payment/deduction/net by fixed-rule fragments.
    Rows that carry an amount but match no rule become ``unclassified_items`` so
    they are surfaced for human review rather than silently dropped. The caller
    must set ``period`` before parsing.
    """
    payment: list[dict[str, Any]] = []
    deduction: list[dict[str, Any]] = []
    attendance: list[dict[str, Any]] = []
    unclassified: list[dict[str, Any]] = []
    net_payment: dict[str, Any] | None = None
    line_re = re.compile("^(?P<label>.+?)[\\s:：]+(?P<amount>-?[\\d,]+)\\s*円?$")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = line_re.match(line)
        if not match:
            continue
        label = match.group("label").strip()
        amount_token = match.group("amount")
        entry = {"label": label, "amount": amount_token}
        if any((fragment in label for fragment in fixed_rules.NET_LABEL_FRAGMENTS)):
            net_payment = entry
        elif fixed_rules.deduction_sub_account(label) is not None:
            deduction.append(entry)
        elif any(
            (fragment in label for fragment in fixed_rules.PAYMENT_LABEL_FRAGMENTS)
        ):
            payment.append(entry)
        elif any(
            (fragment in label for fragment in fixed_rules.ATTENDANCE_LABEL_FRAGMENTS)
        ):
            attendance.append({"label": label})
        else:
            unclassified.append(entry)
    payload: dict[str, Any] = {
        "schema": INPUT_SCHEMA,
        "source_channel": "local_payroll_statement_pdf_text_fallback",
        "payment_items": payment,
        "deduction_items": deduction,
        "attendance_items": attendance,
    }
    if net_payment is not None:
        payload["net_payment"] = net_payment
    if unclassified:
        payload["unclassified_items"] = unclassified
    return payload


import hashlib
from collections import OrderedDict


def _private_key_hash(namespace: str, raw_value: str) -> str:
    """Stable pseudonymous key used only inside the final allocation hash."""
    value = f"phase3-payroll-{namespace}:{raw_value}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _grouped_deductions(statement: PayrollStatement) -> "OrderedDict[str, int]":
    """Sum employee deductions by 預り金 sub-account, preserving order."""
    grouped: "OrderedDict[str, int]" = OrderedDict()
    for item in statement.items_in(DEDUCTION):
        sub_account = (
            item.sub_account or fixed_rules.CREDIT_UNKNOWN_DEDUCTION_SUBACCOUNT
        )
        grouped[sub_account] = grouped.get(sub_account, 0) + item.amount
    return grouped


def _grouped_company_insurance(statement: PayrollStatement) -> tuple[int, int]:
    social_insurance = 0
    labor_insurance = 0
    for item in statement.items_in(COMPANY_SOCIAL_INSURANCE):
        if any(
            (
                fragment in item.canonical_label
                for fragment in fixed_rules.LABOR_INSURANCE_LABEL_FRAGMENTS
            )
        ):
            labor_insurance += item.amount
        else:
            social_insurance += item.amount
    return (social_insurance, labor_insurance)


def _validate(
    statement: PayrollStatement,
    deductions: "OrderedDict[str, int]",
    debit_total: int,
    credit_total: int,
) -> PayrollValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    balanced = debit_total == credit_total
    if not balanced:
        errors.append("debit total does not equal credit total")
    explicit_net = statement.explicit_net_total
    aggregate_net = (
        explicit_net if explicit_net is not None else statement.derived_net_total
    )
    payment_equals = (
        statement.payment_total == statement.deduction_total + aggregate_net
    )
    if explicit_net is not None and explicit_net != statement.derived_net_total:
        errors.append(
            "explicit net payment does not equal payment total minus deductions"
        )
    if statement.employee_detail_present:
        employee_gross_total = sum(
            (employee.payment_total for employee in statement.employees)
        )
        if employee_gross_total != statement.payment_total:
            errors.append("employee gross total does not equal aggregate payment total")
        employee_deduction_total = sum(
            (employee.deduction_total for employee in statement.employees)
        )
        if employee_deduction_total != statement.deduction_total:
            errors.append(
                "employee deduction total does not equal aggregate deduction total"
            )
        employee_net_total = sum(
            (employee.net_total for employee in statement.employees)
        )
        if employee_net_total != aggregate_net:
            errors.append("employee net total does not equal aggregate net total")
        employee_unclassified_total = sum(
            (employee.unclassified_total for employee in statement.employees)
        )
        if employee_unclassified_total != statement.unclassified_total:
            errors.append(
                "employee unclassified total does not equal aggregate unclassified total"
            )
        for employee in statement.employees:
            if (
                employee.explicit_net_total is not None
                and (employee.items_in(PAYMENT) or employee.items_in(DEDUCTION))
                and (employee.explicit_net_total != employee.derived_net_total)
            ):
                errors.append(
                    "an employee net payment does not equal payments minus deductions"
                )
            if not employee.allocation_evidence_hash:
                errors.append("employee allocation evidence hash is required")
        if any(
            (
                fixed_rules.is_generic_employee_sub_account(
                    employee.payable_sub_account
                )
                for employee in statement.employees
            )
        ):
            errors.append("generic employee payable sub-account is not allowed")
    present = set(deductions.keys())
    missing = tuple(
        (
            sub
            for sub in fixed_rules.REQUIRED_DEDUCTION_SUBACCOUNTS
            if sub not in present
        )
    )
    if missing:
        warnings.append(
            "missing expected deduction sub-accounts: " + ", ".join(missing)
        )
    unclassified = statement.has_unclassified_money
    if unclassified:
        warnings.append("statement has money items that no fixed rule classified")
    unknown_deductions = any(
        (item.sub_account is None for item in statement.items_in(DEDUCTION))
    )
    if unknown_deductions:
        warnings.append("statement has deductions without a fixed sub-account rule")
    company_present = statement.company_social_insurance_present
    if not company_present:
        warnings.append("company-portion social insurance (法定福利費) not provided")
    return PayrollValidationResult(
        balanced=balanced,
        payment_equals_deduction_plus_net=payment_equals,
        required_deduction_categories_present=not missing,
        missing_deduction_categories=missing,
        has_unclassified_money_items=unclassified,
        company_social_insurance_present=company_present,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


def build_payroll_journal_candidate(
    statement: PayrollStatement, *, net_credit_account: str = None
) -> PayrollJournalCandidate:
    """Build a masked monthly payroll journal candidate from a statement.

    Employee-level statements become registration-ready when every monetary
    item is classified, aggregate totals match, and each employee allocation
    is bound to canonical evidence. Aggregate-only legacy inputs remain
    non-postable.
    """
    net_credit_account = (
        net_credit_account
        if net_credit_account is not None
        else fixed_rules.CREDIT_NET_ACCOUNT_DEFAULT
    )
    if net_credit_account not in fixed_rules.ALLOWED_NET_CREDIT_ACCOUNTS:
        raise ValueError("unsupported net credit account")
    has_negative_net = (
        any((employee.net_total < 0 for employee in statement.employees))
        if statement.employee_detail_present
        else statement.net_total < 0
    )
    if (
        has_negative_net
        and net_credit_account != fixed_rules.CREDIT_NET_ACCOUNT_DEFAULT
    ):
        raise ValueError(
            "negative net requires the configured default net liability account"
        )
    deductions = _grouped_deductions(statement)
    gross = statement.payment_total
    net = statement.net_total
    company_social_insurance, company_labor_insurance = _grouped_company_insurance(
        statement
    )
    lines: list[JournalLine] = []
    if statement.payroll_type == "bonus" and statement.employee_detail_present:
        for employee in statement.employees:
            if employee.payment_total:
                lines.append(
                    JournalLine(
                        "debit",
                        fixed_rules.DEBIT_BONUS_ACCOUNT,
                        employee.payment_total,
                        employee.payable_sub_account,
                        sensitive_sub_account=True,
                    )
                )
    elif statement.payroll_type == "bonus":
        lines.append(JournalLine("debit", fixed_rules.DEBIT_BONUS_ACCOUNT, gross))
    elif statement.employee_detail_present:
        executive_gross = sum(
            (
                employee.payment_total
                for employee in statement.employees
                if employee.role == "executive"
            )
        )
        employee_gross = gross - executive_gross
        if executive_gross:
            lines.append(
                JournalLine(
                    "debit",
                    _setting("payroll.rules.debit_executive_salary_account"),
                    executive_gross,
                )
            )
        if employee_gross:
            lines.append(
                JournalLine("debit", fixed_rules.DEBIT_SALARY_ACCOUNT, employee_gross)
            )
    else:
        lines.append(JournalLine("debit", fixed_rules.DEBIT_SALARY_ACCOUNT, gross))
    if company_social_insurance:
        lines.append(
            JournalLine(
                "debit",
                fixed_rules.DEBIT_LEGAL_WELFARE_ACCOUNT,
                company_social_insurance,
                fixed_rules.DEBIT_COMPANY_SOCIAL_INSURANCE_SUBACCOUNT,
            )
        )
    if company_labor_insurance:
        lines.append(
            JournalLine(
                "debit",
                fixed_rules.DEBIT_LEGAL_WELFARE_ACCOUNT,
                company_labor_insurance,
                fixed_rules.DEBIT_COMPANY_LABOR_INSURANCE_SUBACCOUNT,
            )
        )
    for sub_account, amount in deductions.items():
        lines.append(
            JournalLine(
                "credit", fixed_rules.CREDIT_WITHHOLDING_ACCOUNT, amount, sub_account
            )
        )
    if statement.employee_detail_present:
        for employee in statement.employees:
            employee_net = employee.net_total
            if employee_net > 0:
                lines.append(
                    JournalLine(
                        "credit",
                        net_credit_account,
                        employee_net,
                        employee.payable_sub_account,
                        sensitive_sub_account=True,
                    )
                )
            elif employee_net < 0:
                lines.append(
                    JournalLine(
                        "debit",
                        net_credit_account,
                        abs(employee_net),
                        employee.payable_sub_account,
                        sensitive_sub_account=True,
                    )
                )
    elif net > 0:
        lines.append(JournalLine("credit", net_credit_account, net))
    elif net < 0:
        lines.append(JournalLine("debit", net_credit_account, abs(net)))
    if company_social_insurance:
        lines.append(
            JournalLine(
                "credit",
                fixed_rules.CREDIT_COMPANY_SI_ACCOUNT,
                company_social_insurance,
                fixed_rules.CREDIT_COMPANY_SOCIAL_INSURANCE_SUBACCOUNT,
            )
        )
    if company_labor_insurance:
        lines.append(
            JournalLine(
                "credit",
                fixed_rules.CREDIT_COMPANY_SI_ACCOUNT,
                company_labor_insurance,
                fixed_rules.CREDIT_COMPANY_LABOR_INSURANCE_SUBACCOUNT,
            )
        )
    debit_total = sum((line.amount for line in lines if line.side == "debit"))
    credit_total = sum((line.amount for line in lines if line.side == "credit"))
    validation = _validate(statement, deductions, debit_total, credit_total)
    risk_flags: list[str] = []
    human_review_points: list[str] = []
    unresolved_points: list[str] = []
    generic_sub_accounts = False
    if statement.legacy_aggregate:
        risk_flags.append("legacy_aggregate_input_not_postable")
        unresolved_points.append(
            "legacy v1集計入力は従業員別補助科目を欠くため投稿候補として使用しない"
        )
        if net < 0:
            risk_flags.append("negative_net_without_employee_sub_account")
    else:
        generic_sub_accounts = any(
            (
                fixed_rules.is_generic_employee_sub_account(
                    employee.payable_sub_account
                )
                for employee in statement.employees
            )
        )
        if generic_sub_accounts:
            risk_flags.append("generic_employee_sub_account")
            human_review_points.append("従業員を一意に識別できる補助科目へ修正する")
        if any((employee.net_total < 0 for employee in statement.employees)):
            risk_flags.append("negative_net_routed_to_employee_payable_offset")
    if not validation.balanced:
        risk_flags.append("debit_credit_imbalance")
    if statement.employee_detail_present and any(
        (message.startswith("employee ") for message in validation.errors)
    ):
        risk_flags.append("employee_aggregate_mismatch")
    if validation.has_unclassified_money_items:
        risk_flags.append("unclassified_money_item")
        human_review_points.append("固定ルール未分類の金額項目を人間が確認する")
    if fixed_rules.CREDIT_UNKNOWN_DEDUCTION_SUBACCOUNT in deductions:
        risk_flags.append("unmapped_deduction_sub_account")
        human_review_points.append("未分類控除を固定済み預り金補助科目へ解決する")
    if validation.missing_deduction_categories:
        risk_flags.append("missing_required_deduction")
    if not validation.company_social_insurance_present:
        risk_flags.append("company_social_insurance_absent")
    registration_allowed = (
        statement.employee_detail_present
        and (not statement.legacy_aggregate)
        and validation.ok
        and (not validation.has_unclassified_money_items)
        and validation.required_deduction_categories_present
        and validation.company_social_insurance_present
        and (fixed_rules.CREDIT_UNKNOWN_DEDUCTION_SUBACCOUNT not in deductions)
        and (not generic_sub_accounts)
    )
    requires_human_review = not registration_allowed
    if registration_allowed:
        confidence = 1.0
    elif not validation.ok:
        confidence = 0.3
    elif validation.warnings:
        confidence = 0.45
    else:
        confidence = 0.6
    return PayrollJournalCandidate(
        period=statement.period,
        source_channel=statement.source_channel,
        lines=tuple(lines),
        validation=validation,
        confidence=confidence,
        requires_human_review=requires_human_review,
        registration_allowed=registration_allowed,
        risk_flags=tuple(risk_flags),
        unresolved_points=tuple(unresolved_points),
        human_review_points=tuple(human_review_points),
        employee_allocation_signature=tuple(
            sorted(
                (
                    (
                        _private_key_hash("employee", employee.employee_ref),
                        _private_key_hash("subaccount", employee.payable_sub_account),
                        employee.allocation_evidence_hash,
                        employee.payment_total,
                        employee.deduction_total,
                        employee.net_total,
                        employee.unclassified_total,
                    )
                    for employee in statement.employees
                )
            )
        ),
    )
