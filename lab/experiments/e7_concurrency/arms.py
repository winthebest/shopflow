"""The arms by name, as a run's `--arm` selects them."""

from .direct import W2_PY
from .method import METHOD, METHOD_RESOLVE
from .scenario import BASELINE, Arm

ARMS: dict[str, Arm] = {arm.name: arm for arm in (BASELINE, METHOD, METHOD_RESOLVE, W2_PY)}
