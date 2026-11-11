"""Offline integration checks for Drive and direct company-RAG entry points.

Only disposable SQLite files and synthetic HTTP responses are used. The actual
application clients and guards run; no provider/credential access is permitted.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
import urllib.parse
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from mf_phase12.accounting_api import MoneyForwardAccountingClient
from mf_phase12.db import Phase12Database
from mf_phase12.drive_move_execute import GoogleDriveApiMoveClient
from mf_phase12.office_profile import (
    OfficeProfile, OfficeProfileError, bind_database_office_identity,
    using_office_profile,
)
from mf_phase12.office_rag_pipeline import build_office_rag


def synthetic_profile(root: Path, slug: str) -> OfficeProfile:
    return OfficeProfile(
        schema_version=2, configured=True, slug=slug,
        legal_name=f"Synthetic {slug}", display_name=f"Synthetic {slug}",
        expected_office_names=(f"Synthetic {slug}",),
        google_account=f"{slug}@synthetic.invalid",
        moneyforward_keychain_service=f"mf-{slug}",
        moneyforward_db_path=root / "data" / "phase12" / "offices" / slug / "ledger.sqlite3",
        google_drive_account="shared@synthetic.invalid",
        google_drive_evidence_root_id=f"synthetic_root_{slug}",
        google_drive_evidence_source_folder_ids=(f"synthetic_root_{slug}", f"synthetic_intake_{slug}"),
        google_drive_processed_folder_id=f"synthetic_processed_{slug}",
        google_drive_temporary_ocr_folder_id=f"synthetic_ocr_{slug}",
        repository_root=root,
    )


class DriveTransport:
    def __init__(self, profile: OfficeProfile):
        self.actor = profile.google_drive_account
        self.parents = [profile.google_drive_evidence_root_id]
        self.calls: list[tuple[str, str]] = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        parsed = urllib.parse.urlsplit(url)
        if method == "GET" and parsed.path.endswith("/about"):
            response = {"user": {"emailAddress": self.actor}}
        elif method == "GET" and "/files/" in parsed.path:
            response = {"id": "synthetic-file", "parents": self.parents}
        elif method == "PATCH" and "/files/" in parsed.path:
            query = urllib.parse.parse_qs(parsed.query)
            self.parents = [query["addParents"][0]]
            response = {"id": "synthetic-file", "parents": self.parents}
        else:
            raise AssertionError("Unexpected synthetic Drive request")
        return 200, {}, json.dumps(response).encode()


class AccountingTransport:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        if method != "GET" or not url.endswith("/api/v3/offices"):
            raise AssertionError("RAG rejection must not reach business endpoints")
        subject = kwargs["headers"]["Authorization"].split()[-1]
        response = {"data": [{"id": f"synthetic-office-{subject}", "name": f"Synthetic {subject}"}]}
        return 200, {}, json.dumps(response).encode()


class OfflineCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for target in ("socket.create_connection", "urllib.request.urlopen", "subprocess.run"):
            guard = patch(target, side_effect=AssertionError("Live service/credential access is forbidden"))
            guard.start()
            self.addCleanup(guard.stop)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.a = synthetic_profile(self.root, "tenant-a")
        self.b = synthetic_profile(self.root, "tenant-b")


class DriveTransportIsolationTests(OfflineCase):
    def client(self, profile=None, *, write_enabled=False):
        profile = profile or self.a
        transport = DriveTransport(profile)
        client = GoogleDriveApiMoveClient("synthetic-drive-token", office_profile=profile,
            write_enabled=write_enabled, transport=transport)
        return client, transport

    def move(self, client, **overrides):
        args = {"file_id": "synthetic-file",
            "source_folder_id": self.a.google_drive_evidence_root_id,
            "destination_folder_id": self.a.google_drive_processed_folder_id}
        args.update(overrides)
        return client.move_file(**args)

    def test_wrong_actor_never_reaches_metadata_or_patch(self):
        client, transport = self.client(write_enabled=True)
        transport.actor = "wrong@synthetic.invalid"
        with self.assertRaises(PermissionError):
            self.move(client)
        self.assertEqual(len(transport.calls), 1)
        self.assertIn("/about?", transport.calls[0][1])

    def test_wrong_source_route_rejected_without_requests(self):
        client, transport = self.client(write_enabled=True)
        with self.assertRaises(OfficeProfileError):
            self.move(client, source_folder_id=self.b.google_drive_evidence_root_id)
        self.assertEqual(transport.calls, [])

    def test_wrong_destination_rejected_without_requests(self):
        client, transport = self.client(write_enabled=True)
        with self.assertRaises(OfficeProfileError):
            self.move(client, destination_folder_id=self.b.google_drive_processed_folder_id)
        self.assertEqual(transport.calls, [])

    def test_other_company_file_parent_rejected_without_patch(self):
        client, transport = self.client(write_enabled=True)
        transport.parents = [self.b.google_drive_evidence_root_id]
        with self.assertRaises(PermissionError):
            self.move(client)
        self.assertEqual([method for method, _ in transport.calls], ["GET", "GET"])

    def test_permitted_metadata_parent_must_match_exact_requested_source(self):
        client, transport = self.client(write_enabled=True)
        transport.parents = [self.a.google_drive_processed_folder_id]
        with self.assertRaises(PermissionError):
            self.move(client)
        self.assertEqual([method for method, _ in transport.calls], ["GET", "GET"])

    def test_writes_default_false(self):
        transport = DriveTransport(self.a)
        client = GoogleDriveApiMoveClient("synthetic-drive-token", office_profile=self.a, transport=transport)
        with self.assertRaises(PermissionError):
            self.move(client)
        self.assertEqual(transport.calls, [])

    def test_shared_actor_correct_routes_allow_mock_write_for_each_company(self):
        self.assertEqual(self.a.google_drive_account, self.b.google_drive_account)
        for profile in (self.a, self.b):
            with self.subTest(slug=profile.slug):
                client, transport = self.client(profile, write_enabled=True)
                response = client.move_file(file_id="synthetic-file",
                    source_folder_id=profile.google_drive_evidence_root_id,
                    destination_folder_id=profile.google_drive_processed_folder_id)
                self.assertEqual(response["parents"], [profile.google_drive_processed_folder_id])
                self.assertEqual([method for method, _ in transport.calls], ["GET", "GET", "PATCH"])
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(transport.calls[-1][1]).query)
                self.assertEqual(query["removeParents"], [profile.google_drive_evidence_root_id])
                self.assertEqual(query["addParents"], [profile.google_drive_processed_folder_id])
                self.assertEqual(client.get_file(file_id="synthetic-file")["parents"], response["parents"])

    def test_actor_changed_after_read_is_revalidated_before_write(self):
        client, transport = self.client(write_enabled=True)
        client.get_file(file_id="synthetic-file")
        transport.actor = "wrong@synthetic.invalid"
        count = len(transport.calls)
        with self.assertRaises(PermissionError):
            self.move(client)
        self.assertEqual([method for method, _ in transport.calls[count:]], ["GET"])

    def test_mixed_company_file_parents_fail_closed(self):
        client, transport = self.client(write_enabled=True)
        transport.parents = [self.a.google_drive_evidence_root_id, self.b.google_drive_evidence_root_id]
        with self.assertRaises(PermissionError):
            self.move(client)
        self.assertTrue(all(method == "GET" for method, _ in transport.calls))

    def test_direct_transport_cannot_bypass_read_only_or_route_guards(self):
        client, transport = self.client()
        query = urllib.parse.urlencode({"addParents": self.b.google_drive_processed_folder_id,
                                       "removeParents": self.a.google_drive_evidence_root_id})
        with self.assertRaises(PermissionError):
            client.transport.request("PATCH", f"{client.base_url}/files/synthetic-file?{query}",
                headers={"Authorization": "Bearer synthetic-drive-token"})
        self.assertTrue(all(method == "GET" for method, _ in transport.calls))


class RagSkipSyncIsolationTests(OfflineCase):
    def setUp(self):
        super().setUp()
        self.clients = {}
        self.transports = {}
        for profile in (self.a, self.b):
            transport = AccountingTransport()
            client = MoneyForwardAccountingClient(profile.slug, office_profile=profile,
                keychain_service=profile.moneyforward_keychain_service, transport=transport)
            with using_office_profile(profile):
                db = Phase12Database(profile.moneyforward_db_path)
                db.initialize()
                with db.connect() as conn:
                    bind_database_office_identity(conn, profile, client.validate_office_identity())
                    conn.execute("CREATE TABLE preservation_marker (value TEXT NOT NULL)")
                    conn.execute("INSERT INTO preservation_marker VALUES ('synthetic-existing-company-data')")
                    conn.commit()
            self.clients[profile.slug] = client
            self.transports[profile.slug] = transport

    def snapshot(self):
        snapshots = {}
        for profile in (self.a, self.b):
            with closing(sqlite3.connect(profile.moneyforward_db_path.as_uri() + "?mode=ro", uri=True)) as conn:
                snapshots[profile.slug] = hashlib.sha256(
                    "\n".join(conn.iterdump()).encode()
                ).hexdigest()
        return snapshots

    def assert_rejected_unchanged(self, *, profile, db_path, client=None, **extra):
        before = self.snapshot()
        try:
            with using_office_profile(profile):
                with self.assertRaises((PermissionError, OfficeProfileError)):
                    build_office_rag(db_path=db_path, client=client, skip_sync=True, **extra)
        finally:
            self.assertEqual(before, self.snapshot(), "Rejected RAG request must not mutate either company DB")

    def test_direct_skip_sync_wrong_db_no_mutation_both_directions(self):
        for selected, other in ((self.a, self.b), (self.b, self.a)):
            with self.subTest(slug=selected.slug):
                self.assert_rejected_unchanged(profile=selected, db_path=other.moneyforward_db_path)

    def test_direct_skip_sync_wrong_client_no_mutation_both_directions(self):
        for selected, other in ((self.a, self.b), (self.b, self.a)):
            with self.subTest(slug=selected.slug):
                self.assert_rejected_unchanged(profile=selected, db_path=selected.moneyforward_db_path,
                    client=self.clients[other.slug])

    def test_direct_skip_sync_wrong_token_no_mutation(self):
        client = self.clients[self.a.slug]
        client.access_token = self.b.slug
        self.assert_rejected_unchanged(profile=self.a, db_path=self.a.moneyforward_db_path, client=client)

    def test_direct_skip_sync_wrong_client_keychain_no_mutation(self):
        client = self.clients[self.a.slug]
        client.keychain_service = self.b.moneyforward_keychain_service
        self.assert_rejected_unchanged(profile=self.a, db_path=self.a.moneyforward_db_path, client=client)

    def test_direct_skip_sync_wrong_client_db_no_mutation(self):
        client = self.clients[self.a.slug]
        client.db_path = self.b.moneyforward_db_path
        self.assert_rejected_unchanged(profile=self.a, db_path=self.a.moneyforward_db_path, client=client)

    def test_direct_skip_sync_wrong_office_no_mutation(self):
        self.assert_rejected_unchanged(profile=self.a, db_path=self.a.moneyforward_db_path,
            client=self.clients[self.a.slug], office_id="synthetic-office-other")

    def test_direct_skip_sync_revoked_binding_no_mutation(self):
        with closing(sqlite3.connect(self.a.moneyforward_db_path)) as conn:
            with conn:
                conn.execute("UPDATE office_identity_bindings SET status='revoked'")
        self.assert_rejected_unchanged(profile=self.a, db_path=self.a.moneyforward_db_path)

    def test_direct_skip_sync_missing_profile_no_mutation(self):
        before = self.snapshot()
        with self.assertRaises(OfficeProfileError):
            build_office_rag(db_path=self.a.moneyforward_db_path, skip_sync=True)
        self.assertEqual(before, self.snapshot())

    def test_direct_skip_sync_missing_db_does_not_create_or_initialize(self):
        third = synthetic_profile(self.root, "tenant-c")
        with using_office_profile(third), self.assertRaises(PermissionError):
            build_office_rag(db_path=third.moneyforward_db_path, skip_sync=True)
        self.assertFalse(third.moneyforward_db_path.parent.exists())

    def test_direct_skip_sync_reset_explicitly_refused(self):
        self.assert_rejected_unchanged(profile=self.a, db_path=self.a.moneyforward_db_path,
            client=self.clients[self.a.slug], reset_existing_rag=True)


if __name__ == "__main__":
    unittest.main()
