"""What changes in a service's or a network's definition: the lines plan shows under an update.

Both sides are compose's resolved JSON, so how the repo's files are organised never shows, only
what compose would run differently. Secrets never show: an environment variable whose name looks
secret, or whose value is one of the env's secrets, is shown as changed without its values, and a
secret value anywhere else is masked.

Pure functions only.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from labctl.state import NodeState

SECRET_NAME = re.compile(r"PASS|SECRET|TOKEN|KEY|CLAIM|CREDENTIAL|AUTH|PRIVATE|SALT", re.IGNORECASE)
HIDDEN = "<secret>"
MAX_ITEMS = 6  # per field; the rest is counted
# Folded into `environment` by compose: the paths say nothing the environment doesn't.
_SKIP = frozenset({"env_file"})
# Order matters: shown whole.
_ORDERED = frozenset({"command", "entrypoint"})
_MISSING: Any = object()


def definition_changes(
    before: str, after: str, secrets: Collection[str] = (), limit: int | None = MAX_ITEMS
) -> list[str]:
    """
    What differs between two resolved definitions, one line per field.

    Args:
        before (str):
            The definition now, as canonical JSON ("": unknown).
        after (str):
            The definition the repo wants, as canonical JSON ("": unknown).
        secrets (Collection[str]):
            Secret values, masked wherever they would show (default: none).
        limit (int | None):
            Items shown per field; the rest are counted (default: MAX_ITEMS; None: all).

    Returns:
        list[str]:
            e.g. ``environment: + TZ=UTC, VPN us_atlanta → us_east``; nothing when either side
            is unknown.
    """
    if not before or not after:
        return []
    old, new = json.loads(before), json.loads(after)
    lines = []
    for key in sorted(old.keys() | new.keys()):
        if key in _SKIP or old.get(key) == new.get(key):
            continue
        items = _field(key, old.get(key), new.get(key), secrets)
        if limit is not None and len(items) > limit:
            items = [*items[:limit], f"… {len(items) - limit} more"]
        lines.append(mask(f"{key}: {', '.join(items)}", secrets))
    return lines


@dataclass(frozen=True)
class NetworkChange:
    """A network whose definition changes: compose removes and recreates it."""

    name: str
    stack: str  # the stack that recreates it
    lines: tuple[str, ...]
    services: tuple[str, ...]  # the stack's services on it: they restart


def network_changes(
    before: NodeState | None, after: NodeState, secrets: Collection[str] = ()
) -> list[NetworkChange]:
    """
    The networks a node keeps with another definition: compose recreates them.

    Args:
        before (NodeState | None):
            The node now (None: unknown, nothing to compare).
        after (NodeState):
            The node as the repo wants it.
        secrets (Collection[str]):
            Secret values, masked wherever they would show (default: none).

    Returns:
        list[NetworkChange]:
            By real name: what changes, the stack that recreates it, and its services.
    """
    if before is None:
        return []
    found = []
    for name, network in sorted(after.networks.items()):
        old = before.networks.get(name)
        if old is None or old.config == network.config:
            continue
        was, now = json.loads(old.config or "{}"), json.loads(network.config or "{}")
        lines = []
        if was.get("ipam") != now.get("ipam"):
            lines.append(f"subnet {_subnets(was)} → {_subnets(now)}")
        rest = definition_changes(
            json.dumps({k: v for k, v in was.items() if k != "ipam"}),
            json.dumps({k: v for k, v in now.items() if k != "ipam"}),
            secrets,
            limit=None,
        )
        found.append(NetworkChange(name, network.stack, (*lines, *rest), network.services))
    return found


def mask(text: str, secrets: Collection[str]) -> str:
    for secret in sorted(secrets, key=len, reverse=True):
        if len(secret) >= 4:
            text = text.replace(secret, HIDDEN)
    return text


def _subnets(config: Mapping[str, Any]) -> str:
    entries = (config.get("ipam") or {}).get("config") or []
    subnets = [
        str(entry["subnet"])
        for entry in entries
        if isinstance(entry, Mapping) and entry.get("subnet")
    ]
    return ", ".join(subnets) or "picked by Docker"


def _field(key: str, old: Any, new: Any, secrets: Collection[str]) -> list[str]:
    if _all(Mapping, old, new):
        return _mapping(key, old or {}, new or {}, secrets)
    if _all(list, old, new):
        return _sequence(key, old or [], new or [])
    if old is None:
        return [f"+ {_show(new)}"]
    if new is None:
        return [f"- {_show(old)}"]
    return [f"{_show(old)} → {_show(new)}"]


def _all(kind: type, *values: Any) -> bool:
    return all(value is None or isinstance(value, kind) for value in values)


def _mapping(
    field: str, old: Mapping[str, Any], new: Mapping[str, Any], secrets: Collection[str]
) -> list[str]:
    items = []
    for key in sorted(old.keys() | new.keys()):
        was, now = old.get(key, _MISSING), new.get(key, _MISSING)
        if was == now:
            continue
        hidden = field == "environment" and bool(
            SECRET_NAME.search(key) or was in secrets or now in secrets
        )
        if was is _MISSING:
            items.append(f"+ {key}" if hidden or now is None else f"+ {key}={_show(now)}")
        elif now is _MISSING:
            items.append(f"- {key}")
        elif hidden:
            items.append(f"{key} changed (secret)")
        else:
            items.append(f"{key} {_show(was)} → {_show(now)}")
    return items


def _sequence(field: str, old: Sequence[Any], new: Sequence[Any]) -> list[str]:
    if field in _ORDERED:
        return [f"{_show(old) or '(none)'} → {_show(new) or '(none)'}"]
    was = [_item(field, item) for item in old]
    now = [_item(field, item) for item in new]
    gone = [item for item in was if item not in now]
    added = [item for item in now if item not in was]
    if not gone and not added:
        return ["order or details changed"]
    return [*(f"- {item}" for item in gone), *(f"+ {item}" for item in added)]


def _item(field: str, value: Any) -> str:
    """A list entry the way a compose file would write it, where there is a short form."""
    if isinstance(value, Mapping):
        if field == "volumes" and value.get("target"):
            spec = (
                f"{value['source']}:{value['target']}" if value.get("source") else value["target"]
            )
            return f"{spec}:ro" if value.get("read_only") else str(spec)
        if field == "ports" and value.get("target"):
            host = f"{value['host_ip']}:" if value.get("host_ip") else ""
            published = value.get("published")
            spec = f"{host}{published}:{value['target']}" if published else str(value["target"])
            protocol = value.get("protocol") or "tcp"
            return spec if protocol == "tcp" else f"{spec}/{protocol}"
        if field == "devices" and value.get("source"):
            return f"{value['source']}:{value.get('target') or value['source']}"
    return _show(value)


def _show(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str | int | float) for item in value):
        return " ".join(str(item) for item in value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
