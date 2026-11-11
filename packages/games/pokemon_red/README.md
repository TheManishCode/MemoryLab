# Pokémon Red — local-only bundle (bring your own ROM)

A **local, private** game bundle for benchmarking agents on Pokémon Red. It is **not** a public
game and is never offered to visitors, listed in the `games` table, or built into any image
(per spec decision D1 and `docs/BLOCKED.md` B-7: Pokémon Red is owner's-machine-only until legal
advice says otherwise). The architecture is ROM-agnostic; this directory just wires one ROM.

## What is and isn't here

| File | Committed? | Why |
|---|---|---|
| `pokemon_red.gb` | **no** (gitignored) | Nintendo's copyrighted ROM. Provide your own legally-obtained copy. |
| `pokemon_red.state` | **no** (gitignored) | A PyBoy save state derived from the ROM. Regenerate it (below). |
| `pokemon_red.statemap.json` | yes | RAM addresses only (facts, not copyrightable), from the public pokered map. |
| `bundle.json` | yes | Metadata + sha256 pins. |
| `.gitignore` | yes | Keeps the ROM and state out of git. |

Expected ROM: Pokémon Red (UE), sha256 `5ca7ba01642a3b27b0cc0b5349b52792795b62d3ed977e98a09390659af96b7b`
(sha1 `ea9bcae617fdf159b045185467ae58b2e4a48b9a`). A different revision needs its own `bundle.json` sha.

## Set it up from your own ROM

```bash
# 1. put your ROM here
cp "/path/to/Pokemon Red.gb" packages/games/pokemon_red/pokemon_red.gb
# 2. generate the start state (boots past the intro to the controllable bedroom, deterministic)
.venv/bin/python -I scripts/pokered/gen_start.py --write
# 3. (if your ROM's state sha differs) refresh bundle.json's start_state sha256 to match
```

`gen_start.py` drives the real title → NEW GAME → Oak intro → preset names → bedroom and verifies
the player can step before saving.

## The state map

`core`: `map_id` (`0xD35E`), `x` (`0xD362`), `y` (`0xD361`), `dialog_active` (`0xCFC4` = font loaded),
`milestones` (`0xD356` = the badges byte, so **progress score = gym badges earned**).
`extras`: `in_battle` (`0xD057`) and `party` (count at `0xD163`, six 44-byte structs at `0xD16B`:
species, HP, level, max-HP). Money is BCD, which the reader doesn't support, so it's omitted.

## Watch a model play

```bash
set -a; . ./.env; set +a        # MEMORYLAB_PROVIDER_KEY=nvapi-...
.venv/bin/python -I scripts/play_local.py --bundle packages/games/pokemon_red \
    --provider nvidia --model nvidia/nemotron-3.5-lightning-30b-a3b --steps 30 \
    --guide "You are playing Pokemon Red... to walk a tile press a direction with hold_frames 16..."
```

This is the local single-run path (the model calls the provider directly over TLS). The full
platform path — web UI, sandbox, orchestrator — still needs the API key-validator wired (B-4).
