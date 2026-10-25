"""text_summary: deterministic extractive summarizer + optional LLM backends.

The extractive path only ever reuses sentences (or a prefix of one) from the input, so it
cannot add a figure, date or name the input lacks — the pre-payout screen's main concern —
and it returns the same summary for the same input every time.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import Optional

import httpx

from .config import Config

log = logging.getLogger("groshik.summarizer")

# Kept equal to the model_id in the registered model card; the algorithm revision is
# visible in prompt_template_hash (the template text below changed in 0.1.1).
EXTRACTIVE_MODEL_ID = "groshik-extractive-v1"

# The template is part of the proof (sha256), so any change to it shows up as a new hash.
EXTRACTIVE_TEMPLATE = (
    "groshik-extractive-v2: split into sentences; score each by the frequency of its content "
    "words; keep the top 1-3 whole sentences in original order within max_words; a sentence "
    "longer than max_words is reduced to its most important complete clauses; never an "
    "ellipsis, never only the opening words of the input."
)
LLM_TEMPLATE = (
    "You summarize text for THE LINE Protocol. Write 1-3 concise sentences, at most "
    "{max_words} words, preserving the key information. Use only facts present in the "
    "text: add no numbers, dates, names or claims that the text does not contain. "
    "Rephrase in your own words rather than copying the opening of the text, and end "
    "with a complete sentence. Reply with the summary only."
)

_STOP = set(
    """a an the and or but if then than so of to in on at by for with from as is are was were be
    been being it its this that these those there here they them their he she his her we our you
    your i me my not no do does did done have has had having will would can could should may might
    must shall into over under about after before while which who whom whose what when where why
    how all any both each few more most other some such only own same too very just also""".split()
)
_SENT = re.compile(r"(?<=[.!?…])\s+(?=[\"'«(\[]?[A-ZА-ЯЁ0-9])")
_WORD = re.compile(r"[\wÀ-ÿА-Яа-яЁё'-]+", re.UNICODE)


@dataclass
class Summary:
    summary: str
    model_id: str
    prompt_template: str


class InputError(ValueError):
    pass


def parse_input(task_input: dict) -> tuple[str, int]:
    data = task_input
    if "text" not in data and isinstance(data.get("input_data"), dict):
        data = data["input_data"]
    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        raise InputError("input.text must be a non-empty string")
    max_words = data.get("max_words", 80)
    try:
        max_words = int(max_words)
    except (TypeError, ValueError):
        raise InputError("input.max_words must be an integer") from None
    max_words = max(10, min(1000, max_words))
    return text.strip(), max_words


def _words(s: str) -> list[str]:
    return s.split()


def _norm(s: str) -> str:
    return re.sub(r"[^\w]+", " ", s.lower()).strip()


def _split_sentences(text: str) -> list[str]:
    flat = re.sub(r"\s+", " ", text).strip()
    return [s.strip() for s in _SENT.split(flat) if s.strip()]


def is_verbatim_prefix(summary: str, text: str) -> bool:
    """True if the summary is just the input's opening words (punctuation ignored)."""
    a, b = _norm(summary), _norm(text)
    return bool(a) and b.startswith(a)


def _finish(s: str) -> str:
    """Capitalize, drop dangling punctuation/connectors, end with exactly one full stop."""
    s = re.sub(r"\s+", " ", s).strip().strip(",;:—–- ")
    w = s.split()
    while w and w[-1].lower().strip(",;:") in _STOP | _CONJ:
        w.pop()
    while w and w[0].lower().strip(",;:") in _CONJ:
        w.pop(0)
    s = " ".join(w).rstrip(",;:—–- …")
    if not s:
        return ""
    s = s[0].upper() + s[1:]
    return s if s[-1] in ".!?" else s + "."


_CONJ = set(
    """and but or so yet while whereas because although though which who whom where when after
    before since unless until then with without и а но или что который которая которое которые
    потому хотя когда где если чтобы""".split()
)
_CLAUSE_PUNCT = re.compile(r"\s*[,;:()\[\]—–]\s*|\s+-\s+")
_CLAUSE_CONJ = re.compile(r"\s+(?=(?:" + "|".join(sorted(_CONJ, key=len, reverse=True)) + r")\b)", re.I)


def _clauses(sentence: str) -> list[str]:
    parts: list[str] = []
    for chunk in _CLAUSE_PUNCT.split(sentence.rstrip(".!?…")):
        if not chunk.strip():
            continue
        sub = _CLAUSE_CONJ.split(chunk)
        merged: list[str] = []
        for piece in sub:  # don't create fragments shorter than 3 words
            if merged and (len(piece.split()) < 3 or len(merged[-1].split()) < 3):
                merged[-1] += " " + piece
            else:
                merged.append(piece)
        parts.extend(m.strip() for m in merged if m.strip())
    return parts


def _scorer(text: str):
    freq: dict[str, int] = {}
    for tok in _WORD.findall(text.lower()):
        if tok not in _STOP and len(tok) > 2:
            freq[tok] = freq.get(tok, 0) + 1

    def score(s: str) -> float:
        toks = [t for t in _WORD.findall(s.lower()) if t in freq]
        return sum(freq[t] for t in toks) / math.sqrt(len(toks)) if toks else 0.0

    return score


def _window(sentence: str, limit: int, score) -> str:
    """No usable boundary at all: best-scoring run of words that does not start at word 0."""
    w = sentence.rstrip(".!?…").split()
    size = max(5, min(limit, len(w) - 1))
    best, best_i = -1.0, 1
    for i in range(1, len(w) - size + 1):
        sc = score(" ".join(w[i:i + size]))
        if sc > best:
            best, best_i = sc, i
    return _finish(" ".join(w[best_i:best_i + size]))


def _compress_sentence(sentence: str, limit: int, score, text: str) -> tuple[str, str]:
    """Keep the most important complete clauses (in order) within `limit` words.
    Never a mid-clause cut, never an ellipsis, never just the opening words of the input."""
    cl = _clauses(sentence)
    n = [len(c.split()) for c in cl]
    if len(cl) < 2 or min(n) > limit:
        return _window(sentence, limit, score), "window"
    order = sorted(range(len(cl)), key=lambda i: (-score(cl[i]), i))
    chosen: list[int] = []
    used = 0
    for i in order:
        if used + n[i] <= limit:
            chosen.append(i)
            used += n[i]
    chosen.sort()
    if chosen == list(range(len(chosen))):  # contiguous from the start -> verbatim prefix
        later = [i for i in order if i > chosen[-1] + 1]
        for j in later:
            for drop in reversed(chosen):
                trial = [c for c in chosen if c != drop] + [j]
                if trial and sum(n[c] for c in trial) <= limit:
                    chosen = sorted(trial)
                    break
            else:
                continue
            break
    out = _finish(", ".join(cl[i] for i in chosen))
    if not out or is_verbatim_prefix(out, text):
        return _window(sentence, limit, score), "window"
    return out, "clauses"


def extractive_details(text: str, max_words: int) -> tuple[str, str]:
    """Return (summary, method). method: sentences | whole_sentence | clauses | window."""
    sentences = _split_sentences(text)
    if not sentences:
        return "", "empty"
    score = _scorer(text)
    if len(sentences) == 1:
        s = sentences[0]
        if len(s.split()) <= max_words:
            return _finish(s) if s[-1] not in ".!?" else s, "whole_sentence"
        return _compress_sentence(s, max_words, score, text)

    total = len(text.split())
    n = [len(s.split()) for s in sentences]
    lead = lambda i: score(sentences[i]) * (1.15 if i == 0 else 1.0)  # noqa: E731
    ranked = sorted(range(len(sentences)), key=lambda i: (-lead(i), i))
    seen: set[str] = set()
    uniq = []
    for i in ranked:  # repeated sentences count once
        k = _norm(sentences[i])
        if k not in seen:
            seen.add(k)
            uniq.append(i)
    ranked = uniq
    budget = min(max_words, max(8, math.ceil(total * 0.6)))
    chosen: list[int] = []
    used = 0
    for i in ranked:
        if len(chosen) < 3 and used + n[i] <= budget:
            chosen.append(i)
            used += n[i]
    if not chosen:
        top = sentences[ranked[0]]
        if n[ranked[0]] <= max_words and ranked[0] != 0:
            return top, "sentences"
        return _compress_sentence(top, max_words, score, text)
    chosen.sort()
    if chosen == list(range(len(chosen))):  # would be the opening of the input verbatim
        cap = min(max_words, math.floor(total * 0.85))
        later = [i for i in ranked if i not in chosen and i > chosen[-1] + 1]
        fixed = False
        for j in later:  # add a non-adjacent sentence if it still fits
            if used + n[j] <= cap:
                chosen.append(j)
                fixed = True
                break
        if not fixed and len(chosen) >= 2:
            for j in later:  # or swap the last lead sentence for a later one
                if used - n[chosen[-1]] + n[j] <= max_words:
                    chosen[-1] = j
                    fixed = True
                    break
        if not fixed:  # single lead sentence: compress it so it is not a verbatim copy
            out, how = _compress_sentence(sentences[0], min(max_words, max(5, n[0] - 1)), score, text)
            if out and not is_verbatim_prefix(out, text):
                return out, how
        chosen.sort()
    return " ".join(sentences[i] for i in chosen), "sentences"


def extractive_summary(text: str, max_words: int) -> str:
    return extractive_details(text, max_words)[0]


_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def _llm_ok(out: str, text: str, max_words: int) -> tuple[str, str]:
    """Make an LLM answer screen-safe or reject it ('' + reason)."""
    out = re.sub(r"\s+", " ", out).strip().strip('"«»')
    if not out:
        return "", "empty"
    if len(out.split()) > max_words:  # keep whole sentences only
        kept, used = [], 0
        for s in _split_sentences(out):
            if used + len(s.split()) > max_words:
                break
            kept.append(s)
            used += len(s.split())
        if not kept:
            return "", "too long"
        out = " ".join(kept)
    out = out.rstrip("…").rstrip()
    if out and out[-1] not in ".!?":
        out += "."
    if not set(_NUM.findall(out)) <= set(_NUM.findall(text)):
        return "", "added a number not in the input"
    if _norm(out) == _norm(text) or is_verbatim_prefix(out, text):
        return "", "verbatim copy/prefix of input"
    return out, ""


async def _openai(cfg: Config, text: str, max_words: int) -> str:
    async with httpx.AsyncClient(timeout=cfg.llm_timeout_s) as http:
        r = await http.post(
            f"{cfg.openai_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {cfg.openai_api_key}"},
            json={
                "model": cfg.openai_model,
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": LLM_TEMPLATE.format(max_words=max_words)},
                    {"role": "user", "content": text},
                ],
            },
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()


async def _anthropic(cfg: Config, text: str, max_words: int) -> str:
    async with httpx.AsyncClient(timeout=cfg.llm_timeout_s) as http:
        r = await http.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": cfg.anthropic_api_key, "anthropic-version": "2023-06-01"},
            json={
                "model": cfg.anthropic_model,
                "max_tokens": 400,
                "temperature": 0.2,
                "system": LLM_TEMPLATE.format(max_words=max_words),
                "messages": [{"role": "user", "content": text}],
            },
        )
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", [])).strip()


def llm_backend(cfg: Config) -> Optional[str]:
    p = cfg.llm_provider
    if p in ("none", "extractive", "off"):
        return None
    if p in ("openai", "auto") and cfg.openai_api_key:
        return "openai"
    if p in ("anthropic", "auto") and cfg.anthropic_api_key:
        return "anthropic"
    return None


async def summarize(cfg: Config, text: str, max_words: int) -> Summary:
    ext, method = extractive_details(text, max_words)
    extractive = Summary(ext, EXTRACTIVE_MODEL_ID, EXTRACTIVE_TEMPLATE)
    backend = llm_backend(cfg)
    # LLM_MODE=hard (default): the LLM only gets inputs extraction handles badly — a single
    # sentence, or anything that needs clause compression. LLM_MODE=always: every input.
    hard = method != "sentences"
    if not backend or (cfg.llm_mode != "always" and not hard):
        return extractive
    try:
        if backend == "openai":
            raw, model = await _openai(cfg, text, max_words), cfg.openai_model
        else:
            raw, model = await _anthropic(cfg, text, max_words), cfg.anthropic_model
        out, why = _llm_ok(raw, text, max_words)
        if out:
            return Summary(out, model, LLM_TEMPLATE)
        log.warning("llm answer rejected, using extractive", extra={"backend": backend, "reason": why})
    except Exception as exc:  # noqa: BLE001 — any LLM failure falls back to extractive
        log.warning("llm failed, falling back to extractive",
                    extra={"backend": backend, "error": type(exc).__name__})
    return extractive
