"""The plan, as plan and apply show it."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from git_wrap import StatusEntry
from rich.markup import escape

from labctl.changes import definition_changes, network_changes
from labctl.checks import (
    Finding,
    Level,
    PathInfo,
    archive_path,
    human_size,
    key_label,
    move_paths,
)
from labctl.cli.git_sync import list_changes
from labctl.collect import FILES_ARCHIVE, Collected
from labctl.console import console
from labctl.moves import (
    DATA,
    VOLUME,
    FannedOut,
    Move,
    MoveConflict,
    MoveNotice,
    NotRenamed,
    UnevenRename,
    where_now,
)
from labctl.planner import (
    Add,
    Assumed,
    Migrate,
    MissingBaseline,
    NotMigrated,
    Operation,
    Plan,
    Problem,
    Remove,
    Restore,
    UnknownNode,
    Update,
)
from labctl.repo import ConfigRepo
from labctl.runtime import Runtime, StackRun
from labctl.state import NodeState, Scope, Service, is_volume


def render_repo(repo: ConfigRepo, changes: list[StatusEntry], fetched: str | None) -> None:
    branch = repo.branch() or "detached"
    console.print(f"[bold]Repo[/] {repo.root} [dim]{branch} @ {repo.head()[:7]}[/]")
    if fetched == "no remote":
        console.print("  [dim]no remote: apply tags stay in this clone[/]")
    if changes:
        console.print(f"  [yellow]![/] {escape(list_changes(repo, changes, limit=10))}")
        console.print("  [dim]planned as they are; apply commits them once applied[/]")
    console.print()


def render_repo_notices(notices: list[str]) -> None:
    if not notices:
        return
    console.print("\n[bold yellow]Repo[/] [dim](apply asks first)[/]")
    for notice in notices:
        console.print(f"  [yellow]![/] {escape(notice)}")


def render_nodes(collected: Collected) -> None:
    console.print("[bold]Nodes[/]")
    sources = {
        "node": "[green]recorded[/]",
        "first": "[yellow]first apply[/]",
        "tag": "[cyan]as of tag[/]",
        "none": "[yellow]assumed[/]",
    }
    for report in sorted(collected.reports, key=lambda r: r.node):
        source = sources[report.source]
        if report.node in collected.unreachable:
            source = f"[red]unreachable[/], {source}"
        console.print(f"  {report.node:<10} {source}  [dim]{escape(report.detail)}[/]")


def render_problems(
    problems: tuple[Problem, ...], placeholders: dict[str, tuple[str, ...]]
) -> None:
    if not (problems or placeholders):
        return
    console.print("\n[bold red]Problems (block apply)[/]")
    for problem in problems:
        match problem:
            case MissingBaseline(node):
                console.print(f"  [red]✗[/] {node}: its current state could not be read")
            case UnknownNode(node):
                console.print(
                    f"  [red]✗[/] {node}: recorded state exists but the repo no "
                    "longer defines this node"
                )
            case MoveConflict(node, message):
                console.print(f"  [red]✗[/] {node}: {escape(message)}")
    for node, names in sorted(placeholders.items()):
        console.print(f"  [red]✗[/] {node}: no value for {', '.join(names)}")


def render_drift(collected: Collected) -> None:
    if not collected.drift:
        return
    console.print(
        "\n[bold yellow]Edited directly on the node[/] [dim](apply replaces them with the repo's; "
        f"the node's copies are archived to {FILES_ARCHIVE}/node-edits-<timestamp>)[/]"
    )
    for drift in collected.drift:
        for change in drift.changes:
            console.print(f"  [yellow]![/] {drift.node}: {escape(change)}")


def file_changes(
    wanted: Mapping[str, str], current: Mapping[str, str]
) -> dict[str, dict[str, list[str]]]:
    """Top-level entry -> {"new"|"changed"|"removed": [paths]}; only entries with changes."""
    changes: dict[str, dict[str, list[str]]] = {}
    for path in sorted(wanted.keys() | current.keys()):
        if path not in current:
            kind = "new"
        elif path not in wanted:
            kind = "removed"
        elif wanted[path] != current[path]:
            kind = "changed"
        else:
            continue
        top = path.split("/", 1)[0] + ("/" if "/" in path else "")
        changes.setdefault(top, {}).setdefault(kind, []).append(path)
    return changes


def render_files(collected: Collected, scope: Scope, verbose: bool) -> int:
    """Per node, how apply makes DEPLOY_DIR match the bundle. Returns how many nodes have
    changes."""
    lines: list[str] = []
    changed = 0
    for node in sorted(collected.desired):
        if not scope.in_scope(node) or collected.recorded.get(node) is None:
            continue
        desired = collected.desired[node]
        names = collected.node_names.get(node, {})
        changes = file_changes(desired.files, collected.node_files.get(node, {}))
        # Names the bundle does not have and that hold no files (an empty folder, a symlink).
        for name, kind in names.items():
            label = name + ("/" if kind == "dir" else "")
            if kind != "data" and name not in desired.top_level and label not in changes:
                changes[label] = {"removed": []}
        kept = sorted(name for name, kind in names.items() if kind == "data")
        if changes:
            changed += 1
            parts = []
            for top, kinds in changes.items():
                if "/" not in top or not any(kinds.values()):
                    (kind,) = kinds
                    parts.append(f"{escape(top)} ({kind})")
                else:
                    counts = ", ".join(f"{len(v)} {k}" for k, v in kinds.items())
                    parts.append(f"{escape(top)} ({counts})")
            lines.append(f"  {node}: {', '.join(parts)}")
        elif kept:
            lines.append(f"  {node}: no changes")
        if any(name.rstrip("/") not in desired.top_level for name in changes):
            lines.append(
                f"    [dim]removed: archived to {desired.dirs.deploy_dir}/{FILES_ARCHIVE}/"
                "<name>-<timestamp>[/]"
            )
        if kept:
            mounted = escape(", ".join(name + "/" for name in kept))
            lines.append(f"    [dim]left alone, a container mounts it: {mounted}[/]")
        if verbose:
            marks = {"new": "[green]+[/]", "changed": "[yellow]~[/]", "removed": "[red]-[/]"}
            for kinds in changes.values():
                for kind, paths in kinds.items():
                    lines.extend(f"      {marks[kind]} {escape(path)}" for path in paths)
    if lines:
        console.print("\n[bold]Files[/]")
        for line in lines:
            console.print(line)
    return changed


def render_stacks(collected: Collected, runtime: Runtime, work: set[str], verbose: bool) -> None:
    """Stacks added or removed per node, whether the stacks apply writes run afterwards (where
    that is not plain "up, and stays up"), and node ymls that are not stacks."""
    runs = {(stack.node, stack.stack): stack for stack in runtime.stacks}
    secrets = _secrets(collected)
    lines: list[str] = []
    for node, desired in sorted(collected.desired.items()):
        wanted = set(desired.state.stacks)
        recorded = collected.recorded.get(node)
        had = set(recorded.stacks) if recorded else wanted
        parts = [f"[green]+[/] {escape(name)} (new)" for name in sorted(wanted - had)]
        projects = collected.projects.get(node)
        parts += [
            f"[red]-[/] {escape(name)} "
            + ("(removed: taken down)" if projects is None or name in projects else "(removed)")
            for name in sorted(had - wanted)
        ]
        if parts:
            lines.append(f"  {node}: {', '.join(parts)}")
        for change in network_changes(recorded, desired.state, secrets) if node in work else ():
            run = runs.get((node, change.stack))
            if run is None or run.start:
                then = "[yellow]recreated[/]"
                restart = f"; {', '.join(change.services)} restart" if change.services else ""
            else:  # compose only recreates it on `up`
                then, restart = f"changes [dim](recreated when {change.stack} starts)[/]", ""
            lines.append(
                f"  {node}: network {escape(change.name)} {then} "
                f"({escape('; '.join(change.lines))}{escape(restart)})"
            )
        for name in sorted(wanted) if node in work else ():
            run = runs.get((node, name))
            line = _describe_run(run, desired.state.services) if run else ""
            if line:
                lines.append(f"  {node}/{escape(name)}: {line}")
        for file, why in sorted(desired.not_stacks.items()):
            reason = "defines no services" if why == "defines no services" else "does not resolve"
            lines.append(
                f"  [dim]{node}: {escape(file)} is not a stack ({reason}); copied as is[/]"
            )
            if verbose and reason != why:
                lines.extend(f"      [dim]{escape(line)}[/]" for line in why.splitlines()[-3:])
    if lines:
        console.print("\n[bold]Stacks[/]")
        for line in lines:
            console.print(line)


def _describe_run(run: StackRun, services: Mapping[str, Service]) -> str:
    """How a stack runs after apply, when that needs saying."""
    if not run.start:
        if run.why == "new":
            state = "new"
        elif run.containers:
            state = f"down ({run.running}/{run.containers} containers running)"
        else:
            state = "down"
        return f"[dim]{state}: files only, stays down (--start starts it)[/]"
    if run.why == "up":
        if run.running >= run.containers:
            return ""
        count = sum(1 for service in services.values() if service.stack == run.stack)
        return (
            f"up ({run.running}/{run.containers} containers running): apply starts all {count} "
            f"of its service{'s' if count > 1 else ''}"
        )
    return f"[green]starts[/] ({escape(run.why)})"


def render_runtime(runtime: Runtime) -> None:
    """Containers labctl does not manage, and what is in the way of what apply starts."""
    if runtime.unmanaged:
        console.print("\n[bold]Not managed by labctl[/] [dim](left alone)[/]")
        for node, containers in sorted(runtime.unmanaged.items()):
            for container in containers:
                origin = (
                    f"; compose project {container.project} in {container.working_dir}"
                    if container.project
                    else ""
                )
                console.print(
                    f"  [yellow]![/] {node}: {escape(container.name)} "
                    f"[dim]({escape(container.image)}, {container.state}{escape(origin)})[/]"
                )
    if runtime.conflicts or runtime.blocked:
        console.print(
            "\n[bold]In the way of what apply starts[/] [dim](apply asks in a terminal, all or "
            "nothing; without one it stops)[/]"
        )
    for down in runtime.take_down:
        services = ", ".join(down.services) or "no compose services"
        if down.in_repo and len(down.in_repo) == len(down.services):
            likely = "the same services as the repo's: likely this stack moving into DEPLOY_DIR"
        elif down.in_repo:
            likely = f"{len(down.in_repo)} of them in the repo's {down.project}"
        else:
            likely = "none of them in the repo's: likely an unrelated stack with the same name"
        mark = "[red]✗[/]" if down.missing else "[yellow]![/]"
        then = "" if down.missing else " → taken down (undo brings it back)"
        console.print(
            f"  {mark} {down.node}: compose project {escape(down.project)} runs from "
            f"{escape(down.working_dir)} ({escape(services)}; {likely}){then}"
        )
        for note in down.data:
            console.print(f"      [yellow]![/] {escape(note)}")
    for blocked in runtime.blocked:
        console.print(f"      [red]✗[/] {escape(blocked)}")
    for stop in runtime.stops:
        then = "stopped, restart policy set to no"
        if stop.rename:
            then += f", renamed {stop.container.name}-labctl-<timestamp>"
        console.print(
            f"  [yellow]![/] {stop.node}: container {escape(stop.container.name)} "
            f"[dim]({escape(stop.container.image)})[/] {escape('; '.join(stop.why))} → {then}"
        )
    for defer in runtime.defers:
        services = ", ".join(defer.services)
        console.print(
            f"  [yellow]![/] {defer.node}: {escape('; '.join(defer.why))} → {escape(services)} "
            "created, not started (start it once the port is free)"
        )
    if runtime.later:
        console.print("\n[bold]Would be in the way[/] [dim](nothing to do now)[/]")
        for line in runtime.later:
            console.print(f"  [dim]{escape(line)}[/]")
    for node in runtime.no_port_check:
        console.print(
            f"\n[yellow]![/] {node}: no `ss` there; ports held outside Docker not checked"
        )


def render_moves(plan: Plan, probes: dict[str, dict[str, PathInfo]]) -> None:
    if not plan.moves:
        return
    console.print("\n[bold]Moves[/] [dim](run first, in this order; affected services restart)[/]")
    for move, source, target in move_paths(plan.moves):
        console.print("  " + _describe_move(move, probes, source, target))


def _describe_move(
    move: Move, probes: dict[str, dict[str, PathInfo]], source: str = "", target: str = ""
) -> str:
    if move.kind == DATA:
        return (
            f"[magenta]» rename [/] {escape(move.old)}/ → {escape(move.new)}/ on {move.node} "
            f"[dim]({', '.join(move.services)})[/]"
        )
    if move.kind == VOLUME:
        size = probes.get(move.node, {}).get(source or move.old)
        details = [", ".join(move.services), "copied; the old one archived"]
        if size and size.size is not None:
            details.insert(1, human_size(size.size))
        return (
            f"[magenta]» rename [/] {escape(key_label(move.old))} → {escape(key_label(move.new))} "
            f"on {move.node} [dim]({'; '.join(details)})[/]"
        )
    head = (
        f"[magenta]» move   [/] {move.kind} on {move.node}: {escape(move.old)} → {escape(move.new)}"
    )
    if move.carried:
        return f"{head} [dim](moves with the one above)[/]"
    details = []
    if move.source != move.old:
        details.append(f"from {escape(move.source)}, where the move above takes it")
    src = probes.get(move.node, {}).get(source)
    dst = probes.get(move.node, {}).get(target)
    if src and src.size is not None:
        details.append(human_size(src.size))
    if src and dst and src.device and dst.device:
        details.append(
            "same filesystem: a rename"
            if src.device == dst.device
            else "[yellow]other filesystem: a full copy[/]"
        )
    return f"{head} [dim]({'; '.join(details)})[/]" if details else head


def render_services(
    plan: Plan, collected: Collected, probes: dict[str, dict[str, PathInfo]]
) -> None:
    if plan.operations:
        console.print("\n[bold]Services[/]")
        for op in plan.operations:
            console.print("  " + _describe(op, collected, probes, plan.moves))
    if plan.held:
        console.print("\n[bold]Held (touch a node not named, or unreachable)[/]")
        for held in plan.held:
            console.print(
                f"  [dim]{_describe(held.operation, collected, probes)}  "
                f"(skipped: {', '.join(held.skipped)})[/]"
            )


def _describe(
    op: Operation | Move,
    collected: Collected,
    probes: dict[str, dict[str, PathInfo]],
    moves: tuple[Move, ...] = (),
) -> str:
    match op:
        case Move():
            return _describe_move(op, probes)
        case Add(service, node):
            state = collected.desired[node].state
            note = _remote_note(state.services.get(service), "mounted as it is")
            return f"[green]+ add    [/] {service} on {_on(state, service)}{note}"
        case Remove(service, node):
            where = _on(collected.recorded.get(node), service, node)
            return f"[red]- remove [/] {service} from {where}{_remove_note(op, collected)}"
        case Update(service, node):
            desired = collected.desired[node].state
            recorded = collected.recorded.get(node)
            before = recorded.services.get(service) if recorded else None
            moved = (
                f" [dim](moves from stack {before.stack})[/]"
                if before and before.stack != desired.services[service].stack
                else ""
            )
            changes = definition_changes(
                before.config if before else "",
                desired.services[service].config,
                _secrets(collected),
            )
            shown = "".join(f"\n      {escape(line)}" for line in changes)
            return f"[yellow]~ update [/] {service} on {_on(desired, service)}{moved}{shown}"
        case Migrate(data, source, target, removed, added):
            services = (
                ", ".join(added)
                if removed == added
                else f"{', '.join(removed)} → {', '.join(added)}"
            )
            names = dict(op.renamed)
            details = [
                ", ".join(
                    f"{key_label(names[key])} → {key_label(key)}"
                    if key in names
                    else key_label(key)
                    for key in data
                )
            ]
            size = _migrate_size(op, collected, probes, moves)
            if size:
                details.append(size)
            wanted = collected.desired[target].state.services
            anonymous = sorted(
                {
                    path
                    for service in added
                    if service in wanted
                    for path in wanted[service].anonymous
                }
            )
            if anonymous:
                details.append(f"anonymous volumes not copied: {', '.join(anonymous)}")
            remote = sorted(
                {
                    volume
                    for service in added
                    if service in wanted
                    for volume in wanted[service].remote
                }
            )
            details += [
                f"volume {name} on {where}: not copied, {target} mounts it"
                for name, where in remote
            ]
            if op.archive:
                details.append(f"{source}'s copy archived")
            return (
                f"[magenta]» migrate[/] {services} {source} → {target} "
                f"[dim]({'; '.join(details)})[/]"
            )
        case Restore(service, node, source, stamp, entries=entries, size=size):
            where = "its archive" if source == node else f"{source}'s archive"
            keys = ", ".join(key_label(key) for _, key in entries)
            note = f"{keys}; {human_size(size)}" if size is not None else keys
            return f"[green]↺ restore[/] {service} on {node} from {where} {stamp} [dim]({note})[/]"


def _secrets(collected: Collected) -> frozenset[str]:
    """Every secret value the plan may come across: masked wherever it would be shown."""
    return frozenset().union(*(desired.env.secret_values for desired in collected.desired.values()))


def _on(state: NodeState | None, service: str, node: str = "") -> str:
    """The node, or node/stack when the node has several stacks."""
    if state is None:
        return node
    if state.multi_stack and service in state.services:
        return f"{state.node}/{state.services[service].stack}"
    return state.node


def _remove_note(op: Remove, collected: Collected) -> str:
    state = collected.recorded.get(op.node)
    service = state.services.get(op.service) if state else None
    if op.archive:
        root = collected.desired[op.node].dirs.appdata_dir  # archiving runs after any moves
        where = ", ".join(
            archive_path(root, key).removeprefix(root + "/") + "-<timestamp>" for key in op.archive
        )
        return f" [dim](data archived to {where})[/]{_remote_note(service, 'its data stays there')}"
    if service and service.stateful:
        return " [dim](its data stays: still used by other services)[/]"
    return _remote_note(service, "its data stays there")


def _remote_note(service: Service | None, what: str) -> str:
    """Volumes on remote storage: never copied or archived; said on the service's line."""
    if service is None or not service.remote:
        return ""
    volumes = "; ".join(f"volume {name} on {escape(where)}" for name, where in service.remote)
    return f" [dim]({volumes}: {what})[/]"


def remote_sharing(
    desired: Mapping[str, NodeState], recorded: Mapping[str, NodeState | None]
) -> list[str]:
    """Remote storage this plan has mounted on more nodes than before (the services on each
    must be able to share it: a database on NFS used from two nodes is how it gets corrupted)."""

    def users(states: Iterable[NodeState | None]) -> dict[str, dict[str, list[str]]]:
        found: dict[str, dict[str, list[str]]] = {}
        for state in states:
            if state is None:
                continue
            for service in state.services.values():
                for _, where in service.remote:
                    found.setdefault(where, {}).setdefault(state.node, []).append(service.name)
        return found

    want, have = users(desired.values()), users(recorded.values())
    notes = []
    for where, nodes in sorted(want.items()):
        if len(nodes) > 1 and not set(nodes) <= set(have.get(where, {})):
            on = "; ".join(f"{node} ({', '.join(sorted(s))})" for node, s in sorted(nodes.items()))
            notes.append(
                f"{where} is mounted on {len(nodes)} nodes: {on}. Make sure what uses it can "
                "share it (one database on NFS written from two nodes is how it gets corrupted)"
            )
    return notes


def _migrate_size(
    op: Migrate,
    collected: Collected,
    probes: dict[str, dict[str, PathInfo]],
    moves: tuple[Move, ...],
) -> str:
    root = collected.desired[op.source].dirs.appdata_dir
    on_source = [move for move in moves if move.node == op.source]
    paths = [
        key if is_volume(key) else where_now(f"{root}/{key}", on_source) for key in op.source_data
    ]
    sizes = [probes.get(op.source, {}).get(path) for path in paths]
    known = [size.size for size in sizes if size and size.size is not None]
    return human_size(sum(known)) if known else ""


def render_notices(notices: tuple[NotMigrated | MoveNotice | Assumed, ...]) -> None:
    assumed = [notice.node for notice in notices if isinstance(notice, Assumed)]
    if assumed:
        console.print("\n[bold]Not read, and no apply tag[/]")
        runs = "it runs" if len(assumed) == 1 else "they run"
        console.print(
            f"  [yellow]![/] {', '.join(assumed)}: planned as if {runs} what the repo says. A "
            "service the repo has moved from there to another node is added there fresh, WITHOUT "
            "its data, and may then run on both."
        )
    uneven = [notice for notice in notices if isinstance(notice, UnevenRename)]
    if uneven:
        console.print("\n[bold]Check these renames[/]")
    for rename in uneven:
        where = f"{rename.node} → {rename.target}" if rename.target else rename.node
        console.print(
            f"  [yellow]![/] {rename.service} ({where}): the mounts in {escape(rename.old)}/ end "
            f"up in {' and '.join(escape(name) + '/' for name in rename.new)}. "
            "Done as written, but usually a typo.\n"
            "    [dim]Each container path keeps its own data; check the volumes in the repo.[/]"
        )
    not_renamed = [notice for notice in notices if isinstance(notice, NotRenamed)]
    if not_renamed:
        console.print("\n[bold]Data not renamed[/]")
    for notice in not_renamed:
        console.print(
            f"  [yellow]![/] {notice.node}: {', '.join(notice.services)} now "
            f"{'uses' if len(notice.services) == 1 else 'use'} {escape(notice.new)}/ instead of "
            f"{escape(notice.old)}/, but {', '.join(notice.kept)} still "
            f"{'uses' if len(notice.kept) == 1 else 'use'} {escape(notice.old)}/, so it is not "
            "moved.\n"
            f"    [dim]{', '.join(notice.services)} will start with whatever is in "
            f"{escape(notice.new)}/ (nothing, if it does not exist).[/]"
        )
    migrated = [notice for notice in notices if isinstance(notice, NotMigrated)]
    fanned = [notice for notice in notices if isinstance(notice, FannedOut)]
    if not migrated and not fanned:
        return
    console.print("\n[bold]Data not migrated[/]")
    for fan_out in fanned:
        console.print(
            f"  [yellow]![/] {fan_out.service} leaves {fan_out.node} for "
            f"{', '.join(fan_out.targets)}; its volumes are not copied: it starts without them on "
            f"each [dim](archived on {fan_out.node})[/]"
        )
    for unmigrated in migrated:
        data = ", ".join(unmigrated.data)
        fresh = ", ".join(f"{service} on {node}" for service, node in unmigrated.fresh)
        source, moved, kept = unmigrated.source, unmigrated.moved, unmigrated.kept
        match unmigrated.reason:
            case "shared":
                console.print(
                    f"  [bold red]![/] [bold]{data}[/] on {source} is shared: "
                    f"{', '.join(moved)} {'is' if len(moved) == 1 else 'are'} "
                    f"leaving {source}, but {', '.join(kept)} still "
                    f"{'uses' if len(kept) == 1 else 'use'} it there.\n"
                    f"    The data stays on {source}. {fresh} will start WITHOUT it.\n"
                    "    [dim]Services that share data usually work together. "
                    "Apply will ask you to confirm this.[/]"
                )
            case "duplicate":
                console.print(
                    f"  [yellow]![/] {data} stays on {source} with "
                    f"{', '.join(kept)}; {fresh} starts as a separate instance without it"
                )
            case "fanout":
                console.print(
                    f"  [yellow]![/] {data} leaves {source} for more than one node; it is "
                    f"not copied: {fresh} start without it [dim](archived on {source})[/]"
                )
            case "no-migrate":
                console.print(
                    f"  [cyan]i[/] {data}: --no-migrate; {fresh} starts fresh "
                    f"[dim](archived on {source})[/]"
                )


def render_findings(findings: list[Finding]) -> None:
    if not findings:
        return
    console.print("\n[bold]Checks[/]")
    marks = {Level.INFO: "[cyan]i[/]", Level.WARN: "[yellow]![/]", Level.BLOCK: "[red]✗[/]"}
    for finding in sorted(
        findings,
        key=lambda finding: (finding.level is not Level.BLOCK, finding.node, finding.service),
    ):
        flag = f" [dim](needs {finding.flag})[/]" if finding.flag else ""
        service = f"{finding.service}: " if finding.service else ""
        console.print(
            f"  {marks[finding.level]} {finding.node}: {escape(service + finding.message)}{flag}"
        )


def render_summary(plan: Plan, files_changed: int, reasons: list[str], conflicts: int) -> None:
    counts = {
        kind: sum(isinstance(op, cls) for op in plan.operations)
        for kind, cls in (
            ("add", Add),
            ("remove", Remove),
            ("update", Update),
            ("migrate", Migrate),
            ("restore", Restore),
        )
    }
    parts = [f"{n} {kind}" for kind, n in counts.items() if n] or ["no service changes"]
    real_moves = [move for move in plan.moves if not move.carried]
    if real_moves:
        parts.insert(0, f"{len(real_moves)} move{'s' if len(real_moves) > 1 else ''}")
    if plan.held:
        parts.append(f"{len(plan.held)} held")
    parts.append(f"{len(plan.unchanged)} unchanged")
    if files_changed:
        parts.append(f"files differ on {files_changed} node{'s' if files_changed > 1 else ''}")
    if conflicts:
        parts.append(f"{conflicts} in the way")
    verdict = f"[bold red]blocked:[/] {'; '.join(reasons)}" if reasons else "[bold green]ready[/]"
    console.print(f"\n{', '.join(parts)} — {verdict}")
