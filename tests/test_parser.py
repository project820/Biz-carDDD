import os
import unittest
from pathlib import Path
from unittest.mock import patch

from bizcard_intake import parser
from bizcard_intake.draft import CardDraft
from bizcard_intake.vision_ocr import OcrLine, OcrResult


class Completed:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


class ParserTest(unittest.TestCase):
    def test_codex_command_uses_lean_flags(self):
        with patch.object(parser, "find_binary", lambda name: "/bin/codex"), patch.dict(os.environ, {"BIZCARD_CODEX_MODEL": "m"}):
            command = parser.codex_command(True, Path("/img.jpg"), "/tmp/empty")
        for flag in ("--ignore-user-config", "--ignore-rules", "--ephemeral", "--image", "-C"):
            self.assertIn(flag, command)
        self.assertEqual(command[command.index("-m") + 1], "m")
        self.assertEqual(command[-1], "-")
        self.assertNotIn("--output-schema", command)  # triples token usage on codex

    def test_claude_command_uses_json_schema_and_read_tool_for_images(self):
        with patch.object(parser, "find_binary", lambda name: "/bin/claude"):
            with_image = parser.claude_command(Path("/img.jpg"))
            text_only = parser.claude_command(None)
        self.assertIn("--json-schema", with_image)
        self.assertEqual(with_image[with_image.index("--allowedTools") + 1], "Read")
        self.assertIn("--tools", text_only)

    def test_backend_auto_prefers_codex_when_installed(self):
        with patch.dict(os.environ, {"BIZCARD_PARSER": "auto"}):
            with patch.object(parser, "find_binary", lambda name: "/bin/codex" if name == "codex" else None):
                self.assertEqual(parser.backend_name(), "codex")
            with patch.object(parser, "find_binary", lambda name: None):
                self.assertEqual(parser.backend_name(), "claude")
        with patch.dict(os.environ, {"BIZCARD_PARSER": "claude"}):
            self.assertEqual(parser.backend_name(), "claude")

    def test_parse_card_extracts_json_from_noisy_codex_output(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs["input"]))
            return Completed('tokens used\n5,000\n{"name": "홍길동", "title": "", "company": "ACME", "mobile": ["010-1234-5678"], "work": [], "fax": [], "email": [], "website": [], "address": [], "notes": "", "warnings": []}\n')

        ocr = OcrResult((OcrLine("홍길동", 0.9), OcrLine("010-1234-5678", 0.8)))
        with patch.dict(os.environ, {"BIZCARD_PARSER": "codex"}), patch.object(parser, "find_binary", lambda name: "/bin/codex"), patch.object(parser.subprocess, "run", fake_run):
            draft = parser.parse_card(Path("/img.jpg"), ocr, "front")

        self.assertEqual(draft.name_field, "홍길동 대표")
        self.assertIn("010-1234-5678", calls[0][1])  # OCR text is passed to the model
        self.assertIn("--image", calls[0][0])

    def test_parse_card_falls_back_to_warning_when_backend_fails(self):
        with patch.dict(os.environ, {"BIZCARD_PARSER": "codex"}), patch.object(parser, "find_binary", lambda name: "/bin/codex"), patch.object(
            parser.subprocess, "run", lambda *a, **k: Completed("", 1, "boom")
        ):
            draft = parser.parse_card(Path("/img.jpg"), OcrResult(()), "front")
        self.assertFalse(draft.is_savable)
        self.assertTrue(any("자동 파싱 실패" in w for w in draft.warnings))

    def test_revise_draft_returns_none_on_failure_and_draft_on_success(self):
        draft = CardDraft.from_dict({"name": "홍길동", "work": ["052-297-8976~7"]})
        with patch.object(parser, "run_backend", lambda prompt, image_path=None: None):
            self.assertIsNone(parser.revise_draft(draft, "두 회선"))
        with patch.object(parser, "run_backend", lambda prompt, image_path=None: {"name": "홍길동", "work": ["052-297-8976", "052-297-8977"]}):
            self.assertEqual(parser.revise_draft(draft, "두 회선").work, ["052-297-8976", "052-297-8977"])

    def test_extract_json(self):
        self.assertEqual(parser.extract_json('noise {"a": 1} tail'), {"a": 1})
        self.assertIsNone(parser.extract_json("no json"))
        self.assertIsNone(parser.extract_json("[1, 2]"))


if __name__ == "__main__":
    unittest.main()
