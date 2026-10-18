"""The lab project carries the agent's runtime pins unchanged, so an experiment that drives the agent (agent_safety)
runs in lab's environment on the versions the agent's own tests use: the direct pins in both pyproject files, and
every package of the agent's runtime closure resolved to the same versions in both lockfiles."""

import re
import tomllib

from lab.trino_client import REPO


def requirements(path: str) -> dict[str, str]:
    deps = tomllib.loads((REPO / path).read_text())["project"]["dependencies"]
    return {re.split(r"[\[=<>!~ ]", dep, maxsplit=1)[0].lower(): dep for dep in deps}


def locked(path: str) -> dict[str, list[dict]]:
    packages: dict[str, list[dict]] = {}
    for package in tomllib.loads((REPO / path).read_text())["package"]:
        packages.setdefault(package["name"], []).append(package)
    return packages


def closure(packages: dict[str, list[dict]], root: str) -> set[str]:
    """Every package the root's runtime dependencies pull in, through extras, under any marker (no dev groups)."""
    seen, todo = set(), [root]
    while todo:
        for package in packages[todo.pop()]:
            for dep in package.get("dependencies", []) + [
                d for extra in package.get("optional-dependencies", {}).values() for d in extra
            ]:
                if dep["name"] not in seen:
                    seen.add(dep["name"])
                    todo.append(dep["name"])
    return seen


def test_every_agent_runtime_pin_is_in_lab_unchanged():
    agent, lab = requirements("agent/pyproject.toml"), requirements("lab/pyproject.toml")
    assert agent, "agent/pyproject.toml lists no dependencies"
    assert {name: spec for name, spec in agent.items() if lab.get(name) != spec} == {}


def test_the_agent_runtime_closure_resolves_to_the_same_versions_in_lab():
    agent, lab = locked("agent/uv.lock"), locked("lab/uv.lock")
    names = closure(agent, "shopflow-agent")
    assert len(names) > len(requirements("agent/pyproject.toml")), "the closure should include transitive packages"

    def versions(packages: dict[str, list[dict]], name: str) -> set[str]:
        return {p["version"] for p in packages.get(name, [])}

    assert {name: (versions(agent, name), versions(lab, name)) for name in sorted(names)
            if versions(agent, name) != versions(lab, name)} == {}  # fmt: skip
