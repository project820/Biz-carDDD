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
        with patch.dict(os.environ, {"BIZCARD_PARSER": "auto"}), patch.object(parser, "openai_api_key", lambda: ""):
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

        self.assertEqual(draft.display_name, "홍길동 ACME")
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
        draft = CardDraft.from_dict({"name": "홍길동", "work": ["052-555-0126~7"]})
        with patch.object(parser, "run_backend", lambda prompt, image_path=None: None):
            self.assertIsNone(parser.revise_draft(draft, "두 회선"))
        with patch.object(parser, "run_backend", lambda prompt, image_path=None: {"name": "홍길동", "work": ["052-555-0126", "052-555-0127"]}):
            self.assertEqual(parser.revise_draft(draft, "두 회선").work, ["052-555-0126", "052-555-0127"])

    def test_extract_json(self):
        self.assertEqual(parser.extract_json('noise {"a": 1} tail'), {"a": 1})
        self.assertIsNone(parser.extract_json("no json"))
        self.assertIsNone(parser.extract_json("[1, 2]"))


class OpenAIBackendTest(unittest.TestCase):
    def test_auto_prefers_openai_when_a_key_is_stored(self):
        with patch.dict(os.environ, {"BIZCARD_PARSER": "auto"}), patch.object(parser, "openai_api_key", lambda: "sk-test"):
            self.assertEqual(parser.backend_name(), "openai")

    def test_key_is_read_from_env_file_without_sourcing(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as handle:
            handle.write("# comment\nexport OPENAI_API_KEY='sk-file'\n")
        env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
        env["BIZCARD_OPENAI_ENV_FILE"] = handle.name
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(parser.openai_api_key(), "sk-file")
            self.assertEqual(parser.openai_key_source(), handle.name)
        os.unlink(handle.name)

    def test_request_uses_luna_medium_strict_schema_and_inline_image(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as handle:
            handle.write(b"jpg")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BIZCARD_OPENAI_MODEL", None)
            os.environ.pop("BIZCARD_REASONING_EFFORT", None)
            body = parser.openai_request_body("prompt", Path(handle.name))
        os.unlink(handle.name)
        self.assertEqual((body["model"], body["reasoning"]["effort"]), ("gpt-6-luna", "medium"))
        fmt = body["text"]["format"]
        self.assertTrue(fmt["strict"])
        self.assertEqual(set(fmt["schema"]["required"]), set(fmt["schema"]["properties"]))
        image = body["input"][0]["content"][1]
        self.assertTrue(image["image_url"].startswith("data:image/jpeg;base64,"))
        self.assertFalse(body["store"])

    def test_api_failure_falls_back_to_codex(self):
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command[0])
            return Completed('{"name": "홍길동", "mobile": ["010-1"]}')

        with patch.dict(os.environ, {"BIZCARD_PARSER": "openai"}), patch.object(parser, "run_openai", side_effect=RuntimeError("OpenAI HTTP 429")), patch.object(
            parser, "find_binary", lambda name: "/bin/codex" if name == "codex" else None
        ), patch.object(parser.subprocess, "run", fake_run):
            data = parser.run_backend("p")
        self.assertEqual(data["name"], "홍길동")
        self.assertEqual(calls, ["/bin/codex"])

    def test_run_openai_parses_output_text(self):
        import io
        import json as _json

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        payload = {"status": "completed", "model": "gpt-6-luna", "usage": {"input_tokens": 1, "output_tokens": 2},
                   "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"name": "홍길동"}'}]}]}
        seen = {}

        def fake_urlopen(request, timeout):
            seen["auth"] = request.get_header("Authorization")
            return Response(_json.dumps(payload).encode())

        with patch.object(parser, "openai_api_key", lambda: "sk-test"), patch.object(parser.urllib.request, "urlopen", fake_urlopen):
            self.assertEqual(parser.run_openai("p", None), {"name": "홍길동"})
        self.assertEqual(seen["auth"], "Bearer sk-test")


if __name__ == "__main__":
    unittest.main()
