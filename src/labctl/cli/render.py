"""The plan, as plan and apply show it."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

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
from labctl.runtime import Runtime
from labctl.state import NodeState, Scope, Service, is_volume


def render_repo_notices(notices: list[str]) -> None:
    if not notices:
        return
    console.print("\n[bold]Repo[/] (apply asks first)")
    for notice in notices:
        console.print(f"  [yellow]![/] {escape(notice)}")


def render_nodes(collected: Collected) -> None:
    """The nodes named, and the others (planned as of their latest apply, never changed)."""
    nodes = sorted(collected.desired)
    included = [node for node in nodes if collected.scope.in_scope(node)]
    skipped = [node for node in nodes if not collected.scope.in_scope(node)]
    console.print(f"Nodes included:  {', '.join(included) or 'none'}")
    if skipped:
        console.print(f"Nodes skipped:   {', '.join(skipped)}")


def render_problems(
    problems: tuple[Problem, ...], placeholders: dict[str, tuple[str, ...]]
) -> None:
    if not (problems or placeholders):
        return
    console.print("\n[bold]Problems (block apply)[/]")
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
        "\n[bold]Edited directly on the node[/] (apply replaces them with the repo's; the "
        f"node's copies are archived to {FILES_ARCHIVE}/node-edits-<timestamp>)"
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


_FILE_OPERATIONS = (("new", "Add"), ("changed", "Update"), ("removed", "Archive"))


def render_files(collected: Collected, scope: Scope, verbose: bool) -> int:
    """Per node, how apply makes DEPLOY_DIR match the bundle: what it adds, updates and archives.
    Returns how many nodes have changes."""
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
        if not changes:
            continue
        changed += 1
        console.print(f"\n[bold]File operations on {node}[/]")
        for kind, title in _FILE_OPERATIONS:
            entries = [(top, kinds[kind]) for top, kinds in changes.items() if kind in kinds]
            if not entries:
                continue
            shown = ", ".join(
                f"{escape(top)} ({len(paths)} file{'s' if len(paths) != 1 else ''})"
                if top.endswith("/") and paths
                else escape(top)
                for top, paths in entries
            )
            console.print(f"  - {title + ':':<9}{shown}")
            for _, paths in entries if verbose else ():
                for path in paths:
                    console.print(f"      {escape(path)}")
    return changed


def render_stacks(
    plan: Plan,
    collected: Collected,
    runtime: Runtime,
    work: set[str],
    probes: dict[str, dict[str, PathInfo]],
    verbose: bool,
) -> None:
    """The stacks of the nodes included that are new, go away or change, each with what happens
    to its services: a service under the stack it ends up in, a remove under the stack it leaves,
    a migrate under both."""
    runs = {(stack.node, stack.stack): stack for stack in runtime.stacks}
    secrets = _secrets(collected)
    entries: dict[tuple[str, str], list[str]] = {}

    def add(node: str, stack: str, *lines: str) -> None:
        entries.setdefault((node, stack), []).extend(lines)

    def stack_of(state: NodeState | None, service: str) -> str:
        found = state.services.get(service) if state else None
        return found.stack if found else ""

    for op in plan.operations:
        match op:
            case Add(service, node):
                state = collected.desired[node].state
                note = _remote_note(state.services.get(service), "mounted as it is")
                add(node, stack_of(state, service), f"* add:     {service}{note}")
            case Remove(service, node):
                recorded = collected.recorded.get(node)
                note = _remove_note(op, collected)
                add(node, stack_of(recorded, service), f"* [red]remove[/]:  {service}{note}")
            case Update(service, node):
                recorded = collected.recorded.get(node)
                before = recorded.services.get(service) if recorded else None
                after = collected.desired[node].state.services[service]
                changes = definition_changes(before.config if before else "", after.config, secrets)
                was = before.stack if before else after.stack
                if was != after.stack and changes:
                    how = f" (update and move from {node}/{was})"
                elif was != after.stack:
                    how = f" (move from {node}/{was})"
                else:
                    how = ""
                add(
                    node,
                    after.stack,
                    f"* update:  {service}{escape(how)}",
                    *(f"    {escape(line)}" for line in changes),
                )
            case Migrate(_, source, target, removed, added):
                there = collected.desired[target].state
                to = f"{target}/{stack_of(there, added[0])}" if added else target
                for service in removed:
                    add(
                        source,
                        stack_of(collected.recorded.get(source), service),
                        f"* migrate: {service} (move to {to})",
                    )
                frm = collected.recorded.get(source)
                origin = f"{source}/{stack_of(frm, removed[0])}" if removed else source
                details = _migrate_details(op, collected, probes, plan.moves)
                for service in added:
                    add(
                        target,
                        stack_of(there, service),
                        f"* migrate: {service} (move from {origin}; {escape(details)})",
                    )
            case Restore(service, node, source, stamp, entries=restored, size=size):
                where = "its archive" if source == node else f"{source}'s archive"
                keys = ", ".join(key_label(key) for _, key in restored)
                note = f"{keys}; {human_size(size)}" if size is not None else keys
                add(
                    node,
                    stack_of(collected.desired[node].state, service),
                    f"* restore: {service} from {where} {stamp} ({escape(note)})",
                )

    for node, bundle in sorted(collected.desired.items()):
        if not collected.scope.in_scope(node):
            continue
        recorded = collected.recorded.get(node)
        for change in network_changes(recorded, bundle.state, secrets) if node in work else ():
            run = runs.get((node, change.stack))
            what = escape("; ".join(change.lines))
            if run is not None and not run.start:  # compose only recreates it on `up`
                line = f"changes ({what}; recreated when the stack starts)"
            else:
                kept = run.stay_down if run else ()
                restarting = [service for service in change.services if service not in kept]
                restart = f"; {', '.join(restarting)} restart" if restarting else ""
                line = f"recreated ({what}{escape(restart)})"
            add(node, change.stack, f"* network: {escape(change.name)} {line}")
        for name in bundle.state.stacks if node in work else ():
            run = runs.get((node, name))
            for service, starts, why in run.stopped if run else ():
                label = "starts:  " if starts else "stays down: "
                add(node, name, f"* {label}{service} ({escape(why)})")

    lines: list[str] = []
    for node, bundle in sorted(collected.desired.items()):
        if not collected.scope.in_scope(node):
            continue
        wanted = set(bundle.state.stacks)
        recorded = collected.recorded.get(node)
        had = set(recorded.stacks) if recorded else wanted
        for name in sorted(wanted | had):
            run = runs.get((node, name))
            if name in wanted - had:
                how = " (add)"
            elif name in had - wanted:
                how = " ([red]remove[/])"
            else:
                how = ""
            if run is not None and not run.start and node in work:
                how += " (stays down: not running)"
            body = entries.get((node, name), [])
            if how or body:
                lines.append(f"  - {node}/{escape(name)}{how}")
                lines.extend(f"    {line}" for line in body)
        for file, why in sorted(bundle.not_stacks.items()):
            reason = "defines no services" if why == "defines no services" else "does not resolve"
            lines.append(f"  - {node}: {escape(file)} is not a stack ({reason}); copied as is")
            if verbose and reason != why:
                lines.extend(f"      {escape(line)}" for line in why.splitlines()[-3:])
    if lines:
        console.print("\n[bold]Stacks[/]")
        for line in lines:
            console.print(line)


def render_runtime(runtime: Runtime) -> None:
    """Containers labctl does not manage, and what is in the way of what apply starts."""
    if runtime.unmanaged:
        console.print("\n[bold]Not managed by labctl[/] (left alone)")
        for node, containers in sorted(runtime.unmanaged.items()):
            for container in containers:
                origin = (
                    f"; compose project {container.project} in {container.working_dir}"
                    if container.project
                    else ""
                )
                console.print(
                    f"  [yellow]![/] {node}: {escape(container.name)} "
                    f"({escape(container.image)}, {container.state}{escape(origin)})"
                )
    if runtime.conflicts or runtime.blocked:
        console.print(
            "\n[bold]In the way of what apply starts[/] (apply asks in a terminal, all or "
            "nothing; without one it stops)"
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
            f"({escape(stop.container.image)}) {escape('; '.join(stop.why))} → {then}"
        )
    for defer in runtime.defers:
        services = ", ".join(defer.services)
        console.print(
            f"  [yellow]![/] {defer.node}: {escape('; '.join(defer.why))} → {escape(services)} "
            "created, not started (start it once the port is free)"
        )
    if runtime.later:
        console.print("\n[bold]Would be in the way[/] (nothing to do now)")
        for line in runtime.later:
            console.print(f"  {escape(line)}")
    for node in runtime.no_port_check:
        console.print(
            f"\n[yellow]![/] {node}: no `ss` there; ports held outside Docker not checked"
        )


def render_moves(plan: Plan, probes: dict[str, dict[str, PathInfo]]) -> None:
    if not plan.moves:
        return
    console.print("\n[bold]Moves[/] (run first, in this order; affected services restart)")
    for move, source, target in move_paths(plan.moves):
        console.print("  " + _describe_move(move, probes, source, target))


def _describe_move(
    move: Move, probes: dict[str, dict[str, PathInfo]], source: str = "", target: str = ""
) -> str:
    if move.kind == DATA:
        return (
            f"- rename: {escape(move.old)}/ → {escape(move.new)}/ on {move.node} "
            f"({', '.join(move.services)})"
        )
    if move.kind == VOLUME:
        size = probes.get(move.node, {}).get(source or move.old)
        details = [", ".join(move.services), "copied; the old one archived"]
        if size and size.size is not None:
            details.insert(1, human_size(size.size))
        return (
            f"- rename: {escape(key_label(move.old))} → {escape(key_label(move.new))} "
            f"on {move.node} ({'; '.join(details)})"
        )
    head = f"- move:   {move.kind} on {move.node}: {escape(move.old)} → {escape(move.new)}"
    if move.carried:
        return f"{head} (moves with the one above)"
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
    return f"{head} ({'; '.join(details)})" if details else head


def render_held(plan: Plan, collected: Collected) -> None:
    """Operations that touch a node not named: left for when it is."""
    if not plan.held:
        return
    console.print("\n[bold]Held (touch a node not named)[/]")
    for held in plan.held:
        console.print(
            f"  - {_describe(held.operation, collected)} (skipped: {', '.join(held.skipped)})"
        )


def _describe(op: Operation | Move, collected: Collected) -> str:
    """One operation in a line, for the held ones."""
    match op:
        case Move():
            return _describe_move(op, {}).removeprefix("- ")
        case Add(service, node):
            return f"add {service} on {_on(collected.desired[node].state, service)}"
        case Remove(service, node):
            return f"remove {service} from {_on(collected.recorded.get(node), service, node)}"
        case Update(service, node):
            return f"update {service} on {_on(collected.desired[node].state, service)}"
        case Migrate(_, source, target, removed, added):
            names = ", ".join(added)
            if removed != added:
                names = f"{', '.join(removed)} → {names}"
            return f"migrate {names} {source} → {target}"
        case Restore(service, node, source, stamp):
            return f"restore {service} on {node} from {source}'s archive {stamp}"
    return str(op)


def _migrate_details(
    op: Migrate,
    collected: Collected,
    probes: dict[str, dict[str, PathInfo]],
    moves: tuple[Move, ...],
) -> str:
    """What a migrate copies: its data, the size, and what it leaves behind."""
    names = dict(op.renamed)
    details = [
        ", ".join(
            f"{key_label(names[key])} → {key_label(key)}" if key in names else key_label(key)
            for key in op.data
        )
    ]
    size = _migrate_size(op, collected, probes, moves)
    if size:
        details.append(size)
    wanted = collected.desired[op.target].state.services
    anonymous = sorted(
        {path for service in op.added if service in wanted for path in wanted[service].anonymous}
    )
    if anonymous:
        details.append(f"anonymous volumes not copied: {', '.join(anonymous)}")
    remote = sorted(
        {volume for service in op.added if service in wanted for volume in wanted[service].remote}
    )
    details += [
        f"volume {name} on {where}: not copied, {op.target} mounts it" for name, where in remote
    ]
    if op.archive:
        details.append(f"{op.source}'s copy archived")
    return "; ".join(details)


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
        return f" (data archived to {where}){_remote_note(service, 'its data stays there')}"
    if service and service.stateful:
        return " (its data stays: still used by other services)"
    return _remote_note(service, "its data stays there")


def _remote_note(service: Service | None, what: str) -> str:
    """Volumes on remote storage: never copied or archived; said on the service's line."""
    if service is None or not service.remote:
        return ""
    volumes = "; ".join(f"volume {name} on {escape(where)}" for name, where in service.remote)
    return f" ({volumes}: {what})"


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
            "    Each container path keeps its own data; check the volumes in the repo."
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
            f"    {', '.join(notice.services)} will start with whatever is in "
            f"{escape(notice.new)}/ (nothing, if it does not exist)."
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
            f"each (archived on {fan_out.node})"
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
                    "    Services that share data usually work together. "
                    "Apply will ask you to confirm this."
                )
            case "duplicate":
                console.print(
                    f"  [yellow]![/] {data} stays on {source} with "
                    f"{', '.join(kept)}; {fresh} starts as a separate instance without it"
                )
            case "fanout":
                console.print(
                    f"  [yellow]![/] {data} leaves {source} for more than one node; it is "
                    f"not copied: {fresh} start without it (archived on {source})"
                )
            case "no-migrate":
                console.print(
                    f"  [cyan]i[/] {data}: --no-migrate; {fresh} starts fresh "
                    f"(archived on {source})"
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
        flag = f" (needs {finding.flag})" if finding.flag else ""
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
    blocked = f" — [bold red]blocked:[/] {'; '.join(reasons)}" if reasons else ""
    console.print(f"\n{', '.join(parts)}{blocked}")
