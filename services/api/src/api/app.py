"""Public API (`/api/v1`). Identity is the verified JWT `sub`, never a body or path field. Reads go
through the caller's own database role so RLS applies; run-scoped routes share one visibility
dependency that answers 404 for both missing and forbidden runs (no existence oracle)."""

import asyncio
import bisect
import difflib
import hashlib
import io
import ipaddress
import json
import logging
import re
import secrets
import uuid
import zipfile
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Path, Query, Request, Response, WebSocket
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from ingest.app import BodyLimit, problem
from labharness.agent import RunConfig
from labharness.presets import CUSTOM_LABEL, MAX_CUSTOM_CHARS, PRESETS, custom_preset, get_preset
from labharness.providers.registry import PROVIDER_MODELS, check_model
from psycopg.errors import RaiseException
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from runlog.eventlog import EventLogReader
from runlog.lifecycle import FINAL
from runlog.redaction import ScrubFilter, scan_bytes
from runlog.store import NotFound as StoreNotFound
from runlog.store import ObjectStore

from .accounts_client import AccountsClient, AccountsError
from .auth import AuthGateway, JwtVerifier, SessionChecker, Unauthenticated, User
from .db import Conn, Database
from .limits import RateLimiter
from .mailjobs import MailJobs
from .orchestrator_client import KeyValidator, OrchestratorClient, SubmitUnknown, UpstreamError
from .settings import Settings
from .signing import LocalUrlSigner, UrlSigner
from .stats import compare, summarize
from .stream import (
    MAX_CLIENT_MESSAGE_BYTES,
    TICKET_TTL_S,
    SocketCounter,
    new_ticket,
    ticket_hash,
    well_formed,
)

log = logging.getLogger("api")
log.addFilter(ScrubFilter())
MAX_BODY = 64 * 1024
API = "/api/v1"
SIGNED_URL_TTL_S = 60
MAX_COMPARE_EVENTS = 20_000      # per run: bounds the work of one compare request
MAX_COMPARE_STEPS = 5_000
MAX_EXPORT_BYTES = 64 * 1024 * 1024        # the store's own object cap; also bounds the work of a re-scan
_FRAME_KEY = re.compile(r"^runs/[0-9a-f-]{36}/frames/(\d{7})\.png$")
_ACTIVE = ("queued", "starting", "running", "session_boundary", "dreaming", "stopping")   # runlog.lifecycle minus FINAL


def _has_nul(value: Any) -> bool:
    if isinstance(value, SecretStr):
        value = value.get_secret_value()
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(_has_nul(k) or _has_nul(v) for k, v in value.items())
    if isinstance(value, list | tuple):
        return any(_has_nul(v) for v in value)
    return False


class Body(BaseModel):
    """Every request body: unknown fields refused, no coercion, and no NUL byte in any string (Postgres text
    cannot hold one, so it would surface as a 500 DataError instead of a 422)."""
    model_config = ConfigDict(extra="forbid", strict=True)

    @model_validator(mode="after")
    def _no_nul(self) -> Any:
        if _has_nul(dict(self)):
            raise ValueError("NUL byte in input")
        return self


class CapsIn(Body):
    max_steps: int = Field(ge=1, le=100_000)
    max_tokens: int = Field(ge=1, le=1_000_000_000)
    max_wall_s: int = Field(ge=1, le=86_400)           # strict int: 0.5 or "60" is a 422 before any quota is used


class ValidateIn(Body):
    config: dict[str, Any]
    caps: CapsIn | None = None
    preset_text: str | None = Field(default=None, max_length=MAX_CUSTOM_CHARS)


class CreateRunIn(ValidateIn):
    game: str = Field(max_length=32)
    visibility: Literal["private", "unlisted", "public"] = "private"
    api_key: SecretStr = Field(min_length=8, max_length=512)


class ForkIn(Body):
    checkpoint_id: str = Field(min_length=1, max_length=64)
    config_patch: dict[str, Any]
    caps: CapsIn | None = None
    preset_text: str | None = Field(default=None, max_length=MAX_CUSTOM_CHARS)
    visibility: Literal["private", "unlisted", "public"] = "private"
    api_key: SecretStr = Field(min_length=8, max_length=512)


# the top-level fields of a run configuration: an experiment's variable is one of them
EXPERIMENT_VARIABLES = frozenset({"provider", "model_id", "preset_ref", "memory", "inject_path", "inject_cap_chars",
                                  "observation", "screenshots", "caps", "sessions", "mode"})


class ExperimentIn(Body):
    title: str = Field(min_length=1, max_length=120)
    variable: str = Field(min_length=1, max_length=64)
    run_ids: list[str] = Field(min_length=1, max_length=50)
    visibility: Literal["private", "unlisted", "public"] = "private"


class ExperimentPatchIn(Body):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    visibility: Literal["private", "unlisted", "public"] | None = None
    add_run_ids: list[str] = Field(default_factory=list, max_length=50)
    remove_run_ids: list[str] = Field(default_factory=list, max_length=50)


class PatchRunIn(Body):
    title: str | None = Field(default=None, max_length=120)
    visibility: Literal["private", "unlisted", "public"] | None = None


class SignupIn(Body):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: SecretStr = Field(min_length=12, max_length=128)


class EmailIn(Body):
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


SIGNUP_MESSAGE = "If an account exists, we've sent an email."


class ReportIn(Body):
    reason: str = Field(min_length=1, max_length=1000)


class BreakerIn(Body):
    open: bool
    reason: str = Field(min_length=1, max_length=200)          # a reason for closing the breaker too: every action is audited


class DeleteAccountIn(Body):
    confirm: Literal["DELETE"]


class AdminKillIn(Body):
    reason: str = Field(min_length=1, max_length=200)


class ReportResolveIn(Body):
    status: Literal["actioned", "dismissed"]
    reason: str = Field(min_length=1, max_length=500)


class UnverifyIn(Body):
    reason: str = Field(min_length=1, max_length=500)


_REFUNDED_REFUSALS = ("capacity_full", "breaker_open", "user_busy", "ip_busy")


class NotFound(Exception):
    pass


def _uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise NotFound from None


def _client_ip(request: Request, hops: int = 0) -> str:
    """The socket peer, or with `hops` trusted proxies in front the address the outermost trusted proxy saw:
    X-Forwarded-For entries to the left of that were supplied by the client and are never used."""
    peer = request.client.host if request.client else "unknown"
    if hops <= 0:
        return peer
    chain = [p.strip() for line in request.headers.getlist("x-forwarded-for") for p in line.split(",") if p.strip()]
    if len(chain) < hops:
        return peer                      # fewer entries than proxies: the header was not set by our gateway
    try:
        return str(ipaddress.ip_address(chain[-hops]))
    except ValueError:
        return peer


def _ip_hash(ip: str, salt: bytes) -> str:
    return hashlib.sha256(salt + ip.encode()).hexdigest()[:32]


RUN_COLS = ("id, owner_id, title, status, visibility, game, parent_checkpoint_id, fork_patch, harness_version, "
            "model_id, provider, cap_steps, cap_tokens, cap_wall_s, steps, tokens, started_at, ended_at, end_reason, "
            "created_at")


def _resolve_config(body: ValidateIn) -> tuple[RunConfig, str | None, dict[str, Any]]:
    """Validate a run configuration. Caps are mandatory and explicit (`caps_required`)."""
    if body.caps is None:
        raise ApiError(422, "caps_required", "caps are required")
    doc = dict(body.config)
    if "caps" in doc:
        raise ApiError(422, "validation_failed", "give caps in `caps`, not in `config`")
    doc["caps"] = {"max_steps": body.caps.max_steps, "max_tokens": body.caps.max_tokens,
                   "max_wall_s": body.caps.max_wall_s}
    try:
        config = RunConfig.from_dict(doc)
        check_model(config.provider, config.model_id)
    except ValueError as exc:
        raise ApiError(422, "validation_failed", str(exc)[:160]) from None
    preset_text: str | None = None
    if config.preset_ref.startswith("custom@"):
        if not body.preset_text:
            raise ApiError(422, "validation_failed", "preset_text is required for a custom preset")
        preset = custom_preset(body.preset_text)
        if config.preset_ref != preset.ref:
            raise ApiError(422, "validation_failed", "preset_ref does not match preset_text")
        preset_text = body.preset_text
    else:
        try:
            get_preset(config.preset_ref)
        except (KeyError, ValueError):
            raise ApiError(422, "validation_failed", "unknown preset") from None
        if body.preset_text is not None:
            raise ApiError(422, "validation_failed", "preset_text is only for custom presets")
    if config.mode == "benchmark" and preset_text is not None:
        raise ApiError(422, "benchmark_patch_invalid", "benchmark mode uses an approved preset")
    return config, preset_text, doc


class ApiError(Exception):
    def __init__(self, status: int, code: str, title: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(code)
        self.status, self.code, self.title, self.headers = status, code, title, headers


# On every response, including the 500 the server-error layer answers (it sits outside the middleware). The API serves JSON and
# redirects, never a page, so the CSP is the strictest one. HSTS is ignored over plain http, so it is safe to send in development.
SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "cache-control": "no-store",
    "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
    "referrer-policy": "no-referrer",
    "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()",
    "strict-transport-security": "max-age=63072000; includeSubDomains",
}


def create_app(settings: Settings, *, verifier: JwtVerifier, db: Database, orchestrator: OrchestratorClient,
               key_validator: KeyValidator, session_checker: SessionChecker, auth_gateway: AuthGateway,
               accounts: AccountsClient, store: ObjectStore | None = None, signer: UrlSigner | None = None, mail_jobs: MailJobs | None = None,
               clock: Callable[[], float] | None = None, ip_salt: bytes | None = None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, title="memorylab-api")
    app.add_middleware(BodyLimit, max_bytes=MAX_BODY)
    salt = ip_salt or secrets.token_bytes(16)
    limiter = RateLimiter(*([clock] if clock else []))

    def client_ip(request: Request) -> str:
        return _client_ip(request, settings.trusted_proxy_hops)

    @app.middleware("http")
    async def headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        resp = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            resp.headers.setdefault(name, value)
        return resp

    @app.exception_handler(ApiError)
    async def api_error(_: Request, exc: ApiError) -> JSONResponse:
        return problem(exc.status, exc.code, exc.title, exc.headers)

    @app.exception_handler(UpstreamError)
    async def upstream(_: Request, exc: UpstreamError) -> JSONResponse:
        headers = {"retry-after": str(exc.retry_after_s)} if exc.retry_after_s else None
        return problem(exc.status, exc.code, exc.code, headers)

    @app.exception_handler(NotFound)
    async def not_found(_: Request, exc: NotFound) -> JSONResponse:
        return problem(404, "not_found", "Not found")

    @app.exception_handler(Unauthenticated)
    async def unauth(_: Request, exc: Unauthenticated) -> JSONResponse:
        return problem(401, "unauthenticated", "Unauthenticated", {"www-authenticate": "Bearer"})

    @app.exception_handler(RequestValidationError)
    async def bad_input(_: Request, exc: RequestValidationError) -> JSONResponse:
        return problem(422, "validation_failed", "Invalid request")      # never echoes input: it may hold a key

    @app.exception_handler(Exception)
    async def unexpected(_: Request, exc: Exception) -> JSONResponse:
        incident = secrets.token_hex(6)
        log.error("unhandled error incident=%s type=%s", incident, type(exc).__name__)
        return JSONResponse({"type": "about:blank", "title": "Internal error", "status": 500, "code": "internal",
                             "incident": incident}, status_code=500, media_type="application/problem+json", headers=SECURITY_HEADERS)

    # ------------------------------------------------------------ dependencies

    def bearer(request: Request) -> User | None:
        header = request.headers.get("authorization")
        if header is None:
            return None
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise Unauthenticated
        return verifier.verify(token)       # a bad token is 401, never silently anonymous

    def require_user(user: Annotated[User | None, Depends(bearer)]) -> User:
        if user is None:
            raise Unauthenticated
        return user

    def read_guard(request: Request, user: Annotated[User | None, Depends(bearer)]) -> None:
        who = user.id if user else f"ip:{_ip_hash(client_ip(request), salt)}"
        if not limiter.allow(("read", who), settings.read_limit_per_min, 60):
            raise ApiError(429, "rate_limited", "Too many requests", {"retry-after": "60"})

    def export_guard(request: Request, user: Annotated[User | None, Depends(bearer)]) -> None:
        """An export re-scans a whole bundle, so it has its own, tighter, per-identity limit."""
        who = user.id if user else f"ip:{_ip_hash(client_ip(request), salt)}"
        if not limiter.allow(("export", who), 10, 60):
            raise ApiError(429, "rate_limited", "Too many requests", {"retry-after": "60"})

    def session(user: Annotated[User | None, Depends(bearer)]) -> Any:
        with db.session(user) as conn:
            yield conn

    Authed = Annotated[User, Depends(require_user)]
    Sess = Annotated[Conn, Depends(session, scope="function")]   # commit before the response is sent: a client that reads right after a write must see it
    def write_guard(user: Authed, conn: Sess) -> None:
        """Every user write: the account must still exist (a token outlives an erase by up to its TTL; that is a 401,
        not a foreign-key failure further down), then the per-user limit."""
        row = conn.execute("select erased_at from public.profiles where id = %s", (user.id,)).fetchone()
        if row is None or row["erased_at"] is not None:
            raise Unauthenticated
        if not limiter.allow(("write", user.id), settings.write_limit_per_min, 60):
            raise ApiError(429, "rate_limited", "Too many requests", {"retry-after": "60"})

    Guard = Depends(read_guard)
    WriteGuard = Depends(write_guard)
    ExportGuard = Depends(export_guard)

    def load_visible_run(run_id: str, conn: Sess) -> dict[str, Any]:
        """The one place a run is looked up for a caller. The database decides visibility (owner, admin, or
        link-only unlisted/public); absent and hidden are indistinguishable (404)."""
        row = conn.execute(f"select {RUN_COLS} from api_private.get_visible_run(%s) where deleted_at is null",
                           (_uuid(run_id),)).fetchone()
        if row is None:
            raise NotFound
        return _public_run(row)

    Run = Annotated[dict[str, Any], Depends(load_visible_run)]

    def owner_run(run: Run, user: Authed) -> dict[str, Any]:
        if run["owner_id"] != user.id:
            raise NotFound               # visible but not theirs: same answer as hidden
        return run

    OwnerRun = Annotated[dict[str, Any], Depends(owner_run)]

    def admin_user(user: Authed, conn: Sess) -> User:
        if not session_checker.is_active(user.token):
            raise Unauthenticated        # revoked/signed-out sessions lose admin access now, not at expiry
        if not Database.is_admin(conn):
            raise NotFound               # admin routes do not exist for non-admins
        return user

    Admin = Annotated[User, Depends(admin_user)]

    # ------------------------------------------------------------ catalogue and account

    @app.get(f"{API}/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(f"{API}/me", dependencies=[Guard])
    def me(user: Authed, conn: Sess) -> dict[str, Any]:
        prof = conn.execute("select id, handle, role, created_at from public.profiles where id = %s",
                            (user.id,)).fetchone()
        if prof is None:
            raise Unauthenticated               # a token for an erased account
        active = conn.execute("select count(*) n from public.runs where owner_id = %s and status = any(%s) "
                              "and deleted_at is null", (user.id, list(_ACTIVE))).fetchone()["n"]  # type: ignore[index]
        return {**_jsonable(prof), "active_runs": active, "limits": {
            "concurrent_runs": 1, "runs_per_hour": settings.run_create_per_user_per_hour,
            "reads_per_minute": settings.read_limit_per_min}}

    @app.delete(f"{API}/me", status_code=204, dependencies=[WriteGuard])
    def delete_me(body: DeleteAccountIn, user: Authed) -> Response:
        """Erase the caller's account. The session is re-checked first, so a token that outlived a sign-out (or a
        previous erase) cannot do this; the service-role work happens in the accounts service."""
        if not session_checker.is_active(user.token):
            raise Unauthenticated
        if not limiter.allow(("erase", user.id), 3, 3600):
            raise ApiError(429, "rate_limited", "Too many attempts", {"retry-after": "3600"})
        try:
            accounts.erase(user.id)
        except AccountsError as exc:
            headers = {"retry-after": str(exc.retry_after_s)} if exc.retry_after_s else None
            if exc.code == "runs_still_active":
                raise ApiError(409, "runs_still_active", "Your runs are still stopping; try again shortly", headers) from None
            raise ApiError(exc.status, "internal", "Try again later", headers) from None
        return Response(status_code=204)

    @app.get(f"{API}/games", dependencies=[Guard])
    def games(conn: Sess) -> list[dict[str, Any]]:
        rows = conn.execute("select id, name, licence, state_map_version from public.games order by name").fetchall()
        return [_jsonable(r) for r in rows]

    @app.get(API + "/games/{game_id}/start-states", dependencies=[Guard])
    def start_states(game_id: str, conn: Sess) -> list[dict[str, Any]]:
        rows = conn.execute("select id, label, savestate_sha256 from public.start_states where game_id = %s",
                            (_uuid(game_id),)).fetchall()
        return [_jsonable(r) for r in rows]

    @app.get(f"{API}/presets", dependencies=[Guard])
    def presets() -> list[dict[str, Any]]:
        return [{"id": p.id, "version": p.version, "ref": p.ref, "text": p.text, "text_sha256": p.text_sha256,
                 "label": p.label, "is_adaptation": p.is_adaptation} for p in PRESETS.values()]

    @app.get(f"{API}/features", dependencies=[Guard])
    def features() -> dict[str, bool]:
        """What this deployment has switched on, so a client can hide what would only be refused."""
        return {"fork": settings.fork_enabled}

    @app.get(f"{API}/models", dependencies=[Guard])
    def models() -> dict[str, Any]:
        return {"providers": {k: list(v) for k, v in PROVIDER_MODELS.items()},
                "price_table": {"version": None, "as_of": None, "note": "no price table loaded; no cost estimate shown"}}

    @app.post(f"{API}/auth/signup", status_code=202)
    def signup(body: SignupIn, request: Request) -> dict[str, str]:
        """One answer for new and existing emails. An existing account gets a recovery email instead of an error."""
        ip = _ip_hash(client_ip(request), salt)
        if not limiter.allow(("signup-ip", ip), settings.signup_per_ip_per_hour, 3600):
            raise ApiError(429, "rate_limited", "Too many attempts", {"retry-after": "3600"})
        email, password = body.email.lower(), body.password.get_secret_value()

        def job() -> None:
            if not auth_gateway.signup(email, password):
                auth_gateway.recover(email)

        mail.submit("signup", email, job)                         # password rules were checked by SignupIn (12..128 chars)
        return {"message": SIGNUP_MESSAGE}

    mail = mail_jobs or MailJobs()

    def mail_endpoint(kind: str, send: Callable[[str], None]) -> Callable[[EmailIn, Request], dict[str, str]]:
        def handler(body: EmailIn, request: Request) -> dict[str, str]:
            """One answer whatever the address is: the limits apply to every address alike (per IP, and per address so the
            endpoint cannot be used to flood a mailbox), so none of them says whether an account exists."""
            email = body.email.lower()
            ip = _ip_hash(client_ip(request), salt)
            if not limiter.allow((f"{kind}-ip", ip), settings.signup_per_ip_per_hour, 3600) or \
                    not limiter.allow((f"{kind}-email", hashlib.sha256(email.encode()).hexdigest()), 5, 3600):
                raise ApiError(429, "rate_limited", "Too many attempts", {"retry-after": "3600"})
            mail.submit(kind, email, lambda: send(email))       # after the answer: its duration says nothing about the address
            return {"message": SIGNUP_MESSAGE}
        return handler

    app.post(f"{API}/auth/magic-link", status_code=202)(mail_endpoint("magic", auth_gateway.magic_link))
    app.post(f"{API}/auth/recover", status_code=202)(mail_endpoint("recover", auth_gateway.recover))

    # ------------------------------------------------------------ runs

    @app.post(f"{API}/runs/validate", dependencies=[Guard])
    def validate_run(body: ValidateIn, user: Authed) -> dict[str, Any]:
        config, _, doc = _resolve_config(body)
        return {"config": doc, "config_hash": config.config_hash, "mode": config.mode,
                "benchmark_eligible": config.mode == "benchmark" and config.preset_ref in PRESETS}

    def launch(conn: Conn, user: User, ip: str, *, game: str, config: RunConfig, doc: dict[str, Any],
               preset_text: str | None, visibility: str, api_key: SecretStr,
               parent: dict[str, Any] | None = None, fork_patch: dict[str, Any] | None = None) -> dict[str, Any]:
        """The one create path (new run and fork): lockout, one-active-run, quota, key check, the api_private
        function, commit, submit. Everything before the function call costs nothing but the read guard."""
        if not _account_is_live(conn, user):                      # before any quota or provider call is spent
            raise Unauthenticated
        if limiter.count(("keyfail", user.id), 3600) >= settings.key_failures_per_user_per_hour:
            raise ApiError(429, "rate_limited", "Run creation is locked for an hour", {"retry-after": "3600"})
        if _has_active_run(conn, user):                           # cheap early answer; the DB function is the authority
            raise _active_run()
        create_keys = (("create", user.id), ("create-ip", ip))
        if not limiter.allow_all([(create_keys[0], settings.run_create_per_user_per_hour, 3600),
                                  (create_keys[1], settings.run_create_per_ip_per_hour, 3600)]):
            raise ApiError(429, "rate_limited", "Too many runs", {"retry-after": "3600"})
        refund = True                                             # every exit below refunds unless the run was accepted
        try:
            if not key_validator.validate(config.provider, api_key.get_secret_value()):
                refund = False
                limiter.record(("keyfail", user.id), 3600)
                raise ApiError(422, "key_rejected", "The key was not accepted")   # one generic answer, always
            try:
                run_id = _create_run_request(conn, game, visibility, config, doc,
                                             str(parent["checkpoint_id"]) if parent else None, fork_patch)
            except RaiseException as exc:
                if "active_run_exists" in str(exc):
                    raise _active_run() from None
                if "account_erased" in str(exc):
                    raise Unauthenticated from None
                if "caps_out_of_range" in str(exc):
                    raise ApiError(422, "validation_failed", "caps out of range") from None
                if "parent_checkpoint_invalid" in str(exc):
                    raise NotFound from None
                raise
            conn.commit()     # the run row must be visible to ingest before the orchestrator mints a token
            Database.apply_identity(conn, user)
            refund = False
            payload: dict[str, Any] = {
                "run_id": run_id, "user_id": user.id, "ip_hash": ip, "game": game,
                "run_config": config.to_dict(), "preset_text": preset_text,
                "provider": {"name": config.provider, "api_key": api_key.get_secret_value()}}
            if parent is not None:
                payload["parent"] = parent
            try:
                orchestrator.submit(payload)
            except SubmitUnknown:
                # the orchestrator may have accepted it: do not fail the row or refund the quota (a sandbox may be
                # running on this key); ask for a kill and leave the row to the reaper
                try:
                    orchestrator.kill(run_id, "system", "submit_unknown")
                except Exception:  # noqa: BLE001 - best effort; the reaper is the backstop
                    log.warning("could not kill an ambiguous submit run=%s", run_id)
                raise ApiError(503, "internal", "Try again later", {"retry-after": "30"}) from None
            except Exception as exc:
                # a refused submit must not leave a queued row that blocks the user until the reaper runs
                conn.execute("select api_private.abandon_run_request(%s)", (run_id,))
                conn.commit()
                Database.apply_identity(conn, user)
                refund = isinstance(exc, UpstreamError) and exc.code in _REFUNDED_REFUSALS
                if isinstance(exc, UpstreamError) and exc.code in ("user_busy", "ip_busy"):
                    # the orchestrator frees a finished run's slot a moment after the run row turns final: the same answer as an
                    # active run, with a short retry (it surfaced as a 502 when a fork was submitted right after its parent ended)
                    raise ApiError(409, "rate_limited", "You already have an active run", {"retry-after": "5"}) from None
                raise
            return {"run_id": run_id, "status": "queued"}
        finally:
            if refund:
                for key in create_keys:
                    limiter.refund(key)

    @app.post(f"{API}/runs", status_code=202, dependencies=[Guard])
    def create_run(body: CreateRunIn, request: Request, user: Authed, conn: Sess) -> dict[str, Any]:
        ip = _ip_hash(client_ip(request), salt)
        config, preset_text, doc = _resolve_config(body)          # invalid requests cost nothing but the read guard
        if not conn.execute("select 1 from public.games where name = %s", (body.game,)).fetchone():
            raise ApiError(422, "validation_failed", "unknown game")
        return launch(conn, user, ip, game=body.game, config=config, doc=doc, preset_text=preset_text,
                      visibility=body.visibility, api_key=body.api_key)

    @app.post(API + "/runs/{run_id}/fork", status_code=202, dependencies=[Guard])
    def fork_run(user: Authed, run: Run, body: ForkIn, request: Request, conn: Sess) -> dict[str, Any]:
        """A new run that starts from one of this run's checkpoints, with its config changed by `config_patch`.
        The caller must be able to see the run; the checkpoint must belong to it (same 404 as a missing one); the
        parent checkpoint is set here, never taken from the body. Disabled by default: kept as an operator switch (MEMORYLAB_FORK_ENABLED=0)."""
        if not settings.fork_enabled:
            raise ApiError(422, "validation_failed", "forking is not available yet")
        ip = _ip_hash(client_ip(request), salt)
        if body.caps is None:
            raise ApiError(422, "caps_required", "caps are required")
        if "caps" in body.config_patch:
            raise ApiError(422, "validation_failed", "give caps in `caps`, not in `config_patch`")
        if not run.get("game"):
            raise ApiError(422, "validation_failed", "this run has no recorded game to fork")
        checkpoint = conn.execute("select id, step from api_private.run_checkpoint(%s, %s)",
                                  (run["id"], _uuid(body.checkpoint_id))).fetchone()
        source = conn.execute("select config_json, mode from api_private.run_config(%s)", (run["id"],)).fetchone()
        if checkpoint is None or source is None:
            raise NotFound
        old: dict[str, Any] = dict(source["config_json"])
        changed = {k: v for k, v in body.config_patch.items() if old.get(k) != v}
        new_caps = {"max_steps": body.caps.max_steps, "max_tokens": body.caps.max_tokens,
                    "max_wall_s": body.caps.max_wall_s}
        if any(old.get("caps", {}).get(k) != v for k, v in new_caps.items()):
            changed["caps"] = new_caps
        merged = {k: v for k, v in {**old, **body.config_patch}.items() if k != "caps"}
        config, preset_text, doc = _resolve_config(
            ValidateIn(config=merged, caps=body.caps, preset_text=body.preset_text))
        if (source["mode"] == "benchmark" or config.mode == "benchmark") and len(changed) > 1:
            raise ApiError(422, "benchmark_patch_invalid", "a benchmark fork may change one field only")
        return launch(conn, user, ip, game=run["game"], config=config, doc=doc, preset_text=preset_text,
                      visibility=body.visibility, api_key=body.api_key, fork_patch=changed,
                      parent={"run_id": run["id"], "checkpoint_id": str(checkpoint["id"]), "step": checkpoint["step"]})

    @app.get(f"{API}/runs", dependencies=[Guard])
    def list_runs(user: Authed, conn: Sess, limit: Annotated[int, Query(ge=1, le=100)] = 25,
                  before: str | None = None) -> dict[str, Any]:
        cursor = None if before is None else _cursor(before)
        rows = conn.execute(
            f"select {RUN_COLS} from public.runs where owner_id = %s and deleted_at is null "
            "and (%s::timestamptz is null or (created_at, id) < (%s::timestamptz, %s::uuid)) "
            "order by created_at desc, id desc limit %s",
            (user.id, cursor and cursor[0], cursor and cursor[0], cursor and cursor[1], limit + 1)).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        nxt = f"{rows[-1]['created_at'].isoformat()}|{rows[-1]['id']}" if more else None
        return {"runs": [_public_run(r) for r in rows], "next": nxt}

    @app.get(API + "/runs/{run_id}", dependencies=[Guard])
    def get_run(run: Run) -> dict[str, Any]:
        return run

    @app.patch(API + "/runs/{run_id}", dependencies=[WriteGuard])
    def patch_run(body: PatchRunIn, run: OwnerRun, conn: Sess) -> dict[str, Any]:
        if body.title is None and body.visibility is None:
            raise ApiError(422, "validation_failed", "nothing to change")
        try:
            row = conn.execute(
                f"update public.runs set title = coalesce(%s, title), visibility = coalesce(%s, visibility) "
                f"where id = %s returning {RUN_COLS}", (body.title, body.visibility, run["id"])).fetchone()
        except RaiseException as exc:
            if "benchmark_visibility_locked" in str(exc):
                raise ApiError(422, "benchmark_patch_invalid", "a leaderboard run cannot be made private") from None
            raise
        if row is None:
            raise NotFound
        return _public_run(row)

    @app.post(API + "/runs/{run_id}/stop", dependencies=[WriteGuard])
    def stop_run(run_id: str, user: Authed, conn: Sess) -> dict[str, Any]:
        run = load_visible_run(run_id, conn)
        if run["owner_id"] != user.id and not Database.is_admin(conn):
            raise NotFound
        return {"result": orchestrator.stop(run["id"], user.id)}      # idempotent upstream

    @app.delete(API + "/runs/{run_id}", status_code=204, dependencies=[WriteGuard])
    def delete_run(run: OwnerRun, conn: Sess) -> Response:
        conn.execute("select public.soft_delete_run(%s)", (run["id"],))
        return Response(status_code=204)

    @app.get(API + "/runs/{run_id}/events", dependencies=[Guard])
    def events(run: Run, after_seq: Annotated[int, Query(ge=0)] = 0,
               limit: Annotated[int, Query(ge=1, le=1000)] = 1000) -> dict[str, Any]:
        if store is None:
            raise ApiError(503, "internal", "event store unavailable")
        page = EventLogReader(store, run["id"]).read_page(after_seq, limit)
        return {"events": [e.to_obj() for e in page], "next_after_seq": page[-1].seq if page else after_seq}

    @app.get(API + "/runs/{run_id}/config", dependencies=[Guard])
    def run_config_view(run: Run, conn: Sess) -> dict[str, Any]:
        """The configuration a run was started with (what a fork starts from). Same visibility as the run."""
        row = conn.execute("select config_json, config_hash, mode from api_private.run_config(%s)", (run["id"],)).fetchone()
        if row is None:
            raise NotFound
        return {"config": row["config_json"], "config_hash": row["config_hash"], "mode": row["mode"]}

    @app.get(API + "/runs/{run_id}/score", dependencies=[Guard])
    def score(run: Run, conn: Sess) -> dict[str, Any]:
        row = conn.execute("select * from api_private.run_score(%s)", (run["id"],)).fetchone()
        if row is None:
            raise NotFound
        return _jsonable(row)

    @app.get(API + "/runs/{run_id}/checkpoints", dependencies=[Guard])
    def checkpoints(run: Run, conn: Sess) -> list[dict[str, Any]]:
        rows = conn.execute("select id, step, memory_version, config_hash from api_private.run_checkpoints(%s)",
                            (run["id"],)).fetchall()
        return [_jsonable(r) for r in rows]

    @app.post(API + "/runs/{run_id}/stream-ticket", dependencies=[Guard])
    def stream_ticket(run: Run, request: Request, user: Annotated[User | None, Depends(bearer)],
                      conn: Sess) -> dict[str, Any]:
        """A single-use, 60 s ticket for the WebSocket, bound to this run and to the caller (anonymous for a public or
        unlisted run). It keeps JWTs out of WebSocket URLs."""
        who = user.id if user else f"ip:{_ip_hash(client_ip(request), salt)}"
        if not limiter.allow(("ticket", who), 20, 60):
            raise ApiError(429, "rate_limited", "Too many requests", {"retry-after": "60"})
        ticket = new_ticket()
        try:
            conn.execute("select api_private.issue_stream_ticket(%s, %s)", (run["id"], ticket_hash(ticket)))
        except RaiseException as exc:
            if "run_not_found" in str(exc):
                raise NotFound from None
            if "account_erased" in str(exc):
                raise Unauthenticated from None
            raise
        return {"ticket": ticket, "expires_in": TICKET_TTL_S, "path": f"/ws/runs/{run['id']}"}

    @app.get(API + "/runs/{run_id}/frames/{step}", dependencies=[Guard])
    def frame(run: Run, step: Annotated[int, Path(ge=0, le=1_000_000)]) -> Response:
        """302 to a 60 s signed URL for the nearest sampled frame at or before `step`; 404 if there is none."""
        if store is None or signer is None:
            raise ApiError(503, "internal", "frame storage unavailable")
        prefix = f"runs/{run['id']}/frames/"
        steps = sorted(int(m.group(1)) for k in store.list(prefix) if (m := _FRAME_KEY.match(k)))
        at = bisect.bisect_right(steps, step) - 1
        if at < 0:
            raise NotFound
        url = signer.sign_get(f"{prefix}{steps[at]:07d}.png", SIGNED_URL_TTL_S)
        return RedirectResponse(url, status_code=302, headers={
            "cache-control": "no-store", "referrer-policy": "no-referrer", "x-frame-step": str(steps[at])})

    @app.get(API + "/runs/{run_id}/export", dependencies=[Guard, ExportGuard])
    def export(run: Run, conn: Sess) -> dict[str, Any]:
        """A signed URL for the bundle and its sha256, for finished runs whose log is sealed. The bundle is checked
        against the recorded hash and the redaction scan is re-run over every file in it before a URL is issued."""
        if store is None or signer is None:
            raise ApiError(503, "internal", "export storage unavailable")
        row = conn.execute("select status, bundle_sha256 from api_private.run_bundle(%s)", (run["id"],)).fetchone()
        if row is None:
            raise NotFound
        if row["status"] not in FINAL or not row["bundle_sha256"]:
            raise ApiError(409, "export_not_ready", "The run has not finished and been sealed yet")
        key = f"runs/{run['id']}/bundle.zip"
        try:
            data = store.get(key)
        except StoreNotFound:
            raise ApiError(409, "export_not_ready", "The run has not finished and been sealed yet") from None
        digest = hashlib.sha256(data).hexdigest()
        if digest != row["bundle_sha256"] or not _bundle_is_clean(data):
            log.error("export refused run=%s", run["id"])
            raise ApiError(409, "export_refused", "The bundle failed verification")
        return {"sha256": digest, "bytes": len(data), "url": signer.sign_get(key, SIGNED_URL_TTL_S),
                "expires_in": SIGNED_URL_TTL_S}

    @app.get(API + "/runs/{run_id}/memory", dependencies=[Guard])
    def memory_tree(run: Run, conn: Sess, at_step: Annotated[int, Query(ge=0)] = 1_000_000) -> list[dict[str, Any]]:
        rows = conn.execute("select path, chars, version, deleted from api_private.run_memory_tree(%s, %s)",
                            (run["id"], at_step)).fetchall()
        return [{"path": r["path"], "chars": r["chars"], "version": r["version"]} for r in rows if not r["deleted"]]

    @app.get(API + "/runs/{run_id}/memory/file", dependencies=[Guard])
    def memory_file(run: Run, conn: Sess, path: Annotated[str, Query(max_length=256)],
                    version: Annotated[int | None, Query(ge=1)] = None) -> Response:
        if "\x00" in path:
            raise NotFound
        row = _memory_version(conn, run["id"], path, version)
        if row is None or row["deleted"]:
            raise NotFound
        return PlainTextResponse(row["content"], headers={"x-content-type-options": "nosniff"})

    @app.get(API + "/runs/{run_id}/memory/diff", dependencies=[Guard])
    def memory_diff(run: Run, conn: Sess, path: Annotated[str, Query(max_length=256)],
                    from_: Annotated[int, Query(alias="from", ge=0)], to: Annotated[int, Query(ge=1)]) -> Response:
        if "\x00" in path:
            raise NotFound
        old = "" if from_ == 0 else (_memory_version(conn, run["id"], path, from_) or {}).get("content")
        new = _memory_version(conn, run["id"], path, to)
        if old is None or new is None:
            raise NotFound
        diff = difflib.unified_diff(old.splitlines(keepends=True), new["content"].splitlines(keepends=True),
                                    f"v{from_}", f"v{to}")
        return PlainTextResponse("".join(diff))

    @app.post(API + "/runs/{run_id}/report", status_code=202, dependencies=[WriteGuard])
    def report(body: ReportIn, run: Run, user: Authed, conn: Sess) -> dict[str, str]:
        if run["owner_id"] == user.id:
            raise ApiError(422, "validation_failed", "you cannot report your own run")
        try:
            conn.execute("select api_private.file_report(%s, %s)", (run["id"], body.reason))
        except RaiseException as exc:
            if "account_erased" in str(exc) or "not_authenticated" in str(exc):
                raise Unauthenticated from None
            if "report_rate_limited" in str(exc):
                raise ApiError(429, "rate_limited", "Too many reports", {"retry-after": "3600"}) from None
            if "run_not_found" in str(exc):
                raise NotFound from None
            if "cannot_report_own_run" in str(exc):
                raise ApiError(422, "validation_failed", "you cannot report your own run") from None
            raise
        return {"status": "received"}

    # ------------------------------------------------------------ experiments

    def run_id_list(values: list[str]) -> list[str]:
        try:
            return [str(uuid.UUID(v)) for v in values]
        except ValueError:
            raise ApiError(422, "invalid_runs", "run_ids must be run ids") from None

    def check_variable(conn: Conn, user: User, run_ids: list[str], variable: str) -> None:
        """The runs of an experiment differ in the declared variable and in nothing else. They must all be the
        caller's (a missing run and someone else's run are the same answer)."""
        if variable not in EXPERIMENT_VARIABLES:
            raise ApiError(422, "validation_failed", "unknown variable")
        if not run_ids:
            return
        rows = conn.execute(
            "select r.id, c.config_json from public.runs r join public.run_configs c on c.id = r.config_id "
            "where r.id = any(%s) and r.owner_id = %s and r.deleted_at is null", (run_ids, user.id)).fetchall()
        if len(rows) != len(set(run_ids)):
            raise ApiError(422, "invalid_runs", "run_ids must be your own runs")
        try:
            configs = [RunConfig.from_dict(dict(r["config_json"])).to_dict() for r in rows]
        except ValueError:
            raise ApiError(422, "variable_mismatch", "a run has no valid configuration") from None
        differing = sorted(k for k in configs[0] if k != variable and any(c.get(k) != configs[0][k] for c in configs[1:]))
        if differing:
            raise ApiError(422, "variable_mismatch",
                           "runs differ in more than the declared variable: " + ", ".join(differing)[:120])

    def experiment_error(exc: RaiseException) -> ApiError | Unauthenticated | NotFound:
        text = str(exc)
        if "account_erased" in text or "not_authenticated" in text:
            return Unauthenticated()
        if "experiment_run_not_owned" in text or "experiment_duplicate_runs" in text:
            return ApiError(422, "invalid_runs", "run_ids must be distinct runs of yours")
        if "experiment_not_found" in text:
            return NotFound()
        if "too_many_experiments" in text:
            return ApiError(409, "limit_reached", "You have the maximum number of experiments")
        raise exc

    def _arm_label(value: Any) -> str:
        """A short, stable label for one value of the experiment's variable (the arm)."""
        if value is None:
            return "default"
        if isinstance(value, str):
            return value or "default"
        return json.dumps(value, sort_keys=True, separators=(",", ":"))[:80]

    def _arms(conn: Conn, variable: str, scored: list[Any]) -> dict[str, tuple[Any, list[float]]]:
        """Group scored runs by the normalised value of the experiment's variable. Best-effort: any run whose
        configuration cannot be read or parsed is left out of the arms (the pooled summary still counts it)."""
        if not scored:
            return {}
        try:
            ids = [str(r["run_id"]) for r in scored]
            cfgs = conn.execute("select rid, c.config_json from unnest(%s::uuid[]) as rid "
                                "left join lateral api_private.run_config(rid) c on true", (ids,)).fetchall()
        except Exception:
            log.exception("experiment arms: could not load run configs")
            return {}
        value_of: dict[str, Any] = {}
        for cr in cfgs:
            cfg = cr["config_json"]
            if cfg is None:
                continue
            try:
                value_of[str(cr["rid"])] = RunConfig.from_dict(dict(cfg)).to_dict().get(variable)
            except (ValueError, TypeError):
                continue
        arms: dict[str, tuple[Any, list[float]]] = {}
        for r in scored:
            rid = str(r["run_id"])
            if rid in value_of:
                value = value_of[rid]
                arms.setdefault(_arm_label(value), (value, []))[1].append(float(r["progress_score"]))
        return arms

    def experiment_view(conn: Conn, experiment_id: str) -> dict[str, Any]:
        exp = conn.execute("select id, owner_id, title, variable, visibility, created_at "
                           "from api_private.get_visible_experiment(%s)", (experiment_id,)).fetchone()
        if exp is None:
            raise NotFound
        rows = conn.execute("select run_id, status, title, model_id, provider, steps, tokens, ended_at, "
                            "progress_score, verified from api_private.experiment_runs(%s)", (experiment_id,)).fetchall()
        scored = [r for r in rows if r["progress_score"] is not None]
        scores = [float(r["progress_score"]) for r in scored]
        in_progress = sum(r["status"] not in FINAL for r in rows)
        warnings = (["n_below_5"] if len(scores) < 5 else []) + (["runs_still_running"] if in_progress else [])

        arms_scores = _arms(conn, exp["variable"], scored)
        arms = [{"label": label, "value": value, **summarize(vals).to_dict()}
                for label, (value, vals) in sorted(arms_scores.items())]
        comparison = None
        if len(arms_scores) == 2:                                   # the designed one-variable A/B
            (la, (_, va)), (lb, (_, vb)) = sorted(arms_scores.items())
            verdict = compare(la, va, lb, vb)
            comparison = verdict.to_dict() if verdict is not None else None

        overall = summarize(scores)
        summary = {"n": overall.n, "mean": overall.mean, "median": overall.median,
                   "min": overall.min, "max": overall.max, "stdev": overall.stdev,
                   "ci95": list(overall.ci95) if overall.ci95 is not None else None,
                   "runs": len(rows), "runs_in_progress": in_progress, "warnings": warnings,
                   "arms": arms, "comparison": comparison}
        return {**_jsonable(exp), "runs": [_jsonable(r) for r in rows], "summary": summary}

    @app.post(f"{API}/experiments", status_code=201, dependencies=[WriteGuard])
    def create_experiment(body: ExperimentIn, user: Authed, conn: Sess) -> dict[str, Any]:
        ids = run_id_list(body.run_ids)
        check_variable(conn, user, ids, body.variable)
        try:
            row = conn.execute("select api_private.create_experiment(%s, %s, %s::uuid[], %s) as id",
                               (body.title, body.variable, ids, body.visibility)).fetchone()
        except RaiseException as exc:
            raise experiment_error(exc) from None
        return {"id": str(row["id"])}  # type: ignore[index]

    @app.get(f"{API}/experiments", dependencies=[Guard])
    def list_experiments(user: Authed, conn: Sess, limit: Annotated[int, Query(ge=1, le=100)] = 50) -> list[dict[str, Any]]:
        rows = conn.execute("select id, title, variable, visibility, n_runs, created_at from api_private.my_experiments(%s)",
                            (limit,)).fetchall()
        return [_jsonable(r) for r in rows]

    @app.get(API + "/experiments/{experiment_id}", dependencies=[Guard])
    def get_experiment(experiment_id: str, conn: Sess) -> dict[str, Any]:
        return experiment_view(conn, _uuid(experiment_id))

    @app.patch(API + "/experiments/{experiment_id}", dependencies=[WriteGuard])
    def patch_experiment(experiment_id: str, body: ExperimentPatchIn, user: Authed, conn: Sess) -> dict[str, Any]:
        eid = _uuid(experiment_id)
        add, remove = run_id_list(body.add_run_ids), run_id_list(body.remove_run_ids)
        if body.title is None and body.visibility is None and not add and not remove:
            raise ApiError(422, "validation_failed", "nothing to change")
        try:
            conn.execute("select api_private.update_experiment(%s, %s, %s, %s::uuid[], %s::uuid[])",
                         (eid, body.title, body.visibility, add or None, remove or None))
        except RaiseException as exc:
            raise experiment_error(exc) from None
        current = conn.execute("select variable from public.experiments where id = %s and owner_id = %s",
                               (eid, user.id)).fetchone()
        ids = conn.execute("select api_private.my_experiment_run_ids(%s) as ids", (eid,)).fetchone()
        if current is None or ids is None or ids["ids"] is None:
            raise NotFound
        check_variable(conn, user, [str(r) for r in ids["ids"]], current["variable"])   # rolls the change back
        return experiment_view(conn, eid)

    def compare_guard(request: Request, user: Annotated[User | None, Depends(bearer)]) -> None:
        who = user.id if user else f"ip:{_ip_hash(client_ip(request), salt)}"
        if not limiter.allow(("compare", who), 10, 60):
            raise ApiError(429, "rate_limited", "Too many requests", {"retry-after": "60"})

    @app.get(API + "/experiments/{experiment_id}/compare", dependencies=[Guard, Depends(compare_guard)])
    def compare_experiment(experiment_id: str, conn: Sess,
                           metric: Annotated[Literal["score", "tokens"], Query()] = "score") -> dict[str, Any]:
        """Per-step series of one metric for every run of the experiment the viewer may see, aligned on one step axis
        (a value carries forward until the run's next observation; null before its first)."""
        if store is None:
            raise ApiError(503, "internal", "event store unavailable")
        eid = _uuid(experiment_id)
        if conn.execute("select 1 from api_private.get_visible_experiment(%s)", (eid,)).fetchone() is None:
            raise NotFound
        runs = conn.execute("select run_id from api_private.experiment_runs(%s)", (eid,)).fetchall()
        per_run: dict[str, dict[int, float]] = {}
        for r in runs:
            rid = str(r["run_id"])
            points: dict[int, float] = {}
            for n, e in enumerate(EventLogReader(store, rid).iter_events()):
                if n >= MAX_COMPARE_EVENTS:
                    break
                if metric == "score" and e.type == "obs" and isinstance(e.data.get("score"), int | float):
                    points[e.step] = float(e.data["score"])
                elif metric == "tokens" and e.type == "model.call" and isinstance(e.data.get("tokens_total"), int | float):
                    points[e.step] = float(e.data["tokens_total"])
            per_run[rid] = points
        axis = sorted({step for pts in per_run.values() for step in pts})
        if len(axis) > MAX_COMPARE_STEPS:
            stride = -(-len(axis) // MAX_COMPARE_STEPS)
            axis = axis[::stride]
        series = []
        for rid, pts in per_run.items():
            values: list[float | None] = []
            last: float | None = None
            known = sorted(pts)
            at = 0
            for step in axis:
                while at < len(known) and known[at] <= step:
                    last = pts[known[at]]
                    at += 1
                values.append(last)
            series.append({"run_id": rid, "values": values})
        return {"metric": metric, "steps": axis, "series": series}

    # ------------------------------------------------------------ leaderboard

    @app.get(f"{API}/leaderboard", dependencies=[Guard])
    def leaderboard(conn: Sess, limit: Annotated[int, Query(ge=1, le=100)] = 50) -> list[dict[str, Any]]:
        rows = conn.execute(
            "select r.id, r.model_id, r.provider, s.progress_score, s.steps, s.tokens_in, s.tokens_out "
            "from public.run_scores s join public.runs r on r.id = s.run_id "
            "where s.verified and r.visibility = 'public' and r.deleted_at is null "
            "order by s.progress_score desc, s.steps asc limit %s", (limit,)).fetchall()
        return [_jsonable(r) for r in rows]

    # ------------------------------------------------------------ admin

    def audited(conn: Conn, admin: User, request: Request, action: str, target: str, reason: str,
                act: Callable[[], Any]) -> Any:
        """Record the request (committed as `requested` before anything is attempted: an action whose audit row cannot
        be written does not happen), run `act`, then record its outcome: ok, failed, or unknown (a timeout after the
        request was sent: it may have taken effect)."""
        try:
            row = conn.execute("select api_private.admin_audit(%s, %s, %s, %s) as id",
                               (action, target, reason, _ip_hash(client_ip(request), salt))).fetchone()
        except RaiseException as exc:
            if "reason_required" in str(exc):
                raise ApiError(422, "validation_failed", "a reason is required") from None
            raise
        audit_id = row["id"]  # type: ignore[index]
        conn.commit()
        Database.apply_identity(conn, admin)

        def outcome(value: str) -> None:
            conn.execute("select api_private.admin_audit_outcome(%s, %s)", (audit_id, value))
            conn.commit()
            Database.apply_identity(conn, admin)

        try:
            result = act()
        except SubmitUnknown:
            outcome("unknown")
            raise ApiError(503, "internal", "The result is unknown; check before retrying", {"retry-after": "30"}) from None
        except BaseException:
            outcome("failed")
            raise
        outcome("ok")
        return result

    @app.get(f"{API}/admin/runs")
    def admin_runs(_: Admin) -> dict[str, Any]:
        return orchestrator.snapshot()

    @app.post(API + "/admin/runs/{run_id}/kill", dependencies=[WriteGuard])
    def admin_kill(run_id: str, body: AdminKillIn, admin: Admin, request: Request, conn: Sess) -> dict[str, Any]:
        target = _uuid(run_id)
        return {"result": audited(conn, admin, request, "run.kill", target, body.reason,
                                  lambda: orchestrator.kill(target, admin.id, body.reason))}

    @app.post(f"{API}/admin/breaker", dependencies=[WriteGuard])
    def admin_breaker(body: BreakerIn, admin: Admin, request: Request, conn: Sess) -> dict[str, Any]:
        state: dict[str, Any] = audited(conn, admin, request, "breaker.open" if body.open else "breaker.close", "global",
                                        body.reason, lambda: orchestrator.breaker(body.open, admin.id, body.reason))
        return state

    @app.get(f"{API}/admin/reports")
    def admin_reports(_: Admin, conn: Sess, status: Annotated[Literal["open", "actioned", "dismissed", "all"], Query()] = "open",
                      limit: Annotated[int, Query(ge=1, le=100)] = 50, before: str | None = None) -> dict[str, Any]:
        cursor = None if before is None else _cursor(before)
        rows = conn.execute(
            "select r.id, r.run_id, r.reporter_id, r.reason, r.status, r.created_at, r.resolved_at, r.resolved_by, "
            "r.resolution_reason, u.title as run_title, u.owner_id as run_owner_id, u.visibility as run_visibility, "
            "u.status as run_status, u.deleted_at is not null as run_deleted "
            "from public.reports r join public.runs u on u.id = r.run_id "
            "where (%s = 'all' or r.status = %s) "
            "and (%s::timestamptz is null or (r.created_at, r.id) < (%s::timestamptz, %s::uuid)) "
            "order by r.created_at desc, r.id desc limit %s",
            (status, status, cursor and cursor[0], cursor and cursor[0], cursor and cursor[1], limit + 1)).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        nxt = f"{rows[-1]['created_at'].isoformat()}|{rows[-1]['id']}" if more else None
        return {"reports": [_jsonable(r) for r in rows], "next": nxt}

    @app.patch(API + "/admin/reports/{report_id}", dependencies=[WriteGuard])
    def admin_resolve_report(report_id: str, body: ReportResolveIn, _: Admin, request: Request,
                             conn: Sess) -> dict[str, str]:
        """open -> actioned | dismissed, once, with a reason; the audit row is written in the same transaction."""
        rid = _uuid(report_id)
        try:
            conn.execute("select api_private.admin_resolve_report(%s, %s, %s, %s)",
                         (rid, body.status, body.reason, _ip_hash(client_ip(request), salt)))
        except RaiseException as exc:
            text = str(exc)
            if "reason_required" in text:
                raise ApiError(422, "validation_failed", "a reason is required") from None
            if "report_not_found" in text:
                raise NotFound from None
            if "report_closed" in text:
                raise ApiError(409, "report_closed", "The report has already been resolved") from None
            if "not_admin" in text:
                raise NotFound from None
            raise
        return {"id": rid, "status": body.status}

    @app.post(API + "/admin/leaderboard/{run_id}/unverify", dependencies=[WriteGuard])
    def admin_unverify(run_id: str, body: UnverifyIn, _: Admin, request: Request, conn: Sess) -> dict[str, str]:
        rid = _uuid(run_id)
        try:
            conn.execute("select api_private.admin_unverify(%s, %s, %s)",
                         (rid, body.reason, _ip_hash(client_ip(request), salt)))
        except RaiseException as exc:
            text = str(exc)
            if "reason_required" in text:
                raise ApiError(422, "validation_failed", "a reason is required") from None
            if "run_not_found" in text or "not_admin" in text:
                raise NotFound from None
            if "not_verified" in text:
                raise ApiError(409, "not_verified", "The run is not on the leaderboard") from None
            raise
        return {"run_id": rid, "status": "unverified"}

    sockets = SocketCounter()

    def redeem(ticket: str) -> tuple[str, str | None] | None:
        """(run_id, user_id) if the ticket existed and had not expired; it is consumed either way."""
        with db.session(None) as conn:
            row = conn.execute("select run_id, user_id from api_private.redeem_stream_ticket(%s)",
                               (ticket_hash(ticket),)).fetchone()
        return None if row is None else (str(row["run_id"]), None if row["user_id"] is None else str(row["user_id"]))

    def still_visible(run_id: str, user_id: str | None) -> bool:
        """The viewer's own view of the run, evaluated now: a run made private or deleted since the ticket was issued
        is gone for them."""
        with db.session(None if user_id is None else User(user_id)) as conn:
            return conn.execute("select 1 from api_private.get_visible_run(%s) where deleted_at is null",
                                (run_id,)).fetchone() is not None

    def read_events(run_id: str, after: int, limit: int) -> list[dict[str, Any]]:
        if store is None:
            return []
        return [e.to_obj() for e in EventLogReader(store, run_id).read_page(after, limit)]

    def frame_url(run_id: str, step: int) -> str | None:
        if store is None or signer is None:
            return None
        prefix = f"runs/{run_id}/frames/"
        steps = sorted(int(m.group(1)) for k in store.list(prefix) if (m := _FRAME_KEY.match(k)))
        at = bisect.bisect_right(steps, step) - 1
        return None if at < 0 else signer.sign_get(f"{prefix}{steps[at]:07d}.png", SIGNED_URL_TTL_S)

    @app.websocket("/ws/runs/{run_id}")
    async def ws_run(ws: WebSocket, run_id: str, ticket: str = "") -> None:
        """Read-only live stream. Admission: Origin allowlist, a well-formed single-use ticket for this run, and the
        viewer's own visibility of the run now. The client may send only {"t":"resume","after_seq":n} (at most 1 KB);
        anything else closes the socket with 1008. Visibility is re-checked every heartbeat."""
        try:
            rid = str(uuid.UUID(run_id))
        except ValueError:
            await ws.close(code=1008)
            return
        if ws.headers.get("origin") not in settings.allowed_origins or not well_formed(ticket):
            await ws.close(code=1008)
            return
        redeemed = await run_in_threadpool(redeem, ticket)
        if redeemed is None or redeemed[0] != rid or not await run_in_threadpool(still_visible, rid, redeemed[1]):
            await ws.close(code=1008)
            return
        viewer = redeemed[1] or f"ip:{_ip_hash(client_ip_ws(ws), salt)}"
        if not sockets.acquire(rid, viewer):
            await ws.close(code=1008)
            return
        try:
            await ws.accept()
            await _serve_socket(ws, rid, redeemed[1], viewer)
        finally:
            sockets.release(rid, viewer)

    def client_ip_ws(ws: WebSocket) -> str:
        peer = ws.client.host if ws.client else "unknown"
        if settings.trusted_proxy_hops <= 0:
            return peer
        chain = [p.strip() for p in ws.headers.get("x-forwarded-for", "").split(",") if p.strip()]
        try:
            return str(ipaddress.ip_address(chain[-settings.trusted_proxy_hops]))
        except (IndexError, ValueError):
            return peer

    async def _serve_socket(ws: WebSocket, rid: str, user_id: str | None, viewer: str) -> None:
        state = {"after": 0, "closing": False}
        last_resume = [-1e9]

        async def push() -> None:
            last_beat = asyncio.get_running_loop().time()
            while not state["closing"]:
                sent = False
                events = await run_in_threadpool(read_events, rid, state["after"], 200)
                for obj in events:
                    await ws.send_json({"t": "event", **obj})
                    state["after"] = max(state["after"], int(obj["seq"]))
                    sent = True
                obs = [o for o in events if o["type"] == "obs"]
                if obs:
                    url = await run_in_threadpool(frame_url, rid, int(obs[-1]["step"]))
                    if url is not None:
                        await ws.send_json({"t": "frame", "step": int(obs[-1]["step"]), "url": url})
                now = asyncio.get_running_loop().time()
                if now - last_beat >= settings.ws_heartbeat_s:
                    last_beat = now
                    if not await run_in_threadpool(still_visible, rid, user_id):
                        state["closing"] = True
                        await ws.close(code=1008)
                        return
                    await ws.send_json({"t": "hb"})
                if len(events) < 200 and not sent:
                    await asyncio.sleep(settings.ws_poll_s)

        async def listen() -> None:
            while not state["closing"]:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    state["closing"] = True
                    return
                text = message.get("text")
                if text is None:                                  # binary frames are not part of the protocol
                    state["closing"] = True
                    await ws.close(code=1008)
                    return
                if len(text.encode()) > MAX_CLIENT_MESSAGE_BYTES:
                    state["closing"] = True
                    await ws.close(code=1009)
                    return
                after = _resume(text)
                now = asyncio.get_running_loop().time()
                # a resume re-reads the log: at most one a second per socket, and each is a read charged to the
                # viewer, so a socket cannot loop the log around the limit on /events
                if (after is None or now - last_resume[0] < 1.0
                        or not limiter.allow(("read", viewer), settings.read_limit_per_min, 60)):
                    state["closing"] = True
                    await ws.close(code=1008)
                    return
                last_resume[0] = now
                state["after"] = after

        pusher = asyncio.create_task(push())
        try:
            await listen()
        except Exception:  # noqa: BLE001 - a socket error ends this socket only
            state["closing"] = True
        finally:
            state["closing"] = True
            pusher.cancel()
            await asyncio.gather(pusher, return_exceptions=True)

    if isinstance(signer, LocalUrlSigner):
        local_signer: LocalUrlSigner = signer

        @app.get(LocalUrlSigner.PATH + "{token}")
        def local_blob(token: str, exp: str = "", sig: str = "") -> Response:
            """Development stand-in for the bucket: the signature is the only credential."""
            key = local_signer.verify(token, exp, sig)
            if key is None or store is None:
                raise NotFound
            kind = {"png": ("image/png", None), "zip": ("application/zip", 'attachment; filename="bundle.zip"')}.get(
                key.rsplit(".", 1)[-1])
            if kind is None:
                raise NotFound
            try:
                data = store.get(key)
            except StoreNotFound:
                raise NotFound from None
            headers = {"content-disposition": kind[1]} if kind[1] else {}
            return Response(data, media_type=kind[0], headers={**headers, "cache-control": "no-store"})

    return app


# ---------------------------------------------------------------- helpers

def _resume(text: str) -> int | None:
    """The one message a client may send: {"t": "resume", "after_seq": n}. None for anything else."""
    try:
        msg = json.loads(text)
    except ValueError:
        return None
    if (not isinstance(msg, dict) or set(msg) != {"t", "after_seq"} or msg["t"] != "resume"
            or isinstance(msg["after_seq"], bool) or not isinstance(msg["after_seq"], int)
            or not 0 <= msg["after_seq"] <= 2**53):
        return None
    return int(msg["after_seq"])


def _bundle_is_clean(data: bytes) -> bool:
    """The export's redaction scan, re-run: no file inside the zip may contain credential-like text. A zip that cannot
    be read, or that expands past a sane size, is refused rather than served."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()
            if sum(i.file_size for i in infos) > 4 * MAX_EXPORT_BYTES:
                return False
            return not any(scan_bytes(zf.read(i)) for i in infos)
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError):
        return False


_CURSOR = re.compile(r"^([0-9T:.+\-Z]{16,40})\|([0-9a-f-]{36})$")


def _cursor(value: str) -> tuple[datetime, str]:
    m = _CURSOR.match(value) if len(value) <= 80 else None
    if not m:
        raise ApiError(422, "validation_failed", "bad cursor")
    try:
        created = datetime.fromisoformat(m.group(1))
        if created.tzinfo is None:
            raise ValueError("no timezone")
        created.astimezone()           # out-of-range offsets overflow here, not in Postgres
    except (ValueError, OverflowError):
        raise ApiError(422, "validation_failed", "bad cursor") from None
    try:
        return created, str(uuid.UUID(m.group(2)))
    except ValueError:
        raise ApiError(422, "validation_failed", "bad cursor") from None


def _jsonable(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in row.items():
        out[k] = v.isoformat() if hasattr(v, "isoformat") else str(v) if isinstance(v, uuid.UUID) else (
            float(v) if v.__class__.__name__ == "Decimal" else v)
    return out


def _public_run(row: dict[str, Any]) -> dict[str, Any]:
    run = _jsonable(row)
    run["caps"] = {"max_steps": run.pop("cap_steps"), "max_tokens": run.pop("cap_tokens"),
                   "max_wall_s": run.pop("cap_wall_s")}
    return run


def _memory_version(conn: Conn, run_id: str, path: str, version: int | None) -> dict[str, Any] | None:
    return conn.execute("select content, deleted from api_private.run_memory_version(%s, %s, %s)",
                        (run_id, path, version)).fetchone()


def _active_run() -> ApiError:
    return ApiError(409, "rate_limited", "You already have an active run", {"retry-after": "60"})


def _account_is_live(conn: Conn, user: User) -> bool:
    row = conn.execute("select erased_at from public.profiles where id = %s", (user.id,)).fetchone()
    return row is not None and row["erased_at"] is None


def _has_active_run(conn: Conn, user: User) -> bool:
    return conn.execute("select 1 from public.runs where owner_id = %s and status = any(%s) limit 1",
                        (user.id, list(_ACTIVE))).fetchone() is not None


def _create_run_request(conn: Conn, game: str, visibility: str, config: RunConfig, doc: dict[str, Any],
                        parent_checkpoint_id: str | None, fork_patch: dict[str, Any] | None) -> str:
    """The only write path for a new run: the owner is auth.uid() inside the function, never a parameter."""
    from psycopg.types.json import Jsonb
    caps = config.caps
    row = conn.execute(
        "select api_private.create_run_request(p_config_json => %s, p_config_hash => %s, p_mode => %s, "
        "p_visibility => %s, p_cap_steps => %s, p_cap_tokens => %s, p_cap_wall_s => %s, p_game => %s, "
        "p_parent_checkpoint_id => %s, p_fork_patch => %s) as id",
        (Jsonb(doc), config.config_hash, config.mode, visibility, caps.max_steps, caps.max_tokens,
         int(caps.max_wall_s), game, parent_checkpoint_id, None if fork_patch is None else Jsonb(fork_patch))).fetchone()
    return str(row["id"])  # type: ignore[index]


__all__ = ["CUSTOM_LABEL", "create_app"]
