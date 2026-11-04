import unittest

from bizcard_intake.draft import MISSING_TITLE_WARNING, CardDraft, apply_edits, clean_value, expand_phone_range, parse_edits, preserve_locked


class DraftTest(unittest.TestCase):
    def test_missing_title_is_flagged_not_invented(self):
        draft = CardDraft.from_dict({"name": "홍길동", "company": "ACME", "mobile": ["010-1234-5678"]})
        self.assertEqual(draft.title, "")
        self.assertEqual(draft.display_name, "홍길동 ACME")
        self.assertIn(MISSING_TITLE_WARNING, draft.warnings)
        self.assertTrue(draft.is_savable)
        self.assertNotIn(MISSING_TITLE_WARNING, apply_edits(draft, {"display_title": "대표"}).warnings)

    def test_display_name_is_name_short_title_company(self):
        cases = [
            ({"name": "최민준", "title": "차장", "company": "(주) 다솔이엔지"}, "최민준 차장 (주)다솔이엔지"),
            ({"name": "정호준", "title": "영업팀장/컨설턴트 (Sales Executive)", "company": "누리네트웍스"}, "정호준 영업팀장 누리네트웍스"),
            ({"name": "윤대현", "title": "과장", "display_title": "과장", "company": "한빛에어테크"}, "윤대현 과장 한빛에어테크"),
            ({"name": "Min-Jun Park", "title": "Sales Consultant (선임)", "company": "가람모터스(주)"}, "Min-Jun Park Sales Consultant 가람모터스(주)"),
        ]
        for data, expected in cases:
            self.assertEqual(CardDraft.from_dict(data).display_name, expected)

    def test_parse_edits_reads_labelled_lines_and_reports_the_rest(self):
        edits, unknown = parse_edits("이름 : 윤대현\n- 직함: 과장\n휴대폰: 010-1, 010-2\n팩스: -\n이건 그냥 문장")
        self.assertEqual(edits, {"name": "윤대현", "display_title": "과장", "mobile": "010-1, 010-2", "fax": "-"})
        self.assertEqual(unknown, ["이건 그냥 문장"])
        draft = apply_edits(CardDraft.from_dict({"name": "김", "fax": ["1"]}), edits)
        self.assertEqual((draft.mobile, draft.fax), (["010-1", "010-2"], []))
        self.assertEqual(draft.locked, ["name", "display_title", "mobile", "fax"])

    def test_preserve_locked_keeps_hand_typed_fields(self):
        old = apply_edits(CardDraft.from_dict({"name": "김", "company": "A"}), {"company": "한빛에어테크"})
        new = CardDraft.from_dict({"name": "윤대현", "company": "OCR회사", "mobile": ["010-1"]})
        kept = preserve_locked(new, old)
        self.assertEqual((kept.name, kept.company, kept.mobile, kept.locked), ("윤대현", "한빛에어테크", ["010-1"], ["company"]))

    def test_phone_range_notation_expands_to_two_lines(self):
        self.assertEqual(expand_phone_range("052-555-0126~7"), ["052-555-0126", "052-555-0127"])
        self.assertEqual(expand_phone_range("02-123-4567~9"), ["02-123-4567", "02-123-4569"])
        self.assertEqual(expand_phone_range("052-555-0126~0127"), ["052-555-0126", "052-555-0127"])
        self.assertEqual(expand_phone_range("010-1234-5678"), ["010-1234-5678"])
        draft = CardDraft.from_dict({"name": "홍길동", "work": "052-555-0126~7"})
        self.assertEqual(draft.work, ["052-555-0126", "052-555-0127"])

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

    def test_to_text_shows_display_name_and_editable_labels(self):
        draft = CardDraft.from_dict({"name": "홍길동", "title": "영업팀장/컨설턴트", "company": "ACME", "mobile": ["010-1234-5678"], "warnings": ["팩스 불명확"]})
        text = draft.to_text()
        self.assertIn("표시 이름: 홍길동 영업팀장 ACME", text)
        self.assertIn("- 직함: 영업팀장\n- 직함원문: 영업팀장/컨설턴트\n- 회사: ACME", text)
        self.assertIn("- 확인 필요: 팩스 불명확", text)
        self.assertEqual(CardDraft.from_json(draft.to_json()), draft)
        edits, unknown = parse_edits("\n".join(line for line in text.splitlines() if line.startswith("- ") and "확인 필요" not in line))
        self.assertEqual(unknown, [])

    def test_from_text_parses_legacy_draft(self):
        text = "명함 입력 초안\n- 이름 필드: 홍길동 이사\n- 성씨 필드: ACME\n- 휴대전화: 010-1234-5678\n- 회사전화: 052-555-0126~7\n- 팩스: (확인 필요)\n- 이메일: a@b.com\n- 웹사이트: \n- 주소: 서울시 강남구\n- 메모: 메모\n- 검증 경고: 팩스 불명확"
        draft = CardDraft.from_text(text)
        self.assertEqual((draft.name, draft.title, draft.company), ("홍길동", "이사", "ACME"))
        self.assertEqual(draft.work, ["052-555-0126", "052-555-0127"])
        self.assertEqual(draft.fax, [])
        self.assertEqual(draft.warnings, ["팩스 불명확"])
        self.assertEqual(CardDraft.from_session_value(text), draft)


if __name__ == "__main__":
    unittest.main()
