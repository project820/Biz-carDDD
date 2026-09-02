import unittest

from bizcard_intake.draft import CardDraft, clean_value, expand_phone_range


class DraftTest(unittest.TestCase):
    def test_missing_title_defaults_to_daepyo_and_name_field_joins_title(self):
        draft = CardDraft.from_dict({"name": "홍길동", "company": "ACME", "mobile": ["010-1234-5678"]})
        self.assertEqual(draft.title, "대표")
        self.assertEqual(draft.name_field, "홍길동 대표")
        self.assertTrue(draft.is_savable)

    def test_phone_range_notation_expands_to_two_lines(self):
        self.assertEqual(expand_phone_range("052-297-8976~7"), ["052-297-8976", "052-297-8977"])
        self.assertEqual(expand_phone_range("02-123-4567~9"), ["02-123-4567", "02-123-4569"])
        self.assertEqual(expand_phone_range("052-297-8976~8977"), ["052-297-8976", "052-297-8977"])
        self.assertEqual(expand_phone_range("010-1234-5678"), ["010-1234-5678"])
        draft = CardDraft.from_dict({"name": "홍길동", "work": "052-297-8976~7"})
        self.assertEqual(draft.work, ["052-297-8976", "052-297-8977"])

    def test_placeholders_and_check_notes_are_dropped(self):
        self.assertEqual(clean_value("(확인 필요)"), "")
        self.assertEqual(clean_value("(미기재)"), "")
        self.assertEqual(clean_value("010-1234-5678 (확인 필요)"), "010-1234-5678")
        self.assertEqual(clean_value("서울시 강남구 (본사)"), "서울시 강남구 (본사)")
        draft = CardDraft.from_dict({"name": "홍길동", "fax": "(확인 필요)", "mobile": "010-1234-5678, 010-1234-5678"})
        self.assertEqual(draft.fax, [])
        self.assertEqual(draft.mobile, ["010-1234-5678"])

    def test_from_dict_tolerates_strings_none_and_lists(self):
        draft = CardDraft.from_dict({"name": None, "email": "a@b.com; c@d.com", "address": "서울시 강남구 테헤란로 1, 2층", "warnings": "이름 없음"})
        self.assertEqual(draft.name, "")
        self.assertEqual(draft.email, ["a@b.com", "c@d.com"])
        self.assertEqual(draft.address, ["서울시 강남구 테헤란로 1, 2층"])
        self.assertEqual(draft.warnings, ["이름 없음"])
        self.assertFalse(draft.is_savable)

    def test_to_text_keeps_korean_draft_format(self):
        draft = CardDraft.from_dict({"name": "홍길동", "title": "이사", "company": "ACME", "mobile": ["010-1234-5678"], "warnings": ["팩스 불명확"]})
        text = draft.to_text()
        self.assertTrue(text.startswith("명함 입력 초안\n- 이름 필드: 홍길동 이사\n- 성씨 필드: ACME\n- 휴대전화: 010-1234-5678\n"))
        self.assertIn("- 검증 경고: 팩스 불명확", text)
        self.assertEqual(CardDraft.from_json(draft.to_json()), draft)

    def test_from_text_parses_legacy_draft(self):
        text = "명함 입력 초안\n- 이름 필드: 홍길동 이사\n- 성씨 필드: ACME\n- 휴대전화: 010-1234-5678\n- 회사전화: 052-297-8976~7\n- 팩스: (확인 필요)\n- 이메일: a@b.com\n- 웹사이트: \n- 주소: 서울시 강남구\n- 메모: 메모\n- 검증 경고: 팩스 불명확"
        draft = CardDraft.from_text(text)
        self.assertEqual((draft.name, draft.title, draft.company), ("홍길동", "이사", "ACME"))
        self.assertEqual(draft.work, ["052-297-8976", "052-297-8977"])
        self.assertEqual(draft.fax, [])
        self.assertEqual(draft.warnings, ["팩스 불명확"])
        self.assertEqual(CardDraft.from_session_value(text), draft)


if __name__ == "__main__":
    unittest.main()
