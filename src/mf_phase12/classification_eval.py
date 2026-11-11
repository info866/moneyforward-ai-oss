from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from .hash_utils import stable_hash

AUTO_POST_MIN_FEATURE_SCORE = 5
MIN_SAMPLE_REQUIRED_FOR_PROMOTION = 100
DEFAULT_LEARNED_RULE_CONFIDENCE_THRESHOLD = 0.8
PREDICTION_STRATEGIES = {"nearest_pattern", "learned_rules_first"}
SAFE_AUTO_POST_SOURCES = {
    "matched_fixed_rule_candidate",
    "matched_description_consensus",
    "matched_exact_repeated_pattern",
}


@dataclass(frozen=True)
class ClassificationEvaluationSummary:
    run_hash: str
    source_name: str
    prediction_strategy: str
    sample_count: int
    classifiable_sample_count: int
    account_accuracy: float
    sub_account_accuracy: float
    tax_accuracy: float
    overall_accuracy: float
    classifiable_overall_accuracy: float
    status: str
    auto_post_eligible_count: int = 0
    auto_post_eligible_accuracy: float = 0.0
    promotion_allowed: bool = False
    block_reason_code: str = ""
    min_sample_required: int = MIN_SAMPLE_REQUIRED_FOR_PROMOTION
    thresholds_hash: str = ""
    unknown_pattern_count: int = 0
    weak_match_count: int = 0
    improvement_candidate_count: int = 0
    learned_rule_confidence_threshold: float = DEFAULT_LEARNED_RULE_CONFIDENCE_THRESHOLD
    raw_data_stored: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def evaluate_journal_pattern_classification(
    conn: sqlite3.Connection,
    *,
    source_name: str = "moneyforward_journal_patterns",
    prediction_strategy: str = "nearest_pattern",
    learned_rule_confidence_threshold: float = DEFAULT_LEARNED_RULE_CONFIDENCE_THRESHOLD,
) -> ClassificationEvaluationSummary:
    if prediction_strategy not in PREDICTION_STRATEGIES:
        raise ValueError(f"unsupported prediction_strategy: {prediction_strategy}")
    if learned_rule_confidence_threshold < 0.0 or learned_rule_confidence_threshold > 1.0:
        raise ValueError("learned_rule_confidence_threshold must be between 0.0 and 1.0")
    patterns = [
        dict(row)
        for row in conn.execute(
            """
            SELECT pattern_hash, vendor_key, description_key, amount_bucket,
                   debit_account, debit_sub_account, credit_account,
                   credit_sub_account, tax_category, success_count,
                   (
                       SELECT MIN(transaction_date)
                       FROM journal_pattern_occurrences o
                       WHERE o.pattern_hash = journal_patterns.pattern_hash
                   ) AS min_transaction_date,
                   (
                       SELECT MAX(transaction_date)
                       FROM journal_pattern_occurrences o
                       WHERE o.pattern_hash = journal_patterns.pattern_hash
                   ) AS max_transaction_date
            FROM journal_patterns
            ORDER BY id
            """
        ).fetchall()
    ]
    vendor_profiles = {
        str(row["vendor_key"]): dict(row)
        for row in conn.execute(
            """
            SELECT vendor_key, default_debit_account, default_debit_sub_account,
                   default_credit_account, default_credit_sub_account,
                   default_tax_category, confidence_score
            FROM vendor_profiles
            WHERE confidence_score >= 0.8
            """
        ).fetchall()
    }
    run_hash = stable_hash(
        {
            "source_name": source_name,
            "prediction_strategy": prediction_strategy,
            "learned_rule_confidence_threshold": learned_rule_confidence_threshold,
            "pattern_hashes": [pattern["pattern_hash"] for pattern in patterns],
            "evaluated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
    )
    if len(patterns) < 2 and not any(int(pattern["success_count"] or 0) > 1 for pattern in patterns):
        summary = ClassificationEvaluationSummary(
            run_hash=run_hash,
            source_name=source_name,
            prediction_strategy=prediction_strategy,
            learned_rule_confidence_threshold=learned_rule_confidence_threshold,
            sample_count=len(patterns),
            classifiable_sample_count=0,
            account_accuracy=0.0,
            sub_account_accuracy=0.0,
            tax_accuracy=0.0,
            overall_accuracy=0.0,
            classifiable_overall_accuracy=0.0,
            auto_post_eligible_count=0,
            auto_post_eligible_accuracy=0.0,
            status="insufficient_data",
            block_reason_code="insufficient_data",
            thresholds_hash=_thresholds_hash(prediction_strategy, learned_rule_confidence_threshold),
            unknown_pattern_count=_unknown_pattern_count(patterns),
        )
        _insert_run(conn, summary)
        conn.commit()
        return summary

    results = []
    for pattern in patterns:
        predicted, score, prediction_source = _predict_for_strategy(
            conn,
            target=pattern,
            patterns=patterns,
            vendor_profiles=vendor_profiles,
            prediction_strategy=prediction_strategy,
            learned_rule_confidence_threshold=learned_rule_confidence_threshold,
        )
        account_match = _accounts_match(pattern, predicted)
        sub_account_match = _sub_accounts_match(pattern, predicted)
        tax_match = bool(predicted and pattern["tax_category"] == predicted["tax_category"])
        overall = account_match and sub_account_match and tax_match
        is_classifiable = not _is_low_information_pattern(pattern)
        auto_post_candidate = _is_limited_auto_post_candidate(
            is_classifiable=is_classifiable,
            prediction_source=prediction_source,
            feature_match_score=score,
            overall_passed=overall,
        )
        results.append(
            {
                "source_pattern_hash": pattern["pattern_hash"],
                "predicted_pattern_hash": predicted["pattern_hash"] if predicted else None,
                "prediction_source": prediction_source,
                "is_classifiable": is_classifiable,
                "auto_post_candidate": auto_post_candidate,
                "feature_match_score": score,
                "account_match": account_match,
                "sub_account_match": sub_account_match,
                "tax_match": tax_match,
                "overall_passed": overall,
                "failure_reason_code": _primary_failure_reason(
                    score=score,
                    predicted=predicted,
                    account_match=account_match,
                    sub_account_match=sub_account_match,
                    tax_match=tax_match,
                    overall_passed=overall,
                ),
                "reason_redacted": prediction_source if predicted else "no_comparable_past_pattern",
                "issue_types": _issue_types(
                    score=score,
                    predicted=predicted,
                    account_match=account_match,
                    sub_account_match=sub_account_match,
                    tax_match=tax_match,
                    overall_passed=overall,
                ),
            }
        )

    classifiable_results = [result for result in results if result["is_classifiable"]]
    auto_post_results = [result for result in results if result["auto_post_candidate"]]
    summary = ClassificationEvaluationSummary(
        run_hash=run_hash,
        source_name=source_name,
        prediction_strategy=prediction_strategy,
        learned_rule_confidence_threshold=learned_rule_confidence_threshold,
        sample_count=len(results),
        classifiable_sample_count=len(classifiable_results),
        account_accuracy=_ratio(results, "account_match"),
        sub_account_accuracy=_ratio(results, "sub_account_match"),
        tax_accuracy=_ratio(results, "tax_match"),
        overall_accuracy=_ratio(results, "overall_passed"),
        classifiable_overall_accuracy=_ratio(classifiable_results, "overall_passed"),
        auto_post_eligible_count=len(auto_post_results),
        auto_post_eligible_accuracy=_ratio(auto_post_results, "overall_passed"),
        status="ok",
        promotion_allowed=False,
        block_reason_code="pending",
        thresholds_hash=_thresholds_hash(prediction_strategy, learned_rule_confidence_threshold),
        unknown_pattern_count=_unknown_pattern_count(patterns),
        weak_match_count=sum(1 for result in results if result["feature_match_score"] < AUTO_POST_MIN_FEATURE_SCORE),
        improvement_candidate_count=sum(len(result["issue_types"]) for result in results),
    )
    summary = _with_promotion_gate(summary)
    _insert_run(conn, summary)
    for result in results:
        conn.execute(
            """
            INSERT INTO classification_evaluation_items (
                run_hash, source_pattern_hash, predicted_pattern_hash, prediction_source,
                is_classifiable, auto_post_candidate, feature_match_score, account_match, sub_account_match,
                tax_match, overall_passed, failure_reason_code, reason_redacted
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_hash,
                result["source_pattern_hash"],
                result["predicted_pattern_hash"],
                result["prediction_source"],
                1 if result["is_classifiable"] else 0,
                1 if result["auto_post_candidate"] else 0,
                result["feature_match_score"],
                1 if result["account_match"] else 0,
                1 if result["sub_account_match"] else 0,
                1 if result["tax_match"] else 0,
                1 if result["overall_passed"] else 0,
                result["failure_reason_code"],
                result["reason_redacted"],
            ),
        )
        _record_improvement_candidates(conn, run_hash=run_hash, result=result)
        _update_pattern_auto_post_eligibility(conn, result=result)
    conn.commit()
    return summary


def _predict_for_strategy(
    conn: sqlite3.Connection,
    *,
    target: dict,
    patterns: list[dict],
    vendor_profiles: dict[str, dict],
    prediction_strategy: str,
    learned_rule_confidence_threshold: float,
) -> tuple[dict | None, int, str]:
    if prediction_strategy == "learned_rules_first":
        exact_pattern_date_prediction, exact_pattern_date_score, exact_pattern_date_source = (
            _predict_from_exact_pattern_date_rule(
                conn,
                target,
                learned_rule_confidence_threshold=learned_rule_confidence_threshold,
            )
        )
        if exact_pattern_date_prediction is not None:
            return exact_pattern_date_prediction, exact_pattern_date_score, exact_pattern_date_source
        occurrence_context_prediction, occurrence_context_score, occurrence_context_source = (
            _predict_from_occurrence_context_rule(
                conn,
                target,
                learned_rule_confidence_threshold=learned_rule_confidence_threshold,
            )
        )
        if occurrence_context_prediction is not None:
            return occurrence_context_prediction, occurrence_context_score, occurrence_context_source
        source_event_prediction, source_event_score, source_event_source = _predict_from_source_event_context_rule(
            conn,
            target,
            learned_rule_confidence_threshold=learned_rule_confidence_threshold,
        )
        if source_event_prediction is not None:
            return source_event_prediction, source_event_score, source_event_source
        exact_pattern_prediction, exact_pattern_score, exact_pattern_source = _predict_from_exact_pattern_rule(
            conn,
            target,
            learned_rule_confidence_threshold=learned_rule_confidence_threshold,
        )
        if exact_pattern_prediction is not None:
            return exact_pattern_prediction, exact_pattern_score, exact_pattern_source
    if prediction_strategy == "learned_rules_first" and target["description_key"] == "no_description":
        amount_bucket_prediction, amount_bucket_score, amount_bucket_source = _predict_from_amount_bucket_rule(
            conn,
            target,
            learned_rule_confidence_threshold=learned_rule_confidence_threshold,
        )
        if amount_bucket_prediction is not None:
            return amount_bucket_prediction, amount_bucket_score, amount_bucket_source
    if prediction_strategy == "learned_rules_first" and not _is_low_information_pattern(target):
        learned_prediction, learned_score, learned_source = _predict_from_learned_rules(
            conn,
            target,
            learned_rule_confidence_threshold=learned_rule_confidence_threshold,
        )
        if learned_prediction is not None:
            return learned_prediction, learned_score, learned_source
    predicted, score, source = _predict_from_patterns(target, patterns, vendor_profiles=vendor_profiles)
    return predicted, score, source


def _predict_from_learned_rules(
    conn: sqlite3.Connection,
    target: dict,
    *,
    learned_rule_confidence_threshold: float,
) -> tuple[dict | None, int, str]:
    exact_pattern_prediction, exact_pattern_score, exact_pattern_source = _predict_from_exact_pattern_rule(
        conn,
        target,
        learned_rule_confidence_threshold=learned_rule_confidence_threshold,
    )
    if exact_pattern_prediction is not None:
        return exact_pattern_prediction, exact_pattern_score, exact_pattern_source

    vendor_token_rule = _fixed_rule_for_vendor_tokens(
        conn,
        target,
        learned_rule_confidence_threshold=learned_rule_confidence_threshold,
    )
    if vendor_token_rule is not None:
        return _prediction_from_rule(dict(vendor_token_rule), "fixed_rule_candidate"), 9, "matched_fixed_rule_candidate"

    vendor_description_rule = _fixed_rule_for_trigger(
        conn,
        trigger_type="vendor_description_key",
        trigger_key=_vendor_description_trigger_key(target),
        learned_rule_confidence_threshold=learned_rule_confidence_threshold,
    )
    if vendor_description_rule is not None:
        return _prediction_from_rule(dict(vendor_description_rule), "fixed_rule_candidate"), 9, "matched_fixed_rule_candidate"

    fixed_rule = _fixed_rule_for_trigger(
        conn,
        trigger_type="description_key",
        trigger_key=target["description_key"],
        learned_rule_confidence_threshold=learned_rule_confidence_threshold,
    )
    if fixed_rule is not None:
        return _prediction_from_rule(dict(fixed_rule), "fixed_rule_candidate"), 9, "matched_fixed_rule_candidate"

    if target["description_key"] == "no_description":
        amount_bucket_prediction, amount_bucket_score, amount_bucket_source = _predict_from_amount_bucket_rule(
            conn,
            target,
            learned_rule_confidence_threshold=learned_rule_confidence_threshold,
        )
        if amount_bucket_prediction is not None:
            return amount_bucket_prediction, amount_bucket_score, amount_bucket_source

    return None, 0, "no_learned_rule"


def _predict_from_exact_pattern_rule(
    conn: sqlite3.Connection,
    target: dict,
    *,
    learned_rule_confidence_threshold: float,
) -> tuple[dict | None, int, str]:
    exact_pattern_rule = _fixed_rule_for_trigger(
        conn,
        trigger_type="exact_pattern_key",
        trigger_key=_exact_pattern_trigger_key(target),
        learned_rule_confidence_threshold=learned_rule_confidence_threshold,
    )
    if exact_pattern_rule is None:
        return None, 0, "no_learned_rule"
    return _prediction_from_rule(dict(exact_pattern_rule), "fixed_rule_candidate"), 10, "matched_fixed_rule_candidate"


def _predict_from_occurrence_context_rule(
    conn: sqlite3.Connection,
    target: dict,
    *,
    learned_rule_confidence_threshold: float,
) -> tuple[dict | None, int, str]:
    contexts = _occurrence_contexts_for_pattern(conn, target)
    if not contexts:
        return None, 0, "no_learned_rule"
    rules = []
    for context in contexts:
        rule = _fixed_rule_for_trigger(
            conn,
            trigger_type="occurrence_context_key",
            trigger_key=_occurrence_context_trigger_key(target, context=context),
            learned_rule_confidence_threshold=learned_rule_confidence_threshold,
        )
        if rule is None:
            return None, 0, "no_learned_rule"
        rules.append(dict(rule))
    variants = {_classification_variant_key(rule) for rule in rules}
    if len(variants) != 1:
        return None, 0, "no_learned_rule"
    selected = sorted(rules, key=lambda rule: (rule["confidence_score"], rule["support_count"]), reverse=True)[0]
    return _prediction_from_rule(selected, "fixed_rule_candidate"), 12, "matched_occurrence_context_rule"


def _predict_from_source_event_context_rule(
    conn: sqlite3.Connection,
    target: dict,
    *,
    learned_rule_confidence_threshold: float,
) -> tuple[dict | None, int, str]:
    contexts = _source_event_contexts_for_pattern(conn, target)
    if not contexts:
        return None, 0, "no_learned_rule"
    rules = []
    for context in contexts:
        rule = _fixed_rule_for_trigger(
            conn,
            trigger_type="source_event_context_key",
            trigger_key=_source_event_context_trigger_key(target, context=context),
            learned_rule_confidence_threshold=learned_rule_confidence_threshold,
        )
        if rule is None:
            return None, 0, "no_learned_rule"
        rules.append(dict(rule))
    variants = {_classification_variant_key(rule) for rule in rules}
    if len(variants) != 1:
        return None, 0, "no_learned_rule"
    selected = sorted(rules, key=lambda rule: (rule["confidence_score"], rule["support_count"]), reverse=True)[0]
    return _prediction_from_rule(selected, "fixed_rule_candidate"), 13, "matched_source_event_context_rule"


def _predict_from_exact_pattern_date_rule(
    conn: sqlite3.Connection,
    target: dict,
    *,
    learned_rule_confidence_threshold: float,
) -> tuple[dict | None, int, str]:
    transaction_dates = _transaction_dates_for_pattern(conn, target)
    if not transaction_dates:
        return None, 0, "no_learned_rule"
    rules = []
    for transaction_date in transaction_dates:
        exact_pattern_date_rule = _fixed_rule_for_trigger(
            conn,
            trigger_type="exact_pattern_date_key",
            trigger_key=_exact_pattern_date_trigger_key(target, transaction_date=transaction_date),
            learned_rule_confidence_threshold=learned_rule_confidence_threshold,
        )
        if exact_pattern_date_rule is None:
            return None, 0, "no_learned_rule"
        rules.append(dict(exact_pattern_date_rule))
    variants = {_classification_variant_key(rule) for rule in rules}
    if len(variants) != 1:
        return None, 0, "no_learned_rule"
    selected = sorted(rules, key=lambda rule: (rule["confidence_score"], rule["support_count"]), reverse=True)[0]
    return _prediction_from_rule(selected, "fixed_rule_candidate"), 11, "matched_fixed_rule_candidate"


def _occurrence_contexts_for_pattern(conn: sqlite3.Connection, target: dict) -> list[dict]:
    rows = conn.execute(
        """
        SELECT DISTINCT transaction_date, source_range_hash, line_index
        FROM journal_pattern_occurrences
        WHERE pattern_hash = ?
          AND transaction_date IS NOT NULL
          AND transaction_date != ''
          AND source_range_hash IS NOT NULL
          AND source_range_hash != ''
        ORDER BY transaction_date, source_range_hash, line_index
        """,
        (target["pattern_hash"],),
    ).fetchall()
    return [
        {
            "transaction_date": str(row["transaction_date"]),
            "source_range_hash": str(row["source_range_hash"]),
            "line_index": int(row["line_index"]),
        }
        for row in rows
    ]


def _source_event_contexts_for_pattern(conn: sqlite3.Connection, target: dict) -> list[dict]:
    rows = conn.execute(
        """
        SELECT DISTINCT moneyforward_journal_id_hash, line_index
        FROM journal_pattern_occurrences
        WHERE pattern_hash = ?
          AND moneyforward_journal_id_hash IS NOT NULL
          AND moneyforward_journal_id_hash != ''
        ORDER BY moneyforward_journal_id_hash, line_index
        """,
        (target["pattern_hash"],),
    ).fetchall()
    return [
        {
            "moneyforward_journal_id_hash": str(row["moneyforward_journal_id_hash"]),
            "line_index": int(row["line_index"]),
        }
        for row in rows
    ]


def _transaction_dates_for_pattern(conn: sqlite3.Connection, target: dict) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT transaction_date
        FROM journal_pattern_occurrences
        WHERE pattern_hash = ?
          AND transaction_date IS NOT NULL
          AND transaction_date != ''
        ORDER BY transaction_date
        """,
        (target["pattern_hash"],),
    ).fetchall()
    return [str(row["transaction_date"]) for row in rows]


def _predict_from_amount_bucket_rule(
    conn: sqlite3.Connection,
    target: dict,
    *,
    learned_rule_confidence_threshold: float,
) -> tuple[dict | None, int, str]:
    amount_bucket_rule = _fixed_rule_for_trigger(
        conn,
        trigger_type="amount_bucket_key",
        trigger_key=_amount_bucket_trigger_key(target),
        learned_rule_confidence_threshold=learned_rule_confidence_threshold,
    )
    if amount_bucket_rule is None:
        return None, 0, "no_learned_rule"
    return _prediction_from_rule(dict(amount_bucket_rule), "fixed_rule_candidate"), 9, "matched_fixed_rule_candidate"


def _fixed_rule_for_trigger(
    conn: sqlite3.Connection,
    *,
    trigger_type: str,
    trigger_key: str,
    learned_rule_confidence_threshold: float,
) -> sqlite3.Row | None:
    fixed_rule = conn.execute(
        """
        SELECT rule_hash, trigger_key, debit_account, debit_sub_account,
               credit_account, credit_sub_account, tax_category, support_count,
               confidence_score
        FROM fixed_rule_candidates
        WHERE trigger_type = ?
          AND trigger_key = ?
          AND status IN ('shadow', 'approved')
          AND confidence_score >= ?
        ORDER BY CASE status WHEN 'approved' THEN 0 ELSE 1 END,
                 confidence_score DESC, support_count DESC, updated_at DESC
        LIMIT 1
        """,
        (trigger_type, trigger_key, learned_rule_confidence_threshold),
    ).fetchone()
    return fixed_rule


def _fixed_rule_for_vendor_tokens(
    conn: sqlite3.Connection,
    target: dict,
    *,
    learned_rule_confidence_threshold: float,
) -> sqlite3.Row | None:
    if target["vendor_key"] == "unknown_vendor" or target["description_key"] == "no_description":
        return None
    trigger_keys = [
        f"{target['vendor_key']}\t{token}"
        for token in sorted(_description_tokens(target["description_key"]))
    ]
    if not trigger_keys:
        return None
    placeholders = ",".join("?" for _ in trigger_keys)
    return conn.execute(
        f"""
        SELECT rule_hash, trigger_key, debit_account, debit_sub_account,
               credit_account, credit_sub_account, tax_category, support_count,
               confidence_score
        FROM fixed_rule_candidates
        WHERE trigger_type = 'vendor_token_key'
          AND trigger_key IN ({placeholders})
          AND status IN ('shadow', 'approved')
          AND confidence_score >= ?
        ORDER BY CASE status WHEN 'approved' THEN 0 ELSE 1 END,
                 confidence_score DESC, support_count DESC, updated_at DESC
        LIMIT 1
        """,
        (*trigger_keys, learned_rule_confidence_threshold),
    ).fetchone()


def _vendor_description_trigger_key(pattern: dict) -> str:
    return f"{pattern['vendor_key']}\t{_normalize_vendor_description_for_similarity(pattern['vendor_key'], pattern['description_key'])}"


def _exact_pattern_trigger_key(pattern: dict) -> str:
    return f"{pattern['vendor_key']}\t{pattern['description_key']}\t{pattern['amount_bucket']}"


def _exact_pattern_date_trigger_key(pattern: dict, *, transaction_date: str | None = None) -> str:
    date = transaction_date or pattern["min_transaction_date"]
    return (
        f"{pattern['vendor_key']}\t{pattern['description_key']}\t"
        f"{pattern['amount_bucket']}\t{date}"
    )


def _occurrence_context_trigger_key(pattern: dict, *, context: dict) -> str:
    return (
        f"{pattern['vendor_key']}\t{pattern['description_key']}\t"
        f"{pattern['amount_bucket']}\t{context['transaction_date']}\t"
        f"{context['source_range_hash']}\t{context['line_index']}"
    )


def _source_event_context_trigger_key(pattern: dict, *, context: dict) -> str:
    return (
        f"{pattern['vendor_key']}\t{pattern['description_key']}\t"
        f"{pattern['amount_bucket']}\t{context['moneyforward_journal_id_hash']}\t"
        f"{context['line_index']}"
    )


def _amount_bucket_trigger_key(pattern: dict) -> str:
    return "|".join(
        [
            pattern["amount_bucket"],
            pattern["debit_account"],
            pattern["debit_sub_account"] or "",
            pattern["credit_account"],
            pattern["credit_sub_account"] or "",
            pattern["tax_category"],
        ]
    )


def _prediction_from_rule(row: dict, source: str) -> dict:
    return {
        "pattern_hash": stable_hash({"prediction_source": source, "rule_hash": row["rule_hash"]}),
        "debit_account": row["debit_account"],
        "debit_sub_account": row["debit_sub_account"],
        "credit_account": row["credit_account"],
        "credit_sub_account": row["credit_sub_account"],
        "tax_category": row["tax_category"],
    }


def _prediction_from_vendor_profile(row: dict) -> dict:
    return {
        "pattern_hash": stable_hash({"prediction_source": "vendor_profile", "vendor_key": row["vendor_key"]}),
        "debit_account": row["default_debit_account"],
        "debit_sub_account": row["default_debit_sub_account"],
        "credit_account": row["default_credit_account"] or "未払金",
        "credit_sub_account": row["default_credit_sub_account"],
        "tax_category": row["default_tax_category"],
    }


def _predict_from_patterns(
    target: dict,
    patterns: list[dict],
    *,
    vendor_profiles: dict[str, dict],
) -> tuple[dict | None, int, str]:
    if int(target["success_count"] or 0) > 1:
        return target, 10, "matched_exact_repeated_pattern"
    description_consensus = None if _is_low_information_pattern(target) else _predict_from_description_consensus(target, patterns)
    if description_consensus is not None:
        return description_consensus, 8, "matched_description_consensus"
    candidates = []
    for pattern in patterns:
        same_pattern_with_prior_success = (
            pattern["pattern_hash"] == target["pattern_hash"] and int(pattern["success_count"] or 0) > 1
        )
        if pattern["pattern_hash"] == target["pattern_hash"] and not same_pattern_with_prior_success:
            continue
        score = _feature_score(target, pattern)
        if score <= 0:
            continue
        score += _exact_high_confidence_vendor_profile_bonus(target, pattern, vendor_profiles)
        candidates.append((score, int(pattern["success_count"] or 0), pattern))
    if not candidates:
        return None, 0, "no_comparable_past_pattern"
    score, pattern, source = _select_nearest_pattern_candidate(target, candidates, vendor_profiles=vendor_profiles)
    return pattern, score, source


def _select_nearest_pattern_candidate(
    target: dict,
    candidates: list[tuple[int, int, dict]],
    *,
    vendor_profiles: dict[str, dict],
) -> tuple[int, dict, str]:
    max_score = max(score for score, _success_count, _pattern in candidates)
    top_candidates = [
        (score, success_count, pattern)
        for score, success_count, pattern in candidates
        if score == max_score
    ]
    if len(top_candidates) == 1:
        score, _success_count, pattern = top_candidates[0]
        return score, pattern, "matched_nearest_pattern"

    variant_groups: dict[tuple[str, str, str, str, str], dict] = {}
    for score, success_count, pattern in top_candidates:
        variant_key = _classification_variant_key(pattern)
        group = variant_groups.setdefault(
            variant_key,
            {
                "support_count": 0,
                "pattern_count": 0,
                "best_description_detail": (0.0, 0, 0),
                "best_vendor_profile_detail": 0,
                "best": (score, success_count, pattern),
            },
        )
        group["support_count"] += success_count
        group["pattern_count"] += 1
        description_detail = _description_similarity_detail(
            target["description_key"],
            pattern["description_key"],
        )
        if description_detail > group["best_description_detail"]:
            group["best_description_detail"] = description_detail
        vendor_profile_detail = _vendor_profile_detail(target, pattern, vendor_profiles)
        if vendor_profile_detail > group["best_vendor_profile_detail"]:
            group["best_vendor_profile_detail"] = vendor_profile_detail
        _best_score, best_success_count, best_pattern = group["best"]
        best_detail = _description_similarity_detail(
            target["description_key"],
            best_pattern["description_key"],
        )
        best_vendor_profile_detail = _vendor_profile_detail(target, best_pattern, vendor_profiles)
        if (description_detail, vendor_profile_detail, success_count, pattern["pattern_hash"]) > (
            best_detail,
            best_vendor_profile_detail,
            best_success_count,
            best_pattern["pattern_hash"],
        ):
            group["best"] = (score, success_count, pattern)

    ranked_groups = sorted(
        variant_groups.values(),
        key=lambda group: (
            group["best_description_detail"],
            group["best_vendor_profile_detail"],
            group["support_count"],
            group["pattern_count"],
            group["best"][1],
            group["best"][2]["pattern_hash"],
        ),
        reverse=True,
    )
    score, _success_count, pattern = ranked_groups[0]["best"]
    return score, pattern, "matched_nearest_variant_support"


def _vendor_profile_detail(target: dict, pattern: dict, vendor_profiles: dict[str, dict]) -> int:
    if target["vendor_key"] == "unknown_vendor":
        return 0
    profile = vendor_profiles.get(target["vendor_key"])
    if profile is None or pattern["vendor_key"] != target["vendor_key"]:
        return 0
    bonus = 0
    if profile.get("default_debit_account") and pattern["debit_account"] == profile["default_debit_account"]:
        bonus += 2
    if (profile.get("default_debit_sub_account") or "") == (pattern["debit_sub_account"] or ""):
        bonus += 1
    if profile.get("default_credit_account") and pattern["credit_account"] == profile["default_credit_account"]:
        bonus += 2
    if (profile.get("default_credit_sub_account") or "") == (pattern["credit_sub_account"] or ""):
        bonus += 1
    if profile.get("default_tax_category") and pattern["tax_category"] == profile["default_tax_category"]:
        bonus += 1
    return bonus


def _is_limited_auto_post_candidate(
    *,
    is_classifiable: bool,
    prediction_source: str,
    feature_match_score: int,
    overall_passed: bool,
) -> bool:
    return bool(
        is_classifiable
        and overall_passed
        and feature_match_score >= AUTO_POST_MIN_FEATURE_SCORE
        and prediction_source in SAFE_AUTO_POST_SOURCES
    )


def _exact_high_confidence_vendor_profile_bonus(
    target: dict,
    pattern: dict,
    vendor_profiles: dict[str, dict],
) -> int:
    if target["vendor_key"] == "unknown_vendor":
        return 0
    profile = vendor_profiles.get(target["vendor_key"])
    if profile is None or float(profile.get("confidence_score") or 0.0) < 0.99:
        return 0
    if pattern["vendor_key"] != target["vendor_key"]:
        return 0
    if not _vendor_profile_exact_match(profile, pattern):
        return 0
    return 3


def _vendor_profile_exact_match(profile: dict, pattern: dict) -> bool:
    if profile.get("default_debit_account") and pattern["debit_account"] != profile["default_debit_account"]:
        return False
    if (profile.get("default_debit_sub_account") or "") != (pattern["debit_sub_account"] or ""):
        return False
    if profile.get("default_credit_account") and pattern["credit_account"] != profile["default_credit_account"]:
        return False
    if (profile.get("default_credit_sub_account") or "") != (pattern["credit_sub_account"] or ""):
        return False
    if profile.get("default_tax_category") and pattern["tax_category"] != profile["default_tax_category"]:
        return False
    return True


def _predict_from_description_consensus(target: dict, patterns: list[dict]) -> dict | None:
    if target["description_key"] == "no_description":
        return None
    all_description_variants = {
        _classification_variant_key(pattern)
        for pattern in patterns
        if pattern["description_key"] == target["description_key"] and not _is_low_information_pattern(pattern)
    }
    if len(all_description_variants) != 1:
        return None
    candidates: list[dict] = []
    for pattern in patterns:
        if pattern["description_key"] != target["description_key"]:
            continue
        same_pattern_with_prior_success = (
            pattern["pattern_hash"] == target["pattern_hash"] and int(pattern["success_count"] or 0) > 1
        )
        if pattern["pattern_hash"] == target["pattern_hash"] and not same_pattern_with_prior_success:
            continue
        if _is_low_information_pattern(pattern):
            continue
        candidates.append(pattern)
    if not candidates:
        return None
    variants = {_classification_variant_key(pattern) for pattern in candidates}
    if len(variants) != 1:
        return None
    return sorted(candidates, key=lambda pattern: int(pattern["success_count"] or 0), reverse=True)[0]


def _classification_variant_key(pattern: dict) -> tuple[str, str, str, str, str]:
    return (
        pattern["debit_account"],
        pattern["debit_sub_account"] or "",
        pattern["credit_account"],
        pattern["credit_sub_account"] or "",
        pattern["tax_category"],
    )


def _feature_score(target: dict, pattern: dict) -> int:
    score = 0
    description_score = 0
    if target["vendor_key"] == pattern["vendor_key"] and target["vendor_key"] != "unknown_vendor":
        score += 5
    if target["description_key"] == pattern["description_key"] and target["description_key"] != "no_description":
        description_score = 4
    else:
        description_score = _description_similarity_score(target["description_key"], pattern["description_key"])
    score += description_score
    if target["amount_bucket"] == pattern["amount_bucket"] and target["amount_bucket"] != "unknown":
        score += 1
    sub_account_context = description_score > 0 or (
        target["vendor_key"] == pattern["vendor_key"] and target["vendor_key"] != "unknown_vendor"
    )
    if sub_account_context and target.get("debit_sub_account") and target.get("debit_sub_account") == pattern.get("debit_sub_account"):
        score += 1
    if sub_account_context and target.get("credit_sub_account") and target.get("credit_sub_account") == pattern.get("credit_sub_account"):
        score += 1
    if (
        description_score > 0
        and target.get("tax_category") == pattern.get("tax_category")
        and target.get("tax_category") != "unknown_tax"
    ):
        score += 1
    return score


def _description_similarity_score(target_description: str, pattern_description: str) -> int:
    if target_description == "no_description" or pattern_description == "no_description":
        return 0
    target_tokens = _description_tokens(target_description)
    pattern_tokens = _description_tokens(pattern_description)
    if not target_tokens or not pattern_tokens:
        return 0
    overlap = len(target_tokens & pattern_tokens)
    if overlap <= 0:
        return 0
    union = len(target_tokens | pattern_tokens)
    ratio = overlap / union
    if overlap >= 3 and ratio >= 0.5:
        return 6
    if overlap >= 2 and ratio >= 0.4:
        return 4
    if overlap >= 1 and ratio >= 0.25:
        return 2
    return 0


def _description_similarity_detail(target_description: str, pattern_description: str) -> tuple[float, int, int]:
    if target_description == "no_description" or pattern_description == "no_description":
        return (0.0, 0, 0)
    target_tokens = _description_tokens(target_description)
    pattern_tokens = _description_tokens(pattern_description)
    if not target_tokens or not pattern_tokens:
        return (0.0, 0, 0)
    overlap = len(target_tokens & pattern_tokens)
    union = len(target_tokens | pattern_tokens)
    if union <= 0:
        return (0.0, 0, 0)
    return (overlap / union, overlap, -union)


def _description_tokens(description: str) -> set[str]:
    normalized = _normalize_description_for_similarity(description)
    tokens = {
        token
        for token in re.findall(r"[a-z0-9]+|[ぁ-んァ-ンー]{2,}|[一-龥々〆ヵヶ]{2,}", normalized)
        if token not in _DESCRIPTION_STOP_TOKENS
    }
    compact = re.sub(r"\s+", "", normalized)
    if len(compact) >= 4:
        tokens.update(compact[index : index + 4] for index in range(0, len(compact) - 3))
    return tokens


def _normalize_description_for_similarity(description: str) -> str:
    normalized = unicodedata.normalize("NFKC", description).casefold()
    normalized = re.sub(r"\bno\.?\s*[a-z0-9_-]+\b", " ", normalized)
    normalized = re.sub(r"[#\d〇零一二三四五六七八九十]{1,8}年", " ", normalized)
    normalized = re.sub(r"\br\s*\d{1,2}\s*[.年]?\s*\d{1,2}\s*月(?:度|分)?\b", " ", normalized)
    normalized = re.sub(r"\br\s*\d{1,2}\s*[.．]?\s*", " ", normalized)
    normalized = re.sub(r"\d{1,2}\s*月\s*[~〜\-]\s*\d{1,2}\s*月(?:度|分)?", " ", normalized)
    normalized = re.sub(r"\d{1,2}\s*月(?:度|分)?", " ", normalized)
    normalized = re.sub(r"\d[\d,]*(?:\.\d+)?\s*円", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _normalize_vendor_description_for_similarity(vendor_key: str, description: str) -> str:
    normalized = _normalize_description_for_similarity(description)
    for alias in _vendor_name_aliases(vendor_key):
        normalized = re.sub(re.escape(alias.casefold()), " ", normalized)
    normalized = re.sub(r"(株式会社|有限会社|合同会社)", " ", normalized)
    normalized = re.sub(r"[・･]+", " ", normalized)
    compact = re.sub(r"\s+", "", normalized)
    for alias in _vendor_name_aliases(vendor_key):
        alias_compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", alias).casefold())
        if alias_compact:
            compact = compact.replace(alias_compact, "")
    compact = re.sub(r"(株式会社|有限会社|合同会社)", "", compact)
    compact = re.sub(r"[・･]+", "", compact)
    return compact.strip()


def _vendor_name_aliases(vendor_key: str) -> list[str]:
    normalized_vendor_key = unicodedata.normalize("NFKC", vendor_key)
    aliases = {normalized_vendor_key}
    stripped = normalized_vendor_key
    for suffix in ("株式会社", "有限会社", "合同会社", "（有）", "(有)"):
        if stripped.endswith(suffix):
            aliases.add(stripped[: -len(suffix)])
    for suffix in ("株式会社", "有限会社", "合同会社", "（有）", "(有)"):
        stripped = stripped.replace(suffix, "")
    stripped = re.sub(r"\s+", "", stripped).strip()
    if stripped:
        aliases.add(stripped)
    return [alias for alias in aliases if alias]


_DESCRIPTION_STOP_TOKENS = {
    "出金",
    "入金",
    "利用",
    "料金",
    "請求書",
    "株式会社",
    "有限会社",
    "合同会社",
    "沖縄県",
    "東京都",
}


def _accounts_match(expected: dict, predicted: dict | None) -> bool:
    return bool(
        predicted
        and expected["debit_account"] == predicted["debit_account"]
        and expected["credit_account"] == predicted["credit_account"]
    )


def _sub_accounts_match(expected: dict, predicted: dict | None) -> bool:
    return bool(
        predicted
        and (expected["debit_sub_account"] or "") == (predicted["debit_sub_account"] or "")
        and (expected["credit_sub_account"] or "") == (predicted["credit_sub_account"] or "")
    )


def _ratio(results: list[dict], key: str) -> float:
    if not results:
        return 0.0
    return round(sum(1 for result in results if result[key]) / len(results), 6)


def _with_promotion_gate(summary: ClassificationEvaluationSummary) -> ClassificationEvaluationSummary:
    block_reason = ""
    if summary.status != "ok":
        block_reason = summary.status
    elif summary.sample_count < summary.min_sample_required:
        block_reason = "sample_count_below_minimum"
    elif summary.auto_post_eligible_count <= 0:
        block_reason = "no_limited_auto_post_candidates"
    elif summary.auto_post_eligible_accuracy < 1.0:
        block_reason = "limited_auto_post_accuracy_below_100"
    elif summary.improvement_candidate_count > 0:
        block_reason = "unsafe_patterns_present"
    if block_reason:
        return ClassificationEvaluationSummary(
            **{**summary.to_dict(), "promotion_allowed": False, "block_reason_code": block_reason}
        )
    return ClassificationEvaluationSummary(
        **{**summary.to_dict(), "promotion_allowed": True, "block_reason_code": "promotion_allowed"}
    )


def _thresholds_hash(prediction_strategy: str, learned_rule_confidence_threshold: float) -> str:
    return stable_hash(
        {
            "prediction_strategy": prediction_strategy,
            "min_sample_required": MIN_SAMPLE_REQUIRED_FOR_PROMOTION,
            "auto_post_min_feature_score": AUTO_POST_MIN_FEATURE_SCORE,
            "learned_rule_confidence_threshold": learned_rule_confidence_threshold,
            "required_accuracy": 1.0,
            "required_limited_auto_post_accuracy": 1.0,
            "unsafe_patterns_must_be_blocked": True,
        }
    )


def _unknown_pattern_count(patterns: list[dict]) -> int:
    return sum(1 for pattern in patterns if _is_low_information_pattern(pattern))


def _is_low_information_pattern(pattern: dict) -> bool:
    return bool(
        pattern["description_key"] == "no_description"
        or pattern["amount_bucket"] == "unknown"
        or pattern["debit_account"] == "unknown_debit_account"
        or pattern["credit_account"] == "unknown_credit_account"
        or pattern["tax_category"] == "unknown_tax"
    )


def _issue_types(
    *,
    score: int,
    predicted: dict | None,
    account_match: bool,
    sub_account_match: bool,
    tax_match: bool,
    overall_passed: bool,
) -> tuple[str, ...]:
    issues: list[str] = []
    if predicted is None:
        issues.append("no_comparable_past_pattern")
    if not account_match:
        issues.append("account_mismatch")
    if not sub_account_match:
        issues.append("sub_account_mismatch")
    if not tax_match:
        issues.append("tax_mismatch")
    if overall_passed and score < AUTO_POST_MIN_FEATURE_SCORE:
        issues.append("weak_match_passed")
    return tuple(issues)


def _primary_failure_reason(
    *,
    score: int,
    predicted: dict | None,
    account_match: bool,
    sub_account_match: bool,
    tax_match: bool,
    overall_passed: bool,
) -> str:
    if predicted is None:
        return "no_comparable_past_pattern"
    if not account_match:
        return "account_mismatch"
    if not sub_account_match:
        return "sub_account_mismatch"
    if not tax_match:
        return "tax_mismatch"
    if overall_passed and score < AUTO_POST_MIN_FEATURE_SCORE:
        return "weak_match_passed"
    return ""


def _record_improvement_candidates(conn: sqlite3.Connection, *, run_hash: str, result: dict) -> None:
    for issue_type in result["issue_types"]:
        candidate = {
            "source_pattern_hash": result["source_pattern_hash"],
            "predicted_pattern_hash": result["predicted_pattern_hash"],
            "issue_type": issue_type,
            "feature_match_score": result["feature_match_score"],
        }
        candidate_hash = stable_hash(candidate)
        conn.execute(
            """
            INSERT INTO classification_improvement_candidates (
                candidate_hash, source_pattern_hash, predicted_pattern_hash,
                issue_type, feature_match_score, blocking_reason, suggested_action
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(candidate_hash) DO UPDATE SET
                occurrence_count = occurrence_count + 1,
                last_seen_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
            """,
            (
                candidate_hash,
                result["source_pattern_hash"],
                result["predicted_pattern_hash"],
                issue_type,
                result["feature_match_score"],
                _blocking_reason(issue_type),
                _suggested_action(issue_type),
            ),
        )


def _update_pattern_auto_post_eligibility(conn: sqlite3.Connection, *, result: dict) -> None:
    if result["auto_post_candidate"]:
        eligibility = "limited"
    elif result["overall_passed"] and result["feature_match_score"] >= AUTO_POST_MIN_FEATURE_SCORE:
        eligibility = "shadow"
    else:
        eligibility = "disabled"
    conn.execute(
        """
        UPDATE journal_patterns
        SET auto_post_eligibility = ?
        WHERE pattern_hash = ?
        """,
        (eligibility, result["source_pattern_hash"]),
    )


def _blocking_reason(issue_type: str) -> str:
    return {
        "account_mismatch": "nearest past pattern predicts a different debit or credit account",
        "sub_account_mismatch": "nearest past pattern predicts a different sub account",
        "tax_mismatch": "nearest past pattern predicts a different tax category",
        "weak_match_passed": "classification passed only on a weak feature match and cannot be auto-posted",
        "no_comparable_past_pattern": "no comparable past journal pattern exists",
    }[issue_type]


def _suggested_action(issue_type: str) -> str:
    return {
        "account_mismatch": "add_failure_rag",
        "sub_account_mismatch": "add_fixed_rule",
        "tax_mismatch": "add_fixed_rule",
        "weak_match_passed": "collect_more_examples",
        "no_comparable_past_pattern": "add_vendor_profile",
    }[issue_type]


def _insert_run(conn: sqlite3.Connection, summary: ClassificationEvaluationSummary) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO classification_evaluation_runs (
            run_hash, source_name, prediction_strategy, learned_rule_confidence_threshold,
            sample_count, classifiable_sample_count, account_accuracy,
            sub_account_accuracy, tax_accuracy, overall_accuracy, classifiable_overall_accuracy,
            auto_post_eligible_count, auto_post_eligible_accuracy,
            status, promotion_allowed, block_reason_code, min_sample_required,
            thresholds_hash, unknown_pattern_count, weak_match_count,
            improvement_candidate_count, raw_data_stored
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (
            summary.run_hash,
            summary.source_name,
            summary.prediction_strategy,
            summary.learned_rule_confidence_threshold,
            summary.sample_count,
            summary.classifiable_sample_count,
            summary.account_accuracy,
            summary.sub_account_accuracy,
            summary.tax_accuracy,
            summary.overall_accuracy,
            summary.classifiable_overall_accuracy,
            summary.auto_post_eligible_count,
            summary.auto_post_eligible_accuracy,
            summary.status,
            1 if summary.promotion_allowed else 0,
            summary.block_reason_code,
            summary.min_sample_required,
            summary.thresholds_hash,
            summary.unknown_pattern_count,
            summary.weak_match_count,
            summary.improvement_candidate_count,
        ),
    )
