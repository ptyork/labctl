from __future__ import annotations

from labctl.moves import (
    APPDATA,
    DATA,
    HOME,
    VOLUME,
    FannedOut,
    Move,
    MoveConflict,
    MovePlan,
    NotRenamed,
    UnevenRename,
    plan_moves,
    root_moves,
    where_now,
)
from labctl.planner import Add, Migrate, Plan, Remove, Update, make_plan
from labctl.state import Dirs, NodeState, Service, is_volume, volume_name


def svc(name: str, fp: str = "x", **mounts: str) -> Service:
    """Service whose mounts are given as container_path=data_path ('_' stands for '/')."""
    pairs = tuple((f"/{t.replace('_', '/')}", key) for t, key in mounts.items())
    keys = list(dict.fromkeys(key for _, key in pairs))
    return Service(
        name,
        data_paths=tuple(k for k in keys if not is_volume(k)),
        volumes=tuple(volume_name(k) for k in keys if is_volume(k)),
        fingerprint=fp,
        mounts=pairs,
    )


def states(**services: list[Service]) -> dict[str, NodeState]:
    return {node: NodeState.of(node, *svcs) for node, svcs in services.items()}


SAME = Dirs("/home/paul/docker", "/srv/appdata")


def dirs_for(*nodes: str, value: Dirs = SAME) -> dict[str, Dirs]:
    return {node: value for node in nodes}


# ── Roots ─────────────────────────────────────────────────────────────────────


def test_unchanged_roots_move_nothing() -> None:
    assert root_moves("frodo", SAME, SAME) == ([], None)


def test_appdata_move() -> None:
    moves, conflict = root_moves("frodo", SAME, Dirs(SAME.deploy_dir, "/tank/appdata"))
    assert conflict is None
    assert moves == [
        Move("frodo", APPDATA, "/srv/appdata", "/tank/appdata", "/srv/appdata", "/tank/appdata")
    ]


def test_nested_appdata_moves_with_its_deploy_dir() -> None:
    """cirdan's layout: APPDATA_DIR inside DEPLOY_DIR, both moving together."""
    old = Dirs("/home/paul/docker", "/home/paul/docker/app_data")
    new = Dirs("/srv/docker", "/srv/docker/app_data")
    moves, conflict = root_moves("cirdan", old, new)
    assert conflict is None
    assert [(m.kind, m.source, m.target, m.carried) for m in moves] == [
        (HOME, "/home/paul/docker", "/srv/docker", False),
        (APPDATA, "/srv/docker/app_data", "/srv/docker/app_data", True),
    ]


def test_nested_appdata_that_stays_is_moved_back_after_its_deploy_dir() -> None:
    old = Dirs("/home/paul/docker", "/home/paul/docker/app_data")
    new = Dirs("/srv/docker", "/home/paul/docker/app_data")
    moves, _ = root_moves("cirdan", old, new)
    assert [(m.kind, m.source, m.target) for m in moves] == [
        (HOME, "/home/paul/docker", "/srv/docker"),
        (APPDATA, "/srv/docker/app_data", "/home/paul/docker/app_data"),
    ]


def test_new_deploy_dir_inside_moving_appdata_moves_appdata_first() -> None:
    old = Dirs("/opt/docker", "/data")
    new = Dirs("/data/docker", "/srv/data")
    moves, _ = root_moves("frodo", old, new)
    assert [(m.kind, m.source, m.target) for m in moves] == [
        (APPDATA, "/data", "/srv/data"),
        (HOME, "/opt/docker", "/data/docker"),
    ]


def test_roots_that_would_carry_each_other_conflict() -> None:
    moves, conflict = root_moves("frodo", Dirs("/a", "/b"), Dirs("/b/home", "/a/data"))
    assert moves == []
    assert conflict is not None and "move one per apply" in conflict.message


def test_where_now_reverses_the_moves() -> None:
    old = Dirs("/home/paul/docker", "/home/paul/docker/app_data")
    new = Dirs("/srv/docker", "/srv/docker/app_data")
    moves, _ = root_moves("cirdan", old, new)
    moves.append(
        Move(
            "cirdan",
            DATA,
            "plexms",
            "plex",
            "/srv/docker/app_data/plexms",
            "/srv/docker/app_data/plex",
        )
    )
    assert (
        where_now("/srv/docker/app_data/plex/db", moves) == "/home/paul/docker/app_data/plexms/db"
    )
    assert where_now("/mnt/media", moves) == "/mnt/media"


# ── Data renames ──────────────────────────────────────────────────────────────


def test_rename_is_a_move_plus_update_not_new_data() -> None:
    have = states(frodo=[svc("plex", "old", config="plexms")])
    want = states(frodo=[svc("plex", "new", config="plex")])
    moves = plan_moves(want, have, dirs_for("frodo"), dirs_for("frodo"))
    assert moves.moves == (
        Move(
            "frodo", DATA, "plexms", "plex", "/srv/appdata/plexms", "/srv/appdata/plex", ("plex",)
        ),
    )
    plan = make_plan(want, have, move_plan=moves)
    assert plan.moves == moves.moves
    assert plan.operations == (Update("plex", "frodo"),)
    assert plan.notices == () and plan.problems == ()


def test_without_the_rename_it_would_be_new_data() -> None:
    """Sanity check of the old behaviour: no archive, no move, the data is just orphaned."""
    have = states(frodo=[svc("plex", "old", config="plexms")])
    want = states(frodo=[svc("plex", "new", config="plex")])
    assert make_plan(want, have).operations == (Update("plex", "frodo"),)
    assert make_plan(want, have).moves == ()


def test_nested_rename_implied_by_parent_is_one_move() -> None:
    have = states(frodo=[svc("plex", config="plexms", transcode="plexms/transcode")])
    want = states(frodo=[svc("plex", config="plex", transcode="plex/transcode")])
    moves = plan_moves(want, have, dirs_for("frodo"), dirs_for("frodo"))
    assert [(m.old, m.new) for m in moves.moves] == [("plexms", "plex")]
    assert moves.conflicts == ()


def test_nested_rename_contradicting_parent_conflicts() -> None:
    have = states(frodo=[svc("plex", config="plexms", transcode="plexms/transcode")])
    want = states(frodo=[svc("plex", config="plex", transcode="cache/transcode")])
    moves = plan_moves(want, have, dirs_for("frodo"), dirs_for("frodo"))
    assert moves.moves == ()
    assert "contradicts plexms/ → plex/" in moves.conflicts[0].message


def test_renaming_shared_data_keeps_it_for_the_others() -> None:
    have = states(frodo=[svc("plex", config="plexms"), svc("tautulli", logs="plexms")])
    want = states(frodo=[svc("plex", config="plex"), svc("tautulli", logs="plexms")])
    moves = plan_moves(want, have, dirs_for("frodo"), dirs_for("frodo"))
    assert moves.moves == ()
    assert moves.notices == (NotRenamed("frodo", "plexms", "plex", ("plex",), ("tautulli",)),)


def test_shared_data_renamed_by_all_its_services_moves_once() -> None:
    have = states(frodo=[svc("plex", config="plexms"), svc("tautulli", logs="plexms")])
    want = states(frodo=[svc("plex", config="plex"), svc("tautulli", logs="plex")])
    moves = plan_moves(want, have, dirs_for("frodo"), dirs_for("frodo"))
    assert [(m.old, m.new, m.services) for m in moves.moves] == [
        ("plexms", "plex", ("plex", "tautulli"))
    ]


def test_rename_onto_another_services_data_conflicts() -> None:
    have = states(frodo=[svc("plex", config="plexms"), svc("sonarr", config="media")])
    want = states(frodo=[svc("plex", config="media/plex"), svc("sonarr", config="media")])
    moves = plan_moves(want, have, dirs_for("frodo"), dirs_for("frodo"))
    assert moves.moves == ()
    assert "already used by sonarr" in moves.conflicts[0].message


def test_one_directory_split_two_ways_conflicts() -> None:
    have = states(frodo=[svc("a", config="shared"), svc("b", config="shared")])
    want = states(frodo=[svc("a", config="one"), svc("b", config="two")])
    moves = plan_moves(want, have, dirs_for("frodo"), dirs_for("frodo"))
    assert moves.moves == ()
    assert "more than one directory" in moves.conflicts[0].message


def test_moving_into_itself_conflicts() -> None:
    have = states(frodo=[svc("plex", config="plexms")])
    want = states(frodo=[svc("plex", config="plexms/config")])
    moves = plan_moves(want, have, dirs_for("frodo"), dirs_for("frodo"))
    assert "cannot move into itself" in moves.conflicts[0].message


def cross(have: dict[str, NodeState], want: dict[str, NodeState]) -> tuple[MovePlan, Plan]:
    nodes = sorted(want)
    moves = plan_moves(want, have, dirs_for(*nodes), dirs_for(*nodes))
    return moves, make_plan(want, have, move_plan=moves)


def test_migrate_with_rename_copies_from_the_old_name() -> None:
    have = states(frodo=[svc("plex", config="plexms")], bilbo=[])
    want = states(frodo=[], bilbo=[svc("plex", config="plex")])
    moves, plan = cross(have, want)
    assert moves.moves == () and moves.conflicts == ()
    assert plan.operations == (
        Migrate(("plex",), "frodo", "bilbo", ("plex",), ("plex",), (("plex", "plexms"),)),
    )
    (migrate,) = plan.operations
    assert isinstance(migrate, Migrate)
    assert migrate.source_data == ("plexms",)
    assert migrate.archive == ("plexms",)  # frodo's copy is archived under its own name
    assert plan.notices == () and not plan.problems


def test_migrate_with_nested_renames_moves_each_mount() -> None:
    have = states(frodo=[svc("plex", config="plexms/config", icons="plexms/icons")], bilbo=[])
    want = states(frodo=[], bilbo=[svc("plex", config="plex/config", icons="plex/icons")])
    _, plan = cross(have, want)
    (migrate,) = plan.operations
    assert isinstance(migrate, Migrate)
    assert dict(migrate.renamed) == {"plex/config": "plexms/config", "plex/icons": "plexms/icons"}
    assert plan.notices == ()


def test_migrate_with_a_mistyped_mount_does_it_per_mount_and_warns() -> None:
    """plexms/config -> plex/config, but plexms/icons keeps its name: each mount is copied to its
    own new path, and the split is pointed out."""
    have = states(frodo=[svc("plex", config="plexms/config", icons="plexms/icons")], bilbo=[])
    want = states(frodo=[], bilbo=[svc("plex", config="plex/config", icons="plexms/icons")])
    _, plan = cross(have, want)
    (migrate,) = plan.operations
    assert isinstance(migrate, Migrate)
    assert sorted(migrate.data) == ["plex/config", "plexms/icons"]
    assert dict(migrate.renamed) == {"plex/config": "plexms/config"}
    assert sorted(migrate.source_data) == ["plexms/config", "plexms/icons"]
    assert plan.notices == (
        UnevenRename("frodo", "plex", "plexms", ("plex", "plexms"), target="bilbo"),
    )
    assert not plan.problems


def test_same_node_mistyped_mount_warns_too() -> None:
    have = states(frodo=[svc("plex", config="plexms/config", icons="plexms/icons")])
    want = states(frodo=[svc("plex", config="plex/config", icons="plexms/icons")])
    moves, _ = cross(have, want)
    assert [(m.old, m.new) for m in moves.moves] == [("plexms/config", "plex/config")]
    assert moves.notices == (UnevenRename("frodo", "plex", "plexms", ("plex", "plexms")),)


def test_migrate_with_rename_of_shared_data_conflicts() -> None:
    have = states(frodo=[svc("plex", config="plexms"), svc("tautulli", logs="plexms")], bilbo=[])
    want = states(frodo=[svc("tautulli", logs="plexms")], bilbo=[svc("plex", config="plex")])
    moves, plan = cross(have, want)
    assert moves.conflicts == (
        MoveConflict(
            "frodo",
            "plex moves to bilbo with renamed data, but tautulli also uses plexms/ on frodo; it "
            "cannot take shared data under a new name",
        ),
    )
    assert plan.problems


def test_migrate_with_rename_to_two_nodes_conflicts() -> None:
    have = states(frodo=[svc("plex", config="plexms")], bilbo=[], cirdan=[])
    want = states(frodo=[], bilbo=[svc("plex", config="plex")], cirdan=[svc("plex", config="plex")])
    moves, _ = cross(have, want)
    assert "cannot be migrated to more than one node" in moves.conflicts[0].message


def test_one_source_directory_to_two_target_names_conflicts() -> None:
    have = states(frodo=[svc("plex", config="plexms", cache="plexms")], bilbo=[])
    want = states(frodo=[], bilbo=[svc("plex", config="plex", cache="cache")])
    moves, _ = cross(have, want)
    assert "would go to both" in moves.conflicts[0].message


def test_no_migrate_with_rename_archives_the_old_name() -> None:
    have = states(frodo=[svc("plex", config="plexms")], bilbo=[])
    want = states(frodo=[], bilbo=[svc("plex", config="plex")])
    nodes = sorted(want)
    moves = plan_moves(want, have, dirs_for(*nodes), dirs_for(*nodes))
    plan = make_plan(want, have, no_migrate={"plex"}, move_plan=moves)
    assert Remove("plex", "frodo", ("plexms",)) in plan.operations
    assert Add("plex", "bilbo") in plan.operations


def test_changed_container_path_is_not_a_rename() -> None:
    have = states(frodo=[svc("plex", config="plexms")])
    want = states(frodo=[svc("plex", data="plex")])
    assert plan_moves(want, have, dirs_for("frodo"), dirs_for("frodo")).moves == ()


def test_renamed_data_of_a_removed_service_follows_the_move() -> None:
    """A service removed in the same apply that used the old directory: no archive of the moved
    data, because the renamed service still uses it."""
    have = states(frodo=[svc("plex", config="plexms"), svc("old-helper", data="plexms")])
    want = states(frodo=[svc("plex", "y", config="plex")])
    plan = make_plan(
        want, have, move_plan=plan_moves(want, have, dirs_for("frodo"), dirs_for("frodo"))
    )
    assert [(m.old, m.new) for m in plan.moves] == [("plexms", "plex")]
    assert Remove("old-helper", "frodo") in plan.operations
    assert Add("plex", "frodo") not in plan.operations


def test_moves_on_a_skipped_node_are_held() -> None:
    from labctl.state import Scope

    have = states(frodo=[svc("plex", "a", config="plexms")])
    want = states(frodo=[svc("plex", "b", config="plex")])
    moves = plan_moves(want, have, dirs_for("frodo"), dirs_for("frodo"))
    plan = make_plan(want, have, Scope(frozenset({"frodo"})), move_plan=moves)
    assert plan.moves == ()
    assert [h.operation for h in plan.held] == [moves.moves[0], Update("plex", "frodo")]


def test_root_and_rename_together_rename_in_the_new_root() -> None:
    have = states(frodo=[svc("plex", config="plexms")])
    want = states(frodo=[svc("plex", config="plex")])
    new = Dirs(SAME.deploy_dir, "/tank/appdata")
    moves = plan_moves(want, have, {"frodo": new}, dirs_for("frodo"))
    assert [(m.kind, m.source, m.target) for m in moves.moves] == [
        (APPDATA, "/srv/appdata", "/tank/appdata"),
        (DATA, "/tank/appdata/plexms", "/tank/appdata/plex"),
    ]
    assert where_now("/tank/appdata/plex", moves.moves) == "/srv/appdata/plexms"


# ── Named volumes ─────────────────────────────────────────────────────────────


def test_a_service_moving_stack_renames_its_volume() -> None:
    """bilbo's first apply: project `docker` becomes `homelab-bilbo`, so `docker_db` would be
    left behind and the service would start on an empty `homelab-bilbo_db`."""
    have = states(bilbo=[svc("pg", "v1", data="volume:docker_db")])
    want = states(bilbo=[svc("pg", "v2", data="volume:homelab-bilbo_db")])
    moves = plan_moves(want, have, dirs_for("bilbo"), dirs_for("bilbo"))
    assert moves.moves == (
        Move(
            "bilbo",
            VOLUME,
            "volume:docker_db",
            "volume:homelab-bilbo_db",
            "volume:docker_db",
            "volume:homelab-bilbo_db",
            ("pg",),
        ),
    )
    plan = make_plan(want, have, move_plan=moves)
    assert plan.operations == (Update("pg", "bilbo"),)
    assert plan.notices == () and plan.problems == ()


def test_a_directory_becoming_a_volume_is_a_copy_too() -> None:
    have = states(bilbo=[svc("pg", "v1", data="pg")])
    want = states(bilbo=[svc("pg", "v2", data="volume:homelab-bilbo_pg")])
    moves = plan_moves(want, have, dirs_for("bilbo"), dirs_for("bilbo"))
    assert [(m.kind, m.old, m.new) for m in moves.moves] == [
        (VOLUME, "pg", "volume:homelab-bilbo_pg")
    ]


def test_a_volume_migrates_under_the_target_s_project_name() -> None:
    have = states(frodo=[svc("pg", data="volume:homelab-frodo_db")], bilbo=[])
    want = states(frodo=[], bilbo=[svc("pg", data="volume:homelab-bilbo_db")])
    moves, plan = cross(have, want)
    assert moves.conflicts == ()
    (op,) = plan.operations
    assert op == Migrate(
        ("volume:homelab-bilbo_db",),
        "frodo",
        "bilbo",
        ("pg",),
        ("pg",),
        (("volume:homelab-bilbo_db", "volume:homelab-frodo_db"),),
    )
    assert isinstance(op, Migrate)
    assert op.source_data == ("volume:homelab-frodo_db",)
    assert op.archive == ("volume:homelab-frodo_db",)


def test_a_volume_leaving_for_two_nodes_is_a_fan_out_not_a_conflict() -> None:
    have = states(frodo=[svc("pg", data="volume:homelab-frodo_db")], bilbo=[], cirdan=[])
    want = states(
        frodo=[],
        bilbo=[svc("pg", data="volume:homelab-bilbo_db")],
        cirdan=[svc("pg", data="volume:homelab-cirdan_db")],
    )
    moves, plan = cross(have, want)
    assert moves.conflicts == ()
    assert moves.notices == (FannedOut("frodo", "pg", ("bilbo", "cirdan")),)
    assert Remove("pg", "frodo", ("volume:homelab-frodo_db",)) in plan.operations
    assert {op for op in plan.operations if isinstance(op, Add)} == {
        Add("pg", "bilbo"),
        Add("pg", "cirdan"),
    }
