from __future__ import annotations

import json
from collections.abc import Sequence

from labctl.planner import Add, Plan, Restore
from labctl.restore import Archived, Copy, copies_for, parse_archives, plan_restores, restored_key
from labctl.state import NodeState, Service

ROOT = "/srv/appdata"


def archived(
    node: str,
    entry: str,
    key: str,
    mounts: Sequence[tuple[str, str, str]],  # (service, target, key)
    stamp: str = "20261101-090000",
    reason: str = "removed",
    size: int | None = 10,
) -> Archived:
    data = {
        "stamp": stamp,
        "reason": reason,
        "key": key,
        "mounts": [{"service": s, "stack": "x", "target": t, "key": k} for s, t, k in mounts],
    }
    return Archived(node, ROOT, entry, size, data)


def pg(node: str) -> NodeState:
    """pg wants its database in the node's own project volume."""
    name = f"homelab-{node}_db"
    return NodeState.of(
        node, Service("pg", volumes=(name,), mounts=(("/var/lib/pg", f"volume:{name}"),))
    )


def test_sidecars_are_listed_with_their_entry_and_size() -> None:
    data = {"stamp": "20261101-090000", "key": "plex"}
    output = f"plex-20261101-090000\t1234\t{json.dumps(data)}\nbroken\t\tnot json\n"
    (item,) = parse_archives("frodo", ROOT, output)
    assert (item.node, item.entry, item.size, item.stamp) == (
        "frodo",
        "plex-20261101-090000",
        1234,
        "20261101-090000",
    )


def test_a_volume_restores_into_the_target_s_own_volume() -> None:
    frodo_db = archived(
        "frodo",
        "volume-homelab-frodo_db-20261101-090000",
        "volume:homelab-frodo_db",
        [("pg", "/var/lib/pg", "volume:homelab-frodo_db")],
    )
    (copy,) = copies_for("bilbo", "pg", pg("bilbo"), [frodo_db])
    assert copy == Copy(
        "frodo",
        "20261101-090000",
        "removed",
        ROOT,
        (("volume-homelab-frodo_db-20261101-090000", "volume:homelab-bilbo_db"),),
        10,
    )


def test_newest_first_and_failed_copies_left_out() -> None:
    def copy(stamp: str, reason: str = "removed") -> Archived:
        return archived(
            "frodo",
            f"volume-homelab-frodo_db-{stamp}",
            "volume:homelab-frodo_db",
            [("pg", "/var/lib/pg", "volume:homelab-frodo_db")],
            stamp=stamp,
            reason=reason,
        )

    found = [copy("20261101-090000"), copy("20261103-090000"), copy("20261104-090000", "failed")]
    assert [c.stamp for c in copies_for("bilbo", "pg", pg("bilbo"), found)] == [
        "20261103-090000",
        "20261101-090000",
    ]


def test_another_service_s_data_is_not_offered() -> None:
    other = archived("frodo", "x-1", "x", [("sonarr", "/var/lib/pg", "x")])
    assert copies_for("bilbo", "pg", pg("bilbo"), [other]) == []


def test_a_directory_restores_under_its_new_name() -> None:
    assert restored_key("plexms", "plexms", "plex") == "plex"
    assert restored_key("plexms", "plexms/config", "plex/config") == "plex"
    assert restored_key("plexms", "plexms/config", "plex/settings") is None  # layout changed
    assert restored_key("volume:a_db", "volume:a_db", "db") == "db"  # a volume into a directory


def adding(node: str, service: str = "pg") -> Plan:
    return Plan(operations=(Add(service, node),))


FRODO_DB = archived(
    "frodo",
    "volume-homelab-frodo_db-20261101-090000",
    "volume:homelab-frodo_db",
    [("pg", "/var/lib/pg", "volume:homelab-frodo_db")],
)
RESTORE = Restore(
    "pg",
    "bilbo",
    "frodo",
    "20261101-090000",
    ROOT,
    (("volume-homelab-frodo_db-20261101-090000", "volume:homelab-bilbo_db"),),
    "removed",
    10,
)


def test_the_newest_copy_is_restored_by_default_and_said_so() -> None:
    result = plan_restores(adding("bilbo"), {"bilbo": pg("bilbo")}, [FRODO_DB])
    assert result.operations == (RESTORE,)
    assert result.chosen == ("pg@20261101-090000",)
    assert "restoring the newest copy" in result.notes[0] and "--fresh pg" in result.notes[0]


def test_fresh_overrides_and_keeps_the_archive() -> None:
    result = plan_restores(adding("bilbo"), {"bilbo": pg("bilbo")}, [FRODO_DB], fresh=["pg"])
    assert result.operations == () and result.fresh == ("pg",)
    assert "starts empty (1 archived copy kept)" in result.notes[0]


def test_a_stamp_picks_the_copy_and_a_wrong_one_is_an_error() -> None:
    picked = plan_restores(
        adding("bilbo"), {"bilbo": pg("bilbo")}, [FRODO_DB], restore=["pg@20261101-090000"]
    )
    assert picked.operations == (RESTORE,) and picked.notes == ()
    wrong = plan_restores(adding("bilbo"), {"bilbo": pg("bilbo")}, [FRODO_DB], restore=["pg@x"])
    assert wrong.operations == () and "copies: 20261101-090000" in wrong.errors[0]


def test_in_a_terminal_the_answer_decides() -> None:
    asked: list[str] = []

    def ask(service: str, node: str, copies: Sequence[Copy]) -> Copy | None:
        asked.append(f"{service} {node} {len(copies)}")
        return None

    result = plan_restores(adding("bilbo"), {"bilbo": pg("bilbo")}, [FRODO_DB], ask=ask)
    assert asked == ["pg bilbo 1"]
    assert result.operations == () and result.fresh == ("pg",)


def test_a_stateless_service_has_nothing_to_restore() -> None:
    state = NodeState.of("bilbo", Service("dozzle"))
    assert plan_restores(adding("bilbo", "dozzle"), {"bilbo": state}, [FRODO_DB]).operations == ()
