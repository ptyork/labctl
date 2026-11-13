"""One apply of a ``Planned``, start to finish: lock the nodes, plan again under the locks, run
the plan (apply.Executor), commit what was applied, unlock, then tag each node and push.

Nothing here asks, and it prints only through the ``say`` and ``progress`` it is given: the
apply command confirms the plan first and words the ``Applied`` this returns.
"""

from __future__ import annotations

import getpass
import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from labctl import __version__
from labctl.apply import ApplyError, Executor, RollbackFailed
from labctl.collect import collect
from labctl.git_sync import Published, commit_applied, publish_applied
from labctl.node import Node, NodeError, lock, unlock
from labctl.planning import Planned, make_planned
from labctl.progress import Progress
from labctl.remote import node_remote
from labctl.repo import ConfigRepo, TagInfo
from labctl.settings import Secrets, Settings
from labctl.state import Scope


@dataclass(frozen=True)
class Applied:
    """
    What an apply came to, once every node is up.

    Attributes:
        published (Published):
            The commit applied, its tags and the push.
        branch (str):
            The branch (or HEAD) the commit was made for.
        advanced (bool):
            Whether the branch now holds the commit; False when it moved during the apply (the
            commit's tags name it, and it is left to be merged).
    """

    published: Published
    branch: str
    advanced: bool = True


def roll_out(
    settings: Settings,
    scope: Scope,
    secrets: Secrets,
    planned: Planned,
    targets: list[str],
    wait: int,
    repo: ConfigRepo,
    message: Callable[[], str] = lambda: "labctl apply",
    say: Callable[[str], None] = lambda line: None,
    progress: Progress | None = None,
) -> Applied:
    """
    Apply a plan to its nodes; everything is rolled back when a step fails.

    Args:
        settings (Settings):
            labctl's settings.
        scope (Scope):
            The nodes named.
        secrets (Secrets):
            The secrets the plan was made with.
        planned (Planned):
            The plan as shown and confirmed, its conflicts answered (its options hold the
            answers). The nodes and the lab config must not have changed since.
        targets (list[str]):
            The nodes with work, in the order they are locked.
        wait (int):
            Seconds services may take to come up.
        repo (ConfigRepo):
            The lab config.
        message (Callable[[], str]):
            Gives the message for the commit of what was applied; called once everything is up,
            and only when there is something to commit.
        say (Callable[[str], None]):
            Prints a line.
        progress (Progress | None):
            Shows the steps as they run (default: each finished step, with ``say``).

    Returns:
        Applied:
            The commit made, and what became of it.

    Raises:
        ApplyError:
            The homelab or the lab config changed since the plan, or a step failed and everything
            was rolled back.
        RollbackFailed:
            A step failed and undoing it did too: the nodes stay locked.
        NodeError:
            A node is locked by another apply, or could not be reached.
    """
    assert planned.tree is not None  # apply plans a snapshot, never the working tree itself
    assert planned.options is not None
    options = planned.options
    remotes = {node: node_remote(node) for node in targets}
    by = f"{getpass.getuser()}@{socket.gethostname()}"
    owner = f"{by} {datetime.now().isoformat(timespec='seconds')}"
    locked: list[str] = []
    stuck = False  # the rollback failed: the locks stay
    tags: list[tuple[str, TagInfo]] = []
    try:
        for node in targets:
            lock(remotes[node], owner)
            locked.append(node)
        head = repo.head()
        if head != planned.base:
            raise ApplyError(
                f"HEAD moved from {planned.base[:7]} to {head[:7]} since the plan; run apply again"
            )
        if repo.snapshot() != planned.tree:
            raise ApplyError("The lab config changed since the plan; run apply again")
        with collect(settings, scope, secrets, None, repo, planned.tree, locked=True) as again:
            if again.errors:
                raise ApplyError(
                    "Cannot plan again under the lock:\n  " + "\n  ".join(again.errors)
                )
            computed = make_planned(again, options)
            if computed.works is None:
                raise ApplyError(computed.work_error)
            if not computed.same_as(planned):
                raise ApplyError("The homelab changed since it was planned; run apply again")
            tree, base = planned.tree, planned.base
            executor = Executor(
                say,
                wait,
                options.replace_stale,
                tree=tree,
                commit_with=lambda: commit_applied(repo, tree, base, message, say),
                progress=progress,
            )
            say("")
            try:
                executor.run(
                    {node: Node(remote) for node, remote in remotes.items()},
                    computed.works,
                    computed.plan,
                )
            except RollbackFailed:
                stuck = True
                raise
            except NodeError as exc:  # already shown, with the rollback
                raise ApplyError("Not applied: everything was rolled back") from exc
            when = executor.when.isoformat(timespec="seconds")
            for work in computed.works:
                if work.node not in executor.recorded:
                    continue
                up = (
                    computed.runtime.running.get(work.node, frozenset())
                    if work.record_only
                    else work.runtime.start
                )
                info = TagInfo(
                    when,
                    by,
                    __version__,
                    tuple(sorted(up)),
                    work.desired.dirs,
                    work.desired.env.host_ip,
                    retired=again.nodes[work.node].retiring,
                )
                tags.append((work.node, info))
    finally:
        if not stuck:
            for node in locked:
                unlock(remotes[node])
    advanced = repo.advance(executor.commit, planned.base)
    committed = executor.commit != planned.base
    published = publish_applied(repo, executor.commit, executor.stamp, tags, advanced, committed)
    return Applied(published, repo.branch() or "HEAD", advanced)
