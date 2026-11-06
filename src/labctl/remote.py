"""Running commands locally or on nodes over the system ssh.

Each node's ssh alias is its node name; user, port, keys and so on come from ~/.ssh/config.
Where OpenSSH supports it (not native Windows), connections are shared: the first call to a node
opens a master connection and later calls reuse it.
"""

from __future__ import annotations

import logging
import os
import shlex
import socket
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Starts a section line in a script's output: "@@LABCTL@@ <section name>".
MARK = "@@LABCTL@@"


def sections(output: str) -> dict[str, list[str]]:
    """A script's output split into its MARK sections (lines before the first are dropped)."""
    found: dict[str, list[str]] = {}
    current: list[str] = []
    for line in output.splitlines():
        if line.startswith(MARK + " "):
            current = []
            found[line[len(MARK) + 1 :]] = current
        else:
            current.append(line)
    return found


class RemoteError(Exception):
    pass


class Unreachable(RemoteError):
    pass


@dataclass(frozen=True)
class Result:
    stdout: str
    stderr: str
    code: int


def execute(
    args: Sequence[str],
    cwd: str,
    remote: Remote | None = None,
    env: Mapping[str, str] | None = None,
) -> Result:
    """Run a command in ``cwd``: locally, or on the node when ``remote`` is given.

    ``env`` replaces the environment of a local command (a remote one gets the node's).
    A command that cannot start (no such program, no such directory) is exit status 127.
    """
    if remote is not None:
        return remote.execute(args, cwd)
    logger.debug("local (in %s): %s", cwd, shlex.join(args))
    try:
        proc = subprocess.run(
            list(args),
            cwd=cwd,
            env=dict(env) if env is not None else None,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, NotADirectoryError) as exc:
        return Result("", str(exc), 127)
    _log_failure("local", proc.returncode, proc.stderr)
    return Result(proc.stdout, proc.stderr, proc.returncode)


@dataclass(frozen=True)
class Remote:
    """A node's shell: over ssh, or this machine's own when ``local`` (the node labctl runs on)."""

    host: str
    local: bool = False

    def argv(self, command: str, tty: bool = False) -> list[str]:
        """The argv running a shell command on the node."""
        if self.local:
            return ["sh", "-c", command]
        return ["ssh", *_ssh_options(), *(["-t"] if tty else []), self.host, command]

    def _unreachable(self, code: int, stderr: str) -> None:
        if code == 255 and not self.local:
            raise Unreachable(f"{self.host}: {stderr.strip() or 'ssh failed'}")

    def execute(self, args: Sequence[str], cwd: str) -> Result:
        """Run a command in ``cwd`` on the node and capture its output."""
        command = f"cd {shlex.quote(cwd)} && {shlex.join(args)}"
        logger.debug("%s: %s", self.host, command)
        proc = subprocess.run(
            self.argv(command), stdin=subprocess.DEVNULL, capture_output=True, text=True
        )
        _log_failure(self.host, proc.returncode, proc.stderr)
        self._unreachable(proc.returncode, proc.stderr)
        return Result(proc.stdout, proc.stderr, proc.returncode)

    def run_script(self, script: str, *args: str) -> str:
        """Run a POSIX sh script on the node with positional arguments; return stdout."""
        command = "sh -s -- " + " ".join(shlex.quote(arg) for arg in args)
        logger.debug("%s: script (%d lines) %s", self.host, script.count("\n"), command)
        proc = subprocess.run(self.argv(command), input=script, capture_output=True, text=True)
        _log_failure(self.host, proc.returncode, proc.stderr)
        self._unreachable(proc.returncode, proc.stderr)
        if proc.returncode:
            raise RemoteError(
                f"{self.host}: remote script failed ({proc.returncode}): {proc.stderr.strip()}"
            )
        return proc.stdout

    def upload(self, local: Path, dest: str) -> None:
        """Copy a local folder's contents into ``dest`` on the node (made if needed), modes kept."""
        command = f"mkdir -p {shlex.quote(dest)} && tar -C {shlex.quote(dest)} -xpf -"
        self._pipe(
            ["tar", "-C", str(local), "-cf", "-", "."], self.argv(command), f"upload to {dest}"
        )

    def download(self, cwd: str, names: Sequence[str], local: Path) -> None:
        """Copy ``names`` (relative to ``cwd`` on the node) into the local folder ``local``."""
        local.mkdir(parents=True, exist_ok=True)
        command = f"cd {shlex.quote(cwd)} && tar -cf - -- {shlex.join(names)}"
        self._pipe(
            self.argv(command), ["tar", "-C", str(local), "-xpf", "-"], f"download from {cwd}"
        )

    def _pipe(self, source: list[str], sink: list[str], what: str) -> None:
        """``source | sink``; either may be the node's side."""
        logger.debug("%s: %s | %s", self.host, shlex.join(source), shlex.join(sink))
        first = subprocess.Popen(source, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        second = subprocess.run(sink, stdin=first.stdout, capture_output=True)
        if first.stdout:
            first.stdout.close()
        errors = (first.stderr.read() if first.stderr else b"") + second.stderr
        first_code = first.wait()
        if first_code or second.returncode:
            stderr = errors.decode(errors="replace").strip()
            self._unreachable(255 if 255 in (first_code, second.returncode) else 0, stderr)
            raise RemoteError(f"{self.host}: {what} failed: {stderr}")

    def run(self, command: str, tty: bool = False) -> int:
        """Run a shell command on the node, its output going straight to the terminal.

        Returns the command's exit status. With ``tty``, ssh allocates a terminal so progress
        output renders and Ctrl-C reaches the remote command.
        """
        logger.debug("%s: %s", self.host, command)
        proc = subprocess.run(self.argv(command, tty))
        self._unreachable(proc.returncode, "")
        return proc.returncode

    def stream(self, command: str) -> subprocess.Popen[str]:
        """Start a shell command on the node; read its output (stderr too) line by line.

        Over ssh with a terminal (-tt), so the command gets a hangup and ends when ssh does
        (without one it would run on after ssh is gone); -q leaves out ssh's own messages.
        """
        logger.debug("%s: %s (streamed)", self.host, command)
        argv = (
            self.argv(command)
            if self.local
            else ["ssh", *_ssh_options(), "-tt", "-q", self.host, command]
        )
        return subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )

    @staticmethod
    def close_all(hosts: Iterable[str]) -> None:
        """Close shared master connections opened by this run (no-op where unsupported)."""
        if os.name == "nt":
            return
        for host in hosts:
            subprocess.run(["ssh", *_ssh_options(), "-O", "exit", host], capture_output=True)


def relay(source: Remote, read: Sequence[str], target: Remote, write: Sequence[str]) -> None:
    """Stream one node's command output into another node's command, through this machine."""
    logger.debug("%s: %s | %s: %s", source.host, shlex.join(read), target.host, shlex.join(write))
    first = subprocess.Popen(
        source.argv(shlex.join(read)), stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    second = subprocess.run(target.argv(shlex.join(write)), stdin=first.stdout, capture_output=True)
    if first.stdout:
        first.stdout.close()
    read_errors = first.stderr.read() if first.stderr else b""
    if first.wait() or second.returncode:
        errors = (read_errors + second.stderr).decode(errors="replace").strip()
        raise RemoteError(f"copy {source.host} → {target.host} failed: {errors}")


def _log_failure(where: str, code: int, stderr: str) -> None:
    if code:
        logger.debug("%s: exit %d: %s", where, code, stderr.strip())


def node_remote(node: str) -> Remote:
    """The node's shell. The node named like this machine (ignoring case) is run locally."""
    return Remote(node, local=node.lower() == socket.gethostname().lower())


def _ssh_options() -> list[str]:
    options = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    if os.name != "nt":
        options += [
            "-o",
            "ControlMaster=auto",
            "-o",
            "ControlPath=~/.ssh/labctl-%C",
            "-o",
            "ControlPersist=120",
        ]
    return options
