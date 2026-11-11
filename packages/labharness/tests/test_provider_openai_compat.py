import copy
import json
import logging
import pickle
import socket

import httpx
import pytest

from labharness.providers import (
    BadRequest,
    BadResponse,
    ImageBlock,
    KeyRejected,
    Message,
    Overloaded,
    ProviderError,
    ProviderRequest,
    ProviderTimeout,
    SecretKey,
    ServerError,
    SystemBlock,
    TextBlock,
    ToolResultBlock,
    ToolSchema,
    ToolUseBlock,
)
from labharness.providers.openai_compat import MAX_RESPONSE_BYTES, OpenAICompatProvider
from labharness.providers.registry import OPENAI_COMPAT, PROVIDER_HOSTS, PROVIDER_MODELS

CANARY = "sk-or-CANARYCANARYCANARYCANARYCANARY0123456789-canary"
OK_BODY = {
    "id": "chatcmpl-1", "object": "chat.completion", "model": "gpt-4.1",
    "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": "Going right.",
        "tool_calls": [{"id": "call_1", "type": "function",
                        "function": {"name": "press", "arguments": "{\"buttons\": [\"Right\"], \"hold_frames\": 2}"}}]}}],
    "usage": {"prompt_tokens": 120, "completion_tokens": 30, "prompt_tokens_details": {"cached_tokens": 7}},
}
TOOLS = (ToolSchema("press", "press buttons", {"type": "object", "properties": {}, "additionalProperties": False}),)
# one provider per base-path shape
EACH = ("openai", "openrouter", "groq", "nvidia")


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(*_a: object, **_k: object) -> None:
        raise AssertionError("real network access attempted")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)


def req(**kw: object) -> ProviderRequest:
    base = {"system": (SystemBlock("You play a game.", cache=True),),
            "messages": (Message("user", (TextBlock("obs"),)),), "tools": TOOLS,
            "max_output_tokens": 512, "timeout_s": 30.0}
    return ProviderRequest(**{**base, **kw})  # type: ignore[arg-type]


class Rec:
    def __init__(self, *responses: object) -> None:
        self.queue = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, httpx.Response)
        return item


def resp(status: int = 200, body: object = OK_BODY, headers: dict[str, str] | None = None) -> httpx.Response:
    content = body if isinstance(body, bytes) else json.dumps(body).encode()
    return httpx.Response(status, content=content, headers={"x-request-id": "req_abc", **(headers or {})})


def provider(rec: Rec, name: str = "openai", model: str | None = None, **kw: object) -> tuple[OpenAICompatProvider, list[float]]:
    sleeps: list[float] = []
    model_id = model or PROVIDER_MODELS[name][0]
    p = OpenAICompatProvider(name, SecretKey(CANARY), model_id, transport=httpx.MockTransport(rec),
                             sleep=sleeps.append, jitter=lambda: 0.0, **kw)  # type: ignore[arg-type]
    return p, sleeps


# ---- construction ----------------------------------------------------------------------------
def test_rejects_non_openai_compat_provider() -> None:
    with pytest.raises(ValueError, match="OpenAI-compatible"):
        OpenAICompatProvider("anthropic", SecretKey(CANARY), "claude-haiku-5-5")


def test_rejects_unlisted_model() -> None:
    with pytest.raises(ValueError, match="allowlisted"):
        OpenAICompatProvider("groq", SecretKey(CANARY), "not-a-real-model")


def test_requires_secret_key() -> None:
    with pytest.raises(TypeError):
        OpenAICompatProvider("openai", CANARY, "gpt-4.1")  # type: ignore[arg-type]


# ---- request shape ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", EACH)
def test_request_url_host_path_and_auth(name: str) -> None:
    rec = Rec(resp())
    p, _ = provider(rec, name)
    p.step(req())
    request = rec.requests[0]
    assert request.url.host == PROVIDER_HOSTS[name]
    assert request.url.path == f"{OPENAI_COMPAT[name]}/chat/completions"
    assert request.url.scheme == "https"
    assert request.headers["authorization"] == f"Bearer {CANARY}"


def test_request_body_shape() -> None:
    rec = Rec(resp())
    p, _ = provider(rec, "openrouter")
    p.step(req())
    body = json.loads(rec.requests[0].content)
    assert body["model"] == PROVIDER_MODELS["openrouter"][0]
    assert body["max_tokens"] == 512                       # non-openai providers use max_tokens
    assert body["messages"][0] == {"role": "system", "content": "You play a game."}
    assert body["messages"][1] == {"role": "user", "content": "obs"}
    assert body["tool_choice"] == "required"
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["function"]["name"] == "press"


def test_openai_uses_max_completion_tokens() -> None:
    rec = Rec(resp())
    p, _ = provider(rec, "openai")
    p.step(req())
    body = json.loads(rec.requests[0].content)
    assert body["max_completion_tokens"] == 512 and "max_tokens" not in body


def test_image_block_becomes_data_url() -> None:
    rec = Rec(resp())
    p, _ = provider(rec, "openai")
    p.step(req(messages=(Message("user", (TextBlock("see"), ImageBlock(b"\x89PNG..."))),)))
    body = json.loads(rec.requests[0].content)
    content = body["messages"][1]["content"]
    assert content[0] == {"type": "text", "text": "see"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_tool_use_and_tool_result_roundtrip_to_openai_messages() -> None:
    rec = Rec(resp())
    p, _ = provider(rec, "openai")
    messages = (
        Message("assistant", (TextBlock("I will press"), ToolUseBlock("call_9", "press", {"buttons": ["A"]}))),
        Message("user", (ToolResultBlock("call_9", "pressed A"),)),
    )
    p.step(req(messages=messages))
    msgs = json.loads(rec.requests[0].content)["messages"]
    assistant = next(m for m in msgs if m["role"] == "assistant")
    assert assistant["content"] == "I will press"
    assert assistant["tool_calls"][0] == {"id": "call_9", "type": "function",
                                          "function": {"name": "press", "arguments": "{\"buttons\":[\"A\"]}"}}
    tool = next(m for m in msgs if m["role"] == "tool")
    assert tool == {"role": "tool", "tool_call_id": "call_9", "content": "pressed A"}


def test_no_tools_omits_tool_choice() -> None:
    rec = Rec(resp())
    p, _ = provider(rec, "openai")
    p.step(req(tools=()))
    body = json.loads(rec.requests[0].content)
    assert "tools" not in body and "tool_choice" not in body


def test_max_output_tokens_bounds() -> None:
    p, _ = provider(Rec(resp()), "openai")
    with pytest.raises(BadRequest):
        p.step(req(max_output_tokens=0))
    with pytest.raises(BadRequest):
        p.step(req(max_output_tokens=70000))


# ---- response parsing ------------------------------------------------------------------------
def test_parse_tool_calls_text_and_usage() -> None:
    rec = Rec(resp())
    p, _ = provider(rec, "openai")
    result = p.step(req())
    assert result.summary == "Going right."
    assert len(result.actions) == 1
    assert result.actions[0].name == "press"
    assert result.actions[0].input == {"buttons": ["Right"], "hold_frames": 2}
    assert result.usage.input_tokens == 120 and result.usage.output_tokens == 30
    assert result.usage.cache_read_input_tokens == 7
    assert result.usage.total == 157
    assert result.stop_reason == "tool_calls"
    assert result.request_id == "req_abc"


def test_parse_text_only_response() -> None:
    body = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "done"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2}}
    p, _ = provider(Rec(resp(body=body)), "groq")
    result = p.step(req())
    assert result.summary == "done" and result.actions == ()


@pytest.mark.parametrize("body", [
    {"usage": {}},                                                           # no choices
    {"choices": []},                                                         # empty choices
    {"choices": [{"message": {"role": "assistant", "content": "x"}}]},       # no usage
    b"not json",
])
def test_bad_responses(body: object) -> None:
    p, _ = provider(Rec(resp(body=body)), "openai")
    with pytest.raises(BadResponse):
        p.step(req())


def test_tool_call_arguments_must_be_json_object() -> None:
    body = copy.deepcopy(OK_BODY)
    body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "not json"  # type: ignore[index]
    p, _ = provider(Rec(resp(body=body)), "openai")
    with pytest.raises(BadResponse):
        p.step(req())


# ---- error mapping ---------------------------------------------------------------------------
@pytest.mark.parametrize("status,exc", [(401, KeyRejected), (403, KeyRejected), (400, BadRequest),
                                        (500, ServerError), (502, ServerError), (503, Overloaded), (529, Overloaded),
                                        (301, BadResponse)])
def test_error_status_mapping(status: int, exc: type[ProviderError]) -> None:
    p, _ = provider(Rec(resp(status=status, body={"error": {"type": "bad_thing"}})), "openai", max_retries=0)
    with pytest.raises(exc):
        p.step(req())


def test_rate_limited_reads_retry_after_and_retries() -> None:
    rec = Rec(resp(status=429, headers={"retry-after": "2"}), resp())
    p, sleeps = provider(rec, "groq")
    result = p.step(req())
    assert result.summary == "Going right."
    assert sleeps and sleeps[0] >= 2.0


def test_server_error_then_success_retries() -> None:
    rec = Rec(resp(status=500), resp())
    p, sleeps = provider(rec, "nvidia")
    assert p.step(req()).summary == "Going right."
    assert len(rec.requests) == 2 and sleeps


def test_timeout_is_mapped() -> None:
    p, _ = provider(Rec(httpx.ConnectTimeout("slow")), "openai", max_retries=0)
    with pytest.raises(ProviderTimeout):
        p.step(req())


def test_transport_error_is_server_error() -> None:
    p, _ = provider(Rec(httpx.ConnectError("no route")), "openai", max_retries=0)
    with pytest.raises(ServerError):
        p.step(req())


def test_oversized_response_is_rejected() -> None:
    big = b'{"choices":[{"message":{"content":"' + b"a" * (MAX_RESPONSE_BYTES + 1) + b'"}}]}'
    p, _ = provider(Rec(resp(body=big)), "openai", max_retries=0)
    with pytest.raises(BadResponse):
        p.step(req())


# ---- key secrecy -----------------------------------------------------------------------------
def test_key_never_appears_in_repr_or_errors(caplog: pytest.LogCaptureFixture) -> None:
    rec = Rec(resp(status=401, body={"error": {"type": "invalid_api_key"}}))
    p, _ = provider(rec, "openai", max_retries=0)
    assert CANARY not in repr(p)
    with caplog.at_level(logging.DEBUG), pytest.raises(KeyRejected) as ei:
        p.step(req())
    assert CANARY not in str(ei.value)
    assert CANARY not in caplog.text


def test_secret_key_cannot_be_pickled() -> None:
    with pytest.raises(TypeError):
        pickle.dumps(SecretKey(CANARY))


# ---- proxy / environment isolation -----------------------------------------------------------
def test_proxy_shape_is_validated() -> None:
    with pytest.raises(ValueError, match="proxy"):
        OpenAICompatProvider("openai", SecretKey(CANARY), "gpt-4.1", proxy="http://user:pw@host:3128/path")


def test_env_is_not_trusted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://evil:3128")
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent")
    rec = Rec(resp())
    p, _ = provider(rec, "openai")           # construction must not read the environment
    p.step(req())
    assert rec.requests[0].url.host == PROVIDER_HOSTS["openai"]


# ---- validate_key ----------------------------------------------------------------------------
def test_validate_key_ok_and_rejected() -> None:
    ok = Rec(resp(body={"data": []}))
    provider(ok, "openai")[0].validate_key()
    assert ok.requests[0].url.path == "/v1/models" and ok.requests[0].method == "GET"
    with pytest.raises(KeyRejected):
        provider(Rec(resp(status=401)), "openai", max_retries=0)[0].validate_key()
