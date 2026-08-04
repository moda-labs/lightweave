from control.mock_conductor import MockConductor


def test_snapshot_counts_healthy_placed_over_placed_total() -> None:
    conductor = MockConductor()

    snapshot = conductor.snapshot()

    assert snapshot["summary"]["alive"] == 8
    assert snapshot["summary"]["total"] == 9
    assert snapshot["summary"]["attention"] == 2
    assert snapshot["pattern"]["pattern"] == "Glow"
    assert snapshot["summary"]["firmware"]["consistent"] is True
    assert snapshot["summary"]["firmware"]["matching"] == 8
    assert snapshot["summary"]["firmware"]["expected"] == 9
    assert snapshot["summary"]["firmware"]["version"] == "0.3.0"


def test_firmware_mismatch_is_attention() -> None:
    conductor = MockConductor()
    conductor._lanterns[0].firmware = {
        "version": "0.2.0",
        "proto": 6,
        "build_id": 0xDEADBEEF,
        "build_label": "deadbeef",
        "dirty": False,
    }

    snapshot = conductor.snapshot()
    lantern = next(item for item in snapshot["lanterns"] if item["mac"] == "8C:94:DF:8F:71:50")

    assert lantern["attention"] == "Firmware mismatch"
    assert snapshot["summary"]["firmware"]["consistent"] is False
    assert snapshot["summary"]["firmware"]["matching"] == 7
    assert snapshot["summary"]["attention"] == 3
    assert snapshot["recovery"]["status"] == "mixed_firmware"
    assert snapshot["recovery"]["mismatched"][0]["mac"] == "8C:94:DF:8F:71:50"


def test_ota_readiness_updates_online_performers_and_defers_missing_rows() -> None:
    conductor = MockConductor()

    idle = conductor.snapshot()["ota"]
    assert idle["mode"] == "idle"
    assert idle["ready"] is False
    assert "not in maintenance mode" in idle["blocked"]

    conductor.set_ota_mode(True)
    missing = conductor.snapshot()["ota"]
    assert missing["mode"] == "maintenance"
    assert missing["ready"] is True
    assert missing["ready_count"] == 8
    assert missing["deferred"] == 1
    assert missing["blocked"] == []
    assert conductor.snapshot()["recovery"]["status"] == "missing_nodes"

    replacement = next(item for item in conductor._lanterns if item.mac == "A0:B7:65:11:44:91")
    replacement.status = "alive"
    replacement.last_seen_s = 2
    ready = conductor.snapshot()["ota"]
    assert ready["ready"] is True
    assert ready["blocked"] == []
    assert conductor.snapshot()["recovery"]["status"] == "ready"

    conductor._lanterns[0].firmware = {
        "version": "0.3.0-mismatch",
        "proto": 6,
        "build_id": 0x44D028FD,
        "build_label": "44d028fd",
        "dirty": False,
    }
    recovery_ready = conductor.snapshot()
    assert recovery_ready["summary"]["firmware"]["consistent"] is False
    assert recovery_ready["ota"]["ready"] is True
    assert recovery_ready["ota"]["blocked"] == []
    assert recovery_ready["recovery"]["status"] == "mixed_firmware"


def test_ota_readiness_blocks_when_no_placed_performer_is_online() -> None:
    conductor = MockConductor()
    for lantern in conductor._lanterns:
        if lantern.x is not None and lantern.y is not None:
            lantern.status = "missing"

    conductor.set_ota_mode(True)
    ota = conductor.snapshot()["ota"]

    assert ota["ready"] is False
    assert ota["ready_count"] == 0
    assert ota["deferred"] == ota["expected"]
    assert ota["blocked"] == ["no placed lanterns online"]


def test_recovery_summary_reports_failed_ota_node() -> None:
    conductor = MockConductor()
    conductor._ota_nodes = {
        "8C:94:DF:8F:71:50": {
            "mac": "8C:94:DF:8F:71:50",
            "phase": "failed",
            "error": "chunk offset mismatch",
            "offset": 200,
            "crc32": 0,
            "last_seen_s": 1,
        }
    }

    recovery = conductor.snapshot()["recovery"]

    assert recovery["status"] == "ota_failed"
    assert recovery["failed_ota"] == [
        {
            "mac": "8C:94:DF:8F:71:50",
            "label": "#1",
            "reason": "chunk offset mismatch",
            "phase": "failed",
        }
    ]


def test_power_policy_force_awake_overrides_off_window() -> None:
    conductor = MockConductor()

    conductor.update_power_policy({
        "light_sleep_check_s": 20,
        "deep_sleep_check_min": 45,
        "led_on_start_min": 18 * 60,
        "led_on_end_min": 6 * 60,
        "schedule_enabled": True,
        "force_awake": False,
        "force_sleep": False,
        "current_min": 12 * 60,
        "current_epoch_s": 1_720_123_400,
    })
    assert conductor.snapshot()["power"]["leds_on"] is False

    conductor.update_power_policy({
        "light_sleep_check_s": 20,
        "deep_sleep_check_min": 45,
        "led_on_start_min": 18 * 60,
        "led_on_end_min": 6 * 60,
        "schedule_enabled": True,
        "force_awake": True,
        "force_sleep": False,
        "current_min": 12 * 60,
        "current_epoch_s": 1_720_123_400,
    })
    snapshot = conductor.snapshot()
    assert snapshot["power"]["force_awake"] is True
    assert snapshot["power"]["leds_on"] is True
    assert snapshot["conductor"]["wake"] is True


def test_power_policy_force_sleep_overrides_disabled_schedule() -> None:
    conductor = MockConductor()

    conductor.update_power_policy({
        "schedule_enabled": False,
        "force_awake": False,
        "force_sleep": True,
    })
    snapshot = conductor.snapshot()

    assert snapshot["power"]["force_sleep"] is True
    assert snapshot["power"]["leds_on"] is False
    assert snapshot["conductor"]["wake"] is False


def test_assign_sets_position_and_clears_attention() -> None:
    conductor = MockConductor()
    mac = "8C:94:DF:57:7F:14"

    ack = conductor.assign(mac, 0.25, 0.75)
    lantern = next(item for item in conductor.lanterns() if item["mac"] == mac)

    assert ack["ok"] is True
    assert lantern["position"] == "Set"
    assert lantern["attention"] == "None"
    assert lantern["x"] == 0.25
    assert lantern["y"] == 0.75


def test_blackout_preserves_pattern_and_sets_brightness_zero() -> None:
    conductor = MockConductor()

    conductor.update_pattern("Sweep", 72, {"period": 8000})
    ack = conductor.blackout()

    assert ack["ok"] is True
    assert conductor.snapshot()["pattern"] == {
        "pattern": "Sweep",
        "brightness": 0,
        "params": {"period": 8000},
    }


def test_replace_moves_only_position_to_unpositioned_spare() -> None:
    conductor = MockConductor()
    old_mac = "A0:B7:65:11:44:91"
    new_mac = "8C:94:DF:57:7F:14"

    ack = conductor.replace(old_mac, new_mac)
    lanterns = conductor.lanterns()
    old = next(item for item in lanterns if item["mac"] == old_mac)
    new = next(item for item in lanterns if item["mac"] == new_mac)

    assert ack["ok"] is True
    assert old["position"] == "Missing"
    assert old["label"] == "#18"
    assert old["status"] == "missing"
    assert old["attention"] == "Not seen"
    assert new["position"] == "Set"
    assert new["label"] == "#57"
    assert new["x"] == 0.66
    assert new["y"] == 0.69


def test_replace_rejects_positioned_replacement() -> None:
    conductor = MockConductor()

    ack = conductor.replace("A0:B7:65:11:44:91", "30:76:F5:93:67:3C")

    assert ack == {"ok": False, "error": "replacement lantern already has a position"}


def test_replace_rejects_unpositioned_old_lantern() -> None:
    conductor = MockConductor()

    ack = conductor.replace("8C:94:DF:57:7F:14", "A0:B7:65:11:44:91")

    assert ack == {"ok": False, "error": "old lantern has no position to replace"}


def test_reserve_id_is_stable_and_uses_lowest_unused_number() -> None:
    conductor = MockConductor()
    mac = "AA:BB:CC:DD:EE:FF"

    created = conductor.reserve_id(mac)
    repeated = conductor.reserve_id(mac)

    used = {item.node_id for item in conductor._lanterns if item.mac != mac and item.node_id}
    assert created["node_id"] == next(candidate for candidate in range(1, 65536) if candidate not in used)
    assert created["created"] is True
    assert repeated == {
        "ok": True,
        "message": "permanent ID already reserved",
        "node_id": created["node_id"],
        "created": False,
    }


def test_reserve_id_adopts_unclaimed_reported_id_and_rejects_conflict() -> None:
    conductor = MockConductor()
    first = conductor.reserve_id("AA:BB:CC:DD:EE:01", 53)
    conflict = conductor.reserve_id("AA:BB:CC:DD:EE:02", 53)

    assert first["node_id"] == 53
    assert first["created"] is True
    assert conflict == {"ok": False, "error": "permanent ID conflict"}
