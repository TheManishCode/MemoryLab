import threading
from pathlib import Path

import pytest

from runlog.store import AlreadyExists, BadKey, LocalObjectStore, NotFound, TooLarge, check_key

GOOD = ["a", "runs/abc/events/000001.jsonl.zst", "x/y_z/0.json", "a-b_c.d", "m" * 100]
BAD = ["", "/a", "a/", "a//b", "../a", "a/../b", "a/./b", ".hidden", "a/.hidden", "A", "a b", "a\\b", "a\x00b", "é",
       "a" * 301, "/".join(["a"] * 9), "a..b", "x.", "~/a", "a:b", "a?b", "a%2e", None, 5]


@pytest.mark.parametrize("key", GOOD)
def test_good_keys(key: str) -> None:
    assert check_key(key) == key


@pytest.mark.parametrize("key", BAD)
def test_bad_keys(key: object) -> None:
    with pytest.raises(BadKey):
        check_key(key)


def test_put_get_exists_list_delete(store: LocalObjectStore) -> None:
    store.put("runs/a/x.json", b"1")
    store.put("runs/a/sub/y.json", b"2")
    store.put("runs/b/z.json", b"3")
    assert store.get("runs/a/x.json") == b"1" and store.exists("runs/a/x.json") and not store.exists("runs/a/nope")
    assert store.list("runs/a") == ["runs/a/sub/y.json", "runs/a/x.json"] and store.list("runs/zzz") == []
    store.delete("runs/a/x.json")
    store.delete("runs/a/x.json")  # idempotent
    with pytest.raises(NotFound):
        store.get("runs/a/x.json")


def test_objects_are_immutable_unless_overwrite_is_explicit(store: LocalObjectStore) -> None:
    store.put("k", b"1")
    with pytest.raises(AlreadyExists):
        store.put("k", b"2")
    assert store.get("k") == b"1"
    store.put("k", b"3", overwrite=True)
    assert store.get("k") == b"3"


def test_size_limits_and_append(tmp_path: Path) -> None:
    s = LocalObjectStore(tmp_path / "o", max_object_bytes=10)
    with pytest.raises(TooLarge):
        s.put("big", b"x" * 11)
    assert s.append("log", b"abcde") == 0 and s.append("log", b"fghij") == 5
    with pytest.raises(TooLarge):
        s.append("log", b"k")
    assert s.get("log") == b"abcdefghij"


def test_no_temp_files_remain_and_listing_hides_them(store: LocalObjectStore) -> None:
    store.put("a/b", b"1")
    with pytest.raises(AlreadyExists):
        store.put("a/b", b"2")
    assert [p.name for p in (store.root / "a").iterdir()] == ["b"]


def test_a_symlink_cannot_lead_outside_the_root(store: LocalObjectStore, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (store.root / "link").symlink_to(outside)
    except OSError:  # Windows refuses symlink creation without privilege/Developer Mode
        pytest.skip("creating a symlink requires privilege on this platform")
    with pytest.raises(BadKey):
        store.put("link/escape.txt", b"x")
    with pytest.raises(BadKey):
        store.get("link/secret")
    assert not (outside / "escape.txt").exists()


def test_concurrent_appends_do_not_interleave(store: LocalObjectStore) -> None:
    def worker(n: int) -> None:
        for i in range(50):
            store.append("log", f"{n}:{i:03d}\n".encode())

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    lines = store.get("log").decode().splitlines()
    assert len(lines) == 400 and all(len(x) == 5 for x in lines)
