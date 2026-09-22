"""
Desktop-side HTTP client for the apply-assist runner (pipeline.jobs.assist, T-025).

Talks to the signalslate API from the user's own machine, so it is the "opposite" of every
server-side pipeline/ module: a human is watching this run live, so every failure — a connection
refused, a timeout, a non-2xx response — must surface clearly rather than degrade gracefully into
a silent no-op or a malformed dict.

Design decisions:

- base_url defaults to the SIGNALSLATE_API_URL environment variable, read directly with
  os.environ.get. This is desktop-side code, not the server process, so it does not go through
  pipeline.health.env()'s overlay (a server-only concept) — there is no server .env to overlay
  here, just whatever the user's shell/launcher sets.
- session is injectable (Optional[requests.Session] = None, defaulting to a real
  requests.Session()) so tests can pass Starlette's TestClient instead: both requests.Session and
  httpx.Client (which TestClient subclasses) expose `.request(method, url, **kwargs)` returning a
  response with `.status_code`, `.json()` and `.content`, which is all this client ever calls, so
  the two are interchangeable here without any adapter shim.
- The auth model is this codebase's only one (api/security.py): no bearer token or login, just the
  X-Requested-With: signalslate header (every non-safe-method request needs it) plus the host
  allowlist enforced server-side. The header name/value are duplicated here as literals rather than
  imported from api.security, keeping this desktop package's import graph independent of the
  server's api/ package (mirrors this package's own Playwright-isolation rule for __init__.py:
  minimal, self-contained imports).
- Every non-2xx response raises AssistApiError with the server's {code, message} body (or its raw
  text when the body isn't the usual coded-error shape) instead of returning a malformed dict or
  swallowing the failure — see module docstring above.
- fetch_application composes three GETs (applications list, postings list, profile) rather than
  requiring a new "give me everything" server endpoint: neither GET /jobs/applications nor
  GET /jobs/postings exposes a by-id lookup, only list+filter (api/routers/job_apply.py,
  api/routers/jobs.py), and this task's scope excludes touching those routers. The runner still
  gets everything it needs to work: the application row (incl. its packet/cover-letter/screening
  drafts), its posting (incl. ats_kind, for pipeline.jobs.apply.get_playbook(ats_kind), which the
  caller invokes locally — it is a pure function, no network) and a profile subset that already
  excludes raw resume bytes (JobsProfileOut only ever carries resume_paths, server-side file paths,
  never file content).
- download_file reads the whole response body into memory via `.content` and writes it in one
  `open(...).write()` rather than a chunked/streaming download: resumes and cover letters are
  capped well under 5 MB server-side (api/routers/job_apply.py's _RESUME_MAX_BYTES), and a uniform
  `.content` read is the one code path both requests.Response and httpx.Response (TestClient)
  support identically — requests' `stream=True` kwarg has no httpx.Client.request() equivalent.
"""
import os
from typing import Any, Optional

import requests

REQUIRED_HEADER = "X-Requested-With"
REQUIRED_HEADER_VALUE = "signalslate"

DEFAULT_BASE_URL = "http://127.0.0.1:8000"


class AssistApiError(RuntimeError):
    """
    Raised for any non-2xx response the signalslate API returns to an AssistClient call.

    Args:
        message: Human-readable summary, already including the method/path/status.
        status_code: The HTTP status code, when known.
        code: The server's short error code (e.g. "not_queued"), when the body carried one.
    """

    def __init__(self, message: str, *, status_code: Optional[int] = None, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code

    @classmethod
    def from_response(cls, method: str, path: str, response: Any) -> "AssistApiError":
        """
        Builds an AssistApiError from a non-2xx requests/httpx response.

        Args:
            method: The HTTP method that was sent (e.g. "POST").
            path: The request path (without base_url) that was sent.
            response: The requests.Response or httpx.Response received.

        Returns:
            An AssistApiError describing the failure, extracting {code, message} from the body's
            `detail` when the server used its usual coded-error shape (api.routers.*'s `_coded`
            convention), falling back to the raw response text otherwise.
        """
        status_code = getattr(response, "status_code", None)
        try:
            body = response.json()
        except ValueError:
            body = None
        detail = body.get("detail") if isinstance(body, dict) else None
        code: Optional[str] = None
        if isinstance(detail, dict) and "code" in detail:
            code = detail.get("code")
            reason = detail.get("message") or code
        elif detail is not None:
            reason = str(detail)
        else:
            reason = getattr(response, "text", "") or f"HTTP {status_code}"
        return cls(f"{method} {path} -> {status_code}: {reason}", status_code=status_code, code=code)


class AssistClient:
    """
    HTTP client the desktop apply-assist runner uses to talk to the signalslate API.

    Args:
        base_url: API root, e.g. "http://127.0.0.1:8000". Defaults to the SIGNALSLATE_API_URL
            environment variable, or DEFAULT_BASE_URL when that is unset.
        session: Injectable HTTP session (a requests.Session or duck-type-compatible object such
            as Starlette's TestClient). Defaults to a real requests.Session().
    """

    def __init__(self, base_url: Optional[str] = None, session: Optional[Any] = None) -> None:
        resolved = base_url if base_url is not None else os.environ.get("SIGNALSLATE_API_URL", DEFAULT_BASE_URL)
        self.base_url = resolved.rstrip("/")
        self.session = session if session is not None else requests.Session()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """
        Sends one request with the required auth header, raising AssistApiError on a non-2xx reply.

        Args:
            method: HTTP method, e.g. "GET".
            path: Request path, e.g. "/api/jobs/assist/queue" (joined onto self.base_url as-is).
            **kwargs: Passed through to `self.session.request` (e.g. `json=...`).

        Returns:
            The successful response object (requests.Response or the injected session's own type).

        Raises:
            AssistApiError: the response status code was >= 400.
            Exception: whatever `self.session.request` itself raises on a connection failure,
                timeout, or similar (deliberately not caught here — see module docstring).
        """
        headers = {**kwargs.pop("headers", {}), REQUIRED_HEADER: REQUIRED_HEADER_VALUE}
        response = self.session.request(method, f"{self.base_url}{path}", headers=headers, **kwargs)
        if response.status_code >= 400:
            raise AssistApiError.from_response(method, path, response)
        return response

    def next_queued(self) -> Optional[dict]:
        """
        The next application waiting for the desktop runner to claim.

        Returns:
            The application dict from GET /api/jobs/assist/queue, or None when the queue is empty
            (the route itself returns JSON null for an empty queue, not a 404).

        Raises:
            AssistApiError: the API returned a non-2xx response.
        """
        return self._request("GET", "/api/jobs/assist/queue").json()

    def claim(self, application_id: int) -> dict:
        """
        Claims a queued application for this runner session.

        Args:
            application_id: The JobApplication id to claim.

        Returns:
            The updated application dict (assist_state == "claimed", with a fresh
            assist_session_id) from POST /api/jobs/assist/queue/{id}/claim.

        Raises:
            AssistApiError: 404 if the application doesn't exist, 409 if it is not currently
                queued (already claimed by another poll, or never queued).
        """
        return self._request("POST", f"/api/jobs/assist/queue/{application_id}/claim").json()

    def fetch_application(self, application_id: int) -> dict:
        """
        Everything the runner needs to work one application, composed from three GETs (see module
        docstring for why this client composes rather than the API growing a new endpoint).

        Args:
            application_id: The JobApplication id to fetch.

        Returns:
            {"application": <ApplicationOut dict>, "posting": <PostingOut dict, or None if the
            posting is somehow missing>, "profile": <JobsProfileOut dict>}.

        Raises:
            AssistApiError: application_id was not found among GET /api/jobs/applications (code
                "application_not_found"), or any of the three GETs returned a non-2xx response.
        """
        applications = self._request("GET", "/api/jobs/applications").json()
        application = next((row for row in applications if row["id"] == application_id), None)
        if application is None:
            raise AssistApiError(
                f"application {application_id} not found in GET /api/jobs/applications",
                status_code=404,
                code="application_not_found",
            )
        postings = self._request("GET", "/api/jobs/postings").json()
        posting = next((row for row in postings if row["id"] == application["posting_id"]), None)
        profile = self._request("GET", "/api/jobs/profile").json()
        return {"application": application, "posting": posting, "profile": profile}

    def download_file(self, application_id: int, kind: str, dest_path: str) -> str:
        """
        Downloads one application's cover-letter or resume PDF to dest_path.

        Args:
            application_id: The JobApplication id.
            kind: "cover_letter" or "resume" (matches api/routers/job_apply.py's file kinds).
            dest_path: Local filesystem path to write the file's bytes to.

        Returns:
            dest_path, for chaining.

        Raises:
            AssistApiError: 404 if the application doesn't exist, the file was never recorded, or
                the recorded file is missing on disk.
            OSError: dest_path could not be written.
        """
        response = self._request("GET", f"/api/jobs/applications/{application_id}/files/{kind}")
        with open(dest_path, "wb") as fh:
            fh.write(response.content)
        return dest_path

    def report_progress(
        self,
        session_id: str,
        *,
        step: Optional[str] = None,
        assist_state: Optional[str] = None,
        filled_fields: Optional[list] = None,
        approved_answers: Optional[list] = None,
    ) -> dict:
        """
        Reports the runner's progress for one assist session. Only the fields passed are sent —
        an omitted (None) keyword is left out of the request body entirely, not sent as JSON null,
        so the server's AssistSessionUpdateBody Optional[...] = None fields keep their existing
        stored values (api/routers/job_apply.py's update_assist_session updates only fields
        provided).

        Args:
            session_id: The assist_session_id this runner was given by claim().
            step: Current playbook step id, when reporting one.
            assist_state: One of idle/queued/claimed/running/paused/done/failed, when changing it.
            filled_fields: List of {field_id, value, source, confidence} dicts, when reporting any.
            approved_answers: List of {question, answer} dicts the user approved, when reporting any.

        Returns:
            The updated application dict from PATCH /api/jobs/assist/sessions/{session_id}.

        Raises:
            AssistApiError: 404 if no application currently holds this session id, 422 if
                assist_state is not one of the recognized values.
        """
        body: dict[str, Any] = {}
        if step is not None:
            body["step"] = step
        if assist_state is not None:
            body["assist_state"] = assist_state
        if filled_fields is not None:
            body["filled_fields"] = filled_fields
        if approved_answers is not None:
            body["approved_answers"] = approved_answers
        return self._request("PATCH", f"/api/jobs/assist/sessions/{session_id}", json=body).json()
