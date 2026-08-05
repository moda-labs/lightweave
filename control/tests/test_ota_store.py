import json

from control.ota_store import OtaInstallStore, PersistentOtaInstall


def test_persistent_ota_install_survives_process_recreation(tmp_path) -> None:
    store = OtaInstallStore(tmp_path)
    install = PersistentOtaInstall(store, store.load())

    install.reset({"running": True, "phase": "broadcasting", "bytes_sent": 256})
    install.update({"phase": "repairing", "repair_chunks": 2})

    restored = OtaInstallStore(tmp_path).load()
    assert restored == {
        "running": True,
        "phase": "repairing",
        "bytes_sent": 256,
        "repair_chunks": 2,
    }


def test_ota_install_store_ignores_invalid_or_non_object_state(tmp_path) -> None:
    store = OtaInstallStore(tmp_path)
    store.path.write_text("not json", encoding="utf-8")
    assert store.load() == {"running": False, "complete": False, "error": None}

    store.path.write_text(json.dumps(["unexpected"]), encoding="utf-8")
    assert store.load() == {"running": False, "complete": False, "error": None}
