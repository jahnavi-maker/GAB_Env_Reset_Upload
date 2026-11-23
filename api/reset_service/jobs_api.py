"""HTTP routes for module jobs (see jobs.py and docs/MODULE_JOBS.md).

Two kinds of caller, two keys:
  * callers (this platform's reset flow, QC tooling): the platform's existing RESET_API_KEY. Enqueue, read, follow-ups.
  * workers (a machine holding signed-in browser profiles): their own WORKER_API_KEY. Claim, heartbeat, report.
Neither key opens the other's routes. The worker routes stay off (503) until WORKER_API_KEY is set.
"""
from __future__ import annotations

import hmac
from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request, Response

from .jobs import InvalidJob, JobError, JobService, NotFound
from .jobs_files import PNG_MAGIC, FileStore


def make_bearer_dependency(get_key: Callable[[], str]) -> Callable[..., None]:
    """A FastAPI dependency that checks ``Authorization: Bearer <key>`` against ``get_key()``, read on every request.
    No key configured means the route is off (503), never open."""

    def dependency(authorization: str | None = Header(default=None)) -> None:
        key = get_key()
        if not key:
            raise HTTPException(status_code=503, detail="this endpoint is not configured")
        if not authorization or not hmac.compare_digest(authorization.encode(), f"Bearer {key}".encode()):
            raise HTTPException(status_code=401, detail="missing or invalid bearer token", headers={"WWW-Authenticate": "Bearer"})

    return dependency


def _http(error: JobError) -> HTTPException:
    if isinstance(error, InvalidJob):
        return HTTPException(status_code=422, detail=str(error))   # names the field, never the value
    if isinstance(error, NotFound):
        return HTTPException(status_code=404, detail=str(error))
    return HTTPException(status_code=409, detail=str(error))       # Conflict and the state errors


def build_router(get_service: Callable[[], JobService], require_caller: Callable[..., Any], require_worker: Callable[..., Any],
                 get_files: Callable[[], FileStore] | None = None) -> APIRouter:
    router = APIRouter()
    caller = [Depends(require_caller)]
    worker = [Depends(require_worker)]

    async def call(action):
        try:
            return await action(get_service())
        except JobError as error:
            raise _http(error) from None

    # ---------------------------------------------------------------- callers
    @router.post("/api/jobs", dependencies=caller)
    async def enqueue(response: Response, payload: dict = Body(...)):
        out = await call(lambda svc: svc.enqueue(payload))
        response.status_code = 201 if out["created"] else 200
        return out

    @router.get("/api/jobs", dependencies=caller)
    async def list_jobs(status: str | None = None, reset_session_id: str | None = None):
        return await call(lambda svc: svc.list_jobs(status=status, reset_session_id=reset_session_id))

    @router.get("/api/jobs/{job_id}", dependencies=caller)
    async def get_job(job_id: str):
        return await call(lambda svc: svc.get(job_id))

    @router.get("/api/followups", dependencies=caller)
    async def follow_ups():
        return await call(lambda svc: svc.follow_ups())

    @router.post("/api/jobs/{job_id}/resolve", dependencies=caller)
    async def resolve(job_id: str, payload: dict = Body(...)):
        return await call(lambda svc: svc.resolve(job_id, payload.get("note")))

    @router.post("/api/jobs/{job_id}/retry", dependencies=caller)
    async def retry(job_id: str):
        return await call(lambda svc: svc.retry(job_id))

    @router.post("/api/jobs/{job_id}/cancel", dependencies=caller)
    async def cancel(job_id: str):
        return await call(lambda svc: svc.cancel(job_id))

    @router.get("/api/jobs/{job_id}/screenshot", dependencies=caller)
    async def screenshot(job_id: str):
        files = get_files() if get_files else None
        data = files.get(job_id) if files else None
        if data is None:
            raise HTTPException(status_code=404, detail="no screenshot for this job")
        return Response(content=data, media_type="image/png", headers={"Cache-Control": "no-store"})

    # ---------------------------------------------------------------- workers
    @router.post("/api/worker/claim", dependencies=worker)
    async def claim(payload: dict = Body(...)):
        job = await call(lambda svc: svc.claim(payload))
        return {"job": job} if job else Response(status_code=204)

    @router.post("/api/worker/jobs/{job_id}/heartbeat", dependencies=worker)
    async def heartbeat(job_id: str, payload: dict = Body(...)):
        return await call(lambda svc: svc.heartbeat(job_id, str(payload.get("worker_id") or "")))

    @router.post("/api/worker/jobs/{job_id}/result", dependencies=worker)
    async def result(job_id: str, payload: dict = Body(...)):
        return await call(lambda svc: svc.result(
            job_id, str(payload.get("worker_id") or ""), str(payload.get("outcome") or ""),
            summary=payload.get("summary"), follow_up=payload.get("follow_up"), pack_version=payload.get("pack_version")))

    @router.post("/api/worker/jobs/{job_id}/screenshot", dependencies=worker, status_code=201)
    async def upload_screenshot(job_id: str, request: Request, x_worker_id: str | None = Header(default=None)):
        files = get_files() if get_files else None
        if files is None:
            raise HTTPException(status_code=503, detail="screenshots are not configured")
        await call(lambda svc: svc.holds(job_id, x_worker_id or ""))   # 404 unknown job, 409 not held by this worker
        too_large = HTTPException(status_code=413, detail=f"the screenshot is larger than {files.max_bytes} bytes")
        if int(request.headers.get("content-length") or 0) > files.max_bytes:   # refuse before reading the body
            raise too_large
        body = await request.body()
        if len(body) > files.max_bytes:
            raise too_large
        if not body.startswith(PNG_MAGIC):
            raise HTTPException(status_code=422, detail="the screenshot must be a PNG image")
        files.put(job_id, body)
        return {"url": f"/api/jobs/{job_id}/screenshot"}

    return router
