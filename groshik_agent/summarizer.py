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
from itertools import product
from typing import Optional

import httpx

from .config import Config

log = logging.getLogger("groshik.summarizer")

# Kept equal to the model_id in the registered model card; the algorithm revision is
# visible in prompt_template_hash (the template text below changed in 0.1.3).
EXTRACTIVE_MODEL_ID = "groshik-extractive-v1"

# The template is part of the proof (sha256), so any change to it shows up as a new hash.
EXTRACTIVE_TEMPLATE = (
    "groshik-extractive-v4: split into sentences and clauses; rank spans by lead-term coverage, "
    "how many sentences they keep whole, and centrality; within max_words keep several key "
    "sentences or whole clauses in original order so the main facts survive; keep the main "
    "clause (subject, main verb, and its core object) and drop subordinate or trailing material "
    "rather than returning only a subordinate clause; never cut inside a phrase, never end on a "
    "dangling word, never start on a bare verb, never return the input unchanged, never an ellipsis."
)
LLM_TEMPLATE = (
    "You summarize text for THE LINE Protocol. Write 1-3 concise sentences, at most "
    "{max_words} words, preserving the key information. Use only facts present in the "
    "text: add no numbers, dates, names or claims that the text does not contain. "
    "Rephrase in your own words rather than copying the text or its opening, and never "
    "return the input unchanged. End with a complete sentence. Reply with the summary only."
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

# Function words a summary must not end on, and must not start on (except a determiner
# that introduces a noun). Cutting here is what produced "after a long" / "Get paid…".
_ART = {"a", "an", "the"}
_DET = _ART | {
    "this", "that", "these", "those", "their", "his", "her", "its", "our", "your", "my",
    "some", "any", "each", "every", "no", "both", "few", "another", "such", "those",
}
_AUX = {
    "is", "are", "was", "were", "be", "been", "being", "do", "does", "did", "have", "has",
    "had", "having", "will", "would", "can", "could", "should", "may", "might", "must", "shall",
}
_PREP = {
    "of", "to", "in", "on", "at", "by", "for", "with", "from", "into", "over", "under", "about",
    "after", "before", "between", "through", "during", "without", "within", "against", "among",
    "around", "across", "behind", "beyond", "despite", "except", "toward", "towards", "upon",
    "via", "per", "as", "like", "off", "up", "down", "out", "near", "above", "below", "until",
    "since", "along", "inside", "outside", "following",
}
# Adjuncts we may drop whole. "of"/"to" stay attached: they usually complete a noun or infinitive.
_ADJUNCT_PREP = _PREP - {"of", "to", "as", "for", "per"}
_DANGLING = _ART | _DET | _AUX | _PREP | _CONJ
_VERBISH = {
    "get", "gets", "got", "make", "makes", "let", "lets", "see", "take", "give", "keep", "find",
    "tell", "tells", "say", "go", "come", "know", "use", "work", "call", "try", "ask", "need",
    "hand", "hands", "settle", "settles", "negotiate", "negotiates", "pay", "pays", "paid",
    "pass", "passes", "release", "releases", "check", "checks", "move", "moves", "discover",
    "discovers", "point", "points", "walk", "walks", "lie", "lies", "follow", "follows", "fly",
    "flies", "learn", "learns", "waggle", "waggles", "approve", "approved", "authorize",
    "authorized", "close", "rise", "rises", "rose", "interact", "interacted", "record",
    "recorded", "route", "routes", "delay", "delayed", "publish", "published", "leave", "left",
    "fall", "fell", "carry", "carries", "return", "returns",
}
# Adjectives that show up when a noun phrase is sliced ("a long", "a sudden").
_HANGING_ADJ = {
    "long", "short", "sudden", "large", "small", "big", "high", "low", "new", "old", "good",
    "bad", "final", "main", "next", "same", "other", "great", "full", "whole", "early", "late",
}


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


def _terms(s: str) -> set[str]:
    return {t for t in _WORD.findall(s.lower()) if t not in _STOP and len(t) > 2}


def _bare(w: str) -> str:
    return w.lower().strip(",;:.!?\"'«»()[]")


def _word_is_verb(w: str) -> bool:
    lw = _bare(w)
    if lw in _VERBISH or lw in _AUX:
        return True
    return len(lw) > 4 and (lw.endswith("ed") or lw.endswith("ing")) and lw not in _PREP


def _has_verb(words: list[str]) -> bool:
    return any(_word_is_verb(w) for w in words)


# Introduces a subordinate clause. "after"/"before" with no verb are prepositional phrases.
_SUBORD = {
    "when", "whenever", "if", "because", "although", "though", "while", "whereas", "unless",
    "until", "which", "who", "whom", "whose", "where", "whether", "once", "that", "so",
    "since", "before", "after",
}


def _looks_english(words: list[str]) -> bool:
    if not words:
        return False
    ascii_words = sum(1 for w in words if w.isascii())
    return ascii_words * 2 >= len(words)


def _starts_ok(words: list[str]) -> bool:
    """A clause should open on its subject, not a bare verb, conjunction or stray preposition."""
    if not words:
        return False
    w0 = _bare(words[0])
    if w0 in _CONJ or w0 in _AUX:
        return False
    if w0 in _VERBISH or (len(w0) > 4 and (w0.endswith("ed") or w0.endswith("ing")) and w0 not in _PREP):
        return False
    if w0 in _DET:
        if len(words) < 2:
            return False
        w1 = _bare(words[1])
        return w1 not in _DET | _CONJ | _AUX | _PREP | _VERBISH
    if w0 in _PREP:
        return _has_verb(words)
    return True


def _ends_ok(words: list[str]) -> bool:
    if not words:
        return False
    last = _bare(words[-1])
    if last in _DANGLING:
        return False
    if len(words) >= 2 and last in _HANGING_ADJ and _bare(words[-2]) in _DET:
        return False
    return True


def _grammatical(s: str) -> bool:
    words = s.rstrip(".!?…").split()
    if len(words) < 3 or not _starts_ok(words) or not _ends_ok(words):
        return False
    if _looks_english(words) and not _has_verb(words):
        return False
    return True


def _polish(s: str) -> str:
    """Drop a trailing function word or a sliced noun phrase ('a long', 'a sudden')."""
    words = s.rstrip(".!?…").split()
    changed = True
    while words and changed:
        changed = False
        if not _ends_ok(words):
            last = _bare(words[-1])
            if last in _DANGLING:
                words.pop()
                changed = True
            elif len(words) >= 2 and last in _HANGING_ADJ and _bare(words[-2]) in _DET:
                del words[-2:]
                changed = True
    return _finish(" ".join(words))


def _too_close(summary: str, text: str) -> bool:
    """Unchanged input, its opening, or the input with only the first word removed."""
    if not summary or not summary.strip():
        return True
    if _norm(summary) == _norm(text) or is_verbatim_prefix(summary, text):
        return True
    words = text.split()
    return len(words) > 1 and _norm(summary) == _norm(" ".join(words[1:]))


def _correlative_pair(prev: str, part: str) -> bool:
    """Keep 'the longer …, the farther …' together; splitting it leaves a fragment."""
    return bool(re.match(r"(?i)the\s+(longer|more|less|sooner|harder)\b", prev.strip())) and bool(
        re.match(r"(?i)the\s+\w+", part.strip()))


def _refine_clauses(sentence: str) -> list[str]:
    """Clause split. Glue coordinated scraps; keep correlatives and subordinate clauses whole."""
    raw = _clauses(sentence) or [sentence.rstrip(".!?…")]
    merged: list[str] = []
    for part in raw:
        words = part.split()
        if merged and _correlative_pair(merged[-1], part):
            merged[-1] = f"{merged[-1]}, {part.strip()}"
        elif (merged and words and not _has_verb(words) and len(words) <= 4
              and _bare(words[0]) in {"and", "or"} and _has_verb(merged[-1].split())):
            merged[-1] = f"{merged[-1]} {part.strip()}"
        else:
            merged.append(part.strip())
    return merged or [sentence.rstrip(".!?…")]


def _subject_np(sentence: str) -> str:
    """Words before the first verb — the shared subject of a coordinated predicate."""
    subj: list[str] = []
    for w in sentence.rstrip(".!?…").split():
        lw = _bare(w)
        if lw in _VERBISH or lw in _AUX or (len(lw) > 4 and lw.endswith("ed") and lw not in _PREP):
            break
        if lw in _CONJ and subj:
            break
        subj.append(w.strip(","))
        if len(subj) >= 4:
            break
    while subj and _bare(subj[0]) in _CONJ | _PREP:
        subj.pop(0)
    return " ".join(subj)


def _restore_subject(clause: str, sentence: str) -> str:
    """'and fly straight to the source' → 'Other bees fly straight to the source'."""
    words = clause.split()
    if not words or _bare(words[0]) not in {"and", "or", "but", "then"}:
        return ""
    finished = _finish(clause)
    fw = finished.rstrip(".!?").split()
    if not fw or _starts_ok(fw):
        return ""
    subj = _subject_np(sentence)
    if not subj or _norm(subj) in _norm(finished):
        return ""
    restored = _finish(f"{subj} {finished[0].lower()}{finished[1:]}")
    return restored if _grammatical(restored) else ""


def _segment_forms(clause: str) -> list[str]:
    """Subsets of a clause's adjunct phrases. The core stays; an internal adjunct may be dropped.

    Dropping only a trailing phrase is a prefix of the clause. Dropping an internal one
    ('on Tuesday') while keeping a later phrase is not, so the summary is not the opening.
    """
    if re.search(r"(?i)\bthe longer\b", clause) and re.search(r"(?i)\bthe (farther|more|less)\b", clause):
        return [clause.strip()]
    words = clause.split()
    if not words:
        return []
    # A preposition that follows another preposition is its object or an adjective
    # ("by outside experts"), not a new phrase boundary.
    starts: list[int] = []
    for i, w in enumerate(words):
        if i == 0 or _bare(w) not in _ADJUNCT_PREP:
            continue
        if starts and starts[-1] == i - 1:
            continue
        starts.append(i)
    bounds = [0, *starts, len(words)]
    segs = [words[bounds[i]:bounds[i + 1]] for i in range(len(bounds) - 1)]
    tail = len(segs) - 1
    forms: list[str] = []
    # All subsets of the adjunct segments when there are few; otherwise full, core, and drop-one.
    if tail <= 4:
        masks = range(1 << tail)
    else:
        full = (1 << tail) - 1
        masks = {full, 0, *{full ^ (1 << j) for j in range(tail)}}
    for mask in masks:
        parts = [segs[0]]
        for j in range(tail):
            if mask & (1 << j):
                parts.append(segs[1 + j])
        flat = [w for part in parts for w in part]
        if flat:
            forms.append(" ".join(flat))
    # Longest first so a multi-clause join keeps the full adjunct unless this clause is last.
    forms.extend(_clause_trims(clause, forms))
    forms.sort(key=lambda s: len(s.split()), reverse=True)
    seen: set[str] = set()
    out: list[str] = []
    for form in forms:
        key = _norm(form)
        if key and key not in seen:
            seen.add(key)
            out.append(form)
    return out or [clause.strip()]


def _is_subordinate(clause: str) -> bool:
    words = clause.split()
    return bool(words) and _bare(words[0]) in _SUBORD and _has_verb(words)


def _clause_trims(clause: str, forms: list[str]) -> list[str]:
    """Shorter clause forms that still end on a phrase boundary.

    A trailing adverb ('only') can go. On a subordinate clause the object after the
    verb can go too ('passes the checks' → 'passes'), so the matrix clause stays and
    the summary is not only the subordinate clause.
    """
    extra: list[str] = []
    subordinate = _is_subordinate(clause)
    for form in forms:
        words = form.split()
        if (len(words) > 3 and _bare(words[-1]) in _STOP | _CONJ
                and _has_verb(words[:-1]) and _ends_ok(words[:-1])):
            extra.append(" ".join(words[:-1]))
        if not subordinate:
            continue
        # The clause verb is the first verb not used as a noun ("the checks").
        last = next(
            (i for i, w in enumerate(words)
             if _word_is_verb(w) and not (i > 0 and _bare(words[i - 1]) in _DET)),
            -1,
        )
        if 0 <= last < len(words) - 1:
            trimmed = words[:last + 1]
            if len(trimmed) >= 3 and _ends_ok(trimmed):
                extra.append(" ".join(trimmed))
    return extra


def _matrix_anchors(sentence: str) -> set[str]:
    """Subject head + main verb of the first clause that is not subordinate."""
    for clause in _refine_clauses(sentence):
        if _is_subordinate(clause):
            continue
        subject: list[str] = []
        verb = ""
        for w in clause.split():
            if _word_is_verb(w):
                verb = _bare(w)
                break
            if _bare(w) not in _DET | _CONJ | _PREP | _AUX:
                subject.append(_bare(w))
        if verb and subject:
            return {subject[0], verb}
        if verb:
            return {verb}
    return set()


def _covers_matrix(span: str, anchors: set[str]) -> bool:
    if not anchors:
        return True
    toks = {_bare(w) for w in span.replace("—", " ").split()}
    return anchors <= toks


def _join(parts: list[str]) -> str:
    bits: list[str] = []
    for part in parts:
        part = part.strip().strip(",;").rstrip(".!?")
        if not part:
            continue
        first = _bare(part.split()[0])
        if not bits:
            bits.append(part)
        elif first in _CONJ:
            bits.append(" " + part)
        else:
            bits.append(", " + part)
    return _polish("".join(bits))


def _skippable_clause(clause: str) -> bool:
    """A middle clause we may omit without gluing the next words onto the wrong head."""
    words = clause.split()
    if not words:
        return False
    w0 = _bare(words[0])
    if w0 in {"and", "or", "but"}:
        return len(words) > 1 and (_bare(words[1]) in _VERBISH or _has_verb(words))
    return w0 in _ADJUNCT_PREP or w0 in _CONJ or w0 == "subject"


def _valid_indexes(idxs: list[int], clauses: list[str]) -> bool:
    """Contiguous groups, or a gap only where the skipped clause is a side predicate
    and the clause after the gap is not a stranded 'and …'."""
    if not idxs:
        return False
    for left, right in zip(idxs, idxs[1:]):
        if right == left + 1:
            continue
        if not all(_skippable_clause(clauses[k]) for k in range(left + 1, right)):
            return False
        head = _bare(clauses[right].split()[0])
        if head in {"and", "or", "but"}:
            return False
    return True


def _spans(sentence: str) -> list[tuple[str, bool]]:
    """Grammatical spans of one sentence: whole clauses and clause groups, adjuncts dropped whole.

    The bool is True when the span is the sentence itself.
    """
    whole = _norm(sentence)
    clauses = _refine_clauses(sentence)
    # Longest form first, then the same clause with trailing adjuncts removed, then a
    # subject restored onto a coordinated verb phrase ('and fly …').
    forms: list[list[str]] = []
    restored_keys: set[str] = set()
    for clause in clauses:
        opts = _segment_forms(clause)
        restored = _restore_subject(clause, sentence)
        if restored:
            bare = restored.rstrip(".!?")
            opts.append(bare)
            restored_keys.add(_norm(bare))
        forms.append(opts or [clause])
    n = len(clauses)
    if n <= 8:
        index_sets = [[i for i in range(n) if mask & (1 << i)] for mask in range(1, 1 << n)]
    else:
        index_sets = [[i] for i in range(n)]
        index_sets.append(list(range(n)))
    out: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for idxs in index_sets:
        if not _valid_indexes(idxs, clauses):
            continue
        chosen = []
        for i in idxs:
            opts = forms[i][:3]
            # A restored subject ('Other bees fly …') is a standalone span, not a tail.
            if len(idxs) > 1:
                opts = [piece for piece in opts if _norm(piece) not in restored_keys] or opts[:1]
            chosen.append(opts)
        combos = list(product(*chosen)) if len(idxs) <= 4 else [tuple(c[0] for c in chosen)]
        if len(combos) > 48:
            full = tuple(c[0] for c in chosen)
            combos = [full]
            for i, opts in enumerate(chosen):
                for alt in opts[1:]:
                    combo = list(full)
                    combo[i] = alt
                    combos.append(tuple(combo))
        for combo in combos:
            text = _join(list(combo))
            key = _norm(text)
            if not key or key in seen or not _grammatical(text):
                continue
            seen.add(key)
            out.append((text, key == whole))
    return out


def _window(sentence: str, limit: int, score) -> str:
    """No clause boundary at all: best-scoring run of words that does not start at word 0."""
    w = sentence.rstrip(".!?…").split()
    if len(w) < 2:
        return _polish(sentence)
    size = max(5, min(limit, len(w) - 1))
    best, best_i = -1.0, 1
    if len(w) <= size:
        return _polish(" ".join(w[1:]))
    for i in range(1, len(w) - size + 1):
        sc = score(" ".join(w[i:i + size]))
        if sc > best:
            best, best_i = sc, i
    return _polish(" ".join(w[best_i:best_i + size]))


def _best_span(sentence: str, limit: int, score, text: str) -> tuple[str, str]:
    """Highest-scoring grammatical span inside `limit` that keeps the main clause.

    A span that is only the subordinate clause ('Their result passes the checks.') is
    used only when no main-clause span fits. A sentence that already fits `limit` is
    still shortened, at a phrase boundary, so the input is not echoed back.
    """
    anchors = _matrix_anchors(sentence)
    sent_n = len(_words(sentence))
    pool: list[tuple[str, bool, int]] = []
    for span, whole in _spans(sentence):
        n = len(span.split())
        if n == 0 or n > limit or _too_close(span, text):
            continue
        pool.append((span, whole, n))
    anchored = [item for item in pool if _covers_matrix(item[0], anchors)]
    chosen = anchored or pool
    if anchored and sent_n <= limit:
        need = max(2, sent_n // 3)
        shortened = [item for item in anchored if item[2] <= sent_n - need]
        if shortened:
            chosen = shortened
    if chosen:
        chosen.sort(key=lambda item: (score(item[0]), item[2]), reverse=True)
        span, whole, _n = chosen[0]
        return span, "sentences" if whole else "clauses"
    window = _window(sentence, max(1, limit), score)
    # No clause cut that is not the opening. A word window that is not the opening
    # still beats echoing the sentence (no punctuation to split on).
    if window and not _too_close(window, text):
        return window, "window"
    # Every non-prefix cut was rejected. A clean main clause (even the opening)
    # beats echoing the sentence or returning only the subordinate clause.
    for span, whole in _spans(sentence):
        n = len(span.split())
        if n == 0 or n >= sent_n or n > limit or not _covers_matrix(span, anchors):
            continue
        chosen.append((span, whole, n))
    if chosen and sent_n <= limit:
        need = max(2, sent_n // 3)
        shortened = [item for item in chosen if item[2] <= sent_n - need]
        if shortened:
            chosen = shortened
    if chosen:
        chosen.sort(key=lambda item: (score(item[0]), item[2]), reverse=True)
        span, whole, _n = chosen[0]
        return span, "sentences" if whole else "clauses"
    # A clean shorter clause beats a broken window even when it is the opening of the input.
    clean = [span for span, _whole in _spans(sentence) if span and len(span.split()) <= limit]
    main = [span for span in clean if _covers_matrix(span, anchors)] or clean
    if main:
        main.sort(key=lambda s: (score(s), len(s.split())), reverse=True)
        return main[0], "clauses"
    return _polish(sentence), "window"


def _term_weights(text: str) -> dict[str, int]:
    freq: dict[str, int] = {}
    for tok in _WORD.findall(text.lower()):
        if tok not in _STOP and len(tok) > 2:
            freq[tok] = freq.get(tok, 0) + 1
    boost: dict[str, int] = {}
    sentences = _split_sentences(text)
    if sentences:
        for tok in _terms(sentences[0]):
            boost[tok] = boost.get(tok, 0) + 2
        if len(sentences) > 1:
            for tok in _terms(sentences[-1]):
                boost[tok] = boost.get(tok, 0) + 2
    return {tok: 1 + freq[tok] + boost.get(tok, 0) for tok in freq}


def _assemble(choice: list[tuple[int, str, bool]]) -> str:
    return " ".join(span if span.endswith((".", "!", "?")) else _finish(span) for _, span, _ in choice)


def _combo_key(choice: list[tuple[int, str, bool]], weights: dict[str, int], sentences: list[str]):
    """Prefer the lead's terms, then more sentences, then whole sentences, then each sentence's ending."""
    covered: set[str] = set()
    whole = tails = 0
    for si, span, is_whole in choice:
        covered |= _terms(span)
        if is_whole:
            whole += 1
        content = [t for t in _WORD.findall(sentences[si].lower()) if t not in _STOP and len(t) > 2]
        if content and content[-1] in _terms(span):
            tails += 1
    lead_hit = len(_terms(sentences[0]) & covered) if sentences else 0
    words = sum(len(span.split()) for _si, span, _whole in choice)
    fragments = len(choice) - whole
    # Whole sentences beat fragments. Among equally whole summaries, fewer leftover
    # fragments, then more sentences, then the shorter one (drops a long aside).
    return (lead_hit, whole, -fragments, len(choice), tails, -words)


def _candidates(sentence: str, max_words: int, score) -> list[tuple[str, bool, int]]:
    cands: list[tuple[str, bool, int]] = []
    seen: set[str] = set()
    for span, whole in _spans(sentence):
        n = len(span.split())
        key = _norm(span)
        if n == 0 or n > max_words or key in seen:
            continue
        seen.add(key)
        cands.append((span, whole, n))
    cands.sort(key=lambda c: (c[1], score(c[0]), c[2]), reverse=True)
    return cands[:8]


def _last_tail(choice: list[tuple[int, str, bool]], sentences: list[str]) -> int:
    if not sentences or not choice:
        return 0
    content = [t for t in _WORD.findall(sentences[-1].lower()) if t not in _STOP and len(t) > 2]
    if not content:
        return 0
    covered: set[str] = set()
    for _si, span, _whole in choice:
        covered |= _terms(span)
    return int(content[-1] in covered)


def _search(sentences: list[str], max_words: int, score, text: str, wholes_only: bool = False) -> list[tuple[int, str, bool]]:
    weights = _term_weights(text)
    per = []
    for sentence in sentences:
        cands = _candidates(sentence, max_words, score)
        if wholes_only:
            cands = [c for c in cands if c[1]]
        per.append(cands)
    best_key = None
    best: list[tuple[int, str, bool]] = []

    def rec(i: int, choice: list[tuple[int, str, bool]], words: int) -> None:
        nonlocal best_key, best
        if words > max_words:
            return
        if i == len(per):
            if not choice:
                return
            if _too_close(_assemble(choice), text):
                return
            key = _combo_key(choice, weights, sentences)
            if best_key is None or key > best_key:
                best_key = key
                best = list(choice)
            return
        rec(i + 1, choice, words)
        for span, whole, n in per[i]:
            choice.append((i, span, whole))
            rec(i + 1, choice, words + n)
            choice.pop()

    rec(0, [], 0)
    return best


def _choose(sentences: list[str], max_words: int, score, text: str) -> list[tuple[int, str, bool]]:
    """Whole sentences when they already carry the lead and the closing fact; otherwise clauses."""
    mixed = _search(sentences, max_words, score, text, wholes_only=False)
    whole = _search(sentences, max_words, score, text, wholes_only=True)
    if not mixed:
        return whole
    if not whole:
        return mixed
    if (_combo_key(whole, {}, sentences)[0] >= _combo_key(mixed, {}, sentences)[0]
            and _last_tail(whole, sentences) >= _last_tail(mixed, sentences)):
        return whole
    return mixed


def _greedy(sentences: list[str], max_words: int, score, text: str) -> list[tuple[int, str, bool]]:
    """Long documents: a handful of high-scoring, de-duplicated spans in original order."""
    pool: list[tuple[int, str, bool, int, float]] = []
    seen: set[str] = set()
    for si, sentence in enumerate(sentences):
        for span, whole, n in _candidates(sentence, max_words, score):
            key = _norm(span)
            if key in seen:
                continue
            seen.add(key)
            pool.append((si, span, whole, n, score(span) * (1.15 if si == 0 else 1.0)))
            break
    pool.sort(key=lambda item: -item[4])
    chosen: list[tuple[int, str, bool, int]] = []
    used = 0
    for si, span, whole, n, _sc in pool:
        if len(chosen) >= 4 or used + n > max_words:
            continue
        chosen.append((si, span, whole, n))
        used += n
    chosen.sort(key=lambda item: item[0])
    picked = [(si, span, whole) for si, span, whole, _n in chosen]
    while len(picked) > 1 and _too_close(_assemble(picked), text):
        picked.pop(1 if len(picked) > 2 else -1)
    return picked


def extractive_details(text: str, max_words: int) -> tuple[str, str]:
    """Return (summary, method). method: sentences | whole_sentence | clauses | window."""
    sentences = _split_sentences(text)
    if not sentences:
        return "", "empty"
    score = _scorer(text)
    if len(sentences) == 1:
        sentence = sentences[0]
        if len(sentence.split()) <= 1:
            return (sentence if sentence[-1:] in ".!?" else _finish(sentence)), "whole_sentence"
        return _best_span(sentence, max_words, score, text)

    choice = _choose(sentences, max_words, score, text) if len(sentences) <= 5 else _greedy(
        sentences, max_words, score, text)
    if not choice:
        return _best_span(sentences[0], max_words, score, text)
    method = "sentences" if all(whole for _si, _span, whole in choice) else "clauses"
    return _assemble(choice), method


def extractive_summary(text: str, max_words: int) -> str:
    return extractive_details(text, max_words)[0]


_NUM = re.compile(r"\d+(?:[.,]\d+)?")
# Models that reject a temperature parameter. Keyed by "provider:model", per process.
_omit_temperature: set[str] = set()
_SECRET_TEXT = re.compile(
    r"(?i)(sk-[A-Za-z0-9_\-]{4,}|bearer\s+\S+|api[_-]?key[\"']?\s*[:=]\s*\S+)"
)


def _short_error(text: str) -> str:
    """One line of provider error text, with anything that looks like a key removed."""
    text = _SECRET_TEXT.sub("[redacted]", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:240]


def _is_temperature_rejection(status: int, text: str) -> bool:
    """HTTP 400 whose body is about temperature (Claude deprecation, OpenAI unsupported)."""
    if status != 400:
        return False
    low = (text or "").lower()
    if "temperature" not in low:
        return False
    # Claude: "temperature is deprecated for this model".
    # OpenAI: code unsupported_parameter / message "Unsupported value" for temperature.
    return True


def _failure_detail(exc: BaseException) -> tuple[Optional[int], str]:
    response = getattr(exc, "response", None)
    if response is not None:
        status = getattr(response, "status_code", None)
        body = _short_error(getattr(response, "text", "") or "")
        return status, body or type(exc).__name__
    return None, _short_error(str(exc)) or type(exc).__name__


async def _chat_post(http, url: str, headers: dict, payload: dict, model_key: str):
    """POST a chat payload. On a temperature 400, retry once without it and remember."""
    body = dict(payload)
    if model_key in _omit_temperature:
        body.pop("temperature", None)
    response = await http.post(url, headers=headers, json=body)
    if (response.status_code == 400 and "temperature" in body
            and _is_temperature_rejection(response.status_code, response.text)):
        _omit_temperature.add(model_key)
        short = _short_error(response.text)
        log.warning(
            "llm rejected temperature, retrying without it status=%s error=%s",
            response.status_code, short,
            extra={"model": model_key, "status_code": response.status_code, "error": short},
        )
        body.pop("temperature", None)
        response = await http.post(url, headers=headers, json=body)
    response.raise_for_status()
    return response


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
        r = await _chat_post(
            http,
            f"{cfg.openai_base_url.rstrip('/')}/chat/completions",
            {"Authorization": f"Bearer {cfg.openai_api_key}"},
            {
                "model": cfg.openai_model,
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": LLM_TEMPLATE.format(max_words=max_words)},
                    {"role": "user", "content": text},
                ],
            },
            f"openai:{cfg.openai_model}",
        )
        return r.json()["choices"][0]["message"]["content"].strip()


async def _anthropic(cfg: Config, text: str, max_words: int) -> str:
    async with httpx.AsyncClient(timeout=cfg.llm_timeout_s) as http:
        r = await _chat_post(
            http,
            "https://api.anthropic.com/v1/messages",
            {"x-api-key": cfg.anthropic_api_key, "anthropic-version": "2023-06-01"},
            {
                "model": cfg.anthropic_model,
                "max_tokens": 400,
                "temperature": 0.2,
                "system": LLM_TEMPLATE.format(max_words=max_words),
                "messages": [{"role": "user", "content": text}],
            },
            f"anthropic:{cfg.anthropic_model}",
        )
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
        status, short = _failure_detail(exc)
        log.warning(
            "llm failed, falling back to extractive status=%s error=%s",
            "-" if status is None else status, short,
            extra={"backend": backend, "status_code": status, "error": short},
        )
    return extractive
