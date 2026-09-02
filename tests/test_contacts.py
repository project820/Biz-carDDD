import json
import unittest
from unittest.mock import patch

from bizcard_intake import contacts
from bizcard_intake.draft import CardDraft


class ContactsTest(unittest.TestCase):
    def test_build_person_maps_fields(self):
        draft = CardDraft.from_dict({"name": "홍길동", "title": "이사", "company": "ACME", "mobile": ["010-1234-5678"], "work": ["052-297-8976~7"], "fax": ["052-1"], "email": ["a@b.com"], "notes": "메모", "warnings": ["w"]})
        person = contacts.build_person(draft)
        self.assertEqual(person["names"], [{"givenName": "홍길동 이사", "familyName": "ACME"}])
        self.assertEqual(person["organizations"], [{"name": "ACME", "title": "이사"}])
        self.assertEqual([p["value"] for p in person["phoneNumbers"] if p["type"] == "work"], ["052-297-8976", "052-297-8977"])
        self.assertEqual(person["biographies"][0]["value"], "메모\n검증 경고: w")

    def test_save_refuses_draft_without_name_without_calling_gws(self):
        draft = CardDraft.from_dict({"company": "ACME", "mobile": ["010-1234-5678"]})
        with patch.object(contacts, "run_gws", side_effect=AssertionError("gws must not be called")):
            with self.assertRaises(contacts.ContactSaveError) as ctx:
                contacts.save_contact(draft, None)
        self.assertIn("이름 또는 전화번호", str(ctx.exception))

    def test_find_existing_contact_scans_all_pages(self):
        draft = CardDraft.from_dict({"name": "홍길동", "title": "이사", "mobile": ["010-1234-5678"]})
        existing = {"resourceName": "people/c1", "names": [{"displayName": "ACME홍길동 이사", "givenName": "홍길동 이사", "familyName": "ACME"}], "phoneNumbers": [{"value": "+82 10-1234-5678"}]}
        pages = {
            None: {"connections": [{"resourceName": "people/c0", "names": [{"givenName": "김철수"}], "phoneNumbers": [{"value": "010-0000-0000"}]}], "nextPageToken": "p2"},
            "p2": {"connections": [existing]},
        }
        calls = []

        def fake_run_gws(args, allow_failure=False):
            calls.append(args)
            self.assertEqual(args[:4], ["people", "people", "connections", "list"])
            return pages[json.loads(args[5]).get("pageToken")]

        with patch.object(contacts, "run_gws", fake_run_gws):
            self.assertEqual(contacts.find_existing_contact(draft)["resourceName"], "people/c1")
        self.assertEqual(len(calls), 2)

    def test_gws_error_message_prefers_json_api_error_over_keyring_notice(self):
        stdout = '{"error": {"code": 401, "message": "Authentication failed", "reason": "authError"}}'
        self.assertEqual(contacts.gws_error_message(stdout, "Using keyring backend: keyring\nerror[auth]: dup"), "Authentication failed")
        self.assertEqual(contacts.gws_error_message("", "Using keyring backend: keyring\nerror[auth]: failed"), "error[auth]: failed")


if __name__ == "__main__":
    unittest.main()
