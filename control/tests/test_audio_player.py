from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from control.audio_player import (
    AudioPlayer,
    AudioPlayerError,
    Mpg123Backend,
    main,
    mp3_duration,
    validate_audio_install,
)


class FakeProcess:
    def __init__(self) -> None:
        self.running = True
        self.paused = False


class FakeBackend:
    available = True
    unavailable_reason = None

    def __init__(self) -> None:
        self.started: list[Path] = []
        self.processes: list[FakeProcess] = []

    def start(self, path: Path) -> FakeProcess:
        process = FakeProcess()
        self.started.append(path)
        self.processes.append(process)
        return process

    def pause(self, process: FakeProcess) -> None:
        process.paused = True

    def resume(self, process: FakeProcess) -> None:
        process.paused = False

    def stop(self, process: FakeProcess) -> None:
        process.running = False

    def is_running(self, process: FakeProcess) -> bool:
        return process.running


class UnavailableBackend(FakeBackend):
    available = False
    unavailable_reason = "decoder unavailable"


class FailingBackend(FakeBackend):
    def start(self, path: Path) -> FakeProcess:
        raise OSError("audio device busy")


def test_mpg123_backend_starts_an_infinite_headless_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "mpg123"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    track = tmp_path / "track.mp3"
    fake_mp3(track)
    captured = {}

    class FakePopen:
        returncode = None

        def poll(self):
            return self.returncode

        def send_signal(self, _signal) -> None:
            pass

        def terminate(self) -> None:
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakePopen()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    backend = Mpg123Backend(
        binary=str(binary), audio_device="hw:Headphones", startup_grace_s=0
    )

    process = backend.start(track)

    assert captured["command"] == [
        str(binary),
        "--quiet",
        "--loop",
        "-1",
        "--audiodevice",
        "hw:Headphones",
        str(track),
    ]
    assert captured["kwargs"]["start_new_session"] is True
    assert captured["kwargs"]["stdin"] == subprocess.DEVNULL
    assert captured["kwargs"]["stderr"] != subprocess.DEVNULL
    backend.stop(process)


def test_mpg123_backend_reports_startup_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "mpg123"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    track = tmp_path / "track.mp3"
    fake_mp3(track)

    class ExitedPopen:
        def poll(self):
            return 1

    def fake_popen(_command, **kwargs):
        kwargs["stderr"].write(b"ALSA output failed\n")
        return ExitedPopen()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    backend = Mpg123Backend(binary=str(binary), startup_grace_s=0)

    with pytest.raises(AudioPlayerError, match="ALSA output failed"):
        backend.start(track)


def test_mpg123_backend_can_probe_the_output_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "mpg123"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    track = tmp_path / "track.mp3"
    fake_mp3(track)
    captured = {}

    class FakePopen:
        returncode = None

        def poll(self):
            return self.returncode

        def send_signal(self, _signal) -> None:
            pass

        def terminate(self) -> None:
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

    def fake_popen(command, **_kwargs):
        captured["command"] = command
        return FakePopen()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    backend = Mpg123Backend(binary=str(binary), startup_grace_s=0, muted=True)

    process = backend.start(track)

    assert captured["command"] == [
        str(binary),
        "--quiet",
        "--loop",
        "-1",
        "--scale",
        "0",
        str(track),
    ]
    backend.stop(process)


def fake_mp3(path: Path, size: int = 320_000) -> None:
    # MPEG-1 Layer III, 320 kbps, 44.1 kHz stereo.
    path.write_bytes(bytes.fromhex("fffb e000") + b"\0" * (size - 4))


def test_default_soundscape_autoplays_loops_and_tracks_position(tmp_path: Path) -> None:
    tracks = tmp_path / "sound"
    tracks.mkdir()
    fake_mp3(tracks / "baskets-final-boat.mp3")
    fake_mp3(tracks / "baskets-soundscape-v4.mp3", size=640_000)
    now = [100.0]
    backend = FakeBackend()
    player = AudioPlayer(
        tracks,
        tmp_path / "state" / "player.json",
        backend=backend,
        clock=lambda: now[0],
    )

    player.start()
    now[0] += 3.25
    status = player.status()

    assert status["selected_track"] == "baskets-soundscape-v4.mp3"
    assert status["track"]["name"] == "Baskets Soundscape V4"
    assert status["playing"] is True
    assert status["loop"] is True
    assert status["position_s"] == pytest.approx(3.25)
    assert backend.started == [tracks / "baskets-soundscape-v4.mp3"]
    assert mp3_duration(tracks / "baskets-soundscape-v4.mp3") == pytest.approx(16.0)


def test_pause_resume_and_track_selection_are_persistent(tmp_path: Path) -> None:
    tracks = tmp_path / "sound"
    tracks.mkdir()
    fake_mp3(tracks / "baskets-final-boat.mp3")
    fake_mp3(tracks / "baskets-soundscape-v4.mp3")
    state_path = tmp_path / "audio" / "player.json"
    now = [10.0]
    backend = FakeBackend()
    player = AudioPlayer(tracks, state_path, backend=backend, clock=lambda: now[0])
    player.start()

    now[0] += 4
    paused = player.pause()
    assert paused["paused"] is True
    assert paused["playing"] is False
    assert paused["position_s"] == pytest.approx(4)

    selected = player.select("baskets-final-boat.mp3")
    assert selected["selected_track"] == "baskets-final-boat.mp3"
    assert selected["paused"] is True
    assert len(backend.started) == 1

    resumed = player.play()
    assert resumed["playing"] is True
    assert backend.started[-1] == tracks / "baskets-final-boat.mp3"
    stored = json.loads(state_path.read_text())
    assert stored == {
        "paused": False,
        "schema_version": 1,
        "selected_track": "baskets-final-boat.mp3",
    }

    player.shutdown()
    assert backend.processes[-1].running is False
    restored_backend = FakeBackend()
    restored = AudioPlayer(tracks, state_path, backend=restored_backend)
    restored.start()
    assert restored.status()["selected_track"] == "baskets-final-boat.mp3"
    assert restored.status()["playing"] is True


def test_paused_state_stays_paused_across_restart(tmp_path: Path) -> None:
    tracks = tmp_path / "sound"
    tracks.mkdir()
    fake_mp3(tracks / "baskets-soundscape-v4.mp3")
    state_path = tmp_path / "audio" / "player.json"
    state_path.parent.mkdir()
    state_path.write_text(
        json.dumps({"schema_version": 1, "selected_track": "baskets-soundscape-v4.mp3", "paused": True})
    )
    backend = FakeBackend()

    player = AudioPlayer(tracks, state_path, backend=backend)
    player.start()

    assert player.status()["paused"] is True
    assert player.status()["playing"] is False
    assert backend.started == []


def test_unknown_track_is_rejected_without_stopping_current_track(tmp_path: Path) -> None:
    tracks = tmp_path / "sound"
    tracks.mkdir()
    fake_mp3(tracks / "baskets-soundscape-v4.mp3")
    backend = FakeBackend()
    player = AudioPlayer(tracks, None, backend=backend)
    player.start()

    with pytest.raises(AudioPlayerError, match="unknown soundtrack"):
        player.select("../not-a-track.mp3")

    assert player.status()["playing"] is True
    assert backend.processes[0].running is True


def test_git_lfs_pointer_is_not_treated_as_playable_audio(tmp_path: Path) -> None:
    tracks = tmp_path / "sound"
    tracks.mkdir()
    (tracks / "baskets-soundscape-v4.mp3").write_text(
        "version https://git-lfs.github.com/spec/v1\n"
        "oid sha256:" + "0" * 64 + "\nsize 123456\n"
    )
    player = AudioPlayer(tracks, None, backend=FakeBackend())

    player.start()
    status = player.status()

    assert status["available"] is False
    assert status["playing"] is False
    assert status["tracks"] == []
    assert status["error"] == "MP3 assets are Git LFS pointers; run git lfs pull"
    assert validate_audio_install(tracks, backend=FakeBackend()) == status["error"]


def test_install_validation_requires_the_default_soundscape(tmp_path: Path) -> None:
    tracks = tmp_path / "sound"
    tracks.mkdir()
    fake_mp3(tracks / "another-track.mp3")

    assert validate_audio_install(tracks, backend=FakeBackend()) == (
        "default soundtrack is missing: baskets-soundscape-v4.mp3"
    )


def test_missing_or_invalid_mp3_metadata_is_reported_without_starting(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    assert mp3_duration(missing / "nope.mp3") is None

    tracks = tmp_path / "sound"
    tracks.mkdir()
    (tracks / "broken.mp3").write_bytes(b"not an mp3")
    backend = FakeBackend()
    player = AudioPlayer(tracks, None, backend=backend)

    player.start()
    status = player.status()

    assert status["available"] is False
    assert status["tracks"] == []
    assert status["error"] == "cannot read MP3 metadata for broken.mp3"
    assert backend.started == []


def test_xing_frame_count_takes_precedence_over_cbr_size(tmp_path: Path) -> None:
    track = tmp_path / "vbr.mp3"
    payload = bytearray(10_000)
    payload[:4] = bytes.fromhex("fffb e000")
    payload[36:40] = b"Xing"
    payload[40:44] = (1).to_bytes(4, "big")
    payload[44:48] = (100).to_bytes(4, "big")
    track.write_bytes(payload)

    assert mp3_duration(track) == pytest.approx(100 * 1152 / 44_100)


def test_unavailable_or_failing_backend_surfaces_recoverable_status(tmp_path: Path) -> None:
    tracks = tmp_path / "sound"
    tracks.mkdir()
    fake_mp3(tracks / "baskets-soundscape-v4.mp3")

    unavailable = AudioPlayer(tracks, None, backend=UnavailableBackend())
    unavailable.start()
    assert unavailable.status()["error"] == "decoder unavailable"
    assert unavailable.status()["playing"] is False

    failing = AudioPlayer(tracks, None, backend=FailingBackend())
    failing.start()
    assert failing.status()["error"] == "audio device busy"
    assert failing.status()["playing"] is False


def test_unexpected_backend_exit_restarts_after_retry_window(tmp_path: Path) -> None:
    tracks = tmp_path / "sound"
    tracks.mkdir()
    fake_mp3(tracks / "baskets-soundscape-v4.mp3")
    now = [100.0]
    backend = FakeBackend()
    player = AudioPlayer(tracks, None, backend=backend, clock=lambda: now[0])
    player.start()
    backend.processes[-1].running = False

    now[0] += 1
    assert player.status()["playing"] is False
    assert len(backend.started) == 1

    now[0] += 5
    assert player.status()["playing"] is True
    assert len(backend.started) == 2


def test_restart_while_paused_resets_position_without_starting(tmp_path: Path) -> None:
    tracks = tmp_path / "sound"
    tracks.mkdir()
    fake_mp3(tracks / "baskets-soundscape-v4.mp3")
    now = [20.0]
    backend = FakeBackend()
    player = AudioPlayer(tracks, None, backend=backend, clock=lambda: now[0])
    player.start()
    now[0] += 7
    player.pause()

    restarted = player.restart()

    assert restarted["paused"] is True
    assert restarted["position_s"] == 0
    assert restarted["playing"] is False
    assert len(backend.started) == 1


def test_cli_preflight_reports_usage_failure_and_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main([]) == 2
    assert "usage:" in capsys.readouterr().err

    tracks = tmp_path / "sound"
    tracks.mkdir()
    fake_mp3(tracks / "baskets-soundscape-v4.mp3")
    binary = tmp_path / "mpg123"
    binary.write_text("#!/bin/sh\nsleep 2\n")
    binary.chmod(0o755)
    monkeypatch.setenv("CONTROL_AUDIO_DIR", str(tracks))
    monkeypatch.setenv("CONTROL_AUDIO_PLAYER_BIN", str(binary))

    assert main(["check"]) == 0
