from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from labctl import checks
from labctl.bundle import Desired
from labctl.checks import (
    DOCKER,
    Finding,
    Level,
    PathInfo,
    archive_path,
    check_paths,
    human_size,
    key_label,
    parse_free,
    parse_probe,
    parse_size,
    parse_volumes,
    space_findings,
    space_needed,
)
from labctl.compose import reduce_config
from labctl.envfile import CompiledEnv
from labctl.moves import APPDATA, DATA, Move
from labctl.planner import Add, Migrate, Restore, Update
from labctl.remote import Remote
from labctl.state import NodeState, Service

ROOT_A = "/home/paul/docker/app_data"
ROOT_B = "/srv/appdata"

MOUNTS: list[dict[str, Any]] = [
    {"type": "bind", "source": "{root}/plexms", "target": "/config"},
    {"type": "bind", "source": "/mnt/media/TV", "target": "/data/tv"},
    {"type": "bind", "source": "/var/run/docker.sock", "target": "/sock"},
]


def desired(node: str, root: str) -> Desired:
    volumes = [{**m, "source": m["source"].format(root=root)} for m in MOUNTS]
    config = {"services": {"plex": {"volumes": volumes}}}
    env = CompiledEnv(values={"DEPLOY_DIR": "/x", "APPDATA_DIR": root})
    return Desired(node, Path("/unused"), env, {"": config}, reduce_config(node, config, root), {})


DESIRED = {"frodo": desired("frodo", ROOT_A), "cirdan": desired("cirdan", ROOT_B)}
RECORDED: dict[str, NodeState | None] = {
    "frodo": DESIRED["frodo"].state,
    "cirdan": NodeState.of("cirdan"),
}

WHEN = datetime(2026, 3, 2, tzinfo=UTC)
PLEX_MOVE = Migrate(("plexms",), "frodo", "cirdan", ("plex",), ("plex",))
OUTSIDE_OK = {"/mnt/media/TV": PathInfo("dir"), "/var/run/docker.sock": PathInfo("other")}


@dataclass
class FakeProbe:
    """Per-node probe answers (unknown paths are missing), plus what was asked."""

    answers: dict[str, dict[str, PathInfo]] = field(
        default_factory=lambda: {"frodo": {}, "cirdan": {}}
    )
    asked: dict[str, list[tuple[str, str]]] = field(default_factory=dict)

    def __getitem__(self, node: str) -> dict[str, PathInfo]:
        return self.answers[node]


@pytest.fixture
def fake_probe(monkeypatch: pytest.MonkeyPatch) -> FakeProbe:
    fake = FakeProbe()

    def probe(remote: Remote, paths: Sequence[tuple[str, str]]) -> dict[str, PathInfo]:
        fake.asked[remote.host] = list(paths)
        return {p: fake.answers[remote.host].get(p, PathInfo("missing")) for p, mode in paths}

    monkeypatch.setattr(checks, "probe", probe)
    monkeypatch.setattr(checks, "probe_free", lambda remote, paths: {})  # space not judged
    return fake


def messages(findings: list[Finding]) -> list[tuple[Level, str, str]]:
    return [(f.level, f.node, f.message) for f in findings]


def test_migrate_probes_source_with_sizes_and_target(fake_probe: FakeProbe) -> None:
    fake_probe["frodo"][f"{ROOT_A}/plexms"] = PathInfo("dir", 42 * 1024**3, WHEN)
    fake_probe["cirdan"].update(OUTSIDE_OK)

    findings, results = check_paths([PLEX_MOVE], DESIRED, RECORDED)
    assert findings == []
    assert fake_probe.asked["frodo"] == [(f"{ROOT_A}/plexms", "S")]
    assert fake_probe.asked["cirdan"] == [
        ("/mnt/media/TV", "-"),
        (f"{ROOT_B}/plexms", "S"),
        ("/var/run/docker.sock", "-"),
    ]
    assert results["frodo"][f"{ROOT_A}/plexms"].size == 42 * 1024**3


def test_migrate_warns_about_missing_paths(fake_probe: FakeProbe) -> None:
    findings, _ = check_paths([PLEX_MOVE], DESIRED, RECORDED)
    nothing_creates = "does not exist; nothing creates it (Docker would make an empty root-owned "
    assert messages(findings) == [
        (Level.WARN, "cirdan", f"/mnt/media/TV {nothing_creates}directory)"),
        (Level.WARN, "cirdan", f"/var/run/docker.sock {nothing_creates}directory)"),
        (Level.WARN, "frodo", f"{ROOT_A}/plexms is missing on the source: nothing to copy"),
    ]


def test_existing_target_data_blocks_without_replace_stale(fake_probe: FakeProbe) -> None:
    fake_probe["frodo"][f"{ROOT_A}/plexms"] = PathInfo("dir", 10, WHEN)
    fake_probe["cirdan"][f"{ROOT_B}/plexms"] = PathInfo("dir", 5 * 1024**2, WHEN)
    fake_probe["cirdan"].update(OUTSIDE_OK)

    findings, _ = check_paths([PLEX_MOVE], DESIRED, RECORDED)
    assert [(f.level, f.flag) for f in findings] == [(Level.BLOCK, "--replace-stale")]
    assert findings[0].message == (
        f"{ROOT_B}/plexms already exists on the target (directory, 5.0 MB, last modified "
        f"2026-03-02); it will be archived to {ROOT_B}/.labctl/archive/plexms-<timestamp> before "
        "the copy"
    )

    findings, _ = check_paths([PLEX_MOVE], DESIRED, RECORDED, replace_stale=True)
    assert [(f.level, f.flag) for f in findings] == [(Level.INFO, "")]


def test_empty_target_dir_is_treated_as_missing(fake_probe: FakeProbe) -> None:
    fake_probe["frodo"][f"{ROOT_A}/plexms"] = PathInfo("dir", 10, WHEN)
    fake_probe["cirdan"][f"{ROOT_B}/plexms"] = PathInfo("empty")
    fake_probe["cirdan"].update(OUTSIDE_OK)
    findings, _ = check_paths([PLEX_MOVE], DESIRED, RECORDED)
    assert findings == []


def test_add_reuses_existing_data(fake_probe: FakeProbe) -> None:
    fake_probe["cirdan"][f"{ROOT_B}/plexms"] = PathInfo("dir", 2048, WHEN)
    fake_probe["cirdan"].update(OUTSIDE_OK)
    findings, _ = check_paths([Add("plex", "cirdan")], DESIRED, RECORDED)
    assert messages(findings) == [
        (
            Level.INFO,
            "cirdan",
            f"{ROOT_B}/plexms already has data (directory, 2.0 KB, last modified 2026-03-02); "
            "it will be used",
        ),
    ]


def test_update_checks_only_outside_paths_without_sizes(fake_probe: FakeProbe) -> None:
    check_paths([Update("plex", "frodo")], DESIRED, RECORDED)
    assert fake_probe.asked["frodo"] == [("/mnt/media/TV", "-"), ("/var/run/docker.sock", "-")]


def test_paths_the_ssh_user_cannot_see_are_not_reported_missing(fake_probe: FakeProbe) -> None:
    """e.g. /var/lib/docker/volumes under root-only /var/lib/docker (portainer's bind mount)."""
    fake_probe["frodo"][f"{ROOT_A}/plexms"] = PathInfo("dir", 10, WHEN)
    fake_probe["cirdan"]["/mnt/media/TV"] = PathInfo("unreadable")
    fake_probe["cirdan"]["/var/run/docker.sock"] = PathInfo("other")
    findings, _ = check_paths([PLEX_MOVE], DESIRED, RECORDED)
    assert findings == []


def test_parse_probe() -> None:
    output = "/a\tdir\t2048\t1772409600\t7\n/b\tmissing\t\t\t\n/c\tfile\t5\t\t\n"
    assert parse_probe(output) == {
        "/a": PathInfo("dir", 2048, datetime(2026, 3, 2, tzinfo=UTC), device="7"),
        "/b": PathInfo("missing"),
        "/c": PathInfo("file", 5),
    }


def test_human_size() -> None:
    assert human_size(512) == "512 B"
    assert human_size(42 * 1024**3) == "42.0 GB"
    assert human_size(3 * 1024**4) == "3.0 TB"


ROOT_MOVE = Move("cirdan", APPDATA, "/old/appdata", ROOT_B, "/old/appdata", ROOT_B)


def test_paths_are_probed_where_they_are_before_the_moves(fake_probe: FakeProbe) -> None:
    fake_probe["cirdan"]["/old/appdata/plexms"] = PathInfo("dir", 2048, WHEN)
    fake_probe["cirdan"].update(OUTSIDE_OK)
    findings, _ = check_paths([Add("plex", "cirdan")], DESIRED, RECORDED, moves=[ROOT_MOVE])
    asked = dict(fake_probe.asked["cirdan"])
    assert asked["/old/appdata"] == "S" and asked[ROOT_B] == "S"
    assert asked["/old/appdata/plexms"] == "S"
    assert f"{ROOT_B}/plexms" not in asked
    # Findings still name the post-move path.
    assert any(f.message.startswith(f"{ROOT_B}/plexms already has data") for f in findings)


def test_root_move_onto_existing_data_blocks(fake_probe: FakeProbe) -> None:
    fake_probe["cirdan"]["/old/appdata"] = PathInfo("dir", 10, WHEN, device="1")
    fake_probe["cirdan"][ROOT_B] = PathInfo("dir", 10, WHEN, device="1")
    findings, _ = check_paths([], DESIRED, RECORDED, replace_stale=True, moves=[ROOT_MOVE])
    assert [(f.level, f.message.split(";")[0]) for f in findings] == [
        (
            Level.BLOCK,
            f"APPDATA_DIR target {ROOT_B} already exists (directory, 10 B, last modified "
            "2026-03-02)",
        )
    ]


def test_rename_onto_existing_data_needs_replace_stale(fake_probe: FakeProbe) -> None:
    rename = Move("cirdan", DATA, "plexms", "plex", f"{ROOT_B}/plexms", f"{ROOT_B}/plex", ("plex",))
    fake_probe["cirdan"][f"{ROOT_B}/plexms"] = PathInfo("dir", 10, WHEN)
    fake_probe["cirdan"][f"{ROOT_B}/plex"] = PathInfo("dir", 10, WHEN)
    findings, _ = check_paths([], DESIRED, RECORDED, moves=[rename])
    assert [(f.level, f.flag) for f in findings] == [(Level.BLOCK, "--replace-stale")]
    assert f"archived to {ROOT_B}/.labctl/archive/plex-<timestamp>" in findings[0].message
    findings, _ = check_paths([], DESIRED, RECORDED, replace_stale=True, moves=[rename])
    assert [f.level for f in findings] == [Level.INFO]


def test_missing_move_source_warns(fake_probe: FakeProbe) -> None:
    findings, _ = check_paths([], DESIRED, RECORDED, moves=[ROOT_MOVE])
    assert messages(findings) == [
        (Level.WARN, "cirdan", "/old/appdata is missing: nothing to move")
    ]


def test_renamed_migrate_probes_the_source_under_its_own_name(fake_probe: FakeProbe) -> None:
    renamed_desired = {
        **DESIRED,
        "cirdan": desired("cirdan", ROOT_B),
    }
    # cirdan wants the data as "plex"; frodo recorded it (rewritten) as "plex" too, alias plexms.
    config = {
        "services": {
            "plex": {"volumes": [{"type": "bind", "source": f"{ROOT_B}/plex", "target": "/config"}]}
        }
    }
    env = CompiledEnv(values={"DEPLOY_DIR": "/x", "APPDATA_DIR": ROOT_B})
    renamed_desired["cirdan"] = Desired(
        "cirdan", Path("/unused"), env, {"": config}, reduce_config("cirdan", config, ROOT_B), {}
    )
    recorded = {
        "frodo": NodeState.of("frodo", Service("plex", data_paths=("plex",))),
        "cirdan": NodeState.of("cirdan"),
    }
    move = Migrate(("plex",), "frodo", "cirdan", ("plex",), ("plex",), (("plex", "plexms"),))
    fake_probe["frodo"][f"{ROOT_A}/plexms"] = PathInfo("dir", 10, WHEN)
    findings, _ = check_paths([move], renamed_desired, recorded)
    assert fake_probe.asked["frodo"] == [(f"{ROOT_A}/plexms", "S")]
    assert (f"{ROOT_B}/plex", "S") in fake_probe.asked["cirdan"]
    assert findings == []


def test_volume_probe_parses_docker_output() -> None:
    usage = {
        "Volumes": [
            {"Name": "homelab-frodo_db", "Size": "1.049MB"},
            {"Name": "homelab-frodo_empty", "Size": "0B"},
        ]
    }
    output = "homelab-frodo_db\nhomelab-frodo_empty\n--\n" + json.dumps(usage)
    keys = ["volume:homelab-frodo_db", "volume:homelab-frodo_empty", "volume:gone"]
    assert parse_volumes(output, keys) == {
        "volume:homelab-frodo_db": PathInfo("dir", size=1_049_000),
        "volume:homelab-frodo_empty": PathInfo("empty", size=0),
        "volume:gone": PathInfo("missing"),
    }
    assert parse_volumes("--\n{}", ["volume:x"]) == {"volume:x": PathInfo("missing")}


@pytest.mark.parametrize(
    ("text", "size"),
    [("0B", 0), ("3B", 3), ("12.3kB", 12_300), ("2GB", 2 * 10**9), ("N/A", None)],
)
def test_docker_sizes(text: str, size: int | None) -> None:
    assert parse_size(text) == size


def test_archive_names_and_labels() -> None:
    assert archive_path("/srv/a", "plex") == "/srv/a/.labctl/archive/plex"
    assert (
        archive_path("/srv/a", "volume:homelab-frodo_db")
        == "/srv/a/.labctl/archive/volume-homelab-frodo_db"
    )
    assert key_label("plex") == "plex/"
    assert key_label("volume:homelab-frodo_db") == "volume homelab-frodo_db"


# ── Free space ─────────────────────────────────────────────────────────────────


def test_what_the_copies_need_on_each_node() -> None:
    want = {"a": desired("a", ROOT_B), "b": desired("b", ROOT_B)}
    have: dict[str, NodeState | None] = {
        "a": NodeState.of("a", Service("plex", ("plexms", "plexms/cache"), ("vol",), "p"))
    }
    plex = Migrate(("plexms", "volume:vol"), "a", "b", ("plex",), ("plex",))
    restore = Restore("sonarr", "b", "a", "x", ROOT_B, (("sonarr-x", "sonarr"),), size=300)
    same_node = Restore("radarr", "a", "a", "x", ROOT_B, (("radarr-x", "radarr"),), size=9)
    planned = {
        "a": {
            f"{ROOT_B}/plexms": PathInfo("dir", size=5000),
            f"{ROOT_B}/plexms/cache": PathInfo("dir", size=4000),  # inside plexms: counted once
            "volume:vol": PathInfo("dir", size=70),
        }
    }
    across = Move("b", APPDATA, ROOT_B, "/big/appdata", ROOT_B, "/big/appdata")
    results = {
        "b": {
            ROOT_B: PathInfo("dir", size=800, device="1"),
            "/big/appdata": PathInfo("missing", device="2"),  # another filesystem: a copy
        }
    }
    needed = space_needed([plex, restore, same_node], [across], want, have, results, planned)
    assert needed == {
        "b": {
            ROOT_B: (5300, ("plex", "sonarr")),
            DOCKER: (70, ("plex",)),
            "/big/appdata": (800, (f"APPDATA_DIR {ROOT_B} → /big/appdata",)),
        }
    }
    results["b"]["/big/appdata"] = PathInfo("missing", device="1")  # one filesystem: a rename
    assert "/big/appdata" not in space_needed([], [across], want, have, results, planned).get(
        "b", {}
    )


def test_copies_onto_one_filesystem_add_up() -> None:
    free = parse_free("/srv/appdata\t1000\t/srv\n@docker\t1000\t/srv\n/big\t50\t/big\nbad\n")
    destinations = {
        "/srv/appdata": (600, ("plex",)),
        DOCKER: (500, ("db",)),
        "/big": (10, ("x",)),
        "/unread": (10**12, ("y",)),  # its space could not be read: not judged
    }
    (finding,) = space_findings("b", destinations, free)
    assert finding.level is Level.BLOCK and finding.service == "plex, db"
    assert (
        finding.message == "not enough free space on /srv: the copies need 1.1 KB, 1000 B is free"
    )


def test_a_migrate_that_does_not_fit_blocks(
    fake_probe: FakeProbe, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_probe["frodo"][f"{ROOT_A}/plexms"] = PathInfo("dir", size=2 * 1024**3)
    monkeypatch.setattr(
        checks, "probe_free", lambda remote, paths: {p: (1024**3, "/srv") for p in paths}
    )
    findings, _ = check_paths([PLEX_MOVE], DESIRED, RECORDED)
    assert (
        Level.BLOCK,
        "cirdan",
        "not enough free space on /srv: the copies need 2.0 GB, 1.0 GB is free",
    ) in messages(findings)
