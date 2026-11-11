"""The job spec a worker receives on stdin (never argv or environment: those are visible via
`docker inspect` and /proc). One JSON line, bounded, strictly validated."""

import json
import re
from dataclasses import dataclass
from typing import Any

from labharness.agent import RunConfig
from labharness.providers.registry import OPENAI_COMPAT
from labharness.providers.secret import SecretKey

MAX_SPEC_BYTES = 256 * 1024
_GAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
_RUN_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_URL = re.compile(r"http://[A-Za-z0-9.\-]{1,253}:[0-9]{1,5}")


class SpecError(ValueError):
    """The spec is malformed. Messages never include spec values (they may hold a key)."""


@dataclass(frozen=True)
class Job:
    run_id: str
    game: str
    config: RunConfig
    preset_text: str | None
    ingest_url: str
    ingest_token: SecretKey
    proxy: str | None
    provider_name: str
    api_key: SecretKey | None
    fake_script: list[dict[str, Any]] | None
    heartbeat_s: float
    parent: dict[str, Any] | None = None   # a fork: {run_id, checkpoint_id, step}; the worker fetches the rest from ingest


def _need(doc: dict[str, Any], key: str, kind: type | tuple[type, ...]) -> Any:
    value = doc.get(key)
    if not isinstance(value, kind) or isinstance(value, bool) and kind is int:
        raise SpecError(f"field {key!r} has the wrong type")
    return value


def parse_spec(raw: bytes, *, allow_fake: bool) -> Job:
    if len(raw) > MAX_SPEC_BYTES:
        raise SpecError("spec too large")
    try:
        doc = json.loads(raw)
    except ValueError:
        raise SpecError("spec is not valid JSON") from None
    if not isinstance(doc, dict) or doc.get("v") != 1:
        raise SpecError("unsupported spec")
    if set(doc) - {"v", "run_id", "game", "run_config", "preset_text", "ingest", "provider", "heartbeat_s", "parent"}:
        raise SpecError("unknown spec fields")
    run_id = _need(doc, "run_id", str)
    game = _need(doc, "game", str)
    if not _RUN_ID.fullmatch(run_id) or not _GAME.fullmatch(game):
        raise SpecError("bad run_id or game")
    try:
        config = RunConfig.from_dict(doc.get("run_config"))
    except (ValueError, TypeError):
        raise SpecError("run_config rejected") from None
    preset_text = doc.get("preset_text")
    if preset_text is not None and not isinstance(preset_text, str):
        raise SpecError("preset_text must be a string")
    ingest = _need(doc, "ingest", dict)
    if set(ingest) - {"url", "token", "proxy"} or not {"url", "token"} <= set(ingest):
        raise SpecError("bad ingest section")
    url, token, proxy = ingest["url"], ingest["token"], ingest.get("proxy")
    if not isinstance(url, str) or not _URL.fullmatch(url) or (proxy is not None and (
            not isinstance(proxy, str) or not _URL.fullmatch(proxy))):
        raise SpecError("bad ingest url or proxy")
    prov = _need(doc, "provider", dict)
    name = prov.get("name")
    key: SecretKey | None = None
    script: list[dict[str, Any]] | None = None
    if name == "anthropic" or name in OPENAI_COMPAT:
        if set(prov) != {"name", "api_key"}:
            raise SpecError("bad provider section")
        key = _secret(prov["api_key"])
    elif name == "fake" and allow_fake:
        if set(prov) - {"name", "api_key", "script"}:
            raise SpecError("bad provider section")
        script = prov.get("script", [])
        if not isinstance(script, list) or not all(isinstance(s, dict) for s in script):
            raise SpecError("bad fake script")
        if prov.get("api_key") is not None:
            key = _secret(prov["api_key"])  # a canary key in tests, so redaction is exercised
    else:
        raise SpecError("provider not allowed")
    heartbeat = doc.get("heartbeat_s", 10.0)
    if isinstance(heartbeat, bool) or not isinstance(heartbeat, int | float) or not 0.05 <= heartbeat <= 60:
        raise SpecError("bad heartbeat_s")
    parent = doc.get("parent")
    if parent is not None and not _valid_parent(parent):
        raise SpecError("bad parent section")
    return Job(run_id, game, config, preset_text, url, _secret(token), proxy, name, key, script, float(heartbeat), parent)


def _secret(value: object) -> SecretKey:
    try:
        return SecretKey(value)  # type: ignore[arg-type]
    except ValueError:
        raise SpecError("a secret has an invalid shape") from None


def _valid_parent(parent: object) -> bool:
    return (isinstance(parent, dict) and set(parent) == {"run_id", "checkpoint_id", "step"} and isinstance(parent["run_id"], str)
            and _RUN_ID.fullmatch(parent["run_id"]) is not None and isinstance(parent["step"], int) and not isinstance(parent["step"], bool)
            and isinstance(parent["checkpoint_id"], str) and len(parent["checkpoint_id"]) <= 64)
