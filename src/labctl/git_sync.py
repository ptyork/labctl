"""The lab config's git side of plan and apply: catching up with its upstream before planning,
the commit made once applied, and tagging and pushing it.

Nothing here prints or asks: each step returns what happened (the commands word it), and takes a
callback where there is something to ask or say along the way.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from labctl.repo import ConfigRepo, RepoError, TagInfo

#############################################################################
# Before planning
#############################################################################


@dataclass(frozen=True)
class CaughtUp:
    """
    What catching up with the upstream came to.

    Attributes:
        forwarded (str):
            What the branch was fast-forwarded to and by how much, when it was.
        warning (str):
            Why it was not, when planning goes on regardless (the branch has diverged).
    """

    forwarded: str = ""
    warning: str = ""


def catch_up(
    repo: ConfigRepo,
    ff: bool,
    applying: bool,
    ask: Callable[[str, str], bool] | None = None,
) -> CaughtUp:
    """
    The branch behind its upstream: fast-forward it before planning (the plan is of what the repo
    holds).

    Args:
        repo (ConfigRepo):
            The lab config, fetched.
        ff (bool):
            Fast-forward without asking (``--ff``).
        applying (bool):
            Whether this is for an apply: it goes on behind its upstream only when that was chosen
            in a terminal (``--yes`` does not fast-forward, nor go on behind).
        ask (Callable[[str, str], bool] | None):
            Asks whether to fast-forward first, given the question and the commits it would bring;
            None without a terminal.

    Returns:
        CaughtUp:
            What was done.

    Raises:
        RepoError:
            The fast-forward failed, or an apply cannot go on as the branch is.
    """
    state = repo.ahead_behind()
    if not state or not state[2]:
        return CaughtUp()
    upstream, ahead, behind = state
    branch, root = repo.branch() or "HEAD", repo.root
    count = f"{behind} commit{'s' if behind > 1 else ''}"
    if ahead:
        message = (
            f"{branch} has diverged from {upstream} ({ahead} ahead, {behind} behind): no "
            f"fast-forward. Rebase or merge it:\n  git -C {root} pull --rebase\n"
            f"  git -C {root} merge {upstream}"
        )
        if applying and not ask:
            raise RepoError(message)
        return CaughtUp(warning=message)
    forward = ff
    if not forward and ask:
        subjects = repo.subjects(f"HEAD..{upstream}")
        shown = "; ".join(subjects[:3]) + (
            f"; and {len(subjects) - 3} more" if len(subjects) > 3 else ""
        )
        forward = ask(f"{branch} is {count} behind {upstream}. Fast-forward first?", shown)
    if forward:
        problem = repo.fast_forward(upstream)
        if problem:
            raise RepoError(
                f"Cannot fast-forward {branch} to {upstream}:\n{problem}\nSet your changes aside "
                f"first:\n  git -C {root} stash\n  git -C {root} merge --ff-only {upstream}\n"
                f"  git -C {root} stash pop   # resolve conflicts, if any; then run labctl again"
            )
        return CaughtUp(forwarded=f"fast-forwarded {branch} to {upstream} ({count})")
    if applying and not ask:
        raise RepoError(
            f"{branch} is {count} behind {upstream}: applying it would undo what was applied "
            f"from there. Pass --ff to fast-forward first, or: git -C {root} pull --ff-only"
        )
    return CaughtUp()


def branch_notices(repo: ConfigRepo, fetched: str | None) -> list[str]:
    """What apply asks about first: a failed fetch (with a remote), a branch behind its upstream
    (chosen in a terminal: plan as it is)."""
    notices = []
    if fetched and fetched != "no remote":
        notices.append(
            f"not fetched ({fetched}): nodes not read are planned from this clone's tags"
        )
    state = repo.ahead_behind()
    if state and state[2]:
        upstream, _, behind = state
        notices.append(
            f"{repo.branch()} is {behind} commit{'s' if behind > 1 else ''} behind {upstream}: "
            "this plan undoes what was applied from there (--ff fast-forwards first)"
        )
    return notices


#############################################################################
# Once applied
#############################################################################


def default_message(repo: ConfigRepo, base: str, tree: str, nodes: Sequence[str]) -> str:
    """The message offered for the commit of what was applied: the nodes named and what changed."""
    paths = repo.changed_paths(base, tree)
    listed = ", ".join(paths[:3]) + (f" and {len(paths) - 3} more" if len(paths) > 3 else "")
    return f"labctl apply {' '.join(nodes)}: {listed}"


def commit_applied(
    repo: ConfigRepo,
    tree: str,
    base: str,
    message: Callable[[], str],
    say: Callable[[str], None] = lambda line: None,
) -> str:
    """What was just applied, as a commit on ``base`` (``base`` itself when nothing changed).
    ``message`` is called only when there is something to commit, after ``say`` announced it."""
    if repo.tree_of(base) == tree:
        return base
    say("All nodes successfully applied. Committing changes.")
    return repo.commit_tree(tree, base, message())


@dataclass(frozen=True)
class Published:
    """
    What became of the commit applied.

    Attributes:
        commit (str):
            The commit applied.
        committed (bool):
            Whether the apply made it (False: nothing had changed, it is the commit planned on).
        subject (str):
            Its subject when the apply made it.
        tagged (int):
            How many nodes were tagged.
        untagged (tuple[tuple[str, str], ...]):
            The nodes that could not be tagged, each with why.
        remote (str | None):
            The remote pushed to; None without one.
        push_error (str):
            Why the push failed, when it did.
    """

    commit: str
    committed: bool = False
    subject: str = ""
    tagged: int = 0
    untagged: tuple[tuple[str, str], ...] = ()
    remote: str | None = None
    push_error: str = ""


def publish_applied(
    repo: ConfigRepo,
    commit: str,
    stamp: str,
    tags: Sequence[tuple[str, TagInfo]],
    branch: bool,
    committed: bool,
) -> Published:
    """Tag each node recorded, then push the tags with the branch (when it holds the commit).
    Failures are returned, not raised: it is applied."""
    tagged = 0
    untagged = []
    for node, info in tags:
        try:
            repo.tag_applied(node, stamp, commit, info)
            tagged += 1
        except RepoError as exc:
            untagged.append((node, str(exc)))
    failed = repo.publish(branch)
    lines = next(iter(repo.subjects(f"{commit}^!")), "").splitlines() if committed else []
    no_remote = failed == "no remote"
    return Published(
        commit,
        committed,
        lines[0] if lines else "",
        tagged,
        tuple(untagged),
        None if no_remote else repo.remote(),
        "" if no_remote or not failed else failed,
    )
