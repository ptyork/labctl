"""The config repo: the lab config's git repository (the lab config is its root).

apply ships what it planned: a snapshot of the working tree (a tree object), committed on HEAD once
it is applied, and only then (labctl commits nothing else, but init's first commit). Each node an
apply records gets an annotated tag, `applied/<node>/<stamp>`, on that commit (the stamp is the
apply's own, as in its archive names). Tags never move; apply pushes them with the branch.

A node that is not read (not named, or unreachable) is planned against its latest tag's tree,
rebuilt the way its bundle was. The tag message carries what the tree cannot say (which stacks were
up, where DEPLOY_DIR and APPDATA_DIR were) and nothing secret: no manifest, no hashes of the
compiled .env, whose other lines the repo shows.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from git_wrap import CommandError, GitRepo, NotARepoError, StatusEntry

from labctl.state import Dirs

TAG_PREFIX = "applied"
_TAGS = f"refs/tags/{TAG_PREFIX}/*"
FETCH_TIMEOUT = 15  # seconds
PUSH_TIMEOUT = 60


class RepoError(Exception):
    pass


@dataclass(frozen=True)
class TagInfo:
    """What an apply tag's message says (as `Key: value` trailers)."""

    applied_at: str
    applied_by: str
    labctl: str
    stacks_up: tuple[str, ...]
    dirs: Dirs
    host_ip: str = ""  # the address `_HOST_IP_` had ("": none used, or a tag from before)


@dataclass(frozen=True)
class Applied:
    """A node's latest apply tag."""

    tag: str
    commit: str
    info: TagInfo | None  # None: not a message labctl wrote

    @property
    def stamp(self) -> str:
        return self.tag.rsplit("/", 1)[-1]


def tag_name(node: str, stamp: str) -> str:
    return f"{TAG_PREFIX}/{node}/{stamp}"


def tag_message(node: str, info: TagInfo) -> str:
    return (
        f"labctl apply: {node}\n\n"
        f"Applied-At: {info.applied_at}\n"
        f"Applied-By: {info.applied_by}\n"
        f"Labctl: {info.labctl}\n"
        f"Stacks-Up: {' '.join(info.stacks_up)}\n"
        f"Deploy-Dir: {info.dirs.deploy_dir}\n"
        f"Appdata-Dir: {info.dirs.appdata_dir}\n"
    ) + (f"Host-IP: {info.host_ip}\n" if info.host_ip else "")


def parse_tag_message(text: str) -> TagInfo | None:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key and " " not in key:
            fields[key] = value.strip()
    try:
        return TagInfo(
            fields["Applied-At"],
            fields.get("Applied-By", ""),
            fields.get("Labctl", ""),
            tuple(fields.get("Stacks-Up", "").split()),
            # Docker-Home: tags made before DOCKER_HOME was renamed DEPLOY_DIR
            Dirs(fields.get("Deploy-Dir") or fields["Docker-Home"], fields["Appdata-Dir"]),
            fields.get("Host-IP", ""),
        )
    except KeyError:
        return None


def _root_repo(lab_config: Path) -> GitRepo:
    """The repository whose root is the lab config."""
    try:
        return GitRepo(lab_config)
    except NotARepoError as exc:
        outer = GitRepo.get_repository_root(lab_config.resolve())
        if outer is not None:
            raise RepoError(
                f"{lab_config} is inside the git repository at {outer}; the lab config must be "
                "the root of its own repository"
            ) from exc
        raise RepoError(
            f"{lab_config} is not a git repository; labctl records applies with git "
            "('labctl init' sets one up)"
        ) from exc


@contextmanager
def _errors(what: str) -> Iterator[None]:
    try:
        yield
    except CommandError as exc:
        raise RepoError(f"{what}: {exc.stderr.strip() or exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RepoError(f"{what}: no answer in {exc.timeout:g}s") from exc


class ConfigRepo:
    def __init__(self, git: GitRepo) -> None:
        self.git = git

    @classmethod
    def open(cls, lab_config: Path) -> ConfigRepo:
        """The lab config's repository, which has a commit."""
        repo = cls(_root_repo(lab_config))
        if not repo.has_commits():
            raise RepoError(f"{repo.root} has no commits yet ('labctl init' makes the first)")
        return repo

    @classmethod
    def init(cls, lab_config: Path) -> tuple[ConfigRepo, bool]:
        """The lab config's repository, created (branch main) if there is none: (repo, created)."""
        if GitRepo.get_repository_root(lab_config.resolve()) is None:
            with _errors("git init"):
                return cls(GitRepo(lab_config, create=True)), True
        return cls(_root_repo(lab_config)), False

    @property
    def root(self) -> Path:
        return self.git.root_dir

    def has_commits(self) -> bool:
        return self.git.resolve_commit("HEAD") is not None

    def head(self) -> str:
        sha = self.git.resolve_commit("HEAD")
        if sha is None:
            raise RepoError(f"{self.root}: HEAD names no commit")
        return sha

    def branch(self) -> str | None:
        return self.git.get_current_branch()

    def remote(self) -> str | None:
        """The current branch's upstream remote, else origin, else None."""
        branch = self.branch()
        if branch:
            proc = self.git.run("config", "--get", f"branch.{branch}.remote", check=False)
            if proc.returncode == 0 and proc.stdout.strip() not in ("", "."):
                return proc.stdout.strip()
        with _errors("git remote"):
            names = [remote.name for remote in self.git.list_remotes()]
        return "origin" if "origin" in names else None

    def ahead_behind(self) -> tuple[str, int, int] | None:
        """(upstream, commits ahead of it, commits behind it), or None without an upstream."""
        upstream = self.git.get_tracking_branch()
        if upstream is None:
            return None
        with _errors("git rev-list"):
            ahead, behind = self.git.get_ahead_behind("HEAD", upstream)
        return upstream, ahead, behind

    def add_remote(self, name: str, url: str) -> None:
        with _errors(f"git remote add {name}"):
            self.git.add_remote(name, url)

    def remote_url(self, name: str) -> str | None:
        with _errors("git remote"):
            remote = self.git.get_remote(name)
        return None if remote is None else remote.fetch_url

    def push_branch(self, remote: str) -> None:
        """Push the current branch, setting it to track the remote's."""
        with _errors(f"git push {remote}"):
            self.git.push(remote, set_upstream=True)

    def changes(self) -> list[StatusEntry]:
        """Uncommitted changes (untracked files included)."""
        with _errors("git status"):
            return self.git.list_changes()

    def commit(self, message: str, paths: Sequence[Path | str] | None = None) -> str:
        """Commit every change (or only ``paths``); return the new commit."""
        with _errors("git commit"):
            return self.git.commit(message, list(paths) if paths else ["."])

    def snapshot(self) -> str:
        """
        The working tree as it is now (less what git ignores), as a tree object.

        HEAD, the index and the working tree are untouched: this is what apply plans and ships, and
        commits once it is applied.

        Returns:
            str:
                The tree's sha.
        """
        with tempfile.TemporaryDirectory(prefix="labctl-index-") as tmp:
            env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
            with _errors("git write-tree"):
                self.git.run("read-tree", "HEAD", env=env)
                self.git.run("add", "-A", env=env)
                return self.git.run("write-tree", env=env).stdout.strip()

    def tree_of(self, rev: str) -> str:
        with _errors(f"git rev-parse {rev}"):
            return self.git.run("rev-parse", f"{rev}^{{tree}}").stdout.strip()

    def changed_paths(self, old: str, new: str) -> list[str]:
        """The paths that differ between two commits or trees."""
        with _errors("git diff"):
            return self.git.run("diff", "--name-only", old, new).stdout.split()

    def commit_tree(self, tree: str, parent: str, message: str) -> str:
        """
        A commit of a tree on a parent. No branch moves: see ``advance``.

        Args:
            tree (str):
                The tree.
            parent (str):
                The parent commit.
            message (str):
                The commit message.

        Returns:
            str:
                The commit's sha (``parent`` itself when the tree is its own).
        """
        if self.tree_of(parent) == tree:
            return parent
        with _errors("git commit-tree"):
            return self.git.run(
                "commit-tree", tree, "-p", parent, "-F", "-", input=message
            ).stdout.strip()

    def advance(self, commit: str, old: str) -> bool:
        """
        Move the current branch (or a detached HEAD) to a commit, and the index with it.

        The working tree is left as it is.

        Args:
            commit (str):
                Where the branch goes.
            old (str):
                Where it must still be.

        Returns:
            bool:
                False when it is no longer at ``old`` (nothing moved).
        """
        if commit == old:
            return True
        branch = self.branch()
        ref = f"refs/heads/{branch}" if branch else "HEAD"
        proc = self.git.run("update-ref", "-m", "labctl apply", ref, commit, old, check=False)
        if proc.returncode:
            return False
        with _errors("git reset"):
            self.git.run("reset", "-q")
        return True

    def export(self, rev: str, dest: Path) -> None:
        """The files at ``rev``, into the (new) folder ``dest``."""
        with _errors(f"git archive {rev}"):
            self.git.export(dest, rev)

    def export_worktree(self, dest: Path) -> None:
        """The working tree as it is now, less what git ignores, into the (new) folder ``dest``."""
        with _errors("git ls-files"):
            files = self.git.list_files(untracked=True)
        dest.mkdir(parents=True)
        for path in files:
            source = self.root / path
            if not (source.exists() or source.is_symlink()):  # deleted, not yet committed
                continue
            target = dest / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target, follow_symlinks=False)

    def has_commit(self, sha: str) -> bool:
        return bool(sha) and self.git.resolve_commit(sha) is not None

    def is_ancestor(self, ancestor: str, rev: str) -> bool:
        with _errors("git merge-base"):
            return self.git.is_ancestor(ancestor, rev)

    def count_between(self, older: str, newer: str) -> int:
        """Commits in ``newer`` that ``older`` does not have."""
        with _errors("git rev-list"):
            return self.git.get_ahead_behind(newer, older)[0]

    def subjects(self, rev_range: str) -> list[str]:
        with _errors("git log"):
            return [commit.message or "" for commit in self.git.get_commits(rev=rev_range)]

    def tag_commit(self, name: str) -> str | None:
        """The commit a tag names, or None when there is no such tag."""
        return self.git.resolve_commit(f"refs/tags/{name}")

    def last_applied(self, node: str) -> Applied | None:
        """
        The node's latest apply tag.

        Args:
            node (str):
                The node.

        Returns:
            Applied | None:
                Its tag, commit and parsed message; None when the node was never applied.
        """
        with _errors("git for-each-ref"):
            tags = self.git.list_tags(f"{TAG_PREFIX}/{node}/", sort="-refname", limit=1)
        if not tags:
            return None
        return Applied(tags[0].name, tags[0].commit, parse_tag_message(tags[0].message))

    def tag_applied(self, node: str, stamp: str, commit: str, info: TagInfo) -> str:
        """
        Tag what was applied to a node: ``applied/<node>/<stamp>``, annotated.

        Args:
            node (str):
                The node.
            stamp (str):
                The apply's stamp (also in the node's record and its archive entries).
            commit (str):
                The commit applied.
            info (TagInfo):
                The tag message's trailers: when, by whom, labctl's version, the stacks up and
                the dirs.

        Returns:
            str:
                The tag's name.
        """
        name = tag_name(node, stamp)
        with _errors(f"git tag {name}"):
            self.git.create_tag(name, commit, tag_message(node, info))
        return name

    def fetch(self) -> str | None:
        """Bring in the remote's branches and apply tags. Returns why it could not, or None."""
        remote = self.remote()
        if remote is None:
            return "no remote"
        refspecs = [f"+refs/heads/*:refs/remotes/{remote}/*", f"{_TAGS}:{_TAGS}"]
        try:
            with _errors(f"git fetch {remote}"):
                self.git.fetch(remote, refspecs, timeout=FETCH_TIMEOUT)
        except RepoError as exc:
            return str(exc)
        return None

    def publish(self, branch: bool = False) -> str | None:
        """
        Push every apply tag, and with ``branch`` the current branch, in one atomic push.

        Args:
            branch (bool):
                Push the current branch too: a fast-forward only, its upstream set when it has none
                (default: False).

        Returns:
            str | None:
                Why it could not ("no remote", or git's error), or None.
        """
        remote = self.remote()
        if remote is None:
            return "no remote"
        refspecs = [f"{_TAGS}:{_TAGS}"]
        current = self.branch() if branch else None
        upstream = self.git.get_tracking_branch() if current else None
        if current:
            target = upstream.removeprefix(f"{remote}/") if upstream else current
            refspecs.insert(0, f"refs/heads/{current}:refs/heads/{target}")
        try:
            with _errors(f"git push {remote}"):
                self.git.push_refs(remote, *refspecs, timeout=PUSH_TIMEOUT)
                if current and not upstream:
                    self.git.run("branch", f"--set-upstream-to={remote}/{current}")
        except RepoError as exc:
            return str(exc)
        return None

    def fast_forward(self, upstream: str) -> str | None:
        """
        Fast-forward the current branch to its upstream.

        Uncommitted changes stay; git refuses when it would overwrite one.

        Args:
            upstream (str):
                e.g. ``origin/main``.

        Returns:
            str | None:
                Why it could not (git's error), or None.
        """
        proc = self.git.run("merge", "--ff-only", "--quiet", upstream, check=False)
        return None if proc.returncode == 0 else (proc.stderr or proc.stdout).strip()
