"""The experiment imports the agent and its eval (agent/, evals/) from the repository root, as lab.runner's loader
makes possible when it runs the experiment; the tests here need the same."""

import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[3])
if ROOT not in sys.path:
    sys.path.append(ROOT)
