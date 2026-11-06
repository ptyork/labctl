from __future__ import annotations

import pytest

from labctl.planner import (
    Add,
    Assumed,
    Held,
    Migrate,
    MissingBaseline,
    NotMigrated,
    Remove,
    UnknownNode,
    Update,
    archived_keys,
    make_plan,
    top_level_paths,
)
from labctl.state import NodeState, Scope, Service

PLEX = Service("plex", ("plexms",), fingerprint="plex-v1")
PLEX_V2 = Service("plex", ("plexms",), fingerprint="plex-v2")
# Variants for different GPUs: different service names, same data.
PLEX_NVIDIA = Service("plex-nvidia", ("plexms",), fingerprint="nv-v1")
PLEX_INTEL = Service("plex-intel", ("plexms",), fingerprint="intel-v1")
SONARR = Service("sonarr", ("sonarr",), fingerprint="sonarr-v1")
NPM = Service("npm", ("npm", "letsencrypt"), fingerprint="npm-v1")
PORTAINER = Service("portainer", ("portainer",), fingerprint="portainer-v1")
DOZZLE_AGENT = Service("dozzle-agent", fingerprint="dozzle-v1")
DOZZLE_AGENT_V2 = Service("dozzle-agent", fingerprint="dozzle-v2")
# Two services sharing data: notebook/ holds both; the api mounts a subdirectory.
NB_API = Service("api", ("notebook/data",), fingerprint="api-v1")
NB_DB = Service("db", ("notebook",), fingerprint="db-v1")
PG = Service("pg", (), ("pgdata",), "pg-v1")
PG_ADMIN = Service("pgadmin", (), ("pgdata",), "pga-v1")


def homelab(**nodes: list[Service]) -> dict[str, NodeState]:
    return {name: NodeState.of(name, *services) for name, services in nodes.items()}


def migrate(data: tuple[str, ...], source: str, target: str, *services: str) -> Migrate:
    return Migrate(data, source, target, services, services)


def test_identical_state_is_an_empty_plan() -> None:
    state = homelab(frodo=[PLEX, DOZZLE_AGENT], bilbo=[NPM])
    plan = make_plan(state, dict(state))
    assert (plan.moves, plan.operations, plan.held, plan.problems) == ((), (), (), ())
    assert sorted(plan.unchanged) == [
        ("dozzle-agent", "frodo"),
        ("npm", "bilbo"),
        ("plex", "frodo"),
    ]


def test_add_update_and_remove() -> None:
    recorded = homelab(frodo=[PLEX, DOZZLE_AGENT], bilbo=[])
    desired = homelab(frodo=[PLEX_V2, SONARR], bilbo=[])
    plan = make_plan(desired, recorded)
    assert plan.operations == (
        Remove("dozzle-agent", "frodo"),
        Update("plex", "frodo"),
        Add("sonarr", "frodo"),
    )


def test_removed_service_has_its_data_archived() -> None:
    plan = make_plan(homelab(frodo=[]), homelab(frodo=[PORTAINER, NPM]))
    assert plan.operations == (
        Remove("npm", "frodo", archive=("letsencrypt", "npm")),
        Remove("portainer", "frodo", archive=("portainer",)),
    )


def test_data_still_used_on_the_node_is_not_archived() -> None:
    plan = make_plan(homelab(frodo=[NB_DB]), homelab(frodo=[NB_API, NB_DB]))
    assert plan.operations == (Remove("api", "frodo"),)


def test_shared_data_is_archived_once() -> None:
    plan = make_plan(homelab(frodo=[]), homelab(frodo=[NB_API, NB_DB]))
    assert plan.operations == (
        Remove("api", "frodo", archive=("notebook",)),
        Remove("db", "frodo"),
    )


def test_top_level_paths() -> None:
    assert top_level_paths(["notebook/data", "notebook", "npm", "volume:pg"]) == (
        "notebook",
        "npm",
    )


# ── Migration is defined by data ──────────────────────────────────────────────


def test_data_on_one_node_before_and_another_after_is_a_migrate() -> None:
    plan = make_plan(homelab(frodo=[], cirdan=[PLEX]), homelab(frodo=[PLEX], cirdan=[]))
    (op,) = plan.operations
    assert op == migrate(("plexms",), "frodo", "cirdan", "plex")
    assert op.archive == ("plexms",)  # the source's copy
    assert plan.notices == ()


def test_migrate_with_changed_definition_is_still_one_migrate() -> None:
    plan = make_plan(homelab(frodo=[], cirdan=[PLEX_V2]), homelab(frodo=[PLEX], cirdan=[]))
    assert plan.operations == (migrate(("plexms",), "frodo", "cirdan", "plex"),)


def test_different_services_with_the_same_data_migrate() -> None:
    """plex-nvidia on frodo becomes plex-intel on cirdan: same plexms/ data, so it moves."""
    plan = make_plan(
        homelab(frodo=[], cirdan=[PLEX_INTEL]), homelab(frodo=[PLEX_NVIDIA], cirdan=[])
    )
    assert plan.operations == (
        Migrate(("plexms",), "frodo", "cirdan", ("plex-nvidia",), ("plex-intel",)),
    )


def test_services_sharing_data_migrate_together() -> None:
    plan = make_plan(
        homelab(frodo=[SONARR], cirdan=[NB_API, NB_DB]),
        homelab(frodo=[SONARR, NB_API, NB_DB], cirdan=[]),
    )
    assert plan.operations == (
        migrate(("notebook", "notebook/data"), "frodo", "cirdan", "api", "db"),
    )


def test_shared_named_volume_groups_services() -> None:
    plan = make_plan(homelab(a=[], b=[PG, PG_ADMIN]), homelab(a=[PG, PG_ADMIN], b=[]))
    (op,) = plan.operations
    assert op == migrate(("volume:pgdata",), "a", "b", "pg", "pgadmin")
    assert op.archive == ("volume:pgdata",)  # the source's volume, copied to files and removed


def test_same_compose_key_in_two_stacks_is_not_shared() -> None:
    """Volumes are keyed by their real name: two stacks' `data` volumes are different data."""
    a = Service("a", (), ("one_data",), "a-v1", stack="one")
    b = Service("b", (), ("two_data",), "b-v1", stack="two")
    plan = make_plan(homelab(x=[a], y=[b]), homelab(x=[a, b], y=[]))
    (op,) = plan.operations
    assert op == migrate(("volume:two_data",), "x", "y", "b")


def test_a_removed_service_s_volume_is_archived() -> None:
    plan = make_plan(homelab(a=[]), homelab(a=[PG]))
    assert plan.operations == (Remove("pg", "a", ("volume:pgdata",)),)


def test_archived_keys_are_top_level_paths_then_volumes() -> None:
    assert archived_keys(["volume:z", "notebook/data", "notebook", "volume:a", "volume:z"]) == (
        "notebook",
        "volume:a",
        "volume:z",
    )


def test_data_nothing_uses_any_more_is_archived_not_moved() -> None:
    """The service's new definition mounts no data: plexms/ is archived on frodo."""
    no_data = Service("plex", fingerprint="plex-v3")
    plan = make_plan(homelab(frodo=[], cirdan=[no_data]), homelab(frodo=[PLEX], cirdan=[]))
    assert plan.operations == (
        Add("plex", "cirdan"),
        Remove("plex", "frodo", archive=("plexms",)),
    )
    assert plan.notices == ()


# ── Data that does not move ───────────────────────────────────────────────────


def test_second_instance_starts_without_data() -> None:
    plan = make_plan(homelab(frodo=[PLEX], cirdan=[PLEX]), homelab(frodo=[PLEX], cirdan=[]))
    assert plan.operations == (Add("plex", "cirdan"),)
    assert plan.notices == (
        NotMigrated(("plexms",), "frodo", "duplicate", (("plex", "cirdan"),), kept=("plex",)),
    )
    assert not plan.problems


def test_moving_one_of_two_services_that_share_data_needs_confirmation() -> None:
    plan = make_plan(
        homelab(frodo=[NB_DB], cirdan=[NB_API]),
        homelab(frodo=[NB_API, NB_DB], cirdan=[]),
    )
    # The data stays in use on frodo, so the leaving service's remove archives nothing.
    assert plan.operations == (Add("api", "cirdan"), Remove("api", "frodo"))
    (notice,) = plan.notices
    assert notice.reason == "shared"
    assert notice.kept == ("db",)
    assert notice.moved == ("api",)
    assert notice.fresh == (("api", "cirdan"),)


def test_data_leaving_for_two_nodes_is_not_copied() -> None:
    plan = make_plan(
        homelab(frodo=[], cirdan=[NB_API], bilbo=[NB_DB]),
        homelab(frodo=[NB_API, NB_DB], cirdan=[], bilbo=[]),
    )
    assert not any(isinstance(op, Migrate) for op in plan.operations)
    assert Remove("api", "frodo", archive=("notebook",)) in plan.operations
    (notice,) = plan.notices
    assert notice.reason == "fanout"


def test_no_migrate_turns_a_move_into_a_fresh_start() -> None:
    plan = make_plan(
        homelab(frodo=[], cirdan=[PLEX]),
        homelab(frodo=[PLEX], cirdan=[]),
        no_migrate={"plex"},
    )
    assert plan.operations == (
        Add("plex", "cirdan"),
        Remove("plex", "frodo", archive=("plexms",)),
    )
    assert [n.reason for n in plan.notices] == ["no-migrate"]


def test_data_already_on_two_nodes_is_left_alone() -> None:
    plan = make_plan(homelab(frodo=[PLEX], cirdan=[]), homelab(frodo=[PLEX], cirdan=[PLEX]))
    assert plan.operations == (Remove("plex", "cirdan", archive=("plexms",)),)
    assert plan.notices == ()


def test_stateless_services_are_planned_per_node() -> None:
    recorded = homelab(frodo=[DOZZLE_AGENT], cirdan=[DOZZLE_AGENT], bilbo=[])
    desired = homelab(frodo=[], cirdan=[DOZZLE_AGENT_V2], bilbo=[DOZZLE_AGENT])
    plan = make_plan(desired, recorded)
    assert plan.operations == (
        Add("dozzle-agent", "bilbo"),
        Update("dozzle-agent", "cirdan"),
        Remove("dozzle-agent", "frodo"),
    )
    assert plan.notices == ()


# ── Baselines ─────────────────────────────────────────────────────────────────


def test_node_without_baseline_blocks_everything() -> None:
    recorded: dict[str, NodeState | None] = {"frodo": NodeState.of("frodo", PLEX), "cirdan": None}
    plan = make_plan(homelab(frodo=[], cirdan=[PLEX]), recorded)
    # A migrate from an unknown node would look like an add, so nothing is planned.
    assert plan.operations == ()
    assert plan.problems == (MissingBaseline("cirdan"),)


def test_node_dropped_from_repo_blocks_everything() -> None:
    plan = make_plan(homelab(frodo=[PLEX]), homelab(frodo=[PLEX], bilbo=[NPM]))
    assert plan.problems == (UnknownNode("bilbo"),)
    assert plan.operations == ()


# ── Scope ─────────────────────────────────────────────────────────────────────


def test_migrate_touching_a_skipped_node_is_held() -> None:
    recorded = homelab(frodo=[PLEX, SONARR], cirdan=[])
    desired = homelab(frodo=[SONARR], cirdan=[PLEX])
    plan = make_plan(desired, recorded, Scope(skipped=frozenset({"frodo"})))
    assert plan.operations == ()
    assert plan.held == (Held(migrate(("plexms",), "frodo", "cirdan", "plex"), ("frodo",)),)
    assert not plan.problems


def test_operations_within_scope_proceed_alongside_held_ones() -> None:
    recorded = homelab(frodo=[PLEX], cirdan=[], bilbo=[])
    desired = homelab(frodo=[], cirdan=[PLEX], bilbo=[NPM])
    plan = make_plan(desired, recorded, Scope(skipped=frozenset({"frodo"})))
    assert plan.operations == (Add("npm", "bilbo"),)
    assert [h.operation for h in plan.held] == [migrate(("plexms",), "frodo", "cirdan", "plex")]


def test_skipped_node_never_applied_is_assumed_to_match_the_repo() -> None:
    recorded: dict[str, NodeState | None] = {"frodo": None, "cirdan": NodeState.of("cirdan")}
    desired = homelab(frodo=[PLEX], cirdan=[SONARR])
    plan = make_plan(desired, recorded, Scope(skipped=frozenset({"frodo"})))
    assert plan.problems == ()
    assert plan.operations == (Add("sonarr", "cirdan"),)  # nothing planned for frodo
    assert plan.notices == (Assumed("frodo"),)


def test_scope_is_the_nodes_named() -> None:
    nodes = {"frodo", "bilbo", "cirdan"}
    assert Scope.build(nodes, ["cirdan"]).skipped == frozenset({"frodo", "bilbo"})
    assert Scope.build(nodes, ["cirdan", "frodo", "cirdan"]).skipped == frozenset({"bilbo"})
    assert Scope.build(nodes, ["all"]) == Scope()
    unreachable = Scope.build(nodes, ["all"]).without(["frodo"])
    assert unreachable.skipped == frozenset({"frodo"})


@pytest.mark.parametrize(
    ("nodes", "targets", "message"),
    [
        ({"frodo"}, [], "Name the nodes, or 'all'. Nodes: frodo"),
        ({"frodo"}, ["gandalf"], "Unknown node"),
        ({"frodo"}, ["all", "frodo"], "already means every node"),
        ({"frodo", "all"}, ["frodo"], "may not be called 'all'"),
    ],
)
def test_scope_rejects_bad_targets(nodes: set[str], targets: list[str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Scope.build(nodes, targets)
