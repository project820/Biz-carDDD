import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bizcard_intake import contacts
from bizcard_intake.contacts import ContactSaveError
from bizcard_intake.draft import CardDraft


def command(args):
    return "connections.list" if args[2] == "connections" else args[2]


class FakeGws:
    """Routes run_gws calls by People API method; records (method, params, body)."""

    def __init__(self, connections=(), people=None, create=None, errors=None):
        self.connections = list(connections)
        self.people = dict(people or {})
        self.create = {"resourceName": "people/new"} if create is None else create
        self.errors = dict(errors or {})
        self.calls = []

    def __call__(self, args, allow_failure=False, expect_json=True, mutating=False):
        method = command(args)
        params = json.loads(args[args.index("--params") + 1]) if "--params" in args else {}
        body = json.loads(args[args.index("--json") + 1]) if "--json" in args else None
        self.calls.append((method, params, body))
        if method in self.errors:
            raise self.errors[method]
        if method == "connections.list":
            return {"connections": self.connections}
        if method == "get":
            if params["resourceName"] not in self.people:
                raise ContactSaveError("연락처를 찾을 수 없습니다(404).", kind="not_found")
            return self.people[params["resourceName"]]
        if method == "createContact":
            if body is not None and "resourceName" in self.create:
                self.people[self.create["resourceName"]] = body
            return self.create
        if method == "updateContact":
            self.people[params["resourceName"]] = {**self.people.get(params["resourceName"], {}), **body}
            return body
        return {}

    def methods(self):
        return [c[0] for c in self.calls]

    def body(self, method):
        return next(c[2] for c in self.calls if c[0] == method)


def card(**extra):
    data = {"name": "최민준", "title": "차장", "company": "(주) 다솔이엔지", "mobile": ["010-1234-5678"]}
    data.update(extra)
    return CardDraft.from_dict(data)


def completed(returncode, stdout="", stderr=""):
    return subprocess.CompletedProcess(["gws"], returncode, stdout, stderr)


class MappingTest(unittest.TestCase):
    def test_build_person_puts_display_name_in_given_name_only(self):
        draft = CardDraft.from_dict({"name": "홍길동", "title": "영업팀장/컨설턴트 (Sales Executive)", "company": "(주) ACME", "mobile": ["010-1234-5678"], "work": ["052-555-0126~7"], "fax": ["052-1"], "email": ["a@b.com"], "notes": "메모", "warnings": ["w"]})
        person = contacts.build_person(draft)
        self.assertEqual(person["names"], [{"givenName": "홍길동 영업팀장 (주)ACME"}])
        self.assertEqual(person["organizations"], [{"name": "(주) ACME", "title": "영업팀장/컨설턴트 (Sales Executive)"}])
        self.assertEqual([p["value"] for p in person["phoneNumbers"] if p["type"] == "work"], ["052-555-0126", "052-555-0127"])
        self.assertEqual([p["type"] for p in person["phoneNumbers"]], ["mobile", "work", "work", "workFax"])
        self.assertEqual(person["biographies"][0]["value"], "메모\n확인 필요: w")

    def test_build_person_omits_empty_organization_keys(self):
        person = contacts.build_person(CardDraft.from_dict({"name": "홍길동", "company": "ACME", "mobile": ["010-1"]}))
        self.assertEqual(person["organizations"], [{"name": "ACME"}])

    def test_normalize_phone_variants(self):
        for value in ("010-1234-5678", "+82 10-1234-5678", "+82 (0)10 1234 5678", "0082-10-1234-5678", "82 10 1234 5678", "+82 010-1234-5678"):
            self.assertEqual(contacts.normalize_phone(value), "01012345678", value)
        self.assertEqual(contacts.normalize_phone("+82 2-123-4567"), "021234567")
        self.assertEqual(contacts.normalize_phone("052-555-0126"), "0525550126")


class RunGwsTest(unittest.TestCase):
    def run_with(self, result, **kwargs):
        with patch.object(contacts.subprocess, "run", return_value=result):
            return contacts.run_gws(["people", "people", "get"], **kwargs)

    def test_html_502_becomes_short_transient_error(self):
        html = "<!DOCTYPE html><html><head><title>Error 502 (Server Error)!!1</title></head><body><p><b>502.</b> That’s an error.</p></body></html>"
        with self.assertRaises(ContactSaveError) as ctx:
            self.run_with(completed(1, stdout=html, stderr="Using keyring backend: keyring"))
        exc = ctx.exception
        self.assertEqual((exc.kind, exc.retryable), ("transient", True))
        self.assertEqual(str(exc), "Google 서버 일시 오류(502). 잠시 후 다시 시도합니다.")
        self.assertNotIn("<", str(exc))

    def test_json_errors_are_classified(self):
        cases = {
            '{"error": {"code": 401, "message": "No credentials provided", "reason": "authError"}}': "auth",
            '{"error": {"code": 403, "message": "Request had insufficient authentication scopes."}}': "auth",
            '{"error": {"code": 404, "message": "Requested entity was not found."}}': "not_found",
            '{"error": {"code": 429, "message": "Quota exceeded"}}': "transient",
            '{"error": {"code": 503, "message": "The service is currently unavailable."}}': "transient",
            '{"error": {"code": 400, "message": "Invalid phone"}}': "invalid",
        }
        for stdout, kind in cases.items():
            with self.assertRaises(ContactSaveError) as ctx:
                self.run_with(completed(1, stdout=stdout))
            self.assertEqual(ctx.exception.kind, kind, stdout)
        with self.assertRaises(ContactSaveError) as ctx:
            self.run_with(completed(1, stdout='{"error": {"code": 401, "message": "x"}}'))
        self.assertIn("gws auth login --scopes", str(ctx.exception))

    def test_plain_stderr_without_credentials_is_auth(self):
        with self.assertRaises(ContactSaveError) as ctx:
            self.run_with(completed(1, stderr="Using keyring backend: keyring\nerror[auth]: No credentials provided"))
        self.assertEqual(ctx.exception.kind, "auth")

    def test_invalid_json_on_success(self):
        with self.assertRaises(ContactSaveError) as ctx:
            self.run_with(completed(0, stdout="not json"))
        self.assertEqual(ctx.exception.kind, "unknown_outcome")
        self.assertIn("저장 여부 확인 필요", str(ctx.exception))
        self.assertEqual(self.run_with(completed(0, stdout=""), expect_json=False), {})
        self.assertEqual(self.run_with(completed(0, stdout="Note\n{\"a\": 1}")), {"a": 1})

    def test_timeout_depends_on_mutating(self):
        with patch.object(contacts.subprocess, "run", side_effect=subprocess.TimeoutExpired("gws", 60)):
            with self.assertRaises(ContactSaveError) as ctx:
                contacts.run_gws(["people", "people", "createContact"], mutating=True)
            self.assertEqual((ctx.exception.kind, ctx.exception.retryable), ("unknown_outcome", False))
            with self.assertRaises(ContactSaveError) as ctx:
                contacts.run_gws(["people", "people", "get"])
            self.assertEqual((ctx.exception.kind, ctx.exception.retryable), ("transient", True))

    def test_error_constructor_defaults(self):
        exc = ContactSaveError("x")
        self.assertEqual((str(exc), exc.kind, exc.retryable), ("x", "other", False))


class SaveContactTest(unittest.TestCase):
    def test_refuses_draft_without_name_without_calling_gws(self):
        draft = CardDraft.from_dict({"company": "ACME", "mobile": ["010-1234-5678"]})
        with patch.object(contacts, "run_gws", side_effect=AssertionError("gws must not be called")):
            with self.assertRaises(ContactSaveError) as ctx:
                contacts.save_contact(draft, None)
        self.assertIn("이름 또는 전화번호", str(ctx.exception))
        self.assertEqual(ctx.exception.kind, "invalid")

    def test_create_with_missing_photo_warns_and_checkpoints_first(self):
        fake = FakeGws()
        checkpoints = []

        def checkpoint(resource_name, action):
            checkpoints.append((resource_name, action, list(fake.methods())))

        with patch.object(contacts, "run_gws", fake):
            result = contacts.save_contact(card(), "/nonexistent/card.jpg", checkpoint=checkpoint)
        self.assertEqual((result.action, result.resource_name, result.photo), ("created", "people/new", "missing"))
        self.assertEqual(checkpoints, [("people/new", "created", ["connections.list", "createContact"])])
        self.assertTrue(any("사진" in w for w in result.warnings))
        self.assertNotIn("updateContactPhoto", fake.methods())
        self.assertEqual(result.summary, "새로 저장: 최민준 차장 (주)다솔이엔지")
        self.assertEqual(fake.body("createContact")["names"], [{"givenName": "최민준 차장 (주)다솔이엔지"}])

    def test_photo_upload_failure_is_a_warning(self):
        fake = FakeGws(errors={"updateContactPhoto": ContactSaveError("Google 서버 일시 오류(502). 잠시 후 다시 시도합니다.", kind="transient", retryable=True)})
        with tempfile.NamedTemporaryFile(suffix=".jpg") as image, patch.object(contacts, "run_gws", fake):
            image.write(b"jpg")
            image.flush()
            result = contacts.save_contact(card(), image.name)
        self.assertEqual((result.action, result.photo), ("created", "failed"))
        self.assertTrue(any("사진 업로드 실패" in w for w in result.warnings))

    def test_photo_uploaded_and_verify_mismatch_warns(self):
        fake = FakeGws()
        original = fake.__call__

        def run(args, **kwargs):
            data = original(args, **kwargs)
            if command(args) == "get":
                return {"names": [{"givenName": "다른 이름"}], "phoneNumbers": []}
            return data

        with tempfile.NamedTemporaryFile(suffix=".jpg") as image, patch.object(contacts, "run_gws", run):
            image.write(b"jpg")
            image.flush()
            result = contacts.save_contact(card(), image.name)
        self.assertEqual(result.photo, "uploaded")
        self.assertTrue(any("이름" in w for w in result.warnings))
        self.assertTrue(any("전화번호 누락" in w for w in result.warnings))

    def test_create_without_resource_name_is_unknown_outcome(self):
        fake = FakeGws(create={})
        checkpoints = []
        with patch.object(contacts, "run_gws", fake):
            with self.assertRaises(ContactSaveError) as ctx:
                contacts.save_contact(card(), None, checkpoint=lambda *a: checkpoints.append(a))
        self.assertEqual((ctx.exception.kind, ctx.exception.retryable), ("unknown_outcome", False))
        self.assertIn("저장 여부 확인 필요", str(ctx.exception))
        self.assertEqual(checkpoints, [])

    def test_resource_name_updates_without_search_and_keeps_existing_values(self):
        existing = {
            "resourceName": "people/c9",
            "etag": "e1",
            "names": [{"givenName": "최민준 차장", "familyName": "(주) 다솔이엔지"}],
            "phoneNumbers": [{"value": "+82 10-1234-5678", "type": "mobile", "metadata": {"primary": True}, "canonicalForm": "+821012345678"}, {"value": "02-555-0000", "type": "home"}],
            "emailAddresses": [{"value": "old@x.com", "type": "home"}],
        }
        fake = FakeGws(people={"people/c9": existing})
        with patch.object(contacts, "run_gws", fake):
            result = contacts.save_contact(card(work=["052-111-2222"], email=["OLD@x.com", "new@x.com"]), None, resource_name="people/c9")
        self.assertNotIn("connections.list", fake.methods())
        self.assertNotIn("createContact", fake.methods())
        self.assertEqual((result.action, result.resource_name, result.photo), ("updated", "people/c9", "none"))
        update = next(c for c in fake.calls if c[0] == "updateContact")
        params, body = update[1], update[2]
        self.assertEqual(body["etag"], "e1")
        self.assertEqual(body["names"], [{"givenName": "최민준 차장 (주)다솔이엔지"}])
        self.assertEqual(body["phoneNumbers"], [{"value": "+82 10-1234-5678", "type": "mobile"}, {"value": "02-555-0000", "type": "home"}, {"value": "052-111-2222", "type": "work"}])
        self.assertEqual([e["value"] for e in body["emailAddresses"]], ["old@x.com", "new@x.com"])
        self.assertEqual(params["updatePersonFields"], "names,phoneNumbers,emailAddresses,organizations")
        self.assertNotIn("biographies", body)
        self.assertEqual(result.summary, "기존 연락처 갱신: 최민준 차장 (주)다솔이엔지")

    def test_resource_name_404_falls_back_to_search_and_create(self):
        fake = FakeGws()
        with patch.object(contacts, "run_gws", fake):
            result = contacts.save_contact(card(), None, resource_name="people/gone")
        self.assertEqual(fake.methods()[:3], ["get", "connections.list", "createContact"])
        self.assertEqual(result.action, "created")

    def test_resource_name_auth_error_propagates(self):
        fake = FakeGws(errors={"get": ContactSaveError("login", kind="auth")})
        with patch.object(contacts, "run_gws", fake), self.assertRaises(ContactSaveError) as ctx:
            contacts.save_contact(card(), None, resource_name="people/c9")
        self.assertEqual(ctx.exception.kind, "auth")
        self.assertNotIn("createContact", fake.methods())

    def test_two_candidates_create_new_with_warning(self):
        people = [
            {"resourceName": "people/a", "names": [{"givenName": "최민준 차장"}], "phoneNumbers": [{"value": "010-1234-5678"}]},
            {"resourceName": "people/b", "names": [{"givenName": "최민준"}], "phoneNumbers": [{"value": "0082 10 1234 5678"}]},
        ]
        fake = FakeGws(connections=people)
        with patch.object(contacts, "run_gws", fake):
            result = contacts.save_contact(card(), None)
        self.assertEqual(result.action, "created")
        self.assertIn("중복 후보 2건: 새 연락처로 저장함", result.warnings)


class FindExistingTest(unittest.TestCase):
    def find(self, draft, connections):
        with patch.object(contacts, "run_gws", FakeGws(connections=connections)):
            return contacts.find_existing_candidates(draft)

    def test_scans_all_pages(self):
        draft = CardDraft.from_dict({"name": "홍길동", "title": "이사", "mobile": ["010-1234-5678"]})
        existing = {"resourceName": "people/c1", "names": [{"displayName": "ACME홍길동 이사", "givenName": "홍길동 이사", "familyName": "ACME"}], "phoneNumbers": [{"value": "+82 10-1234-5678"}]}
        pages = {
            None: {"connections": [{"resourceName": "people/c0", "names": [{"givenName": "김철수"}], "phoneNumbers": [{"value": "010-0000-0000"}]}], "nextPageToken": "p2"},
            "p2": {"connections": [existing]},
        }
        calls = []

        def fake_run_gws(args, **kwargs):
            calls.append(args)
            self.assertEqual(args[:4], ["people", "people", "connections", "list"])
            return pages[json.loads(args[5]).get("pageToken")]

        with patch.object(contacts, "run_gws", fake_run_gws):
            self.assertEqual(contacts.find_existing_contact(draft)["resourceName"], "people/c1")
        self.assertEqual(len(calls), 2)

    def test_longer_name_with_same_prefix_does_not_match(self):
        match, count = self.find(card(), [{"resourceName": "people/x", "names": [{"givenName": "최민준수 과장"}], "phoneNumbers": [{"value": "010-1234-5678"}]}])
        self.assertEqual((match, count), (None, 0))

    def test_matches_on_second_mobile_with_0082_and_legacy_layout(self):
        legacy = {"resourceName": "people/old", "names": [{"givenName": "최민준 차장", "familyName": "(주)다솔이엔지"}], "phoneNumbers": [{"value": "0082-10-9999-0000"}]}
        match, count = self.find(card(mobile=["010-1111-2222", "010-9999-0000"]), [legacy])
        self.assertEqual((match["resourceName"], count), ("people/old", 1))

    def test_new_layout_display_name_matches(self):
        person = {"resourceName": "people/n", "names": [{"givenName": "최민준 차장 (주)다솔이엔지"}], "phoneNumbers": [{"value": "+82 (0)10 1234 5678"}]}
        self.assertEqual(self.find(card(), [person])[0]["resourceName"], "people/n")

    def test_work_number_used_only_without_mobile(self):
        person = {"resourceName": "people/w", "names": [{"givenName": "최민준"}], "phoneNumbers": [{"value": "052-555-0126"}]}
        self.assertEqual(self.find(card(mobile=[], work=["052-555-0126"]), [person])[0]["resourceName"], "people/w")
        self.assertEqual(self.find(card(work=["052-555-0126"]), [person]), (None, 0))

    def test_same_person_listed_twice_counts_once(self):
        person = {"resourceName": "people/d", "names": [{"givenName": "최민준"}], "phoneNumbers": [{"value": "010-1234-5678"}]}
        self.assertEqual(self.find(card(), [person, dict(person)])[1], 1)


class UploadPhotoTest(unittest.TestCase):
    def test_missing_file_raises(self):
        with patch.object(contacts, "run_gws", side_effect=AssertionError("no call")):
            with self.assertRaises(ContactSaveError):
                contacts.upload_photo("people/c1", "/nonexistent.jpg")


class GwsErrorMessageTest(unittest.TestCase):
    def test_prefers_json_api_error_over_keyring_notice(self):
        stdout = '{"error": {"code": 401, "message": "Authentication failed", "reason": "authError"}}'
        self.assertEqual(contacts.gws_error_message(stdout, "Using keyring backend: keyring\nerror[auth]: dup"), "Authentication failed")
        self.assertEqual(contacts.gws_error_message("", "Using keyring backend: keyring\nerror[auth]: failed"), "error[auth]: failed")


if __name__ == "__main__":
    unittest.main()
