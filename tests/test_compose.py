from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from labctl.compose import ComposeError, canonical, reduce_config

FIXTURES = Path(__file__).parent / "fixtures"
CIRDAN_APPDATA = "/home/paul/docker/app_data"


@pytest.fixture
def cirdan_config() -> dict[str, Any]:
    config: dict[str, Any] = json.loads((FIXTURES / "cirdan" / "config.json").read_text())
    return config


def test_reduce_cirdan(cirdan_config: dict[str, Any]) -> None:
    state = reduce_config("cirdan", cirdan_config, CIRDAN_APPDATA)
    assert state.node == "cirdan"
    assert list(state.services) == ["dockerproxy", "dozzle-agent", "fileflows", "portainer-agent"]

    fileflows = state.services["fileflows"]
    # logs, media and docker.sock are outside the managed root: node-local, not data.
    assert fileflows.data_paths == ("fileflows/data",)
    assert fileflows.stateful

    for stateless in ("dockerproxy", "dozzle-agent", "portainer-agent"):
        assert not state.services[stateless].stateful, stateless


def test_fingerprint_changes_only_for_the_edited_service(cirdan_config: dict[str, Any]) -> None:
    before = reduce_config("cirdan", cirdan_config, CIRDAN_APPDATA)
    cirdan_config["services"]["fileflows"]["image"] = "revenz/fileflows:25.1"
    after = reduce_config("cirdan", cirdan_config, CIRDAN_APPDATA)

    changed = [s for s in before.services if before.services[s] != after.services[s]]
    assert changed == ["fileflows"]


def test_fingerprint_ignores_key_order(cirdan_config: dict[str, Any]) -> None:
    before = reduce_config("cirdan", cirdan_config, CIRDAN_APPDATA)
    reordered = json.loads(json.dumps(cirdan_config), object_pairs_hook=lambda p: dict(reversed(p)))
    assert reduce_config("cirdan", reordered, CIRDAN_APPDATA) == before


def test_named_volume_makes_a_service_stateful() -> None:
    config = {
        "services": {
            "db": {
                "volumes": [
                    {"type": "volume", "source": "pgdata", "target": "/var/lib/postgresql/data"}
                ]
            }
        }
    }
    db = reduce_config("cirdan", config, CIRDAN_APPDATA).services["db"]
    assert db.volumes == ("pgdata",)
    assert db.stateful


def test_volumes_by_real_name_and_kind() -> None:
    """Compose gives every top-level volume its real name (project-prefixed unless fixed)."""
    config = {
        "name": "homelab-frodo",
        "services": {
            "app": {
                "volumes": [
                    {"type": "volume", "source": "data", "target": "/data"},
                    {"type": "volume", "source": "shared", "target": "/shared"},
                    {"type": "volume", "source": "disguised", "target": "/cfg"},
                    {"type": "volume", "source": "outside", "target": "/media"},
                    {"type": "volume", "source": "nas", "target": "/nas"},
                    {"type": "volume", "target": "/cache"},
                ]
            }
        },
        "volumes": {
            "data": {"name": "homelab-frodo_data"},
            "shared": {"name": "shared-data", "external": True},
            "disguised": {
                "name": "homelab-frodo_disguised",
                "driver": "local",
                "driver_opts": {"type": "none", "o": "bind", "device": f"{CIRDAN_APPDATA}/app"},
            },
            "outside": {
                "name": "homelab-frodo_outside",
                "driver_opts": {"type": "none", "o": "bind", "device": "/media/tv"},
            },
            "nas": {
                "name": "homelab-frodo_nas",
                "driver_opts": {"type": "nfs", "o": "addr=10.0.0.5", "device": ":/export"},
            },
        },
    }
    state = reduce_config("frodo", config, CIRDAN_APPDATA)
    app = state.services["app"]
    assert app.volumes == ("homelab-frodo_data", "shared-data")
    assert app.data_paths == ("app",)  # the bind in disguise, under APPDATA_DIR
    assert app.mounts == (
        ("/data", "volume:homelab-frodo_data"),
        ("/shared", "volume:shared-data"),
        ("/cfg", "app"),
    )
    assert app.anonymous == ("/cache",)
    assert app.remote == (("homelab-frodo_nas", "nfs 10.0.0.5:/export"),)  # never copied
    assert app.data_keys == ("app", "volume:homelab-frodo_data", "volume:shared-data")
    assert state.volumes["shared-data"].external
    assert state.volumes["homelab-frodo_data"].key == "data"
    assert set(state.volumes) == {"homelab-frodo_data", "shared-data"}


def test_mounting_appdata_dir_itself_is_rejected() -> None:
    config = {"services": {"x": {"volumes": [{"type": "bind", "source": CIRDAN_APPDATA}]}}}
    with pytest.raises(ComposeError, match="mounts APPDATA_DIR .* itself"):
        reduce_config("cirdan", config, CIRDAN_APPDATA)


def test_the_archive_folder_is_reserved() -> None:
    source = f"{CIRDAN_APPDATA}/archive/plexms-20260101"
    config = {"services": {"x": {"volumes": [{"type": "bind", "source": source}]}}}
    with pytest.raises(ComposeError, match="reserved"):
        reduce_config("cirdan", config, CIRDAN_APPDATA)


def test_networks_by_real_name_with_their_services() -> None:
    config = {
        "name": "homelab-bilbo",
        "services": {
            "npm": {"image": "npm", "networks": {"reverse_proxy": None, "default": None}},
            "app": {"image": "app", "networks": {"default": None}},
            "host": {"image": "h", "network_mode": "host"},
        },
        "networks": {
            "reverse_proxy": {"name": "reverse_proxy", "driver": "bridge", "ipam": {}},
            "default": {"name": "homelab-bilbo_default"},
        },
    }
    state = reduce_config("bilbo", config, CIRDAN_APPDATA)
    proxy, default = state.networks["reverse_proxy"], state.networks["homelab-bilbo_default"]
    assert (proxy.key, proxy.stack, proxy.services) == ("reverse_proxy", "homelab-bilbo", ("npm",))
    assert proxy.config == '{"driver":"bridge","ipam":{}}'  # the name is the key it is found by
    assert default.services == ("app", "npm")
    assert state.services["npm"].config == canonical(config["services"]["npm"])
