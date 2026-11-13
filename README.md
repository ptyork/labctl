# labctl

Keeps the Docker Compose stacks of several machines in one git repository, and makes the
machines match it: `labctl plan` shows what would change, `labctl apply` does it.

labctl is pre-alpha: commands and file layouts may still change without notice.

## How it works

- **The lab config** is a git repository you edit by hand. Each folder in `nodes/` is one node,
  named after its ssh host: its compose files, and a `node.env` with at least `DEPLOY_DIR`
  (where the stacks go on the node) and `APPDATA_DIR` (where the services keep their data).
  `nodes/_shared/` holds env files every node gets. How you organise the rest of the repository
  (shared compose files, includes) is up to you.
- **Plan, then apply.** `plan` reads the nodes over ssh and compares them with the lab config:
  services added, removed, updated, or moved to another node. `apply` shows the same plan, asks,
  and carries it out. If a step fails, the steps already done are undone in reverse.
- **Data moves with its service.** A service that moves from one node to another takes its data
  under `APPDATA_DIR` (and its named volumes) along: copied, verified, then started.
- **Archive, never delete.** Data nothing uses any more is moved to an archive folder on its
  node. Only `labctl archive prune` deletes, and only what it showed first.
- **Only apply commits.** A successful apply commits what it applied, tags the commit for each
  node, and pushes. `labctl history` lists the applies and can put the files back as they were.
- **Secrets** live in `secrets.toml`, outside the lab config, and are written into each node's
  `.env` on apply.

## Commands

| Command | What it does |
| --- | --- |
| `init [DIRECTORY]` | Set up a lab config: the folders, a git repository, the first commit. |
| `add NODE` | Add a node, taking in the stacks it already runs. |
| `remove NODE` | Take a node out; the next apply retires it. |
| `validate` | Check the lab config without touching a node. |
| `plan [NODE...]` | Show what apply would change. Never changes a node. |
| `apply [NODE...]` | Make the nodes match the lab config, then commit, tag and push. |
| `discard` | Undo the changes no apply has committed (kept in a git stash). |
| `history list\|show\|diff\|restore` | The applies, and going back to one. |
| `node status\|up\|down\|restart\|update\|logs\|prune` | Compose commands on the deployed stacks. |
| `ports [NODE...]` | The host ports in use, those the lab config wants, and clashes. |
| `archive list\|prune` | See and prune what apply archived on the nodes. |
| `unlock NODE` | Remove the lock an interrupted apply left behind. |
| `status` | labctl's environment and each node's key settings. |
| `settings`, `secrets` | View and edit `labctl.toml` and `secrets.toml`. |

Every command explains itself: `labctl COMMAND --help`.

## Getting started

```bash
labctl init ~/lab            # a new lab config, made labctl's lab_config
labctl add bilbo             # bilbo is an ssh host; its running stacks are imported
labctl plan bilbo            # what the first apply would do
labctl apply bilbo
```

The nodes need Docker with Compose v2, and an ssh user who may run `docker`. The workstation
needs `git`, `ssh` and the `docker` CLI (compose files are resolved locally).

labctl finds the lab config from `$LABCTL_LAB_CONFIG`, then from `lab_config` in `labctl.toml`
(`labctl settings`).

## Development

The project uses `uv`. For now it depends on three libraries by local path (`craftable`,
`get_rich`, `git_wrap`, expected next to the folder that holds `labctl/`), so it only builds
where those are checked out.

```bash
uv sync
uv run labctl --help

uv run pytest -q
uv run mypy
uv run ruff check src tests
uv run ruff format src tests
```

Code layout: commands under `src/labctl/cli/` only ask and print. What they do is in the
modules of `src/labctl/`, each named for what it holds (`repo.py`, `node.py`, `planner.py`,
`apply.py`, …).
