import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bizcard_intake import pipeline
from bizcard_intake.contacts import ContactSaveError


def session_with_images(tmp: Path) -> dict:
    session = {"status": "drafted", "draft": {"name": "홍길동", "mobile": ["010-1234-5678"]}}
    for key in pipeline.IMAGE_PATH_KEYS:
        path = tmp / f"{key}.jpg"
        path.write_bytes(b"x")
        session[key] = str(path)
    return session


class PipelineTest(unittest.TestCase):
    def test_approve_success_archives_session_and_deletes_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            session = session_with_images(tmp)
            with patch.object(pipeline, "SESSION_DIR", tmp / "s"), patch.object(pipeline, "SESSION_ARCHIVE_DIR", tmp / "a"), patch.object(
                pipeline, "save_contact", lambda draft, image: f"created: {draft.name_field}"
            ):
                result = pipeline.approve("42", session)
                self.assertIsNone(pipeline.load_session("42"))

            self.assertEqual(result, "저장 완료\ncreated: 홍길동 대표")
            self.assertEqual(len(list((tmp / "a").glob("42-*.json"))), 1)
            for key in pipeline.IMAGE_PATH_KEYS:
                self.assertFalse(Path(session[key]).exists(), key)

    def test_approve_failure_queues_pending_contact_and_keeps_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            session = session_with_images(tmp)
            with patch.object(pipeline, "SESSION_DIR", tmp / "s"), patch.object(pipeline, "PENDING_CONTACT_DIR", tmp / "p"), patch.object(
                pipeline, "save_contact", side_effect=ContactSaveError("auth expired")
            ):
                result = pipeline.approve("42", session)

            self.assertEqual(result, "저장 실패: auth expired")
            pending = json.loads(next((tmp / "p").glob("42-*.json")).read_text(encoding="utf-8"))
            self.assertEqual(pending["pending_reason"], "auth expired")
            self.assertTrue(Path(session["front_image_path"]).exists())

    def test_revise_updates_draft_and_front_draft(self):
        session = {"draft": {"name": "홍길동", "work": ["052-297-8976~7"]}, "front_draft": {"name": "홍길동"}}
        with patch.object(pipeline, "revise_draft", lambda draft, reply: None):
            self.assertFalse(pipeline.revise(session, "x"))
        with patch.object(pipeline, "revise_draft", lambda draft, reply: pipeline.CardDraft.from_dict({"name": "홍길동", "work": ["052-297-8976", "052-297-8977"]})):
            self.assertTrue(pipeline.revise(session, "두 회선"))
        self.assertEqual(session["draft"]["work"], ["052-297-8976", "052-297-8977"])
        self.assertEqual(session["front_draft"], session["draft"])
        self.assertIn("- 회사전화: 052-297-8976, 052-297-8977", pipeline.draft_text(session))

    def test_retry_pending_moves_saved_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pending = tmp / "p"
            pending.mkdir()
            (pending / "a.json").write_text(json.dumps({"draft": {"name": "홍길동", "mobile": ["010-1"]}}), encoding="utf-8")
            (pending / "b.json").write_text(json.dumps({"draft": {"company": "no name", "mobile": ["010-2"]}}), encoding="utf-8")
            with patch.object(pipeline, "PENDING_CONTACT_DIR", pending), patch.object(
                pipeline, "save_contact", lambda draft, image: "created" if draft.is_savable else (_ for _ in ()).throw(ContactSaveError("no name"))
            ):
                saved, failures = pipeline.retry_pending()
            self.assertEqual(saved, 1)
            self.assertEqual(len(failures), 1)
            self.assertEqual(len(list((pending / "saved").glob("*-a.json"))), 1)
            self.assertTrue((pending / "b.json").exists())


if __name__ == "__main__":
    unittest.main()
