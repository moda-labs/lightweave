from control.mock_conductor import MockConductor


def test_snapshot_counts_healthy_placed_over_placed_total() -> None:
    conductor = MockConductor()

    snapshot = conductor.snapshot()

    assert snapshot["summary"]["alive"] == 8
    assert snapshot["summary"]["total"] == 9
    assert snapshot["summary"]["attention"] == 2
    assert snapshot["pattern"]["pattern"] == "Glow"
    assert len(snapshot["patterns"]) == 8
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


def test_blackout_can_restore_distinct_group_brightness_values() -> None:
    conductor = MockConductor()

    conductor.update_pattern("White", 24, {}, group_id=0)
    conductor.update_pattern("Fire Flicker", 56, {"period": 1200}, group_id=1)
    blackout_ack = conductor.blackout()
    # A repeated emergency click must not replace the saved values with zeroes.
    conductor.blackout()
    blacked_out = conductor.snapshot()
    restore_ack = conductor.restore_blackout()
    restored = conductor.snapshot()

    assert blackout_ack["ok"] is True
    assert blacked_out["blackout"] == {"restore_available": True}
    assert [entry["config"]["brightness"] for entry in blacked_out["patterns"][:2]] == [0, 0]
    assert restore_ack["ok"] is True
    assert restored["blackout"] == {"restore_available": False}
    assert [entry["config"]["brightness"] for entry in restored["patterns"][:2]] == [24, 56]
    assert restored["patterns"][0]["config"]["pattern"] == "White"
    assert restored["patterns"][1]["config"]["pattern"] == "Fire Flicker"


def test_groups_keep_independent_patterns_and_membership() -> None:
    conductor = MockConductor()
    mac = conductor._lanterns[0].mac

    group_ack = conductor.assign_group(mac, 2)
    pattern_ack = conductor.update_pattern("Sweep", 72, {"period": 8000}, group_id=2)
    snapshot = conductor.snapshot()

    lantern = next(item for item in snapshot["lanterns"] if item["mac"] == mac)
    assert group_ack["ok"] is True
    assert pattern_ack["ok"] is True
    assert lantern["group_id"] == 2
    assert snapshot["patterns"][2]["config"]["pattern"] == "Sweep"
    assert snapshot["patterns"][0]["config"]["pattern"] == "Glow"


def test_pattern_update_without_group_targets_every_group() -> None:
    conductor = MockConductor()

    ack = conductor.update_pattern("White", 36, {})
    patterns = conductor.snapshot()["patterns"]

    assert ack["ok"] is True
    expected = {"pattern": "White", "brightness": 36, "params": {}}
    assert all(item["config"] == expected for item in patterns)


def test_unpositioned_lantern_keeps_group_before_and_after_placement_changes() -> None:
    conductor = MockConductor()
    mac = "8C:94:DF:57:7F:14"

    grouped = conductor.assign_group(mac, 4)
    before = next(item for item in conductor.lanterns() if item["mac"] == mac)
    conductor.assign(mac, 0.2, 0.3)
    conductor.forget(mac)
    after = next(item for item in conductor.lanterns() if item["mac"] == mac)

    assert grouped["ok"] is True
    assert before["position"] == "Missing"
    assert before["group_id"] == 4
    assert after["position"] == "Missing"
    assert after["group_id"] == 4


def test_led_count_profile_is_board_specific_and_survives_placement_changes() -> None:
    conductor = MockConductor()
    mac = "8C:94:DF:57:7F:14"

    assert conductor.assign_led_count(mac, 64)["ok"] is True
    assert conductor.assign_led_count(mac, 24)["ok"] is False
    conductor.assign(mac, 0.2, 0.3)
    conductor.forget(mac)
    lantern = next(item for item in conductor.lanterns() if item["mac"] == mac)

    assert lantern["position"] == "Missing"
    assert lantern["led_count"] == 64


def test_replace_moves_position_and_group_but_keeps_each_boards_led_profile() -> None:
    conductor = MockConductor()
    old_mac = "A0:B7:65:11:44:91"
    new_mac = "8C:94:DF:57:7F:14"

    assert conductor.assign_group(old_mac, 2)["ok"] is True
    assert conductor.assign_led_count(old_mac, 64)["ok"] is True
    assert conductor.assign_led_count(new_mac, 32)["ok"] is True
    ack = conductor.replace(old_mac, new_mac)
    lanterns = conductor.lanterns()
    old = next(item for item in lanterns if item["mac"] == old_mac)
    new = next(item for item in lanterns if item["mac"] == new_mac)

    assert ack["ok"] is True
    assert old["position"] == "Missing"
    assert old["label"] == "#18"
    assert old["status"] == "missing"
    assert old["attention"] == "Not seen"
    assert old["group_id"] == 2
    assert old["led_count"] == 64
    assert new["position"] == "Set"
    assert new["label"] == "#57"
    assert new["group_id"] == 2
    assert new["led_count"] == 32
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
