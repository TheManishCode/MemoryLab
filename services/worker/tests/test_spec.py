import copy
import json

import pytest

from conftest import CANARY, RUN_CONFIG
from worker.spec import MAX_SPEC_BYTES, SpecError, parse_spec

RUN = "11111111-2222-3333-4444-555555555555"
GOOD = {"v": 1, "run_id": RUN, "game": "fixture", "run_config": RUN_CONFIG,
        "ingest": {"url": "http://ingest:8000", "token": "tok-tok-tok-tok", "proxy": "http://ml-egress:3128"},
        "provider": {"name": "anthropic", "api_key": CANARY}, "heartbeat_s": 5}


def parse(doc: object, *, fake: bool = False):  # type: ignore[no-untyped-def]
    return parse_spec(json.dumps(doc).encode(), allow_fake=fake)


def test_valid_spec() -> None:
    job = parse(GOOD)
    assert (job.run_id, job.game, job.provider_name, job.heartbeat_s) == (RUN, "fixture", "anthropic", 5.0)
    assert job.api_key is not None and job.api_key.reveal() == CANARY and CANARY not in repr(job) and CANARY not in str(job)
    assert job.proxy == "http://ml-egress:3128" and job.config.caps.max_steps == 4


def mutated(**changes: object) -> dict:  # type: ignore[type-arg]
    doc = copy.deepcopy(GOOD)
    doc.update(changes)
    return doc


BAD = {
    "wrong version": mutated(v=2), "missing run id": {k: v for k, v in GOOD.items() if k != "run_id"},
    "bad run id": mutated(run_id="nope"), "bad game": mutated(game="../etc"), "game upper": mutated(game="Fixture"),
    "unknown field": mutated(extra=1), "bad config": mutated(run_config={"provider": "x"}),
    "ingest not http": mutated(ingest={"url": "https://ingest:8000", "token": "tok-tok-tok-tok"}),
    "ingest has path": mutated(ingest={"url": "http://ingest:8000/x", "token": "tok-tok-tok-tok"}),
    "ingest unknown key": mutated(ingest={"url": "http://ingest:8000", "token": "tok-tok-tok-tok", "x": 1}),
    "token too short": mutated(ingest={"url": "http://ingest:8000", "token": "short"}),
    "proxy with creds": mutated(ingest={"url": "http://ingest:8000", "token": "tok-tok-tok-tok", "proxy": "http://u:p@x:1"}),
    "no provider": mutated(provider={}), "unknown provider": mutated(provider={"name": "mistral", "api_key": CANARY}),
    "key with space": mutated(provider={"name": "anthropic", "api_key": "has a space in it"}),
    "extra provider key": mutated(provider={"name": "anthropic", "api_key": CANARY, "script": []}),
    "openai extra key": mutated(provider={"name": "openai", "api_key": CANARY, "script": []}),
    "openai no key": mutated(provider={"name": "openai"}),
    "fake without flag": mutated(provider={"name": "fake", "script": []}),
    "heartbeat zero": mutated(heartbeat_s=0), "heartbeat huge": mutated(heartbeat_s=1000), "heartbeat bool": mutated(heartbeat_s=True),
    "preset_text number": mutated(preset_text=5),
}


@pytest.mark.parametrize("name", sorted(BAD))
def test_bad_specs_are_rejected_without_echoing_values(name: str) -> None:
    with pytest.raises(SpecError) as info:
        parse(BAD[name])
    assert CANARY not in str(info.value) and "tok-tok" not in str(info.value)


@pytest.mark.parametrize("raw", [b"", b"{", b"[]", b"null", b"\xff\xfe", b"x" * (MAX_SPEC_BYTES + 1)])
def test_garbage_and_oversize(raw: bytes) -> None:
    with pytest.raises(SpecError):
        parse_spec(raw, allow_fake=False)


@pytest.mark.parametrize("name", ["openai", "openrouter", "groq", "nvidia"])
def test_openai_compatible_providers_parse(name: str) -> None:
    job = parse(mutated(provider={"name": name, "api_key": CANARY}))
    assert job.provider_name == name
    assert job.api_key is not None and job.api_key.reveal() == CANARY
    assert job.fake_script is None and CANARY not in repr(job)


def test_fake_provider_only_with_the_flag() -> None:
    doc = mutated(provider={"name": "fake", "api_key": CANARY, "script": [{"calls": []}]})
    job = parse(doc, fake=True)
    assert job.provider_name == "fake" and job.fake_script == [{"calls": []}]
    with pytest.raises(SpecError):
        parse(doc, fake=False)
    with pytest.raises(SpecError):
        parse(mutated(provider={"name": "fake", "script": "x"}), fake=True)
