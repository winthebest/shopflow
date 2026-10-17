"""The lab runs the platform's images: every lab image that the platform also runs is pinned to the same digest."""

import re

import pytest
import yaml

from lab.trino_client import REPO

# Lab image -> the platform component that pins it (deploy/platform/<component>).
PLATFORM_COMPONENT = {
    "trinodb/trino": "trino",
    "apache/polaris": "iceberg-catalog",
    "apache/polaris-admin-tool": "iceberg-catalog",
    "chrislusf/seaweedfs": "seaweedfs",
    "otel/opentelemetry-collector-k8s": "otel-collector",
}
# Images only the lab has: the catalog's Postgres (the platform's runs under CNPG), the setup helper, the spike's OPA.
LAB_ONLY = {"postgres", "python", "openpolicyagent/opa"}
COMPOSE_FILES = ["lab/compose.yml", "lab/spike/compose.yml"]


def images() -> list[tuple[str, str, str]]:
    found = []
    for path in COMPOSE_FILES:
        for service, spec in yaml.safe_load((REPO / path).read_text())["services"].items():
            if "image" in spec:
                found.append((f"{path}:{service}", *spec["image"].split(":", 1)))
    return found


@pytest.mark.parametrize(("where", "name", "pin"), images())
def test_every_lab_image_is_pinned_by_digest(where: str, name: str, pin: str):
    assert re.fullmatch(r"[^@]+@sha256:[0-9a-f]{64}", pin), f"{where}: {name}:{pin} has no digest"
    assert name in PLATFORM_COMPONENT or name in LAB_ONLY, f"{where}: {name} is neither the platform's nor lab-only"


@pytest.mark.parametrize(("where", "name", "pin"), [i for i in images() if i[1] in PLATFORM_COMPONENT])
def test_shared_images_have_the_platform_digest(where: str, name: str, pin: str):
    tag, digest = pin.split("@")
    component = REPO / "deploy/platform" / PLATFORM_COMPONENT[name]
    manifests = "\n".join(p.read_text() for p in sorted(component.rglob("*.yaml")))
    assert digest in manifests, f"{where}: {name}:{tag} digest differs from {component.relative_to(REPO)}"
    assert re.search(rf"\b{re.escape(tag)}\b", manifests), f"{where}: tag {tag} not in {component.relative_to(REPO)}"
