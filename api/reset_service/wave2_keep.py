"""Keep on the /wave2 page: is an account's Keep holding exactly the persona's notes?

Keep has no API for consumer accounts, so Keep is seeded by a worker machine (Clickless) through the module job queue
(jobs.py). This module only *reads*: given the persona's expected notes and the account's latest keep-seed job, it says
where Keep stands in words a person can act on.

  * every expected note there, once           (missing, duplicates)
  * nothing that should not be there          (extra notes in Keep, anything left in Archive or Trash)

The strongest answer needs the worker to report what it saw in Keep after its run (the job's ``inventory``:
{home: [titles], archive: [titles], trash: [titles]}). A worker that does not send it yet still reports how many notes
it confirmed after reloading Keep (its summary, e.g. "adrian_castellanos: 3 created, 2 deleted first"); then the note
count is checked, and extras are reported as not checked.

Pure functions, no I/O: tested in tests/test_wave2_keep.py.
"""
from __future__ import annotations

import re
from typing import Any, Optional

_KEY_LEN, _MATCH_LEN = 60, 40   # Clickless recipes/keep/pack.js: noteKey (60) and matchKey (40)
_SUMMARY = re.compile(r"(\d+)\s+(created|already there|deleted first)")
OPEN = ("queued", "running")


def title_key(title: Any) -> str:
    """Same rule as seeder/wave2/runner.keep_title_key and Clickless: squash spaces, 60 then 40 characters, lowercase."""
    squashed = re.sub(r"\s+", " ", str(title if title is not None else "")).strip()
    return squashed[:_KEY_LEN][:_MATCH_LEN].strip().lower()


def summary_counts(summary: Optional[str]) -> dict[str, int]:
    """Numbers out of the worker's done summary ("persona: 3 created, 2 already there, 5 deleted first")."""
    out = {"created": 0, "already there": 0, "deleted first": 0}
    for number, what in _SUMMARY.findall(summary or ""):
        out[what] = int(number)
    return out


def compare(expected: list[dict[str, Any]], inventory: dict[str, list[str]]) -> dict[str, Any]:
    """Expected notes against what the worker saw: missing, duplicated, extra, and what is left in Archive and Trash."""
    want = {e["key"]: e["title"] for e in expected}
    counts: dict[str, int] = {}
    extra: list[str] = []
    for title in inventory.get("home") or []:
        key = title_key(title)
        if key in want:
            counts[key] = counts.get(key, 0) + 1
        elif key:
            extra.append(title)
    return {
        "expected": len(want),
        "found": sum(1 for k in want if counts.get(k)),
        "missing": [want[k] for k in want if not counts.get(k)],
        "duplicates": [f"{want[k]} (×{n})" for k, n in counts.items() if n > 1],
        "extra": extra,
        "archive": list(inventory.get("archive") or []),
        "trash": list(inventory.get("trash") or []),
    }


def _job_view(job: dict[str, Any]) -> dict[str, Any]:
    keep = ("job_id", "status", "mode", "persona", "attempt", "max_attempts", "worker_id", "created_at", "finished_at", "summary", "derived")
    return {k: job.get(k) for k in keep}


def check(expected: list[dict[str, Any]], persona: str, job: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Where Keep stands for one account. ``state`` is one of:
    none · queued · running · verified · confirmed (count right, extras not checked) · problem · needs_manual · failed · cancelled.
    ``ok`` is True only for verified and confirmed."""
    n = len(expected)
    base: dict[str, Any] = {"expected": n, "titles": [e["title"] for e in expected], "ok": False, "job": _job_view(job) if job else None,
                            "missing": [], "duplicates": [], "extra": [], "archive": [], "trash": [], "found": None, "inventory": False}
    if not job:
        return {**base, "state": "none", "headline": "Keep not run yet", "detail": f"{n} note(s) to seed."}
    status = job.get("status")
    follow = job.get("follow_up") or {}
    if status == "queued":
        return {**base, "state": "queued", "headline": "Keep queued, waiting for the worker",
                "detail": "It starts as soon as a worker that has this account signed in asks for work."}
    if status == "running":
        return {**base, "state": "running", "headline": "Keep running on the worker", "detail": f"Attempt {job.get('attempt')} of {job.get('max_attempts')}."}
    if status == "needs_manual":
        return {**base, "state": "needs_manual", "headline": "Keep needs a person", "detail": follow.get("reason") or "The worker could not finish.",
                "code": follow.get("code"), "needs": follow.get("needs")}
    if status == "failed":
        return {**base, "state": "failed", "headline": "Keep failed", "detail": follow.get("reason") or job.get("summary") or "", "code": follow.get("code")}
    if status in ("cancelled", "resolved"):
        detail = job.get("resolution_note") if status == "resolved" else "Run Keep again."
        return {**base, "state": status, "headline": "Keep was cancelled" if status == "cancelled" else "Marked done by a person (not checked)",
                "detail": detail or ""}
    if status != "done":
        return {**base, "state": "problem", "headline": f"Keep job is {status}", "detail": ""}

    if job.get("persona") and job["persona"] != persona:
        return {**base, "state": "problem", "headline": f"Keep was seeded for {job['persona']}, not this persona", "detail": "Run Keep again for this persona."}
    inventory = job.get("inventory")
    if inventory:
        diff = compare(expected, inventory)
        out = {**base, **diff, "inventory": True}
        problems = []
        if diff["missing"]:
            problems.append(f"{len(diff['missing'])} missing")
        if diff["duplicates"]:
            problems.append(f"{len(diff['duplicates'])} duplicated")
        if diff["extra"]:
            problems.append(f"{len(diff['extra'])} extra note(s)")
        if diff["archive"]:
            problems.append(f"{len(diff['archive'])} in Archive")
        if diff["trash"]:
            problems.append(f"{len(diff['trash'])} in Trash")
        if problems:
            return {**out, "state": "problem", "headline": "Keep doesn't match: " + ", ".join(problems),
                    "detail": "Run Keep again (reseed wipes Keep first)."}
        return {**out, "state": "verified", "ok": True, "headline": f"Keep verified: {n}/{n} notes, nothing extra",
                "detail": "Every note is there once; Archive and Trash are empty."}

    counts = summary_counts(job.get("summary"))
    confirmed = counts["created"] + counts["already there"]
    out = {**base, "found": confirmed}
    if confirmed != n:
        return {**out, "state": "problem", "headline": f"Worker confirmed {confirmed} of {n} notes", "detail": job.get("summary") or ""}
    wiped = job.get("mode") == "reseed"
    detail = "Extra notes not checked: the worker did not report what it saw in Keep."
    if wiped:
        detail = f"Keep was emptied first ({counts['deleted first']} deleted). " + detail
    return {**out, "state": "confirmed", "ok": True, "headline": f"Keep done: {n}/{n} notes confirmed", "detail": detail}
