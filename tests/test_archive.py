"""labctl archive: what is an entry, what gets deleted, and how it is deleted."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from click.testing import CliRunner

from labctl.apply import ApplyError
from labctl.archive import (
    DATA,
    FAILED,
    FILES,
    Entry,
    NodeArchive,
    delete_entries,
    group_entries,
    parse_age,
    parse_listing,
    select,
)
from labctl.cli.commands import archive as commands

FOLDER = "/d/app_data/.labctl/archive"
FILES_FOLDER = "/d/.labctl/archive"
NOW = datetime(2026, 11, 7, 12, 0, 0)


def data(path: str, *mounts: tuple[str, str], reason: str = "removed") -> Entry:
    sidecar = {
        "reason": reason,
        "mounts": [{"service": service, "target": target} for service, target in mounts],
    }
    return Entry(FOLDER, path, 1024, False, sidecar)


def stray(path: str) -> Entry:
    return Entry(FILES_FOLDER, path, 10, True)


OLD = data("heimdall-20261001-100000", ("heimdall", "/config"), reason="migrated")
MID = data("heimdall-20261101-100000", ("heimdall", "/config"))
NEW = data("heimdall-20261105-100000", ("heimdall", "/config"))


def deleted(entries: list[Entry], **options: Any) -> frozenset[Entry]:
    return select(group_entries(entries), now=NOW, **options)


def test_listing_parses_entries_their_sidecars_and_orphans() -> None:
    output = (
        'entry\theimdall-20261105-100000\t2048\t{"reason": "removed", "mounts": []}\n'
        "entry\tincoming-failed-20261031-193314\t5\t\n"
        "entry\tbroken-20261031-193314\t\t{not json\n"
        "orphan\tplex-20261101-090000.labctl.json.restored\n"
    )
    entries, orphans = parse_listing(FOLDER, False, output)
    assert [(entry.path, entry.size, entry.reason) for entry in entries] == [
        ("heimdall-20261105-100000", 2048, "removed"),
        ("incoming-failed-20261031-193314", 5, ""),
        ("broken-20261031-193314", None, ""),
    ]
    assert orphans == [(FOLDER, "plex-20261101-090000.labctl.json.restored")]


def test_an_entry_knows_its_stamp_and_name_also_nested() -> None:
    entry = data("fileflows/data-20261106-131306")
    assert (entry.name, entry.stamp) == ("fileflows/data", "20261106-131306")
    assert entry.when == datetime(2026, 11, 6, 13, 13, 6)


def test_groups_data_by_service_and_container_path_newest_first() -> None:
    other = data("sonarr-20261101-100000", ("sonarr", "/config"))
    groups = group_entries([OLD, other, NEW, MID])
    assert [(group.kind, group.title, group.service) for group in groups] == [
        (DATA, "heimdall /config", "heimdall"),
        (DATA, "sonarr /config", "sonarr"),
    ]
    assert groups[0].entries == (NEW, MID, OLD)


def test_groups_data_with_no_mounts_failed_copies_and_files() -> None:
    plain = data("oldthing-20261101-100000")
    failed = Entry(FOLDER, "incoming-failed-20261031-193314")
    failed_key = Entry(FOLDER, "plex-failed-20261031-193314")
    edits = stray("node-edits-20261106-010233")
    groups = group_entries([stray("up.sh-20261105-231159"), edits, failed, plain, failed_key])
    assert [(group.kind, group.title) for group in groups] == [
        (DATA, "oldthing"),
        (FAILED, "failed copies"),
        (FILES, "node-edits"),
        (FILES, "up.sh"),
    ]


def test_described_data_or_a_stray_file_named_failed_is_not_a_failed_copy() -> None:
    assert not data("x-failed-20261101-100000", ("x", "/data")).failed
    assert not stray("x-failed-20261101-100000").failed


def test_prune_keeps_the_newest_of_each_group() -> None:
    assert deleted([OLD, MID, NEW]) == {OLD, MID}


def test_keep_keeps_more_and_zero_keeps_none() -> None:
    assert deleted([OLD, MID, NEW], keep=2) == {OLD}
    assert deleted([OLD, MID, NEW], keep=0) == {OLD, MID, NEW}


def test_failed_copies_always_go_whatever_keep_says() -> None:
    failed = Entry(FOLDER, "incoming-failed-20261031-193314")
    assert deleted([failed], keep=5) == {failed}


def test_files_keep_the_newest_per_name() -> None:
    older, newer = stray("up.sh-20261001-100000"), stray("up.sh-20261105-100000")
    other = stray("down.sh-20261001-100000")
    assert deleted([older, newer, other]) == {older}


def test_older_than_limits_what_is_deleted() -> None:
    assert deleted([OLD, MID, NEW], older_than=timedelta(days=30)) == {OLD}
    assert deleted([OLD, MID, NEW], keep=0, older_than=timedelta(days=30)) == {OLD}


def test_services_narrow_to_their_groups() -> None:
    other_old = data("sonarr-20261001-100000", ("sonarr", "/config"))
    other_new = data("sonarr-20261101-100000", ("sonarr", "/config"))
    failed = Entry(FOLDER, "incoming-failed-20261031-193314")
    older_file = stray("up.sh-20261001-100000")
    newer_file = stray("up.sh-20261105-100000")
    everything = [OLD, NEW, other_old, other_new, failed, older_file, newer_file]
    assert deleted(everything, services=["heimdall"]) == {OLD}


def test_a_shared_entry_goes_only_when_every_group_lets_it() -> None:
    shared_old = data("shared-20261001-100000", ("a", "/data"), ("b", "/data"))
    a_new = data("a-20261101-100000", ("a", "/data"))
    assert deleted([shared_old, a_new]) == frozenset()  # still b's newest
    b_new = data("b-20261101-100000", ("b", "/data"))
    assert deleted([shared_old, a_new, b_new]) == {shared_old}
    # -s a would delete it for a, but b is left out and still keeps it.
    assert deleted([shared_old, a_new], services=["a"]) == frozenset()


def test_parse_age() -> None:
    assert parse_age("30d") == timedelta(days=30)
    assert parse_age("12h") == timedelta(hours=12)
    for text in ("30", "d", "1w", "-1d", ""):
        with pytest.raises(ValueError):
            parse_age(text)


class FakeNode:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.fail = fail
        self.remote = "remote"

    def helper(self, root: str, script: str, *args: str, read_only: bool = False) -> str:
        self.calls.append(("helper", root, *args))
        if self.fail:
            raise ApplyError("rm failed")
        return ""


@pytest.fixture
def locks(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    taken: list[str] = []
    monkeypatch.setattr("labctl.archive.lock", lambda remote, owner: taken.append(f"lock {owner}"))
    monkeypatch.setattr("labctl.archive.unlock", lambda remote: taken.append("unlock"))
    return taken


def test_delete_mounts_each_archive_and_includes_orphans(locks: list[str]) -> None:
    node: Any = FakeNode()
    archive = NodeArchive("cirdan", orphans=((FOLDER, "x-20261101-090000.labctl.json.restored"),))
    delete_entries(node, archive, [OLD, stray("up.sh-20261001-100000")], "me")
    assert node.calls == [
        ("helper", FILES_FOLDER, "up.sh-20261001-100000"),
        (
            "helper",
            FOLDER,
            "heimdall-20261001-100000",
            "x-20261101-090000.labctl.json.restored",
        ),
    ]
    assert locks == ["lock me", "unlock"]


def test_a_failed_delete_still_unlocks(locks: list[str]) -> None:
    node: Any = FakeNode(fail=True)
    with pytest.raises(ApplyError):
        delete_entries(node, NodeArchive("cirdan"), [OLD], "me")
    assert locks == ["lock me", "unlock"]


def test_no_delete_without_the_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    def held(remote: Any, owner: str) -> None:
        raise ApplyError("held")

    monkeypatch.setattr("labctl.archive.lock", held)
    node: Any = FakeNode()
    with pytest.raises(ApplyError):
        delete_entries(node, NodeArchive("cirdan"), [OLD], "me")
    assert node.calls == []


##############################################################################


# The commands


##############################################################################


@pytest.fixture
def run_cli(monkeypatch: pytest.MonkeyPatch) -> Any:
    """labctl archive against one node holding OLD and NEW; deletions are recorded, not made."""
    archive = NodeArchive("cirdan", (OLD, NEW))
    removed: list[tuple[str, frozenset[Entry]]] = []
    monkeypatch.setattr(commands, "_read", lambda targets: ({"cirdan": "node"}, [archive], []))
    monkeypatch.setattr(commands.Remote, "close_all", lambda names: None)
    monkeypatch.setattr(
        commands,
        "delete_entries",
        lambda node, found, doomed, owner: removed.append((found.node, frozenset(doomed))),
    )

    def run(*args: str, **options: Any) -> Any:
        result = CliRunner().invoke(commands.archive_cmd, list(args), **options)
        result.removed = removed
        return result

    return run


def test_list_deletes_nothing_and_shows_no_keep_or_delete(run_cli: Any) -> None:
    result = run_cli("list", "cirdan")
    assert result.exit_code == 0
    assert "heimdall /config" in result.output and "20261001-100000" in result.output
    assert "delete" not in result.output and "keep" not in result.output
    assert result.removed == []


def test_prune_dry_run_shows_it_and_deletes_nothing(run_cli: Any) -> None:
    result = run_cli("prune", "cirdan", "--dry-run")
    assert result.exit_code == 0
    assert "Delete 1 entry (1.0 KB) on 1 node; keep 1" in result.output
    assert result.removed == []


def test_prune_with_no_terminal_needs_yes(run_cli: Any) -> None:
    result = run_cli("prune", "cirdan")
    assert result.exit_code == 1
    assert "pass --yes" in result.output
    assert result.removed == []


def test_prune_yes_deletes_what_it_showed(run_cli: Any) -> None:
    result = run_cli("prune", "cirdan", "--yes")
    assert result.exit_code == 0
    assert result.removed == [("cirdan", frozenset({OLD}))]
    assert "cirdan: deleted 1 entry (1.0 KB)" in result.output


def test_prune_nothing_to_delete_does_not_ask(run_cli: Any) -> None:
    result = run_cli("prune", "cirdan", "--keep", "5")
    assert result.exit_code == 0
    assert "Nothing to delete" in result.output
    assert result.removed == []


def test_a_service_with_nothing_archived_is_an_error(run_cli: Any) -> None:
    result = run_cli("prune", "cirdan", "-s", "plex", "--yes")
    assert result.exit_code == 1
    assert "Nothing archived for plex" in result.output
    assert result.removed == []


def test_a_bad_age_is_refused(run_cli: Any) -> None:
    assert run_cli("prune", "cirdan", "--older-than", "soon").exit_code == 2
