# Third-party components and licence audit

Audited 2026-11-08 from the installed distributions' own metadata and licence files
(`scripts/licence_audit.py`, run in the locked venv). Re-run whenever `uv.lock` changes and
update this file. Dependencies are added only if the licence is permissive or weak copyleft
and acceptable for a hosted service; anything stronger (GPL/AGPL/SSPL/BUSL) is refused.

## Runtime dependencies (Milestone 1; later milestones append here)

| Component | Version | Licence (from metadata) | Notes |
|---|---|---|---|
| PyBoy | 2.9.0 | LGPL-3.0-only | Imported unmodified as a pip dependency, never vendored or patched. Images must ship the licence text and attribution and keep PyBoy replaceable (plain pip install). Server-side use does not distribute it to visitors, but images/bundles we share would; get legal sign-off before any public launch (see LAUNCH.md). |
| numpy | 2.5.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | |
| Pillow | 13.0.0 | MIT-CMU (HPND-style) | |
| PySDL2 | 0.9.17 | Public Domain / zlib | PyBoy dependency; unused at runtime (null window). |
| pysdl2-dll | 2.32.10 | MPL-2.0 (wrapper); bundles SDL2 (zlib) and SDL2_gfx/image/mixer/ttf plus avif, dav1d, tiff, webp, ogg, opus, wavpack, xmp, gme | MPL-2.0 is file-level copyleft; we do not modify it. Its many bundled codecs are unneeded with a null window and enlarge the attack surface: the sandbox image (M5) should prune unused libraries if PyBoy still starts, otherwise record as a known limitation. |

| httpx | 0.28.1 | BSD-3-Clause | Direct HTTPS calls to the model provider (M3). Redirects, proxies and CA overrides from the environment are disabled in our client. |
| click | 8.5.1 | BSD-3-Clause | `memorylab` CLI argument parsing and prompts. |
| rich (+ markdown-it-py, mdurl) | 15.0.0 | MIT (mdurl: MIT) | CLI tables. Markup and highlighting are off; server text is stripped of control characters first. |
| websockets | 17.4 | BSD-3-Clause | CLI live tail (`runs watch`), read-only client. |
| httpcore / h11 / anyio / idna | 1.0.9 / 0.16.0 / 4.16.0 / 3.20 | BSD-3-Clause / MIT / MIT / BSD-3-Clause | httpx dependencies. |
| certifi | 2026.10.23 | MPL-2.0 | CA bundle (data file); weak copyleft, unmodified. |

| fastapi / starlette / pydantic (+core, annotated-types, annotated-doc) | 0.162.2 / 1.10.0 / 2.14.3 | MIT / BSD-3-Clause / MIT | Ingest service and (M6) the public API. |
| uvicorn / click | 0.54.0 / 8.5.1 | BSD-3-Clause | ASGI server for local runs. |
| psycopg / psycopg-binary / psycopg-pool | 3.3.8 / 3.3.8 / 3.3.3 | LGPL-3.0-only | M6 Postgres access (API, ingest). Weak copyleft like PyBoy: unmodified pip dependency, never vendored; images must keep it replaceable and ship the licence text. Same legal sign-off item as PyBoy before public launch. |
| PyJWT | 2.15.1 | MIT | M6 JWT verification (asymmetric algorithms only). |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause | PyJWT[crypto] (ES256/RS256). |
| `compression.zstd` | Python 3.14 stdlib | PSF-2.0 | Event-log segment compression (no third-party zstd package). |

## Development-only dependencies (not shipped)

| Component | Version | Licence |
|---|---|---|
| pytest / pluggy / iniconfig / packaging | 9.2.0 / 1.7.0 / 2.3.1 / 26.3 | MIT / MIT / MIT / Apache-2.0 OR BSD-2-Clause |
| hypothesis | 6.170.15 | MPL-2.0 (+ sortedcontainers 2.4.0, Apache-2.0) |
| ruff | 0.17.4 | MIT |
| mypy (+ mypy_extensions, librt, ast_serialize, pathspec) | 2.4.1 | MIT (pathspec: MPL-2.0 per classifier) |
| Pygments, typing_extensions | 2.21.0 / 4.16.0 | BSD-2-Clause / PSF-2.0 |

## Tools and assets

| Item | Licence | Notes |
|---|---|---|
| rgbds 1.0.1 | MIT | Build tool for the fixture ROMs only; not shipped. Installed from the upstream release tarball, sha256 `80a5cad8dae27e24e46a93041352c47cadbc165103983f41c2b3082c42f6dad9` verified. |
| Fixture ROMs (`packages/games/fixture`) | CC0-1.0 | Authored for Memory Lab. Test-only; never offered to visitors. |

## Web app (`apps/web`, npm)

| Package | Version | Licence | Use |
|---|---|---|---|
| next, react, react-dom | 16.4.5 / 19.3.1 | MIT | The web app. |
| Radix UI / shadcn/ui (evaluated, **not used**) | n/a | MIT | Radix Dialog and Tabs render inline `style` attributes and an un-nonced `<style>`; our production CSP (nonce-only styles) blocks them, so `src/components/dialog.tsx` (native `<dialog>`) and `tabs.tsx` (ARIA tabs) are written in-house. The API shape follows shadcn's wrapper; no code was copied. |
| tailwindcss, @tailwindcss/postcss, postcss | 4.3.5 / 8.5.32 | MIT | Styling with our own tokens. |
| typescript | 6.0.3 | Apache-2.0 | Build-time only. |
| eslint, eslint-config-next, typescript-eslint | 9.39.5 / 16.4.5 / 8.72.1 | MIT | Build-time only; includes our `no-raw-html` rule. |
| vitest | 5.1.0 | MIT | Unit tests. |
| @playwright/test | 1.65.0 | Apache-2.0 | End-to-end tests (build-time only; browsers are downloaded separately by Playwright). |
| @axe-core/playwright, axe-core | 4.14.0 | MPL-2.0 | Accessibility checks in tests; not shipped. |
| lighthouse | 13.6.0 | Apache-2.0 | Score check in tests; not shipped. |

`npm audit` reports 8 moderate findings, all in the lint toolchain's `minimatch` (`eslint-config-next` -> `typescript-eslint`). That chain runs at build time only and is not in the shipped bundle; revisit when upstream updates.

## Container images and build-time components (launch readiness)

| Component | Version | Licence | Notes |
|---|---|---|---|
| python:3.14-slim | tag `3.14-slim` (Docker Official Image, Debian) | PSF-2.0 (Python); Debian packages under their own licences | Base of every Python image (sandbox, egress proxy, services, Docker filter). Pin by digest when hosting is chosen. |
| node:26-slim | tag `26-slim` (Docker Official Image) | MIT (Node.js) | Base of the web image. |
| docker:cli | tag `cli` (Docker Official Image) | Apache-2.0 | Only `/usr/local/bin/docker` is copied from it, into the orchestrator image (the orchestrator starts sandboxes with the CLI, through `dockerproxy`). |
| dockerproxy | in-repo (`services/dockerproxy`) | ours | Standard library only; no third-party dependency. The OSS alternative considered (Tecnativa docker-socket-proxy, Apache-2.0, HAProxy-based) filters by endpoint only and cannot refuse a create by its body, so it was not used. |
| `@vercel/og` (Satori, resvg, Yoga; Geist font) | 1.0.1, compiled into `next/dist/compiled/@vercel/og` | MPL-2.0 (package LICENSE and `package.json`); Geist is OFL-1.1 | Renders the generated OG image (`opengraph-image.tsx`) in the web server. Used unmodified, as shipped inside Next.js; MPL-2.0 is file-level copyleft and we change none of its files. Not part of the browser bundle. |
