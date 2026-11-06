# labctl

CLI utility tool for orchestrating multi-node homelab Docker environments using `click`, `rich`, and Git-ops.

## Features
- node status and application monitoring
- multi-node compose file updates
- backup & restore utilities

## Development Setup

The project uses `uv` for python environment and dependency management.

```bash
# Sync environment
uv sync

# Run the CLI tool
uv run labctl --help
```
