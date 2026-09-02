import unittest
from unittest.mock import patch

from bizcard_intake import doctor


class DoctorTest(unittest.TestCase):
    def test_gws_check_states(self):
        with patch.object(doctor.shutil, "which", lambda name: None):
            self.assertFalse(doctor.check_gws().ok)
        with patch.object(doctor.shutil, "which", lambda name: "/bin/gws"):
            with patch.object(doctor, "gws_auth_status", lambda: {"token_valid": False, "token_error": "invalid_rapt"}):
                check = doctor.check_gws()
                self.assertFalse(check.ok)
                self.assertIn("invalid_rapt", check.detail)
                self.assertIn("gws auth login", check.fix)
            with patch.object(doctor, "gws_auth_status", lambda: {"token_valid": True, "scopes": ["openid"]}):
                self.assertIn("contacts", doctor.check_gws().detail)
            with patch.object(doctor, "gws_auth_status", lambda: {"token_valid": True, "scopes": [doctor.CONTACTS_SCOPE], "user": "me@x"}):
                self.assertTrue(doctor.check_gws().ok)

    def test_parser_check_reports_missing_cli(self):
        with patch.object(doctor.parser, "backend_name", lambda: "codex"), patch.object(doctor.parser, "find_binary", lambda name: None):
            check = doctor.check_parser()
        self.assertFalse(check.ok)
        self.assertIn("codex", check.detail)

    def test_format_report_lists_fixes_for_failures(self):
        report = doctor.format_report([doctor.Check("a", True, "fine"), doctor.Check("b", False, "broken", "do this")])
        self.assertIn("[OK] a: fine", report)
        self.assertIn("[FAIL] b: broken", report)
        self.assertIn("해결: do this", report)


if __name__ == "__main__":
    unittest.main()
