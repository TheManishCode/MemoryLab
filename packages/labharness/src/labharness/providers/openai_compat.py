"""One adapter for every provider that speaks the OpenAI Chat Completions API: OpenAI, OpenRouter,
Groq and NVIDIA (`registry.OPENAI_COMPAT` lists each host and base path).

Security properties are the same as the Anthropic adapter (tested with a canary key):
- the key lives in a `SecretKey`, is read once per request to build one `Authorization: Bearer`
  header, and never appears in reprs, exceptions, logs, or events;
- the host is fixed to the provider's host over TLS: no base-URL override, no redirects, no proxy
  or CA settings from the environment (`trust_env=False`); the only proxy is the one passed in
  (the run sandbox's egress proxy);
- errors are mapped to stable codes; response bodies and request details are not echoed.

NOT VERIFIED AGAINST ANY LIVE API in this repository's CI (no key available, BLOCKED B-1): the
request/response shapes follow the OpenAI Chat Completions API that all four providers implement
and are covered by a mock transport only. Small per-provider differences (OpenAI's newest models
want `max_completion_tokens`; some models do not support vision or `tool_choice: required`) are
documented, not exercised.
"""

import base64
import json
import random
import re
import time
from collections.abc import Callable
from typing import Any

import httpx

from .base import (
    BadRequest,
    BadResponse,
    ImageBlock,
    KeyRejected,
    Message,
    Overloaded,
    ProviderError,
    ProviderRequest,
    ProviderResult,
    ProviderTimeout,
    RateLimited,
    ServerError,
    TextBlock,
    ToolCall,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from .registry import OPENAI_COMPAT, PROVIDER_HOSTS, check_model
from .secret import SecretKey

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_RETRY_DELAY_S = 30.0
_SAFE_TYPE = re.compile(r"[a-z_.]{1,60}")
_PROXY = re.compile(r"http://[A-Za-z0-9.\-]{1,253}:[0-9]{1,5}")
# OpenAI's newest models reject `max_tokens` and want `max_completion_tokens`; the others accept
# `max_tokens`. We send the field the provider is known to accept.
_MAX_TOKENS_FIELD = {"openai": "max_completion_tokens"}


class OpenAICompatProvider:
    def __init__(
        self,
        provider: str,
        key: SecretKey,
        model_id: str,
        *,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
        proxy: str | None = None,
    ) -> None:
        if not isinstance(key, SecretKey):
            raise TypeError("key must be a SecretKey")
        if provider not in OPENAI_COMPAT:
            raise ValueError("provider is not an OpenAI-compatible provider")
        check_model(provider, model_id)
        if not 0 <= max_retries <= 6:
            raise ValueError("max_retries must be 0..6")
        self.name = provider
        self.model_id = model_id
        self._key = key
        self._base_path = OPENAI_COMPAT[provider].rstrip("/")
        self._max_tokens_field = _MAX_TOKENS_FIELD.get(provider, "max_tokens")
        self._max_retries = max_retries
        self._sleep, self._monotonic, self._jitter = sleep, monotonic, jitter
        if proxy is not None and not _PROXY.fullmatch(proxy):
            raise ValueError("proxy must look like http://host:port (no credentials, no path)")
        self._client = httpx.Client(
            base_url=f"https://{PROVIDER_HOSTS[provider]}", transport=transport, follow_redirects=False,
            trust_env=False, proxy=proxy,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
        )

    def __repr__(self) -> str:
        return f"OpenAICompatProvider(provider={self.name!r}, model_id={self.model_id!r})"

    def close(self) -> None:
        self._client.close()

    # ---- public API ---------------------------------------------------------------
    def validate_key(self, timeout_s: float = 10.0) -> None:
        """One cheap, token-free call (list models). Raises KeyRejected for a bad key; the
        caller must show only the generic `key_rejected` code."""
        status, _, _ = self._send("GET", f"{self._base_path}/models", None, timeout_s)
        if status != 200:
            raise self._error_for(status, b"", None)

    def step(self, request: ProviderRequest) -> ProviderResult:
        body = self._build_body(request)
        started = self._monotonic()
        status, raw, headers = self._send("POST", f"{self._base_path}/chat/completions", body, request.timeout_s)
        if status != 200:
            raise self._error_for(status, raw, headers)
        return self._parse(raw, headers.get("x-request-id") or headers.get("request-id"), self._monotonic() - started)

    # ---- request ----------------------------------------------------------------------
    def _build_body(self, request: ProviderRequest) -> bytes:
        if not 1 <= request.max_output_tokens <= 64000:
            raise BadRequest("max_output_tokens out of range")
        messages: list[dict[str, Any]] = []
        system_text = "\n\n".join(s.text for s in request.system)
        if system_text:
            messages.append({"role": "system", "content": system_text})
        for message in request.messages:
            messages.extend(self._messages(message))
        body: dict[str, Any] = {
            "model": self.model_id,
            self._max_tokens_field: request.max_output_tokens,
            "messages": messages,
        }
        if request.tools:
            body["tools"] = [
                {"type": "function",
                 "function": {"name": t.name, "description": t.description, "parameters": t.input_schema}}
                for t in request.tools
            ]
            body["tool_choice"] = "required"  # every step must act; text-only steps waste budget
        return json.dumps(body, separators=(",", ":"), ensure_ascii=True).encode()

    @staticmethod
    def _messages(message: Message) -> list[dict[str, Any]]:
        """One neutral Message becomes one or more OpenAI messages: tool results each become their
        own `role:"tool"` message; an assistant turn becomes one message with text and/or tool_calls."""
        if message.role not in ("user", "assistant"):
            raise BadRequest("unknown message role")
        out: list[dict[str, Any]] = []
        parts: list[dict[str, Any]] = []            # user/assistant content parts
        text_chunks: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in message.blocks:
            if isinstance(block, TextBlock):
                parts.append({"type": "text", "text": block.text})
                text_chunks.append(block.text)
            elif isinstance(block, ImageBlock):
                if message.role != "user":
                    raise BadRequest("images only allowed in user messages")
                data = base64.b64encode(block.png).decode()
                parts.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}})
            elif isinstance(block, ToolUseBlock):
                if message.role != "assistant":
                    raise BadRequest("tool_use only allowed in assistant messages")
                tool_calls.append({"id": block.id, "type": "function",
                                   "function": {"name": block.name, "arguments": json.dumps(block.input, separators=(",", ":"))}})
            elif isinstance(block, ToolResultBlock):
                if message.role != "user":
                    raise BadRequest("tool_result only allowed in user messages")
                out.append({"role": "tool", "tool_call_id": block.tool_use_id, "content": block.content})
        if message.role == "assistant":
            msg: dict[str, Any] = {"role": "assistant"}
            msg["content"] = "\n".join(text_chunks) if text_chunks else None
            if tool_calls:
                msg["tool_calls"] = tool_calls
            if msg["content"] is None and not tool_calls:
                raise BadRequest("empty assistant message")
            out.append(msg)
        elif parts:
            # A user turn with image or multiple parts uses the content-array form; plain text stays a string.
            if len(parts) == 1 and parts[0]["type"] == "text":
                out.append({"role": "user", "content": parts[0]["text"]})
            else:
                out.append({"role": "user", "content": parts})
        elif not out:
            raise BadRequest("empty message")
        return out

    # ---- transport with bounded retries -----------------------------------------------------
    def _send(self, method: str, path: str, body: bytes | None, total_timeout_s: float) -> tuple[int, bytes, httpx.Headers]:
        deadline = self._monotonic() + total_timeout_s
        last: ProviderError | None = None
        for attempt in range(self._max_retries + 1):
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                break
            result, failure = self._attempt(method, path, body, remaining)
            if failure is None and result is not None:
                status, raw, headers = result
                if status == 200 or not self._error_for(status, raw, headers).retryable:
                    return result
                failure = self._error_for(status, raw, headers)
            assert failure is not None
            last = failure
            if not failure.retryable or attempt == self._max_retries:
                break
            delay = self._delay(attempt, failure)
            if self._monotonic() + delay >= deadline:
                break
            self._sleep(delay)
        raise last or ProviderTimeout("call budget exhausted before a response")

    def _delay(self, attempt: int, failure: ProviderError) -> float:
        hinted = failure.retry_after_s if isinstance(failure, RateLimited) else None
        base = hinted if hinted is not None else min(MAX_RETRY_DELAY_S, 1.0 * 2**attempt)
        return min(MAX_RETRY_DELAY_S, base) * (1 + 0.25 * self._jitter())

    def _attempt(self, method: str, path: str, body: bytes | None, timeout_s: float,
                 ) -> tuple[tuple[int, bytes, httpx.Headers] | None, ProviderError | None]:
        headers = {"authorization": f"Bearer {self._key.reveal()}",
                   "content-type": "application/json", "user-agent": "memory-lab/0.2"}
        result: tuple[int, bytes, httpx.Headers] | None = None
        failure: ProviderError | None = None
        try:
            request = self._client.build_request(method, path, content=body, headers=headers,
                                                 timeout=httpx.Timeout(timeout_s))
            response = self._client.send(request, stream=True)
            try:
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        failure = BadResponse("response too large")
                        break
                    chunks.append(chunk)
                else:
                    result = (response.status_code, b"".join(chunks), response.headers)
            finally:
                response.close()
        except httpx.TimeoutException:
            failure = ProviderTimeout("provider call timed out")
        except httpx.HTTPError:
            failure = ServerError("could not reach the provider")
        # Raised outside the handler so no exception (which holds the request and its headers)
        # is chained onto ours.
        return result, failure

    # ---- responses ------------------------------------------------------------------------------
    @staticmethod
    def _error_for(status: int, raw: bytes, headers: httpx.Headers | None) -> ProviderError:
        kind = ""
        try:
            doc = json.loads(raw) if raw else {}
            err = doc.get("error") if isinstance(doc, dict) else None
            candidate = ""
            if isinstance(err, dict):
                candidate = err.get("type") or err.get("code") or ""
            elif isinstance(err, str):
                candidate = err
            kind = candidate if isinstance(candidate, str) and _SAFE_TYPE.fullmatch(candidate) else ""
        except (ValueError, AttributeError):
            kind = ""
        suffix = f" ({kind})" if kind else ""
        if status in (401, 403):
            return KeyRejected("the provider rejected the key")
        if status == 429:
            retry_after = None
            if headers is not None:
                try:
                    retry_after = max(0.0, min(MAX_RETRY_DELAY_S, float(headers.get("retry-after", ""))))
                except ValueError:
                    retry_after = None
            return RateLimited(f"rate limited{suffix}", retry_after)
        if status in (503, 529):
            return Overloaded(f"provider overloaded{suffix}")
        if 500 <= status < 600:
            return ServerError(f"provider error {status}{suffix}")
        if 300 <= status < 400:
            return BadResponse("unexpected redirect")
        return BadRequest(f"provider rejected the request ({status}){suffix}")

    @staticmethod
    def _parse(raw: bytes, request_id: str | None, latency_s: float) -> ProviderResult:
        doc: Any = None
        valid = True
        try:
            doc = json.loads(raw)
        except ValueError:
            valid = False
        if not valid:  # raised outside the handler: nothing is chained onto our error
            raise BadResponse("response was not valid JSON")
        if not isinstance(doc, dict) or not isinstance(doc.get("choices"), list) or not doc["choices"]:
            raise BadResponse("response has no choices")
        choice = doc["choices"][0]
        if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
            raise BadResponse("choice has no message")
        message = choice["message"]
        usage_doc = doc.get("usage")
        if not isinstance(usage_doc, dict):
            raise BadResponse("response has no usage")

        def count(name: str) -> int:
            value = usage_doc.get(name, 0)
            if value is None:
                return 0
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise BadResponse(f"usage.{name} is not a non-negative integer")
            return value

        cached = 0
        details = usage_doc.get("prompt_tokens_details")
        if isinstance(details, dict) and isinstance(details.get("cached_tokens"), int) \
                and not isinstance(details["cached_tokens"], bool) and details["cached_tokens"] >= 0:
            cached = details["cached_tokens"]
        usage = Usage(input_tokens=count("prompt_tokens"), output_tokens=count("completion_tokens"),
                      cache_read_input_tokens=cached)

        text = message.get("content")
        summary = text if isinstance(text, str) else ""
        blocks: list[TextBlock | ToolUseBlock] = []
        if summary:
            blocks.append(TextBlock(summary))
        calls: list[ToolCall] = []
        tool_calls = message.get("tool_calls")
        if tool_calls is not None:
            if not isinstance(tool_calls, list):
                raise BadResponse("tool_calls is not a list")
            for item in tool_calls:
                if not isinstance(item, dict) or not isinstance(item.get("function"), dict):
                    raise BadResponse("malformed tool_call")
                fn = item["function"]
                call_id, name_, args = item.get("id"), fn.get("name"), fn.get("arguments")
                if not (isinstance(call_id, str) and isinstance(name_, str) and isinstance(args, str)):
                    raise BadResponse("malformed tool_call fields")
                try:
                    parsed = json.loads(args) if args else {}
                except ValueError:
                    raise BadResponse("tool_call arguments were not valid JSON") from None
                if not isinstance(parsed, dict):
                    raise BadResponse("tool_call arguments were not an object")
                calls.append(ToolCall(call_id, name_, parsed))
                blocks.append(ToolUseBlock(call_id, name_, parsed))
        stop = choice.get("finish_reason")
        return ProviderResult(tuple(calls), summary, usage, stop if isinstance(stop, str) else "unknown",
                              request_id, latency_s, tuple(blocks))
