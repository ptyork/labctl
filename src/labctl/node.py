"""A node, as labctl works on it: its shell, file work in the helper container, and the apply lock.

File work (moving, copying, checksums, deleting) runs as root in a short-lived helper container
with the directory or volume mounted, so it needs no sudo and app data owned by anyone can be
handled. The lock (``~/.labctl-lock``) is held by whatever changes the node: an apply, or
``archive prune``.
"""

from __future__ import annotations

import json
import logging
import posixpath
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from labctl import docker
from labctl.layout import LOCK
from labctl.remote import MARK, Remote, RemoteError, execute
from labctl.stack import Snapshot, snapshot
from labctl.state import Stack

logger = logging.getLogger(__name__)


class NodeError(Exception):
    """A command on a node failed."""


# In the helper: $1 to $2 when one filesystem holds both (a rename); else it only says
# "elsewhere". $2's parents are created.
_MOVE = """\
set -e
mkdir -p "$(dirname "$2")"
if [ "$(stat -c %d "$1")" != "$(stat -c %d "$(dirname "$2")")" ]; then echo elsewhere; exit 0; fi
mv -T -- "$1" "$2"
"""

# In the helper, on /x: missing, empty (a directory Docker made for a missing bind source) or data.
_STATE = """\
p="/x/$1"
if [ ! -e "$p" ] && [ ! -L "$p" ]; then echo missing
elif [ -d "$p" ] && [ -z "$(ls -A "$p")" ]; then echo empty
else echo data; fi
"""

# In the helper, on /x: checksums of every file under the given paths, sorted.
_SUMS = 'cd /x && find "$@" -type f -exec sha256sum {} + | sort -k2'

# The same for everything in /x, relative to it.
_RELATIVE_SUMS = "cd /x && find . -type f -exec sha256sum {} + | sort -k2"


class Node:
    """The commands labctl runs on one node. Every one raises NodeError when it fails."""

    def __init__(self, remote: Remote) -> None:
        self.remote = remote
        self.name = remote.host
        # Directories moved to another filesystem: copied, and deleted once the apply succeeded.
        self.moved_away: list[str] = []

    def run(self, *args: str, cwd: str = "/") -> str:
        result = execute(args, cwd, self.remote)
        if result.code:
            raise NodeError(f"{self.name}: {' '.join(args)}: {result.stderr.strip()}")
        return result.stdout

    def snapshot(self) -> Snapshot:
        """The node's compose containers now."""
        return snapshot(self.run(*docker.snapshot()))

    def script(self, script: str, *args: str) -> None:
        self.run("sh", "-c", script, "sh", *args)

    def compose(
        self, home: str, stack: Stack, *args: str, watch: Callable[[str], None] | None = None
    ) -> None:
        """A compose command. Its output is read, line by line (to ``watch``), and shown only when
        it fails; with debug logging it goes straight to the terminal."""
        if logging.getLogger().isEnabledFor(logging.DEBUG):
            if self.remote.run(docker.compose_command(home, stack, *args)):
                raise NodeError(
                    f"{self.name}: docker compose {' '.join(args)} failed ({stack.name})"
                )
            return
        proc = self.remote.stream(docker.compose_command(home, stack, *args, progress="plain"))
        assert proc.stdout is not None
        lines = []
        for raw in proc.stdout:
            line = raw.strip()
            if line:
                lines.append(line)
                if watch is not None:
                    watch(line)
        if proc.wait():
            shown = "\n".join(f"    {line}" for line in lines[-20:])
            raise NodeError(
                f"{self.name}: docker compose {' '.join(args)} failed ({stack.name}):\n{shown}"
            )

    def upload(self, local: Path, dest: str) -> None:
        self.remote.upload(local, dest)

    def exists(self, path: str) -> bool:
        return execute(["test", "-e", path], "/", self.remote).code == 0

    def helper(self, root: str, script: str, *args: str, read_only: bool = False) -> str:
        """A script in the helper container (as root), with ``root`` mounted at /x."""
        return self.run(*docker.helper(root, script, *args, read_only=read_only))

    def move(self, now: str, new: str) -> Callable[[], object]:
        """
        Move a directory (as root: app data is often root-owned). Parents of ``new`` are created.

        On one filesystem it is a rename. To another, it is copied and checked, and ``now`` stays
        until the apply has succeeded (``delete_moved_away``), so undoing only drops the copy.

        Args:
            now (str):
                The directory.
            new (str):
                Where it goes (nothing there yet).

        Returns:
            Callable[[], object]:
                What undoes the move.

        Raises:
            NodeError:
                It failed, or the copy differs from the original (the copy is removed).
        """
        root = posixpath.commonpath([now, new])
        inside = [f"/x/{posixpath.relpath(path, root)}" for path in (now, new)]
        if self.helper(root, _MOVE, *inside).strip() != "elsewhere":
            return partial(self.move, new, now)
        logger.debug(
            "%s: %s and %s are on different filesystems: copy and check", self.name, now, new
        )
        if root in (now, new):
            raise NodeError(f"{self.name}: cannot move {now} to {new}: one is inside the other")
        self.copy(now, new)
        if self.sums(now) != self.sums(new):
            self.remove(new)
            raise NodeError(f"{self.name}: the copy of {now} at {new} differs from the original")
        self.moved_away.append(now)
        return partial(self._drop_copy, now, new)

    def _drop_copy(self, now: str, new: str) -> None:
        self.remove(new)
        self.moved_away.remove(now)

    def remove(self, path: str) -> None:
        """Delete a directory (as root)."""
        folder, name = posixpath.split(path)
        self.helper(folder, 'rm -rf -- "/x/$1"', name)

    def delete_moved_away(self) -> list[str]:
        """Delete the originals of the directories copied to another filesystem. Returns the
        problems (the apply has succeeded: they are only reported)."""
        problems = []
        for path in self.moved_away:
            try:
                self.remove(path)
            except NodeError as exc:
                problems.append(f"{path} was copied, but is still there: {exc}")
        self.moved_away = []
        return problems

    def data_state(self, appdata: str, key: str) -> str:
        """ "missing", "empty" or "data" for APPDATA_DIR/key."""
        return self.helper(appdata, _STATE, key).strip()

    def checksums(self, root: str, paths: list[str]) -> str:
        return self.helper(root, _SUMS, *paths, read_only=True)

    # A data "spec" is what docker -v takes: a host directory, or a volume's name. Mounting a
    # volume that does not exist creates it, so a source volume is checked first.

    def sums(self, spec: str) -> str:
        """Checksums of every file in a directory or volume, relative to it."""
        return self.helper(spec, _RELATIVE_SUMS, read_only=True)

    def copy(self, source: str, target: str) -> None:
        """Copy the contents of ``source`` into ``target`` (as root, ownership kept)."""
        self.run(*docker.copy(source, target))

    def has_data(self, spec: str) -> bool:
        return self.helper(spec, '[ -n "$(ls -A /x)" ] && echo data || true').strip() == "data"

    def volume_exists(self, name: str) -> bool:
        return execute(docker.volume_inspect(name), "/", self.remote).code == 0

    def volume_labels(self, name: str) -> dict[str, str]:
        out = self.run(*docker.volume_labels(name))
        return {str(k): str(v) for k, v in (json.loads(out or "null") or {}).items()}

    def volume_create(self, name: str, labels: Mapping[str, str]) -> None:
        self.run(*docker.volume_create(name, labels))

    def volume_remove(self, name: str) -> None:
        self.run(*docker.volume_remove(name))

    def volume_users(self, name: str) -> list[str]:
        """Containers (any state) that mount the volume."""
        return self.run(*docker.volume_users(name)).split()

    def log(self, step: str) -> None:
        """Note a step in the lock, so an interrupted run shows how far it got."""
        self.script(f'echo "$1" >> "$HOME/{LOCK}/steps"', step)


#############################################################################

# The apply lock

#############################################################################


def lock(remote: Remote, owner: str) -> None:
    """
    Take the node's apply lock (``~/.labctl-lock``), naming its owner.

    Args:
        remote (Remote):
            The node's shell.
        owner (str):
            Who holds it and since when, shown to the next apply.

    Raises:
        NodeError:
            The lock is held: another apply is running, or one was interrupted.
    """
    script = f'mkdir "$HOME/{LOCK}" && printf "%s\\n" "$1" > "$HOME/{LOCK}/owner"'
    if execute(["sh", "-c", script, "sh", owner], "/", remote).code:
        raise NodeError(
            f"{remote.host}: ~/{LOCK} exists: another apply is running, or one was interrupted "
            f"(see `labctl plan {remote.host}`; once nothing runs there, `labctl unlock "
            f"{remote.host}`)"
        )


def unlock(remote: Remote) -> None:
    execute(["sh", "-c", f'rm -rf "$HOME/{LOCK}"'], "/", remote)


@dataclass(frozen=True)
class HeldLock:
    """A node's apply lock: who took it and when, and the steps its run recorded."""

    owner: str
    steps: tuple[str, ...]


def read_lock(remote: Remote) -> HeldLock | None:
    """
    The node's apply lock, or None when it is not locked.

    Args:
        remote (Remote):
            The node's shell.

    Raises:
        RemoteError:
            The node cannot be reached, or the lock cannot be read.
    """
    script = (
        f'[ -d "$HOME/{LOCK}" ] || exit 3; '
        f'cat "$HOME/{LOCK}/owner" 2>/dev/null; echo {MARK}; cat "$HOME/{LOCK}/steps" 2>/dev/null; '
        "exit 0"
    )
    result = execute(["sh", "-c", script], "/", remote)
    if result.code == 3:
        return None
    if result.code:
        raise RemoteError(f"{remote.host}: cannot read ~/{LOCK}: {result.stderr.strip()}")
    owner, _, steps = result.stdout.partition(f"{MARK}\n")
    return HeldLock(
        owner.strip() or "unknown", tuple(line for line in steps.splitlines() if line.strip())
    )
