#!/usr/bin/env python3
"""OFFLINE_SYNTHETIC journal execution, official-readback code and restart demo.

Run from the checkout: PYTHONPATH=src python examples/accounting_pipeline_demo.py
No pytest, network, OAuth or Keychain required. All data lives in a temporary
directory or memory and is removed at exit. Output contains counts/flags only.

Exercises: request builder, explicit synthetic planned ledger, SDK approval,
real journal_write_post, MoneyForwardAccountingClient with synthetic transport,
journal_write_verify, new instances/connections, replay, uncertain POST recovery.
Not exercised: source intake/OCR/classification, master sync, source SDK planning,
file-based grant CLI, attachments, DAILY orchestration, notifications, or live API.
Journal VERIFIED is not accounting workflow completion or real Money Forward E2E.
"""
from pathlib import Path
import json
import sys
from tempfile import TemporaryDirectory

# Support the documented direct command without installing examples as a package.
if __package__ in (None, ""):
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "src"))

from examples.accounting_fixtures import prepare_scenario
from mf_phase12.journal_write_post import JournalCreateDeliveryUncertainError


def demonstrate(root: Path):
    verified = 0
    mutations = 0
    post_attempts = 0
    replay_extra_mutations = 0
    for multiline in (False, True):
        scenario = prepare_scenario(root / ("multi" if multiline else "single"), multiline=multiline)
        post = scenario.execute()
        scenario.restart()  # crash/restart boundary after POST, before verification
        result = scenario.verify(post)
        assert result.verified and result.tax_readback_verified
        assert scenario.states() == ("verified", "verified", "journal_created")
        # The normal path keeps the source ledger awaiting voucher completion.
        before = scenario.provider.mutations
        scenario.restart()
        replay = scenario.execute()
        assert replay.already_executed and not replay.moneyforward_api_called
        replay_extra_mutations += scenario.provider.mutations - before
        verified += 1
        mutations += scenario.provider.mutations
        post_attempts += scenario.provider.post_attempts

    uncertain = prepare_scenario(root / "uncertain", timeout_after_commit=True)
    try:
        uncertain.execute()
    except TimeoutError:
        pass
    else:
        raise AssertionError("synthetic timeout was not exercised")
    assert uncertain.states() == ("planned", "delivery_uncertain", "planned")
    uncertain.restart()
    try:
        uncertain.execute()
    except JournalCreateDeliveryUncertainError:
        blind_retry_blocked = True
    else:
        raise AssertionError("blind retry was not blocked")
    # ID comes from an authoritative synthetic GET, never a manufactured POST result.
    observed = uncertain.client.journals(start_date="2026-01-15", end_date="2026-01-15")
    assert len(observed["data"]) == 1
    recovered = uncertain.reconcile(observed["data"][0]["id"])
    assert recovered.verified and recovered.tax_readback_verified
    assert uncertain.states() == ("verified", "verified", "verified")
    uncertain.restart()
    assert uncertain.execute().already_executed
    assert uncertain.provider.post_attempts == uncertain.provider.mutations == 1
    assert replay_extra_mutations == 0
    return dict(
        evidence_level="OFFLINE_SYNTHETIC", verified_journals=verified + 1,
        synthetic_post_attempts=post_attempts + uncertain.provider.post_attempts,
        synthetic_provider_mutations=mutations + uncertain.provider.mutations,
        replay_extra_mutations=replay_extra_mutations,
        blind_retry_blocked=blind_retry_blocked, uncertain_reconciled=True,
        resumed_new_instances=True, live_api_exercised=False,
        accounting_workflow_completed=False, production_ready=False,
    )


if __name__ == "__main__":
    with TemporaryDirectory(prefix="mf-offline-accounting-") as directory:
        print(json.dumps(demonstrate(Path(directory)), sort_keys=True))
