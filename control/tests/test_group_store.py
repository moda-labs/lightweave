from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
import time

import pytest

from control.group_store import GroupStore, GroupStoreError


def test_corrupt_group_store_reports_a_domain_error(tmp_path: Path) -> None:
    store = GroupStore(tmp_path)
    tmp_path.mkdir(exist_ok=True)
    store.path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(GroupStoreError, match="corrupt or unreadable"):
        store.list()


def test_group_store_wraps_atomic_save_failures(tmp_path: Path, monkeypatch) -> None:
    store = GroupStore(tmp_path)

    def fail_replace(_self: Path, _target: Path) -> None:
        raise OSError("read-only filesystem")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(GroupStoreError, match="could not save"):
        store.update(0, "Bikes")


def test_group_store_serializes_concurrent_updates(tmp_path: Path, monkeypatch) -> None:
    store = GroupStore(tmp_path)
    original_save = store._save
    counter_lock = Lock()
    active = 0
    max_active = 0

    def tracked_save(names: dict[int, str]) -> None:
        nonlocal active, max_active
        with counter_lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.02)
        original_save(names)
        with counter_lock:
            active -= 1

    monkeypatch.setattr(store, "_save", tracked_save)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(store.update, 0, "Box lanterns"),
            pool.submit(store.update, 1, "Bikes"),
        ]
        for future in futures:
            future.result()

    assert max_active == 1
    assert [group["name"] for group in store.list()[:2]] == ["Box lanterns", "Bikes"]
