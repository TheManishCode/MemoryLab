"""Parent-side emulator client.

PyBoy runs in a spawned child process so that a hung or crashing emulator can be killed
on a per-call timeout and bounded with rlimits. The ROM is passed as bytes (never a
path), so PyBoy has nothing to write beside it, and the child always stops with
save=False.
"""

import multiprocessing as mp
from collections.abc import Sequence
from dataclasses import dataclass
from multiprocessing.connection import Connection
from typing import Any, Self

import numpy as np

from . import _wire
from ._worker import main as _worker_main
from .actions import Action, Press, Wait, checked_cost
from .constants import PINNED_PYBOY, RELEASE_FRAMES_MAX, RELEASE_FRAMES_MIN
from .errors import EmulatorFailure, EmulatorTimeout, FrameLimitExceeded, HarnessError
from .rom import RomInfo, inspect_rom

SCREEN_H, SCREEN_W = 144, 160
MAX_STATE_BYTES = 16 * 1024 * 1024
# RLIMIT_AS counts virtual address space. Measured on one workstation: 512 MiB cannot import PyBoy (a hosted runner can),
# 768 MiB starts and runs. The container limit (Milestone 3) is the real memory bound.
MIN_MEMORY_LIMIT_BYTES = 768 * 1024**2


@dataclass(frozen=True)
class Limits:
    call_timeout_s: float = 30.0
    startup_timeout_s: float = 60.0
    max_total_frames: int = 1_200_000  # 2000 steps x 600 frames, the architecture's step cap
    max_steps: int = 2000
    memory_limit_bytes: int = 2 * 1024**3
    cpu_limit_s: int = 3600

    def __post_init__(self) -> None:
        if not (self.call_timeout_s > 0 and self.startup_timeout_s > 0):
            raise ValueError("timeouts must be positive")
        if self.max_total_frames < 1 or self.max_steps < 1:
            raise ValueError("frame and step caps must be positive")
        if self.memory_limit_bytes < MIN_MEMORY_LIMIT_BYTES or self.cpu_limit_s < 1:
            raise ValueError("resource limits too small")


class Emulator:
    """One emulator child. Not thread-safe; one caller at a time."""

    def __init__(self, rom: bytes, release_frames: int, limits: Limits | None = None) -> None:
        if isinstance(release_frames, bool) or not RELEASE_FRAMES_MIN <= release_frames <= RELEASE_FRAMES_MAX:
            raise ValueError(f"release_frames must be in {RELEASE_FRAMES_MIN}..{RELEASE_FRAMES_MAX}")
        self.release_frames = release_frames
        self.rom_info: RomInfo = inspect_rom(rom)  # refuses RTC carts and bad headers
        self._rom = rom
        self.limits = limits or Limits()
        self.frames = 0  # frames advanced since the logical start (restore resets it)
        self._proc: Any = None
        self._conn: Connection | None = None
        self._failed = False
        self.pyboy_version: str | None = None

    # -- lifecycle ---------------------------------------------------------------
    def start(self) -> None:
        if self._proc is not None:
            raise EmulatorFailure("emulator already started")
        ctx = mp.get_context("spawn")
        parent, child = ctx.Pipe(duplex=True)
        proc = ctx.Process(
            target=_worker_main,
            args=(child, self._rom, self.rom_info.cgb, self.limits.memory_limit_bytes, self.limits.cpu_limit_s),
            daemon=True,
        )
        proc.start()
        child.close()
        self._proc, self._conn = proc, parent
        header, _ = self._recv(self.limits.startup_timeout_s)
        if header.get("pyboy_version") != PINNED_PYBOY:
            self._kill()
            raise EmulatorFailure("pyboy version mismatch")
        self.pyboy_version = header["pyboy_version"]

    def close(self) -> None:
        """Idempotent shutdown."""
        conn, proc = self._conn, self._proc
        self._conn = None
        if conn is not None and proc is not None and proc.is_alive() and not self._failed:
            try:
                conn.send_bytes(_wire.encode({"cmd": "stop"}))
                proc.join(5)
            except (OSError, BrokenPipeError):
                pass
        if proc is not None and proc.is_alive():
            proc.kill()
        if proc is not None:
            proc.join(5)
        if conn is not None:
            conn.close()
        self._failed = True

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def alive(self) -> bool:
        return self._conn is not None and not self._failed and self._proc is not None and self._proc.is_alive()

    # -- transport ----------------------------------------------------------------
    def _kill(self) -> None:
        self._failed = True
        if self._proc is not None and self._proc.is_alive():
            self._proc.kill()

    def _recv(self, timeout: float) -> tuple[dict[str, Any], bytes]:
        conn = self._conn
        if conn is None:
            raise EmulatorFailure("emulator is not running")
        try:
            if not conn.poll(timeout):
                self._kill()
                raise EmulatorTimeout(f"emulator call exceeded {timeout}s; child killed")
            header, payload = _wire.decode(conn.recv_bytes())
        except (EOFError, OSError, ValueError) as exc:
            self._kill()
            raise EmulatorFailure(f"emulator child died or sent a bad reply: {type(exc).__name__}") from exc
        if not header.get("ok"):
            self._kill()
            raise EmulatorFailure(f"emulator error {header.get('error')}: {header.get('message')}")
        return header, payload

    def _call(self, header: dict[str, Any], payload: bytes = b"") -> tuple[dict[str, Any], bytes]:
        conn = self._conn
        if conn is None or self._failed or self._proc is None:
            raise EmulatorFailure("emulator is not running")
        try:
            conn.send_bytes(_wire.encode(header, payload))
        except (OSError, ValueError) as exc:
            self._kill()
            raise EmulatorFailure(f"could not reach emulator child: {type(exc).__name__}") from exc
        return self._recv(self.limits.call_timeout_s)

    # -- operations -----------------------------------------------------------------
    def execute(self, action: Action) -> int:
        """Run one validated action; returns frames advanced. The frame cap is checked
        before anything runs, so a run stops exactly at the limit."""
        cost = checked_cost(action, self.release_frames)
        if self.frames + cost > self.limits.max_total_frames:
            raise FrameLimitExceeded(f"{self.frames}+{cost} frames exceeds {self.limits.max_total_frames}")
        if isinstance(action, Press):
            header, _ = self._call(
                {"cmd": "press", "buttons": list(action.buttons), "hold": action.hold_frames, "release": self.release_frames}
            )
        elif isinstance(action, Wait):
            header, _ = self._call({"cmd": "wait", "frames": action.frames_to_wait})
        else:
            raise HarnessError("not an action")
        if header.get("frames") != cost:
            self._kill()
            raise EmulatorFailure("emulator advanced an unexpected number of frames")
        self.frames += cost
        return cost

    def read_ram(self, ranges: Sequence[tuple[int, int]]) -> bytes:
        _, data = self._call({"cmd": "read", "ranges": [list(r) for r in ranges]})
        if len(data) != sum(length for _s, length in ranges):
            self._kill()
            raise EmulatorFailure("short RAM read")
        return data

    def ram_digest(self) -> str:
        header, _ = self._call(
            {"cmd": "digest", "ram_banks": self.rom_info.ram_banks, "ram_bank_size": self.rom_info.ram_bank_size}
        )
        digest = header.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            self._kill()
            raise EmulatorFailure("bad digest reply")
        return digest

    def frame_rgb(self) -> np.ndarray[Any, Any]:
        _, data = self._call({"cmd": "frame"})
        if len(data) != SCREEN_H * SCREEN_W * 3:
            self._kill()
            raise EmulatorFailure("bad frame size")
        return np.frombuffer(data, dtype=np.uint8).reshape(SCREEN_H, SCREEN_W, 3)

    def save_state(self) -> bytes:
        """Serialise emulator state. Only valid at a step boundary (enforced in the child)."""
        _, data = self._call({"cmd": "save"})
        return data

    def load_state(self, state: bytes, frames: int = 0) -> None:
        if not 0 < len(state) <= MAX_STATE_BYTES:
            raise EmulatorFailure("save state size out of range")
        self._call({"cmd": "load"}, state)
        self.frames = frames

