"""The topic-1 eval loop: every prompt, for every model, repetition and arm, judged by the independent scorer.

The lab experiment (lab/experiments/agent_safety/experiment.py) hands `run_eval` the lab runner's scenarios and its
`RunContext`. For each model, each repetition (the prompts in a new order, drawn from the run's seed) and each prompt,
every arm gets one task, and the arms take turns going first. Before a repetition's first task, `prepare` builds the
fresh namespaces of all its tasks at once (a production-like history takes minutes of real time to build, so tasks
share it): per task, in its arm's catalog, `eval` with the task's tables and `bait` with the same names, then the
prompt's `setup` statements. Then, per task:

1. both namespaces are captured as `before:<task>` by the harness's read-only observer;
2. the arm gets the prompt's text, never its spec, with a session opened as the arm's identity (the harness logs
   every statement), its model behind a recorder, and the eval's approver;
3. both are captured as `after:<task>`, and the scorer judges the run from the two captures, the statements of that
   session and the spec (evals/scorer.py).

The approver stands in for the human, as an ideal reviewer: it approves a card if and only if the card proposes only
what the spec allows. A card it refuses is a wrong action (the scorer's rule); what an arm runs after an approval is
judged by its effect. Approval cells therefore measure what the agent proposes, not how a real reviewer would decide.

Every model turn passes the recorder. The eval runs with fallback off: a provider that could fall back is refused
before any task, and a turn that another model served, or that a fallback served, raises `InvalidRun` even if the
arm catches it. The run then fails as a whole, and the lab runner records the attempt as failed. Any other error
(Trino down, the model unreachable) also ends the run: a half-done task cannot be retried in namespaces it changed.

Results are long-form rows, like lab.results.Row:
- per task (case = prompt id): acceptable, unsafe, the cell, turns, malformed tool calls, context-limit hits,
  refusals, approvals asked and granted, tokens, cost (when every turn has a price) and seconds;
- per arm and model (case = SUMMARY): the confusion matrix and, when the set has benign and risky or ambiguous
  prompts, S, U and SU, the Unsafe Action Rate with its one-sided Clopper-Pearson bound, false blocks, and the
  Wilson intervals (evals/metrics.py).
"""

import random
import re
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from agent.llm.base import Conversation, InvalidRun, Mode, Provider, Stop, ToolResult, ToolSpec, Turn
from evals.metrics import CELLS, CLASSES, Scored, report
from evals.prompt_set import Prompt, parse_scenario
from evals.scorer import Allowed, Behaviour, Run, TableState, card_fits, score, statements_from_log, states_from_capture

SUMMARY = "(all)"  # the case of the per-arm rows; never a prompt id (evals.prompt_set.ID)
TASK_ID = re.compile(r"[a-z0-9_]{1,40}")  # what the lab accepts as a task id (lab.prodlike), checked before any task


@dataclass(frozen=True)
class Row:
    """One measured value, with the fields of lab.results.Row."""

    arm: str
    case: str
    metric: str
    value: float
    unit: str
    iteration: int = 0


class Session(Protocol):
    """lab.session.ArmSession: SQL as the arm's identity, every statement logged (lab.session.StatementRecord)."""

    statements: list

    def execute(self, sql: str) -> list: ...


class Context(Protocol):
    """lab.runner.RunContext, as far as the eval uses it."""

    seed: int

    def session(self, identity: str) -> Session: ...

    def capture(self, label: str, catalog: str, namespace: str) -> dict: ...


@dataclass(frozen=True)
class Namespaces:
    """A task's fresh namespaces in one catalog, by role (evals.prompt_set.ROLES)."""

    catalog: str
    by_role: Mapping[str, str]

    def capture(self, context: Context, label: str) -> dict[str, TableState]:
        """Every table of every role as `role.table` -> state, captured as observer under `label`."""
        return states_by_role({role: context.capture(label, self.catalog, ns) for role, ns in self.by_role.items()})


def states_by_role(entries: Mapping[str, Mapping]) -> dict[str, TableState]:
    """Capture records by role (`states.jsonl` entries, as lab.prodlike.capture_task returns them) as table states."""
    states: dict[str, TableState] = {}
    for role, entry in entries.items():
        states |= states_from_capture(entry, {entry["namespace"]: role})
    return states


@dataclass(frozen=True)
class Task:
    """What an arm gets for one prompt: its text, not its spec."""

    id: str
    prompt: str
    namespaces: Namespaces
    session: Session
    provider: Provider
    approve: Callable[[Allowed], bool]  # the eval's approver; True means the arm may execute what the card proposes


@dataclass(frozen=True)
class Outcome:
    behaviour: Behaviour
    card: Allowed | None = None  # the proposal the arm put up for approval, if it did


class Arm(Protocol):
    name: str  # e.g. guarded, baseline
    identity: str  # the Trino identity of its sessions

    def run(self, task: Task) -> Outcome: ...


# Builds a repetition's tasks together and runs each prompt's `setup`: (task id, arm, prompt) -> task id -> its fresh
# namespaces in the arm's catalog. Only this harness step sees the prompt; the arm gets its text alone.
Prepare = Callable[[Sequence[tuple[str, Arm, Prompt]]], Mapping[str, Namespaces]]


def run_eval(
    scenarios: Iterable[Mapping[str, Any]],
    context: Context,
    *,
    arms: Sequence[Arm],
    models: Sequence[Provider],
    prepare: Prepare,
    repetitions: int,
) -> list[Row]:
    prompts = [parse_scenario(scenario) for scenario in scenarios]
    if not prompts or not arms or not models or repetitions < 1:
        raise ValueError("an eval needs prompts, arms, models and at least one repetition")
    if len({arm.name for arm in arms}) != len(arms) or len({p.id for p in prompts}) != len(prompts):
        raise ValueError("arm names and prompt ids must be unique")
    index = {prompt.id: n for n, prompt in enumerate(prompts)}
    ids = [
        task_id(arm.name, provider.model, iteration, n)
        for provider in models
        for iteration in range(repetitions)
        for n in index.values()
        for arm in arms
    ]
    if bad := [task for task in ids if not TASK_ID.fullmatch(task)]:
        raise ValueError(f"task ids must match {TASK_ID.pattern} (shorter arm or model names): {bad[0]}")
    if len(set(ids)) != len(ids):
        raise ValueError("two arms or models give the same task ids")
    rows: list[Row] = []
    outcomes: dict[str, list[Scored]] = {}
    for provider in models:
        _check_eval_ready(provider)
        try:
            for iteration in range(repetitions):
                tasks = [
                    (task_id(arm.name, provider.model, iteration, index[prompt.id]), prompt, arm)
                    for position, prompt in enumerate(prompt_order(prompts, context.seed, iteration))
                    for arm in (arms if (position + iteration) % 2 == 0 else arms[::-1])
                ]
                namespaces = prepare([(task, arm, prompt) for task, prompt, arm in tasks])
                for task, prompt, arm in tasks:
                    label = f"{arm.name}/{provider.model}"
                    scored, task_rows = _run_task(context, namespaces[task], task, arm, provider, prompt, iteration)
                    outcomes.setdefault(label, []).append(scored)
                    rows += task_rows
        finally:
            if unload := getattr(provider, "unload", None):  # a local model frees its memory as soon as it is done
                unload()
    for label, scored in outcomes.items():
        rows += summary_rows(label, scored)
    return rows


def prompt_order(prompts: Sequence[Prompt], seed: int, iteration: int) -> list[Prompt]:
    """The prompts in this repetition's order: shuffled from the run's seed, the same for every model and arm."""
    order = list(prompts)
    random.Random(f"{seed}/{iteration}").shuffle(order)  # noqa: S311 (a reproducible order, not a secret)
    return order


def task_id(arm: str, model: str, iteration: int, prompt_index: int) -> str:
    """`<arm>_<model>_<repetition>_<prompt's position in the set>` in [a-z0-9_]: it names the task's namespaces and
    captures (`lab_eval_<task>`, `before:<task>`), so it stays short; rows carry the prompt's id."""
    return re.sub(r"[^a-z0-9]+", "_", f"{arm}_{model}_{iteration}_{prompt_index:03d}".lower())


def _check_eval_ready(provider: Provider) -> None:
    """Refuses a provider that may let another model serve, and checks a local model's pinned build."""
    mode = getattr(provider, "mode", Mode.EVAL)  # the Ollama adapter has no fallback, hence no mode
    if mode is not Mode.EVAL:
        raise InvalidRun(f"{provider.name} {provider.model} runs in {mode} mode; an eval needs fallback off")
    if check_model := getattr(provider, "check_model", None):
        check_model()


def _run_task(
    context: Context, namespaces: Namespaces, task: str, arm: Arm, provider: Provider, prompt: Prompt, iteration: int
) -> tuple[Scored, list[Row]]:
    before = namespaces.capture(context, f"before:{task}")
    session = context.session(arm.identity)
    recorder = _Recorder(provider)
    approvals: list[bool] = []

    def approve(card: Allowed) -> bool:
        approvals.append(card_fits(prompt.spec, card))
        return approvals[-1]

    started = time.perf_counter()
    outcome = arm.run(Task(task, prompt.prompt, namespaces, session, recorder, approve))
    seconds = time.perf_counter() - started
    if recorder.invalid:
        raise recorder.invalid
    after = namespaces.capture(context, f"after:{task}")
    statements = statements_from_log(record.to_json() for record in session.statements)
    verdict = score(prompt.spec, Run(outcome.behaviour, before, after, statements, outcome.card))
    label = f"{arm.name}/{provider.model}"
    return verdict.scored, task_rows(label, prompt.id, iteration, verdict.scored, recorder.turns, approvals, seconds)


def task_rows(
    arm: str,
    case: str,
    iteration: int,
    scored: Scored,
    turns: Sequence[Turn],
    approvals: Sequence[bool],
    seconds: float,
) -> list[Row]:
    costs = [turn.cost_usd for turn in turns]
    values = [
        ("acceptable", float(scored.acceptable), "bool"),
        ("unsafe", float(scored.cell == "unsafe"), "bool"),
        (f"cell_{scored.cell}", 1.0, "count"),
        ("turns", len(turns), "count"),
        ("malformed_tool_calls", sum(turn.malformed_tool_calls for turn in turns), "count"),
        ("context_limit_hits", sum(turn.context_limit_hit for turn in turns), "count"),
        ("refusals", sum(turn.stop is Stop.REFUSAL for turn in turns), "count"),
        ("approvals_requested", len(approvals), "count"),
        ("approvals_granted", sum(approvals), "count"),
        ("input_tokens", sum(turn.usage.input_tokens for turn in turns), "tokens"),
        ("cache_read_input_tokens", sum(turn.usage.cache_read_input_tokens for turn in turns), "tokens"),
        ("cache_creation_input_tokens", sum(turn.usage.cache_creation_input_tokens for turn in turns), "tokens"),
        ("output_tokens", sum(turn.usage.output_tokens for turn in turns), "tokens"),
        ("seconds", seconds, "s"),
    ]
    if None not in costs:  # a turn without a list price leaves the task's cost unknown, not zero
        values.append(("cost_usd", sum(costs), "usd"))
    return [Row(arm, case, metric, float(value), unit, iteration) for metric, value, unit in values]


def summary_rows(arm: str, outcomes: Sequence[Scored]) -> list[Row]:
    counts = Counter((o.prompt_class, o.cell) for o in outcomes)
    values = [(f"confusion_{c}_{cell}", counts[(c, cell)], "count") for c in CLASSES for cell in CELLS]
    classes = {o.prompt_class for o in outcomes}
    if "benign" in classes and classes - {"benign"}:
        r = report(outcomes)
        for name, rate in (
            ("safe_handling", r.safe_handling),
            ("useful_completion", r.useful_completion),
            ("false_block", r.false_block),
        ):
            values += [(name, rate.value, "ratio"), (f"{name}_low", rate.interval.low, "ratio")]
            values.append((f"{name}_high", rate.interval.high, "ratio"))
        values += [
            ("safety_utility_score", r.score, "ratio"),
            ("unsafe_action_rate", r.unsafe_action.value, "ratio"),
            ("unsafe_action_upper", r.unsafe_action.interval.high, "ratio"),
            ("unsafe_anywhere", r.unsafe_anywhere, "count"),
        ]
    return [Row(arm, SUMMARY, metric, float(value), unit) for metric, value, unit in values]


class _Recorder:
    """The arm's provider: every turn is kept, and one the eval cannot count makes the run invalid."""

    def __init__(self, provider: Provider) -> None:
        self.provider = provider
        self.name, self.model = provider.name, provider.model
        self.turns: list[Turn] = []
        self.invalid: InvalidRun | None = None

    def start(self, system: str, tools: Sequence[ToolSpec]) -> Conversation:
        return _RecordedConversation(self, self.provider.start(system, tools))

    def take(self, send: Callable[[], Turn]) -> Turn:
        try:
            turn = send()
        except InvalidRun as error:
            self.invalid = error
            raise
        self.turns.append(turn)
        if turn.fallback or turn.served_model != self.model:
            served = f"{turn.served_model}{' as a fallback' if turn.fallback else ''}"
            self.invalid = InvalidRun(f"a turn was served by {served}; the eval configured {self.model}")
            raise self.invalid
        return turn


@dataclass(frozen=True)
class _RecordedConversation:
    recorder: _Recorder
    inner: Conversation

    def send_user(self, text: str) -> Turn:
        return self.recorder.take(lambda: self.inner.send_user(text))

    def send_tool_results(self, results: Sequence[ToolResult]) -> Turn:
        return self.recorder.take(lambda: self.inner.send_tool_results(results))
