"""Turn a Game Boy ROM the user already has into a ready-to-benchmark local game bundle.

    .venv/bin/python -I scripts/import_game.py --rom /path/to/game.gb --game-id my_game \\
        --statemap /path/to/statemap.json [--licence "..."] \\
        [--start-state /path/to.state | --boot-frames 900]

What it does, all locally (nothing is uploaded anywhere):
  1. validates the ROM header (refuses RTC carts the harness can't run deterministically);
  2. creates packages/games/<game-id>/ with a .gitignore so the ROM + state (copyrighted for a
     commercial game) are never committed;
  3. copies the ROM in and reads its sha256;
  4. installs the state map (RAM addresses for score/position/etc.), rewriting its rom_sha256 to
     this ROM and validating it against the harness schema;
  5. gets a start state: either the one you pass with --start-state, or a cold-boot state after
     --boot-frames frames (good for games playable straight from power-on / the title);
  6. writes bundle.json (every file pinned by sha256) and verifies it loads.

Then watch a model play it:
    set -a; . ./.env; set +a
    .venv/bin/python -I scripts/play_local.py --bundle packages/games/<game-id> \\
        --provider nvidia --model nvidia/nemotron-3.5-lightning-30b-a3b --steps 30

A state map is required because RAM addresses differ per game and cannot be guessed. For a game
with a long intro (e.g. Pokemon Red) make the start state past the intro and pass --start-state;
scripts/pokered/gen_start.py is an example of scripting that.
"""

import argparse
import hashlib
import io
import json
import shutil
import sys
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rom", required=True, help="path to the .gb/.gbc ROM the user owns")
    ap.add_argument("--game-id", required=True, help="[a-z][a-z0-9_]{0,31}; the bundle directory name")
    ap.add_argument("--statemap", required=True, help="path to a state map JSON (RAM addresses)")
    ap.add_argument("--licence", default="proprietary-local-only")
    ap.add_argument("--start-state", help="a PyBoy save state to start from (made with the pinned PyBoy)")
    ap.add_argument("--boot-frames", type=int, default=900,
                    help="if no --start-state, boot this many frames and save that as the start (default 900)")
    ap.add_argument("--release-frames", type=int, default=8)
    args = ap.parse_args()

    import re
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", args.game_id):
        raise SystemExit("--game-id must match [a-z][a-z0-9_]{0,31}")

    from labharness.rom import inspect_rom
    from labharness.statemap import parse_statemap

    rom_path = Path(args.rom)
    rom = rom_path.read_bytes()
    info = inspect_rom(rom)                       # raises on RTC carts / bad header
    print(f"ROM ok: title={info.title!r} cart=0x{info.cart_type:02X} size={info.size} sha256={info.sha256[:16]}…")

    out = ROOT / "packages/games" / args.game_id
    out.mkdir(parents=True, exist_ok=True)
    (out / ".gitignore").write_text(
        "# Local-only game. The ROM and save state may contain copyrighted material: never commit them.\n"
        "*.gb\n*.gbc\n*.state\n")
    rom_name = f"{args.game_id}.gb"
    shutil.copyfile(rom_path, out / rom_name)

    # State map: rewrite rom_sha256 to this ROM, then validate.
    sm_doc = json.loads(Path(args.statemap).read_bytes())
    sm_doc["rom_sha256"] = info.sha256
    sm_doc["game_id"] = args.game_id
    sm_bytes = (json.dumps(sm_doc, indent=2) + "\n").encode()
    parse_statemap(sm_bytes)                      # raises on any defect
    sm_name = f"{args.game_id}.statemap.json"
    (out / sm_name).write_bytes(sm_bytes)
    print(f"state map ok: {len(sm_doc.get('core', {}))} core fields, {len(sm_doc.get('extras', {}))} extras")

    # Start state.
    pinned = version("pyboy")
    st_name = f"{args.game_id}.state"
    if args.start_state:
        state = Path(args.start_state).read_bytes()
        (out / st_name).write_bytes(state)
        print(f"start state: copied {len(state)} bytes from {args.start_state}")
    else:
        from pyboy import PyBoy
        pb = PyBoy(io.BytesIO(rom), window="null", sound_emulated=False, no_input=True,
                   log_level="ERROR", cgb=info.cgb, randomize=False)
        try:
            pb.tick(args.boot_frames, True)
            with open(out / st_name, "wb") as fh:
                pb.save_state(fh)
        finally:
            pb.stop(save=False)
        state = (out / st_name).read_bytes()
        print(f"start state: cold boot + {args.boot_frames} frames ({len(state)} bytes)")

    bundle = {
        "game_id": args.game_id,
        "licence": args.licence,
        "release_frames": args.release_frames,
        "rom": {"file": rom_name, "sha256": info.sha256},
        "start_state": {"file": st_name, "pyboy_version": pinned, "sha256": sha(state)},
        "state_map": {"file": sm_name, "sha256": sha(sm_bytes)},
    }
    (out / "bundle.json").write_text(json.dumps(bundle, indent=2) + "\n")

    from labharness.bundle import load_bundle
    b = load_bundle(out)
    print(f"\n✓ bundle ready: {out}  (game_id={b.game_id}, release_frames={b.release_frames})")
    print("watch a model play it:")
    print("  set -a; . ./.env; set +a")
    print(f"  .venv/bin/python -I scripts/play_local.py --bundle {out} \\")
    print("      --provider nvidia --model nvidia/nemotron-3.5-lightning-30b-a3b --steps 30 --guide \"<game tips>\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
