#!/usr/bin/env python3
"""Smoke test for the module jobs API against a RUNNING platform (local, staging or production).

    BASE_URL=http://127.0.0.1:8791 CALLER_KEY=... WORKER_KEY=... python api/scripts/jobs_smoke.py [--fast]

It plays both roles with a made-up account (smoke-test@example.com) and a made-up worker (smoke-worker), so no real worker or real
Google account is ever involved: a real worker only takes jobs for accounts it has signed in. It leaves no job open: every job it creates
ends cancelled, resolved or done, and if a step fails part-way the jobs it made are cancelled (or closed as failed) before it exits.
The made-up workers (smoke-worker, smoke-other, ...) do stay in module_workers; they hold only the made-up accounts.

--fast also checks lease expiry and the "nobody can take this job" view. Those need short timings, so start the platform with
JOB_LEASE_S=3 JOB_UNROUTABLE_AFTER_S=3 JOB_WORKER_LIVE_S=2 first. Do not use --fast against production.
"""
from __future__ import annotations

import os
import sys
import time
import uuid

import httpx

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8791").rstrip("/")
CALLER = {"Authorization": f"Bearer {os.environ.get('CALLER_KEY', '')}"}
WORKER = {"Authorization": f"Bearer {os.environ.get('WORKER_KEY', '')}"}
ACCOUNT = "smoke-test@example.com"
FAST = "--fast" in sys.argv
RUN = uuid.uuid4().hex[:8]
client = httpx.Client(base_url=BASE, timeout=20.0)
failures: list[str] = []
ACCOUNTS = [ACCOUNT]   # every made-up account a job was created for, so cleanup can find them


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok ' if ok else 'BAD'}  {label}{'  -> ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(label)


def worker_body(worker_id="smoke-worker", accounts=(ACCOUNT,)):
    return {"worker_id": worker_id, "accounts": list(accounts), "workflows": ["keep-seed", "keep-wipe"], "personas": ["smoke_persona"], "pack_version": "smoke"}


def job(n: int, **over):
    body = {"request_id": f"smoke-{RUN}-{n:02d}", "workflow": "keep-seed", "account": ACCOUNT, "persona": "smoke_persona", "mode": "clean"}
    body.update(over)
    return body


def section(title: str) -> None:
    print(f"\n{title}")


def cleanup() -> None:
    """Close every job this run left open: queued or waiting ones are cancelled, running ones are reported failed by their worker."""
    for account in ACCOUNTS:
        try:
            r = client.get("/api/jobs", headers=CALLER, params={"account": account})
            if r.status_code != 200:
                continue
            for j in r.json():
                if not str(j.get("request_id", "")).startswith(f"smoke-{RUN}-"):
                    continue
                if j["status"] in ("queued", "needs_manual"):
                    client.post(f"/api/jobs/{j['job_id']}/cancel", headers=CALLER)
                elif j["status"] == "running":
                    client.post(f"/api/worker/jobs/{j['job_id']}/result", headers=WORKER, json={
                        "worker_id": j["worker_id"], "outcome": "failed",
                        "follow_up": {"code": "SMOKE_ABORTED", "reason": "The smoke test stopped part-way and closed this job.", "needs": "none"}})
        except httpx.HTTPError:
            pass


def main() -> int:
    try:
        return checks()
    except (KeyError, TypeError, ValueError, httpx.HTTPError) as exc:   # an answer the next step could not use: stop, do not guess
        print(f"\nstopped part-way: {type(exc).__name__}: {exc}")
        return 1
    finally:
        cleanup()


def checks() -> int:
    section("platform")
    r = client.get("/healthz")
    check("healthz answers", r.status_code == 200, f"{r.status_code} {r.text[:120]}")

    section("keys")
    check("caller route without a key is refused", client.post("/api/jobs", json=job(0)).status_code == 401)
    check("worker key does not open caller routes", client.post("/api/jobs", headers=WORKER, json=job(0)).status_code == 401)
    r = client.post("/api/worker/claim", headers=CALLER, json=worker_body())
    check("caller key does not open worker routes", r.status_code == 401, f"{r.status_code}")
    r = client.post("/api/worker/claim", headers=WORKER, json=worker_body("smoke-worker", ()))
    check("the worker key is configured (not 503)", r.status_code in (200, 204), f"{r.status_code} {r.text[:120]}")

    section("enqueue")
    r = client.post("/api/jobs", headers=CALLER, json=job(1))
    first = r.json() if r.status_code in (200, 201) else {}
    check("a job is queued (201)", r.status_code == 201 and first.get("job", {}).get("status") == "queued", f"{r.status_code} {r.text[:160]}")
    if not first.get("job"):
        print("\nthe first job could not be created, so the rest cannot run (is the schema applied?)")
        return 1
    check("it is not routable yet: no worker has the account", first.get("routable") is False)
    r = client.post("/api/jobs", headers=CALLER, json=job(1))
    check("the same request again returns the same job (200)", r.status_code == 200 and r.json()["job"]["job_id"] == first["job"]["job_id"])
    check("a different body under the same id is refused (409)", client.post("/api/jobs", headers=CALLER, json=job(1, persona="other")).status_code == 409)
    r = client.post("/api/jobs", headers=CALLER, json={**job(9), "password": "hunter2"})
    check("a password field is refused (422) and not echoed", r.status_code == 422 and "hunter2" not in r.text)
    job_id = first["job"]["job_id"]

    section("worker: claim, heartbeat, report")
    r = client.post("/api/worker/claim", headers=WORKER, json=worker_body("smoke-other", ("someone-else@example.com",)))
    check("a worker without the account gets nothing (204)", r.status_code == 204, f"{r.status_code}")
    r = client.post("/api/worker/claim", headers=WORKER, json=worker_body())
    check("the worker with the account gets the job (200, running)", r.status_code == 200 and r.json()["job"]["job_id"] == job_id and r.json()["job"]["status"] == "running", f"{r.status_code} {r.text[:160]}")
    client.post("/api/jobs", headers=CALLER, json={"request_id": f"smoke-{RUN}-02", "workflow": "keep-wipe", "account": ACCOUNT})   # a second job for the same account
    r = client.post("/api/worker/claim", headers=WORKER, json=worker_body("smoke-other-2"))
    check("only one job per account runs at a time (204)", r.status_code == 204, f"{r.status_code}")
    r = client.post(f"/api/worker/jobs/{job_id}/heartbeat", headers=WORKER, json={"worker_id": "smoke-worker"})
    check("heartbeat extends the lease", r.status_code == 200 and "lease_expires_at" in r.json(), f"{r.status_code} {r.text[:120]}")
    r = client.post(f"/api/worker/jobs/{job_id}/heartbeat", headers=WORKER, json={"worker_id": "someone-else"})
    check("a worker that does not hold the job is refused (409)", r.status_code == 409)
    r = client.post(f"/api/worker/jobs/{job_id}/result", headers=WORKER, json={"worker_id": "smoke-worker", "outcome": "done", "summary": "smoke: 3 created", "pack_version": "smoke"})
    check("done is recorded", r.status_code == 200 and r.json()["status"] == "done", f"{r.status_code} {r.text[:120]}")
    r = client.get(f"/api/jobs/{job_id}", headers=CALLER)
    check("the caller sees the result", r.status_code == 200 and r.json()["summary"] == "smoke: 3 created")

    section("follow-ups")
    r = client.post("/api/worker/claim", headers=WORKER, json=worker_body())
    wipe_id = r.json()["job"]["job_id"] if r.status_code == 200 else ""
    check("the next job for the account (the wipe) is now handed out", r.status_code == 200 and r.json()["job"]["workflow"] == "keep-wipe", f"{r.status_code}")
    r = client.post(f"/api/worker/jobs/{wipe_id}/result", headers=WORKER, json={"worker_id": "smoke-worker", "outcome": "needs_manual",
                    "follow_up": {"code": "SIGN_IN_NEEDED", "reason": "Smoke test: signed out.", "needs": "sign-in", "step": "open Keep"}})
    check("needs_manual is recorded with its reason", r.status_code == 200 and r.json()["status"] == "needs_manual", f"{r.status_code} {r.text[:120]}")
    listed = client.get("/api/followups", headers=CALLER).json()
    check("it shows in the follow-up list", any(j["job_id"] == wipe_id for j in listed))
    r = client.post(f"/api/jobs/{wipe_id}/resolve", headers=CALLER, json={"note": "smoke test"})
    check("a person can resolve it", r.status_code == 200 and r.json()["status"] == "resolved", f"{r.status_code} {r.text[:120]}")
    check("it leaves the follow-up list", all(j["job_id"] != wipe_id for j in client.get("/api/followups", headers=CALLER).json()))

    section("cancel")
    r = client.post("/api/jobs", headers=CALLER, json=job(3))
    cancel_id = r.json()["job"]["job_id"]
    r = client.post(f"/api/jobs/{cancel_id}/cancel", headers=CALLER)
    check("a queued job can be cancelled", r.status_code == 200 and r.json()["status"] == "cancelled", f"{r.status_code} {r.text[:120]}")

    if FAST:
        section("lease expiry and unroutable jobs (needs JOB_LEASE_S=3 JOB_UNROUTABLE_AFTER_S=3 JOB_WORKER_LIVE_S=2)")
        r = client.post("/api/jobs", headers=CALLER, json=job(4))
        lease_id = r.json()["job"]["job_id"]
        r = client.post("/api/worker/claim", headers=WORKER, json=worker_body("smoke-worker"))
        check("claimed", r.status_code == 200 and r.json()["job"]["attempt"] == 1, f"{r.status_code}")
        time.sleep(4.5)   # no heartbeat: the lease runs out
        r = client.post("/api/worker/claim", headers=WORKER, json=worker_body("smoke-worker-2"))
        check("after the lease ran out another worker gets it as attempt 2", r.status_code == 200 and r.json()["job"]["attempt"] == 2 and r.json()["job"]["worker_id"] == "smoke-worker-2", f"{r.status_code} {r.text[:160]}")
        client.post(f"/api/worker/jobs/{lease_id}/result", headers=WORKER, json={"worker_id": "smoke-worker-2", "outcome": "done", "summary": "smoke"})
        orphan = f"nobody-{RUN}@example.com"
        ACCOUNTS.append(orphan)
        r = client.post("/api/jobs", headers=CALLER, json=job(5, account=orphan))
        orphan_id = r.json()["job"]["job_id"]
        check("a job for an account no worker has is flagged at once as not routable", r.json()["routable"] is False)
        time.sleep(3.5)
        r = client.get(f"/api/jobs/{orphan_id}", headers=CALLER)
        check("after the wait it is shown as needing a person (NO_WORKER), with no scheduled job involved",
              r.status_code == 200 and r.json()["status"] == "needs_manual" and r.json()["follow_up"]["code"] == "NO_WORKER", f"{r.status_code} {r.text[:200]}")
        r = client.post(f"/api/jobs/{orphan_id}/cancel", headers=CALLER)
        check("and it can be cancelled", r.status_code == 200 and r.json()["status"] == "cancelled", f"{r.status_code} {r.text[:120]}")

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED:\n  - " + "\n  - ".join(failures))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
