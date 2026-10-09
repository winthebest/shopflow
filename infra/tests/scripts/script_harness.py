"""Harness that runs the cloud scripts against fake CLIs: no AWS account, cluster or network is touched."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
FAKE_CLI = Path(__file__).with_name("fake_cli.py")
FAKED_TOOLS = ("aws", "tofu", "kubectl", "helm", "gh", "curl", "uv", "openssl", "htpasswd")
ACCOUNT = "123456789012"
OPERATOR_ARN = f"arn:aws:sts::{ACCOUNT}:assumed-role/shopflow-operator/me"
REAPER_ARN = f"arn:aws:sts::{ACCOUNT}:assumed-role/shopflow-reaper/GitHubActions"
NOT_FOUND = "\nAn error occurred ({code}) when calling the {op} operation: not found\n"

# Operations that change something, per tool. Used to prove dry-run is read-only.
AWS_MUTATING_OPS = re.compile(r"^(put-|delete-|create-|update-|terminate-|release-|publish|attach-|detach-|cp$|rm$|sync$)")
KUBECTL_MUTATING_VERBS = {"apply", "create", "delete", "patch", "exec", "scale", "annotate", "label"}


def aws_operation(argv: list[str]) -> tuple[str, str]:
    """(service, operation) of an aws CLI call, skipping global options."""
    words, skip = [], False
    for arg in argv:
        if skip:
            skip = False
        elif arg in ("--region", "--output", "--profile"):
            skip = True
        elif not arg.startswith("--"):
            words.append(arg)
    return (words + ["", ""])[0], (words + ["", ""])[1]


def is_mutation(tool: str, argv: list[str]) -> bool:
    if tool == "aws":
        operation = aws_operation(argv)[1]
        # update-kubeconfig only writes a local file.
        return operation != "update-kubeconfig" and bool(AWS_MUTATING_OPS.match(operation))
    if tool == "kubectl":
        return any(a in KUBECTL_MUTATING_VERBS for a in argv[:4] + argv[2:6])
    if tool == "helm":
        return any(a in ("upgrade", "install", "uninstall") for a in argv)
    if tool == "tofu":
        return any(a in ("apply", "destroy") for a in argv)
    if tool == "gh":
        return "enable" in argv or "disable" in argv
    if tool == "curl":
        return "POST" in argv
    if tool == "uv":
        return "teardown" in argv and "--dry-run" not in argv
    return False


@dataclass
class Call:
    tool: str
    argv: list[str]
    stdin: str

    @property
    def line(self) -> str:
        return f"{self.tool} {' '.join(self.argv)}"


@dataclass
class Harness:
    tmp: Path
    rules: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for tool in FAKED_TOOLS:
            wrapper = self.bin / tool
            wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I "{FAKE_CLI}" {tool} "$@"\n')
            wrapper.chmod(0o755)
        self.log = self.tmp / "calls.jsonl"
        self.scenario = self.tmp / "scenario.json"
        self.home = self.tmp / "home"
        self.home.mkdir()

    def on(self, tool: str, match: str, stdout=None, *, json_out=None, exit=0, stderr: str = "") -> Harness:
        """Answer calls of `tool` whose arguments match `match` (first matching rule wins)."""
        if json_out is not None:
            stdout = json.dumps(json_out)
        self.rules.append({"tool": tool, "match": match, "stdout": "" if stdout is None else stdout, "exit": exit, "stderr": stderr})
        return self

    def not_found(self, tool: str, match: str, code: str, op: str) -> Harness:
        return self.on(tool, match, exit=254 if tool == "aws" else 1, stderr=NOT_FOUND.format(code=code, op=op))

    def run(self, script: str, *args: str, stdin: str | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        return self.run_at(REPO_ROOT, script, *args, stdin=stdin, env=env)

    def run_at(
        self, root: Path, script: str, *args: str, stdin: str | None = None, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess:
        """Run scripts/<script> of the checkout at `root` (a copy of the repo for tests that change files)."""
        self.scenario.write_text(json.dumps(self.rules))
        full_env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.home),
            "FAKE_LOG": str(self.log),
            "FAKE_SCENARIO": str(self.scenario),
            "CLOUD_OUT_DIR": str(self.tmp / "out"),
            "CLOUD_POLL_SECONDS": "0",
            "LC_ALL": "C",
            **(env or {}),
        }
        return subprocess.run(
            [str(root / "scripts" / script), *args],
            input=stdin,
            capture_output=True,
            text=True,
            env=full_env,
            timeout=120,
            check=False,
        )

    def calls(self, tool: str | None = None) -> list[Call]:
        if not self.log.exists():
            return []
        out = [Call(**json.loads(line)) for line in self.log.read_text().splitlines()]
        return [c for c in out if tool is None or c.tool == tool]

    def mutations(self) -> list[str]:
        return [c.line for c in self.calls() if is_mutation(c.tool, c.argv)]

    def index_of(self, pattern: str) -> int:
        """Position of the first call whose line matches `pattern` (fails the test when absent)."""
        for i, call in enumerate(self.calls()):
            if re.search(pattern, call.line):
                return i
        raise AssertionError(f"no call matches {pattern!r}; calls:\n" + "\n".join(c.line for c in self.calls()))


def operator(h: Harness) -> Harness:
    h.on("aws", r"sts get-caller-identity --query Account", f"{ACCOUNT}\n")
    h.on("aws", r"sts get-caller-identity --query Arn", f"{OPERATOR_ARN}\n")
    return h
