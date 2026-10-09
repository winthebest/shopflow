"""Stand-in for aws, tofu, kubectl, helm, gh, curl and uv in the cloud script tests.

It never talks to anything: it records each call (argv, and stdin when a script pipes data in)
to $FAKE_LOG and answers from the rules in $FAKE_SCENARIO. A rule matches a tool plus a regex on
the joined arguments; `stdout`/`exit` may be lists, consumed one item per matching call (the last
item repeats), which lets a test model state that changes over time (a backup completing, a load
balancer disappearing). Unmatched calls succeed with no output.
"""

from __future__ import annotations

import json
import os
import re
import sys


def main() -> int:
    tool, argv = sys.argv[1], sys.argv[2:]
    joined = " ".join(argv)
    reads_stdin = "file:///dev/stdin" in argv or (tool == "kubectl" and ("-i" in argv or argv[-2:] == ["-f", "-"]))
    stdin = sys.stdin.read() if reads_stdin else ""

    log_path = os.environ["FAKE_LOG"]
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(json.dumps({"tool": tool, "argv": argv, "stdin": stdin}) + "\n")

    with open(os.environ["FAKE_SCENARIO"], encoding="utf-8") as fh:
        rules = json.load(fh)

    counts_path = log_path + ".counts"
    counts: dict[str, int] = {}
    if os.path.exists(counts_path):
        with open(counts_path, encoding="utf-8") as fh:
            counts = json.load(fh)

    for index, rule in enumerate(rules):
        if rule["tool"] != tool or not re.search(rule["match"], joined):
            continue
        seen = counts.get(str(index), 0)
        counts[str(index)] = seen + 1
        with open(counts_path, "w", encoding="utf-8") as fh:
            json.dump(counts, fh)
        outputs = rule["stdout"] if isinstance(rule["stdout"], list) else [rule["stdout"]]
        codes = rule["exit"] if isinstance(rule["exit"], list) else [rule["exit"]]
        sys.stdout.write(outputs[min(seen, len(outputs) - 1)])
        sys.stderr.write(rule.get("stderr", ""))
        return codes[min(seen, len(codes) - 1)]
    return 0


if __name__ == "__main__":
    sys.exit(main())
