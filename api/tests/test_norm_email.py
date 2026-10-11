"""_norm_email must strip surrounding whitespace/newlines and lowercase, so the exact-match
login gate can't be broken by a stray newline. Regression for a freelancer whose allow-list
row was stored as "user@x.org\\n" (CSV import) and could not sign in. The DB trigger in
db/schema.sql covers non-app write paths; this locks the app-side guarantee.
"""
import unittest

from reset_service.db import _norm_email


class NormEmailTests(unittest.TestCase):
    def test_strips_trailing_newline(self):
        self.assertEqual(_norm_email("chinmaykiranpatwardhan@ieee.org\n"), "chinmaykiranpatwardhan@ieee.org")

    def test_strips_crlf_tabs_spaces(self):
        self.assertEqual(_norm_email("  User@X.org\r\n\t "), "user@x.org")

    def test_lowercases(self):
        self.assertEqual(_norm_email("Mixed.Case@Example.COM"), "mixed.case@example.com")

    def test_none_safe(self):
        self.assertEqual(_norm_email(None), "")  # type: ignore[arg-type]

    def test_clean_email_unchanged(self):
        self.assertEqual(_norm_email("a@b.com"), "a@b.com")


if __name__ == "__main__":
    unittest.main()
