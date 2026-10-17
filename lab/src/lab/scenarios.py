"""An experiment's scenarios: lab/scenarios/<exp>/dev.yaml in the clear, holdout.sops.yaml encrypted with SOPS.

Both files hold {"scenarios": [{"id": ..., ...}, ...]}. The held-out file is written by the user or another session,
never by the lane that tunes the method, and is encrypted to the repository's age recipient (.sops.yaml) with the
public key only: `python -m lab.scenarios encrypt <plain.yaml> <exp>`. Only the held-out run decrypts it, in memory
(`sops -d` with the caller's own key); no plaintext is written. After the final held-out run, `publish` writes the
decrypted file next to it (holdout.yaml) so others can reproduce the run.

The files may live elsewhere when another lane owns them (e.g. the agent eval's sets in evals/datasets/operator/):
lab/scenarios/<exp>/location then holds one repository-relative directory under evals/, read without running any of
the experiment's code (the held-out runner loads that only after its checks). Both places are frozen.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

from lab.trino_client import REPO

_EXPERIMENT = re.compile(r"[a-z][a-z0-9_]{0,63}")


class ScenarioError(ValueError):
    """Scenario files missing or malformed."""


LOCATION_ROOTS = ("evals/",)  # where a location file may point: frozen, owned by another lane


def scenario_dir(experiment: str, root: Path = REPO) -> Path:
    """lab/scenarios/<exp>, or the directory its `location` file names."""
    if not _EXPERIMENT.fullmatch(experiment):
        raise ScenarioError(f"experiment name {experiment!r}: [a-z][a-z0-9_]*")
    home = root / "lab/scenarios" / experiment
    pointer = home / "location"
    if not pointer.is_file():
        return home
    target = pointer.read_text().strip()
    if (
        not target.startswith(LOCATION_ROOTS)
        or Path(target).is_absolute()
        or ".." in Path(target).parts
        or not re.fullmatch(r"[a-z0-9_/.-]+", target)
    ):
        raise ScenarioError(f"{pointer}: {target!r} must be a relative directory under {', '.join(LOCATION_ROOTS)}")
    return root / target


def _scenarios(data: object, source: str) -> list[dict]:
    items = data.get("scenarios") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise ScenarioError(f"{source}: expected {{scenarios: [...]}} with at least one scenario")
    ids = [item.get("id") if isinstance(item, dict) else None for item in items]
    if None in ids or len(set(ids)) != len(ids):
        raise ScenarioError(f"{source}: every scenario needs a unique id")
    return items


def load_dev(experiment: str, root: Path = REPO) -> list[dict]:
    path = scenario_dir(experiment, root) / "dev.yaml"
    if not path.is_file():
        raise ScenarioError(f"no dev scenarios: {path}")
    return _scenarios(yaml.safe_load(path.read_text()), str(path))


def holdout_file(experiment: str, root: Path = REPO) -> Path:
    return scenario_dir(experiment, root) / "holdout.sops.yaml"


def _sops(*args: str) -> str:
    # sops with fixed flags and repository paths; it finds the caller's key in its own environment.
    result = subprocess.run(["sops", *args], capture_output=True, text=True)  # noqa: S603, S607
    if result.returncode:
        raise ScenarioError(f"sops {args[0]} failed (exit {result.returncode}): {result.stderr.strip()[-300:]}")
    return result.stdout


def decrypt_holdout(experiment: str, root: Path = REPO) -> list[dict]:
    """The held-out scenarios, decrypted in memory (YAML in, JSON out); nothing is written to disk."""
    path = holdout_file(experiment, root)
    if not path.is_file():
        raise ScenarioError(f"no held-out scenarios: {path}")
    return _scenarios(json.loads(_sops("-d", "--input-type", "yaml", "--output-type", "json", str(path))), str(path))


def repository_recipient(root: Path = REPO) -> str:
    """The one age recipient of the repository's .sops.yaml creation rules."""
    rules = yaml.safe_load((root / ".sops.yaml").read_text())["creation_rules"]
    recipients = {rule["age"] for rule in rules if "age" in rule}
    if len(recipients) != 1:
        raise ScenarioError(f".sops.yaml: expected one age recipient, found {len(recipients)}")
    return recipients.pop()


def encrypt(plain: Path, experiment: str, recipient: str, root: Path = REPO) -> Path:
    """Encrypts `plain` (kept outside git) to `recipient` only, into lab/scenarios/<exp>/holdout.sops.yaml.

    `--config /dev/null`: the repository's .sops.yaml has no rule for these files, and the recipient is given
    explicitly, so no creation rule (and no other key) can change who can read it.
    """
    _scenarios(yaml.safe_load(plain.read_text()), str(plain))
    out = holdout_file(experiment, root)
    if out.exists():
        raise ScenarioError(f"{out} exists: held-out scenarios are written once")
    text = _sops(
        "--config", "/dev/null", "-e", "--age", recipient, "--input-type", "yaml", "--output-type", "yaml", str(plain)
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    return out


def publish(experiment: str, root: Path = REPO) -> Path:
    """holdout.yaml in the clear, next to the encrypted file; lab.runner allows it only after the final run."""
    out = scenario_dir(experiment, root) / "holdout.yaml"
    out.write_text(yaml.safe_dump({"scenarios": decrypt_holdout(experiment, root)}, sort_keys=False))
    return out


def main() -> None:
    if len(sys.argv) != 4 or sys.argv[1] != "encrypt":
        sys.exit("usage: python -m lab.scenarios encrypt <plain.yaml> <experiment>")
    print(encrypt(Path(sys.argv[2]), sys.argv[3], repository_recipient()))


if __name__ == "__main__":
    main()
