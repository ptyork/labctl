from __future__ import annotations

from typing import Any

from labctl.changes import HIDDEN, definition_changes, network_changes
from labctl.compose import canonical
from labctl.state import Network, NodeState


def changes(old: dict[str, Any], new: dict[str, Any], **kwargs: Any) -> list[str]:
    return definition_changes(canonical(old), canonical(new), **kwargs)


def test_what_an_update_changes_field_by_field() -> None:
    old = {
        "image": "nginx:1.27",
        "environment": {"TZ": "UTC", "VPN": "us_atlanta", "OLD": "x"},
        "networks": {"default": None},
        "volumes": [{"type": "bind", "source": "/srv/a/npm", "target": "/data"}],
        "ports": [{"published": "80", "target": 80, "protocol": "tcp"}],
        "command": ["serve", "--fast"],
        "env_file": ["/srv/docker/a.env"],
    }
    new = {
        "image": "nginx:1.28",
        "environment": {"TZ": "UTC", "VPN": "us_east", "PUID": "1000"},
        "networks": {"reverse_proxy": None},
        "volumes": [{"type": "bind", "source": "/srv/a/npm", "target": "/data", "read_only": True}],
        "ports": [{"published": "8080", "target": 80, "protocol": "udp", "host_ip": "10.0.0.5"}],
        "command": ["serve"],
        "container_name": "npm",
        "env_file": ["/srv/docker/b.env"],  # compose folds it into environment: not shown
    }
    assert changes(old, new) == [
        "command: serve --fast → serve",
        "container_name: + npm",
        "environment: - OLD, + PUID=1000, VPN us_atlanta → us_east",
        "image: nginx:1.27 → nginx:1.28",
        "networks: - default, + reverse_proxy",
        "ports: - 80:80, + 10.0.0.5:8080:80/udp",
        "volumes: - /srv/a/npm:/data, + /srv/a/npm:/data:ro",
    ]
    assert changes(old, old) == []
    assert definition_changes("", canonical(new)) == []  # nothing to compare with


def test_secrets_never_show() -> None:
    old = {"environment": {"DB_PASSWORD": "hunter22", "PLEX_CLAIM": "claim-1", "URL": "x"}}
    new = {
        "environment": {"DB_PASSWORD": "hunter33", "API": "s3cr3t-value", "URL": "x"},
        "command": ["run", "--token=s3cr3t-value"],
    }
    lines = changes(old, new, secrets={"s3cr3t-value"})
    assert lines == [
        f"command: (none) → run --token={HIDDEN}",
        "environment: + API, DB_PASSWORD changed (secret), - PLEX_CLAIM",
    ]
    assert not any(s in " ".join(lines) for s in ("hunter", "claim-1", "s3cr3t"))


def test_long_fields_are_counted_past_the_limit() -> None:
    new = {"environment": {f"V{i}": str(i) for i in range(9)}}
    (line,) = changes({"environment": {}}, new, limit=3)
    assert line == "environment: + V0=0, + V1=1, + V2=2, … 6 more"


def test_a_network_that_changes_is_recreated() -> None:
    def state(stack: str, config: dict[str, Any], *others: Network) -> NodeState:
        proxy = Network("reverse_proxy", "reverse_proxy", stack, canonical(config), ("npm",))
        return NodeState.of("bilbo", networks=[proxy, *others])

    ipam = {"driver": "bridge", "ipam": {"config": [{"subnet": "10.11.4.0/24"}]}}
    before = state("docker", ipam, Network("docker_default", "default", "docker", "{}"))
    after = state("homelab-bilbo", {"driver": "bridge", "ipam": {}})
    (change,) = network_changes(before, after)
    assert change.name == "reverse_proxy" and change.stack == "homelab-bilbo"
    assert change.lines == ("subnet 10.11.4.0/24 → picked by Docker",)
    assert change.services == ("npm",)
    assert network_changes(after, after) == []
    assert network_changes(None, after) == []
    attachable = state("homelab-bilbo", {"driver": "bridge", "ipam": {}, "attachable": True})
    (change,) = network_changes(after, attachable)
    assert change.lines == ("attachable: + true",)
