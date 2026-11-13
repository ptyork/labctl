"""A node's IP address: for `_HOST_IP_` values in env files, and for `labctl status`.

- A remote node: the address ssh connects to (its HostName from ~/.ssh/config, then DNS). That is
  the address the node is reached at; a node reached over another network (Tailscale, a VPN) needs
  its address set in node.env.
- The node labctl runs on: ssh would say 127.0.0.1, so it is the address of the interface with the
  real default route (0.0.0.0/0, lowest metric). Inside WSL, Linux only sees its NAT address, so
  Windows is asked. VPN split routes and Docker bridges never hold that route, and with wired and
  Wi-Fi both up the OS's own metric decides. Nothing here guesses from adapter names.
"""

from __future__ import annotations

import re
import socket
import subprocess
from pathlib import Path

from labctl.envfile import EnvError
from labctl.remote import Remote, node_remote

# Windows: the IPv4 address of the interface holding the lowest-metric default route.
_WINDOWS_DEFAULT_ROUTE = (
    "$r = Get-NetRoute -AddressFamily IPv4 -DestinationPrefix 0.0.0.0/0"
    " -ErrorAction SilentlyContinue"
    " | Sort-Object { $_.RouteMetric + $_.InterfaceMetric } | Select-Object -First 1; "
    "if ($r) { (Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $r.ifIndex"
    ' | Where-Object { $_.IPAddress -notmatch "^169\\.254\\." }).IPAddress }'
)


def host_ip(remote: Remote) -> str:
    """The node's address, or EnvError saying how to set it by hand."""
    address = local_ip() if remote.local else ssh_address(remote.host)[1]
    if not address:
        how = "no default route" if remote.local else "its ssh host name does not resolve"
        raise EnvError(
            f"{remote.host}: cannot find the node's address for _HOST_IP_ ({how}); set the "
            f"variable in nodes/{remote.host}/node.env instead"
        )
    return address


def node_address(node: str) -> tuple[str, str]:
    """(ssh host name, address) the node is reached at ("" when it does not resolve); the node
    labctl runs on is ``local``, with its own address."""
    if node_remote(node).local:
        return "local", local_ip()
    return ssh_address(node)


def ssh_address(node: str) -> tuple[str, str]:
    """The HostName ssh would connect to for the node's alias, and its IP ("" if unresolved).

    Local only: `ssh -G` prints the effective config without connecting.
    """
    host = node
    try:
        out = subprocess.run(
            ["ssh", "-G", node], capture_output=True, text=True, stdin=subprocess.DEVNULL
        ).stdout
        host = next(
            (line.split(None, 1)[1] for line in out.splitlines() if line.startswith("hostname ")),
            node,
        )
    except OSError:
        pass
    try:
        return host, socket.gethostbyname(host)
    except OSError:
        return host, ""


def local_ip() -> str:
    """This machine's address on its default-route interface ("" if there is none)."""
    if in_wsl():
        try:
            out = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", _WINDOWS_DEFAULT_ROUTE],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
            ).stdout
        except OSError:
            return ""
        return next((line.strip() for line in out.splitlines() if line.strip()), "")
    try:
        routes = subprocess.run(
            ["ip", "-4", "route", "show", "default"], capture_output=True, text=True
        ).stdout
    except OSError:
        return ""
    route = default_route(routes)
    if route is None:
        return ""
    device, source = route
    if source:
        return source
    addresses = subprocess.run(
        ["ip", "-4", "-o", "addr", "show", "dev", device, "scope", "global"],
        capture_output=True,
        text=True,
    ).stdout
    match = re.search(r"\binet (\d+\.\d+\.\d+\.\d+)", addresses)
    return match.group(1) if match else ""


def default_route(routes: str) -> tuple[str, str] | None:
    """(device, source address or "") of the lowest-metric route in `ip -4 route show default`."""
    best: tuple[int, str, str] | None = None
    for line in routes.splitlines():
        words = line.split()
        if not words or words[0] != "default" or "dev" not in words:
            continue
        metric = int(words[words.index("metric") + 1]) if "metric" in words else 0
        source = words[words.index("src") + 1] if "src" in words else ""
        candidate = (metric, words[words.index("dev") + 1], source)
        if best is None or candidate[0] < best[0]:
            best = candidate
    return (best[1], best[2]) if best else None


def in_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/sys/kernel/osrelease").read_text().lower()
    except OSError:
        return False
