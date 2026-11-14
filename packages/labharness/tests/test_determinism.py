"""Exit-criteria evidence: identical ROM + start state + scripted actions (game_seed=null)
give identical RAM traces and scores; save-state restore reproduces the uninterrupted run."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from helpers import other_route, route
from labharness import Limits, Press, Session, Wait
from labharness.actions import Action
from labharness.constants import BUTTONS
from labharness.errors import CheckpointError

GOLDEN = Path(__file__).parent / "golden" / "fixture_route.json"
TESTS_DIR = Path(__file__).parent


def run(bundle, actions: list[Action]):  # type: ignore[no-untyped-def]
    with Session(bundle) as s:
        return s.run_script(actions)


# ---- repeated execution ----------------------------------------------------------
def test_repeated_runs_are_identical(bundle) -> None:  # type: ignore[no-untyped-def]
    traces = [run(bundle, route()) for _ in range(3)]  # three separate emulator processes
    first = traces[0]
    for t in traces[1:]:
        assert t.entries == first.entries          # every step: RAM digest, telemetry, frame hash, score
        assert t.sha256 == first.sha256 and t.score == first.score == 5
    assert len({e["ram_sha256"] for e in first.entries}) > 10  # the trace is not trivially constant


def test_concurrent_runs_are_identical(bundle) -> None:  # type: ignore[no-untyped-def]
    """Parallel load must not perturb emulation (no wall-clock or scheduling dependence)."""
    with ThreadPoolExecutor(4) as pool:
        traces = list(pool.map(lambda _: run(bundle, route()), range(4)))
    assert len({t.sha256 for t in traces}) == 1


def test_separate_interpreters_agree_with_each_other_and_with_golden(bundle) -> None:  # type: ignore[no-untyped-def]
    env = {k: v for k, v in os.environ.items() if k != "PYTHONHASHSEED"}
    outs = []
    for hashseed in ("0", "12345"):  # different hash randomisation must not matter
        proc = subprocess.run(
            [sys.executable, "-I", str(TESTS_DIR / "trace_cli.py")], capture_output=True, text=True,
            env={**env, "PYTHONHASHSEED": hashseed, "PYTHONPATH": str(TESTS_DIR)}, timeout=120, check=True,
        )
        outs.append(json.loads(proc.stdout))
    assert outs[0] == outs[1]
    golden = json.loads(GOLDEN.read_text())
    assert outs[0] == golden, "trace changed: see tests/regen_golden.py docstring before regenerating"


def test_in_process_run_matches_golden(bundle) -> None:  # type: ignore[no-untyped-def]
    golden = json.loads(GOLDEN.read_text())
    t = run(bundle, route())
    assert t.sha256 == golden["trace_sha256"] and t.score == golden["score"]
    assert [e["ram_sha256"] for e in t.entries] == golden["ram_sha256_per_step"]


# ---- negative controls: the comparison can fail ---------------------------------
def test_different_script_gives_different_trace(bundle) -> None:  # type: ignore[no-untyped-def]
    a, b = run(bundle, route()), run(bundle, other_route())
    assert a.sha256 != b.sha256 and a.entries[1]["ram_sha256"] != b.entries[1]["ram_sha256"]


def test_one_frame_of_difference_is_detected(bundle) -> None:  # type: ignore[no-untyped-def]
    a = run(bundle, [Press(("Right",), 1), Wait(10)])
    b = run(bundle, [Press(("Right",), 1), Wait(11)])
    assert a.entries[-1]["ram_sha256"] != b.entries[-1]["ram_sha256"]


def test_different_start_state_gives_different_trace(bundle, tmp_path) -> None:  # type: ignore[no-untyped-def]
    import dataclasses
    import hashlib

    from labharness import Emulator
    with Emulator(bundle.rom, bundle.release_frames) as e:
        e.load_state(bundle.start_state)
        e.execute(Wait(1))                       # same game, one frame later
        shifted = e.save_state()
    other = dataclasses.replace(bundle, start_state=shifted, start_state_sha256=hashlib.sha256(shifted).hexdigest())
    a, b = run(bundle, route()), run(other, route())
    assert a.manifest_sha256 != b.manifest_sha256 and a.sha256 != b.sha256
    assert [e["ram_sha256"] for e in a.entries] != [e["ram_sha256"] for e in b.entries]


def test_rng_depends_on_timing_so_equal_traces_are_meaningful(bundle) -> None:  # type: ignore[no-untyped-def]
    """The fixture mixes DIV into its RNG on button edges; shifting the press by one frame
    must change it, otherwise identical traces would prove little."""
    from helpers import read_sym
    rng = read_sym()["wRng"]
    seen = set()
    for pre_wait in (1, 2, 3, 4):
        with Session(bundle) as s:
            s.open()
            s.execute(Wait(pre_wait))
            s.execute(Press(("A",), 1))
            seen.add(s.emulator.read_ram([(rng, 1)]))
    assert len(seen) > 1


# ---- save-state restoration ------------------------------------------------------
def split_run(bundle, k: int):  # type: ignore[no-untyped-def]
    actions = route()
    with Session(bundle) as a:
        a.open()
        for act in actions[:k]:
            a.execute(act)
        cp = a.checkpoint()
        tail = [a.execute(act).trace_entry() for act in actions[k:]]
        final = (a.tracker.to_dict(), a.emulator.ram_digest())
    return cp, tail, final


@pytest.mark.parametrize("k", [1, 5, 8, 14])
def test_restore_in_a_fresh_process_reproduces_the_uninterrupted_tail(bundle, k: int) -> None:  # type: ignore[no-untyped-def]
    cp, tail, final = split_run(bundle, k)
    actions = route()
    with Session(bundle) as b:
        b.open()
        b.restore(cp)
        assert b.step == k and b.emulator.frames == cp.frames
        replay = [b.execute(act).trace_entry() for act in actions[k:]]
        assert replay == tail
        assert (b.tracker.to_dict(), b.emulator.ram_digest()) == final


def test_restore_in_the_same_session_after_diverging(bundle) -> None:  # type: ignore[no-untyped-def]
    cp, tail, _ = split_run(bundle, 6)
    actions = route()
    with Session(bundle) as s:
        s.open()
        for act in actions[:6]:
            s.execute(act)
        cp2 = s.checkpoint()
        assert cp2.state_sha256 == cp.state_sha256 and cp2.ram_sha256 == cp.ram_sha256
        for act in other_route():                  # diverge
            s.execute(act)
        s.restore(cp2)
        assert [s.execute(act).trace_entry() for act in actions[6:]] == tail


def test_restore_preserves_score_history(bundle) -> None:  # type: ignore[no-untyped-def]
    cp, _, _ = split_run(bundle, 9)                # milestones 0,1,2 reached by step 9
    with Session(bundle) as s:
        s.open()
        s.restore(cp)
        assert s.tracker.seen == 0b111 and s.tracker.first_step == {0: 2, 1: 8, 2: 9}
        assert s.tracker.score == 3


def test_checkpoint_is_stable_for_the_same_boundary(bundle) -> None:  # type: ignore[no-untyped-def]
    a, _, _ = split_run(bundle, 7)
    b, _, _ = split_run(bundle, 7)
    assert a.state == b.state and a.state_sha256 == b.state_sha256


def test_restore_rejects_tampered_or_foreign_checkpoints(bundle) -> None:  # type: ignore[no-untyped-def]
    import dataclasses
    cp, _, _ = split_run(bundle, 4)
    bad = {
        "state": dataclasses.replace(cp, state=cp.state[:-1] + bytes([cp.state[-1] ^ 1])),
        "rom": dataclasses.replace(cp, rom_sha256="0" * 64),
        "semantics": dataclasses.replace(cp, semantics_sha256="0" * 64),
        "pyboy": dataclasses.replace(cp, pyboy_version="2.8.0"),
    }
    for broken in bad.values():
        with Session(bundle) as s:
            s.open()
            with pytest.raises(CheckpointError):
                s.restore(broken)
    wrong_ram = dataclasses.replace(cp, ram_sha256="f" * 64)   # state intact but claims other RAM
    with Session(bundle) as s:
        s.open()
        with pytest.raises(CheckpointError, match="does not match"):
            s.restore(wrong_ram)
        assert not s.emulator.alive                              # fail closed: unknown state is discarded
    for over in ({"step": 10**6}, {"frames": 10**9}, {"step": -1}, {"frames": -1}):
        with Session(bundle) as s:
            s.open()
            with pytest.raises(CheckpointError, match="caps"):
                s.restore(dataclasses.replace(cp, **over))
            assert s.emulator.alive and s.step == 0              # rejected before touching anything


def test_checkpoints_are_only_taken_at_step_boundaries(bundle) -> None:  # type: ignore[no-untyped-def]
    """Every action ends with the release consumed (events empty, nothing held), so a
    checkpoint right after any action succeeds; the guard itself is tested in test_emulator."""
    with Session(bundle) as s:
        s.open()
        for act in route():
            s.execute(act)
            assert s.checkpoint().step == s.step


# ---- property-based: arbitrary valid scripts are deterministic ------------------------
actions_strategy = st.lists(
    st.one_of(
        st.builds(Press, st.lists(st.sampled_from(BUTTONS), min_size=1, max_size=8).map(tuple),
                  st.integers(1, 60)),
        st.builds(Wait, st.integers(1, 120)),
    ),
    min_size=1, max_size=6,
)


@settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(actions_strategy)
def test_any_valid_script_is_deterministic(bundle, actions) -> None:  # type: ignore[no-untyped-def]
    a, b = run(bundle, actions), run(bundle, actions)
    assert a.sha256 == b.sha256 and a.score == b.score


# ---- resource exhaustion ---------------------------------------------------------
def rss_kb(pid: int) -> int:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    raise AssertionError("no VmRSS")


@pytest.mark.slow
@pytest.mark.skipif(sys.platform == "win32", reason="RSS is read from /proc/<pid>/status, which is Linux-only")
def test_long_run_does_not_grow_child_memory(bundle) -> None:  # type: ignore[no-untyped-def]
    with Session(bundle, Limits(max_steps=2000)) as s:
        s.open()
        for _ in range(50):
            s.execute(Wait(600))
        warm = rss_kb(s.emulator._proc.pid)
        for i in range(250):
            s.execute(Press(("Right", "Left", "Down", "Up"), 3) if i % 2 else Wait(600))
            s.checkpoint()
        grown = rss_kb(s.emulator._proc.pid) - warm
    assert grown < 20_000, f"child RSS grew by {grown} kB over 250 steps"
