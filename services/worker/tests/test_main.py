import copy
import io
import json
import os
import threading
import time
from pathlib import Path

import pytest
from labharness.providers.registry import PROVIDER_MODELS
from runlog.eventlog import EventLogReader

from conftest import CANARY, GAMES, RUN_CONFIG, Env
from worker import main as worker_main
from worker.main import main
from worker.spec import parse_spec


@pytest.mark.parametrize("name,cls_name", [
    ("anthropic", "AnthropicProvider"), ("openai", "OpenAICompatProvider"), ("openrouter", "OpenAICompatProvider"),
    ("groq", "OpenAICompatProvider"), ("nvidia", "OpenAICompatProvider")])
def test_make_provider_picks_the_right_adapter(name: str, cls_name: str) -> None:
    run_config = copy.deepcopy(RUN_CONFIG)
    run_config["provider"], run_config["model_id"] = name, PROVIDER_MODELS[name][0]
    spec = {"v": 1, "run_id": "11111111-2222-3333-4444-555555555555", "game": "fixture", "run_config": run_config,
            "ingest": {"url": "http://ingest:8000", "token": "tok-tok-tok-tok"},
            "provider": {"name": name, "api_key": CANARY}, "heartbeat_s": 5}
    job = parse_spec(json.dumps(spec).encode(), allow_fake=False)
    provider = worker_main.make_provider(job, "http://ml-egress:3128")
    assert type(provider).__name__ == cls_name and provider.name == name
    close = getattr(provider, "close", None)
    if callable(close):
        close()


def run_main(env: Env, stdin: io.StringIO, **kw):  # type: ignore[no-untyped-def]
    out = io.StringIO()
    code = main(stdin, out, http_client=env.client, games_dir=GAMES, **kw)
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    return code, lines, out.getvalue()


def test_a_full_run_streams_events_frames_and_a_summary(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    run_id = env.run()
    code, lines, raw = run_main(env, env.line(run_id))
    assert code == 0
    done = lines[-1]["done"]
    assert (done["state"], done["reason"], done["steps"]) == ("capped", "steps", 4)
    assert env.repo.get(run_id).status == "capped" and env.repo.get(run_id).steps == 4
    events = list(EventLogReader(env.store, run_id).iter_events())
    assert [e.data["state"] for e in events if e.type == "run.state"] == ["starting", "running", "stopping", "capped"]
    frames = sorted(p.name for p in (env.store.root / "runs" / run_id / "frames").iterdir())
    assert frames[0] == "0000000.png" and env.repo.get(run_id).frames == len(frames) >= 1
    err = capsys.readouterr()
    assert err.err == "" and err.out == ""                                    # stdout is ours, nothing leaks to the real streams
    assert CANARY not in raw


def test_the_key_in_the_spec_never_reaches_stdout_or_the_log_even_if_the_model_echoes_it(env: Env) -> None:
    run_id = env.run()
    script = [{"calls": [["press", {"buttons": ["Right"], "hold_frames": 1}], ["mem_write", {"path": "n", "content": f"key {CANARY}"}]],
               "text": f"the key is {CANARY}", "tokens": 100}] * 2 + [{"error": True, "message": f"provider said {CANARY}"}]
    spec_overrides = {"provider": {"name": "fake", "api_key": CANARY, "script": script}}
    code, lines, raw = run_main(env, env.line(run_id, **spec_overrides))
    assert code == 0 and lines[-1]["done"]["state"] == "failed" and lines[-1]["done"]["redaction_hits"] >= 3
    blob = b"".join(p.read_bytes() for p in env.store.root.rglob("*") if p.is_file()).decode("latin-1")
    assert CANARY not in blob and CANARY not in raw and CANARY not in repr(env.repo.runs) + repr(env.repo.memory_rows)


def test_heartbeats_flow_while_the_run_is_busy(env: Env) -> None:
    run_id = env.run()
    slow = [{"calls": [["wait", {"frames": 1}]], "tokens": 10}] * 3
    original = worker_main.make_provider

    def slow_provider(job, proxy):  # type: ignore[no-untyped-def]
        provider = original(job, proxy)
        real = provider.step

        def step(request):  # type: ignore[no-untyped-def]
            time.sleep(0.25)
            return real(request)

        provider.step = step
        return provider

    worker_main.make_provider = slow_provider  # type: ignore[assignment]
    try:
        code, lines, _ = run_main(env, env.line(run_id, provider={"name": "fake", "api_key": CANARY, "script": slow},
                                                run_config={**RUN_CONFIG, "caps": {"max_steps": 3}}))
    finally:
        worker_main.make_provider = original  # type: ignore[assignment]
    beats = [x["hb"] for x in lines if "hb" in x]
    assert code == 0 and len(beats) >= 3 and beats == sorted(beats) and "done" in lines[-1]


def test_a_stop_command_on_stdin_ends_the_run_gracefully(env: Env) -> None:
    run_id = env.run()
    many = [{"calls": [["wait", {"frames": 1}]], "tokens": 10}] * 50
    stdin = env.line(run_id, '{"cmd":"stop"}\n', provider={"name": "fake", "script": many},
                     run_config={**RUN_CONFIG, "caps": {"max_steps": 50}})
    code, lines, _ = run_main(env, stdin)
    done = lines[-1]["done"]
    assert code == 0 and (done["state"], done["reason"]) == ("completed", "user_stop") and done["steps"] < 50


def test_junk_on_the_control_channel_is_ignored(env: Env) -> None:
    run_id = env.run()
    code, lines, raw = run_main(env, env.line(run_id, 'not json\n[]\n{"cmd":"explode"}\n' + "x" * 10_000 + "\n"))
    assert code == 0 and lines[-1]["done"]["state"] == "capped" and "explode" not in raw


@pytest.mark.parametrize("spec_text", ["", "{", '{"v":1}', '{"v":2}', "[]"])
def test_a_bad_spec_exits_2_with_only_a_type_name(env: Env, spec_text: str) -> None:
    code, lines, raw = run_main(env, io.StringIO(spec_text + "\n"))
    assert code == 2 and lines == [{"fatal": "SpecError"}]


def test_a_bad_spec_does_not_echo_the_key(env: Env) -> None:
    run_id = env.run()
    code, lines, raw = run_main(env, env.line(run_id, game="../etc"))
    assert code == 2 and CANARY not in raw


def test_ingest_refusal_is_exit_3(env: Env) -> None:
    run_id = env.run()
    bad_token = env.spec(run_id)["ingest"]["token"][:-3] + "AAA"
    code, lines, raw = run_main(env, env.line(run_id, ingest={"url": "http://ingest.test:8000", "token": bad_token}))
    assert code == 3 and lines[-1] == {"fatal": "IngestRejected"}


def test_unexpected_errors_report_only_the_type_never_the_message(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = env.run()

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError(f"something with {CANARY}")

    monkeypatch.setattr(worker_main, "run_job", boom)
    code, lines, raw = run_main(env, env.line(run_id))
    assert code == 1 and lines == [{"fatal": "RuntimeError"}] and CANARY not in raw


def test_the_fake_provider_is_refused_without_the_environment_flag(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEMORYLAB_ALLOW_FAKE_PROVIDER")
    code, lines, _ = run_main(env, env.line(env.run()))
    assert code == 2 and lines == [{"fatal": "SpecError"}]


def test_the_worker_writes_nothing_to_its_working_directory(env: Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    run_main(env, env.line(env.run()))
    assert os.listdir(cwd) == []
    assert threading.active_count() >= 1


def test_no_background_thread_writes_after_main_returns_and_the_caller_closes_stdout(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: a heartbeat that had passed its `done` check just before the run ended could still write after main returned and the
    caller closed stdout ('I/O operation on closed file', raised in a thread). main now waits for the heartbeat thread."""
    seen: list[str] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: seen.append(f"{args.exc_type.__name__}: {args.exc_value}"))

    class LateEvent(threading.Event):
        """wait() answers 'not set' once more after set(): the thread was already past its check when the run ended."""

        stale_given = False

        def wait(self, timeout: float | None = None) -> bool:
            time.sleep(timeout or 0)
            if self.is_set() and not self.stale_given:
                self.stale_given = True
                return False
            return self.is_set()

    class Shim:
        Thread, Lock, Event = threading.Thread, threading.Lock, LateEvent

    monkeypatch.setattr(worker_main, "threading", Shim)
    run_id = env.run()
    out = io.StringIO()
    assert main(env.line(run_id, heartbeat_s=0.2), out, http_client=env.client, games_dir=GAMES) == 0
    out.close()                                                   # what the caller (a test, the orchestrator's pipe owner) does next
    time.sleep(0.6)                                               # a straggler would write to the closed stream now
    assert seen == []
