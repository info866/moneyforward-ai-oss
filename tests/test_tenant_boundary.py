"""Synthetic tenant isolation tests; no real DB, Keychain or network access."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import patch

from mf_phase12.accounting_api import MoneyForwardAccountingClient
from mf_phase12.hash_utils import stable_hash
from mf_phase12.office_profile import (
    OfficeIdentityBinding, OfficeProfileError, bind_database_office_identity,
    current_database_office_binding_hash, get_current_profile, get_setting,
    load_office_profile, normalize_office_name, require_database_office_identity,
    require_google_drive_processed_folder, resolve_moneyforward_db_path,
    resolve_moneyforward_keychain_service, resolve_office_profile, using_office_profile,
)
from mf_phase12.tenant_boundary import validate_tenant_boundary

SCHEMA = """
CREATE TABLE office_identity_bindings (
 profile_slug TEXT PRIMARY KEY, profile_hash TEXT NOT NULL,
 office_id_hash TEXT NOT NULL UNIQUE, legal_name_hash TEXT NOT NULL,
 keychain_service_hash TEXT NOT NULL, boundary_fingerprint TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('verified', 'revoked')),
 raw_office_id_stored INTEGER NOT NULL DEFAULT 0 CHECK(raw_office_id_stored=0),
 raw_secret_stored INTEGER NOT NULL DEFAULT 0 CHECK(raw_secret_stored=0)
);
CREATE TABLE rag_cards (content TEXT);
CREATE TABLE journals (description TEXT);
"""


def payload(slug="tenant-a"):
    return {
        "schema_version": 2, "configured": True, "slug": slug,
        "legal_name": f"Synthetic {slug}", "display_name": f"Synthetic {slug}",
        "moneyforward": {"expected_office_names": [f"Synthetic {slug}"],
            "keychain_service": f"mf-{slug}-automation",
            "db_path": f"data/phase12/offices/{slug}/accounting.sqlite3"},
        "google_workspace": {"account": f"{slug}@synthetic.invalid"},
        "google_drive": {"account": "shared@synthetic.invalid",
            "evidence_root_id": f"synthetic_root_{slug}",
            "evidence_source_folder_ids": [f"synthetic_root_{slug}", f"synthetic_intake_{slug}"],
            "processed_folder_id": f"synthetic_processed_{slug}",
            "temporary_ocr_folder_id": f"synthetic_ocr_{slug}"},
        "google_chat": {"keychain_service": f"chat-{slug}", "keychain_account": "incoming_url"},
        "accounting_rules": {"posting": {"enabled": False, "codes": ["synthetic-code"]}},
    }


class SyntheticClient:
    """The token chooses an offices response; no plaintext equality gate."""
    def __init__(self, profile):
        self.office_profile = profile
        self.keychain_service = profile.moneyforward_keychain_service
        self.token_subject = profile.slug
        self.office_id = f"synthetic-office-{profile.slug}"
        self.readbacks = 0
        self.business_calls = 0

    def validate_office_identity(self, *, office_id=None):
        self.readbacks += 1
        if self.token_subject != self.office_profile.slug:
            raise PermissionError("authoritative synthetic offices response is a different tenant")
        if office_id is not None and office_id != self.office_id:
            raise PermissionError("requested office not returned by authoritative readback")
        return OfficeIdentityBinding(
            profile_slug=self.office_profile.slug, profile_hash=self.office_profile.profile_hash,
            office_id=self.office_id, office_id_hash=stable_hash(self.office_id),
            legal_name_hash=stable_hash(normalize_office_name(self.office_profile.legal_name)),
            keychain_service_hash=stable_hash(self.office_profile.moneyforward_keychain_service))


class SyntheticAccountingTransport:
    """Exercise the real API client's guarded request path without a network."""
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        subject = kwargs["headers"]["Authorization"].split()[-1]
        if url.endswith("/api/v3/offices"):
            response = {"data": [{"id": f"synthetic-office-{subject}", "name": f"Synthetic {subject}"}]}
        else:
            response = {"data": []}
        return 200, {}, json.dumps(response).encode()


class TenantBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.profiles = self.root / "config" / "offices"
        self.profiles.mkdir(parents=True)
        self.a = self.load_payload(payload())
        self.b = self.load_payload(payload("tenant-b"))
        self.conn = self.create_db(self.a)
        self.client = SyntheticClient(self.a)
        self.validate(allow_new_binding=True)

    def load_payload(self, value):
        (self.profiles / f"{value['slug']}.json").write_text(json.dumps(value), encoding="utf-8")
        return load_office_profile(value["slug"], profiles_dir=self.profiles, repository_root=self.root)

    def create_db(self, profile):
        profile.moneyforward_db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(profile.moneyforward_db_path)
        self.addCleanup(conn.close)
        conn.executescript(SCHEMA)
        return conn

    def validate(self, **kwargs):
        args = dict(profile=self.a, conn=self.conn, client=self.client,
            keychain_service=self.a.moneyforward_keychain_service,
            provider_principals={"google_workspace": self.a.google_account,
                "google_drive": self.a.google_drive_account},
            drive_routes={"evidence_root": self.a.google_drive_evidence_root_id},
            rag_db_path=self.a.moneyforward_db_path)
        args.update(kwargs)
        return validate_tenant_boundary(**args)

    def test_valid_boundary_and_shared_drive(self):
        result = self.validate()
        self.assertEqual(self.a.google_drive_account, self.b.google_drive_account)
        self.assertEqual(result.db_path, result.rag_db_path)
        self.assertEqual(result.boundary_fingerprint, self.a.boundary_fingerprint)
        self.assertEqual(self.client.business_calls, 0)

    def test_wrong_combinations_fail_before_business_io(self):
        for overrides in (
            {"db_path": self.b.moneyforward_db_path},
            {"rag_db_path": self.b.moneyforward_db_path},
            {"keychain_service": self.b.moneyforward_keychain_service},
            {"drive_routes": {"evidence_root": self.b.google_drive_evidence_root_id}},
            {"provider_principals": {"google_workspace": self.b.google_account, "google_drive": self.a.google_drive_account}},
            {"office_id": "synthetic-wrong-office"},
        ):
            with self.subTest(overrides=overrides), self.assertRaises((PermissionError, OfficeProfileError)):
                self.validate(**overrides)
        self.assertEqual(self.client.business_calls, 0)

    def test_wrong_actual_connection_even_if_path_argument_matches(self):
        other = self.create_db(self.b)
        before = self.client.readbacks
        with self.assertRaises(PermissionError):
            self.validate(conn=other, db_path=self.a.moneyforward_db_path)
        self.assertEqual(before, self.client.readbacks)

    def test_actual_keychain_source_required_not_profile_derived_hash(self):
        self.client.keychain_service = self.b.moneyforward_keychain_service
        before = self.client.readbacks
        with self.assertRaises(PermissionError):
            self.validate(keychain_service=self.a.moneyforward_keychain_service)
        self.assertEqual(before, self.client.readbacks)
        del self.client.keychain_service
        with self.assertRaises(PermissionError):
            self.validate()

    def test_token_change_revalidates_authoritative_readback(self):
        self.validate()
        before = self.client.readbacks
        self.client.token_subject = self.b.slug
        with self.assertRaises(PermissionError):
            self.validate()
        self.assertEqual(self.client.readbacks, before + 1)
        self.assertEqual(self.client.business_calls, 0)

    def test_wrong_live_office_even_with_valid_profile(self):
        self.client.office_id = "synthetic-office-other"
        with self.assertRaises(PermissionError):
            self.validate()

    def test_both_token_directions(self):
        other = self.create_db(self.b)
        bclient = SyntheticClient(self.b)
        principals = {"google_workspace": self.b.google_account, "google_drive": self.b.google_drive_account}
        args = dict(profile=self.b, conn=other, client=bclient,
            provider_principals=principals, keychain_service=self.b.moneyforward_keychain_service,
            drive_routes={"evidence_source": self.b.google_drive_evidence_root_id},
            rag_db_path=self.b.moneyforward_db_path)
        self.validate(**args, allow_new_binding=True)
        bclient.token_subject = self.a.slug
        with self.assertRaises(PermissionError):
            self.validate(**args)
        self.client.token_subject = self.b.slug
        with self.assertRaises(PermissionError):
            self.validate()

    def test_all_routes_exact_and_explicit(self):
        for role, good, bad in (
            ("evidence_source", self.a.google_drive_evidence_source_folder_ids[1], self.b.google_drive_evidence_source_folder_ids[1]),
            ("processed", self.a.google_drive_processed_folder_id, self.b.google_drive_processed_folder_id),
            ("temporary_ocr", self.a.google_drive_temporary_ocr_folder_id, self.b.google_drive_temporary_ocr_folder_id),
        ):
            with self.subTest(role=role):
                self.validate(drive_routes={role: good})
                with self.assertRaises(OfficeProfileError):
                    self.validate(drive_routes={role: bad})
        for routes in ({}, {"unknown": "synthetic_root"}, {"processed": None}):
            with self.assertRaises(OfficeProfileError):
                self.validate(drive_routes=routes)

    def test_full_fingerprint_covers_fields_omitted_by_legacy_hash(self):
        for changes in (
            {"google_account": "other@synthetic.invalid"},
            {"google_drive_account": "other@synthetic.invalid"},
            {"google_chat_keychain_account": "other@synthetic.invalid"},
            {"google_drive_evidence_source_folder_ids": (*self.a.google_drive_evidence_source_folder_ids, "synthetic_extra_source")},
            {"google_drive_temporary_ocr_folder_id": "synthetic_other_ocr"},
            {"accounting_rules": {"posting": {"enabled": True}}},
        ):
            with self.subTest(changes=changes):
                changed = replace(self.a, **changes)
                self.assertEqual(changed.profile_hash, self.a.profile_hash)
                self.assertNotEqual(changed.boundary_fingerprint, self.a.boundary_fingerprint)
                with self.assertRaises(PermissionError):
                    require_database_office_identity(self.conn, changed)

    def test_fingerprint_includes_absolute_location(self):
        new_root = self.root / "another-copy"
        changed = replace(self.a, repository_root=new_root,
            moneyforward_db_path=new_root / self.a.moneyforward_db_path.relative_to(self.root))
        self.assertEqual(changed.profile_hash, self.a.profile_hash)
        self.assertNotEqual(changed.boundary_fingerprint, self.a.boundary_fingerprint)

    def test_single_binding_required_even_if_other_is_revoked(self):
        values = self.conn.execute("SELECT * FROM office_identity_bindings").fetchone()
        self.conn.execute("INSERT INTO office_identity_bindings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.b.slug, values[1], stable_hash("synthetic-other"), *values[3:6], "revoked", 0, 0))
        self.conn.commit()
        with self.assertRaises(PermissionError):
            self.validate()
        with self.assertRaises(PermissionError):
            current_database_office_binding_hash(self.conn)

    def test_revoked_and_stale_bindings_refused(self):
        for column, value in (("status", "revoked"), ("boundary_fingerprint", stable_hash("stale"))):
            original = self.conn.execute(f"SELECT {column} FROM office_identity_bindings").fetchone()[0]
            self.conn.execute(f"UPDATE office_identity_bindings SET {column}=?", (value,))
            self.conn.commit()
            with self.assertRaises(PermissionError):
                self.validate()
            self.conn.execute(f"UPDATE office_identity_bindings SET {column}=?", (original,))
            self.conn.commit()

    def test_existing_binding_and_rag_never_rewritten(self):
        self.conn.execute("INSERT INTO rag_cards VALUES ('synthetic-existing-learning')")
        self.conn.commit()
        before = tuple(self.conn.iterdump())
        self.validate(allow_new_binding=True)
        self.assertEqual(before, tuple(self.conn.iterdump()))
        self.assertNotIn(self.client.office_id, "\n".join(before))
        self.assertNotIn(self.client.office_id, repr(self.validate()))

    def test_new_binding_refuses_nonempty_business_or_rag_db(self):
        self.conn.execute("DELETE FROM office_identity_bindings")
        for table in ("journals", "rag_cards"):
            self.conn.execute(f"INSERT INTO {table} VALUES ('synthetic-preserved')")
            self.conn.commit()
            before = tuple(self.conn.iterdump())
            with self.assertRaises(PermissionError):
                self.validate(allow_new_binding=True)
            self.assertEqual(before, tuple(self.conn.iterdump()))

    def test_new_binding_requires_complete_validated_boundary(self):
        self.conn.execute("DELETE FROM office_identity_bindings")
        self.conn.commit()
        with self.assertRaises(PermissionError):
            bind_database_office_identity(self.conn, self.a, self.client.validate_office_identity())
        self.assertEqual(self.conn.execute("SELECT count(*) FROM office_identity_bindings").fetchone()[0], 0)

    def test_memory_and_attached_other_db_refused(self):
        memory = sqlite3.connect(":memory:")
        self.addCleanup(memory.close)
        with self.assertRaises(PermissionError):
            self.validate(conn=memory)
        self.conn.execute("ATTACH DATABASE ':memory:' AS another_company")
        with self.assertRaises(PermissionError):
            self.validate()

    def test_profile_and_nested_rules_immutable(self):
        with self.assertRaises(FrozenInstanceError):
            self.a.slug = "tenant-b"
        with self.assertRaises(TypeError):
            self.a.accounting_rules["posting"]["enabled"] = True
        self.assertIsInstance(self.a.get_setting("posting.codes"), tuple)

    def test_context_explicit_and_restored_even_on_exception(self):
        self.assertIsNone(get_current_profile())
        with using_office_profile(self.a):
            self.assertIs(resolve_office_profile(), self.a)
            self.assertFalse(get_setting("accounting_rules.posting.enabled"))
            self.assertEqual(get_setting("absent", "fallback"), "fallback")
            with self.assertRaises(RuntimeError):
                with using_office_profile(self.b):
                    self.assertIs(resolve_office_profile(), self.b)
                    raise RuntimeError("synthetic")
            self.assertIs(get_current_profile(), self.a)
        self.assertIsNone(get_current_profile())

    def test_no_implicit_profile_or_env_bypass(self):
        with patch.dict(os.environ, {"MF_SKIP_TENANT_CHECK": "1", "MF_ALLOW_UNBOUND_DB": "1"}, clear=True):
            with self.assertRaises(OfficeProfileError):
                resolve_office_profile(required=True)
            with self.assertRaises(OfficeProfileError):
                get_setting("posting.enabled", False)
            with self.assertRaises(OfficeProfileError):
                self.validate(profile=None)
            with self.assertRaises(OfficeProfileError):
                self.validate(keychain_service="synthetic-wrong")
        with patch.dict(os.environ, {"MF_OFFICE_PROFILE": self.a.slug}, clear=True):
            loaded = resolve_office_profile(required=True, profiles_dir=self.profiles, repository_root=self.root)
            self.assertEqual(loaded.boundary_fingerprint, self.a.boundary_fingerprint)

    def test_no_transport_type_exception(self):
        self.client.transport = object()
        self.client.office_profile = None
        with self.assertRaises(PermissionError):
            self.validate()

    def test_placeholders_and_unconfigured_profiles_cannot_authorize(self):
        for changed in (replace(self.a, configured=False), replace(self.a, google_drive_evidence_root_id="REPLACE_ME_ROOT")):
            with self.assertRaises(OfficeProfileError):
                self.validate(profile=changed)
        item = payload("example")
        item["configured"] = False
        example = self.load_payload(item)
        with self.assertRaises(OfficeProfileError):
            example.require_configured()
        item["configured"] = True
        with self.assertRaises(OfficeProfileError):
            self.load_payload(item)

    def test_secret_fields_rejected_recursively_and_without_echo(self):
        for key in ("token", "access_token", "office_id", "client_secret", "privateKey", "oauthToken", "credentials"):
            item = payload()
            item["accounting_rules"] = {"nested": [{key: "synthetic-secret-must-not-echo"}]}
            with self.subTest(key=key), self.assertRaises(OfficeProfileError) as raised:
                self.load_payload(item)
            self.assertNotIn("synthetic-secret-must-not-echo", str(raised.exception))

    def test_path_escape_and_slug_rejected(self):
        for db_path in ("../outside.sqlite3", "/tmp/outside.sqlite3", "data/phase12/offices/tenant-b/other.sqlite3"):
            item = payload()
            item["moneyforward"]["db_path"] = db_path
            with self.assertRaises(OfficeProfileError):
                self.load_payload(item)
        with self.assertRaises(OfficeProfileError):
            load_office_profile("../tenant-a", profiles_dir=self.profiles)

    def test_resolvers_refuse_mismatch_even_without_external_flag(self):
        with self.assertRaises(OfficeProfileError):
            resolve_moneyforward_db_path(self.a, self.b.moneyforward_db_path)
        with self.assertRaises(OfficeProfileError):
            resolve_moneyforward_keychain_service(self.a, self.b.moneyforward_keychain_service)
        with self.assertRaises(OfficeProfileError):
            resolve_moneyforward_db_path(None, self.a.moneyforward_db_path, external_io=True)

    def test_missing_or_overlapping_drive_destination(self):
        with self.assertRaises(OfficeProfileError):
            require_google_drive_processed_folder(replace(self.a, google_drive_processed_folder_id=None))
        item = payload()
        item["google_drive"]["processed_folder_id"] = item["google_drive"]["evidence_source_folder_ids"][1]
        with self.assertRaises(OfficeProfileError):
            self.load_payload(item)

    def test_v1_hash_algorithm_preserved(self):
        legacy = replace(self.a, schema_version=1, configured=False)
        expected = {
            "schema_version": 1, "slug": legacy.slug,
            "legal_name": normalize_office_name(legacy.legal_name),
            "expected_office_names": sorted(normalize_office_name(n) for n in legacy.expected_office_names),
            "moneyforward_keychain_service": legacy.moneyforward_keychain_service,
            "moneyforward_db_path": legacy.moneyforward_db_path.relative_to(self.root).as_posix(),
            "google_chat_keychain_service": legacy.google_chat_keychain_service,
            "google_drive_evidence_root_id": legacy.google_drive_evidence_root_id,
            "google_drive_processed_folder_id": legacy.google_drive_processed_folder_id or "",
        }
        expected_hash = hashlib.sha256(json.dumps(expected, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(legacy.profile_hash, expected_hash)
        with self.assertRaises(OfficeProfileError):
            legacy.require_configured()

    def test_binding_hash_keeps_legacy_shape(self):
        binding = self.client.validate_office_identity()
        expected = {k: getattr(binding, k) for k in ("profile_slug", "profile_hash", "office_id_hash", "legal_name_hash", "keychain_service_hash")}
        self.assertEqual(binding.binding_hash, stable_hash(expected))
        self.assertEqual(binding.binding_hash, replace(binding, boundary_fingerprint=self.a.boundary_fingerprint).binding_hash)

    def actual_client(self):
        transport = SyntheticAccountingTransport()
        client = MoneyForwardAccountingClient(self.a.slug, office_profile=self.a,
            keychain_service=self.a.moneyforward_keychain_service, transport=transport)
        return client, transport

    def test_request_path_chat_storage_label_is_not_provider_principal(self):
        client, transport = self.actual_client()
        self.assertEqual(self.a.google_chat_keychain_account, "incoming_url")
        self.validate(client=client)
        self.assertEqual(client.accounts(), {"data": []})
        self.assertEqual([url.rsplit("/", 1)[-1] for _, url in transport.calls],
                         ["offices", "offices", "accounts"])

    def test_request_path_rechecks_changed_token_without_helper_reuse(self):
        client, transport = self.actual_client()
        self.validate(client=client)
        client.accounts()
        count = len(transport.calls)
        client.access_token = self.b.slug
        with self.assertRaises(PermissionError):
            client.accounts()
        self.assertGreater(len(transport.calls), count)
        self.assertTrue(all(url.endswith("/offices") for _, url in transport.calls[count:]))

    def test_request_path_rejects_changed_db_keychain_or_profile(self):
        for attribute, value in (("db_path", self.b.moneyforward_db_path),
                ("keychain_service", self.b.moneyforward_keychain_service),
                ("office_profile", replace(self.a, google_drive_account="other@synthetic.invalid"))):
            with self.subTest(attribute=attribute):
                client, transport = self.actual_client()
                self.validate(client=client)
                count = len(transport.calls)
                setattr(client, attribute, value)
                with self.assertRaises((PermissionError, OfficeProfileError)):
                    client.accounts()
                self.assertTrue(all(url.endswith("/offices") for _, url in transport.calls[count:]))

    def test_request_path_shared_drive_wrong_route_stops_before_business_read(self):
        client, transport = self.actual_client()
        self.assertEqual(self.a.google_drive_account, self.b.google_drive_account)
        with self.assertRaises(OfficeProfileError):
            self.validate(client=client, drive_routes={"evidence_source": self.b.google_drive_evidence_root_id})
            client.accounts()
        self.assertEqual(transport.calls, [])
        self.validate(client=client, drive_routes={"evidence_source": self.a.google_drive_evidence_root_id})
        client.accounts()
        self.assertTrue(transport.calls[-1][1].endswith("/accounts"))

    def test_request_path_direct_write_stays_read_only(self):
        client, transport = self.actual_client()
        self.validate(client=client)
        count = len(transport.calls)
        with self.assertRaises(PermissionError):
            client.transport.request("POST", f"{client.base_url}/api/v3/journals",
                headers={"Authorization": f"Bearer {self.a.slug}"}, body=b"{}")
        self.assertTrue(all(method == "GET" and url.endswith("/offices") for method, url in transport.calls[count:]))

    def test_request_path_wrong_office_refused_after_validation(self):
        client, transport = self.actual_client()
        self.validate(client=client)
        count = len(transport.calls)
        with self.assertRaises(PermissionError):
            client.accounts(office_id="synthetic-office-other")
        self.assertTrue(all(url.endswith("/offices") for _, url in transport.calls[count:]))

    def test_request_path_rejects_other_authorization_header(self):
        client, transport = self.actual_client()
        self.validate(client=client)
        count = len(transport.calls)
        with self.assertRaises(PermissionError):
            client.transport.request("GET", f"{client.base_url}/api/v3/accounts",
                headers={"Authorization": f"Bearer {self.b.slug}"})
        self.assertTrue(all(url.endswith("/offices") for _, url in transport.calls[count:]))

    def test_request_path_unconfigured_profile_blocks_identity_read_too(self):
        client, transport = self.actual_client()
        client.office_profile = replace(self.a, configured=False)
        with self.assertRaises((PermissionError, OfficeProfileError)):
            client.offices()
        self.assertEqual(transport.calls, [])


if __name__ == "__main__":
    unittest.main()
