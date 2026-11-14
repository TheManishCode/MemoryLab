# Handoff

Current state, updated at every milestone.

## State
- **M1–M9 done; public launch is blocked only on the owner inputs in `BLOCKED.md`** (checklist verdicts: `docs/LAUNCH.md`). Older per-milestone notes follow.
- **Dependency consolidation (branch `ccr-cdce2714-sgcqry`):** merged the six open PRs into one coherent branch (DECISIONS M11, CHANGELOG). Adopted: Windows support (#6), pyjwt 2.15.1 + psycopg 3.3.8 (#5/#4), ESLint 10 (#2), node:26-slim web base (part of #1). **Held (PRs left open):** TypeScript 7 (#3) — no `typescript-eslint` supports it yet; Python 3.15 base (part of #1) — pinned `pyboy==2.9.0` has no cp315 wheel. Revisit each when its upstream catches up.
- Milestone 1 (emulator harness) and M1-close are implemented and tested. See CHANGELOG.md.
- M2 memory service implemented and tested (`labharness.memory`).
- M3 agent loop, Anthropic adapter (mock-tested only), presets, detectors implemented.
- M4 `runlog` (events, lifecycle, redaction, store, segmented log, manifest, bundle, replay/verify, client sinks) and `services/ingest` implemented.
- Python is pinned to 3.14 (stdlib zstd). Packages: `packages/labharness`, `packages/runlog`, `services/ingest`.
- M5: `services/egressproxy`, `services/worker`, `services/orchestrator`, `infra/docker` (images `memorylab-worker:dev`, `memorylab-egress:dev`; build with `infra/docker/build.sh`). Docker integration tests run when the images exist (they only touch `memorylab`-labelled objects).
- M6 (branch `m6-api`): local Supabase in `infra/supabase`, `services/api` (JWT, RLS, runs, admin, uniform signup), `ingest.pg_repository`. See CHANGELOG for what is built and what is not.
- M6 is feature complete and awaiting the final review and the owner's tag decision (not tagged). Fork is built but disabled: the worker cannot start from a checkpoint (BLOCKED.md B-6); frames/export signing is a local stand-in (B-2). Next: M7.

## How to run
```bash
uv sync --all-packages --all-groups
supabase start --workdir infra -x studio,imgproxy,vector,edge-runtime,logflare,postgres-meta,supavisor   # mailpit stays on (auth tests read mail)
make gate          # needs the stack above; MEMORYLAB_REQUIRE_DB=1 is set by the target
supabase stop --workdir infra
```
`supabase db reset --workdir infra` re-applies the migrations (needed after adding one).

### Processes (M6)
All values come from the environment; secrets are never defaults. Tokens are >= 32 bytes.

| Process | Command | Environment |
|---|---|---|
| API | `uvicorn --factory api.main:build --ws-max-size 1024` (keep access logs off for `/ws/`: the ticket is in the URL) | `MEMORYLAB_JWKS_URL`, `MEMORYLAB_JWT_ISSUER` (required), `MEMORYLAB_LOG_PEPPER` (required, >= 16 chars: keys the address tags in logs), `MEMORYLAB_DB_URL` (the `memorylab_api` login role, never `postgres`), `MEMORYLAB_AUTH_URL`, `MEMORYLAB_AUTH_APIKEY`, `MEMORYLAB_ORCHESTRATOR_URL`/`_TOKEN`, `MEMORYLAB_ACCOUNTS_URL`/`_TOKEN`, `MEMORYLAB_OBJECT_STORE`, `MEMORYLAB_ALLOWED_ORIGINS` (comma list; empty = no WebSocket), `MEMORYLAB_TRUSTED_PROXY_HOPS` (proxies in front, else every caller shares one IP), `MEMORYLAB_FORK_ENABLED=0` switches forking off (on by default since M8), `MEMORYLAB_BLOB_SIGNING_SECRET` + `MEMORYLAB_PUBLIC_API_URL` (local frames/export only) |
| Accounts service | `uvicorn --factory accounts.main:build` (internal network only) | `MEMORYLAB_SERVICE_DB_URL` (service-role access), `MEMORYLAB_ORCHESTRATOR_URL`/`_TOKEN`, `MEMORYLAB_AUTH_URL`, `MEMORYLAB_AUTH_SERVICE_KEY`, `MEMORYLAB_ACCOUNTS_TOKEN` |
| Reaper | `python -m orchestrator.reaper` | `MEMORYLAB_SERVICE_DB_URL`, `MEMORYLAB_INGEST_URL`, `MEMORYLAB_TOKEN_SECRET` (the orchestrator's), `MEMORYLAB_ORCHESTRATOR_URL`/`_TOKEN`, optional `MEMORYLAB_REAPER_PERIOD_S` (60) |

Migrations are `infra/supabase/migrations/*` (16 so far); the local password for `memorylab_api` is set by `infra/supabase/seed.sql` (never in production). Gateway: block `/auth/v1/signup`, `/auth/v1/otp` and `/auth/v1/recover` (enumeration; use `/api/v1/auth/*`). CLI: plain http is refused except to loopback (`MEMORYLAB_ALLOW_INSECURE=1` for dev). `api_private` must never be added to PostgREST's `schemas`.

### Web app (M7)
`cd apps/web && npm ci && MEMORYLAB_PUBLISHABLE_KEY=... npm run build && npm start` (Node 22+). `make web` = ruff on the harness, eslint, tsc, vitest, build, bundle scan, Playwright (needs `supabase start`; browsers via `npx playwright install chromium`). Environment (server side only; none of it is baked into the bundle): `MEMORYLAB_API_URL` (API as the web server reaches it), `MEMORYLAB_PUBLIC_API_URL` (API as browsers reach it: signed frame/export URLs and the WebSocket; the CSP is built from it), `MEMORYLAB_AUTH_URL` (GoTrue: only `/token` and `/logout`), `MEMORYLAB_PUBLISHABLE_KEY`, `MEMORYLAB_SITE_URL` (exact web origin: CSRF check, cookie flags), `MEMORYLAB_WEB_TRUSTED_HOPS` (>= 1 in production), optional `MEMORYLAB_CSP_REPORT_URI`. In production the server refuses to start if any of those URLs is plain http to a non-loopback host. The API needs `MEMORYLAB_ALLOWED_ORIGINS=<the site origin>` and `MEMORYLAB_TRUSTED_PROXY_HOPS=1`. Keep access logs off for the API's `/ws/`.

### Whole stack (launch-ready)
```bash
supabase start --workdir infra
infra/docker/build.sh --all        # every image (the worker is labelled with its source hash)
make smoke                         # boots infra/compose.yaml + the smoke overlay, plays a run and a fork through the CLI, then removes everything it made
```
A deployment: `docker compose -p memorylab -f infra/compose.yaml --env-file <private env> up -d --wait` (variables and the secrets directory: `infra/compose.env.example`).
Processes now also include ingest (`uvicorn --factory ingest.main:build`; `MEMORYLAB_SERVICE_DB_URL`, `MEMORYLAB_TOKEN_SECRET`, `MEMORYLAB_OBJECT_STORE`), the orchestrator (`uvicorn --factory orchestrator.main:build`; see `orchestrator/main.py` for the variables; it needs `DOCKER_HOST` pointing at `dockerproxy`, never a socket) and `dockerproxy` (`python -m dockerproxy.main --image memorylab-worker:dev --network memorylab-sandbox`). Every secret can be given as `NAME_FILE`. Launch checklist status and the deployment requirements (gateway rules, `--ws-max-size 1024`, `MEMORYLAB_WEB_TRUSTED_HOPS`, the Docker filter): `docs/LAUNCH.md`.

## Environment on this machine
- rgbds 1.0.1 at `~/.local/opt/rgbds-1.0.1` (build tool for fixture ROMs).
- gitleaks 8.30.1 at `~/.local/bin/gitleaks`.
- Docker available; gVisor (`runsc`) not installed. Supabase CLI 3.0.0 at `~/.local/bin/supabase` (DECISIONS M6-0).
- No `.env` (no model key): real-model checks are NOT RUN (BLOCKED.md B-1).

## Gotchas
- PyBoy child is spawned: guard entry points with `if __name__ == "__main__":`.
- Changing the fixture ROM changes every hash and the golden trace; see packages/games/fixture/README.md.
