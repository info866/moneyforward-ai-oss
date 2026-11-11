"""Synthetic tests: private reports and evidence-based completion descriptions."""
import json
from argparse import Namespace
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from mf_phase12 import cli, github_issue_readback


@pytest.mark.parametrize("metadata", [
    {"full_name": "synthetic/reporting", "private": False},
    {"full_name": "synthetic/other", "private": True},
    {"full_name": "synthetic/reporting"},
    {},
])
def test_public_or_unverified_reporting_destination_is_rejected(monkeypatch, metadata):
    calls = []
    def fake(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout=json.dumps(metadata))
    monkeypatch.setattr(cli.subprocess, "run", fake)
    with pytest.raises(PermissionError):
        cli._require_private_reporting_repository("synthetic/reporting")
    assert len(calls) == 1
    assert "POST" not in calls[0]


def test_invalid_reporting_route_never_calls_gh(monkeypatch):
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: pytest.fail("unexpected request"))
    with pytest.raises(ValueError):
        cli._require_private_reporting_repository("synthetic/reporting?redirect=other")


def test_reporting_body_does_not_invent_verified_completion(monkeypatch):
    sent = []
    def fake(command, **kwargs):
        if command[-1] == "repos/synthetic/reporting":
            return SimpleNamespace(stdout=json.dumps({"full_name": "synthetic/reporting", "private": True}))
        if "--paginate" in command:
            return SimpleNamespace(stdout="[[]]")
        if "POST" in command:
            sent.append(next(x[5:] for x in command if x.startswith("body=")))
            return SimpleNamespace(stdout='{"id": 1}')
        return SimpleNamespace(stdout=json.dumps({"id": 1, "body": sent[-1]}))
    monkeypatch.setattr(cli.subprocess, "run", fake)
    monkeypatch.setattr(cli, "_setting", lambda name: "synthetic/reporting" if name.endswith("repository") else 1)
    monkeypatch.setattr(github_issue_readback, "record_github_issue_readback", lambda *a, **k: "synthetic-receipt")
    db = SimpleNamespace(connect=lambda: nullcontext(object()))
    status = {"accounting_date": "2026-01-01", "status": "running", "remaining_count": 2,
              "stages": [{"stage": "card", "status": "pending"}]}
    result = cli._record_unified_workflow_issue_update(db, Namespace(run_id="synthetic-run", actor="test"), status=status)
    assert result == ("synthetic-receipt", "executed")
    assert "unresolved: 2" in sent[0]
    assert "7/7 verified" not in sent[0]
    assert "cross audit: verified" not in sent[0]
    assert "ワークフロー完了" not in sent[0]
    assert '"card": "pending"' in sent[0]
