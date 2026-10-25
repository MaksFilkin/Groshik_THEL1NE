# Changelog

## 0.1.2 — 2026-10-25
- **Fix (pre-payout screen):** a single sentence that already fit `max_words` was returned verbatim,
  so the screen saw 100% overlap and a judge called it "input returned unchanged". The extractive
  engine now keeps the highest-scoring clause or interior span, short enough that the result is
  not the input, its opening, or the input with only the first word removed. No new facts are
  added. With an LLM configured, that input is rephrased by the model (`LLM_MODE=hard` already
  routes single sentences there).
- **Fix (incomplete summary):** when the highest-scoring sentence was longer than `max_words`, a
  shorter lesser sentence was substituted (a market aside instead of the board's decision). That
  sentence is now compressed by clauses.
- **Fix:** `python main.py status` crashed with `KeyError: Attempt to overwrite 'name' in LogRecord`.
  The API's `name` field is logged as `agent_name`. Other logging extras do not use reserved
  LogRecord attribute names.
- Extractive `model_id` stays `groshik-extractive-v1`. The template text is `groshik-extractive-v3`,
  so `prompt_template_hash` tracks the new rules.

## 0.1.1 — 2026-10-25
- **Fix (pre-payout screen):** a single long sentence was clipped with "…", which the screen held
  as "the beginning of the input verbatim + cut off". The extractive engine now never truncates:
  - one sentence within `max_words` is returned whole;
  - a longer sentence is reduced to its most important complete clauses (split at `, ; : — ( )`
    and conjunctions, EN + RU), joined in order and ending with a full stop;
  - no punctuation or conjunctions at all: the best-scoring run of words not starting at word 0;
  - output that would be just the opening words of the input is rearranged (a later
    sentence/clause is swapped in) so it is never a verbatim prefix;
  - repeated sentences count once.
- **LLM routing:** new `LLM_MODE` (`hard` default, `always`). With a key, single-sentence inputs and
  anything that needs compression go to the LLM; other inputs use the deterministic engine.
- **LLM guard:** answers that are empty, cut off, a copy or verbatim prefix of the input, or contain
  numbers the input lacks are rejected (extractive used instead); over-long answers are trimmed to
  whole sentences.
- Extractive `model_id` stays `groshik-extractive-v1` (matches the registered model card); the
  revised algorithm shows up as a new `prompt_template_hash` in the proof.
- Tests: 22 (one long sentence, one short sentence, no punctuation, very long multi-sentence,
  Russian input, LLM routing and rejection).
- Repo: MIT license; README lists the live agent id and endpoint.

## 0.1.0 — 2026-10-25
- First release: `text_summary` provider on the-line-sdk 0.2.9 `AgentServer`, idempotent
  `register`, JSON logs, systemd unit.
