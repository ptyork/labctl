"""Node: the real one, with its remote or its helper container faked."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from labctl import docker
from labctl import node as node_module
from labctl.layout import LOCK
from labctl.node import HeldLock, Node, NodeError, lock, read_lock
from labctl.remote import MARK, Remote, RemoteError
from labctl.state import Stack

HOME, APPDATA = "/srv/docker", "/srv/appdata"
NEW = Stack("homelab-cirdan", ("docker-compose.yml",))


class StreamingRemote(Remote):
    """A node whose compose prints two lines, then exits with ``code``."""

    def __init__(self, code: int) -> None:
        super().__init__("cirdan")
        self.code = code

    def stream(self, command: str) -> Any:
        assert "--progress plain" in command
        code = self.code

        class Proc:
            stdout = iter([" Container web  Created\r\n", " Container web  Started\r\n"])

            def wait(self) -> int:
                return code

        return Proc()


def test_compose_output_is_read_and_shown_only_when_it_fails() -> None:
    seen: list[str] = []
    Node(StreamingRemote(0)).compose(HOME, NEW, "up", "-d", watch=seen.append)
    assert seen == ["Container web  Created", "Container web  Started"]
    with pytest.raises(NodeError, match=r"failed \(homelab-cirdan\):\n    Container web  Created"):
        Node(StreamingRemote(1)).compose(HOME, NEW, "up", "-d")


def test_node_commands_name_paths_inside_the_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real Node, not the fake: what the helper container is given."""

    ran: list[tuple[str, ...]] = []
    node = Node(Remote("cirdan"))
    monkeypatch.setattr(node, "run", lambda *args, cwd="/": ran.append(args) or "")
    node.move(f"{APPDATA}/.labctl/incoming/op/heimdall", f"{APPDATA}/heimdall")
    node.data_state(APPDATA, "heimdall")
    node.checksums(f"{APPDATA}/.labctl/incoming/op", ["heimdall"])
    move, state, sums = ran
    assert move[:6] == ("docker", "run", "--rm", "-v", f"{APPDATA}:/x", docker.HELPER)
    assert move[-2:] == ("/x/.labctl/incoming/op/heimdall", "/x/heimdall")
    assert state[4] == f"{APPDATA}:/x" and state[-1] == "heimdall"
    assert sums[4] == f"{APPDATA}/.labctl/incoming/op:/x:ro" and sums[-1] == "heimdall"


class Helper:
    """The real Node with its helper container and copy faked: the filesystems ``elsewhere``
    names are not the one the move starts on."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, elsewhere: bool, same_copy: bool = True):
        self.node = Node(Remote("frodo"))
        self.calls: list[str] = []

        def helper(root: str, script: str, *args: str, read_only: bool = False) -> str:
            if "elsewhere" in script:
                self.calls.append(f"try {' '.join(args)}")
                return "elsewhere\n" if elsewhere else ""
            self.calls.append(f"helper {root} {' '.join(args)}")
            return ""

        def sums(spec: str) -> str:
            return "abc  ./x\n" if same_copy or spec.startswith("/zconfig") else "def  ./x\n"

        monkeypatch.setattr(self.node, "helper", helper)
        monkeypatch.setattr(self.node, "copy", lambda s, t: self.calls.append(f"copy {s} {t}"))
        monkeypatch.setattr(self.node, "sums", sums)


def test_a_move_on_one_filesystem_is_a_rename(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = Helper(monkeypatch, elsewhere=False)
    back = fake.node.move("/zconfig/appdata", "/zconfig/data")
    assert fake.calls == ["try /x/appdata /x/data"] and fake.node.moved_away == []
    back()
    assert fake.calls[-1] == "try /x/data /x/appdata"


def test_a_move_to_another_filesystem_copies_and_deletes_the_original_only_at_the_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = Helper(monkeypatch, elsewhere=True)
    fake.node.move("/zconfig/appdata", "/optane/appdata")
    assert fake.calls == [
        "try /x/zconfig/appdata /x/optane/appdata",
        "copy /zconfig/appdata /optane/appdata",
    ]
    assert fake.node.moved_away == ["/zconfig/appdata"]
    assert fake.node.delete_moved_away() == []
    assert fake.calls[-1] == "helper /zconfig appdata"  # rm -rf, once the apply succeeded
    assert fake.node.moved_away == []


def test_undoing_a_move_to_another_filesystem_drops_the_copy_and_keeps_the_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = Helper(monkeypatch, elsewhere=True)
    back = fake.node.move("/zconfig/appdata", "/optane/appdata")
    back()
    assert fake.calls[-1] == "helper /optane appdata"
    assert fake.node.moved_away == []


def test_a_copy_that_differs_is_removed_and_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = Helper(monkeypatch, elsewhere=True, same_copy=False)
    with pytest.raises(NodeError, match="differs from the original"):
        fake.node.move("/zconfig/appdata", "/optane/appdata")
    assert fake.calls[-1] == "helper /optane appdata" and fake.node.moved_away == []


# ── The apply lock ────────────────────────────────────────────────────────────


def answers(monkeypatch: pytest.MonkeyPatch, code: int, stdout: str = "") -> list[list[str]]:
    """The node answers every command with ``code`` and ``stdout``; returns the commands run."""
    ran: list[list[str]] = []

    def execute(args: list[str], cwd: str, remote: Remote) -> Any:
        ran.append(list(args))
        return SimpleNamespace(code=code, stdout=stdout, stderr="denied")

    monkeypatch.setattr(node_module, "execute", execute)
    return ran


def test_the_lock_names_its_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    ran = answers(monkeypatch, 0)
    lock(Remote("cirdan"), "labctl apply, paul@aragorn")
    assert ran[0][-1] == "labctl apply, paul@aragorn" and f'mkdir "$HOME/{LOCK}"' in ran[0][2]


def test_a_held_lock_refuses_and_says_how_to_clear_it(monkeypatch: pytest.MonkeyPatch) -> None:
    answers(monkeypatch, 1)
    with pytest.raises(NodeError, match="labctl unlock cirdan"):
        lock(Remote("cirdan"), "me")


def test_reading_the_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    answers(monkeypatch, 3)
    assert read_lock(Remote("cirdan")) is None
    answers(monkeypatch, 0, f"labctl apply, paul\n{MARK}\nstopped plex\n\nmoved data\n")
    assert read_lock(Remote("cirdan")) == HeldLock(
        "labctl apply, paul", ("stopped plex", "moved data")
    )
    answers(monkeypatch, 0, f"{MARK}\n")
    assert read_lock(Remote("cirdan")) == HeldLock("unknown", ())
    answers(monkeypatch, 1)
    with pytest.raises(RemoteError, match="cannot read"):
        read_lock(Remote("cirdan"))
