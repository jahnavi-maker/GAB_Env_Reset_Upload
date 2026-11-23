"""Wave 2 persona seeding: discovery, the seeding context, plan/verify, mode routing,
credentials and the extended wipe. No network: Google clients are faked."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PERSONA_EMAIL = "test.person.chi.0@gmail.com"
ACCOUNT = "geminiapp.gab.demo.user9999@gmail.com"


def _world() -> dict:
    bc = {
        "GMAIL": {"mock_context": {"gmail_mock_context": {"emails": [
            {"email_id": "t:m1", "thread_id": "t:th1", "subject": "Hi", "sender": "a@example.com",
             "recipients": [PERSONA_EMAIL], "body": f"Write to {PERSONA_EMAIL}", "timestamp_seconds": "1784125800",
             "labels": ["INBOX", "UNREAD"]},
            {"email_id": "t:m2", "thread_id": "t:th1", "subject": "Re: Hi", "sender": PERSONA_EMAIL,
             "recipients": ["a@example.com"], "body": "ok", "timestamp_seconds": "1784129400", "labels": ["SENT"]},
        ]}}},
        "CALENDAR": {
            "calendar_events_info": {"events": [
                {"id": "t:e1", "summary": "Visit", "calendar_id": "cal_primary",
                 "start": {"date_time": {"time_ms": "1784125800000"}, "time_zone": "America/Chicago"},
                 "end": {"date_time": {"time_ms": "1784129400000"}, "time_zone": "America/Chicago"},
                 "recurrence": ["RRULE:FREQ=WEEKLY"], "attendees": [PERSONA_EMAIL]},
                {"id": "t:e2", "summary": "No start", "calendar_id": "cal_family"},
            ]},
            "calendars_info": {"calendars": [{"id": "t:cal_primary", "summary": "Test", "is_primary": "True"}]},
        },
        "DRIVE": {"mock_context": {"drive_mock_context": {
            "folders": [{"folder_id": "t:f1", "name": "Health", "parent_folder_id": ""}],
            "files": [
                {"file_id": "t:d1", "name": "Labs.pdf", "mime_type": "application/pdf",
                 "parent_folder_id": "t:f1", "content_summary": "PSA 0.2"},
                {"file_id": "t:d2", "name": "photo.jpg", "mime_type": "image/jpeg", "content_summary": "a photo"},
            ]}}},
        "CONTACTS": {"contact_lookup_result": {"contacts": [
            {"contact_id": "c1", "name": "Evelyn", "endpoints": [{"value": "e@example.com", "label": "email"}]}]}},
        "TASKS": {"tasks": [{"task_id": "k1", "task_properties": {"title": "Do it", "completed": "False"}}]},
        "KEEP": {"mock_context": {"keep_mock_context": {"lists": [{"title": "Groceries", "items": ["eggs"]}],
                                                        "notes": [{"title": "Note", "content": "x"}]}}},
        "GOOGLE_CHAT": {"mock_context": {"chat_mock_context": {"items": [
            {"chat_messages": [{"text": "hi"}, {"text": "yo"}]}]}}},
    }
    return {
        "persona": {"id": "test_person", "persona_email": PERSONA_EMAIL,
                    "profile_doc": "# Test Person — The Tester\n**Home Address:** 1 Main St, Chicago (Loop)\n"},
        "bard_contexts": {k: json.dumps(v) for k, v in bc.items()},
        "descriptive_contexts": {"DESCRIPTIVE_CONTEXT": json.dumps({"city": "Chicago", "timezone": "America/Chicago"})},
    }


class _Wave2Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "wave2"
        pdir = root / "test_person" / "turn_1"
        pdir.mkdir(parents=True)
        (pdir / "turn_1_world.json").write_text(json.dumps(_world()))
        (root / "test_person" / "manifest.json").write_text(json.dumps({"persona_id": "test_person"}))
        (root / "not_a_persona").mkdir()
        self.env = patch.dict(os.environ, {"GAB_WAVE2_ROOT": str(root),
                                           "GAB_WAVE2_STATE_DIR": str(Path(self.tmp.name) / "state")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()


class PersonaTests(_Wave2Env):
    def test_disabled_without_root(self):
        from wave2 import personas
        with patch.dict(os.environ, {"GAB_WAVE2_ROOT": ""}):
            self.assertFalse(personas.enabled())
            self.assertEqual(personas.persona_folders(), [])
            self.assertIsNone(personas.match("Test Person"))

    def test_lists_only_folders_with_a_world(self):
        from wave2 import personas
        self.assertEqual(personas.persona_folders(), ["test_person"])

    def test_match_is_loose(self):
        from wave2 import personas
        for name in ("test_person", "Test Person", "Dr. Test Person", "Chief Test Person : The Tester",
                     "TEST-PERSON"):
            self.assertEqual(personas.match(name), "test_person", name)
        for name in ("", None, "Student", "Test"):
            self.assertIsNone(personas.match(name), name)

    def test_normalize_keeps_hyphenated_names_distinct(self):
        from wave2.personas import normalize
        self.assertEqual(normalize("Dr. Claire Sterling-Vance : The Secondary..."), "claire_sterling_vance")
        self.assertEqual(normalize("Claire Ferrante-Ross"), "claire_ferrante_ross")
        self.assertEqual(normalize("Chief Joseph Sholotan"), "joseph_sholotan")

    def test_ctx_fields_and_email_rewrite(self):
        from wave2.personas import WorldCtx
        c = WorldCtx("test_person", ACCOUNT.upper())
        self.assertEqual(c.account, ACCOUNT)
        self.assertEqual(c.persona_email, PERSONA_EMAIL)
        self.assertEqual(c.tz_name, "America/Chicago")
        self.assertEqual(c.persona_name, "Test Person")
        self.assertEqual(c.home_address(), "1 Main St, Chicago")
        self.assertEqual(c.rw(f"mail {PERSONA_EMAIL.upper()} now"), f"mail {ACCOUNT} now")
        self.assertIn(ACCOUNT.replace("@", "_").replace(".", "_"), c.persona)  # per-account state key
        self.assertIn("GMAIL", c.bc)

    def test_contacts_in_the_alternate_format_are_normalized(self):
        from wave2.personas import normalize_contacts
        bc = {"CONTACTS": {"contact_lookup_result": {"contacts": [
            {"contact_id": "c1", "name": "Std", "endpoints": [{"value": "s@example.com", "label": "email"}]},
            {"name": "Buckeye Well", "emails": ["svc@example.com"], "phone_numbers": ["+1-614-555-0164"],
             "relationship": "Local installer"},
        ]}}}
        self.assertEqual(normalize_contacts(bc), 1)
        alt = bc["CONTACTS"]["contact_lookup_result"]["contacts"][1]
        self.assertTrue(alt["contact_id"].startswith("w2_contact_"))
        self.assertEqual(alt["endpoints"], [{"value": "svc@example.com", "label": "email"},
                                            {"value": "+1-614-555-0164", "label": "mobile"}])
        self.assertEqual(alt["notes"], "Relationship: Local installer")
        first_id = alt["contact_id"]
        again = {"CONTACTS": {"contact_lookup_result": {"contacts": [
            {"name": "Buckeye Well", "emails": ["svc@example.com"], "phone_numbers": ["+1-614-555-0164"]}]}}}
        normalize_contacts(again)
        self.assertEqual(again["CONTACTS"]["contact_lookup_result"]["contacts"][0]["contact_id"], first_id)  # stable
        self.assertEqual(normalize_contacts(bc), 0)  # idempotent

    def test_profile_name_variants(self):
        from wave2.personas import _profile_name
        self.assertEqual(_profile_name("# Adrian Castellanos: The Operations Consultant"), "Adrian Castellanos")
        self.assertEqual(_profile_name("# Claire Ferrante-Ross — Elder care"), "Claire Ferrante-Ross")


class PlanVerifyTests(_Wave2Env):
    def test_plan_counts_recreatable_and_manual(self):
        from wave2 import runner
        from wave2.personas import WorldCtx
        p = runner.plan(WorldCtx("test_person", ACCOUNT))
        self.assertEqual(p["expected"], {"GMAIL": 2, "CALENDAR": 1, "DRIVE": 2, "CONTACTS": 1, "TASKS": 1})
        self.assertEqual(p["not_recreatable"]["calendar_events_without_start"], 1)
        self.assertEqual(len(p["not_recreatable"]["drive_files"]), 1)  # the jpeg
        self.assertEqual(p["manual"], {"keep_items": 2, "chat_messages": 2})

    def test_verify_flags_a_short_surface(self):
        from wave2 import runner
        from wave2.personas import WorldCtx
        ctx = WorldCtx("test_person", ACCOUNT)
        full = {"gmail": {"a": 1, "b": 1}, "events": {"e": 1}, "drive_folders": {"f": 1},
                "drive_files": {"d": 1}, "contacts": {"c": 1}, "tasks": {"t": 1}}
        self.assertTrue(runner.verify(ctx, full)["ok"])
        short = dict(full, contacts={})
        v = runner.verify(ctx, short)
        self.assertFalse(v["ok"])
        self.assertFalse(v["surfaces"]["CONTACTS"]["ok"])

    def test_state_lives_under_the_wave2_state_dir(self):
        from wave2 import runner
        b = runner.base()
        self.assertTrue(str(b.state_dir()).startswith(os.environ["GAB_WAVE2_STATE_DIR"]))


class RunRoutingTests(_Wave2Env):
    """run() picks restore vs reseed from the mode and the saved baseline."""

    def _run(self, mode, baseline_folder=None, wipe_errors=()):
        from wave2 import runner
        from wave2.personas import WorldCtx
        b = runner.base()
        ctx = WorldCtx("test_person", ACCOUNT)
        if baseline_folder:
            st = b.load_state(ctx)
            st.update({"baseline": {"gmail": []}, "folder": baseline_folder,
                       "gmail": {"a": 1, "b": 1}, "events": {"e": 1}, "drive_folders": {"f": 1},
                       "drive_files": {"d": 1}, "contacts": {"c": 1}, "tasks": {"t": 1}})
            b.save_state(ctx, st)
        if not hasattr(self, "trash_result"):
            self.trash_result = {"removed": {}, "left": {}, "empty": True, "errors": []}
        S = MagicMock()
        S.gmail.users().getProfile().execute.return_value = {"emailAddress": ACCOUNT}

        def fake_seed(c, s, st):
            st.update({"gmail": {"a": 1, "b": 1}, "events": {"e": 1}, "drive_folders": {"f": 1},
                       "drive_files": {"d": 1}, "contacts": {"c": 1}, "tasks": {"t": 1}})

        with patch.object(runner, "wave2_credentials", return_value=object()), \
             patch.object(runner, "services", return_value=S), \
             patch.object(runner, "full_wipe", return_value={"errors": list(wipe_errors)}) as wipe, \
             patch.object(runner, "seed", side_effect=fake_seed) as seed, \
             patch.object(runner, "restore") as restore, \
             patch.object(runner, "empty_trash", return_value=self.trash_result), \
             patch.object(runner, "live_counts", side_effect=lambda S, st: {"surfaces": {}, "errors": []}):
            out = runner.run(ACCOUNT, "test_person", mode)
        return out, wipe, seed, restore

    def test_upload_seed_wipes_then_seeds(self):
        out, wipe, seed, restore = self._run("seed")
        self.assertEqual(out["action"], "reseed")
        wipe.assert_called_once()
        seed.assert_called_once()
        restore.assert_not_called()
        self.assertTrue(out["ok"], out["detail"])
        self.assertIn("manual (no API)", out["detail"])

    def test_delta_with_baseline_restores(self):
        out, wipe, seed, restore = self._run("delta", baseline_folder="test_person")
        self.assertEqual(out["action"], "restore")
        restore.assert_called_once()
        wipe.assert_not_called()
        seed.assert_not_called()

    def test_delta_without_baseline_falls_back_to_reseed(self):
        out, wipe, seed, restore = self._run("reconcile")
        self.assertEqual(out["action"], "reseed")
        seed.assert_called_once()

    def test_baseline_for_another_persona_is_not_restored(self):
        out, _w, seed, restore = self._run("delta", baseline_folder="someone_else")
        self.assertEqual(out["action"], "reseed")
        restore.assert_not_called()

    def test_wipe_errors_make_the_run_not_ok(self):
        out, *_ = self._run("reseed", wipe_errors=["delete contacts: HttpError 500"])
        self.assertFalse(out["ok"])
        self.assertIn("wipe errors", out["detail"])

    def test_trash_left_behind_makes_the_run_not_ok(self):
        self.trash_result = {"removed": {}, "left": {"gmail_trash": 4, "gmail_spam": 0, "drive_trash": 0},
                             "empty": False, "errors": []}
        out, *_ = self._run("reseed")
        self.assertFalse(out["ok"])
        self.assertIn("trash not empty", out["detail"])

    def test_progress_file_tracks_the_run(self):
        from wave2 import runner
        from wave2.personas import state_key
        out, *_ = self._run("seed")
        p = runner.read_progress_file(state_key("test_person", ACCOUNT))
        self.assertEqual(p["stage"], "done")
        self.assertTrue(p["ok"])
        self.assertEqual(p["email"], ACCOUNT)
        self.assertEqual(p["expected"]["CONTACTS"], 1)
        self.assertEqual(p["counts"]["GMAIL"], 2)
        self.assertEqual(p["manual"]["keep_items"], 2)
        self.assertIn("name", p["account_settings"])
        self.assertEqual(runner.progress_for(ACCOUNT, "Test Person")["stage"], "done")

    def test_progress_file_marks_a_crash_as_failed(self):
        from wave2 import runner
        from wave2.personas import state_key
        with patch.object(runner, "wave2_credentials", side_effect=RuntimeError("network down")):
            with self.assertRaises(RuntimeError):
                runner.run(ACCOUNT, "test_person", "seed")
        p = runner.read_progress_file(state_key("test_person", ACCOUNT))
        self.assertEqual(p["stage"], "failed")
        self.assertFalse(p["ok"])
        self.assertIn("network down", p["detail"])

    def test_persona_summaries_for_the_page(self):
        from wave2 import runner
        s = runner.persona_summaries()
        self.assertEqual([x["folder"] for x in s], ["test_person"])
        self.assertEqual(s[0]["name"], "Test Person")
        self.assertEqual(s[0]["expected"]["CONTACTS"], 1)
        self.assertEqual(s[0]["manual"]["chat_messages"], 2)

    def test_account_status(self):
        from wave2 import runner
        from wave2.creds import Wave2AuthError
        with patch.object(runner, "wave2_credentials", return_value=object()):
            self.assertTrue(runner.account_status(ACCOUNT)["wave2_ready"])
        with patch.object(runner, "wave2_credentials", side_effect=Wave2AuthError("without Wave 2 permissions")):
            st = runner.account_status(ACCOUNT)
            self.assertFalse(st["wave2_ready"])
            self.assertIn("Wave 2", st["reason"])
        with patch.object(runner, "wave2_credentials", side_effect=OSError("timeout")):
            self.assertIsNone(runner.account_status(ACCOUNT)["wave2_ready"])

    def test_token_for_another_account_is_refused(self):
        from wave2 import runner
        from wave2.creds import Wave2AuthError
        S = MagicMock()
        S.gmail.users().getProfile().execute.return_value = {"emailAddress": "someone@gmail.com"}
        with patch.object(runner, "wave2_credentials", return_value=object()), \
             patch.object(runner, "services", return_value=S):
            with self.assertRaises(Wave2AuthError):
                runner.run(ACCOUNT, "test_person", "seed")


class ExtraWipeTests(_Wave2Env):
    def test_extra_wipe_covers_every_surface(self):
        from wave2 import runner
        S = MagicMock()
        g = S.gmail.users()
        g.drafts().list().execute.return_value = {"drafts": [{"id": "d1"}, {"id": "d2"}]}
        g.labels().list().execute.return_value = {"labels": [{"id": "L1", "type": "user"},
                                                             {"id": "INBOX", "type": "system"}]}
        S.cal.calendarList().list().execute.return_value = {"items": [
            {"id": "primary", "primary": True, "accessRole": "owner"},
            {"id": "fam", "accessRole": "owner"},
            {"id": "holidays", "accessRole": "reader"}]}
        S.people.people().connections().list().execute.return_value = {"connections": [
            {"resourceName": "people/1"}, {"resourceName": "people/2"}]}
        S.people.contactGroups().list().execute.return_value = {"contactGroups": [
            {"resourceName": "contactGroups/a", "groupType": "USER_CONTACT_GROUP"},
            {"resourceName": "contactGroups/myContacts", "groupType": "SYSTEM_CONTACT_GROUP"}]}
        S.tasks.tasklists().list().execute.return_value = {"items": [{"id": "default"}, {"id": "extra"}]}
        S.tasks.tasks().list().execute.return_value = {"items": [{"id": "t1"}, {"id": "t2", "deleted": True}]}
        S.drive.about().get().execute.return_value = {"user": {"permissionId": "me1"}}
        S.drive.files().list().execute.return_value = {"files": [{"id": "shared1"}]}

        out = runner.extra_wipe(S, lambda m: None)
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["gmail_drafts"], 2)
        self.assertEqual(out["gmail_user_labels"], 1)
        self.assertEqual(out["calendars"], {"secondary_deleted": 1, "subscriptions_removed": 1})
        self.assertEqual(out["contacts"], 2)
        self.assertEqual(out["contact_groups"], 1)
        self.assertEqual(out["tasks"], 1)
        self.assertEqual(out["drive_shared_in_removed"], 1)
        S.people.people().batchDeleteContacts.assert_called_with(
            body={"resourceNames": ["people/1", "people/2"]})
        S.tasks.tasklists().delete.assert_called_with(tasklist="extra")

    def test_one_failing_surface_does_not_stop_the_rest(self):
        from wave2 import runner
        S = MagicMock()
        S.gmail.users().drafts().list().execute.side_effect = RuntimeError("boom")
        S.gmail.users().labels().list().execute.return_value = {"labels": []}
        S.cal.calendarList().list().execute.return_value = {"items": []}
        S.people.people().connections().list().execute.return_value = {"connections": [{"resourceName": "people/1"}]}
        S.people.contactGroups().list().execute.return_value = {}
        S.tasks.tasklists().list().execute.return_value = {"items": [{"id": "default"}]}
        S.tasks.tasks().list().execute.return_value = {}
        S.drive.about().get().execute.return_value = {"user": {}}
        out = runner.extra_wipe(S, lambda m: None)
        self.assertEqual(len(out["errors"]), 1)
        self.assertIn("drafts", out["errors"][0])
        self.assertEqual(out["contacts"], 1)  # later surfaces still ran


class CredsTests(_Wave2Env):
    def test_missing_token_asks_for_reauth(self):
        from wave2 import creds
        consumer = MagicMock()
        with patch("materialize.authbackend.backend_for", return_value=consumer), \
             patch("materialize.auth.token_path", return_value=Path(self.tmp.name) / "none.json"):
            with self.assertRaisesRegex(creds.Wave2AuthError, "re-authorize"):
                creds.wave2_credentials(ACCOUNT)

    def test_token_without_wave2_scopes_asks_for_reauth(self):
        from google.auth.exceptions import RefreshError

        from wave2 import creds
        tok = Path(self.tmp.name) / "tok.json"
        tok.write_text(json.dumps({"refresh_token": "r", "client_id": "c", "client_secret": "s",
                                   "token_uri": "https://oauth2.googleapis.com/token"}))
        with patch("materialize.authbackend.backend_for", return_value=MagicMock()), \
             patch("materialize.auth.token_path", return_value=tok), \
             patch("google.oauth2.credentials.Credentials.refresh",
                   side_effect=RefreshError("invalid_scope: Bad Request")):
            with self.assertRaisesRegex(creds.Wave2AuthError, "without Wave 2 permissions"):
                creds.wave2_credentials(ACCOUNT)

    def test_clean_extras_skips_accounts_without_a_wave2_grant(self):
        from wave2 import runner
        from wave2.creds import Wave2AuthError
        with patch.object(runner, "wave2_credentials", side_effect=Wave2AuthError("no")):
            self.assertIsNone(runner.clean_extras(ACCOUNT))


class ScopeTests(unittest.TestCase):
    def test_make_flow_default_scopes_unchanged_and_extra_appended(self):
        from materialize import auth
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"web": {"client_id": "x", "client_secret": "y",
                               "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                               "token_uri": "https://oauth2.googleapis.com/token",
                               "redirect_uris": ["http://localhost/oauth/callback"]}}, fh)
        try:
            plain = auth.make_flow("http://localhost/oauth/callback", client_path=Path(fh.name))
            self.assertEqual(list(plain.oauth2session.scope), auth.SCOPES)
            wide = auth.make_flow("http://localhost/oauth/callback", client_path=Path(fh.name),
                                  extra_scopes=auth.WAVE2_EXTRA_SCOPES)
            self.assertEqual(list(wide.oauth2session.scope), auth.WAVE2_SCOPES)
        finally:
            os.unlink(fh.name)

    def test_wave2_scopes_are_contacts_and_tasks_only(self):
        from materialize import auth
        self.assertEqual(auth.WAVE2_EXTRA_SCOPES, ["https://www.googleapis.com/auth/contacts",
                                                   "https://www.googleapis.com/auth/tasks"])
        self.assertEqual(auth.WAVE2_SCOPES[: len(auth.SCOPES)], auth.SCOPES)


if __name__ == "__main__":
    unittest.main()


class BirthdayTests(unittest.TestCase):
    def test_reads_only_the_personas_own_row(self):
        from wave2.personas import profile_birthday
        doc = ("| **Name** | Arthur Pendelton |\n| **Date of Birth & Age** | April 22, 1958 (Age 68) |\n"
               "- **Evelyn Pendelton (Wife, Age 66):** Born September 14, 1960")
        self.assertEqual(profile_birthday(doc), "1958-04-22")
        self.assertEqual(profile_birthday("| **Date of Birth & Age** | Sept. 4, 1986 (Age 40) |"), "1986-09-04")
        self.assertEqual(profile_birthday("no table here; Born September 14, 1960"), "")


class HomeAddressUnitTests(unittest.TestCase):
    def test_unit_from_the_personas_own_data_is_added(self):
        from wave2.personas import with_unit
        data = "Lease: 285 Lafayette St, Apt 4B, New York, NY 10012 | rent"
        self.assertEqual(with_unit("285 Lafayette St, New York, NY 10012", data),
                         "285 Lafayette St, Apt 4B, New York, NY 10012")
        self.assertEqual(with_unit("1180 Peachtree St NE, Atlanta, GA 30309",
                                   "statement 1180 Peachtree St NE, Unit 2408, Atlanta"),
                         "1180 Peachtree St NE, Unit 2408, Atlanta, GA 30309")

    def test_no_unit_or_already_has_one_is_unchanged(self):
        from wave2.personas import with_unit
        self.assertEqual(with_unit("1420 Oakridge Dr, Columbus, OH 43221", "1420 Oakridge Dr, Columbus"),
                         "1420 Oakridge Dr, Columbus, OH 43221")
        self.assertEqual(with_unit("9 Elm St, Apt 2, Austin", "9 Elm St, Unit 7"), "9 Elm St, Apt 2, Austin")
        # another street's unit is never borrowed
        self.assertEqual(with_unit("9 Elm St, Austin", "19 Elm St, Unit 7"), "9 Elm St, Austin")


class EmptyTrashTests(_Wave2Env):
    def _S(self, trash, spam, drive_trashed):
        """Fake services whose Trash/Spam empty once batchDelete is called."""
        S = MagicMock()
        g = S.gmail.users()
        box = {"TRASH": list(trash), "SPAM": list(spam)}

        def lst(**kw):
            req = MagicMock()
            label = (kw.get("labelIds") or [""])[0]
            req.execute.return_value = {"messages": [{"id": i} for i in box.get(label, [])]}
            return req

        def bdel(userId, body):
            for k in box:
                box[k] = [i for i in box[k] if i not in body["ids"]]
            return MagicMock()

        g.messages().list.side_effect = lst
        g.messages().batchDelete.side_effect = bdel
        drive = {"n": drive_trashed}
        S.drive.files().emptyTrash().execute.side_effect = lambda: drive.update(n=0)
        S.drive.files().list.side_effect = lambda **kw: MagicMock(**{"execute.return_value": {
            "files": [{"id": f"f{i}"} for i in range(drive["n"])]}})
        return S, box

    def test_trash_spam_and_drive_trash_are_emptied(self):
        from wave2 import runner
        S, box = self._S(["t1", "t2"], ["s1"], 3)
        out = runner.empty_trash(S, lambda m: None)
        self.assertEqual(out["removed"], {"gmail_trash": 2, "gmail_spam": 1})
        self.assertEqual(out["left"], {"gmail_trash": 0, "gmail_spam": 0, "drive_trash": 0})
        self.assertTrue(out["empty"])
        self.assertEqual(out["errors"], [])

    def test_trash_that_cannot_be_emptied_is_reported(self):
        from wave2 import runner
        S, box = self._S(["t1"], [], 0)
        S.gmail.users().messages().batchDelete.side_effect = RuntimeError("403 insufficient permission")
        out = runner.empty_trash(S, lambda m: None)
        self.assertFalse(out["empty"])
        self.assertEqual(out["left"]["gmail_trash"], 1)
        self.assertTrue(any("Gmail Trash" in e for e in out["errors"]))


class LiveVerifyTests(_Wave2Env):
    def _S(self, gmail, events, drive, contacts, tasks):
        S = MagicMock()
        page = lambda key, items: MagicMock(**{"execute.return_value": {key: items}})
        S.gmail.users().messages().list.side_effect = lambda **kw: page("messages", [{"id": i} for i in gmail])
        S.cal.events().list.side_effect = lambda **kw: page("items", [{"id": i} for i in events] if kw["calendarId"] == "primary" else [])
        S.drive.files().list.side_effect = lambda **kw: page("files", [{"id": i} for i in drive])
        S.people.people().connections().list.side_effect = lambda **kw: page("connections", [{"resourceName": i} for i in contacts])
        S.tasks.tasks().list.side_effect = lambda **kw: page("items", [{"id": i} for i in tasks])
        return S

    ST = {"gmail": {"e1": {"id": "m1"}, "e2": {"id": "m2"}}, "events": {"v1": {"cal": "primary", "id": "ev1"}},
          "drive_folders": {"fo": {"id": "D1"}}, "drive_files": {"fi": {"id": "D2"}},
          "contacts": {"c1": {"resourceName": "people/1"}, "c2": {"resourceName": "people/2"}},
          "tasks": {"t1": {"list": "@default", "id": "T1"}}}

    def test_counts_what_is_really_in_the_account(self):
        from wave2 import runner
        S = self._S(["m1", "x9"], ["ev1"], ["D1", "D2"], ["people/1"], [])   # m2, people/2, T1 gone; x9 extra
        live = runner.live_counts(S, self.ST)
        self.assertEqual(live["errors"], [])
        self.assertEqual(live["surfaces"]["GMAIL"], {"recorded": 2, "present": 1, "extra": 1})
        self.assertEqual(live["surfaces"]["DRIVE"]["present"], 2)
        self.assertEqual(live["surfaces"]["CONTACTS"]["present"], 1)
        self.assertEqual(live["surfaces"]["TASKS"]["present"], 0)

    def test_verify_fails_when_a_seeded_item_was_deleted(self):
        from wave2 import runner
        from wave2.personas import WorldCtx
        ctx = WorldCtx("test_person", ACCOUNT)
        with patch.object(runner, "plan", return_value={"expected": {"GMAIL": 2, "CALENDAR": 1, "DRIVE": 2, "CONTACTS": 2, "TASKS": 1},
                                                         "manual": {}, "not_recreatable": []}):
            full = runner.verify(ctx, self.ST, self._S(["m1", "m2"], ["ev1"], ["D1", "D2"], ["people/1", "people/2"], ["T1"]))
            short = runner.verify(ctx, self.ST, self._S(["m1", "m2"], ["ev1"], ["D1", "D2"], ["people/1"], ["T1"]))
            stateonly = runner.verify(ctx, self.ST)
        self.assertTrue(full["ok"] and full["live"])
        self.assertFalse(short["ok"])
        self.assertEqual(short["surfaces"]["CONTACTS"]["missing"], 1)
        self.assertTrue(stateonly["ok"])   # the old state-only check could not see the deletion


class CalendarFormatTests(unittest.TestCase):
    def test_string_attendees_become_dicts_and_self_is_marked(self):
        from wave2.personas import normalize_attendees
        bc = {"CALENDAR": {"events": [{"id": "e1", "summary": "S", "start": {},
                                       "attendees": ["Me@X.com", "bob@example.com", ""]},
                                      {"id": "e2", "summary": "T", "start": {},
                                       "attendee": [{"email": "a@b.com", "display_name": "A"}]}]}}
        self.assertEqual(normalize_attendees(bc, "me@x.com"), 2)
        ev = bc["CALENDAR"]["events"]
        self.assertEqual(ev[0]["attendees"], [{"email": "Me@X.com", "is_self": True}, {"email": "bob@example.com"}])
        self.assertEqual(ev[1]["attendee"], [{"email": "a@b.com", "display_name": "A"}])   # untouched

    def test_repeated_event_ids_get_stable_suffixes(self):
        from wave2.personas import normalize_attendees
        mk = lambda t: {"id": "lift", "summary": "Lift", "start": {"t": t}}
        bc = {"CALENDAR": {"events": [mk(1), mk(2), mk(3), {"id": "solo", "summary": "S", "start": {}}]}}
        normalize_attendees(bc)
        self.assertEqual([e["id"] for e in bc["CALENDAR"]["events"]], ["lift", "lift__2", "lift__3", "solo"])


class DriveFolderMergeTests(unittest.TestCase):
    def test_same_name_folders_in_the_same_place_become_one(self):
        from wave2.personas import merge_duplicate_folders
        fld = lambda i, n, p="": {"folder_id": i, "name": n, "parent_folder_id": p}
        bc = {"DRIVE": {"mock_context": {"drive_mock_context": {
            "folders": [fld("fld_11", "Client Marketing"), fld("fld_13", "TrustedPeps", "fld_11"),
                        fld("t73:cm", "Client Marketing"), fld("t73:tp", "TrustedPeps", "t73:cm"),
                        fld("t47:cm", "Client Marketing"), fld("t47:co", "Coaching", "t47:cm"),
                        fld("fld_20", "TrustedPeps")],   # same name, different place: kept
            "files": [{"file_id": "f1", "name": "vial.png", "parent_folder_id": "t73:tp"},
                      {"file_id": "f2", "name": "ad.png", "parent_folder_id": "t47:co"},
                      {"file_id": "f3", "name": "root.txt"}]}}}}
        self.assertEqual(merge_duplicate_folders(bc), 3)
        ctx = bc["DRIVE"]["mock_context"]["drive_mock_context"]
        self.assertEqual([(f["folder_id"], f["parent_folder_id"]) for f in ctx["folders"]],
                         [("fld_11", ""), ("fld_13", "fld_11"), ("t47:co", "fld_11"), ("fld_20", "")])
        self.assertEqual([f.get("parent_folder_id") for f in ctx["files"]], ["fld_13", "t47:co", None])

    def test_no_duplicates_changes_nothing(self):
        from wave2.personas import merge_duplicate_folders
        bc = {"DRIVE": {"mock_context": {"drive_mock_context": {"folders": [{"folder_id": "a", "name": "A"}], "files": []}}}}
        self.assertEqual(merge_duplicate_folders(bc), 0)
        self.assertEqual(merge_duplicate_folders({}), 0)


class DriveAttachmentTests(_Wave2Env):
    PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 10

    def rec(self, att, mime="image/png"):
        return {"file_id": "t:img", "name": "logo.png", "mime_type": mime, "parent_folder_id": "fld",
                "content_summary": "LOGO", "file_attachment": att, "modified_time": "2026-10-28T14:21:00Z"}

    def test_bytes_are_read_from_a_dict_or_its_python_repr(self):
        import base64
        from wave2.runner import attachment_bytes
        b64 = base64.b64encode(self.PNG).decode()
        self.assertEqual(attachment_bytes(self.rec({"inline_bytes": b64})), self.PNG)
        self.assertEqual(attachment_bytes(self.rec(str({"inline_bytes": b64}))), self.PNG)   # stored as a repr string
        self.assertIsNone(attachment_bytes(self.rec(None)))
        self.assertIsNone(attachment_bytes(self.rec({"inline_bytes": b64}, mime="application/vnd.google-apps.spreadsheet")))

    def test_a_file_with_bytes_is_uploaded_as_is_not_skipped(self):
        import base64
        from wave2 import runner
        b = runner.base()
        f = self.rec({"inline_bytes": base64.b64encode(self.PNG).decode()})
        self.assertEqual(b.file_kind(f), "attachment")
        S = MagicMock()
        S.drive.files().create().execute.return_value = {"id": "NEW"}
        S.drive.files().get().execute.return_value = {"id": "NEW", "version": "3"}
        st = {"drive_folders": {"fld": {"id": "FOLDER"}}}
        with patch("googleapiclient.http.MediaInMemoryUpload") as up:
            out = b.create_drive_file(MagicMock(), S, st, f)
        self.assertEqual(out, {"id": "NEW", "version": "3"})
        self.assertEqual(up.call_args.args[0], self.PNG)
        body = S.drive.files().create.call_args.kwargs["body"]
        self.assertEqual((body["name"], body["mimeType"], body["parents"]), ("logo.png", "image/png", ["FOLDER"]))

    def test_a_file_without_bytes_keeps_the_old_path(self):
        from wave2 import runner
        b = runner.base()
        f = self.rec(None, mime="text/plain")
        self.assertNotEqual(b.file_kind(f), "attachment")


class CalendarReuseTests(_Wave2Env):
    def _ctx_with(self, names):
        from wave2 import runner
        from wave2.personas import WorldCtx
        ctx = WorldCtx("test_person", ACCOUNT)
        cmap = {"cal_primary": {"id": "cal_primary", "is_primary": True}}
        cmap.update({f"cal_{i}": {"id": f"cal_{i}", "summary": n} for i, n in enumerate(names)})
        return runner, ctx, cmap

    def test_same_name_reused_spare_renamed_extra_deleted_none_created(self):
        runner, ctx, cmap = self._ctx_with(["Family", "Medical"])
        b = runner.base()
        S = MagicMock()
        owned = [{"id": "g_primary", "primary": True, "accessRole": "owner"},
                 {"id": "g_fam", "summary": "Family", "accessRole": "owner"},
                 {"id": "g_old1", "summary": "Old persona work", "accessRole": "owner"},
                 {"id": "g_old2", "summary": "Old persona gym", "accessRole": "owner"},
                 {"id": "g_hol", "summary": "Holidays", "accessRole": "reader"}]
        st = {"calendars": {}}
        with patch.object(b, "calendar_map", return_value=cmap), patch.object(b, "list_calendars", return_value=owned), \
             patch.object(b, "save_state"):
            out = runner.prepare_calendars(ctx, S, st, log=lambda m: None)
        self.assertEqual((out["reused"], out["renamed"], out["created"], out["deleted_spare"], out["fallback"]),
                         (1, 1, 0, 1, []))
        self.assertEqual(st["calendars"], {"cal_0": "g_fam", "cal_1": "g_old1"})
        S.cal.calendars().patch.assert_any_call(calendarId="g_old1", body={"summary": "Medical", "timeZone": ctx.tz_name})
        S.cal.calendars().delete.assert_called_once_with(calendarId="g_old2")
        S.cal.calendars().insert.assert_not_called()

    def test_creation_limit_falls_back_to_primary_and_fails_the_run(self):
        runner, ctx, cmap = self._ctx_with(["A", "B", "C"])
        b = runner.base()
        S = MagicMock()
        S.cal.calendars().insert.return_value.execute.side_effect = RuntimeError(
            'HttpError 403 "Calendar usage limits exceeded." quotaExceeded')
        st = {"calendars": {}}
        with patch.object(b, "calendar_map", return_value=cmap), patch.object(b, "list_calendars", return_value=[]), \
             patch.object(b, "save_state"):
            out = runner.prepare_calendars(ctx, S, st, log=lambda m: None)
        self.assertEqual(out["fallback"], ["A", "B", "C"])
        self.assertEqual(set(st["calendars"].values()), {"primary"})
        self.assertEqual(S.cal.calendars().insert.return_value.execute.call_count, 1)   # stops after the first refusal

    def test_reseed_wipe_keeps_and_empties_owned_calendars(self):
        from wave2 import runner
        S = MagicMock()
        S.cal.calendarList().list().execute.return_value = {"items": [
            {"id": "primary", "primary": True, "accessRole": "owner"}, {"id": "fam", "accessRole": "owner"}]}
        S.cal.events().list.side_effect = lambda **kw: MagicMock(**{"execute.return_value": {"items": [{"id": "e1"}, {"id": "e2"}]}})
        b = runner.base()
        one_page = lambda method, key, **kw: list((method(**kw).execute() or {}).get(key, []) or [])
        with patch.object(b, "pages", side_effect=one_page):
            out = runner.extra_wipe(S, lambda m: None, keep_calendars=True)
        self.assertEqual(out["calendars"]["secondary_kept_emptied"], 1)
        self.assertEqual(out["calendars"]["events_cleared"], 2)
        S.cal.calendars().delete.assert_not_called()


class GmailReadbackTests(unittest.TestCase):
    def test_404_tolerated_only_inside_insert(self):
        from wave2 import runner
        calls = {"n": 0}

        def orig(req, *a, **k):
            calls["n"] += 1
            raise RuntimeError("HttpError 404 notFound")
        req = MagicMock(uri="https://gmail.googleapis.com/gmail/v1/users/me/messages/abc?format=minimal&alt=json", method="GET")
        ex = runner._tolerant_readback(orig)
        with self.assertRaises(RuntimeError):          # outside insert_email: unchanged
            ex(req)
        runner._READBACK.on = True
        try:
            with patch.object(runner.time, "sleep"):
                self.assertIsNone(ex(req))             # inside: retried, then None
        finally:
            runner._READBACK.on = False
        self.assertEqual(calls["n"], 1 + 4)
