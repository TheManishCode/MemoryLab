"""Emulator child process. Runs PyBoy headless; the parent talks to it over a Pipe.

Any exception inside a command is reported to the parent and ends the process: after a
failure the emulator state is unknown, so it is never reused.
"""

import hashlib
import io
import sys
import warnings
from multiprocessing.connection import Connection
from typing import Any

from . import _wire
from .constants import (
    ACTION_FRAMES_MAX,
    HOLD_FRAMES_MAX,
    HOLD_FRAMES_MIN,
    HRAM,
    MAX_BUTTONS_PER_ACTION,
    PINNED_PYBOY,
    WAIT_FRAMES_MAX,
    WRAM,
)

PYBOY_BUTTONS = {
    "A": "a", "B": "b", "Up": "up", "Down": "down",
    "Left": "left", "Right": "right", "Start": "start", "Select": "select",
}
MAX_READ_BYTES = 0x4000


class WorkerError(Exception):
    """Invariant violation inside the child."""


def apply_limits(memory_limit_bytes: int, cpu_limit_s: int) -> None:
    # POSIX rlimits are defense-in-depth; the container (and on Windows the OS/container)
    # is the real memory bound (see emulator.MIN_MEMORY_LIMIT_BYTES). The `resource` module
    # does not exist on Windows, so there this is a no-op and the bound is enforced elsewhere.
    if sys.platform == "win32":
        return
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (memory_limit_bytes, memory_limit_bytes))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit_s, cpu_limit_s))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def assert_input_idle(pyboy: Any, held: set[str]) -> None:
    """Checkpoint/restore precondition. PyBoy's delayed-input heap is a private Cython
    attribute and cannot be read, so emptiness is guaranteed by construction (this module
    only uses button_press/button_release, never delayed input) and verified here through
    what is observable: the pending event list and our own held-button set."""
    if pyboy.events:
        raise WorkerError("pending input events at a step boundary")
    if held:
        raise WorkerError(f"buttons still held at a step boundary: {sorted(held)}")


class Core:
    def __init__(self, pyboy: Any) -> None:
        self.pyboy = pyboy
        self.held: set[str] = set()

    def press(self, buttons: list[str], hold: int, release: int) -> int:
        # The parent validates too; the child re-checks so it cannot be driven out of bounds.
        if not (isinstance(buttons, list) and 1 <= len(buttons) <= MAX_BUTTONS_PER_ACTION
                and all(b in PYBOY_BUTTONS for b in buttons)):
            raise WorkerError("invalid button list")
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (hold, release)):
            raise WorkerError("hold/release must be integers")
        if not HOLD_FRAMES_MIN <= hold <= HOLD_FRAMES_MAX:
            raise WorkerError("hold out of range")
        if release < 1:  # the release event is only consumed by a following tick
            raise WorkerError("release frames must be >= 1")
        if len(buttons) * (hold + release) > ACTION_FRAMES_MAX:
            raise WorkerError("action exceeds the per-action frame cap")
        frames = 0
        for index, name in enumerate(buttons):
            key = PYBOY_BUTTONS[name]
            last = index == len(buttons) - 1
            self.pyboy.button_press(key)
            self.held.add(key)
            try:
                if not self.pyboy.tick(hold, False):
                    raise WorkerError("emulation ended during hold")
            finally:
                self.pyboy.button_release(key)
                self.held.discard(key)
            if not self.pyboy.tick(release, last):
                raise WorkerError("emulation ended during release")
            frames += hold + release
        assert_input_idle(self.pyboy, self.held)
        return frames

    def wait(self, frames: int) -> int:
        if isinstance(frames, bool) or not isinstance(frames, int) or not 1 <= frames <= WAIT_FRAMES_MAX:
            raise WorkerError("wait frames out of range")
        if not self.pyboy.tick(frames, True):
            raise WorkerError("emulation ended during wait")
        assert_input_idle(self.pyboy, self.held)
        return frames

    def read(self, ranges: list[list[int]]) -> bytes:
        if not (isinstance(ranges, list) and all(
                isinstance(r, list) and len(r) == 2 and all(isinstance(v, int) and not isinstance(v, bool) and v >= 0
                                                            for v in r) for r in ranges)):
            raise WorkerError("malformed read ranges")
        total = sum(length for _s, length in ranges)
        if total > MAX_READ_BYTES:
            raise WorkerError("read request too large")
        out = bytearray()
        for start, length in ranges:
            if not any(lo <= start and start + length <= hi for lo, hi in (WRAM, HRAM)):
                raise WorkerError("read outside WRAM/HRAM")
            out += bytes(self.pyboy.memory[start : start + length])
        return bytes(out)

    def digest(self, ram_banks: int, ram_bank_size: int) -> str:
        """sha256 over WRAM, HRAM and every cartridge RAM bank (read directly, so it does not
        depend on whether the game currently has RAM enabled)."""
        if not (all(isinstance(v, int) and not isinstance(v, bool) for v in (ram_banks, ram_bank_size))
                and 0 <= ram_banks <= 16 and 0 <= ram_bank_size <= 0x2000):
            raise WorkerError("bad cartridge RAM layout")
        h = hashlib.sha256()
        for lo, hi in (WRAM, HRAM):
            h.update(bytes(self.pyboy.memory[lo:hi]))
        for bank in range(ram_banks):
            if ram_bank_size:
                # PyBoy 2.9.0 rejects a bank slice that ends exactly at the bank size
                # (`stop < 0x2000` check), so the last byte is read on its own.
                last = 0xA000 + ram_bank_size - 1
                h.update(bytes(self.pyboy.memory[bank, 0xA000:last]))
                h.update(bytes([self.pyboy.memory[bank, last]]))
        return h.hexdigest()

    def frame(self) -> bytes:
        screen = self.pyboy.screen
        if screen.raw_buffer_format != "RGBA":
            raise WorkerError(f"unexpected screen format {screen.raw_buffer_format}")
        return bytes(screen.ndarray[:, :, :3].tobytes())

    def save(self) -> bytes:
        assert_input_idle(self.pyboy, self.held)
        buf = io.BytesIO()
        self.pyboy.save_state(buf)
        return buf.getvalue()

    def load(self, data: bytes) -> None:
        self.pyboy.load_state(io.BytesIO(data))
        assert_input_idle(self.pyboy, self.held)


def _dispatch(core: Core, header: dict[str, Any], payload: bytes) -> tuple[dict[str, Any], bytes]:
    cmd = header["cmd"]
    if cmd == "press":
        return {"frames": core.press(header["buttons"], header["hold"], header["release"])}, b""
    if cmd == "wait":
        return {"frames": core.wait(header["frames"])}, b""
    if cmd == "read":
        return {}, core.read(header["ranges"])
    if cmd == "digest":
        return {"sha256": core.digest(header["ram_banks"], header["ram_bank_size"])}, b""
    if cmd == "frame":
        return {}, core.frame()
    if cmd == "save":
        return {}, core.save()
    if cmd == "load":
        core.load(payload)
        return {}, b""
    if cmd == "ping":
        return {}, b""
    raise WorkerError(f"unknown command {cmd!r}")


def main(conn: Connection, rom: bytes, cgb: bool, memory_limit_bytes: int, cpu_limit_s: int) -> None:
    try:
        apply_limits(memory_limit_bytes, cpu_limit_s)
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Using SDL2 binaries")
            from importlib.metadata import version

            from pyboy import PyBoy

        if version("pyboy") != PINNED_PYBOY:
            raise WorkerError(f"pyboy {version('pyboy')} != pinned {PINNED_PYBOY}")
        pyboy = PyBoy(
            io.BytesIO(rom),  # in memory: PyBoy cannot create .ram/.rtc files beside the ROM
            window="null",
            sound_emulated=False,
            no_input=True,
            log_level="ERROR",
            cgb=cgb,
            randomize=False,
        )
        core = Core(pyboy)
        conn.send_bytes(_wire.encode({"ok": True, "pyboy_version": version("pyboy")}))
    except BaseException as exc:  # noqa: BLE001 - reported to the parent, then the process ends
        conn.send_bytes(_wire.encode({"ok": False, "error": type(exc).__name__, "message": str(exc)[:300]}))
        return

    while True:
        try:
            header, payload = _wire.decode(conn.recv_bytes())
        except EOFError:
            break
        if header.get("cmd") == "stop":
            break
        try:
            reply, data = _dispatch(core, header, payload)
            conn.send_bytes(_wire.encode({"ok": True, **reply}, data))
        except BaseException as exc:  # noqa: BLE001 - reported to the parent; state unknown, so exit
            conn.send_bytes(_wire.encode({"ok": False, "error": type(exc).__name__, "message": str(exc)[:300]}))
            break
    pyboy.stop(save=False)
