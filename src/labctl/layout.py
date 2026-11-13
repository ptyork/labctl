"""Where labctl keeps its own things on a node. Names only: nothing here reads or writes.

    DEPLOY_DIR/.labctl/applied.json    the record of the last apply (collect.Record)
    DEPLOY_DIR/.labctl/managed         the top-level names that apply wrote
    DEPLOY_DIR/.labctl/archive/        files apply removed from DEPLOY_DIR: <name>-<timestamp>
    APPDATA_DIR/.labctl/archive/       data no service uses any more: <key>-<timestamp>, each
                                       with a sidecar <entry>.labctl.json (restore.py)
    ~/.labctl-lock/                    held by a running apply
    ~/.labctl-home                     where the last apply put DEPLOY_DIR

The last two are in the ssh user's home: they do not move with DEPLOY_DIR.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from labctl.state import is_volume, volume_name

# Reserved for labctl at the top of DEPLOY_DIR and of APPDATA_DIR.
LABCTL_DIR = ".labctl"

# Under APPDATA_DIR: the archive of data (removed services, migrate sources, replaced data).
ARCHIVE_DIR = f"{LABCTL_DIR}/archive"
# Next to each entry of the data archive, describing it: <entry>.labctl.json.
SIDECAR = ".labctl.json"

# Under DEPLOY_DIR.
RECORD = f"{LABCTL_DIR}/applied.json"
MANAGED = f"{LABCTL_DIR}/managed"
FILES_ARCHIVE = f"{LABCTL_DIR}/archive"

# In the ssh user's home on the node.
LOCK = ".labctl-lock"
HOME_POINTER = ".labctl-home"


def archive_path(root: str, key: str) -> str:
    """Where data is archived: <key> in <root>'s archive, a volume as volume-<name> (the executor
    appends -<timestamp>)."""
    return str(PurePosixPath(root) / ARCHIVE_DIR / archive_name(key))


def archive_name(key: str) -> str:
    return f"volume-{volume_name(key)}" if is_volume(key) else key
