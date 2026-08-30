from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol


DEFAULT_TRACK_FILENAME = "baskets-soundscape-v4.mp3"


class AudioPlayerError(RuntimeError):
    pass


@dataclass(frozen=True)
class AudioTrack:
    track_id: str
    name: str
    path: Path
    duration_s: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.track_id,
            "name": self.name,
            "duration_s": self.duration_s,
        }


class AudioBackend(Protocol):
    @property
    def available(self) -> bool: ...

    @property
    def unavailable_reason(self) -> str | None: ...

    def start(self, path: Path) -> Any: ...

    def pause(self, process: Any) -> None: ...

    def resume(self, process: Any) -> None: ...

    def stop(self, process: Any) -> None: ...

    def is_running(self, process: Any) -> bool: ...


class Mpg123Backend:
    """Small Unix process wrapper for looped, headless ALSA playback."""

    def __init__(
        self,
        binary: str | None = None,
        audio_device: str | None = None,
        *,
        startup_grace_s: float = 0.1,
        muted: bool = False,
    ) -> None:
        configured = binary or os.getenv("CONTROL_AUDIO_PLAYER_BIN", "mpg123")
        self.binary = shutil.which(configured) if "/" not in configured else configured
        self.audio_device = audio_device or os.getenv("CONTROL_AUDIO_DEVICE")
        self.startup_grace_s = max(0.0, startup_grace_s)
        self.muted = muted

    @property
    def available(self) -> bool:
        return bool(
            self.binary
            and Path(self.binary).is_file()
            and os.access(self.binary, os.X_OK)
        )

    @property
    def unavailable_reason(self) -> str | None:
        return None if self.available else "mpg123 is not installed or executable"

    def start(self, path: Path) -> subprocess.Popen[bytes]:
        if not self.available or self.binary is None:
            raise AudioPlayerError(self.unavailable_reason or "audio player is unavailable")
        command = [self.binary, "--quiet", "--loop", "-1"]
        if self.muted:
            command.extend(["--scale", "0"])
        if self.audio_device:
            command.extend(["--audiodevice", self.audio_device])
        command.append(str(path))
        stderr = tempfile.TemporaryFile()
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
                start_new_session=True,
            )
        except Exception:
            stderr.close()
            raise
        setattr(process, "_lightweave_stderr", stderr)
        if self.startup_grace_s:
            time.sleep(self.startup_grace_s)
        if process.poll() is not None:
            reason = self.failure_reason(process)
            raise AudioPlayerError(reason or "mpg123 exited before playback started")
        return process

    def failure_reason(self, process: subprocess.Popen[bytes]) -> str | None:
        if process.poll() is None:
            return None
        stderr = getattr(process, "_lightweave_stderr", None)
        if stderr is None:
            return None
        setattr(process, "_lightweave_stderr", None)
        try:
            stderr.flush()
            stderr.seek(0)
            message = stderr.read(4096).decode("utf-8", errors="replace").strip()
        except (OSError, ValueError):
            message = ""
        finally:
            stderr.close()
        return message.splitlines()[-1] if message else None

    def pause(self, process: subprocess.Popen[bytes]) -> None:
        process.send_signal(signal.SIGSTOP)

    def resume(self, process: subprocess.Popen[bytes]) -> None:
        process.send_signal(signal.SIGCONT)

    def stop(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            self.failure_reason(process)
            return
        # A stopped process cannot act on SIGTERM until it is continued.
        process.send_signal(signal.SIGCONT)
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        self.failure_reason(process)

    def is_running(self, process: subprocess.Popen[bytes]) -> bool:
        return process.poll() is None


def _track_name(path: Path) -> str:
    words = re.sub(r"[-_]+", " ", path.stem).split()
    return " ".join(word.upper() if word.lower() == "mp3" else word.title() for word in words)


def _synchsafe(value: bytes) -> int:
    if len(value) != 4 or any(byte & 0x80 for byte in value):
        return 0
    return (value[0] << 21) | (value[1] << 14) | (value[2] << 7) | value[3]


def mp3_duration(path: Path) -> float | None:
    """Read enough MPEG metadata for an accurate CBR or Xing duration."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            prefix = handle.read(min(size, 2 * 1024 * 1024))
    except OSError:
        return None
    if prefix.startswith(b"version https://git-lfs.github.com/spec/v1"):
        return None
    offset = 0
    if prefix[:3] == b"ID3" and len(prefix) >= 10:
        offset = 10 + _synchsafe(prefix[6:10])
    bitrate_tables = {
        3: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0),
        2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0),
        0: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0),
    }
    sample_rates = {
        3: (44_100, 48_000, 32_000),
        2: (22_050, 24_000, 16_000),
        0: (11_025, 12_000, 8_000),
    }
    limit = max(offset, len(prefix) - 4)
    for index in range(offset, limit):
        header = int.from_bytes(prefix[index : index + 4], "big")
        if header >> 21 != 0x7FF:
            continue
        version = (header >> 19) & 0x3
        layer = (header >> 17) & 0x3
        bitrate_index = (header >> 12) & 0xF
        sample_index = (header >> 10) & 0x3
        if version == 1 or layer != 1 or bitrate_index in {0, 15} or sample_index == 3:
            continue
        bitrate_kbps = bitrate_tables[version][bitrate_index]
        sample_rate = sample_rates[version][sample_index]
        channel_mode = (header >> 6) & 0x3
        has_crc = ((header >> 16) & 0x1) == 0
        side_info = 17 if version == 3 and channel_mode == 3 else 32 if version == 3 else 9 if channel_mode == 3 else 17
        xing_offset = index + 4 + (2 if has_crc else 0) + side_info
        if prefix[xing_offset : xing_offset + 4] in {b"Xing", b"Info"}:
            flags = int.from_bytes(prefix[xing_offset + 4 : xing_offset + 8], "big")
            if flags & 0x1:
                frames = int.from_bytes(prefix[xing_offset + 8 : xing_offset + 12], "big")
                samples_per_frame = 1152 if version == 3 else 576
                if frames > 0:
                    return frames * samples_per_frame / sample_rate
        trailing_tag = 128 if size >= 128 and prefix[-128:-125] == b"TAG" else 0
        audio_bytes = max(0, size - index - trailing_tag)
        return audio_bytes * 8 / (bitrate_kbps * 1000)
    return None


class AudioPlayer:
    def __init__(
        self,
        tracks_dir: Path,
        state_path: Path | None,
        *,
        backend: AudioBackend | None = None,
        clock: Callable[[], float] = time.monotonic,
        revision_clock: Callable[[], float] = time.time,
    ) -> None:
        self.tracks_dir = tracks_dir
        self.state_path = state_path
        self.backend = backend or Mpg123Backend()
        self.clock = clock
        self.revision_clock = revision_clock
        self._lock = threading.RLock()
        self._discovery_error: str | None = None
        self._tracks = self._discover_tracks()
        stored = self._load_state()
        stored_track = stored.get("selected_track")
        self._selected_track = (
            str(stored_track)
            if isinstance(stored_track, str) and stored_track in self._tracks
            else self._default_track_id()
        )
        self._paused = stored.get("paused") is True
        self._elapsed_s = 0.0
        self._started_at = self.clock()
        self._process: Any | None = None
        self._running = False
        self._last_start_attempt = 0.0
        self._error: str | None = None
        self._revision = int(self.revision_clock() * 1000)

    def _bump_revision(self) -> None:
        self._revision = max(self._revision + 1, int(self.revision_clock() * 1000))

    def _discover_tracks(self) -> dict[str, AudioTrack]:
        try:
            paths = sorted(
                (path for path in self.tracks_dir.iterdir() if path.is_file() and path.suffix.lower() == ".mp3"),
                key=lambda path: path.name.lower(),
            )
        except OSError:
            paths = []
        tracks = {}
        for path in paths:
            try:
                with path.open("rb") as handle:
                    prefix = handle.read(128)
            except OSError:
                continue
            if prefix.startswith(b"version https://git-lfs.github.com/spec/v1"):
                self._discovery_error = "MP3 assets are Git LFS pointers; run git lfs pull"
                continue
            duration_s = mp3_duration(path)
            if duration_s is None:
                self._discovery_error = f"cannot read MP3 metadata for {path.name}"
                continue
            tracks[path.name] = AudioTrack(path.name, _track_name(path), path, duration_s)
        return tracks

    def _default_track_id(self) -> str | None:
        if DEFAULT_TRACK_FILENAME in self._tracks:
            return DEFAULT_TRACK_FILENAME
        candidate = next(
            (track_id for track_id in self._tracks if "soundscape" in track_id.lower() and "v4" in track_id.lower()),
            None,
        )
        return candidate or next(iter(self._tracks), None)

    def _load_state(self) -> dict[str, Any]:
        if self.state_path is None:
            return {}
        try:
            document = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        return document if isinstance(document, dict) else {}

    def _save_state(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(
            {"schema_version": 1, "selected_track": self._selected_track, "paused": self._paused},
            sort_keys=True,
            indent=2,
        ).encode() + b"\n"
        descriptor, temporary = tempfile.mkstemp(prefix=f".{self.state_path.name}.", dir=self.state_path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                os.fchmod(handle.fileno(), 0o600)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _track(self) -> AudioTrack | None:
        return self._tracks.get(self._selected_track or "")

    def _position(self) -> float:
        position = self._elapsed_s
        if self._process is not None and not self._paused and self.backend.is_running(self._process):
            position += max(0.0, self.clock() - self._started_at)
        track = self._track()
        if track and track.duration_s and track.duration_s > 0:
            position %= track.duration_s
        return max(0.0, position)

    def _stop_process(self) -> None:
        process = self._process
        self._process = None
        if process is not None:
            try:
                self.backend.stop(process)
            except (OSError, subprocess.SubprocessError):
                pass

    def _ensure_playing(self, *, force: bool = False) -> None:
        if not self._running or self._paused:
            return
        try:
            previous_process = self._process
            previous_error = self._error
            if self._process is not None and self.backend.is_running(self._process):
                return
            if self._process is not None:
                failure_reason = getattr(self.backend, "failure_reason", lambda _process: None)(
                    self._process
                )
                self._error = failure_reason or "audio player stopped unexpectedly; retrying"
            self._process = None
            now = self.clock()
            if not force and now - self._last_start_attempt < 5:
                return
            self._last_start_attempt = now
            track = self._track()
            if track is None:
                self._error = self._discovery_error or "no MP3 tracks found"
                return
            if not self.backend.available:
                self._error = self.backend.unavailable_reason or "audio player is unavailable"
                return
            try:
                self._process = self.backend.start(track.path)
            except (AudioPlayerError, OSError, subprocess.SubprocessError) as error:
                self._error = str(error)
                return
            self._elapsed_s = 0.0
            self._started_at = now
            self._error = None
        finally:
            if self._process is not previous_process or self._error != previous_error:
                self._bump_revision()

    def start(self) -> None:
        with self._lock:
            changed = not self._running
            self._running = True
            self._ensure_playing(force=True)
            if changed:
                self._bump_revision()

    def shutdown(self) -> None:
        with self._lock:
            changed = self._running or self._process is not None
            self._running = False
            self._stop_process()
            if changed:
                self._bump_revision()

    def select(self, track_id: str) -> dict[str, Any]:
        with self._lock:
            if track_id not in self._tracks:
                raise AudioPlayerError("unknown soundtrack")
            self._stop_process()
            self._selected_track = track_id
            self._elapsed_s = 0.0
            self._started_at = self.clock()
            self._save_state()
            self._ensure_playing(force=True)
            self._bump_revision()
            return self.status()

    def pause(self) -> dict[str, Any]:
        with self._lock:
            if not self._paused:
                self._elapsed_s = self._position()
                if self._process is not None and self.backend.is_running(self._process):
                    try:
                        self.backend.pause(self._process)
                    except (OSError, subprocess.SubprocessError) as error:
                        self._process = None
                        self._error = str(error)
                self._paused = True
                self._save_state()
                self._bump_revision()
            return self.status()

    def play(self) -> dict[str, Any]:
        with self._lock:
            self._running = True
            if self._paused:
                self._paused = False
                if self._process is not None and self.backend.is_running(self._process):
                    try:
                        self.backend.resume(self._process)
                    except (OSError, subprocess.SubprocessError) as error:
                        self._process = None
                        self._error = str(error)
                        self._ensure_playing(force=True)
                    else:
                        self._started_at = self.clock()
                        self._error = None
                else:
                    self._ensure_playing(force=True)
                self._save_state()
                self._bump_revision()
            else:
                self._ensure_playing(force=True)
            return self.status()

    def restart(self) -> dict[str, Any]:
        with self._lock:
            self._stop_process()
            self._elapsed_s = 0.0
            self._started_at = self.clock()
            self._ensure_playing(force=True)
            self._bump_revision()
            return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_playing()
            track = self._track()
            process_running = self._process is not None and self.backend.is_running(self._process)
            return {
                "available": bool(self._tracks) and self.backend.available,
                "playing": process_running and not self._paused,
                "paused": self._paused,
                "loop": True,
                "revision": self._revision,
                "selected_track": self._selected_track,
                "track": track.as_dict() if track else None,
                "position_s": self._position(),
                "error": self._error or self._discovery_error or (self.backend.unavailable_reason if not self.backend.available else None),
                "tracks": [track.as_dict() for track in self._tracks.values()],
            }


def validate_audio_install(
    tracks_dir: Path,
    *,
    backend: AudioBackend | None = None,
    probe_wait_s: float = 0.0,
) -> str | None:
    player = AudioPlayer(
        tracks_dir,
        None,
        backend=backend or Mpg123Backend(muted=True),
    )
    status = player.status()
    if not status["available"]:
        return str(status["error"] or "audio player is unavailable")
    if DEFAULT_TRACK_FILENAME not in {track["id"] for track in status["tracks"]}:
        return f"default soundtrack is missing: {DEFAULT_TRACK_FILENAME}"
    player.start()
    if probe_wait_s > 0:
        time.sleep(probe_wait_s)
    status = player.status()
    player.shutdown()
    if not status["playing"]:
        return str(status["error"] or "audio output probe failed")
    return None


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args != ["check"]:
        print("usage: python -m control.audio_player check", file=sys.stderr)
        return 2
    default_dir = Path(__file__).resolve().parents[1] / "sound"
    error = validate_audio_install(
        Path(os.getenv("CONTROL_AUDIO_DIR", str(default_dir))).expanduser(),
        probe_wait_s=0.15,
    )
    if error:
        print(f"audio preflight failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
