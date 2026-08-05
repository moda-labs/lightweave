from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any


GROUP_COUNT = 8
GROUP_NAME_MAX_LENGTH = 48


class GroupStoreError(ValueError):
    pass


@dataclass
class GroupStore:
    root: Path = Path(".control_groups")
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.path = self.root / "groups.json"

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            names = self._load()
        return [_group(group_id, names.get(group_id, "")) for group_id in range(GROUP_COUNT)]

    def update(self, group_id: int, name: str) -> dict[str, Any]:
        if group_id < 0 or group_id >= GROUP_COUNT:
            raise GroupStoreError("group id must be between 0 and 7")
        clean_name = _normalize_name(name)
        with self._lock:
            names = self._load()
            if clean_name:
                names[group_id] = clean_name
            else:
                names.pop(group_id, None)
            self._save(names)
        return _group(group_id, clean_name)

    def _load(self) -> dict[int, str]:
        if not self.path.exists():
            return {}
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            raise GroupStoreError("group store is corrupt or unreadable") from error
        if not isinstance(raw, dict):
            raise GroupStoreError("group store is corrupt")
        names: dict[int, str] = {}
        for raw_id, raw_name in raw.items():
            try:
                group_id = int(raw_id)
            except (TypeError, ValueError) as error:
                raise GroupStoreError("group store has an invalid group id") from error
            if group_id < 0 or group_id >= GROUP_COUNT:
                raise GroupStoreError("group store has an invalid group id")
            name = _normalize_name(raw_name)
            if name:
                names[group_id] = name
        return names

    def _save(self, names: dict[int, str]) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump({str(key): value for key, value in names.items()}, handle, indent=2, sort_keys=True)
                handle.write("\n")
            tmp.replace(self.path)
        except OSError as error:
            raise GroupStoreError("could not save group names") from error


def _normalize_name(value: Any) -> str:
    if not isinstance(value, str):
        raise GroupStoreError("group name must be text")
    name = " ".join(value.split())
    if len(name) > GROUP_NAME_MAX_LENGTH:
        raise GroupStoreError(f"group name must be {GROUP_NAME_MAX_LENGTH} characters or fewer")
    return name


def _group(group_id: int, name: str) -> dict[str, Any]:
    default_label = f"Group {group_id + 1}"
    return {
        "group_id": group_id,
        "name": name,
        "label": f"{default_label} · {name}" if name else default_label,
    }
