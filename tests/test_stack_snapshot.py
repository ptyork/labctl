from __future__ import annotations

import json

from labctl.stack import ContainerChanges, Seen, container_changes, not_up_again, snapshot


def _line(id: str, project: str, service: str, state: str) -> str:
    return json.dumps({"id": id, "project": project, "service": service, "state": state})


def test_a_snapshot_groups_containers_by_project_and_service() -> None:
    output = "\n".join(
        [
            _line("a", "web", "app", "exited"),
            _line("b", "web", "app", "running"),  # scaled: two containers, one up
            _line("c", "web", "db", "restarting"),
            _line("d", "", "", "running"),  # not compose's
        ]
    )
    assert snapshot(output) == {
        ("web", "app"): Seen(frozenset({"a", "b"}), True),
        ("web", "db"): Seen(frozenset({"c"}), True),
    }


def test_changes_compare_container_ids() -> None:
    before = {
        ("web", "same"): Seen(frozenset({"1"}), True),
        ("web", "new_image"): Seen(frozenset({"2"}), True),
        ("web", "gone"): Seen(frozenset({"3"}), True),
        ("web", "moved_out"): Seen(frozenset({"4"}), True),
        ("old", "moved_in"): Seen(frozenset({"5"}), True),
        ("old", "moved_in_kept"): Seen(frozenset({"6"}), True),
    }
    after = {
        ("web", "same"): Seen(frozenset({"1"}), True),
        ("web", "new_image"): Seen(frozenset({"9"}), True),
        ("web", "moved_in"): Seen(frozenset({"8"}), True),
        ("web", "moved_in_kept"): Seen(frozenset({"6"}), True),
        ("web", "fresh"): Seen(frozenset({"7"}), False),
    }
    changes = container_changes(before, after, "web", came_from={"old"}, staying={"moved_out"})
    assert changes == ContainerChanges(("moved_in", "new_image"), ("fresh",), ("gone",))
    assert str(changes) == "recreated moved_in, new_image; created fresh; removed gone"
    assert not container_changes(before, before, "web")


def test_many_changes_are_counted() -> None:
    assert str(ContainerChanges(recreated=("a", "b", "c", "d"))) == "recreated 4 (a, b, c, …)"


def test_not_up_again_ignores_the_project() -> None:
    before = {
        ("docker", "plex"): Seen(frozenset({"1"}), True),
        ("x", "down"): Seen(frozenset(), False),
    }
    assert not_up_again(before, {("homelab", "plex"): Seen(frozenset({"2"}), True)}) == []
    assert not_up_again(before, {}) == ["plex"]
