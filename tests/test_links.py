"""Containers linked across stacks must run on one node."""

from __future__ import annotations

from test_runtime import desired

from labctl.links import container_names, links, split_links

VPN = {"gluetun": {"container_name": "gluetun"}}
TORRENT = {"transmission": {"network_mode": "container:gluetun"}}
BACKUP = {"backup": {"volumes_from": ["container:homelab-a-db-1:ro", "service:other"]}}


def test_names_and_links() -> None:
    assert container_names(desired("a", {"homelab-a": {"db": {}, **VPN}})) == {
        "homelab-a-db-1": "db",
        "gluetun": "gluetun",
    }
    assert links(TORRENT["transmission"]) == [("gluetun", "network_mode")]
    assert links(BACKUP["backup"]) == [("homelab-a-db-1", "volumes_from")]
    assert links({"network_mode": "service:vpn"}) == []  # one project: compose checks it


def test_a_link_to_a_container_on_another_node_is_a_problem() -> None:
    lab = {
        "a": desired("a", {"homelab-a": {"db": {}, **VPN}}),
        "b": desired("b", {"homelab-b": {**TORRENT, **BACKUP}, "byhand": {}}),
    }
    assert split_links(lab) == [
        "b: backup uses container homelab-a-db-1 (volumes_from), which the lab config runs on a; "
        "both must be on one node",
        "b: transmission uses container gluetun (network_mode), which the lab config runs on a; "
        "both must be on one node",
    ]
    assert split_links(lab, ["a"]) == []  # only the nodes named are checked
    together = {"a": desired("a", {"homelab-a": {**VPN, **TORRENT}})}
    assert split_links(together) == []
    unknown = {"b": desired("b", {"homelab-b": {"t": {"network_mode": "container:by-hand"}}})}
    assert split_links(unknown) == []  # not the lab config's: not judged
