"""The arms by name, as a run's `--arm` selects them."""

from .direct import W2_PY
from .method import METHOD
from .scenario import BASELINE, Arm

ARMS: dict[str, Arm] = {arm.name: arm for arm in (BASELINE, METHOD, W2_PY)}
