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
    assert "paid" in low and "pass" in low  # payment condition stays; no new facts
    assert extractive_details(SHORT_SENTENCE, 80)[0] == s


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


def test_llm_over_length_is_trimmed_to_whole_sentences(monkeypatch):
    async def fake(cfg, text, max_words):
        return "Agents trade work for USDC on a testnet. " + "An oracle checks every proof before payment. " * 3

    monkeypatch.setattr(sm, "_openai", fake)
    r = asyncio.run(sm.summarize(_cfg(), LONG_SENTENCE, 12))
    assert len(r.summary.split()) <= 12 and r.summary.endswith(".") and "…" not in r.summary
