"""Watch a model play a game bundle locally — no web stack, no sandbox. Reads the provider key
from MEMORYLAB_PROVIDER_KEY (e.g. a gitignored .env). The key stays in a SecretKey and is never
printed. Prints each step's observation, the model's move(s), and token usage; saves frames.

    set -a; . ./.env; set +a
    .venv/bin/python -I scripts/play_local.py --bundle packages/games/pokemon_red \\
        --provider nvidia --model meta/llama-3.3-70b-instruct --steps 30

This is the local, single-run path; the full platform run (web UI, sandbox, orchestrator) needs the
key validator wired (BLOCKED B-4). Here the model talks to the provider directly over TLS.
"""

import argparse
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from labharness.agent import AgentLoop, Caps, MemoryConfig, RunConfig
from labharness.bundle import load_bundle
from labharness.emulator import Limits
from labharness.presets import custom_preset, get_preset
from labharness.providers.anthropic import AnthropicProvider
from labharness.providers.openai_compat import OpenAICompatProvider
from labharness.providers.registry import OPENAI_COMPAT
from labharness.providers.secret import SecretKey
from labharness.screenshots import ScreenshotPolicy
from labharness.session import Session


class PrintSink:
    """Minimal event sink: narrate the run to stdout."""

    def __init__(self) -> None:
        self.frames_dir: Path | None = None

    def emit(self, type_: str, step: int, data: dict[str, Any]) -> None:
        if type_ == "obs":
            d = data
            extra = f" dialog={'open' if d.get('dialog_active') else 'closed'}"
            print(f"\n[step {step}] map={d.get('map_id')} x={d.get('x')} y={d.get('y')}{extra} score={d.get('score')}")
        elif type_ == "action":
            ok = "ok" if data.get("ok") else "REJECTED"
            print(f"    -> {data.get('tool')} {data.get('args')}  ({ok}, {data.get('frames')} frames)")
        elif type_ == "model":
            txt = (data.get("summary") or "").strip().replace("\n", " ")
            if txt:
                print(f"    model: {txt[:200]}")
        elif type_.startswith("mem."):
            print(f"    mem: {type_.split('.',1)[1]} {data.get('path','')}")
        elif type_ == "usage":
            print(f"    tokens so far: {data.get('total')}")
        elif type_ == "detector":
            print(f"    !! {data.get('kind')}: {{k:v for k,v in data.items() if k!='kind'}}")


def make_provider(provider: str, key: SecretKey, model: str):  # type: ignore[no-untyped-def]
    if provider == "anthropic":
        return AnthropicProvider(key, model)
    if provider in OPENAI_COMPAT:
        return OpenAICompatProvider(provider, key, model)
    raise SystemExit(f"unknown provider {provider!r}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--provider", default="nvidia")
    ap.add_argument("--model", required=True)
    ap.add_argument("--preset", default="general@1")
    ap.add_argument("--guide", default=None,
                    help="custom guidance text (uses the custom preset instead of --preset; demo mode)")
    ap.add_argument("--steps", type=int, default=30)
    ap.add_argument("--max-output-tokens", type=int, default=512)
    ap.add_argument("--screenshots", default="never", choices=["never", "every_k", "every_k_or_change"])
    ap.add_argument("--frames-dir", default="/tmp/play_frames")
    args = ap.parse_args()

    raw = os.environ.get("MEMORYLAB_PROVIDER_KEY", "")
    if not raw or raw == "nvapi-REPLACE_ME":
        raise SystemExit("set MEMORYLAB_PROVIDER_KEY (e.g. `set -a; . ./.env; set +a`)")
    key = SecretKey(raw)

    bundle = load_bundle(args.bundle)
    shots = ScreenshotPolicy(mode=args.screenshots)
    preset = custom_preset(args.guide) if args.guide else get_preset(args.preset)
    config = RunConfig(
        provider=args.provider, model_id=args.model, preset_ref=preset.ref, mode="demo",
        memory=MemoryConfig(), screenshots=shots,
        caps=Caps(max_steps=args.steps, max_output_tokens=args.max_output_tokens, context_steps=6),
    )
    provider = make_provider(args.provider, key, args.model)

    frames_dir = Path(args.frames_dir)
    frames_dir.mkdir(parents=True, exist_ok=True)
    sink = PrintSink()

    def frame_sink(step: int, png: bytes) -> None:
        (frames_dir / f"step_{step:04d}.png").write_bytes(png)

    run_id = str(uuid.uuid4())
    print(f"=== {args.provider}/{args.model} plays {bundle.game_id} — {args.steps} steps, screenshots={args.screenshots} ===")
    print(f"frames -> {frames_dir}")
    try:
        with Session(bundle, Limits(), shots) as session:
            loop = AgentLoop(session, provider, config, preset, sink, run_id=run_id,
                             content_filter=lambda s: s, frame_sink=frame_sink)
            result = loop.run()
        print(f"\n=== done: {result.state} ({result.reason}) after {result.steps} steps, {result.tokens} tokens ===")
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
