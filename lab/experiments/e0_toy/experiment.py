"""Toy experiment with known answers. It exercises the protocol's mechanics (dev runs, freeze, the held-out run once,
the ledger, the report), not a method: each scenario gives n and the expected sum 1 + … + n; arm `formula` computes it
as n(n + 1)/2, arm `loop` by adding. Both must be correct on every scenario, so a wrong answer means the mechanics
(scenario loading, decryption, results) are broken.
"""

from lab.results import Row

ARMS = {
    "formula": lambda n: n * (n + 1) // 2,
    "loop": lambda n: sum(range(1, n + 1)),
}


def run(scenarios: list[dict], context) -> list[Row]:
    rows = []
    for scenario in scenarios:
        for arm, method in ARMS.items():
            answer = method(scenario["n"])
            rows.append(Row(arm, scenario["id"], "answer", float(answer), "count"))
            rows.append(Row(arm, scenario["id"], "correct", float(answer == scenario["expected"]), "bool"))
    return rows
