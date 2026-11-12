import io
import re
import sys
from pathlib import Path

import pytest
from pyboy import PyBoy

from helpers import FIXTURE_DIR, read_sym
from labharness import Emulator, Limits, Press, Wait, _worker
from labharness.calibration import measure_release_frames
from labharness.errors import EmulatorFailure, EmulatorTimeout, FrameLimitExceeded, RomError

SYM = read_sym()


@pytest.fixture()
def emu(bundle):  # type: ignore[no-untyped-def]
    with Emulator(bundle.rom, bundle.release_frames) as e:
        e.load_state(bundle.start_state)
        yield e


def u16(e: Emulator, addr: int) -> int:
    return int.from_bytes(e.read_ram([(addr, 2)]), "little")


# ---- initialisation and shutdown -------------------------------------------------
def test_init_runs_in_a_separate_process_and_reports_pinned_version(bundle) -> None:  # type: ignore[no-untyped-def]
    import os
    e = Emulator(bundle.rom, bundle.release_frames)
    assert not e.alive
    e.start()
    try:
        assert e.alive and e._proc.pid != os.getpid() and e.pyboy_version == "2.9.0"
    finally:
        e.close()
    assert not e.alive


def test_shutdown_is_idempotent_and_use_after_close_fails(bundle) -> None:  # type: ignore[no-untyped-def]
    e = Emulator(bundle.rom, bundle.release_frames)
    e.start()
    e.close()
    e.close()
    with pytest.raises(EmulatorFailure):
        e.execute(Wait(1))
    with pytest.raises(EmulatorFailure):
        e.read_ram([(0xC000, 1)])


def test_double_start_refused(bundle) -> None:  # type: ignore[no-untyped-def]
    with Emulator(bundle.rom, bundle.release_frames) as e, pytest.raises(EmulatorFailure):
        e.start()


def test_context_manager_kills_child_on_exception(bundle) -> None:  # type: ignore[no-untyped-def]
    proc = None
    with pytest.raises(RuntimeError), Emulator(bundle.rom, bundle.release_frames) as e:
        proc = e._proc
        raise RuntimeError("boom")
    assert proc is not None and not proc.is_alive()


def test_rtc_cartridge_refused_at_construction() -> None:
    rom = bytearray((FIXTURE_DIR / "fixture.gb").read_bytes())
    rom[0x147] = 0x10
    chk = 0
    for b in rom[0x134:0x14D]:
        chk = (chk - b - 1) & 0xFF
    rom[0x14D] = chk
    with pytest.raises(RomError, match="RTC"):
        Emulator(bytes(rom), 7)


def test_never_writes_beside_the_rom(bundle) -> None:  # type: ignore[no-untyped-def]
    before = sorted(p.name for p in FIXTURE_DIR.iterdir())
    with Emulator(bundle.rom, bundle.release_frames) as e:
        e.load_state(bundle.start_state)
        e.execute(Press(("A", "Start"), 3))
    assert sorted(p.name for p in FIXTURE_DIR.iterdir()) == before
    assert not list(FIXTURE_DIR.glob("*.ram")) and not list(FIXTURE_DIR.glob("*.rtc"))


# ---- input semantics -----------------------------------------------------------
@pytest.mark.parametrize("hold", [1, 2, 7, 8, 9, 30, 59, 60])
def test_hold_frames_are_observed_by_the_game_exactly(emu: Emulator, hold: int) -> None:
    before = u16(emu, SYM["wInputFrames"])
    emu.execute(Press(("A",), hold))
    assert u16(emu, SYM["wInputFrames"]) - before == hold


def test_sequence_semantics_and_frame_accounting(emu: Emulator) -> None:
    f0 = u16(emu, SYM["wFrames"])
    start = emu.frames
    cost = emu.execute(Press(("Right", "Down", "A"), 3))
    assert cost == 3 * (3 + emu.release_frames) == emu.frames - start
    assert u16(emu, SYM["wFrames"]) - f0 == cost                      # game saw exactly that many frames
    assert u16(emu, SYM["wInputFrames"]) == 9                          # 3 buttons x 3 held frames
    x, y = emu.read_ram([(SYM["wX"], 1)])[0], emu.read_ram([(SYM["wY"], 1)])[0]
    assert (x, y) == (1, 1)                                            # each button acted in order, once


def test_press_order_matters(bundle) -> None:  # type: ignore[no-untyped-def]
    """Buttons are a sequence, not a chord: Start then B ends closed, B then Start ends open."""
    dialog = SYM["wDialog"]
    with Emulator(bundle.rom, bundle.release_frames) as e:
        e.load_state(bundle.start_state)
        e.execute(Press(("Start", "B"), 1))
        assert e.read_ram([(dialog, 1)]) == b"\x00"
        e.load_state(bundle.start_state)
        e.execute(Press(("B", "Start"), 1))
        assert e.read_ram([(dialog, 1)]) == b"\x01"


def test_release_frames_match_measurement(bundle) -> None:  # type: ignore[no-untyped-def]
    """bundle.json's release_frames must be what calibration measures on this game."""
    cal = measure_release_frames(bundle)
    r = bundle.release_frames
    assert cal.release_frames == r == 7
    assert not cal.passes[r - 1] and all(cal.passes[x] for x in range(r, 21))


def test_frames_do_not_advance_on_read_only_calls(emu: Emulator) -> None:
    f = u16(emu, SYM["wFrames"])
    emu.read_ram([(0xC000, 8)]); emu.ram_digest(); emu.frame_rgb(); emu.save_state()
    assert u16(emu, SYM["wFrames"]) == f and emu.frames == 0


# ---- limits and failures ---------------------------------------------------------
def test_frame_limit_stops_exactly_at_the_cap(bundle) -> None:  # type: ignore[no-untyped-def]
    with Emulator(bundle.rom, bundle.release_frames, Limits(max_total_frames=100)) as e:
        e.load_state(bundle.start_state)
        for _ in range(4):
            e.execute(Wait(25))
        assert e.frames == 100
        digest = e.ram_digest()
        with pytest.raises(FrameLimitExceeded):
            e.execute(Wait(1))
        assert e.frames == 100 and e.ram_digest() == digest and e.alive  # nothing ran; still usable


def test_frame_limit_checked_before_a_straddling_action(bundle) -> None:  # type: ignore[no-untyped-def]
    with Emulator(bundle.rom, bundle.release_frames, Limits(max_total_frames=30)) as e:
        e.load_state(bundle.start_state)
        e.execute(Wait(20))
        with pytest.raises(FrameLimitExceeded):
            e.execute(Wait(11))
        e.execute(Wait(10))
        assert e.frames == 30


def test_per_call_timeout_kills_the_child(bundle) -> None:  # type: ignore[no-untyped-def]
    with Emulator(bundle.rom, bundle.release_frames, Limits(call_timeout_s=0.001)) as e:
        with pytest.raises(EmulatorTimeout):
            e.execute(Wait(600))
        assert not e.alive
        assert e._proc is not None
        e._proc.join(5)
        assert not e._proc.is_alive()
        with pytest.raises(EmulatorFailure):
            e.read_ram([(0xC000, 1)])


def test_child_crash_is_detected_not_hung(emu: Emulator) -> None:
    assert emu._proc is not None
    emu._proc.kill()
    emu._proc.join(5)
    with pytest.raises(EmulatorFailure):
        emu.execute(Wait(1))
    assert not emu.alive


@pytest.mark.skipif(sys.platform == "win32", reason="RLIMIT_AS is POSIX-only; on Windows the OS/container is the memory bound")
def test_memory_limit_is_enforced_in_the_child(bundle) -> None:  # type: ignore[no-untyped-def]
    """Drive the worker entry point below the Limits floor: the rlimit must bite in the child."""
    import multiprocessing as mp

    from labharness import _wire

    ctx = mp.get_context("spawn")
    parent, child = ctx.Pipe(duplex=True)
    proc = ctx.Process(target=_worker.main, args=(child, bundle.rom, False, 512 * 1024**2, 3600), daemon=True)
    proc.start()
    child.close()
    try:
        assert parent.poll(30)
        header, _ = _wire.decode(parent.recv_bytes())
        assert header["ok"] is False and header["error"] in ("ImportError", "MemoryError")
    finally:
        proc.join(10)
        if proc.is_alive():
            proc.kill()
    with Emulator(bundle.rom, bundle.release_frames, Limits(memory_limit_bytes=768 * 1024**2)) as e:  # the documented floor works
        e.load_state(bundle.start_state)
        e.execute(Wait(600))


def test_limits_validation() -> None:
    for kwargs in ({"call_timeout_s": 0}, {"startup_timeout_s": -1}, {"max_total_frames": 0},
                   {"max_steps": 0}, {"memory_limit_bytes": 64 * 1024**2}, {"cpu_limit_s": 0}):
        with pytest.raises(ValueError):
            Limits(**kwargs)


def test_hostile_read_requests_are_refused_by_the_child(emu: Emulator) -> None:
    for ranges in ([(0x0000, 16)], [(0x8000, 4)], [(0xDFFF, 2)], [(0xC000, 0x5000)]):
        e2 = Emulator(emu._rom, emu.release_frames)
        e2.start()
        try:
            with pytest.raises(EmulatorFailure):
                e2.read_ram(ranges)
        finally:
            e2.close()


def test_bad_save_state_payloads_fail_cleanly(bundle) -> None:  # type: ignore[no-untyped-def]
    with Emulator(bundle.rom, bundle.release_frames) as e:
        with pytest.raises(EmulatorFailure):
            e.load_state(b"")
        with pytest.raises(EmulatorFailure):
            e.load_state(b"\x00" * (17 * 1024 * 1024))
        e.load_state(bundle.start_state)  # sizes rejected before reaching the child; still usable
    with Emulator(bundle.rom, bundle.release_frames) as e:
        with pytest.raises(EmulatorFailure):
            e.load_state(b"not a save state" * 10)
        assert not e.alive  # corrupted state: emulator discarded, never reused


# ---- step-boundary guard (PyBoy's input heap is private, so we test what is observable) ----
def make_pyboy(bundle) -> PyBoy:  # type: ignore[no-untyped-def]
    return PyBoy(io.BytesIO(bundle.rom), window="null", sound_emulated=False, no_input=True, log_level="ERROR")


def test_guard_rejects_pending_events_and_held_buttons(bundle) -> None:  # type: ignore[no-untyped-def]
    pb = make_pyboy(bundle)
    try:
        _worker.assert_input_idle(pb, set())
        pb.button_press("a")                      # event queued, not yet consumed by a tick
        with pytest.raises(_worker.WorkerError, match="pending"):
            _worker.assert_input_idle(pb, set())
        pb.tick(1, False)
        _worker.assert_input_idle(pb, set())      # consumed
        with pytest.raises(_worker.WorkerError, match="held"):
            _worker.assert_input_idle(pb, {"a"})
        pb.button_release("a")
        with pytest.raises(_worker.WorkerError, match="pending"):
            _worker.assert_input_idle(pb, set())  # release not yet consumed either
        pb.tick(1, False)
        _worker.assert_input_idle(pb, set())
    finally:
        pb.stop(save=False)


def test_core_refuses_to_checkpoint_mid_input(bundle) -> None:  # type: ignore[no-untyped-def]
    pb = make_pyboy(bundle)
    try:
        core = _worker.Core(pb)
        pb.tick(80, False)
        core.save()
        pb.button_press("start")
        with pytest.raises(_worker.WorkerError):
            core.save()
        pb.tick(1, False); pb.button_release("start")
        with pytest.raises(_worker.WorkerError):
            core.save()
        pb.tick(1, False)
        core.save()
    finally:
        pb.stop(save=False)


def test_worker_never_uses_delayed_input_apis() -> None:
    """The input heap can only be filled by delayed input; keep it provably empty."""
    src = Path(_worker.__file__).read_text()
    code = re.sub(r"#.*", "", src)
    assert "send_input" not in code and ".button(" not in code and "queued_input" not in code
    assert "button_press" in code and "button_release" in code


def test_release_must_be_positive(bundle) -> None:  # type: ignore[no-untyped-def]
    with Emulator(bundle.rom, bundle.release_frames) as e:
        e.load_state(bundle.start_state)
        with pytest.raises(EmulatorFailure, match="release"):
            e._call({"cmd": "press", "buttons": ["A"], "hold": 1, "release": 0})


# ---- child-side defence in depth and CPU limit -----------------------------------
@pytest.mark.parametrize(
    "header",
    [
        {"cmd": "press", "buttons": ["A"], "hold": 61, "release": 7},
        {"cmd": "press", "buttons": ["A"], "hold": 0, "release": 7},
        {"cmd": "press", "buttons": ["A"] * 9, "hold": 1, "release": 7},
        {"cmd": "press", "buttons": [], "hold": 1, "release": 7},
        {"cmd": "press", "buttons": ["Turbo"], "hold": 1, "release": 7},
        {"cmd": "press", "buttons": ["A"] * 8, "hold": 60, "release": 60},
        {"cmd": "press", "buttons": ["A"], "hold": True, "release": 7},
        {"cmd": "press", "buttons": ["A"], "hold": 10**9, "release": 7},
        {"cmd": "wait", "frames": 601}, {"cmd": "wait", "frames": 0}, {"cmd": "wait", "frames": "5"},
        {"cmd": "read", "ranges": "x"}, {"cmd": "read", "ranges": [[-1, 4]]}, {"cmd": "read", "ranges": [[0xC000]]},
        {"cmd": "frobnicate"},
    ],
)
def test_child_rejects_out_of_bounds_commands_even_from_the_parent(bundle, header: dict) -> None:  # type: ignore[no-untyped-def,type-arg]
    with Emulator(bundle.rom, bundle.release_frames) as e:
        e.load_state(bundle.start_state)
        with pytest.raises(EmulatorFailure):
            e._call(header)
        assert not e.alive and e.frames == 0


@pytest.mark.skipif(sys.platform == "win32", reason="RLIMIT_CPU is POSIX-only; on Windows the child is not CPU-capped so this loop would never be killed")
def test_cpu_limit_kills_the_child_without_hanging(bundle) -> None:  # type: ignore[no-untyped-def]
    import time
    t0 = time.monotonic()
    with Emulator(bundle.rom, bundle.release_frames, Limits(cpu_limit_s=3, max_total_frames=10**9, call_timeout_s=60)) as e:
        e.load_state(bundle.start_state)
        with pytest.raises(EmulatorFailure):
            for _ in range(100_000):
                e.execute(Wait(600))
        assert not e.alive
    assert time.monotonic() - t0 < 120
