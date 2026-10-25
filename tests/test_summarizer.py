"""Screen-shaped checks: not empty, no ellipsis/cut-off, within max_words, not the opening of
the input verbatim, every word taken from the input (nothing added)."""
from __future__ import annotations

import asyncio
import re

import pytest

from groshik_agent import summarizer as sm
from groshik_agent.config import Config
from groshik_agent.summarizer import extractive_details, is_verbatim_prefix

LONG_SENTENCE = (
    "THE LINE Protocol lets autonomous AI agents discover each other, negotiate a price for a task, "
    "hand the work to a provider agent that has the right capability, and settle the payment in USDC "
    "on the Tempo testnet, while an oracle checks every result for a valid proof before any money "
    "moves, so buyers never pay for empty or copied answers."
)
SHORT_SENTENCE = "Agents get paid only when their result passes the checks."
NO_PUNCT = (
    "the protocol routes every intent to the provider agent with the best reputation score the lowest "
    "price and the fastest response time and then releases the payment once the oracle has checked "
    "the proof attached to the result"
)
NO_BOUNDARY = " ".join(f"token{i}" for i in range(60))
TOPICS = ["agents", "providers", "buyers", "the gateway", "the oracle", "reputation", "settlement",
          "the ledger", "proofs", "intents", "capabilities", "budgets"]
VERY_LONG = " ".join(
    f"In round {i} {TOPICS[i % 12]} interacted with {TOPICS[(i * 5 + 3) % 12]} under rule {i % 7}, "
    f"and the result was recorded on the network."
    for i in range(300)
)
RU_LONG = (
    "Протокол THE LINE позволяет агентам находить друг друга, договариваться о цене задачи и "
    "рассчитываться в USDC в тестовой сети Tempo, а оракул проверяет каждый результат перед выплатой, "
    "чтобы покупатели не платили за пустые или скопированные ответы."
)


def words_from_input(summary: str, text: str) -> bool:
    src = {w.lower() for w in re.findall(r"[\w'-]+", text)}
    return all(w.lower() in src for w in re.findall(r"[\w'-]+", summary))


def screen_safe(summary: str, text: str, max_words: int, allow_whole: bool = False) -> None:
    assert summary.strip()
    assert "…" not in summary and "..." not in summary
    assert summary.rstrip()[-1] in ".!?"
    assert len(summary.split()) <= max_words
    assert words_from_input(summary, text)
    if not allow_whole:
        assert not is_verbatim_prefix(summary, text), summary


@pytest.mark.parametrize("text,max_words", [(LONG_SENTENCE, 20), (LONG_SENTENCE, 12), (RU_LONG, 15)])
def test_one_long_sentence_is_compressed_to_whole_clauses(text, max_words):
    s, method = extractive_details(text, max_words)
    screen_safe(s, text, max_words)
    assert method in ("clauses", "window")
    assert extractive_details(text, max_words)[0] == s  # deterministic


def test_one_short_sentence_is_not_returned_unchanged():
    """A sentence that fits max_words must not be echoed: 100% overlap holds the payout."""
    s, method = extractive_details(SHORT_SENTENCE, 80)
    assert method in ("clauses", "window")
    assert sm._norm(s) != sm._norm(SHORT_SENTENCE)
    # Dropping only the first word is still the input with the subject cut off.
    first_dropped = " ".join(SHORT_SENTENCE.rstrip(".").split()[1:])
    assert sm._norm(s) != sm._norm(first_dropped)
    # At least a third of the words (and two) must go, so this is not a one-word trim.
    assert len(s.split()) <= len(SHORT_SENTENCE.split()) - max(2, len(SHORT_SENTENCE.split()) // 3)
    screen_safe(s, SHORT_SENTENCE, 80)
    low = s.lower()
    assert low.startswith("agents get paid")  # main clause, not a bare verb or the subordinate
    assert "paid" in low and "pass" in low
    assert not low.startswith("their result")
    assert extractive_details(SHORT_SENTENCE, 80)[0] == s


COUNCIL = "The council approved the new budget on Tuesday after a long debate about city services."
GRAMMAR_CASES = [
    COUNCIL,
    "The committee postponed the vote because of a sudden storm over the harbor.",
    "Researchers published the study in the journal after months of review by outside experts.",
    "The players left the field during the heavy rain before the final whistle.",
]


def _clause_edges_ok(summary: str) -> None:
    words = summary.rstrip(".!?").split()
    assert sm._starts_ok(words), summary
    assert sm._ends_ok(words), summary
    assert words[0].lower().strip(",;:") not in sm._VERBISH


@pytest.mark.parametrize("text", GRAMMAR_CASES)
def test_short_compression_keeps_the_main_clause(text):
    """Drop trailing material. Never a dangling ending, a bare verb, or only the subordinate."""
    s, _method = extractive_details(text, 80)
    assert sm._norm(s) != sm._norm(text)
    _clause_edges_ok(s)
    low = s.lower()
    assert "after a long." not in low and not low.rstrip(".").endswith(" long")
    assert "sudden." not in low
    # The matrix subject and verb survive; a subordinate-only cut does not.
    head = text.split()[0].lower().strip(".,")
    if head == "the" and len(text.split()) > 1:
        head = text.split()[1].lower()
    assert head in low
    assert words_from_input(s, text)


def test_bee_dance_keeps_the_main_facts():
    """Dense short text: location signal, distance encoding, and the flight all survive."""
    from the_line_sdk.cli import SMOKE_TEXT
    s, _method = extractive_details(SMOKE_TEXT, 80)
    screen_safe(s, SMOKE_TEXT, 80)
    low = s.lower()
    assert "danc" in low and "food" in low
    assert "waggl" in low and "farther" in low
    assert "fly" in low and "source" in low


BOARD = (
    "The board approved the acquisition of Northwind Robotics and authorized management to close "
    "the deal this summer, subject to shareholder and regulatory approval. "
    "Shares rose the next day."
)


def test_overlong_main_sentence_keeps_the_board_decision():
    """The highest-scoring sentence is compressed, not replaced by a shorter aside."""
    for text in (BOARD, "Shares rose the next day. " + BOARD.split(". ", 1)[0] + "."):
        s, method = extractive_details(text, 12)
        screen_safe(s, text, 12)
        assert method in ("clauses", "window")
        low = s.lower()
        assert "board" in low
        assert "acquisition" in low or "northwind" in low
        assert "shares rose the next day" not in low


def test_llm_rephrases_a_short_sentence(monkeypatch):
    async def fake(cfg, text, max_words):
        return "Payment follows only after the result passes the checks."

    monkeypatch.setattr(sm, "_openai", fake)
    cfg = _cfg()
    r = asyncio.run(sm.summarize(cfg, SHORT_SENTENCE, 80))
    assert r.model_id == cfg.openai_model
    assert sm._norm(r.summary) != sm._norm(SHORT_SENTENCE)
    assert "…" not in r.summary and r.summary.endswith(".")


def test_long_sentence_without_punctuation():
    s, _ = extractive_details(NO_PUNCT, 15)
    screen_safe(s, NO_PUNCT, 15)


def test_no_boundaries_at_all_still_clean():
    s, _ = extractive_details(NO_BOUNDARY, 20)
    screen_safe(s, NO_BOUNDARY, 20)


@pytest.mark.parametrize("max_words", [10, 80, 400])
def test_very_long_multi_sentence(max_words):
    s, _ = extractive_details(VERY_LONG, max_words)
    screen_safe(s, VERY_LONG, max_words)
    assert len(s.split()) < len(VERY_LONG.split()) * 0.5


def test_multi_sentence_lead_is_not_verbatim_opening():
    from the_line_sdk.cli import SMOKE_TEXT  # the protocol's own smoke-test text
    s, method = extractive_details(SMOKE_TEXT, 80)
    assert method == "sentences"
    screen_safe(s, SMOKE_TEXT, 80)


def _cfg(**kw) -> Config:
    c = Config()
    c.openai_api_key, c.anthropic_api_key, c.llm_provider, c.llm_mode = "sk-test", "", "auto", "hard"
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_llm_gets_single_sentence_but_not_easy_multi_sentence(monkeypatch):
    calls = []

    async def fake(cfg, text, max_words):
        calls.append(text)
        return "Agents find each other and settle payments once an oracle has checked the proof."

    monkeypatch.setattr(sm, "_openai", fake)
    cfg = _cfg()
    r = asyncio.run(sm.summarize(cfg, LONG_SENTENCE, 20))
    assert r.model_id == cfg.openai_model and calls == [LONG_SENTENCE]
    from the_line_sdk.cli import SMOKE_TEXT
    r2 = asyncio.run(sm.summarize(cfg, SMOKE_TEXT, 80))
    assert r2.model_id == sm.EXTRACTIVE_MODEL_ID and len(calls) == 1
    cfg.llm_mode = "always"
    asyncio.run(sm.summarize(cfg, SMOKE_TEXT, 80))
    assert len(calls) == 2


@pytest.mark.parametrize("bad", [
    LONG_SENTENCE[:120] + "…",                       # verbatim opening, cut off
    LONG_SENTENCE,                                    # input handed back
    "In 2024 agents moved 5 million USDC on Tempo.",  # figures the input lacks
    "",
])
def test_bad_llm_answers_fall_back_to_extractive(monkeypatch, bad):
    async def fake(cfg, text, max_words):
        return bad

    monkeypatch.setattr(sm, "_openai", fake)
    r = asyncio.run(sm.summarize(_cfg(), LONG_SENTENCE, 60))
    assert r.model_id == sm.EXTRACTIVE_MODEL_ID
    screen_safe(r.summary, LONG_SENTENCE, 60)


def _http_response(status: int, body: str):
    import httpx
    return httpx.Response(
        status,
        content=body.encode(),
        request=httpx.Request("POST", "https://example.invalid/llm"),
    )


def test_llm_over_length_is_trimmed_to_whole_sentences(monkeypatch):
    async def fake(cfg, text, max_words):
        return "Agents trade work for USDC on a testnet. " + "An oracle checks every proof before payment. " * 3

    monkeypatch.setattr(sm, "_openai", fake)
    r = asyncio.run(sm.summarize(_cfg(), LONG_SENTENCE, 12))
    assert len(r.summary.split()) <= 12 and r.summary.endswith(".") and "…" not in r.summary


class _ScriptedClient:
    """AsyncClient stand-in. `script` is a list of httpx.Response consumed in order."""

    def __init__(self, script, calls, *args, **kwargs):
        self._script = script
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        self._calls.append({"url": url, "json": dict(json or {}), "headers": dict(headers or {})})
        if not self._script:
            raise AssertionError("unexpected extra HTTP call")
        return self._script.pop(0)


def _scripted(monkeypatch, script, calls):
    import httpx

    def factory(*args, **kwargs):
        return _ScriptedClient(script, calls, *args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    monkeypatch.setattr(sm.httpx, "AsyncClient", factory)


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_temperature_400_retries_once_and_is_remembered(monkeypatch, provider):
    sm._omit_temperature.clear()
    if provider == "anthropic":
        cfg = _cfg(llm_provider="anthropic", anthropic_api_key="sk-ant-test", openai_api_key="",
                   anthropic_model="claude-haiku-5-5")
        rejected = '{"type":"error","error":{"message":"temperature is deprecated for this model"}}'
        ok = '{"content":[{"type":"text","text":"Agents are paid after the result passes checks."}]}'
    else:
        cfg = _cfg(llm_provider="openai", openai_model="gpt-5-mini")
        rejected = ('{"error":{"message":"Unsupported value: \'temperature\' does not support 0.2",'
                    '"code":"unsupported_parameter","param":"temperature"}}')
        ok = '{"choices":[{"message":{"content":"Agents are paid after the result passes checks."}}]}'
    calls: list[dict] = []
    script = [_http_response(400, rejected), _http_response(200, ok), _http_response(200, ok)]
    _scripted(monkeypatch, script, calls)
    first = asyncio.run(sm.summarize(cfg, SHORT_SENTENCE, 80))
    assert first.model_id == (cfg.anthropic_model if provider == "anthropic" else cfg.openai_model)
    assert "paid" in first.summary.lower()
    assert "temperature" in calls[0]["json"]
    assert "temperature" not in calls[1]["json"]
    second = asyncio.run(sm.summarize(cfg, SHORT_SENTENCE, 80))
    assert second.summary == first.summary
    assert len(calls) == 3  # the remembered model skips the failing call
    assert "temperature" not in calls[2]["json"]
    sm._omit_temperature.clear()


def test_llm_http_failure_logs_status_and_falls_back(monkeypatch, caplog):
    sm._omit_temperature.clear()
    cfg = _cfg(llm_provider="openai", openai_model="gpt-4o-mini")
    calls: list[dict] = []
    script = [_http_response(
        400,
        '{"error":{"message":"model not found","code":"model_not_found"}} key=sk-live-SECRETVALUE',
    )]
    _scripted(monkeypatch, script, calls)
    caplog.set_level("WARNING", logger="groshik.summarizer")
    result = asyncio.run(sm.summarize(cfg, SHORT_SENTENCE, 80))
    assert len(calls) == 1  # not a temperature error, so no retry
    assert "temperature" in calls[0]["json"]
    assert result.model_id == sm.EXTRACTIVE_MODEL_ID
    rec = next(r for r in caplog.records if "falling back to extractive" in r.getMessage())
    assert rec.status_code == 400
    assert "model not found" in rec.error
    assert "sk-live-SECRETVALUE" not in rec.getMessage()
    assert "sk-live-SECRETVALUE" not in rec.error
    sm._omit_temperature.clear()
