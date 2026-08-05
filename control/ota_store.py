from __future__ import annotations

import hashlib
import json
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MAX_FIRMWARE_BYTES = 0x140000
OTA_CHUNK_BYTES = 128


@dataclass(frozen=True)
class OtaArtifact:
    filename: str
    size: int
    sha256: str
    crc32: int
    chunk_size: int
    chunks: int
    uploaded_at: float
    path: Path

    def as_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "size": self.size,
            "sha256": self.sha256,
            "crc32": self.crc32,
            "chunk_size": self.chunk_size,
            "chunks": self.chunks,
            "uploaded_at": self.uploaded_at,
        }


class OtaArtifactError(ValueError):
    pass


class OtaArtifactStore:
    def __init__(self, root: Path | str = ".control_ota") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._manifest_path = self.root / "current.json"
        self._artifact: OtaArtifact | None = self._load_current()

    def current(self) -> dict[str, Any] | None:
        return self._artifact.as_dict() if self._artifact else None

    def artifact(self) -> OtaArtifact | None:
        return self._artifact

    def read_verified(self, artifact: OtaArtifact | None = None) -> bytes:
        selected = artifact or self._artifact
        if selected is None:
            raise OtaArtifactError("no firmware staged")
        try:
            data = selected.path.read_bytes()
        except OSError as error:
            raise OtaArtifactError(f"cannot read staged firmware: {error}") from error
        if len(data) != selected.size:
            raise OtaArtifactError("staged firmware size mismatch")
        if hashlib.sha256(data).hexdigest() != selected.sha256:
            raise OtaArtifactError("staged firmware SHA-256 mismatch")
        if zlib.crc32(data) & 0xFFFFFFFF != selected.crc32:
            raise OtaArtifactError("staged firmware CRC32 mismatch")
        return data

    def stage(self, filename: str, data: bytes) -> dict[str, Any]:
        clean_name = Path(filename or "firmware.bin").name
        if not clean_name.endswith(".bin"):
            raise OtaArtifactError("firmware artifact must be a .bin file")
        if len(data) == 0:
            raise OtaArtifactError("firmware artifact is empty")
        if len(data) > MAX_FIRMWARE_BYTES:
            raise OtaArtifactError(f"firmware artifact exceeds {MAX_FIRMWARE_BYTES} bytes")

        sha256 = hashlib.sha256(data).hexdigest()
        crc32 = zlib.crc32(data) & 0xFFFFFFFF
        path = self.root / f"{sha256}.bin"
        path.write_bytes(data)
        artifact = OtaArtifact(
            filename=clean_name,
            size=len(data),
            sha256=sha256,
            crc32=crc32,
            chunk_size=OTA_CHUNK_BYTES,
            chunks=(len(data) + OTA_CHUNK_BYTES - 1) // OTA_CHUNK_BYTES,
            uploaded_at=time.time(),
            path=path,
        )
        self._artifact = artifact
        self._manifest_path.write_text(json.dumps(artifact.as_dict(), sort_keys=True), encoding="utf-8")
        return artifact.as_dict()

    def _load_current(self) -> OtaArtifact | None:
        if not self._manifest_path.exists():
            return None
        try:
            metadata = json.loads(self._manifest_path.read_text(encoding="utf-8"))
            path = self.root / f"{metadata['sha256']}.bin"
            if not path.exists():
                return None
            return OtaArtifact(
                filename=str(metadata["filename"]),
                size=int(metadata["size"]),
                sha256=str(metadata["sha256"]),
                crc32=int(metadata["crc32"]),
                chunk_size=int(metadata["chunk_size"]),
                chunks=int(metadata["chunks"]),
                uploaded_at=float(metadata["uploaded_at"]),
                path=path,
            )
        except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
            return None


class OtaInstallStore:
    """Durable OTA job state, separate from the checksum-pinned artifact."""

    def __init__(self, root: Path | str = ".control_ota") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "install.json"

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"running": False, "complete": False, "error": None}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"running": False, "complete": False, "error": None}
        return value if isinstance(value, dict) else {
            "running": False, "complete": False, "error": None,
        }

    def save(self, state: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


class PersistentOtaInstall(dict[str, Any]):
    def __init__(self, store: OtaInstallStore, initial: dict[str, Any]) -> None:
        self.store = store
        super().__init__(initial)

    def update(self, *args: Any, **kwargs: Any) -> None:
        super().update(*args, **kwargs)
        self.store.save(dict(self))

    def update_volatile(self, *args: Any, **kwargs: Any) -> None:
        """Update live UI progress without wearing storage on every OTA chunk."""
        super().update(*args, **kwargs)

    def reset(self, value: dict[str, Any]) -> None:
        super().clear()
        super().update(value)
        self.store.save(dict(self))
