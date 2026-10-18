"""E5, retention policy per table profile (docs/research/e5-retention-policy-design.md).

So far this module holds the scenario check the lab runner and `lab.scenarios` call. `run`, the simulator, follows
the first lab session's measurements: the design fixes them before any modelling, and until then the runner refuses
a run of this experiment because it defines no `run`.
"""

from e5_retention.params import ParamsError, check_scenarios

__all__ = ["ParamsError", "check_scenarios"]
