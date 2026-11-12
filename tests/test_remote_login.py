"""ssh logins that need a person: asked once, in a terminal, and the call made again."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from labctl import remote as remote_module
from labctl.remote import Remote, Unreachable, wants_login

DENIED = "paul@frodo: Permission denied (publickey,keyboard-interactive).\n"


def test_which_refusals_a_person_could_answer() -> None:
    assert wants_login(DENIED)
    assert wants_login("Permission denied (publickey,password).")
    assert not wants_login("Permission denied (publickey).")  # only keys: nothing to type
    assert not wants_login("ssh: connect to host frodo port 22: Connection timed out")


def runs(*results: tuple[int, str]) -> tuple[list[list[str]], Any]:
    """A subprocess.run giving these (code, stderr) in turn; the argv it was called with."""
    calls: list[list[str]] = []
    left = list(results)

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        code, stderr = left.pop(0)
        return subprocess.CompletedProcess(argv, code, "ok\n" if not code else "", stderr)

    return calls, run


def test_a_refused_call_logs_in_and_runs_again(monkeypatch: pytest.MonkeyPatch) -> None:
    logins: list[str] = []
    monkeypatch.setattr(remote_module, "login", lambda host, timeout: logins.append(host) or True)
    calls, run = runs((255, DENIED), (0, ""))
    monkeypatch.setattr(subprocess, "run", run)
    result = Remote("frodo").execute(["true"], "/")
    assert result.code == 0 and logins == ["frodo"] and len(calls) == 2


def test_no_login_means_the_node_cannot_be_reached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote_module, "login", lambda host, timeout: False)
    _, run = runs((255, DENIED))
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(Unreachable, match="Permission denied"):
        Remote("frodo").execute(["true"], "/")


def test_without_a_terminal_nothing_is_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote_module.sys.stdin, "isatty", lambda: False)
    calls, run = runs()
    monkeypatch.setattr(subprocess, "run", run)
    assert remote_module.login("frodo") is False and calls == []
