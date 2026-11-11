"""Actual Payable request-path tests using synthetic credentials and temp DBs."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from mf_phase12.accounting_api import MoneyForwardAccountingClient, MoneyForwardPayableClient, WriteExecutionGrant
from mf_phase12.hash_utils import stable_hash
from mf_phase12.office_profile import OfficeProfile, OfficeProfileError, bind_database_office_identity


def company(root, slug):
    return OfficeProfile(schema_version=2, configured=True, slug=slug,
        legal_name=f"Synthetic {slug}", display_name=f"Synthetic {slug}",
        expected_office_names=(f"Synthetic {slug}",), google_account=f"{slug}@synthetic.invalid",
        moneyforward_keychain_service=f"synthetic-mf-{slug}",
        moneyforward_db_path=root / "data" / "phase12" / "offices" / slug / "ledger.sqlite3",
        google_drive_account="shared@synthetic.invalid", google_drive_evidence_root_id=f"synthetic_root_{slug}",
        google_drive_evidence_source_folder_ids=(f"synthetic_root_{slug}",),
        google_drive_processed_folder_id=f"synthetic_processed_{slug}", repository_root=root,
        accounting_rules={"payable": {"office_id_hash": stable_hash(f"external-office-{slug}")}})


class AccountingTransport:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        if method != "GET" or not url.endswith("/api/v3/offices"):
            raise AssertionError("Payable must not perform Accounting business requests")
        slug = kwargs["headers"]["Authorization"].removeprefix("Bearer accounting-")
        payload = {"data": [{"id": f"accounting-office-{slug}", "name": f"Synthetic {slug}"}]}
        return 200, {}, json.dumps(payload).encode()


class PayableTransport:
    def __init__(self):
        self.calls = []
        self.identity_override = None
        self.identity_status = 200
        self.on_identity = None

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        if method == "GET" and url.endswith("/api/external/v1/offices"):
            slug = kwargs["headers"]["Authorization"].removeprefix("Bearer external-")
            payload = self.identity_override if self.identity_override is not None else {
                "offices": [{"id": f"external-office-{slug}", "identification_code": "1234567890123"}]}
            if self.on_identity is not None:
                self.on_identity()
            return self.identity_status, {}, json.dumps(payload).encode()
        return 200, {}, b'{"data": []}'

    def business_calls(self):
        return [(method, url) for method, url in self.calls if not url.endswith("/api/external/v1/offices")]


class PayableIsolationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for target in ("socket.create_connection", "urllib.request.urlopen", "subprocess.run"):
            guard = patch(target, side_effect=AssertionError("Live network/credential access is forbidden"))
            guard.start()
            self.addCleanup(guard.stop)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.a, self.b = company(self.root, "tenant-a"), company(self.root, "tenant-b")
        self.accounting_transport = AccountingTransport()
        self.accounting = self.accounting_client(self.a)
        for profile in (self.a, self.b):
            profile.moneyforward_db_path.parent.mkdir(parents=True)
            with closing(sqlite3.connect(profile.moneyforward_db_path)) as conn:
                conn.executescript("""
                    CREATE TABLE office_identity_bindings (
                        profile_slug TEXT PRIMARY KEY, profile_hash TEXT NOT NULL,
                        office_id_hash TEXT NOT NULL UNIQUE, legal_name_hash TEXT NOT NULL,
                        keychain_service_hash TEXT NOT NULL, boundary_fingerprint TEXT NOT NULL,
                        status TEXT NOT NULL, raw_office_id_stored INTEGER NOT NULL DEFAULT 0,
                        raw_secret_stored INTEGER NOT NULL DEFAULT 0);
                    CREATE TABLE preservation_marker(value TEXT);
                """)
                bind_database_office_identity(conn, profile, self.accounting_client(profile).validate_office_identity())
                conn.execute("INSERT INTO preservation_marker VALUES ('synthetic-existing-data')")
                conn.commit()
        self.accounting_transport.calls.clear()
        self.transport = PayableTransport()
        self.client = self.new_client()

    def accounting_client(self, profile):
        return MoneyForwardAccountingClient(f"accounting-{profile.slug}", office_profile=profile,
            keychain_service=profile.moneyforward_keychain_service, transport=self.accounting_transport)

    def new_client(self, **kwargs):
        args = dict(gateway_access_token="accounting-tenant-a", external_access_token="external-tenant-a",
            office_id="external-office-tenant-a", office_profile=self.a,
            keychain_service=self.a.moneyforward_keychain_service, accounting_client=self.accounting,
            db_path=self.a.moneyforward_db_path, transport=self.transport)
        args.update(kwargs)
        return MoneyForwardPayableClient(**args)

    def snapshots(self):
        return {p.slug: hashlib.sha256(p.moneyforward_db_path.read_bytes()).hexdigest() for p in (self.a, self.b)}

    def assert_rejected(self, operation=None):
        before = self.snapshots()
        count = len(self.transport.business_calls())
        with self.assertRaises((PermissionError, OfficeProfileError)):
            (operation or self.client.received_invoices)()
        self.assertEqual(len(self.transport.business_calls()), count)
        self.assertEqual(before, self.snapshots())

    def test_both_authenticated_surfaces_read_success_and_no_db_mutation(self):
        before = self.snapshots()
        self.assertEqual(self.client.received_invoices(), {"data": []})
        self.assertEqual(self.client.expense_items(), {"data": []})
        self.assertEqual(len(self.accounting_transport.calls), 2)
        self.assertEqual(len(self.transport.business_calls()), 2)
        self.assertEqual(before, self.snapshots())
        raw = self.a.moneyforward_db_path.read_bytes()
        self.assertNotIn(b"external-office-tenant-a", raw)
        self.assertNotIn(b"accounting-office-tenant-a", raw)

    def test_missing_profile_refused_with_custom_transport(self):
        self.client = self.new_client(office_profile=None)
        self.assert_rejected()
        self.assertEqual(self.transport.calls, [])

    def test_unconfigured_profile_refused(self):
        self.client.office_profile = replace(self.a, configured=False)
        self.assert_rejected()
        self.assertEqual(self.transport.calls, [])

    def test_wrong_or_missing_keychain_refused_before_identity_io(self):
        for value in (None, self.b.moneyforward_keychain_service):
            self.client.keychain_service = value
            self.assert_rejected()
        self.assertEqual(self.accounting_transport.calls, [])
        self.assertEqual(self.transport.calls, [])

    def test_wrong_actual_accounting_keychain_refused(self):
        self.accounting.keychain_service = self.b.moneyforward_keychain_service
        self.assert_rejected()

    def test_wrong_payable_or_accounting_db_refused(self):
        self.client.db_path = self.b.moneyforward_db_path
        self.assert_rejected()
        self.client.db_path = self.a.moneyforward_db_path
        self.accounting.db_path = self.b.moneyforward_db_path
        self.assert_rejected()
        self.assertEqual(self.accounting_transport.calls, [])

    def test_missing_db_never_created(self):
        third = company(self.root, "tenant-c")
        self.client = self.new_client(office_profile=third, keychain_service=third.moneyforward_keychain_service,
            db_path=third.moneyforward_db_path, accounting_client=self.accounting_client(third),
            gateway_access_token="accounting-tenant-c", external_access_token="external-tenant-c", office_id="external-office-tenant-c")
        self.assert_rejected()
        self.assertFalse(third.moneyforward_db_path.parent.exists())

    def test_wrong_accounting_profile_refused(self):
        self.client.accounting_client = self.accounting_client(self.b)
        self.assert_rejected()

    def test_missing_accounting_client_refused(self):
        self.client.accounting_client = None
        self.assert_rejected()

    def test_gateway_token_requires_authoritative_office_not_equality_alone(self):
        self.client.gateway_access_token = "accounting-tenant-b"
        self.accounting.access_token = "accounting-tenant-b"
        self.assert_rejected()
        self.assertEqual(len(self.accounting_transport.calls), 1)
        self.assertEqual(self.transport.calls, [])

    def test_gateway_token_different_from_bound_accounting_credential_refused(self):
        self.client.gateway_access_token = "accounting-tenant-b"
        self.assert_rejected()
        self.assertEqual(self.accounting_transport.calls, [])

    def test_declared_office_does_not_override_authenticated_external_office(self):
        self.client.external_access_token = "external-tenant-b"
        self.assert_rejected()
        self.assertEqual(len(self.transport.calls), 1)

    def test_caller_office_mismatch_refused_before_readback(self):
        self.client.office_id = "external-office-tenant-b"
        self.assert_rejected()
        self.assertEqual(self.transport.calls, [])

    def test_external_token_change_after_success_is_revalidated(self):
        self.client.received_invoices()
        count = len(self.accounting_transport.calls)
        self.client.external_access_token = "external-tenant-b"
        self.assert_rejected()
        self.assertEqual(len(self.accounting_transport.calls), count + 1)

    def test_gateway_token_change_after_success_is_revalidated(self):
        self.client.received_invoices()
        self.client.gateway_access_token = self.accounting.access_token = "accounting-tenant-b"
        self.assert_rejected()

    def test_missing_expected_hash_refused(self):
        self.client.office_profile = replace(self.a, accounting_rules={})
        self.assert_rejected()
        self.assertEqual(self.transport.calls, [])

    def test_ambiguous_or_absent_external_office_refused(self):
        for response in ({"offices": []}, {"offices": [{"id": "external-office-tenant-a"}, {"id": "external-office-tenant-b"}]},
                         {"offices": [{"name": "Synthetic tenant-a"}]}, {"offices": [{"id": True}]}):
            with self.subTest(response=response):
                self.transport.identity_override = response
                self.assert_rejected()

    def test_external_identity_http_failure_refused(self):
        self.transport.identity_status = 401
        self.assert_rejected()

    def test_revoked_binding_refused_before_identity_io(self):
        with closing(sqlite3.connect(self.a.moneyforward_db_path)) as conn:
            with conn:
                conn.execute("UPDATE office_identity_bindings SET status='revoked'")
        self.assert_rejected()
        self.assertEqual(self.accounting_transport.calls, [])

    def test_multiple_binding_rows_refused(self):
        with closing(sqlite3.connect(self.a.moneyforward_db_path)) as conn:
            with conn:
                conn.execute("""INSERT INTO office_identity_bindings
                    SELECT 'tenant-b', profile_hash, 'synthetic-other-hash', legal_name_hash,
                    keychain_service_hash, boundary_fingerprint, 'verified', 0, 0 FROM office_identity_bindings""")
        self.assert_rejected()

    def test_direct_transport_cannot_bypass_default_read_only(self):
        self.assert_rejected(lambda: self.client.transport.request("POST",
            self.client.gateway_base_url + "/v1/received_invoices",
            headers={"Authorization": "Bearer accounting-tenant-a"}, body=b"synthetic"))
        self.assertEqual(self.transport.calls, [])

    def test_direct_transport_wrong_credentials_origin_or_office_refused(self):
        cases = [
            (self.client.gateway_base_url + "/v1/received_invoices", {"Authorization": "Bearer accounting-tenant-b"}),
            ("https://untrusted.synthetic.invalid/v1/received_invoices", {"Authorization": "Bearer accounting-tenant-a"}),
            (self.client.external_base_url + "/api/external/v1/offices/external-office-tenant-b/ex_items", {"Authorization": "Bearer external-tenant-a"}),
            (self.client.gateway_base_url + "/v1/received_invoices", {"Authorization": "Bearer accounting-tenant-a", "authorization": "Bearer accounting-tenant-b"}),
            (self.client.gateway_base_url + "/v1/../other", {"Authorization": "Bearer accounting-tenant-a"}),
        ]
        for url, headers in cases:
            with self.subTest(url=url):
                self.assert_rejected(lambda: self.client.transport.request("GET", url, headers=headers))
        self.assertEqual(self.transport.calls, [])

    def test_direct_identity_endpoint_has_no_public_bypass(self):
        self.assert_rejected(lambda: self.client.transport.request("GET",
            self.client.external_base_url + "/api/external/v1/offices",
            headers={"Authorization": "Bearer external-tenant-a"}))
        self.assertEqual(self.transport.calls, [])

    def test_session_changed_during_identity_readback_refused(self):
        self.transport.on_identity = lambda: setattr(self.client, "gateway_access_token", "accounting-tenant-b")
        self.assert_rejected()

    def test_explicit_mock_write_requires_grant_and_full_boundary(self):
        data = b"synthetic-invoice"
        metadata = self.client.received_invoice_create_metadata(file_name="synthetic.txt", file_hash=hashlib.sha256(data).hexdigest())
        grant = WriteExecutionGrant("payable_received_invoice_create", "synthetic-approval", "synthetic-scope", metadata["request_hash"])
        operation = lambda: self.client.create_received_invoice(file_name="synthetic.txt", content_type="text/plain", file_bytes=data, grant=grant)
        self.assert_rejected(operation)
        self.client.write_enabled = True
        self.assertEqual(operation(), {"data": []})
        self.assertEqual([method for method, _ in self.transport.business_calls()], ["POST"])
        self.client.external_access_token = "external-tenant-b"
        self.assert_rejected(operation)


if __name__ == "__main__":
    unittest.main()
