"""Worker process: runs exactly one run, then exits.

stdin  : line 1 = job spec (JSON, includes the API key); later lines = control commands
         (`{"cmd":"stop"}` for a graceful stop).
stdout : one JSON object per line: heartbeats `{"hb":n}`, a final `{"done":{...}}` or `{"fatal":"Type"}`.
stderr : nothing (messages could carry secrets). Exit codes: 0 run ended (its state is in the log),
         2 bad spec, 3 ingest rejected/unavailable, 1 anything else.
"""

import json
import logging
import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

import httpx
from labharness import Limits, Session, load_bundle
from labharness.agent import AgentLoop, ResumeState
from labharness.presets import custom_preset, get_preset
from labharness.providers import FakeProvider, Provider, ProviderError, ScriptedStep, Usage
from labharness.providers.anthropic import AnthropicProvider
from labharness.providers.openai_compat import OpenAICompatProvider
from labharness.providers.registry import OPENAI_COMPAT
from labharness.sessions import checkpoint_from_event
from runlog.client import HttpEventSink, IngestRejected, IngestUnavailable, RedactingSink
from runlog.redaction import Redactor, ScrubFilter

from .spec import Job, SpecError, parse_spec

GAMES_DIR = Path(os.environ.get("MEMORYLAB_GAMES_DIR", "/opt/games"))
MAX_LINE = 4096


def make_provider(job: Job, proxy: str | None) -> Provider:
    if job.provider_name == "anthropic":
        assert job.api_key is not None
        return AnthropicProvider(job.api_key, job.config.model_id, proxy=proxy)
    if job.provider_name in OPENAI_COMPAT:
        assert job.api_key is not None
        return OpenAICompatProvider(job.provider_name, job.api_key, job.config.model_id, proxy=proxy)
    steps: list[Any] = []
    for item in job.fake_script or []:
        if "error" in item:
            steps.append(ProviderError(str(item.get("message", "scripted error"))))
        else:
            calls = tuple((c[0], c[1]) for c in item.get("calls", []))
            steps.append(ScriptedStep(calls, item.get("text", ""), Usage(item.get("tokens", 100), 0)))
    return FakeProvider(steps, job.config.model_id)


def load_resume(job: Job, http_sink: HttpEventSink) -> ResumeState:
    """A forked run's starting point, fetched from ingest (which knows the parent only through the database link the API created)."""
    payload, state = http_sink.fetch_parent()
    try:
        cp = checkpoint_from_event(payload["checkpoint"], state)
        events = tuple((str(m["type"]), int(m["step"]), dict(m["data"])) for m in payload["memory_events"])
        parent_id = str(payload["parent_run_id"])
        context = dict(payload["context"])
    except (KeyError, TypeError, ValueError):
        raise IngestRejected("malformed parent checkpoint") from None
    if job.parent is not None and (parent_id != job.parent["run_id"] or cp.step != job.parent["step"]):
        raise IngestRejected("the parent checkpoint is not the one this run was started from")
    return ResumeState(cp, context, events, parent_id, {})


def run_job(job: Job, *, stop: threading.Event, out: Callable[[dict[str, Any]], None],
            http_client: httpx.Client | None = None, games_dir: Path | None = None) -> dict[str, Any]:
    """Run one job to its end and return the summary. Everything leaving the process (events, frames,
    stdout) passes the redactor; the key lives only in the provider and the redactor."""
    redactor = Redactor([job.api_key.reveal()] if job.api_key else [])
    client = http_client or httpx.Client(base_url=job.ingest_url, proxy=job.proxy, timeout=30.0, trust_env=False,
                                         follow_redirects=False)
    http_sink = HttpEventSink(client, job.run_id, job.ingest_token)
    sink = RedactingSink(http_sink, redactor)
    game = load_bundle((games_dir or GAMES_DIR) / job.game)
    preset = custom_preset(job.preset_text or "") if job.config.preset_ref.startswith("custom@") else get_preset(job.config.preset_ref)
    provider = make_provider(job, job.proxy)
    resume = load_resume(job, http_sink) if job.parent is not None else None
    try:
        with Session(game, Limits(), job.config.screenshots) as session:
            loop = AgentLoop(session, provider, job.config, preset, sink, run_id=job.run_id, stop=stop,
                             content_filter=redactor.text, frame_sink=http_sink.upload_frame, resume=resume,
                             checkpoint_sink=lambda cp, ctx: http_sink.upload_checkpoint(cp, redactor.obj(ctx)))
            result = loop.run()
        http_sink.flush()
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()
    return {"state": result.state, "reason": result.reason, "steps": result.steps, "tokens": result.tokens,
            "redaction_hits": redactor.hits}


def _heartbeat(period: float, out: Callable[[dict[str, Any]], None], done: threading.Event) -> None:
    n = 0
    while not done.wait(period):
        n += 1
        out({"hb": n})


def _control(stdin: TextIO, stop: threading.Event) -> None:
    try:
        for line in iter(lambda: stdin.readline(MAX_LINE), ""):
            try:
                if json.loads(line).get("cmd") == "stop":
                    stop.set()
            except (ValueError, AttributeError):
                continue  # ignore junk; never echo it
    except (ValueError, OSError):
        return          # the stream was closed under us (the process is ending)


def main(stdin: TextIO | None = None, stdout: TextIO | None = None, *, http_client: httpx.Client | None = None,
         games_dir: Path | None = None) -> int:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    lock = threading.Lock()

    def out(obj: dict[str, Any]) -> None:
        with lock:
            stdout.write(json.dumps(obj, separators=(",", ":")) + "\n")
            stdout.flush()

    logging.basicConfig(level=logging.CRITICAL)
    for handler in logging.getLogger().handlers:
        handler.addFilter(ScrubFilter())
    try:
        raw = stdin.readline(256 * 1024 + 2).encode()
        job = parse_spec(raw, allow_fake=os.environ.get("MEMORYLAB_ALLOW_FAKE_PROVIDER") == "1")
    except SpecError:
        out({"fatal": "SpecError"})
        return 2
    del raw
    stop, done = threading.Event(), threading.Event()
    threading.Thread(target=_control, args=(stdin, stop), daemon=True).start()
    beat = threading.Thread(target=_heartbeat, args=(job.heartbeat_s, out, done), daemon=True)
    beat.start()
    try:
        summary = run_job(job, stop=stop, out=out, http_client=http_client, games_dir=games_dir)
    except (IngestRejected, IngestUnavailable) as exc:
        out({"fatal": type(exc).__name__})
        return 3
    except Exception as exc:  # noqa: BLE001 - report the type only: messages may hold secrets
        out({"fatal": type(exc).__name__})
        return 1
    finally:
        done.set()
        beat.join(timeout=5)     # no heartbeat may be mid-write when the caller closes stdout (it raised "I/O operation on closed file")
    out({"done": summary})
    return 0


if __name__ == "__main__":  # the emulator child is spawned, which re-imports __main__
    sys.exit(main())
