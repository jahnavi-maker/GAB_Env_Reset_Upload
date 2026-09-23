from __future__ import annotations

import unittest

from materialize.csv_ingest import normalize_persona_key, parse_accounts_csv


CSV = (
    "\ufeffGoogle account,Password,Benchmark persona/profile\r\n"
    "geminiapp.gab.demo.user410@gmail.com,secret410,Student\r\n"
    "geminiapp.gab.demo.user411@gmail.com,secret411,Applied ML and Data Scientist\r\n"
    "geminiapp.gab.demo.user412@gmail.com,secret412,Backend software engineer\r\n"
    "geminiapp.gab.demo.user413@gmail.com,secret413,Data Wizard\r\n"
    "geminiapp.gab.demo.user414@gmail.com,secret414,Educator and instructional designer\r\n"
    "geminiapp.gab.demo.user415@gmail.com,secret415,Indie game designer\r\n"
    "geminiapp.gab.demo.user416@gmail.com,secret416,Legal and Contracts Analyst\r\n"
    "geminiapp.gab.demo.user417@gmail.com,secret417,Life Sciences Researcher\r\n"
    "geminiapp.gab.demo.user418@gmail.com,secret418,Luxury travel advisor\r\n"
    "geminiapp.gab.demo.user419@gmail.com,secret419,Applied ML and data scientist\r\n"
    "geminiapp.gab.demo.user411@gmail.com,dup,Student\r\n"
    "\r\n\r\n\r\n\r\n\r\n\r\n\r\n\r\n"
)


class CsvIngestTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(
            normalize_persona_key("Applied ML and Data Scientist"),
            "applied_ml_and_data_scientist",
        )

    def test_sheet_shape(self):
        parsed = parse_accounts_csv(CSV.encode("utf-8"))
        emails = [a["email"] for a in parsed["accounts"]]
        self.assertEqual(len(emails), 11)
        self.assertNotIn("", emails)
        four11 = [a for a in parsed["accounts"] if a["email"].endswith("user411@gmail.com")]
        self.assertEqual(len(four11), 2)
        self.assertEqual({a["persona_key"] for a in four11}, {"applied_ml_and_data_scientist", "student"})
        self.assertFalse(any("Duplicate" in w for w in parsed["warnings"]))
        ml = [a for a in four11 if a["persona_key"] == "applied_ml_and_data_scientist"][0]
        self.assertEqual(ml["persona_dir"], "Applied_ML_and_data_scientist")
        self.assertEqual(ml["persona_status"], "matched")
        wizard = [a for a in parsed["accounts"] if a["email"].endswith("user413@gmail.com")][0]
        self.assertEqual(wizard["persona_status"], "unmatched")
        self.assertIsNone(wizard["persona_dir"])

    def test_same_email_persona_pair_is_the_duplicate(self):
        raw = (
            "email,role\r\n"
            "geminiapp.gab.demo.user410@gmail.com,Student\r\n"
            "geminiapp.gab.demo.user410@gmail.com,Applied ML and Data Scientist\r\n"
            "geminiapp.gab.demo.user410@gmail.com,Student\r\n"
        ).encode()
        parsed = parse_accounts_csv(raw)
        self.assertEqual(len(parsed["accounts"]), 2)
        self.assertEqual(
            [a["persona_key"] for a in parsed["accounts"]],
            ["student", "applied_ml_and_data_scientist"],
        )
        self.assertTrue(any("Duplicate" in w and "user410" in w for w in parsed["warnings"]))

    def test_password_column_optional(self):
        raw = (
            "email,role\r\n"
            "geminiapp.gab.demo.user410@gmail.com,Student\r\n"
        ).encode()
        parsed = parse_accounts_csv(raw)
        self.assertFalse(parsed["has_passwords"])
        self.assertEqual(parsed["accounts"][0]["persona_dir"], "Student")
        self.assertNotIn("secrets", parsed)

    def test_cp1252_codec_recorded(self):
        raw = b"email,role\r\na@b.com,Student\x92s\r\n"
        parsed = parse_accounts_csv(raw)
        self.assertEqual(len(parsed["accounts"]), 1)
        self.assertTrue(any("cp1252" in w for w in parsed["warnings"]))

    def test_mapping_sheet_prefers_gmail_and_persona_loaded(self):
        raw = (
            "User id,gmail,Password,Persona,Persona loaded,Email\r\n"
            "1,user1@deccanexperts.us,secret,ML,Applied_ML_and_data_scientist,personal@gmail.com\r\n"
            "2,user2@deccanexperts.us,secret,Backend,Backend_software_engineer,other@gmail.com\r\n"
        ).encode()
        parsed = parse_accounts_csv(raw)
        self.assertEqual(
            [a["email"] for a in parsed["accounts"]],
            ["user1@deccanexperts.us", "user2@deccanexperts.us"],
        )
        self.assertEqual(parsed["accounts"][0]["persona_dir"], "Applied_ML_and_data_scientist")
        self.assertEqual(parsed["accounts"][1]["persona_dir"], "Backend_software_engineer")
        self.assertEqual(parsed["accounts"][0]["persona_status"], "matched")

    def test_email_id_and_personas_headers(self):
        raw = (
            "email-id,Personas\r\n"
            "user207@deccanexperts.us,Student\r\n"
            "user222@deccanexperts.us,Startup Founder\r\n"
        ).encode()
        parsed = parse_accounts_csv(raw)
        self.assertEqual(
            [a["email"] for a in parsed["accounts"]],
            ["user207@deccanexperts.us", "user222@deccanexperts.us"],
        )
        self.assertEqual(parsed["accounts"][0]["persona_key"], "student")
        self.assertEqual(parsed["accounts"][1]["persona_key"], "startup_founder")

    def test_non_email_row_skipped(self):
        raw = (
            "email,role\r\n"
            "total: 10 accounts,Student\r\n"
            "geminiapp.gab.demo.user410@gmail.com,Student\r\n"
        ).encode()
        parsed = parse_accounts_csv(raw)
        self.assertEqual(len(parsed["accounts"]), 1)
        self.assertTrue(any("non-email" in w for w in parsed["warnings"]))


if __name__ == "__main__":
    unittest.main()
