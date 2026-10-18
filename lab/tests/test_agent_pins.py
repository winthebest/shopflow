"""The lab project carries the agent's runtime pins unchanged, so an experiment that drives the agent (agent_safety)
runs in lab's environment on the versions the agent's own tests use."""

import re
import tomllib

from lab.trino_client import REPO


def requirements(path: str) -> dict[str, str]:
    deps = tomllib.loads((REPO / path).read_text())["project"]["dependencies"]
    return {re.split(r"[\[=<>!~ ]", dep, maxsplit=1)[0].lower(): dep for dep in deps}


def test_every_agent_runtime_pin_is_in_lab_unchanged():
    agent, lab = requirements("agent/pyproject.toml"), requirements("lab/pyproject.toml")
    assert agent, "agent/pyproject.toml lists no dependencies"
    assert {name: spec for name, spec in agent.items() if lab.get(name) != spec} == {}
