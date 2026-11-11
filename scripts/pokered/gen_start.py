"""Generate the Pokemon Red start state with PyBoy directly (same ctor args as the harness child,
so the save loads there). The interactive title is at ~frame 1480 (earlier is the intro animation,
where Start only skips, never opens the menu). Sequence:
  title -> Start (main menu) -> A (NEW GAME) -> mash A through Oak -> player name preset (Down,A=RED)
  -> mash A -> rival name preset (Down,A=BLUE) -> mash A -> bedroom.

    .venv/bin/python -I scripts/pokered/gen_start.py           (dry: prints + screenshots every few steps)
    .venv/bin/python -I scripts/pokered/gen_start.py --write   (writes the state too)
"""

import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
B = ROOT / "packages/games/pokemon_red"
MAP, X, Y, PARTY = 0xD35E, 0xD362, 0xD361, 0xD163
BEDROOM = 0x26


def main() -> int:
    write = "--write" in sys.argv
    import numpy as np
    from labharness.screenshots import encode_png
    from pyboy import PyBoy

    rom = (B / "pokemon_red.gb").read_bytes()
    pb = PyBoy(io.BytesIO(rom), window="null", sound_emulated=False, no_input=True,
               log_level="ERROR", cgb=False, randomize=False)

    def step(n: int) -> None:
        pb.tick(n, True)

    def tap(btn: str, after: int = 24) -> None:
        pb.button(btn, 3)
        step(after)

    def rd(a: int) -> int:
        return pb.memory[a]

    def obs() -> dict:
        return {"map": hex(rd(MAP)), "x": rd(X), "y": rd(Y), "party": rd(PARTY)}

    def shot(t: str) -> None:
        arr = np.asarray(pb.screen.ndarray)[:, :, :3].copy()
        (ROOT / f"scripts/pokered/gs_{t}.png").write_bytes(encode_png(arr))

    try:
        MAXMENU = 0xCC28     # wMaxMenuItem: the player/rival name menus have 4 items (==3)
        step(1480)                       # interactive title
        shot("00_title")
        tap("start", 60); shot("01_menu")          # main menu (NEW GAME highlighted)
        # The A that selects NEW GAME can be eaten by the title->menu fade, so press it twice.
        tap("a", 70); tap("a", 70); shot("02_newgame")
        print("after NEW GAME:", obs())

        # Advance Oak's speech with a cadence slow enough for each textbox to finish, and pick the
        # first preset at each 4-item name menu (Down, A = RED then BLUE). Stop at the bedroom,
        # which is only real once the speech ends and the player can step.
        names = 0
        reached = False
        for i in range(140):
            if rd(MAP) == BEDROOM and names >= 2 and i > 4:
                # confirm it is real control, not the speech's map value, by probing a step
                bx, by = rd(X), rd(Y)
                pb.button("down", 6); step(24)
                if (rd(X), rd(Y)) != (bx, by):
                    reached = True
                    break
            if rd(MAXMENU) == 3 and names < 2:
                tap("down", 16); tap("a", 30)
                names += 1
                print(f"  picked preset name #{names} at i={i}: {obs()}")
                continue
            tap("a", 40)
            if i % 15 == 0:
                print(f"  i={i} names={names} maxmenu={rd(MAXMENU)} {obs()}")
                shot(f"i{i:03d}")
        print("reached bedroom:", reached, "names:", names, obs())
        for _ in range(4):
            step(40)
        shot("07_landing")

        before = (rd(MAP), rd(X), rd(Y))
        for btn in ("down", "down", "up", "left", "right"):
            pb.button(btn, 6); step(24)
        after = (rd(MAP), rd(X), rd(Y))
        print("walk test:", before, after, "MOVED" if before != after else "NO MOVE")
        shot("08_walked")

        if write and rd(MAP) == BEDROOM and before != after:
            with open(B / "pokemon_red.state", "wb") as fh:
                pb.save_state(fh)
            print("WROTE start state at", (rd(MAP), rd(X), rd(Y)), "party", rd(PARTY))
        elif write:
            print("refusing to write: not a controllable bedroom (map", hex(rd(MAP)), "moved", before != after, ")")
    finally:
        pb.stop(save=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
