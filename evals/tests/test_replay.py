"""evals/replay.py: a recorded LLM conversation replays to the same turns with no backend, a changed request is a
miss, and nothing secret reaches the cassette. Recording runs against the agent's fake backends (agent/tests)."""

import json
import subprocess
from pathlib import Path

import anthropic
import fake_anthropic as fa
import fake_ollama as fo
import httpx2
import pytest

from agent.llm.anthropic_provider import AnthropicProvider
from agent.llm.base import Mode, ProviderRequestError, ToolResult, ToolSpec
from agent.llm.ollama_provider import OllamaProvider
from evals.replay import Cassette, redact

ROW_COUNT = ToolSpec(
    "row_count",
    "Number of rows in a table of the read-only catalog",
    {
        "type": "object",
        "properties": {"table": {"type": "string"}},
        "required": ["table"],
        "additionalProperties": False,
    },
)
ARGS = {"table": "lab_ro.shop.orders"}
REPO_ROOT = Path(__file__).resolve().parents[2]


def claude(transport: httpx2.BaseTransport) -> AnthropicProvider:
    http = anthropic.DefaultHttpxClient(transport=transport)
    return AnthropicProvider(mode=Mode.EVAL, client=anthropic.Anthropic(api_key="sk-ant-test-0000", http_client=http))


def ollama(transport: httpx2.BaseTransport) -> OllamaProvider:
    return OllamaProvider(http=httpx2.Client(base_url="http://ollama.test", transport=transport))


def converse(provider, question: str = "How many rows does lab_ro.shop.orders have?"):
    conversation = provider.start("You operate a data lakehouse.", [ROW_COUNT])
    first = conversation.send_user(question)
    final = conversation.send_tool_results([ToolResult(first.tool_calls[0], "1234")])
    return [(t.stop, t.text, t.tool_calls, t.usage, t.served_model) for t in (first, final)]


def claude_backend() -> fa.FakeMessagesAPI:
    return fa.FakeMessagesAPI(
        fa.Reply([fa.thinking(), fa.tool_use("toolu_1", "row_count", ARGS)], stop_reason="tool_use"),
        fa.Reply([fa.thinking(), fa.text("1234 rows.")]),
    )


def ollama_backend() -> fo.FakeOllama:
    return fo.FakeOllama(fo.Reply(tool_calls=[fo.call("row_count", ARGS)]), fo.Reply(content="1234 rows."))


BACKENDS = {"anthropic": (claude, claude_backend), "ollama": (ollama, ollama_backend)}


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_recorded_conversation_replays_without_the_backend(backend, tmp_path):
    provider_for, backend_for = BACKENDS[backend]
    path = tmp_path / "dev.json"
    recorded = converse(provider_for(Cassette(path, "record", backend=backend_for().transport)))

    assert converse(provider_for(Cassette(path, "replay"))) == recorded


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_changed_request_is_a_miss(backend, tmp_path):
    provider_for, backend_for = BACKENDS[backend]
    path = tmp_path / "dev.json"
    converse(provider_for(Cassette(path, "record", backend=backend_for().transport)))

    replay = Cassette(path, "replay")
    with pytest.raises(ProviderRequestError, match="cassette_miss"):
        converse(provider_for(replay), "How many rows does lab_ro.shop.payments have?")
    assert len(replay.misses) == 1  # not retried


def test_repetitions_of_one_request_replay_in_recorded_order(tmp_path):
    path = tmp_path / "dev.json"
    backend = fo.FakeOllama(fo.Reply(content="first"), fo.Reply(content="second"))
    recording = ollama(Cassette(path, "record", backend=backend.transport))
    answers = [recording.start("system", []).send_user("Say something.").text for _ in range(2)]

    cassette = Cassette(path, "replay")
    replay = ollama(cassette)
    assert [replay.start("system", []).send_user("Say something.").text for _ in range(2)] == answers
    with pytest.raises(ProviderRequestError, match="cassette_miss"):
        replay.start("system", []).send_user("Say something.")
    assert [m.split(":")[0] for m in cassette.misses] == ["POST /api/chat"]


def test_scrubbed_values_do_not_change_the_key(tmp_path):
    path = tmp_path / "dev.json"
    scrub = [(r"lab_evalstg_[0-9a-f]{6}", "lab_evalstg_RUN")]
    backend = fo.FakeOllama(fo.Reply(content="ok"))
    ollama(Cassette(path, "record", backend=backend.transport, scrub=scrub)).start("s", []).send_user(
        "Describe lab_evalstg_1a2b3c.orders"
    )

    replay = ollama(Cassette(path, "replay", scrub=scrub))
    assert replay.start("s", []).send_user("Describe lab_evalstg_9f8e7d.orders").text == "ok"


def test_secrets_never_reach_the_cassette(tmp_path):
    path = tmp_path / "dev.json"
    # Built at run time: as one literal, the token would trip the repository's own gitleaks rule.
    webhook = "https://discord.com/api/webhooks/" + "1290453218765432109/" + "Zk3-Qe9_vT2mLp8X" * 5
    leaky = (
        f"Alerts go to {webhook}. Trino at https://agent_planner:s3cret-pw@trino.lab:8443/v1/statement "
        "with X-Trino-User: agent_planner and Authorization: Basic YWdlbnRfcGxhbm5lcjpzM2NyZXQtcHc="
    )
    backend = fo.FakeOllama(fo.Reply(content=f"Posting to {webhook}"))
    ollama(Cassette(path, "record", backend=backend.transport)).start("s", []).send_user(leaky)
    claude_recording = Cassette(tmp_path / "claude.json", "record", backend=claude_backend().transport)
    converse(claude(claude_recording))

    stored = path.read_text() + (tmp_path / "claude.json").read_text()
    for secret in ("Zk3-Qe9_vT2mLp8", "s3cret-pw", "YWdlbnRfcGxhbm5lcjpzM2NyZXQtcHc=", "sk-ant-test-0000", "x-api-key"):
        assert secret not in stored, secret
    assert "X-Trino-User: <redacted>" in stored
    assert ollama(Cassette(path, "replay")).start("s", []).send_user(leaky).text == "Posting to <discord-webhook>"


def test_redact_leaves_ordinary_text_alone():
    text = "SELECT count(*) FROM lab_ro.shop.orders -- see https://trino.io/docs/current/"
    assert redact(text) == text


def test_held_out_cassettes_are_ignored_by_git():
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "evals/cassettes/holdout/run-1.json"],  # noqa: S607 - git from PATH, as in CI
        cwd=REPO_ROOT,
        check=False,
    )
    assert ignored.returncode == 0


def test_a_cassette_file_is_stable_json(tmp_path):
    path = tmp_path / "dev.json"
    converse(ollama(Cassette(path, "record", backend=ollama_backend().transport)))
    data = json.loads(path.read_text())
    assert data["version"] == 1
    assert all(len(key) == 64 for key in data["interactions"])
    assert path.read_text() == json.dumps(data, indent=1, sort_keys=True)
