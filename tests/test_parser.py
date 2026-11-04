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
        self.assertEqual((body["model"], body["reasoning"]["effort"]), ("gpt-6-luna", "low"))
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


class CrossCheckTest(unittest.TestCase):
    OCR = "새봄 엔지니어링\n대표이사\n김철수\nMobile. 010-5555-0104\nTel. 031-555-0105\nTel. +84-221-555-0108 Mobile. +84-86-5555-0109\nTEL. 052-555-0126~7"

    def test_phones_are_unioned_and_verified_against_ocr(self):
        luna = {"name": "김철수", "company": "새봄 엔지니어링", "mobile": ["010-5555-0104", "+84-86-5555-0109"], "work": ["031-555-0105", "02-5555-0106"], "fax": []}
        haiku = {"name": "김철수", "company": "새봄엔지니어링", "mobile": ["010-5555-0104"], "work": ["031-555-0105", "+84-221-555-0108", "052-555-0126", "052-555-0127"], "fax": []}
        merged = parser.crosscheck_merge(luna, haiku, self.OCR)
        self.assertEqual(merged["mobile"], ["010-5555-0104", "+84-86-5555-0109"])
        self.assertEqual(merged["work"], ["031-555-0105", "+84-221-555-0108", "052-555-0126", "052-555-0127"])
        self.assertTrue(any("02-5555-0106" in w for w in merged["warnings"]))
        self.assertFalse(any("엇갈림" in w for w in merged["warnings"]))

    def test_same_number_as_tel_and_fax_keeps_both(self):
        reading = {"name": "박영수", "work": ["052-555-0126", "052-555-0127"], "fax": ["052-555-0127"], "mobile": ["010-5555-0107"]}
        ocr = "TEL. 052)555-0126~7\nFAX. 052)555-0127\n010-5555-0107"
        merged = parser.crosscheck_merge(dict(reading), dict(reading), ocr)
        self.assertEqual(merged["work"], ["052-555-0126", "052-555-0127"])
        self.assertEqual(merged["fax"], ["052-555-0127"])

    def test_conflicting_company_is_flagged_and_ocr_match_preferred(self):
        merged = parser.crosscheck_merge({"name": "김철수", "company": "세봄 엔지니어링"}, {"name": "김철수", "company": "새봄 엔지니어링"}, self.OCR)
        self.assertEqual(merged["company"], "새봄 엔지니어링")
        self.assertTrue(any("회사명 판독이 엇갈림" in w for w in merged["warnings"]))

    def test_one_api_down_uses_the_other(self):
        with patch.object(parser, "run_openai", side_effect=RuntimeError("down")), patch.object(parser, "run_anthropic", lambda p, i: {"name": "홍길동"}):
            self.assertEqual(parser.run_crosscheck("p", Path("/x.jpg"), ""), {"name": "홍길동"})

    def test_enabled_only_with_both_keys(self):
        with patch.dict(os.environ, {"BIZCARD_PARSER": "openai", "ANTHROPIC_API_KEY": "k", "BIZCARD_CROSSCHECK": "auto"}):
            self.assertTrue(parser.crosscheck_enabled())
        with patch.dict(os.environ, {"BIZCARD_PARSER": "openai", "ANTHROPIC_API_KEY": "", "BIZCARD_CROSSCHECK": "auto"}):
            self.assertFalse(parser.crosscheck_enabled())
        with patch.dict(os.environ, {"BIZCARD_PARSER": "openai", "ANTHROPIC_API_KEY": "k", "BIZCARD_CROSSCHECK": "off"}):
            self.assertFalse(parser.crosscheck_enabled())

    def test_run_anthropic_reads_forced_tool_input(self):
        import io
        import json as _json

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        seen = {}

        def fake_urlopen(request, timeout):
            seen["key"] = request.get_header("X-api-key")
            seen["body"] = _json.loads(request.data)
            return Response(_json.dumps({"model": "claude-haiku-5-5", "usage": {}, "content": [{"type": "tool_use", "name": "business_card", "input": {"name": "홍길동"}}]}).encode())

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test"}), patch.object(parser.urllib.request, "urlopen", fake_urlopen):
            self.assertEqual(parser.run_anthropic("p", None), {"name": "홍길동"})
        self.assertEqual(seen["key"], "sk-ant-test")
        self.assertEqual(seen["body"]["tool_choice"], {"type": "tool", "name": "business_card"})


if __name__ == "__main__":
    unittest.main()
