"""Executing a plan on the nodes.

One run, in phases across the nodes it changes:

0. conflicts (see runtime.py), when the plan has any and they were confirmed: compose projects from
   elsewhere named like one of the node's stacks are taken down; containers in the way are
   stopped, their restart policy set to "no", and renamed when they hold a name a service needs.
1. migrates: on the source, stop the services that leave; copy their data (tar in the helper
   container, the source mounted read-only, relayed through this machine) into the target's
   APPDATA_DIR/.labctl/incoming/; compare checksums; rename it into place. A named volume is
   copied straight into the target's volume, created first with the labels compose looks for.
   The source copy is untouched until phase 3.
   restores: archived data back for services added (restore.py): on the same node a directory is
   moved back out of the archive; otherwise a copy, checked, the archive entry kept.
2. per node:
   a. stop: take down the stacks that go away (all of them when anything moves on the node), if
      they have containers;
   b. moves: root moves and data renames (a rename; to another filesystem, a checked copy whose
      original is deleted once the whole apply succeeded); a rename with a
      volume on either side (a service in another stack mounts another volume) is a copy;
   c. files: every top-level name in DEPLOY_DIR except .labctl and data goes to
      DEPLOY_DIR/.labctl/previous/ (the config backup), the bundle takes their place;
   d. up: the stacks that run after apply (see runtime.py; `up -d --remove-orphans`, compose
      itself recreates only what changed), then wait until their services run (and are healthy,
      where they have a healthcheck). Services publishing a port held outside Docker, and stopped
      services their restart policy leaves down, are created, not started. The other stacks get
      their files only (checked with `config`): a stack that was down stays down.
3. archive: data of removed services, the sources of migrates and the old side of volume renames
   go to APPDATA_DIR/.labctl/archive/<path>-<timestamp> (a volume: copied to volume-<name>-<ts>
   there, checked, then removed; one another container still uses stays). Each entry gets a sidecar,
   <entry>.labctl.json, saying what it held, for restoring it. Names the bundle does not have
   (stack files left from before, ...) go from .labctl/previous to
   DEPLOY_DIR/.labctl/archive/<name>-<timestamp>.
4. commit, then record: everything is up, so the caller commits what was applied (``commit_with``:
   the planned snapshot, on HEAD; nothing is committed before this point), then
   .labctl/applied.json (naming that commit and its apply tag), .labctl/managed and ~/.labctl-home
   on each node. The caller moves the branch and tags the commit once the nodes are unlocked. A
   node with nothing to change whose record names no commit this clone has (or none) gets the
   record only.

Every step that changes a node pushes its undo; on failure all of them run in reverse, so every node
of the run ends as it was: stacks that were up come back up, stacks that were down come back with
their containers stopped. Data is never deleted: only renamed (and renamed back on undo), a volume
removed only once its archive copy checks out (and recreated from it on undo), a volume this run
created removed on undo, and a migrate's copy that does not stay is archived as
`<path>-failed-<timestamp>`.
"""

from __future__ import annotations

import json
import logging
import posixpath
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

from rich.markup import escape

from labctl.bundle import Desired, stack_args, top_level
from labctl.checks import archive_name, key_label, move_paths
from labctl.collect import (
    FILES_ARCHIVE,
    HOME_POINTER,
    LOCK,
    MANAGED,
    RECORD,
    Collected,
    make_record,
)
from labctl.compose import ARCHIVE_DIR
from labctl.moves import VOLUME, Move
from labctl.planner import Migrate, Plan, Remove, Restore, archived_keys
from labctl.progress import Progress, Step
from labctl.remote import MARK, Remote, RemoteError, execute, node_remote, relay
from labctl.repo import tag_name
from labctl.restore import SIDECAR
from labctl.runtime import NodeRuntime
from labctl.settings import Settings
from labctl.stack import (
    SNAPSHOT_COMMAND,
    Snapshot,
    compose_command,
    container_changes,
    not_up_again,
    ps_rows,
    snapshot,
)
from labctl.state import (
    NodeState,
    Scope,
    Service,
    Stack,
    is_volume,
    under,
    volume_key,
    volume_name,
)

logger = logging.getLogger(__name__)

# $1: DEPLOY_DIR, then the names to set aside. The bundle is in .labctl/incoming.
_SWAP_IN = """\
set -e
cd "$1"; shift
rm -rf .labctl/previous; mkdir .labctl/previous
for name in "$@"; do
  if [ -e "$name" ] || [ -L "$name" ]; then mv -T -- "$name" ".labctl/previous/$name"; fi
done
for path in .labctl/incoming/* .labctl/incoming/.[!.]*; do
  if [ -e "$path" ] || [ -L "$path" ]; then mv -T -- "$path" "${path#.labctl/incoming/}"; fi
done
rmdir .labctl/incoming
"""

# $1: DEPLOY_DIR, then the bundle's names. Undoes _SWAP_IN.
_SWAP_OUT = """\
set -e
cd "$1"; shift
for name in "$@"; do rm -rf -- "$name"; done
for path in .labctl/previous/* .labctl/previous/.[!.]*; do
  if [ -e "$path" ] || [ -L "$path" ]; then mv -T -- "$path" "${path#.labctl/previous/}"; fi
done
"""

# (label, action) for each change made so far, undone in reverse on failure.
Undo = list[tuple[str, Callable[[], object]]]

# Runs file work on a node as root (GNU tar and coreutils). Pinned by digest (multi-arch index).
HELPER = (
    "debian:bookworm-slim@sha256:99c0b25c46d0214d5a2091092e7908907dcaecd6a962cde0ac700acee222b6f3"
)

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

_TAR = ["--numeric-owner", "--xattrs", "--acls", "--sparse"]


class ApplyError(Exception):
    pass


class RollbackFailed(ApplyError):
    """Apply failed and undoing it did too: the nodes need a look (their locks stay)."""


@dataclass(frozen=True)
class NodeWork:
    """What apply does on one node."""

    node: str
    desired: Desired
    old_home: str  # where the node's files are now
    old_appdata: str  # where its data is now
    old_stacks: tuple[Stack, ...]  # the stacks in DEPLOY_DIR now
    old_projects: frozenset[str]  # those of them (by name) that have containers
    old_stack_of: Mapping[str, str]  # service -> its stack now
    moves: tuple[tuple[Move, str, str], ...]  # (move, path now, new path), in order
    # (data key, why): removed services' data, migrates leaving, the old side of a volume rename
    archive: tuple[tuple[str, str], ...]
    replace: tuple[str, ...]  # top-level names the bundle replaces or removes
    discard: tuple[str, ...]  # of those, the ones the bundle does not have: archived
    keep_drift: bool  # files were edited on the node: archive them before they are replaced
    # What runs: which stacks are up after apply, and what is in the way (runtime.analyse).
    runtime: NodeRuntime = NodeRuntime()
    # Nothing to change, but the node's record names no commit yet: only the record is written.
    record_only: bool = False
    old_services: Mapping[str, Service] = field(default_factory=dict)  # as recorded (sidecars)


def node_work(plan: Plan, collected: Collected, scope: Scope) -> list[NodeWork]:
    """
    What apply does on each in-scope node it changes.

    Args:
        plan (Plan):
            The plan.
        collected (Collected):
            What plan collected.
        scope (Scope):
            The nodes that may change.

    Returns:
        list[NodeWork]:
            In name order. ``runtime`` is filled in afterwards: what is in the way depends on which
            nodes change.

    Raises:
        ApplyError:
            A migrate and a move on one node (not supported yet).
    """
    drifted = {drift.node for drift in collected.drift}
    reports = {report.node: report.source for report in collected.reports}
    moved = {move.node for move in plan.moves}
    work = []
    for node, desired in sorted(collected.desired.items()):
        recorded = collected.recorded.get(node)
        if not scope.in_scope(node) or recorded is None:
            continue
        on_node = collected.node_files.get(node, {})
        ops = [op for op in plan.operations if node in op.nodes]
        migrates = [op for op in ops if isinstance(op, Migrate)]
        if migrates and node in moved:
            raise ApplyError(
                f"{node}: a migrate and a move on the same node in one apply is not supported "
                "yet; apply the move first"
            )
        moves = tuple(
            (move, now, new)
            for move, now, new in move_paths(plan.moves)
            if move.node == node and not move.carried
        )
        present = {
            name for name, kind in collected.node_names.get(node, {}).items() if kind != "data"
        } | set(top_level(on_node))
        discard = tuple(sorted(present - set(desired.top_level)))
        changed = dict(on_node) != desired.files or reports.get(node) != "node" or bool(discard)
        record_only = not (ops or moves or changed)
        if record_only and node not in collected.untracked:
            continue
        old_dirs = collected.recorded_dirs.get(node, desired.dirs)
        archive = [(key, "removed") for op in ops if isinstance(op, Remove) for key in op.archive]
        archive += [(key, "migrated") for op in migrates if op.source == node for key in op.archive]
        archive += [(move.old, "renamed") for move, _, _ in moves if move.kind == VOLUME]
        work.append(
            NodeWork(
                node=node,
                desired=desired,
                old_home=old_dirs.deploy_dir,
                old_appdata=old_dirs.appdata_dir,
                old_stacks=tuple(recorded.stacks.values()),
                old_projects=collected.projects.get(node, frozenset()),
                old_stack_of={name: s.stack for name, s in recorded.services.items()},
                moves=moves,
                archive=tuple(archive),
                replace=tuple(sorted(present | set(desired.top_level))),
                discard=discard,
                keep_drift=node in drifted,
                record_only=record_only,
                old_services=dict(recorded.services),
            )
        )
    return work


class Node:
    """The commands apply runs on one node. Every one raises ApplyError when it fails."""

    def __init__(self, remote: Remote) -> None:
        self.remote = remote
        self.name = remote.host
        # Directories moved to another filesystem: copied, and deleted once the apply succeeded.
        self.moved_away: list[str] = []

    def run(self, *args: str, cwd: str = "/") -> str:
        result = execute(args, cwd, self.remote)
        if result.code:
            raise ApplyError(f"{self.name}: {' '.join(args)}: {result.stderr.strip()}")
        return result.stdout

    def snapshot(self) -> Snapshot:
        """The node's compose containers now."""
        return snapshot(self.run(*SNAPSHOT_COMMAND))

    def script(self, script: str, *args: str) -> None:
        self.run("sh", "-c", script, "sh", *args)

    def compose(
        self, home: str, stack: Stack, *args: str, watch: Callable[[str], None] | None = None
    ) -> None:
        """A compose command. Its output is read, line by line (to ``watch``), and shown only when
        it fails; with debug logging it goes straight to the terminal."""
        if logging.getLogger().isEnabledFor(logging.DEBUG):
            if self.remote.run(compose_command(home, stack, *args)):
                raise ApplyError(
                    f"{self.name}: docker compose {' '.join(args)} failed ({stack.name})"
                )
            return
        proc = self.remote.stream(compose_command(home, stack, *args, progress="plain"))
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
            raise ApplyError(
                f"{self.name}: docker compose {' '.join(args)} failed ({stack.name}):\n{shown}"
            )

    def upload(self, local: Path, dest: str) -> None:
        self.remote.upload(local, dest)

    def exists(self, path: str) -> bool:
        return execute(["test", "-e", path], "/", self.remote).code == 0

    def helper(self, root: str, script: str, *args: str, read_only: bool = False) -> str:
        """A script in the helper container (as root), with ``root`` mounted at /x."""
        mount = f"{root}:/x{':ro' if read_only else ''}"
        return self.run(
            "docker", "run", "--rm", "-v", mount, HELPER, "sh", "-c", script, "sh", *args
        )

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
            ApplyError:
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
            raise ApplyError(f"{self.name}: cannot move {now} to {new}: one is inside the other")
        self.copy(now, new)
        if self.sums(now) != self.sums(new):
            self.remove(new)
            raise ApplyError(f"{self.name}: the copy of {now} at {new} differs from the original")
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
            except ApplyError as exc:
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
        tar = " ".join(_TAR)
        self.run(
            "docker", "run", "--rm", "-v", f"{source}:/s:ro", "-v", f"{target}:/d", HELPER,
            "sh", "-c", f"tar -C /s {tar} -cpf - . | tar -C /d {tar} -xpf -",
        )  # fmt: skip

    def has_data(self, spec: str) -> bool:
        return self.helper(spec, '[ -n "$(ls -A /x)" ] && echo data || true').strip() == "data"

    def volume_exists(self, name: str) -> bool:
        return execute(["docker", "volume", "inspect", name], "/", self.remote).code == 0

    def volume_labels(self, name: str) -> dict[str, str]:
        out = self.run("docker", "volume", "inspect", "--format", "{{json .Labels}}", name)
        return {str(k): str(v) for k, v in (json.loads(out or "null") or {}).items()}

    def volume_create(self, name: str, labels: Mapping[str, str]) -> None:
        flags = [arg for k, v in sorted(labels.items()) for arg in ("--label", f"{k}={v}")]
        self.run("docker", "volume", "create", *flags, name)

    def volume_remove(self, name: str) -> None:
        self.run("docker", "volume", "rm", name)

    def volume_users(self, name: str) -> list[str]:
        """Containers (any state) that mount the volume."""
        out = self.run(
            "docker", "ps", "--all", "--filter", f"volume={name}", "--format", "{{.Names}}"
        )
        return out.split()

    def log(self, step: str) -> None:
        """Note a step in the lock, so an interrupted run shows how far it got."""
        self.script(f'echo "$1" >> "$HOME/{LOCK}/steps"', step)


def mount_spec(root: str, key: str) -> str:
    """A data key as docker -v takes it: the volume's name, or the directory under ``root``."""
    return volume_name(key) if is_volume(key) else f"{root}/{key}"


def compose_labels(state: NodeState, key: str) -> dict[str, str]:
    """The labels compose looks for on a volume it is to use as its own (none: external)."""
    volume = state.volumes.get(volume_name(key))
    if volume is None or volume.external:
        return {}
    return {"com.docker.compose.project": volume.stack, "com.docker.compose.volume": volume.key}


def lock(remote: Remote, owner: str) -> None:
    """
    Take the node's apply lock (``~/.labctl-lock``), naming its owner.

    Args:
        remote (Remote):
            The node's shell.
        owner (str):
            Who holds it and since when, shown to the next apply.

    Raises:
        ApplyError:
            The lock is held: another apply is running, or one was interrupted.
    """
    script = f'mkdir "$HOME/{LOCK}" && printf "%s\\n" "$1" > "$HOME/{LOCK}/owner"'
    if execute(["sh", "-c", script, "sh", owner], "/", remote).code:
        raise ApplyError(
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


@dataclass
class Executor:
    settings: Settings
    say: Callable[[str], None]
    wait: int = 120  # seconds for services to come up
    replace_stale: bool = False  # archive data already at a migrate's target
    when: datetime = field(default_factory=datetime.now)
    commit: str = ""  # the config repo's commit applied (named in each record)
    recorded: list[str] = field(default_factory=list)  # nodes whose record was written
    tree: str = ""  # the lab config's tree applied (named in archive sidecars)
    # Makes the commit once everything is up, before the records name it (sets ``commit``).
    commit_with: Callable[[], str] | None = None
    # The steps as they run (default: each finished step printed with ``say``).
    progress: Progress | None = None
    # Each node's containers before this run touched it.
    before: dict[str, Snapshot] = field(default_factory=dict)

    @property
    def steps(self) -> Progress:
        if self.progress is None:
            self.progress = Progress(self.say)
        return self.progress

    @property
    def stamp(self) -> str:
        return self.when.strftime("%Y%m%d-%H%M%S")

    def run(self, nodes: Mapping[str, Node], works: Sequence[NodeWork], plan: Plan) -> None:
        """
        Apply the plan: phase by phase, one undo stack across every node.

        Args:
            nodes (Mapping[str, Node]):
                Each node's shell, by name.
            works (Sequence[NodeWork]):
                What apply does on each node.
            plan (Plan):
                The plan (its migrates and restores).

        Raises:
            ApplyError:
                A step failed; everything this run did was undone.
            RollbackFailed:
                Undoing failed too: the nodes need a look (their locks stay).
        """
        undo: Undo = []
        by_node = {work.node: work for work in works}
        migrates = [op for op in plan.operations if isinstance(op, Migrate)]
        restores = [op for op in plan.operations if isinstance(op, Restore)]
        changes = [work for work in works if not work.record_only]
        for work in changes:
            self.before[work.node] = nodes[work.node].snapshot()
        try:
            with self.steps:
                for work in changes:
                    self._clear_the_way(nodes[work.node], work, undo)
                for index, op in enumerate(migrates):
                    self._migrate(nodes, by_node, op, index, undo)
                for restore in restores:
                    self._restore(nodes, by_node, restore, undo)
                for work in changes:
                    self._node(nodes[work.node], work, undo)
                for work in changes:
                    self._archive(nodes[work.node], work, undo)
            for node in nodes.values():
                for problem in node.delete_moved_away():
                    self.say(f"[yellow]![/] {node.name}: {escape(problem)}")
            self.say("")
            for work in works:
                done = "recorded (nothing to change)" if work.record_only else "applied"
                self.say(f"[green]✓[/] {work.node}: {done}")
            self.say("")
            if self.commit_with is not None:
                self.commit = self.commit_with()
            for work in works:
                self._record(nodes[work.node], work)
        except Exception as exc:
            self.say(f"[bold red]✗[/] {escape(str(exc))}")
            self.say("Rolling back")
            failures = []
            for label, action in reversed(undo):
                self.say(f"  undo: {escape(label)}")
                try:
                    action()
                except Exception as undo_exc:  # keep undoing the rest
                    failures.append(f"{label}: {undo_exc}")
            self._check_rolled_back(nodes, changes)
            if failures:
                raise RollbackFailed(
                    "apply failed and the rollback did too; the locks stay in place:\n  "
                    + "\n  ".join(failures)
                ) from exc
            raise
        for work in changes:
            self._report_not_started(work)

    def _check_rolled_back(self, nodes: Mapping[str, Node], works: Sequence[NodeWork]) -> None:
        """After a rollback: a warning for each service that ran before the apply and does not
        now."""
        for work in works:
            try:
                missing = not_up_again(self.before[work.node], nodes[work.node].snapshot())
            except Exception:  # the node may be the reason for the rollback
                continue
            if missing:
                self.say(
                    f"[yellow]![/] {work.node}: not running as before the apply: "
                    f"{escape(', '.join(missing))}"
                )

    def _as_before(
        self, node: Node, home: str, stack: Stack, was_up: bool, work: NodeWork
    ) -> tuple[str, Callable[[], object]]:
        """The undo that brings a stack back as it was: up (but the services that were stopped),
        or with its containers stopped."""
        if not was_up:
            return (f"{stack.name} back, stopped", partial(self._stopped, node, home, stack))
        services = work.runtime.partly_up.get(stack.name)
        if services is None:
            return (f"up {stack.name}", partial(node.compose, home, stack, "up", "-d"))
        return (f"up {stack.name}", partial(self._partly_up, node, home, stack, services))

    @staticmethod
    def _stopped(node: Node, home: str, stack: Stack) -> None:
        node.compose(home, stack, "up", "--no-start", "--remove-orphans")
        node.compose(home, stack, "stop")

    @staticmethod
    def _partly_up(node: Node, home: str, stack: Stack, services: Sequence[str]) -> None:
        node.compose(home, stack, "up", "--no-start", "--remove-orphans")
        node.compose(home, stack, "up", "-d", *services)

    ##########################################################################

    # Phase 0: what is in the way

    ##########################################################################

    def _clear_the_way(self, node: Node, work: NodeWork, undo: Undo) -> None:
        for down in work.runtime.take_down:
            project = Stack(down.project, down.files)
            node.log(f"down {down.project} in {down.working_dir}")
            with self.steps.step(work.node, f"down {down.project} (from {down.working_dir})"):
                node.compose(down.working_dir, project, "down")
            undo.append(self._as_before(node, down.working_dir, project, down.running, work))
        for stop in work.runtime.stops:
            name, restart = stop.container.name, stop.container.restart
            node.log(f"stop {name}")
            self.steps.note(f"{work.node}: stop {name}")
            if restart != "no":
                node.run("docker", "update", "--restart=no", name)
                undo.append(
                    (
                        f"restart policy of {name} back to {restart}",
                        partial(node.run, "docker", "update", f"--restart={restart}", name),
                    )
                )
            if stop.container.up:
                node.run("docker", "stop", name)
                undo.append((f"start {name}", partial(node.run, "docker", "start", name)))
            if stop.rename:
                aside = f"{name}-labctl-{self.stamp}"
                self.steps.note(f"{work.node}: rename {name} → {aside}")
                node.run("docker", "rename", name, aside)
                undo.append(
                    (f"rename {aside} back", partial(node.run, "docker", "rename", aside, name))
                )

    def _report_not_started(self, work: NodeWork) -> None:
        services = sorted(work.runtime.defer)
        if services:
            self.say(
                f"[yellow]![/] {work.node}: {', '.join(services)} created, not started (a port is "
                f"held outside Docker); once it is free: labctl node up {work.node} "
                + " ".join(f"-s {service}" for service in services)
            )
        for stack in work.desired.state.stacks:
            if stack not in work.runtime.start:
                target = f"{work.node}/{stack}"
                self.say(
                    f"{target} was not running, so we didn't automatically start it. Use labctl "
                    f"node up {target} to start it."
                )

    ##########################################################################

    # Phase 1: migrates

    ##########################################################################

    def _migrate(
        self,
        nodes: Mapping[str, Node],
        by_node: Mapping[str, NodeWork],
        op: Migrate,
        index: int,
        undo: Undo,
    ) -> None:
        source, target = nodes[op.source], nodes[op.target]
        src, dst = by_node[op.source], by_node[op.target]
        src_root, dst_root = src.old_appdata, dst.desired.dirs.appdata_dir
        to_target = {old: new for new, old in op.renamed}
        pairs = [(key, to_target.get(key, key)) for key in archived_keys(op.source_data)]
        # Directory to directory: one tar of them all. A volume on either side: one stream each.
        keys = [k for k, new in pairs if not is_volume(k) and not is_volume(new)]
        streams = [(k, new) for k, new in pairs if is_volume(k) or is_volume(new)]
        label = f"{', '.join(op.added)} {op.source} → {op.target}"

        # Stop what leaves the source (its data must not change while it is copied); a stack that
        # is down has nothing running.
        stacks: dict[str, list[str]] = {}
        for service in op.removed:
            stacks.setdefault(src.old_stack_of[service], []).append(service)
        for name, services in stacks.items():
            if name not in src.runtime.running:
                continue
            stack = next(old_stack for old_stack in src.old_stacks if old_stack.name == name)
            source.log(f"stop {' '.join(services)}")
            self.steps.note(f"{op.source}: stop {', '.join(services)}")
            source.compose(src.old_home, stack, "stop", *services)
            undo.append(
                (
                    f"start {' '.join(services)} on {op.source}",
                    partial(source.compose, src.old_home, stack, "start", *services),
                )
            )

        # Copy into the target's staging dir (a volume: straight into it), check it arrived intact.
        staging = f".labctl/incoming/{op.source}-{self.stamp}-{index}"
        target.log(f"copy {label}")
        self.steps.note(f"migrate {label}: copying {', '.join(key_label(k) for k, _ in pairs)}")
        undo.append(
            (
                f"archive the partial copy on {op.target}",
                partial(self._archive_if_there, target, dst_root, staging, "incoming-failed"),
            )
        )
        staged: list[tuple[str, str]] = []  # (name in staging, target key): put in place below
        if keys:
            relay(
                source.remote,
                [
                    "docker",
                    "run",
                    "--rm",
                    "-v",
                    f"{src_root}:/src:ro",
                    HELPER,
                    "tar",
                    "-C",
                    "/src",
                    *_TAR,
                    "-cpf",
                    "-",
                    "--",
                    *keys,
                ],
                target.remote,
                [
                    "docker",
                    "run",
                    "--rm",
                    "-i",
                    "-v",
                    f"{dst_root}:/dst",
                    HELPER,
                    "sh",
                    "-c",
                    'mkdir -p "/dst/$1" && tar -C "/dst/$1" ' + " ".join(_TAR) + " -xpf -",
                    "sh",
                    staging,
                ],
            )
            sent = source.checksums(src_root, keys)
            received = target.checksums(f"{dst_root}/{staging}", keys)
            if sent != received:
                raise ApplyError(
                    f"migrate {label}: the copy on {op.target} differs from the source"
                )
            self.steps.note(
                f"[green]✓[/] migrate {label}: verified ({len(sent.splitlines())} files)"
            )
            staged += [(key, to_target.get(key, key)) for key in keys]
        for key, new_key in streams:
            spec = mount_spec(src_root, key)
            if not self._data_there(source, src_root, key):
                self.steps.note(
                    f"[yellow]![/] migrate {label}: {key_label(key)} is not on {op.source}; "
                    "nothing to copy"
                )
                continue
            if is_volume(new_key):
                self._make_room(
                    target, dst_root, new_key, compose_labels(dst.desired.state, new_key), undo, dst
                )
                dest = volume_name(new_key)
                write = ["tar", "-C", "/dst", *_TAR, "-xpf", "-"]
                mount = f"{dest}:/dst"
            else:
                name = archive_name(key)
                dest = f"{dst_root}/{staging}/{name}"
                extract = 'mkdir -p "/dst/$1" && tar -C "/dst/$1" ' + " ".join(_TAR) + " -xpf -"
                write = ["sh", "-c", extract, "sh", f"{staging}/{name}"]
                mount = f"{dst_root}:/dst"
                staged.append((name, new_key))
            relay(
                source.remote,
                ["docker", "run", "--rm", "-v", f"{spec}:/src:ro", HELPER, "tar", "-C", "/src",
                 *_TAR, "-cpf", "-", "."],
                target.remote,
                ["docker", "run", "--rm", "-i", "-v", mount, HELPER, *write],
            )  # fmt: skip
            sent = source.sums(spec)
            if sent != target.sums(dest):
                raise ApplyError(
                    f"migrate {label}: the copy of {key_label(key)} on {op.target} differs from "
                    "the source"
                )
            self.steps.note(
                f"[green]✓[/] migrate {label}: {key_label(new_key)} verified "
                f"({len(sent.splitlines())} files)"
            )

        # Into place, after whatever is already at the target paths.
        for name, new_key in staged:
            state = target.data_state(dst_root, new_key)
            if state == "data":
                if not self.replace_stale:
                    raise ApplyError(
                        f"migrate {label}: {dst_root}/{new_key} already has data (--replace-stale)"
                    )
                aside = f"{dst_root}/{ARCHIVE_DIR}/{new_key}-{self.stamp}"
                back = target.move(f"{dst_root}/{new_key}", aside)
                undo.append((f"restore {new_key} on {op.target}", back))
                self._sidecar(target, dst_root, new_key, "replaced", dst.old_services)
            elif state == "empty":
                target.helper(dst_root, 'rmdir "/x/$1"', new_key)
                undo.append(
                    (
                        f"recreate {new_key} on {op.target}",
                        partial(target.helper, dst_root, 'mkdir -p "/x/$1"', new_key),
                    )
                )
            target.log(f"place {new_key}")
            final = f"{dst_root}/{new_key}"
            target.move(f"{dst_root}/{staging}/{name}", final)
            undo.append(
                (
                    f"archive the copy of {new_key} on {op.target}",
                    partial(
                        target.move,
                        final,
                        f"{dst_root}/{ARCHIVE_DIR}/{new_key}-failed-{self.stamp}",
                    ),
                )
            )
        if target.exists(f"{dst_root}/{staging}"):
            target.helper(dst_root, 'find "/x/$1" -depth -type d -empty -delete', staging)

    def _data_there(self, node: Node, root: str, key: str) -> bool:
        if is_volume(key):
            return node.volume_exists(volume_name(key))
        return node.exists(f"{root}/{key}")

    def _make_room(
        self,
        node: Node,
        root: str,
        key: str,
        labels: Mapping[str, str],
        undo: Undo,
        work: NodeWork,
    ) -> None:
        """An empty place at ``key`` for data copied there. Data already there is archived first
        (only with --replace-stale); a volume is created with the labels compose expects."""
        if is_volume(key):
            name = volume_name(key)
            if node.volume_exists(name):
                if not node.has_data(name):
                    return  # empty: compose (or Docker) made it; used as it is
                self._refuse_stale(node, key)
                self._archive_volume(node, root, name, "replaced", work.old_services, undo)
                self.steps.note(f"{node.name}: volume {name} was in the way; archived")
            node.volume_create(name, labels)
            undo.append((f"remove volume {name} on {node.name}", partial(node.volume_remove, name)))
            return
        self._clear(node, root, key, undo, work)
        node.helper(root, 'mkdir -p "/x/$1"', key)
        undo.append(
            (
                f"archive the copy of {key} on {node.name}",
                partial(self._archive_if_there, node, root, key, f"{key}-failed"),
            )
        )

    def _clear(self, node: Node, root: str, key: str, undo: Undo, work: NodeWork) -> None:
        """Nothing at the directory ``key``: data is archived (--replace-stale), an empty
        directory (Docker's, for a missing bind source) removed."""
        state = node.data_state(root, key)
        if state == "data":
            self._refuse_stale(node, key)
            aside = f"{root}/{ARCHIVE_DIR}/{key}-{self.stamp}"
            back = node.move(f"{root}/{key}", aside)
            undo.append((f"restore {key} on {node.name}", back))
            self._sidecar(node, root, key, "replaced", work.old_services)
        elif state == "empty":
            node.helper(root, 'rmdir "/x/$1"', key)
            undo.append(
                (
                    f"recreate {key} on {node.name}",
                    partial(node.helper, root, 'mkdir -p "/x/$1"', key),
                )
            )

    def _refuse_stale(self, node: Node, key: str) -> None:
        if not self.replace_stale:
            raise ApplyError(f"{node.name}: {key_label(key)} already has data (--replace-stale)")

    ##########################################################################

    # Phase 1b: restores

    ##########################################################################

    def _restore(
        self, nodes: Mapping[str, Node], by_node: Mapping[str, NodeWork], op: Restore, undo: Undo
    ) -> None:
        """Archived data into place before the service starts. On the same node a directory is
        moved back (it leaves the archive); otherwise a copy, checked, the archive kept."""
        target, work = nodes[op.node], by_node[op.node]
        source = nodes.get(op.source) or Node(node_remote(op.source))  # only read
        root = work.desired.dirs.appdata_dir
        label = f"{op.service} on {op.node}"
        for entry, key in op.entries:
            archived = f"{op.root}/{ARCHIVE_DIR}/{entry}"
            target.log(f"restore {entry} to {key}")
            self.steps.note(
                f"restore {label}: {key_label(key)} from {op.source}'s {ARCHIVE_DIR}/{entry}"
            )
            if op.source == op.node and not is_volume(key):
                self._clear(target, root, key, undo, work)
                back = target.move(archived, f"{root}/{key}")
                undo.append((f"put {entry} back in {op.node}'s archive", back))
                # Its description stays, renamed: the entry is no longer in the archive.
                folder, name = posixpath.split(archived)
                done = f"{SIDECAR}.restored"
                target.helper(folder, f'mv -- "/x/$1{SIDECAR}" "/x/$1{done}"', name)
                undo.append(
                    (
                        f"describe {entry} again",
                        partial(
                            target.helper, folder, f'mv -- "/x/$1{done}" "/x/$1{SIDECAR}"', name
                        ),
                    )
                )
                continue
            self._make_room(target, root, key, compose_labels(work.desired.state, key), undo, work)
            spec = mount_spec(root, key)
            if op.source == op.node:
                target.copy(archived, spec)
            else:
                relay(
                    source.remote,
                    ["docker", "run", "--rm", "-v", f"{archived}:/src:ro", HELPER, "tar", "-C",
                     "/src", *_TAR, "-cpf", "-", "."],
                    target.remote,
                    ["docker", "run", "--rm", "-i", "-v", f"{spec}:/dst", HELPER, "tar", "-C",
                     "/dst", *_TAR, "-xpf", "-"],
                )  # fmt: skip
            sent = source.sums(archived)
            if sent != target.sums(spec):
                raise ApplyError(f"restore {label}: the copy of {entry} differs from the archive")
            self.steps.note(
                f"[green]✓[/] restore {label}: {key_label(key)} verified "
                f"({len(sent.splitlines())} files)"
            )

    def _archive_volume(
        self,
        node: Node,
        root: str,
        name: str,
        reason: str,
        services: Mapping[str, Service],
        undo: Undo,
    ) -> None:
        """Copy a volume into the APPDATA_DIR archive (checked), then remove it."""
        key = volume_key(name)
        entry = f"{ARCHIVE_DIR}/{archive_name(key)}-{self.stamp}"
        node.log(f"archive volume {name}")
        node.helper(root, 'mkdir -p "/x/$1"', entry)
        node.copy(name, f"{root}/{entry}")
        if node.sums(name) != node.sums(f"{root}/{entry}"):
            raise ApplyError(f"{node.name}: the archive copy of volume {name} differs from it")
        labels = node.volume_labels(name)
        self._sidecar(node, root, key, reason, services, labels)
        node.volume_remove(name)
        undo.append(
            (
                f"bring volume {name} back on {node.name}",
                partial(self._unarchive_volume, node, root, name, entry, labels),
            )
        )

    @staticmethod
    def _unarchive(node: Node, entry: str, back: Callable[[], object]) -> None:
        """An archived directory back where it was (``back`` undoes its move); its sidecar goes."""
        back()
        folder, name = posixpath.split(entry)
        node.helper(folder, f'rm -f -- "/x/$1{SIDECAR}"', name)

    @staticmethod
    def _unarchive_volume(
        node: Node, root: str, name: str, entry: str, labels: Mapping[str, str]
    ) -> None:
        node.volume_create(name, labels)
        node.copy(f"{root}/{entry}", name)
        node.helper(root, f'rm -rf -- "/x/$1" "/x/$1{SIDECAR}"', entry)

    def _sidecar(
        self,
        node: Node,
        root: str,
        key: str,
        reason: str,
        services: Mapping[str, Service],
        labels: Mapping[str, str] | None = None,
    ) -> None:
        """Describe an archive entry next to it: what it held and where, for restoring it."""
        mounts = [
            {"service": service.name, "stack": service.stack, "target": target, "key": k}
            for service in services.values()
            for target, k in service.mounts
            if k == key or (not is_volume(key) and under(k, key))
        ]
        data: dict[str, Any] = {
            "version": 1,
            "node": node.name,
            "stamp": self.stamp,
            "reason": reason,
            "key": key,
            "tree": self.tree,
            "mounts": mounts,
        }
        if labels:
            data["labels"] = dict(labels)
        entry = f"{ARCHIVE_DIR}/{archive_name(key)}-{self.stamp}{SIDECAR}"
        node.helper(root, 'printf "%s\\n" "$2" > "/x/$1"', entry, json.dumps(data, sort_keys=True))

    def _archive_if_there(self, node: Node, root: str, path: str, name: str) -> None:
        if node.exists(f"{root}/{path}"):
            node.move(f"{root}/{path}", f"{root}/{ARCHIVE_DIR}/{name}-{self.stamp}")

    ##########################################################################

    # Phase 2: per node

    ##########################################################################

    def _node(self, node: Node, work: NodeWork, undo: Undo) -> None:
        desired = work.desired
        home = desired.dirs.deploy_dir
        wanted = list(desired.state.stacks.values())
        was_up, start = work.runtime.running, work.runtime.start

        # a. Stop. A stack without containers (a file left from before) has nothing to stop.
        active = [old_stack for old_stack in work.old_stacks if old_stack.name in work.old_projects]
        going = [stack for stack in active if stack.name not in desired.state.stacks]
        stopping = active if work.moves else going
        for stack in reversed(stopping):
            node.log(f"down {stack.name}")
            with self.steps.step(work.node, f"down {stack.name}") as step:
                node.compose(work.old_home, stack, "down", watch=step.update)
            undo.append(self._as_before(node, work.old_home, stack, stack.name in was_up, work))

        # b. Moves. A volume rename is a copy; the old side is archived with the rest (phase 3).
        for move, now, new in work.moves:
            if move.kind == VOLUME:
                self._copy_renamed(node, work, move.old, move.new, undo)
                continue
            node.log(f"move {now} -> {new}")
            with self.steps.step(work.node, f"move {now} → {new}"):
                back = node.move(now, new)
            undo.append((f"move {new} back", back))

        # c. Files. On undo, once the old files are back, the stacks that kept their containers and
        # are brought up below come back as they were (compose recreates what apply changed).
        kept = [stack for stack in active if stack not in stopping]
        for stack in kept:
            if stack.name in start:
                undo.append(self._as_before(node, home, stack, stack.name in was_up, work))
        node.log("files")
        with self.steps.step(work.node, "files"):
            node.run("mkdir", "-p", f"{home}/.labctl")
            node.run("rm", "-rf", f"{home}/.labctl/incoming")
            node.upload(desired.bundle, f"{home}/.labctl/incoming")
            node.script(_SWAP_IN, home, *work.replace)
            undo.append(
                ("restore files", partial(node.script, _SWAP_OUT, home, *desired.top_level))
            )
        if work.keep_drift:  # .labctl/previous is replaced by the next apply; this copy is not
            edits = f"{FILES_ARCHIVE}/node-edits-{self.stamp}"
            node.run("mkdir", "-p", f"{home}/{FILES_ARCHIVE}")
            node.run("cp", "-a", f"{home}/.labctl/previous", f"{home}/{edits}")
            self.steps.note(f"{work.node}: files edited on the node archived to {edits}")

        # d. Up: the stacks that run after apply, each waited for. The others stay down with their
        # new files.
        had_containers = {stack.name for stack in kept}
        deferred = {*work.runtime.defer, *work.runtime.stay_down}
        for stack in wanted:
            if stack.name not in start:
                node.log(f"check {stack.name}")
                with self.steps.step(work.node, f"{stack.name}: files only, stays down"):
                    node.compose(home, stack, "config", "--quiet")
                continue
            node.log(f"up {stack.name}")
            if stack.name not in had_containers:  # a new stack goes again; others are restored
                undo.append((f"down {stack.name}", partial(node.compose, home, stack, "down")))
            services = [s for s, svc in desired.state.services.items() if svc.stack == stack.name]
            held = [service for service in services if service in deferred]
            with self.steps.step(work.node, f"up {stack.name}") as step:
                if not held:
                    node.compose(home, stack, "up", "-d", "--remove-orphans", watch=step.update)
                elif len(held) == len(services):
                    node.compose(
                        home, stack, "up", "--no-start", "--remove-orphans", watch=step.update
                    )
                else:
                    rest = [service for service in services if service not in deferred]
                    node.compose(
                        home, stack, "up", "-d", "--remove-orphans", *rest, watch=step.update
                    )
                    node.compose(home, stack, "up", "--no-start", *held, watch=step.update)
                self._wait_up(node, work, stack, step)
                changes = container_changes(
                    self.before.get(work.node, {}),
                    node.snapshot(),
                    stack.name,
                    came_from=work.old_projects,
                    staying=desired.state.services,
                )
                if changes:
                    step.result = f"{step.result}, {changes}" if step.result else str(changes)

    def _copy_renamed(self, node: Node, work: NodeWork, old: str, new: str, undo: Undo) -> None:
        """Renamed data with a volume on either side: copied (and checked) while it is stopped."""
        root = work.desired.dirs.appdata_dir
        label = f"{key_label(old)} → {key_label(new)}"
        node.log(f"copy {label}")
        if not self._data_there(node, root, old):
            self.steps.note(
                f"[yellow]![/] {work.node}: {key_label(old)} is not there; nothing to copy"
            )
            return
        with self.steps.step(work.node, f"copy {label}"):
            self._make_room(node, root, new, compose_labels(work.desired.state, new), undo, work)
            source, target = mount_spec(root, old), mount_spec(root, new)
            node.copy(source, target)
            if node.sums(source) != node.sums(target):
                raise ApplyError(f"{work.node}: the copy {label} differs from the original")

    def _wait_up(self, node: Node, work: NodeWork, stack: Stack, step: Step) -> None:
        """Until every service of the stack runs (healthy, where it has a healthcheck), but those
        left created and not started; the step says what is still pending."""
        home = work.desired.dirs.deploy_dir
        expected = [
            service.name
            for service in work.desired.state.services.values()
            if service.stack == stack.name
            and service.name not in {*work.runtime.defer, *work.runtime.stay_down}
        ]
        deadline = time.monotonic() + self.wait
        while True:
            pending, failed = [], []
            ps = node.run(
                "docker", "compose", *stack_args(stack), "ps", "--all", "--format", "json", cwd=home
            )
            rows: dict[str, dict[str, Any]] = {row["Service"]: row for row in ps_rows(ps)}
            for service in expected:
                row = rows.get(service, {})
                state, health = row.get("State", ""), row.get("Health", "")
                if state in ("exited", "dead") or health == "unhealthy":
                    failed.append(f"{service} ({state}{', ' + health if health else ''})")
                elif state != "running" or health not in ("", "healthy"):
                    pending.append(f"{service} ({state or 'no container'})")
            if failed:
                raise ApplyError(f"{work.node}: not running: {', '.join(failed)}")
            running = f"{len(expected) - len(pending)}/{len(expected)} running"
            if not pending:
                step.result = running
                return
            if time.monotonic() > deadline:
                raise ApplyError(f"{work.node}: not up after {self.wait}s: {', '.join(pending)}")
            step.update(f"{running}, waiting for {', '.join(pending)}")
            time.sleep(2)

    ##########################################################################

    # Phases 3 and 4: archive, record

    ##########################################################################

    def _archive(self, node: Node, work: NodeWork, undo: Undo) -> None:
        """Data no service uses any more, and files the bundle does not have: one step, naming
        what went to the archive."""
        if not (work.archive or work.discard):
            return
        with self.steps.step(work.node, "archive") as step:
            archived = self._archive_all(node, work, undo)
            step.result = ", ".join(archived) or "nothing there"

    def _archive_all(self, node: Node, work: NodeWork, undo: Undo) -> list[str]:
        archived: list[str] = []
        appdata = work.desired.dirs.appdata_dir
        for key, reason in work.archive:
            if is_volume(key):
                name = volume_name(key)
                if not node.volume_exists(name):
                    continue
                users = node.volume_users(name)
                if users:
                    self.steps.note(
                        f"[yellow]![/] {work.node}: volume {name} stays: {', '.join(users)} "
                        f"{'uses' if len(users) == 1 else 'use'} it"
                    )
                    continue
                self._archive_volume(node, appdata, name, reason, work.old_services, undo)
                archived.append(f"volume {name}")
                continue
            now = f"{appdata}/{key}"
            if not node.exists(now):
                continue
            new = f"{appdata}/{ARCHIVE_DIR}/{key}-{self.stamp}"
            node.log(f"archive {now}")
            back = node.move(now, new)
            undo.append(
                (f"unarchive {key} on {work.node}", partial(self._unarchive, node, new, back))
            )
            self._sidecar(node, appdata, key, reason, work.old_services)
            archived.append(f"{key}/")
        home = work.desired.dirs.deploy_dir
        if work.discard:
            node.run("mkdir", "-p", f"{home}/{FILES_ARCHIVE}")
        for name in work.discard:
            now = f"{home}/.labctl/previous/{name}"
            new = f"{home}/{FILES_ARCHIVE}/{name}-{self.stamp}"
            node.log(f"archive {name}")
            node.run("mv", "-T", "--", now, new)
            undo.append(
                (f"unarchive {name} on {work.node}", partial(node.run, "mv", "-T", "--", new, now))
            )
            archived.append(name)
        return archived

    def _record(self, node: Node, work: NodeWork) -> None:
        desired = work.desired
        home = desired.dirs.deploy_dir
        node.log("record")
        tag = tag_name(work.node, self.stamp) if self.commit else ""
        record = make_record(desired, self.when, self.commit, tag)
        with tempfile.TemporaryDirectory(prefix="labctl-record-") as tmp:
            (Path(tmp) / RECORD).parent.mkdir()
            (Path(tmp) / RECORD).write_text(json.dumps(record.to_json(), indent=2) + "\n")
            (Path(tmp) / MANAGED).write_text("".join(f"{name}\n" for name in desired.top_level))
            node.upload(Path(tmp), home)
        node.script(f'printf "%s\\n" "$1" > "$HOME/{HOME_POINTER}"', home)
        self.recorded.append(work.node)
