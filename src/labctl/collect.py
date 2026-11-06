"""State collection: desired (local bundles), current (read from the nodes), and the node's files.

A node's current state is its DEPLOY_DIR, all of it. Every stack file there is a stack, whoever
put it there, and apply makes DEPLOY_DIR look like the bundle: what the repo does not have is
archived (to DEPLOY_DIR/.labctl/archive/). Kept out of this: .labctl, and data: the top-level
names the repo's env points at (APPDATA_DIR, LOGS_DIR, ...) or mounts, and those a container on
the node bind-mounts.

The stack files are downloaded into the private temp dir and resolved locally exactly like the
desired bundle, so the two compare like with like. Only what compose needs is downloaded; anything
else is listed (to be archived), not read.

The node also keeps a record of the last apply: DEPLOY_DIR/.labctl/applied.json (dirs, stacks,
file hashes; no config, no secrets) and `managed`, the top-level names apply wrote. Drift is those
files against applied.json's hashes: edits made on the node. ~/.labctl-home says where DEPLOY_DIR
is, so a DEPLOY_DIR the repo has since changed is still found.

With the files comes what runs: every container on the node, and the ports held on its host
(runtime.py decides what that means for apply).

The desired bundles come from the config repo (repo.py): the working tree for plan, a commit for
apply. applied.json names the commit and the apply tag. A node that is not read (not named, or
unreachable) is planned against its latest apply tag: that commit's tree, rebuilt like a bundle.
There is no state on this machine.

A node labctl never applied to is planned against what its DEPLOY_DIR has now (nothing, if it does
not exist): its first apply is an ordinary apply.

Read-only on the nodes. Locally it writes answers given at a prompt (node.env and secrets.toml,
never committed: apply commits once it has applied), and recreates an apply tag a node's record
names when it is missing here.
"""

from __future__ import annotations

import json
import posixpath
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from labctl.bundle import (
    Desired,
    Location,
    find_stacks,
    include_paths,
    resolve_desired,
    resolve_stacks,
    stack_candidates,
    top_level,
)
from labctl.compose import ComposeError, reduce_stacks
from labctl.envfile import EnvError, Prompt
from labctl.remote import MARK, Remote, RemoteError, Unreachable, node_remote, sections
from labctl.repo import ConfigRepo, RepoError, TagInfo
from labctl.runtime import (
    CONTAINER_FORMAT,
    UNAVAILABLE,
    Container,
    Listener,
    parse_containers,
    parse_listeners,
)
from labctl.settings import Secrets, Settings, node_names
from labctl.state import Dirs, NodeState, Scope, Stack

RECORD_VERSION = 2  # 2: commit and tag
RECORD = ".labctl/applied.json"
MANAGED = ".labctl/managed"
# Where apply puts what it removes from DEPLOY_DIR: <name>-<timestamp>.
FILES_ARCHIVE = ".labctl/archive"
# In the ssh user's home on the node (they do not move with DEPLOY_DIR): the lock held by a
# running apply, and where the last apply put DEPLOY_DIR.
LOCK = ".labctl-lock"
HOME_POINTER = ".labctl-home"
# A file listed but not read (not one apply writes): its digest in OnNode.files.
UNREAD = "-"
# Where the source tree is exported in the work folder (node names never start with a dot).
_TREE = ".tree"
_BASELINES = ".baselines"

_MOUNTS = '{{range .Mounts}}{{if eq .Type "bind"}}{{println .Source}}{{end}}{{end}}'

# $1: DEPLOY_DIR. Then the names whose files are hashed (with those in .labctl/managed), "--",
# and the names that are data by the repo. Every other top-level name is listed with its kind
# (data: a container bind-mounts it, and it is not one of the hashed names) and, unless data, its
# files.
_READ_NODE = f"""\
home=$1; shift
if [ -d "$HOME/{LOCK}" ]; then
  echo "{MARK} locked"; cat "$HOME/{LOCK}/owner" "$HOME/{LOCK}/steps" 2>/dev/null
fi
echo "{MARK} pointer"; cat "$HOME/{HOME_POINTER}" 2>/dev/null
echo "{MARK} containers"
docker ps -aq </dev/null 2>/dev/null |
  xargs -r docker inspect --format '{CONTAINER_FORMAT}' 2>/dev/null
echo "{MARK} listeners"
if command -v ss >/dev/null 2>&1; then ss -Htulnp </dev/null 2>/dev/null
else echo "{UNAVAILABLE}"; fi
echo "{MARK} missing"
docker ps -a --format '{{{{.Label "com.docker.compose.project.config_files"}}}}' \\
  </dev/null 2>/dev/null | tr ',' '\\n' | sort -u | while IFS= read -r file; do
    if [ -n "$file" ] && [ ! -e "$file" ]; then printf '%s\\n' "$file"; fi
  done
cd "$home" 2>/dev/null || {{ echo "{MARK} nohome"; exit 0; }}
echo "{MARK} applied"; cat {RECORD} 2>/dev/null && echo
hashed=/
while [ $# -gt 0 ] && [ "$1" != -- ]; do hashed="$hashed$1/"; shift; done
[ $# -gt 0 ] && shift
if [ -f {MANAGED} ]; then
  while IFS= read -r name; do hashed="$hashed$name/"; done < {MANAGED}
fi
mounts=$(docker ps -aq </dev/null | xargs -r docker inspect --format '{_MOUNTS}' 2>/dev/null)
listed=$(ls -A | while IFS= read -r name; do
  [ "$name" = .labctl ] && continue
  for data in "$@"; do [ "$data" = "$name" ] && continue 2; done
  ours=; case "$hashed" in */"$name"/*) ours=1 ;; esac
  if [ -z "$ours" ] && printf '%s\\n' "$mounts" |
    awk -v p="$PWD/$name/" 'index($0 "/", p) == 1 {{f = 1}} END {{exit !f}}'
  then echo "data $name"
  elif [ -d "$name" ] && [ ! -L "$name" ]; then echo "dir $name"
  else echo "file $name"
  fi
done)
echo "{MARK} names"
[ -n "$listed" ] && printf '%s\\n' "$listed"
echo "{MARK} files"
[ -n "$listed" ] && printf '%s\\n' "$listed" | while read -r kind name; do
  [ "$kind" = data ] && continue
  case "$hashed" in
    */"$name"/*) find "$name" -type f -exec sha256sum {{}} + 2>/dev/null ;;
    *) find "$name" -type f -exec printf -- '{UNREAD} %s\\n' {{}} + 2>/dev/null ;;
  esac
done
echo "{MARK} projects"
docker compose ls --all --format json 2>/dev/null </dev/null
"""


@dataclass(frozen=True)
class Record:
    """What one apply did: written to the node (never to the repo: the manifest hashes .env)."""

    applied_at: str
    dirs: Dirs
    stacks: tuple[Stack, ...]
    manifest: Mapping[str, str]  # DEPLOY_DIR-relative path -> sha256
    commit: str = ""  # the config repo's commit applied ("": applied before git tracking)
    tag: str = ""  # its apply tag
    node: str = ""  # the node it was applied to ("": not said)

    @property
    def owner(self) -> str:
        """The node this record is for: its own word, else its tag's; "" when neither says."""
        parts = self.tag.split("/")
        return self.node or (parts[1] if len(parts) == 3 else "")

    def to_json(self) -> dict[str, Any]:
        return {
            "version": RECORD_VERSION,
            "applied_at": self.applied_at,
            "node": self.node,
            "commit": self.commit,
            "tag": self.tag,
            "deploy_dir": self.dirs.deploy_dir,
            "appdata_dir": self.dirs.appdata_dir,
            "stacks": {stack.name: list(stack.files) for stack in self.stacks},
            "manifest": dict(self.manifest),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Record:
        if data.get("version") not in (1, RECORD_VERSION):
            raise ComposeError(f"unsupported {RECORD} version {data.get('version')}")
        return cls(
            data["applied_at"],
            # docker_home: records written before DOCKER_HOME was renamed DEPLOY_DIR
            Dirs(data.get("deploy_dir") or data["docker_home"], data["appdata_dir"]),
            tuple(Stack(name, tuple(files)) for name, files in data["stacks"].items()),
            data["manifest"],
            data.get("commit", ""),
            data.get("tag", ""),
            data.get("node", ""),
        )


@dataclass(frozen=True)
class OnNode:
    """What the node's DEPLOY_DIR has right now."""

    applied: Record | None
    # Path under DEPLOY_DIR -> sha256 (UNREAD for files of names apply neither writes nor wrote).
    files: Mapping[str, str]
    # Compose projects run from DEPLOY_DIR (with containers, running or not): name -> its files.
    projects: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # An apply's lock: who holds it and the steps it has done; None when not locked.
    lock: tuple[str, ...] | None = None
    exists: bool = True  # False: no such DEPLOY_DIR
    pointer: str = ""  # ~/.labctl-home: where the last apply put DEPLOY_DIR
    # Top-level name -> "file", "dir", or "data" (a container mounts it). Not .labctl, and not the
    # names that are data by the repo.
    names: Mapping[str, str] = field(default_factory=dict)
    # Every container on the node; the ports held on its host (None: no ss there); compose files
    # that containers were started from and that are gone.
    containers: tuple[Container, ...] = ()
    listeners: tuple[Listener, ...] | None = ()
    missing: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Drift:
    node: str
    changes: tuple[str, ...]


@dataclass(frozen=True)
class NodeReport:
    """How each node's recorded state was obtained, for the plan header."""

    node: str
    # "node" (applied by labctl), "first" (never applied: what is there now), "tag" (not read: as of
    # its apply tag), "none" (not read, and no apply tag)
    source: str
    detail: str = ""


@dataclass
class Collected:
    desired: dict[str, Desired]
    recorded: dict[str, NodeState | None]
    # The nodes this run may change: those named, less the unreachable (planned as if skipped).
    scope: Scope = field(default_factory=Scope)
    unreachable: tuple[str, ...] = ()
    # DEPLOY_DIR and APPDATA_DIR as last applied (or found); compared with desired for moves.
    recorded_dirs: dict[str, Dirs] = field(default_factory=dict)
    reports: list[NodeReport] = field(default_factory=list)
    drift: list[Drift] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # fatal: unreachable, unreadable
    # For each reachable node: its files, its top-level names, and the projects with containers.
    node_files: dict[str, Mapping[str, str]] = field(default_factory=dict)
    node_names: dict[str, Mapping[str, str]] = field(default_factory=dict)
    projects: dict[str, frozenset[str]] = field(default_factory=dict)
    # For each node read: what runs there (see runtime.py).
    containers: dict[str, tuple[Container, ...]] = field(default_factory=dict)
    listeners: dict[str, tuple[Listener, ...] | None] = field(default_factory=dict)
    missing: dict[str, frozenset[str]] = field(default_factory=dict)
    # The tree the bundles were built from (apply's snapshot; None: plan, the working tree), and
    # the commit the nodes' commits are compared with (HEAD: apply commits the tree on it).
    tree: str | None = None
    base: str = ""
    # Each reachable node's applied.json; the named nodes whose record has no commit yet.
    applied: dict[str, Record] = field(default_factory=dict)
    untracked: set[str] = field(default_factory=set)
    # About the repo and the nodes' commits: apply asks about them first.
    repo_notices: list[str] = field(default_factory=list)


@contextmanager
def collect(
    settings: Settings,
    scope: Scope,
    secrets: Secrets,
    prompt: Prompt | None,
    repo: ConfigRepo,
    tree: str | None = None,
    locked: bool = False,
) -> Iterator[Collected]:
    """
    Collect everything the planner needs. Bundles live in a temp dir for the duration.

    A node that cannot be reached is planned as if not named (``Collected.scope`` leaves it out).

    Args:
        settings (Settings):
            labctl's settings (the lab config, the docker CLI).
        scope (Scope):
            The nodes named: read live; the others are planned as of their latest apply tag.
        secrets (Secrets):
            The values for ``_SECRET_`` variables.
        prompt (Prompt | None):
            Asks for missing node.env values and secrets (None: no terminal, placeholders).
        repo (ConfigRepo):
            The lab config.
        tree (str | None):
            Apply's snapshot of the working tree to build the bundles from; None builds them from
            the working tree itself (plan) (default: None). Answers given at a prompt are written
            to node.env, never committed; with ``tree``, the snapshot is taken again with them
            (``Collected.tree``).
        locked (bool):
            This run holds the nodes' apply locks (apply planning again), so a lock is not an
            error (default: False).

    Yields:
        Collected:
            The desired and recorded state of every node, and what was read on the nodes.
    """
    with tempfile.TemporaryDirectory(prefix="labctl-") as tmp:
        work = Path(tmp)
        source = work / _TREE
        if tree is None:
            repo.export_worktree(source)
        else:
            repo.export(tree, source)
        nodes = node_names(source / "nodes")
        desired = {
            node: resolve_desired(settings, node, secrets, work, prompt, source) for node in nodes
        }
        if tree is not None and any(wanted.env.new_node_vars for wanted in desired.values()):
            tree = repo.snapshot()  # with the answers just written to node.env
        result = Collected(desired=desired, recorded={}, tree=tree, base=repo.head())
        unreachable = []
        for node in nodes:
            if not scope.in_scope(node):
                _collect_baseline(result, settings, node, repo, work, "not named")
                continue
            why = _collect_remote(
                result, settings, node, desired[node], work, locked, repo, result.base
            )
            if why:
                unreachable.append(node)
                _collect_baseline(result, settings, node, repo, work, f"unreachable ({why})")
        result.scope = scope.without(unreachable)
        result.unreachable = tuple(unreachable)
        yield result


def _collect_remote(
    result: Collected,
    settings: Settings,
    node: str,
    desired: Desired,
    workdir: Path,
    locked: bool = False,
    repo: ConfigRepo | None = None,
    base: str = "",
) -> str:
    """Read the node into ``result``. Returns why it cannot be reached, or "" when it was.

    With ``repo``, the node's recorded commit is checked against ``base`` (the commit planned), and
    a missing apply tag its record names is recreated.
    """
    home = desired.dirs.deploy_dir
    remote = node_remote(node)
    data = data_names(desired)
    try:
        on_node = read_node(remote, home, desired.top_level, data)
        # DEPLOY_DIR changed in the repo: the node's files are still where the last apply put
        # them, which the node's pointer says. The pointer is per ssh user, so it can be another
        # node's (two nodes on one host): only a record of this node's is followed.
        moved_from = ""
        if on_node.pointer and on_node.pointer != home and on_node.applied is None:
            at_old = read_node(remote, on_node.pointer, desired.top_level, data)
            if at_old.applied is not None and at_old.applied.owner in ("", node):
                on_node, home, moved_from = at_old, on_node.pointer, on_node.pointer
    except Unreachable as exc:
        return str(exc).removeprefix(f"{node}: ")
    except (RemoteError, ComposeError) as exc:
        result.errors.append(f"{node}: {exc}")
        return ""
    if on_node.lock is not None and not locked:
        done = "; ".join(on_node.lock) or "nothing recorded"
        result.errors.append(
            f"{node}: an apply holds ~/{LOCK} (running now, or interrupted). Its log: {done}. "
            f"Once nothing runs there and the node is checked, remove ~/{LOCK}"
        )
        return ""
    result.containers[node] = on_node.containers
    result.listeners[node] = on_node.listeners
    result.missing[node] = on_node.missing
    if not on_node.exists:  # a new node: nothing runs there yet
        result.recorded[node] = NodeState.of(node)
        result.recorded_dirs[node] = desired.dirs
        result.reports.append(
            NodeReport(node, "first", f"never applied; {home} does not exist yet")
        )
        return ""

    applied = on_node.applied
    dirs = applied.dirs if applied else desired.dirs
    folder = workdir / f"{node}.current" / (PurePosixPath(home).name or "docker")
    try:
        fetch_stack_files(remote, home, on_node, desired.top_level, folder)
        state = current_state(settings, node, folder, dirs, on_node.projects)
    except (RemoteError, ComposeError) as exc:
        result.errors.append(f"{node}: cannot read the stacks in {home}: {exc}")
        return ""
    result.recorded[node] = state
    result.recorded_dirs[node] = dirs
    result.node_files[node] = on_node.files
    result.node_names[node] = on_node.names
    result.projects[node] = frozenset(on_node.projects)

    if applied is None:
        count, stacks = len(state.services), len(state.stacks)
        result.reports.append(
            NodeReport(
                node,
                "first",
                f"never applied; what is there now: {count} services in {home} ({stacks} "
                f"stack{'' if stacks == 1 else 's'})",
            )
        )
        return ""
    where = f" at the old DEPLOY_DIR {moved_from}" if moved_from else ""
    result.applied[node] = applied
    detail = f"applied {applied.applied_at}{where}"
    if not applied.commit:
        detail += " (before git tracking)"
        result.untracked.add(node)
    elif repo is not None:
        detail += f" from {applied.commit[:7]}{_behind(repo, applied.commit, base)}"
        try:
            _check_commit(result, repo, node, applied, base, on_node)
        except RepoError as exc:
            result.repo_notices.append(f"{node}: {exc}")
    result.reports.append(NodeReport(node, "node", detail))
    changes = drift_changes(applied, on_node)
    if changes:
        result.drift.append(Drift(node, changes))
    return ""


def _behind(repo: ConfigRepo, commit: str, base: str) -> str:
    if not repo.has_commit(commit):
        return ""
    count = repo.count_between(commit, base)
    return f", {count} commit{'' if count == 1 else 's'} behind" if count else ""


def _check_commit(
    result: Collected, repo: ConfigRepo, node: str, applied: Record, base: str, on_node: OnNode
) -> None:
    """The node's commit against the repo: known here, contained in what is planned, its tag
    present (recreated from the record when not)."""
    short = applied.commit[:7]
    if not repo.has_commit(applied.commit):
        result.repo_notices.append(
            f"{node} was applied from {short}, which this clone does not have (git fetch, or "
            "pull); planned against the node as it is, and apply records the commit planned"
        )
        result.untracked.add(node)  # like a record from before git tracking: recorded again
        return
    if not repo.is_ancestor(applied.commit, base):
        lost = repo.subjects(f"{base}..{applied.commit}")
        listed = "; ".join(lost[:5]) + (f"; and {len(lost) - 5} more" if len(lost) > 5 else "")
        result.repo_notices.append(
            f"{node} was applied from {short}, which {base[:7]} does not contain; applying it "
            f"undoes: {listed}"
        )
    if applied.tag and repo.tag_commit(applied.tag) is None:
        up = {
            container.project
            for container in on_node.containers
            if container.up and container.project in on_node.projects
        }
        info = TagInfo(
            applied.applied_at,
            "recreated from the node's record",
            "",
            tuple(sorted(up)),
            applied.dirs,
        )
        repo.tag_applied(node, applied.tag.rsplit("/", 1)[-1], applied.commit, info)
    latest = repo.last_applied(node)
    if latest is not None and latest.commit != applied.commit:
        result.repo_notices.append(
            f"{node} reports {short} ({applied.tag or 'no tag'}), but its latest apply tag is "
            f"{latest.tag} ({latest.commit[:7]}); the node is what counts"
        )


def _collect_baseline(
    result: Collected, settings: Settings, node: str, repo: ConfigRepo, work: Path, why: str
) -> None:
    """A node not read (``why``: not named, or unreachable): its state as of its latest apply
    tag, rebuilt from that commit's tree."""
    try:
        applied = repo.last_applied(node)
    except RepoError as exc:
        result.errors.append(f"{node}: {exc}")
        return
    if applied is None:
        result.recorded[node] = None
        result.reports.append(NodeReport(node, "none", f"{why}; no apply tag"))
        return
    if applied.info is None:
        result.errors.append(f"{node}: {applied.tag} is not a labctl apply tag (no trailers)")
        return
    folder = work / _BASELINES
    tree = folder / node / _TREE
    try:
        repo.export(applied.commit, tree)
        then = resolve_desired(settings, node, Secrets(), folder, None, tree, baseline=True)
        resolved = [(then.state.stacks[name], config) for name, config in then.configs.items()]
        result.recorded[node] = reduce_stacks(
            node, resolved, applied.info.dirs.appdata_dir, running=applied.info.stacks_up
        )
    except (RepoError, ComposeError, EnvError, OSError) as exc:
        result.errors.append(f"{node}: cannot rebuild {applied.tag}: {exc}")
        return
    result.recorded_dirs[node] = applied.info.dirs
    behind = _behind(repo, applied.commit, result.base)
    result.reports.append(
        NodeReport(
            node,
            "tag",
            f"{why}; as of {applied.tag} ({applied.commit[:7]}{behind}); drift not checked",
        )
    )


#############################################################################


# Reading a node


#############################################################################


def data_names(desired: Desired) -> tuple[str, ...]:
    """Top-level names in DEPLOY_DIR that hold data by the repo: where an env path (APPDATA_DIR,
    LOGS_DIR, ...) or a bind mount points, unless the bundle has the name itself."""
    home = PurePosixPath(desired.dirs.deploy_dir)
    paths = [value for value in desired.env.values.values() if value.startswith("/")]
    paths += [
        str(mount.get("source", ""))
        for config in desired.configs.values()
        for service in (config.get("services") or {}).values()
        for mount in service.get("volumes") or []
        if mount.get("type") == "bind"
    ]
    names = {
        PurePosixPath(path).relative_to(home).parts[0]
        for path in paths
        if PurePosixPath(path) != home and PurePosixPath(path).is_relative_to(home)
    }
    return tuple(sorted(names - set(desired.top_level)))


def read_node(
    remote: Remote, deploy_dir: str, hashed: Sequence[str], data: Sequence[str]
) -> OnNode:
    output = remote.run_script(_READ_NODE, deploy_dir, *hashed, "--", *data)
    return parse_node_output(output, deploy_dir)


def parse_node_output(output: str, deploy_dir: str) -> OnNode:
    parts = sections(output)
    lock = tuple(line for line in parts["locked"] if line.strip()) if "locked" in parts else None
    pointer = "".join(parts.get("pointer", [])).strip()
    containers = parse_containers(parts.get("containers", []), deploy_dir)
    listeners = parse_listeners(parts.get("listeners", []))
    missing = frozenset(line for line in parts.get("missing", []) if line.strip())
    if "nohome" in parts:
        return OnNode(
            None,
            {},
            lock=lock,
            exists=False,
            pointer=pointer,
            containers=containers,
            listeners=listeners,
            missing=missing,
        )
    applied_text = "\n".join(parts.get("applied", [])).strip()
    names: dict[str, str] = {}
    for line in parts.get("names", []):
        if line.strip():
            kind, name = line.split(" ", 1)
            names[name] = kind
    files: dict[str, str] = {}
    for line in parts.get("files", []):
        if line.strip():
            digest, path = line.split(maxsplit=1)
            files[path.removeprefix("./")] = digest
    projects_text = "\n".join(parts.get("projects", [])).strip()
    return OnNode(
        applied=Record.from_json(json.loads(applied_text)) if applied_text else None,
        files=files,
        projects=projects_in(json.loads(projects_text) if projects_text else [], deploy_dir),
        lock=lock,
        pointer=pointer,
        names=names,
        containers=containers,
        listeners=listeners,
        missing=missing,
    )


def projects_in(
    projects: Sequence[Mapping[str, Any]], deploy_dir: str
) -> dict[str, tuple[str, ...]]:
    """Of `docker compose ls --format json` rows, the projects whose files are all in DEPLOY_DIR:
    name -> files relative to it."""
    found: dict[str, tuple[str, ...]] = {}
    prefix = deploy_dir.rstrip("/") + "/"
    for project in projects:
        paths = [path for path in str(project.get("ConfigFiles", "")).split(",") if path]
        if paths and all(path.startswith(prefix) for path in paths):
            found[project["Name"]] = tuple(path.removeprefix(prefix) for path in paths)
    return found


def drift_changes(applied: Record, on_node: OnNode) -> tuple[str, ...]:
    """Edits made on the node since the apply: files apply wrote that were changed, added or
    removed there."""
    managed = set(top_level(applied.manifest))
    changes: list[str] = []
    for path in sorted(applied.manifest.keys() | on_node.files.keys()):
        if path.split("/", 1)[0] not in managed:
            continue
        if path not in on_node.files:
            changes.append(f"{path}: removed on node")
        elif path not in applied.manifest:
            changes.append(f"{path}: added on node")
        elif applied.manifest[path] != on_node.files[path]:
            changes.append(f"{path}: changed on node")
    return tuple(changes)


def fetch_stack_files(
    remote: Remote, deploy_dir: str, on_node: OnNode, bundle_names: Sequence[str], folder: Path
) -> None:
    """Download into ``folder`` what compose needs to resolve the node's stacks: the names the
    bundle has or apply wrote, top-level compose and env files, the folders that stacks with
    containers run from, and the folders top-level compose files include."""
    kinds = {name: kind for name, kind in on_node.names.items() if kind != "data"}
    managed = set(top_level(on_node.applied.manifest)) if on_node.applied else set()
    first = {
        name
        for name, kind in kinds.items()
        if name in bundle_names
        or name in managed
        or (kind == "file" and (name.startswith(".") or name.endswith((".yml", ".yaml", ".env"))))
    }
    first |= {path.split("/", 1)[0] for files in on_node.projects.values() for path in files}
    first &= kinds.keys()
    folder.mkdir(parents=True)
    if first:
        remote.download(deploy_dir, sorted(first), folder)
    included: set[str] = set()
    for name in first:
        if kinds[name] == "file" and name.endswith((".yml", ".yaml")):
            text = (folder / name).read_text(encoding="utf-8", errors="replace")
            for entry in include_paths(text):
                parts = PurePosixPath(posixpath.normpath(entry)).parts
                if parts and parts[0] not in ("/", ".."):
                    included.add(parts[0])
    second = (included & kinds.keys()) - first
    if second:
        remote.download(deploy_dir, sorted(second), folder)


def current_state(
    settings: Settings,
    node: str,
    folder: Path,
    dirs: Dirs,
    projects: Mapping[str, tuple[str, ...]],
) -> NodeState:
    """
    A node's stacks, from a folder of its files, resolved with paths as on the node.

    Args:
        settings (Settings):
            labctl's settings (the docker CLI).
        node (str):
            The node's name.
        folder (Path):
            A local copy of the node's DEPLOY_DIR files that compose needs.
        dirs (Dirs):
            The node's DEPLOY_DIR and APPDATA_DIR as they are now.
        projects (Mapping[str, tuple[str, ...]]):
            The compose projects with containers there, by name, with their files.

    Returns:
        NodeState:
            Each top-level stack file (found by the rule the bundle uses), and each project with
            containers that runs from other files.

    Raises:
        ComposeError:
            A project has containers, but its files are not in DEPLOY_DIR.
    """
    location = Location(str(folder), docker_cli=settings.docker_cli)
    files = [path.name for path in folder.iterdir() if path.is_file()]
    stacks, _ = find_stacks(location, stack_candidates(location, files))
    known = {stack.name for stack in stacks}
    for name, paths in sorted(projects.items()):
        if name in known:
            continue
        missing = [path for path in paths if not (folder / path).is_file()]
        if missing:
            raise ComposeError(
                f"stack '{name}' has containers, but its files ({', '.join(missing)}) are not in "
                "DEPLOY_DIR (or are in a data folder); take it down by hand"
            )
        stacks.append(Stack(name, paths))
    resolved = resolve_stacks(location, stacks, dirs.deploy_dir)
    return reduce_stacks(node, resolved, dirs.appdata_dir, running=projects.keys())


#############################################################################


# Records


#############################################################################


def make_record(desired: Desired, when: datetime, commit: str = "", tag: str = "") -> Record:
    return Record(
        when.isoformat(timespec="seconds"),
        desired.dirs,
        tuple(desired.state.stacks.values()),
        desired.files,
        commit,
        tag,
        desired.node,
    )
