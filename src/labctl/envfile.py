"""Env compile: nodes/_shared/*.env, then node.env, then secrets, into one flat env per node.

Values are fully expanded here ($VAR / ${VAR} references between variables), so the .env written
into the bundle is plain literal values that compose does not need to interpolate further.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from labctl.settings import Secrets

NODE_ENV = "node.env"  # in nodes/<node>/: compiled into the bundle's .env, never copied itself
SECRET = "_SECRET_"
HOST_IP = "_HOST_IP_"  # filled with the node's address (see labctl.hostip)
# Absolute-path shaped: most variables are paths, and compose reads a relative bind source as a
# named volume and rejects the project.
PLACEHOLDER_PREFIX = "/labctl-missing/"

VAR_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_REF = re.compile(rf"\$(?:({VAR_NAME.pattern})|\{{({VAR_NAME.pattern})\}})")

# (node, variable, is_secret, suggested value or "") -> value, or None to leave a placeholder.
Prompt = Callable[[str, str, bool, str], str | None]


class EnvError(Exception):
    pass


def parse_env(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[key.strip()] = value
    return result


@dataclass(frozen=True)
class CompiledEnv:
    values: Mapping[str, str]
    placeholders: tuple[str, ...] = ()  # variables left unanswered
    new_node_vars: Mapping[str, str] = field(default_factory=dict)  # to append to node.env
    # key -> (value, is_global): answers to save to secrets.toml
    new_secrets: Mapping[str, tuple[str, bool]] = field(default_factory=dict)
    secret_keys: tuple[str, ...] = ()  # variables whose value is a secret (never shown)
    host_ip: str = ""  # the node's address, when a `_HOST_IP_` value needed it

    @property
    def secret_values(self) -> frozenset[str]:
        return frozenset(
            self.values[secret_key]
            for secret_key in self.secret_keys
            if self.values.get(secret_key)
        )


def compile_env(
    node: str,
    shared: Iterable[Mapping[str, str]],
    node_vars: Mapping[str, str],
    secrets: Secrets,
    referenced: Iterable[str],
    prompt: Prompt | None = None,
    suggestions: Mapping[str, str] | None = None,
    host_ip: Callable[[], str] | None = None,
) -> CompiledEnv:
    """
    Merge, fill secrets and the host IP, find missing variables, expand references.

    Args:
        node (str):
            The node's name.
        shared (Iterable[Mapping[str, str]]):
            Each nodes/_shared/*.env, in name order.
        node_vars (Mapping[str, str]):
            The node's node.env.
        secrets (Secrets):
            The values for ``_SECRET_`` variables.
        referenced (Iterable[str]):
            Every variable the node's compose files use (``docker compose config --variables``).
        prompt (Prompt | None):
            Asks for missing values; with none, or no answer, a variable gets a placeholder and is
            listed in ``placeholders`` (default: None).
        suggestions (Mapping[str, str] | None):
            A variable -> another whose value is offered as the prompt's default (e.g. DEPLOY_DIR
            -> APPDATA_DIR) (default: None).
        host_ip (Callable[[], str] | None):
            The node's address for `_HOST_IP_` values; only called when one is used
            (default: None).

    Returns:
        CompiledEnv:
            The expanded values, the placeholders, and the answers to save.

    Raises:
        EnvError:
            A `_HOST_IP_` value without ``host_ip``, or a circular reference.
    """
    merged: dict[str, str] = {}
    shared_keys: set[str] = set()
    for values in shared:
        merged.update(values)
        shared_keys.update(values)
    merged.update(node_vars)

    placeholders: list[str] = []
    new_node_vars: dict[str, str] = {}
    new_secrets: dict[str, tuple[str, bool]] = {}

    def suggest(name: str) -> str:
        source = (suggestions or {}).get(name)
        if source is None or source not in merged or merged[source].startswith(PLACEHOLDER_PREFIX):
            return ""
        try:
            return _expand(source, merged)
        except EnvError:
            return ""

    def ask(name: str, secret: bool) -> str:
        answer = prompt(node, name, secret, "" if secret else suggest(name)) if prompt else None
        if answer is None:
            placeholders.append(name)
            return PLACEHOLDER_PREFIX + name
        if secret:
            new_secrets[name] = (answer, name in shared_keys and name not in node_vars)
        else:
            new_node_vars[name] = answer
        return answer

    address = ""
    secret_keys = tuple(sorted(key for key, value in merged.items() if value == SECRET))
    for key, value in list(merged.items()):
        if value == SECRET:
            known = secrets.get(node, key)
            merged[key] = known if known is not None else ask(key, secret=True)
        elif value == HOST_IP:
            if host_ip is None:
                raise EnvError(f"{node}: {key} is {HOST_IP}, but the node's address is not known")
            address = address or host_ip()
            merged[key] = address

    wanted = set(referenced)
    for value in list(merged.values()):
        wanted.update(match.group(1) or match.group(2) for match in _REF.finditer(value))
    for name in sorted(wanted - merged.keys()):
        merged[name] = ask(name, secret=False)

    return CompiledEnv(
        values={key: _expand(key, merged) for key in merged},
        placeholders=tuple(placeholders),
        new_node_vars=new_node_vars,
        new_secrets=new_secrets,
        secret_keys=secret_keys,
        host_ip=address,
    )


def render_env(values: Mapping[str, str], node: str) -> str:
    """A .env compose reads literally: single-quoted (no interpolation)."""
    lines = [f"# Generated by labctl for '{node}'. DO NOT EDIT; DO NOT COMMIT.", ""]
    for key in sorted(values):
        value = values[key]
        if "'" not in value:
            lines.append(f"{key}='{value}'")
        elif "$" not in value and "\n" not in value:
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{key}="{escaped}"')
        else:
            raise EnvError(
                f"{node}: value of {key} contains both ' and $ (or a newline); unsupported"
            )
    return "\n".join(lines) + "\n"


def append_node_vars(path: Path, new_vars: Mapping[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    while lines and not lines[-1].strip():
        lines.pop()
    if lines:
        lines.append("")
    lines.append("# Added by labctl (referenced by compose files but missing)")
    for key, value in sorted(new_vars.items()):
        needs_quotes = any(c in value for c in " \t#$\"'\\")
        lines.append(f'{key}="{value}"' if needs_quotes else f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def node_env_file(nodes_dir: Path, node: str) -> Path:
    return nodes_dir / node / NODE_ENV


def read_sources(nodes_dir: Path, node: str) -> tuple[list[dict[str, str]], dict[str, str]]:
    """The env inputs for a node: each nodes/_shared/*.env (in name order), and its node.env."""
    shared = [
        parse_env(path.read_text(encoding="utf-8"))
        for path in sorted((nodes_dir / "_shared").glob("*.env"))
    ]
    path = node_env_file(nodes_dir, node)
    own = parse_env(path.read_text(encoding="utf-8")) if path.is_file() else {}
    return shared, own


def preview_env(nodes_dir: Path, node: str) -> dict[str, str]:
    """A node's env without secrets or prompts, expanded where possible (for display)."""
    shared, own = read_sources(nodes_dir, node)
    merged = {k: v for values in [*shared, own] for k, v in values.items()}
    expanded: dict[str, str] = {}
    for key in merged:
        try:
            expanded[key] = _expand(key, merged)
        except EnvError:
            expanded[key] = merged[key]
    return expanded


def _expand(key: str, merged: Mapping[str, str], seen: frozenset[str] = frozenset()) -> str:
    if key in seen:
        raise EnvError(f"Circular variable reference: {' -> '.join([*seen, key])}")

    def replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        return _expand(name, merged, seen | {key}) if name in merged else match.group(0)

    return _REF.sub(replace, merged[key])
