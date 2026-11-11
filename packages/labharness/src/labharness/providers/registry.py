"""Allowlist of providers and models the run gateway accepts (we are never a generic proxy).

Every entry here is reachable from three places that all read this one table: the public catalogue
(`GET /models`, so the web form and the CLI list exactly these), `check_model` (the API and the
orchestrator refuse anything not listed), and the egress proxy's host allowlist (compose passes one
`--allow <host>:443` per provider). Adding a provider or a model is a deliberate allowlist change:
the host must also be added to `infra/compose.yaml`'s egress command, and a provider key is still
refused until the key validator is wired (BLOCKED B-4), exactly as for Anthropic.

`anthropic` uses its own Messages-API adapter; every other provider below speaks the OpenAI
Chat Completions API and shares one adapter (`OpenAICompatProvider`), parameterised by host and
base path. The model lists are a curated starting point the owner can edit; models must support
tool calls (every step must act) and, for screenshot observations, vision.
"""

PROVIDER_MODELS: dict[str, tuple[str, ...]] = {
    "anthropic": ("claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"),
    "openai": ("gpt-5.1", "gpt-5.1-mini", "gpt-4.1", "gpt-4.1-mini"),
    "openrouter": ("openai/gpt-5.1", "anthropic/claude-sonnet-4.5", "google/gemini-2.5-pro", "meta-llama/llama-3.3-70b-instruct"),
    "groq": ("llama-3.3-70b-versatile", "llama-3.1-8b-instant", "openai/gpt-oss-120b", "moonshotai/kimi-k2-instruct"),
    "nvidia": ("meta/llama-3.3-70b-instruct", "deepseek-ai/deepseek-r1", "qwen/qwen2.5-coder-32b-instruct", "nvidia/llama-3.1-nemotron-70b-instruct"),
}
PROVIDER_HOSTS: dict[str, str] = {
    "anthropic": "api.anthropic.com",
    "openai": "api.openai.com",
    "openrouter": "openrouter.ai",
    "groq": "api.groq.com",
    "nvidia": "integrate.api.nvidia.com",
}
# Providers that speak the OpenAI Chat Completions API: the base path under the host where
# `/chat/completions` and `/models` live. (`anthropic` is not here: it has its own adapter.)
OPENAI_COMPAT: dict[str, str] = {
    "openai": "/v1",
    "openrouter": "/api/v1",
    "groq": "/openai/v1",
    "nvidia": "/v1",
}


def check_model(provider: str, model_id: str) -> None:
    if provider not in PROVIDER_MODELS:
        raise ValueError("provider is not allowlisted")
    if model_id not in PROVIDER_MODELS[provider]:
        raise ValueError("model is not allowlisted for this provider")
