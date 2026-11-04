import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bizcard_intake import cards, pipeline
from bizcard_intake.contacts import ContactSaveError, SaveResult
from bizcard_intake.draft import CardDraft


class CardTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.patches = [
            patch.object(cards, "CARD_DIR", self.tmp / "cards"),
            patch.object(pipeline, "SESSION_DIR", self.tmp / "sessions"),
            patch.object(pipeline, "SESSION_ARCHIVE_DIR", self.tmp / "archive"),
            patch.object(pipeline, "PENDING_CONTACT_DIR", self.tmp / "pending"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self._tmp.cleanup()

    def review_card(self, draft=None, with_images=True):
        card = cards.new_card(42)
        if with_images:
            for key in ("front", "profile", "ocr"):
                path = self.tmp / f"{card['id']}_{key}.jpg"
                path.write_bytes(b"x")
                card["images"][key] = str(path)
        pipeline.set_draft(card, CardDraft.from_dict(draft or {"name": "홍길동", "title": "이사", "company": "ACME", "mobile": ["010-1234-5678"]}), "test")
        card["status"] = "review"
        cards.save_card(card)
        return card


def ok_save(draft, image, resource_name=None, checkpoint=None):
    if checkpoint:
        checkpoint("people/c1", "created")
    return SaveResult("created", "people/c1", "uploaded", [], draft.display_name)


class PipelineTest(CardTestCase):
    def test_approve_saves_cleans_images_and_archives(self):
        card = self.review_card()
        with patch.object(pipeline, "save_contact", ok_save):
            outcome = pipeline.approve(card, card["revision"])
        self.assertTrue(outcome["ok"])
        stored = cards.load_card(card["id"])
        self.assertEqual((stored["status"], stored["resource_name"]), ("saved", "people/c1"))
        self.assertFalse(any(stored["images"].get(k) for k in ("front", "profile", "ocr")))
        self.assertTrue((self.tmp / "archive" / f"card-{card['id']}.json").exists())

    def test_stale_revision_is_refused(self):
        card = self.review_card()
        with patch.object(pipeline, "save_contact", side_effect=AssertionError("must not save")):
            outcome = pipeline.approve(card, card["revision"] - 1)
        self.assertTrue(outcome["stale"])

    def test_invalid_draft_is_not_saved_and_explains_what_is_missing(self):
        card = self.review_card({"company": "ACME", "mobile": ["010-1"]})
        with patch.object(pipeline, "save_contact", side_effect=AssertionError("must not save")):
            outcome = pipeline.approve(card)
        self.assertIn("이름", outcome["message"])

    def test_transient_failure_keeps_card_and_retry_reuses_resource_name(self):
        card = self.review_card()
        calls = []

        def failing(draft, image, resource_name=None, checkpoint=None):
            calls.append(resource_name)
            checkpoint("people/c9", "created")
            raise ContactSaveError("Google 서버 일시 오류(502).", kind="transient", retryable=True)

        with patch.object(pipeline, "save_contact", failing):
            outcome = pipeline.approve(card)
        self.assertEqual(outcome["kind"], "transient")
        stored = cards.load_card(card["id"])
        self.assertEqual(stored["status"], "save_failed")
        self.assertTrue(Path(stored["images"]["profile"]).exists())

        seen = []
        with patch.object(pipeline, "save_contact", lambda d, i, resource_name=None, checkpoint=None: seen.append(resource_name) or SaveResult("updated", resource_name, "uploaded", [], "")):
            saved, failures = pipeline.retry_due(force=True)
        self.assertEqual((saved, failures, seen), (1, [], ["people/c9"]))
        self.assertEqual(cards.load_card(card["id"])["status"], "saved")

    def test_retry_never_saves_unapproved_or_edited_cards(self):
        card = self.review_card()
        with patch.object(pipeline, "save_contact", side_effect=ContactSaveError("x", kind="auth")):
            pipeline.approve(card)
        stored = cards.load_card(card["id"])
        pipeline.edit(stored, "직함: 상무")  # edited after approval -> needs a new approval
        self.review_card()  # never approved
        with patch.object(pipeline, "save_contact", side_effect=AssertionError("must not save")):
            self.assertEqual(pipeline.retry_due(force=True), (0, []))
        self.assertEqual(cards.load_card(card["id"])["status"], "review")

    def test_backoff_delays_automatic_retry(self):
        card = self.review_card()
        with patch.object(pipeline, "save_contact", side_effect=ContactSaveError("x", kind="transient", retryable=True)):
            pipeline.approve(card)
        self.assertGreater(cards.load_card(card["id"])["next_retry_at"], time.time())
        with patch.object(pipeline, "save_contact", side_effect=AssertionError("too early")):
            self.assertEqual(pipeline.retry_due(), (0, []))

    def test_manual_edit_needs_no_ai_and_survives_recognition_retry(self):
        card = self.review_card()
        with patch.object(pipeline, "revise_draft", side_effect=AssertionError("no AI for field edits")):
            ok, _ = pipeline.edit(card, "직함: 영업팀장\n회사: 누리네트웍스")
        self.assertTrue(ok)
        self.assertEqual(pipeline.current_draft(card).display_name, "홍길동 영업팀장 누리네트웍스")
        machine = CardDraft.from_dict({"name": "홍길동", "title": "사원", "company": "OTHER", "mobile": ["010-9"]})
        with patch.object(pipeline, "scan_front", lambda path: {"images": {"front": str(path)}, "rectifier": "t", "draft": machine}):
            pipeline.recognize_front(card)
        draft = pipeline.current_draft(card)
        self.assertEqual((draft.short_title, draft.company, draft.mobile), ("영업팀장", "누리네트웍스", ["010-9"]))

    def test_recognition_crash_still_gives_editable_card(self):
        card = cards.new_card(42)
        with patch.object(pipeline, "scan_front", side_effect=RuntimeError("codex down")):
            pipeline.recognize_front(card, self.tmp / "front.jpg")
        self.assertEqual(card["status"], "review")
        self.assertIn("자동 인식 실패", " ".join(pipeline.current_draft(card).warnings))
        ok, _ = pipeline.edit(card, "이름: 이영희\n휴대전화: 010-5555-0103")
        self.assertTrue(ok and pipeline.current_draft(card).is_savable)

    def test_photo_failure_still_counts_as_saved_and_photo_is_retried(self):
        card = self.review_card()
        with patch.object(pipeline, "save_contact", lambda d, i, resource_name=None, checkpoint=None: SaveResult("created", "people/c1", "failed", ["사진 업로드 실패"], "")):
            self.assertTrue(pipeline.approve(card)["ok"])
        stored = cards.load_card(card["id"])
        self.assertTrue(Path(stored["images"]["profile"]).exists())
        uploads = []
        with patch.object(pipeline, "upload_photo", lambda rn, path: uploads.append(rn)):
            pipeline.retry_due(force=True)
        self.assertEqual(uploads, ["people/c1"])
        self.assertEqual(cards.load_card(card["id"])["photo_status"], "uploaded")


class LegacyMigrationTest(CardTestCase):
    def write(self, folder, name, data):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_migrates_pending_and_sessions(self):
        draft = {"name": "홍길동", "title": "이사", "mobile": ["010-1"]}
        # already saved later: the stuck "image missing" entry from production
        self.write(self.tmp / "pending", "42-1-file_60.json", {"status": "approved", "front_image_path": "f60.jpg", "draft": draft, "pending_reason": "<html>502"})
        self.write(self.tmp / "archive", "42-x.json", {"front_image_path": "f60.jpg", "save_output": "created: x"})
        # approved but failed (auth) + duplicate active session of the same card
        self.write(self.tmp / "pending", "42-2-file_61.json", {"status": "approved", "front_image_path": "f61.jpg", "draft": draft, "pending_reason": "No credentials provided"})
        self.write(self.tmp / "sessions", "42.json", {"status": "approved", "front_image_path": "f61.jpg", "draft": draft})
        # superseded by a newer photo: must stay in review
        self.write(self.tmp / "pending", "42-3-file_62.json", {"status": "drafted", "front_image_path": "f62.jpg", "draft": draft, "pending_reason": "superseded_by_new_front_photo"})

        pipeline.migrate_legacy()
        open_cards = cards.list_cards(statuses=cards.OPEN_STATUSES)
        self.assertEqual(sorted(c["status"] for c in open_cards), ["review", "save_failed"])
        failed = next(c for c in open_cards if c["status"] == "save_failed")
        self.assertEqual((failed["error_kind"], failed["approved_revision"]), ("auth", failed["revision"]))
        self.assertEqual(list((self.tmp / "pending").glob("*.json")), [])
        self.assertEqual(pipeline.migrate_legacy(), [])  # idempotent


if __name__ == "__main__":
    unittest.main()
