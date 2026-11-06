from __future__ import annotations

from pathlib import Path

import pytest

from labctl.envfile import EnvError, compile_env
from labctl.hostip import default_route
from labctl.remote import Remote, node_remote
from labctl.settings import Secrets


def test_default_route_takes_the_lowest_metric() -> None:
    routes = (
        "default via 10.11.12.1 dev wlan0 proto dhcp src 10.11.12.60 metric 600\n"
        "default via 10.11.12.1 dev eth0 proto dhcp src 10.11.12.50 metric 100\n"
    )
    assert default_route(routes) == ("eth0", "10.11.12.50")
    assert default_route("default via 10.0.0.1 dev enp1s0\n") == ("enp1s0", "")
    assert default_route("10.11.12.0/24 dev eth0 scope link\n") is None


def test_host_ip_marker() -> None:
    calls: list[str] = []

    def lookup() -> str:
        calls.append("asked")
        return "10.11.12.51"

    shared = [{"SERVER_IP": "_HOST_IP_", "URL": "http://${SERVER_IP}:8080"}]
    env = compile_env("cirdan", shared, {"OTHER_IP": "_HOST_IP_"}, Secrets(), [], host_ip=lookup)
    assert env.values["SERVER_IP"] == env.values["OTHER_IP"] == "10.11.12.51"
    assert env.values["URL"] == "http://10.11.12.51:8080"
    assert calls == ["asked"]  # looked up once

    # A literal in node.env wins, and then nothing is looked up.
    calls.clear()
    env = compile_env("cirdan", shared, {"SERVER_IP": "10.0.0.9"}, Secrets(), [], host_ip=lookup)
    assert env.values["SERVER_IP"] == "10.0.0.9" and calls == []
    with pytest.raises(EnvError, match="SERVER_IP is _HOST_IP_"):
        compile_env("cirdan", shared, {}, Secrets(), [])


def test_the_local_node_runs_without_ssh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("socket.gethostname", lambda: "Aragorn")
    assert node_remote("aragorn").local and not node_remote("cirdan").local

    local = Remote("aragorn", local=True)
    assert local.execute(["pwd"], str(tmp_path)).stdout.strip() == str(tmp_path)
    assert local.run_script('echo "$1"', "hi").strip() == "hi"
    source = tmp_path / "source"
    (source / "apps").mkdir(parents=True)
    (source / "apps" / "a.yml").write_text("services: {}\n")
    local.upload(source, str(tmp_path / "node" / "home"))
    assert (tmp_path / "node" / "home" / "apps" / "a.yml").is_file()
    local.download(str(tmp_path / "node" / "home"), ["apps"], tmp_path / "back")
    assert (tmp_path / "back" / "apps" / "a.yml").read_text() == "services: {}\n"
