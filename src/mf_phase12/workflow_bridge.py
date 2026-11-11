"""Portable subset of the first-party ChatGPT bridge: local run state only.

Source manifests are caller attestations, never official provider receipts.
Accounting verification, Cross Audit and delivery deliberately remain unsupported.
The existing bound SQLite audit log is authoritative; the JSON file is a
rebuildable cache. Local interruptions never authorize a provider retry.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from functools import wraps
import json
import re
import sqlite3
import tempfile
import os
from types import SimpleNamespace
from uuid import uuid4

from .chatgpt_cloud_contract import requested_workflows
from .hash_utils import accounting_run_hash, stable_hash
from .office_profile import using_office_profile, require_database_office_identity


def _bound(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        from .cli import _aggregate_production_execution_lock
        with using_office_profile(self.profile):
            with self._connection():
                pass
            with _aggregate_production_execution_lock():
                self._pending_cache = {}
                try:
                    result = method(self, *args, **kwargs)
                    # Every method has exited its SQLite transaction here.
                    # A failed cache write cannot lose a committed run ID.
                    cache_status = "verified"
                    for run_id, state in self._pending_cache.items():
                        try:
                            self._write_cache(run_id, state)
                        except OSError:
                            cache_status = "repair_pending"
                    return {**result, "local_state_cache": cache_status}
                finally:
                    self._pending_cache = {}
    return call


class WorkflowBridge:
    def __init__(self, profile):
        self.profile = profile
        profile.require_configured()
        # No DB creation, migration, credentials or provider clients.

    @contextmanager
    def _connection(self, write=False):
        with using_office_profile(self.profile):
            self.profile.require_configured()
            conn = sqlite3.connect(self.profile.moneyforward_db_path.as_uri() +
                                   ('?mode=rw' if write else '?mode=ro'), uri=True)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute('PRAGMA foreign_keys=ON')
                require_database_office_identity(conn, self.profile)
                with conn:
                    yield conn
            finally:
                conn.close()

    def _path(self, run_id):
        from .cli import _aggregate_run_artifact_path
        path = _aggregate_run_artifact_path(run_id)
        root = self.profile.moneyforward_db_path.parent
        if root not in path.resolve().parents:
            raise PermissionError('run artifact escapes company root')
        return path

    @staticmethod
    def _digest(value):
        if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value.removeprefix('sha256:')):
            raise ValueError('complete SHA-256 required')
        return value.removeprefix('sha256:')

    def _load(self, run_id, conn):
        from .cli import _assert_run_artifact
        from .shiwake_run import build_run_status
        ledger = build_run_status(conn, run_id)
        anchor = conn.execute(
            "SELECT response_hash,rollback_plan FROM audit_logs "
            "WHERE event_type='portable_workflow_state' AND request_hash=? ORDER BY id DESC LIMIT 1",
            (accounting_run_hash(run_id),)).fetchone()
        if ledger['status'] == 'not_found' or anchor is None:
            raise ValueError('existing portable run and durable state required')
        try:
            envelope = json.loads(anchor['rollback_plan'])
            if envelope.get('schema') != 'portable-workflow-state-v1':
                raise ValueError('unexpected portable state format')
            artifact = envelope['artifact']
            unsigned = {key: value for key, value in artifact.items() if key != 'artifact_hash'}
            digest = stable_hash(unsigned)
            if (anchor['response_hash'] != digest or
                    artifact.get('artifact_hash') != 'sha256:' + digest):
                raise ValueError('durable state digest mismatch')
        except (KeyError, TypeError, AttributeError, ValueError) as error:
            raise ValueError('durable run state integrity check failed') from error
        _assert_run_artifact(artifact)
        if artifact.get('run_id_hash') != 'sha256:' + accounting_run_hash(run_id):
            raise ValueError('run artifact identity mismatch')
        selected = requested_workflows(artifact['project_mode'], artifact['active_workflows'])
        if [w['workflow'] for w in artifact['workflows']] != list(selected):
            raise ValueError('run workflow scope mismatch')
        # A stale/missing/damaged cache is never evidence. Rebuild it from the
        # same verified DB record after this transaction exits, with no API I/O.
        self._pending_cache[run_id] = json.loads(json.dumps(artifact))
        if ledger['status'] == 'uncertain_external_write':
            artifact['status'] = 'uncertain_external_write'
        elif ledger['status'] != 'running':
            raise ValueError('portable run ledger is not open')
        return artifact

    def _write_cache(self, run_id, status):
        path = self._path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8',
                    prefix='.run-cache-', dir=path.parent, delete=False) as handle:
                temp = handle.name
                os.chmod(temp, 0o600)
                json.dump(status, handle, sort_keys=True, indent=2)
                handle.write('\n')
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            if temp is not None:
                try:
                    os.unlink(temp)
                except FileNotFoundError:
                    pass

    def _save(self, conn, run_id, status):
        status.pop('artifact_hash', None)
        status['artifact_hash'] = 'sha256:' + stable_hash(status)
        digest = status['artifact_hash'].removeprefix('sha256:')
        # One SQLite transaction is the durable authority; no new table/DB.
        # The payload holds hashes and local states, never raw documents.
        snapshot = json.loads(json.dumps(status))
        envelope = json.dumps({'schema': 'portable-workflow-state-v1', 'artifact': snapshot},
                              sort_keys=True, separators=(',', ':'))
        conn.execute("INSERT INTO audit_logs(event_hash,event_type,request_hash,response_hash,actor,dry_run,rollback_plan) VALUES(?,'portable_workflow_state',?,?,'workflow_bridge',1,?)",
                     (stable_hash({'transition': uuid4().hex, 'state': digest}),
                      accounting_run_hash(run_id), digest, envelope))
        self._pending_cache[run_id] = snapshot
        return status

    @_bound
    def run_begin(self, instruction, accounting_date, source_scope, mode='DAILY', request_id=None):
        from .cli import _ensure_aggregate_receipt_run
        if mode != 'DAILY':
            raise ValueError('portable backend supports DAILY only')
        if not isinstance(source_scope, dict):
            raise ValueError('source_scope must be an object')
        selected = requested_workflows(mode, source_scope.get('selected_workflows'))
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError('instruction required')
        if date.fromisoformat(accounting_date).isoformat() != accounting_date:
            raise ValueError('canonical accounting date required')
        if set(source_scope) != {'selected_workflows'}:
            raise ValueError('unsupported source scope fields; local backend accepts only selected_workflows')
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', request_id):
            raise ValueError('a caller-retained request_id is required for recoverable run creation')
        # The caller retains this opaque token BEFORE sending the request. A
        # lost response may then be retried without creating another run.
        run_id = 'chatgpt-' + stable_hash({'profile': self.profile.boundary_fingerprint,
                                          'request_id': request_id})
        begin_input_hash = stable_hash({'instruction': instruction, 'date': accounting_date,
                                        'source_scope': source_scope, 'mode': mode})
        status = {'profile_slug': self.profile.slug, 'boundary_fingerprint': self.profile.boundary_fingerprint,
                  'begin_input_hash': begin_input_hash,
                  'run_id_hash': 'sha256:' + accounting_run_hash(run_id), 'project_mode': mode,
                  'accounting_date': accounting_date, 'active_workflows': list(selected),
                  'status': 'incomplete', 'production_ready': False, 'accounting_completed': False,
                  'provider_write_operations_executed': 0,
                  'workflows': [{'workflow': selected[0], 'source_snapshot_hash': stable_hash(source_scope),
                                 'source_discovery_complete': False, 'items': []}]}
        with self._connection(True) as conn:
            exists = conn.execute('SELECT 1 FROM accounting_runs WHERE run_id_hash=?',
                                  (accounting_run_hash(run_id),)).fetchone()
            if exists:
                saved = self._load(run_id, conn)
                if saved.get('begin_input_hash') != begin_input_hash:
                    raise ValueError('request_id already binds different immutable run inputs')
                return {**saved, 'run_id': run_id, 'already_started': True}
            _ensure_aggregate_receipt_run(conn, SimpleNamespace(run_id=run_id, instruction=instruction,
                                                               accounting_date=accounting_date, actor='workflow_bridge'))
            self._save(conn, run_id, status)
        return {**status, 'run_id': run_id, 'already_started': False}

    @_bound
    def run_status(self, run_id):
        with self._connection() as conn:
            return {**self._load(run_id, conn), 'run_id': run_id}

    @_bound
    def run_resume(self, run_id):
        with self._connection() as conn:
            status = self._load(run_id, conn)
        blocked = status['status'] == 'uncertain_external_write'
        pending = [{**w, 'items': [i for i in w['items'] if i['state'] not in
                                  {'REVIEW', 'NOT_READY', 'NOT_APPLICABLE'}]}
                   for w in status['workflows'] if not w['source_discovery_complete'] or
                   any(i['state'] not in {'REVIEW', 'NOT_READY', 'NOT_APPLICABLE'} for i in w['items'])]
        return {**status, 'run_id': run_id, 'resume_workflows': [] if blocked else pending,
                'official_reconciliation_required': blocked,
                'external_operations_executed': 0, 'automatic_writes': False}

    @_bound
    def run_record_source_snapshot(self, run_id, workflow, source_identity_hashes, coverage_hash=''):
        identities = sorted(self._digest(i) for i in source_identity_hashes)
        if len(set(identities)) != len(identities):
            raise ValueError('duplicate source identities')
        coverage = self._digest(coverage_hash)  # Must cover source facts/version, not IDs alone.
        with self._connection(True) as conn:
            status = self._load(run_id, conn)
            self._mutable(status)
            row = self._workflow(status, workflow)
            snapshot = stable_hash({'identities': identities, 'coverage': coverage})
            if row.get('discovery_snapshot_hash') == snapshot:
                return {**status, 'reused': True}
            # Changed coverage invalidates decisions, even with identical IDs.
            row.update(items=[{'item_hash': i, 'state': 'pending'} for i in identities],
                       source_discovery_complete=True, discovery_snapshot_hash=snapshot,
                       discovery_coverage_hash=coverage, discovery_attestation='caller_manifest_only')
            conn.execute('DELETE FROM accounting_run_items WHERE run_id_hash=?', (accounting_run_hash(run_id),))
            for identity in identities:
                conn.execute("INSERT INTO accounting_run_items(run_id_hash,item_hash,source_stage,status) VALUES(?,?,?,'planned')",
                             (accounting_run_hash(run_id), identity, workflow))
            status['status'] = 'incomplete'
            return self._save(conn, run_id, status)

    @staticmethod
    def _workflow(status, workflow):
        return next((w for w in status['workflows'] if w['workflow'] == workflow), None) or _outside_scope()

    @staticmethod
    def _mutable(status):
        if status['status'] == 'uncertain_external_write':
            raise ValueError('official reconciliation required before changes')

    @_bound
    def run_record_item(self, run_id, workflow, source_identity_hash, state, decision, current_facts_hash):
        if state not in {'REVIEW', 'NOT_READY', 'NOT_APPLICABLE', 'retry_pending', 'waiting_for_auth'}:
            raise ValueError('accounting VERIFIED unsupported: official read-back adapter required')
        if not isinstance(decision, dict) or not decision:
            raise ValueError('decision and basis required')
        identity, facts = self._digest(source_identity_hash), self._digest(current_facts_hash)
        with self._connection(True) as conn:
            status = self._load(run_id, conn)
            self._mutable(status)
            row = self._workflow(status, workflow)
            item = next((i for i in row['items'] if i['item_hash'] == identity), None)
            if item is None:
                raise ValueError('item absent from source manifest')
            update = {'item_hash': identity, 'state': state, 'decision_hash': stable_hash(decision),
                      'current_facts_hash': facts}
            if item['state'] in {'REVIEW', 'NOT_READY', 'NOT_APPLICABLE'}:
                if item != update:
                    raise ValueError('finished local decision is immutable until snapshot changes')
                return {'status': 'already_recorded', 'item': item, 'provider_write_operations_executed': 0}
            item.clear()
            item.update(update)
            conn.execute('UPDATE accounting_run_items SET resolution_blocker_code=? WHERE run_id_hash=? AND item_hash=?',
                         (state, accounting_run_hash(run_id), identity))
            self._save(conn, run_id, status)
            return {'status': 'recorded', 'item': item, 'provider_write_operations_executed': 0}


def _outside_scope():
    raise ValueError('workflow outside recorded run scope')
