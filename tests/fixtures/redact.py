"""Redact a `docker compose config --format json` document read on stdin.

Environment values (which hold secrets) become "<redacted>"; everything else is kept so the
fixture still exercises real mounts, networks and service shapes.

    ssh cirdan 'cd ~/docker && docker compose config --format json' \
        | python3 tests/fixtures/redact.py > tests/fixtures/cirdan/config.json
"""

import json
import sys

config = json.load(sys.stdin)
for service in config.get("services", {}).values():
    env = service.get("environment")
    if isinstance(env, dict):
        service["environment"] = {key: "<redacted>" for key in env}
json.dump(config, sys.stdout, indent=2, sort_keys=True)
sys.stdout.write("\n")
