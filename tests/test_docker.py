"""The docker command lines (no docker needed: nothing here runs one)."""

from __future__ import annotations

from labctl import docker
from labctl.state import Stack

KEPT = ["--numeric-owner", "--xattrs", "--acls", "--sparse"]
RUN = ["docker", "run", "--rm"]


def test_data_leaves_as_a_tar_from_a_read_only_mount() -> None:
    assert docker.tar_out("/srv/appdata", "plex", "tautulli") == [
        *RUN, "-v", "/srv/appdata:/src:ro", docker.HELPER,
        "tar", "-C", "/src", *KEPT, "-cpf", "-", "--", "plex", "tautulli",
    ]  # fmt: skip
    # Nothing named: everything in it (a volume, or one directory).
    assert docker.tar_out("homelab-frodo_db")[-3:] == ["-cpf", "-", "."]


def test_data_arrives_with_ownership_kept() -> None:
    into = [*RUN, "-i", "-v", "homelab-bilbo_db:/dst", docker.HELPER]
    assert docker.tar_in("homelab-bilbo_db") == [*into, "tar", "-C", "/dst", *KEPT, "-xpf", "-"]
    # Into a folder under it, made first: a migrate's staging folder.
    extract = 'mkdir -p "/dst/$1" && tar -C "/dst/$1" ' + " ".join(KEPT) + " -xpf -"
    assert docker.tar_in("/srv/appdata", ".labctl/incoming/x")[-5:] == [
        "sh", "-c", extract, "sh", ".labctl/incoming/x",
    ]  # fmt: skip


def test_the_helper_mounts_one_directory_or_volume() -> None:
    assert docker.helper("/srv/appdata", "ls /x", "a", read_only=True) == [
        *RUN, "-v", "/srv/appdata:/x:ro", docker.HELPER, "sh", "-c", "ls /x", "sh", "a",
    ]  # fmt: skip
    assert "pgdata:/x" in docker.helper("pgdata", "true")


def test_a_stack_is_named_by_its_project_and_files() -> None:
    stack = Stack("homelab-frodo", ("docker-compose.yml", "media.yml"))
    assert docker.stack_args(stack) == [
        "-p", "homelab-frodo", "-f", "docker-compose.yml", "-f", "media.yml",
    ]  # fmt: skip
    assert docker.stack_args(Stack("", ())) == []
    assert docker.compose("ps", cli="docker.exe") == ["docker.exe", "compose", "ps"]
    assert docker.snapshot("homelab-frodo")[-2:] == [
        "--filter",
        "label=com.docker.compose.project=homelab-frodo",
    ]
