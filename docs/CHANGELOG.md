# Changelog

Format per milestone: scope, changes, tests/evidence, deviations, tag.

## Multi-provider support (branch `feat/providers-openai-compat`, after `fix/ci-baseline`)

- `labharness.providers.openai_compat.OpenAICompatProvider`: one OpenAI-Chat-Completions adapter for OpenAI, OpenRouter, Groq and NVIDIA, parameterised by host and base path, with the Anthropic adapter's security properties (SecretKey `Authorization: Bearer`, fixed host, no redirects, `trust_env=False`, bounded retries, stable error codes, no key/body echo).
- `providers.registry`: `PROVIDER_MODELS`/`PROVIDER_HOSTS` gain the four providers (+ `OPENAI_COMPAT` base paths); the catalogue (`GET /models`, web form, CLI), `check_model` (API + orchestrator) and the egress host allowlist all read this one table, so no other code changed to accept them. `infra/compose.yaml` egress now allows one host per provider.
- Worker: `make_provider` and `spec.parse_spec` accept the four names (same `{name, api_key}` shape as Anthropic).
- Tests: `test_provider_openai_compat.py` (38: request/response shapes incl. images and tool roundtrip, error mapping, retries, key secrecy, host/env isolation, `validate_key`); `make_provider` picks the right adapter per provider; `spec` parses each and still rejects unlisted names. mypy and ruff clean.
- Not verified against any live API (BLOCKED B-1/B-4): shapes follow the public docs and a mock transport; the key validator is still unwired, so real keys are refused for every provider, as before. See DECISIONS M11-1.

## CI and supply chain (branch `ci`, after `m9-done`)

- `.github/workflows`: `ci.yml` (python, web, docker, `gate`), `security.yml` (gitleaks history, CodeQL, pip-audit/npm audit/osv-scanner, hadolint, trivy config and images, actionlint, zizmor, `security`), `scorecard.yml`, `release.yml` (GHCR, SPDX SBOM, cosign, provenance); `dependabot.yml`.
- `.gitleaks.toml` allowlists exact fake values instead of files; `security/audit-ignores.toml` + `scripts/audit_gate.py` (ignores need a reason and an expiry).
- Images: no pip/npm in runtime images, zlib security update, hadolint justifications; `build.sh --only NAME`.
- Root `SECURITY.md`, `CONTRIBUTING.md`, `.github/CODEOWNERS`, PR template, issue forms; README rewritten; BLOCKED B-16 (licence). docs/SECURITY.md "CI and supply chain" lists the owner-side repository settings. Nothing has run on GitHub yet.

## Launch readiness (branch `launch-ready`, after `m8-done`)

- Entrypoints for ingest and the orchestrator; Dockerfiles for ingest, api, accounts, orchestrator (with the Docker CLI), reaper, web and the Docker filter; `infra/docker/build.sh --all`.
- `infra/compose.yaml` (the full stack, hardened containers, internal networks, secrets as files, only api and web published), `infra/compose.smoke.yaml`, `scripts/launch_smoke.py` (`make images`, `make smoke`, `make launch`).
- `services/dockerproxy`: the orchestrator has no Docker socket; the filter allows our hardened sandboxes only. `runlog.envfile`: secrets from `NAME_FILE`.
- API: security headers on every response including 500; `user_busy`/`ip_busy` is the active-run answer, not a 502. Web: robots, sitemap, favicon, OG, `MEMORYLAB_INTERNAL_HTTP_HOSTS`.
- Docker tests fail on a stale worker image (`memorylab.src` label).
- Docs: `docs/LAUNCH.md` (every checklist item DONE / PARTIAL / NOT RUN / BLOCKED with evidence), `BLOCKED.md` B-7..B-15, `SECURITY.md` U-10, `DECISIONS.md` M9-1..M9-8.
- Review fix (critical): the Docker filter now refuses duplicate/case-variant keys, allowlists every level exactly, forwards a rebuilt body, refuses bodies off the create route and pins API versions (M9-9). Tests: `services/dockerproxy`; the rest of the added tests are in `runlog`, `ingest`, `orchestrator` (entrypoints, secrets from files, stale image), the API (headers, busy codes), web (vitest and Playwright `seo.spec.ts`); the compose smoke is `make smoke`. Suite totals are in the report that accompanies the final commit.

## M8: sessions, dream and resume (on branch `m7-web`)

### M8 review fixes 1-3

- Per-run checkpoint quota: migration 20261108000019 (`run_ingest_state.checkpoints`), `Limits.max_checkpoints_per_run=200`, counted per distinct step at upload time (orphans count, an idempotent re-upload is free, a different blob for the same step is still a 409), 429 `quota_exceeded` beyond it.
- API fork end to end (`services/api/tests/test_fork_e2e.py`, 3 tests): public fork through API, orchestrator, worker and ingest; private is a 404 and creates nothing; an invisible run's checkpoint is unusable.
- Worker: `_control` tolerates a closed stdin and `main` joins the heartbeat thread, which removes the "I/O operation on closed file" warning; unraisable/thread exceptions are errors in the orchestrator and worker suites.
- Sandbox image staleness: `infra/docker/build.sh` labels the worker image `memorylab.src` (hash from `scripts/image_src_hash.py` over the labharness, runlog and worker sources); `test_docker` fails, not skips, when the label differs from the tree (+2 tests). The Docker tests had run against an 11 h old image.

- labharness: `session_len` boundaries (context dropped, emulator and memory kept, capped inject file only), the dream job (own token cap counted in the run total, memory tools only, `dream.*` events with diffs), checkpoints at boundaries and every `checkpoint_every` steps with the model context stored, and resume. runlog/ingest: dream rows are memory mutations; `dream_ops` apart from `memory_ops`.
- Checkpoints in ingest (state + context blobs, `checkpoints` rows) and the worker/orchestrator resume path: B-6 closed, forking on by default. Web: dream changes before/after side by side in the memory panel, dream events in the log; CLI: `memorylab runs dream`.
- Tests: labharness +22 (sessions), runlog +6, ingest +17, orchestrator +5 (incl. 1 Docker fork test with two real sandboxes), web +11 vitest and 2 Playwright, CLI +2.

## M7: web (branch `m7-web`, in progress)

- `apps/web`: Next.js 16 App Router, strict TypeScript, Tailwind with our own tokens. All 13 screens: landing, auth (sign in, sign up, reset, magic link), new run (4 steps + review), live run and replay (one screen, scrub with `?at=`), memory drawer, fork modal, experiment compare, leaderboard, my runs, account, admin, legal drafts and 404. Every listed state: queued, starting, running, session boundary, dreaming, stopping, ended with reason, disconnected/resuming (then polling), capacity_full, key_rejected, n < 5, runs still running, breaker banner.
- Security: nonce CSP without unsafe-inline/eval, HSTS, Referrer/Permissions policy, no tokens in the browser (httpOnly cookie session behind a same-origin proxy with CSRF checks, path allowlist and trusted-hop client address), `no-raw-html` lint rule, built-bundle secret scan, production config guard. See SECURITY.md U-8.
- Tests: 94 vitest, 35 Playwright (real stack: GoTrue, Postgres, accounts, a scripted orchestrator): launch to export, XSS corpus in memory/summaries/titles/reports, axe on every screen (light and dark), keyboard-only flow, 360 px, enumeration, security headers and network, state tests, Lighthouse. `make gate` runs all of it (`make web`).
- Found by the tests: Radix inline styles vs the CSP (replaced), `upgrade-insecure-requests` on http, sign-in rate limit tripping the suite, a tab layout rule lost to `[hidden]`, an unlayered base style overriding utilities.

## CLI (branch `cli`, after M6)

- `packages/cli` (`memorylab`): login (password or magic link), logout [--everywhere], signup, me, delete-account, games, presets, models, runs new/list/show/stop/delete/patch/watch/events/checkpoints/score/frame/export/memory, fork, experiments, leaderboard, admin. `--json` everywhere, stable exit codes, provider key only from stdin/env/hidden prompt, session file 0600 with automatic refresh, WebSocket tail with resume and polling fallback, export verified by sha256, control characters escaped in all server text.
- Tests: 25 unit + 46 against the real API served over HTTP/WS with real GoTrue and the accounts service.
- Found and fixed: the API's DB session committed *after* the response was sent, so a client that read right after a write (a stream ticket, then connecting) could miss it; the session dependency now has `scope="function"`.

## M6: API and auth (branch `m6-api`; complete, awaiting final review, not tagged)

- Local Supabase (CLI 3.0.0, Docker), 16 migrations: core schema with forced RLS, soft delete, ingest state, `api_private` write functions, login role `memorylab_api`, link-only unlisted runs and experiments, account erasure and tombstone, fork support, bundle reader, stream tickets, experiments, admin moderation, audit outcomes, hidden `experiments.run_ids`.
- `services/api`: JWT verification, RLS-backed reads, one visibility dependency (404 for missing/hidden/deleted), runs (validate, create, list, get, patch, stop, delete, events, score, memory, checkpoints, report, fork [disabled], frames, export, stream ticket), WebSocket `/ws/runs/{id}`, experiments (create, get, patch, list, compare), leaderboard, `DELETE /me`, admin (runs, kill, breaker, reports, unverify) with live session re-check and an audit row (with outcome) for every action, uniform signup, per-user/IP limits, strict caps, NUL-byte rejection.
- `services/accounts` (new): account erasure under the service role (kill runs, anonymise verified public runs to a tombstone, soft-delete the rest, delete the auth user). `orchestrator.reaper` (new): fails stale queued/starting/active runs, ends logged runs through ingest, kills non-queued ones. Orchestrator submit is idempotent per run id. `ingest.pg_repository`: Postgres `RunRepository`.
- Tests at the last gate: API 666, orchestrator 113, accounts 12, runlog 154, labharness 510, ingest 63, worker 44, egressproxy 45. Real PostgREST and real GoTrue tests for the write paths, listing leaks, tombstone, erased tokens. Every new check carries a test that fails when the check is removed (mutation results are in the commit messages).
- Review rounds 1-4 fixed: direct PostgREST inserts bypassing every API check; orphan queued rows and burned quota; 500s on malformed input; unlimited write routes; superuser DB role; child tables listing unlisted runs; ScrubFilter breaking `%d`; ambiguous submits abandoned; stuck active runs locking users out; tombstone breaking GoTrue; erased tokens writing; experiment run ids readable; fork costing quota; WS resume bypassing limits; unaudited failed admin calls.
- Not built / NOT RUN: fork from a checkpoint end to end (B-6), signed URLs from a real bucket (B-2), provider key validation (B-4), CAPTCHA and gateway rules (B-5), real-model runs (B-1).
- Deviations/findings: DECISIONS M6-1..M6-19.
## M5: sandbox and orchestrator (branch `m5-sandbox`, tag `m5-done`)

- Egress allowlist proxy, worker (stdin job, heartbeats, graceful stop), orchestrator (validation against server maxima, queue/TTL/limits/breaker, hardened container per run, heartbeat/wall kill, finalisation, audit), Dockerfiles with lockfile-hashed deps and pruned SDL libraries.
- Ingest: per-scope batch numbering, status endpoint, auth before body parsing.
- Tests: real-container probes (hardening, no egress, no key in inspect/top/environ, pids/memory limits, heartbeat kill). DEVIATION: Docker, not gVisor (M5-1).
- NOT RUN: gVisor; real provider egress.
- Provenance: a prior session ended without a state note and left 24 uncommitted files (modified docs, labharness agent/provider, ingest, Makefile, lockfile, plus the new egressproxy, worker, orchestrator and infra trees). They were reviewed and committed as M5 work. The review found `elif False:` in place of the heartbeat-timeout check in `orchestrator/sandbox.py`; it was restored and a real-clock regression test added (fails when the check is stubbed out).

## M4: event log, manifest, replay, export, ingest, redaction (branch `m4-runlog`, tag `m4-done`)

- `packages/runlog`: strict event model, run lifecycle state machine with writer roles, redaction (exact key + encodings + patterns), object store protocol + local implementation, zstd-segmented gapless event log with checksums and crash recovery, write-once sealed manifest, deterministic export bundle with credential scan, replay/verification (log integrity, detector recomputation, **re-execution of recorded actions against the emulator**), worker sinks (redacting, local, HTTP with idempotent batches).
- `services/ingest` (FastAPI): per-run HMAC tokens, scope separation, quotas, body limits, frame validation, manifest sealing, close + export.
- Loop changes: `action` events carry the validated action (enables re-execution), `content_filter` for memory writes, `frame_sink`.
- Python pinned to 3.14.
- Tests: 193 new (runlog 143, ingest 53 less overlap; gate total in HANDOFF); 12 deliberate mutations caught.
- Found and fixed by tests: full-tail resume bug, base64-alignment redaction gap, events accepted after a final state, validation errors echoing input, chunked-body 413, verifier crash on malformed bundles.
- Deviations: see DECISIONS M4-*.

## M3: agent loop and provider adapter (branch `m3-agent`, tag `m3-done`)

- Provider interface (`providers.base`), Anthropic adapter over httpx (retries, backoff, error mapping, no redirects, canary-key tested), `SecretKey`, `FakeProvider`, provider/model allowlist.
- Agent loop with hard caps on steps, tokens (provider-reported usage), wall time and frames; user stop; detectors (position loop, failed actions, memory/play ratio, churn, stale-note read, cap rejections) computed from events; closed game tool schemas; memory tools absent in the no-memory arm.
- Presets `none`, `general`, `failures_only`, `read_first_test_theories` (version 1, labelled adaptation, text hashes pinned); frozen base prompt; rolling context with API-valid structure.
- Untrusted game text handling (SECURITY.md U-1, U-5).
- Tests: 148 new (481 total in the gate): adapter 56, prompt/tools/presets 40, detectors and loop 52; all with the fake provider or a mock transport; 11 deliberate mutations caught.
- NOT RUN: any live model call (BLOCKED.md B-1).

## M2: memory service (branch `m2-memory`, tag `m2-done`)

- Versioned-row virtual filesystem (`labharness.memory`, `memory_paths`): five closed-schema tools, caps (files, per-file, total, history), `mem.read/write/delete/reject` events with full diff, copy-on-write snapshots by `rev`, forks, per-step tree view, version diff.
- Tests: 87 (unit, hostile-input property tests against an independent reference, stateful model test with snapshot-immutability invariants, no-host-disk proof, 8-thread concurrency); 10 deliberate mutations all caught.
- Deviations: M2-2 (rev + content in events), M2-5 (history budget). See DECISIONS.md.

## M1-close: Milestone 1 close-out (branch `m1-close`)

- `release_frames` per game in `bundle.json` (harness 0.2.0, semantics hash updated, golden regenerated; RAM digests per step unchanged).
- Cartridge RAM banks included in the trace digest; probe ROM `probe_sram.gb`; PyBoy bank-slice quirk handled.
- `labharness run` / `labharness calibrate` CLI (`python -m labharness`).
- Tests: `tick()` ended (real QUIT + wrapped ticks), behavioural idle-frame boundary test (+ negative control), RAM-layout parsing, CLI success/failure paths.
- Licence audit (THIRD_PARTY.md, scripts/licence_audit.py); SECURITY.md U-1..U-3; README notes; HANDOFF.md, BLOCKED.md.
- Repo: uv workspace, Makefile (`make gate`), gitleaks.

## Milestone 1: headless emulator step loop and deterministic RAM state reader (branch `milestone-1-emulator`)

Added `packages/labharness` (emulator layer only: no provider, memory service, or agent loop)
and `packages/games/fixture` (test-only ROM, CC0).

- Emulator client running PyBoy 2.9.0 headless in a child process (timeouts, rlimits, kill-on-hang).
- Closed-schema actions (`press`, `wait`) with sequence semantics; `RELEASE_FRAMES = 7` measured and frozen.
- Versioned, validated state map; normalised telemetry (core + per-game extras).
- Session: scripted execution, per-step RAM/telemetry trace, score tracker, screenshot policy, frame/step caps.
- Checkpoint/restore at step boundaries with integrity checks.
- Game bundle loader (sha256-pinned ROM, start state, state map; PyBoy version pinned).
- Tests: see docs/MILESTONE-1.md.

Harness version `0.1.0`. Findings recorded in docs/DECISIONS.md (M1-6, M1-10) and docs/GBSTUDIO_SYMBOLS.md.
