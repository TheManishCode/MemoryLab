"""Property and stateful tests for the memory service (hypothesis)."""

import ast
import builtins
import io
import os
import posixpath
import threading
from itertools import pairwise
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule

from labharness import memory, memory_paths
from labharness.memory import MemoryConfig, MemoryService, MemoryStore
from labharness.memory_paths import Reject, check_content, check_path

ALLOWED = set("abcdefghijklmnopqrstuvwxyz0123456789_-/.")
TOKENS = ["..", ".", "/", "//", "\\", "%2e%2e", "%2f", "\x00", "a", "b1", "é", "\n", " ", "~", "C:", "\u202e", "\ufeff",
          "x" * 40, "/etc/passwd", "../", "..\\", "a/", "/a", "\u2025", "\uff0e\uff0e", "CON", "a.b", "-", "_", "0"]
hostile_paths = st.one_of(
    st.text(max_size=200),
    st.lists(st.sampled_from(TOKENS), min_size=1, max_size=12).map("".join),
    st.text(alphabet=st.sampled_from(sorted(ALLOWED)), min_size=0, max_size=130),
)


def reference_is_valid(path: str) -> bool:
    """Independent re-implementation of the path rules, written differently on purpose."""
    if not 1 <= len(path) <= 120 or any(c not in ALLOWED for c in path):
        return False
    parts = path.split("/")
    return len(parts) <= 4 and all(p not in ("", ".", "..") for p in parts)


@settings(max_examples=2000, deadline=None)
@given(hostile_paths)
def test_check_path_matches_reference_and_accepted_paths_are_safe(path: str) -> None:
    try:
        accepted = check_path(path)
    except Reject as rej:
        assert not reference_is_valid(path) and rej.reason == "invalid_path"
        return
    assert accepted == path and reference_is_valid(path)
    # Safe as a plain key and, even if misused as a path, cannot escape or be absolute:
    assert not posixpath.isabs(path) and posixpath.normpath(path) == path
    assert ".." not in path.split("/") and "\\" not in path and "\x00" not in path and "%" not in path
    # posixpath (not os.path): memory keys are always POSIX-style, and os.path is ntpath on Windows.
    # On Linux os.path IS posixpath, so this is identical there.
    assert posixpath.normpath(posixpath.join("/jail", path)).startswith("/jail/")


@settings(max_examples=1500, deadline=None)
@given(st.text(max_size=60), st.integers(min_value=1, max_value=40))
def test_check_content_is_exact_about_cap_and_characters(content: str, cap: int) -> None:
    bad_char = any((ord(c) < 0x20 and c not in "\t\n\r") or c == "\x7f" for c in content)
    surrogate = any(0xD800 <= ord(c) <= 0xDFFF for c in content)
    try:
        assert check_content(content, cap) == content
        assert not bad_char and not surrogate and len(content) <= cap
    except Reject as rej:
        assert rej.reason in {"invalid_content", "file_cap"}
        assert bad_char or surrogate or len(content) > cap


@settings(max_examples=300, deadline=None)
@given(hostile_paths, st.text(max_size=30))
def test_hostile_writes_never_corrupt_the_store(path: str, content: str) -> None:
    s = MemoryService(MemoryStore("r"), MemoryConfig())
    out = s.dispatch("mem_write", {"path": path, "content": content}, 1)
    keys = list(s.store.live())
    if out.ok:
        assert keys == [path] and reference_is_valid(path)
        assert out.events[0].type == "mem.write"
    else:
        assert keys == [] and out.events[0].type == "mem.reject" and s.store.history_rows == 0


# ---- stateful: compare with a straightforward reference model -----------------------------
POOL = ["a", "b", "c", "d/e", "d/f", "d", "g/h/i", "g", "x.md"]
HOSTILE_POOL = ["../a", "/a", "a//b", "A", "a b", "a" * 130, "p/q/r/s/t"]
CFG = MemoryConfig(max_files=4, max_file_chars=12, max_total_chars=30, max_history_rows=60, max_history_chars=400)


class MemoryMachine(RuleBasedStateMachine):
    @initialize()
    def setup(self) -> None:
        self.svc = MemoryService(MemoryStore("m"), CFG)
        self.model: dict[str, str] = {}
        self.step = 0
        self.rows = 0
        self.chars = 0
        self.snaps: list[tuple[int, dict[str, str]]] = []

    def model_conflict(self, path: str) -> bool:
        return any(o.startswith(path + "/") or path.startswith(o + "/") for o in self.model)

    @rule(path=st.sampled_from(POOL + HOSTILE_POOL), content=st.text(alphabet="ab \n", max_size=16))
    def write(self, path: str, content: str) -> None:
        out = self.svc.dispatch("mem_write", {"path": path, "content": content}, self.step)
        total_after = sum(map(len, self.model.values())) - len(self.model.get(path, "")) + len(content)
        expect_ok = (
            reference_is_valid(path) and len(content) <= CFG.max_file_chars and not self.model_conflict(path)
            and (path in self.model or len(self.model) < CFG.max_files) and total_after <= CFG.max_total_chars
            and self.rows + 1 <= CFG.max_history_rows and self.chars + len(content) <= CFG.max_history_chars
        )
        assert out.ok == expect_ok, (path, content, out.result)
        if out.ok:
            self.model[path] = content
            self.rows += 1
            self.chars += len(content)
            assert out.events[0].type == "mem.write"
        else:
            assert out.events[0].type == "mem.reject"

    @rule(path=st.sampled_from(POOL + HOSTILE_POOL))
    def delete(self, path: str) -> None:
        out = self.svc.dispatch("mem_delete", {"path": path}, self.step)
        expect_ok = reference_is_valid(path) and path in self.model and self.rows + 1 <= CFG.max_history_rows
        assert out.ok == expect_ok
        if out.ok:
            del self.model[path]
            self.rows += 1

    @rule(path=st.sampled_from(POOL))
    def read(self, path: str) -> None:
        out = self.svc.dispatch("mem_read", {"path": path}, self.step)
        assert out.ok == (path in self.model)
        if out.ok:
            assert out.result["content"] == self.model[path]

    @rule()
    def advance_step(self) -> None:
        self.step += 1

    @rule()
    def take_snapshot(self) -> None:
        self.snaps.append((self.svc.snapshot().rev, dict(self.model)))

    @invariant()
    def caps_and_contents_hold(self) -> None:
        live = self.svc.store.live()
        assert {p: r.content for p, r in live.items()} == self.model
        assert len(live) <= CFG.max_files
        assert sum(r.chars for r in live.values()) <= CFG.max_total_chars
        assert self.svc.store.history_rows == self.rows <= CFG.max_history_rows
        assert self.svc.store.history_chars <= CFG.max_history_chars

    @invariant()
    def snapshots_never_change(self) -> None:
        for rev, expected in self.snaps:
            assert {p: r.content for p, r in self.svc.store.view_at(rev).items()} == expected

    @invariant()
    def versions_are_consecutive_and_revs_increase(self) -> None:
        last = 0
        for path in POOL:
            vs = self.svc.store.versions(path)
            assert [r.version for r in vs] == list(range(1, len(vs) + 1))
            assert all(a.rev < b.rev for a, b in pairwise(vs))
        for row in sorted((r for p in POOL for r in self.svc.store.versions(p)), key=lambda r: r.rev):
            assert row.rev > last
            last = row.rev


MemoryMachine.TestCase.settings = settings(max_examples=150, stateful_step_count=40, deadline=None,
                                           suppress_health_check=list(HealthCheck))
TestMemoryStateMachine = MemoryMachine.TestCase


# ---- no host disk ------------------------------------------------------------------------------
def test_modules_have_no_filesystem_capable_imports_or_calls() -> None:
    allowed_imports = {"bisect", "dataclasses", "difflib", "hashlib", "re", "threading", "typing", "__future__"}
    for module in (memory, memory_paths):
        tree = ast.parse(Path(module.__file__).read_text())  # type: ignore[arg-type]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert {a.name.split(".")[0] for a in node.names} <= allowed_imports, module.__name__
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                pure_types = node.module == "collections.abc"  # abstract base classes only: no I/O
                assert node.level == 1 or root in allowed_imports or pure_types, (module.__name__, node.module)
                if node.level == 1:
                    assert node.module in {"memory_paths"}, node.module
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"open", "exec", "eval", "compile", "__import__"}


def test_a_full_session_performs_no_file_operations(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(*_a: object, **_k: object) -> None:
        raise AssertionError("host file access attempted")

    for target in (builtins, io, os):
        monkeypatch.setattr(target, "open", deny)
    monkeypatch.setattr(os, "fdopen", deny)
    monkeypatch.setattr(Path, "open", deny)
    s = MemoryService(MemoryStore("r"), MemoryConfig())
    for i in range(5):
        s.dispatch("mem_write", {"path": f"f{i}", "content": "x\ny\n"}, i)
    s.dispatch("mem_write", {"path": "../../etc/passwd", "content": "x"}, 5)
    s.dispatch("mem_read", {"path": "/etc/passwd"}, 5)
    s.dispatch("mem_search", {"query": "x"}, 5)
    s.dispatch("mem_delete", {"path": "f1"}, 5)
    MemoryService.forked(s, s.snapshot(), "r2", MemoryConfig()).dispatch("mem_list", {}, 5)


# ---- concurrency --------------------------------------------------------------------------------
def test_concurrent_writers_never_break_the_caps() -> None:
    cfg = MemoryConfig(max_files=5, max_file_chars=50, max_total_chars=120, max_history_rows=5000)
    s = MemoryService(MemoryStore("r"), cfg)
    errors: list[str] = []

    def worker(n: int) -> None:
        for i in range(200):
            out = s.dispatch("mem_write", {"path": f"f{(n + i) % 8}", "content": "x" * ((i * 7 + n) % 50 + 1)}, 0)
            live = s.store.live()
            if len(live) > 5 or sum(r.chars for r in live.values()) > 120:
                errors.append("cap exceeded")
            if i % 10 == 0:
                s.dispatch("mem_delete", {"path": f"f{n % 8}"}, 0)
            del out

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    revs = [r.rev for p in s.store.live() for r in s.store.versions(p)]
    assert len(set(revs)) == len(revs)
