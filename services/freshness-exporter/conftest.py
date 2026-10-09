"""pytest configuration for this standalone uv project.

The shop workspace also runs pytest over services/ (root pyproject.toml, ci.yml) in its own environment, where this
project and its dependencies are not installed. These tests run in data-ci (`make data-exporter-test`) with this
project's lockfile, so the workspace run leaves them out instead of failing on imports.
"""

import importlib.util

if not all(importlib.util.find_spec(module) for module in ("freshness_exporter", "prometheus_client", "trino")):
    collect_ignore_glob = ["tests/*"]
