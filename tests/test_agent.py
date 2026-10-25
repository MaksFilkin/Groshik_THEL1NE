"""Offline tests: no network, no registration, throwaway key dir."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from groshik_agent import state
from groshik_agent.agent import GroshikAgent
from groshik_agent.config import Config
from groshik_agent.summarizer import EXTRACTIVE_MODEL_ID, extractive_summary
from the_line_sdk.proof import input_sha256

SECRET = "test-trusted-secret-not-real"
TEXT = (
    "THE LINE is an agent-to-agent commerce protocol running on the Tempo testnet. "
    "Buyers post intents and the protocol routes each one to a provider agent that can do the work. "
    "Providers are paid in USDC only after their result passes automatic checks. "
    "Every result must carry a proof of execution, and refused results are not paid. "
    "The weather in the office was pleasant this week."
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "THE_LINE_TRUSTED_SECRET", "THE_LINE_AGENT_ID",
              "THE_LINE_API_KEY", "AGENT_WALLET_PK", "AGENT_SIGNING_PRIV"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("THE_LINE_KEYDIR", str(tmp_path))
    cfg = Config()
    agent = GroshikAgent(cfg)
    agent._load_keys(create=True)  # throwaway keys in tmp_path
    state.save(tmp_path, {"agent_id": "test-agent", "trusted_secret": SECRET})
    agent.prepare_serve()
    return TestClient(agent._build_app())


def signed(body: dict, secret: str = SECRET, nonce: str | None = None, ts: int | None = None):
    raw = json.dumps(body).encode()
    ts = ts or int(time.time())
    nonce = nonce or uuid.uuid4().hex
    sig = hmac.new(secret.encode(), f"{ts}.{nonce}.".encode() + raw, hashlib.sha256).hexdigest()
    return raw, {"x-trusted-sig": sig, "x-timestamp": str(ts), "x-nonce": nonce,
                 "content-type": "application/json"}


def test_health_ping_quote(client):
    h = client.get("/health").json()
    assert h["status"] == "online" and h["agent"].startswith("Groshik")
    ping = client.post("/the-line/ping", json={}).json()
    assert ping["available"] is True
    q = client.post("/the-line/quote", json={"capability": "text_summary"}).json()
    assert q["price_usdc"] == pytest.approx(0.05)


def test_dispatch_ok_has_summary_proof_envelope(client):
    task = {"text": TEXT, "max_words": 40}
    raw, h = signed({"intent_id": "intent_test", "task_input": task})
    r = client.post("/the-line/dispatch-trusted", content=raw, headers=h)
    assert r.status_code == 200, r.text
    out = r.json()["output"]
    assert out["summary"] and out["summary"] != TEXT
    assert len(out["summary"].split()) <= 40
    assert out["proof"]["input_sha256"] == input_sha256(task)
    assert out["proof"]["model_id"] == EXTRACTIVE_MODEL_ID
    assert "_envelope" in out


def test_dispatch_rejects_bad_sig_missing_headers_and_replay(client):
    body = {"intent_id": "i", "task_input": {"text": TEXT}}
    raw, h = signed(body, secret="wrong")
    assert client.post("/the-line/dispatch-trusted", content=raw, headers=h).status_code == 401
    assert client.post("/the-line/dispatch-trusted", content=raw,
                       headers={"content-type": "application/json"}).status_code == 401
    raw, h = signed(body, ts=int(time.time()) - 3600)
    assert client.post("/the-line/dispatch-trusted", content=raw, headers=h).status_code == 401
    raw, h = signed(body, nonce="fixed-nonce")
    assert client.post("/the-line/dispatch-trusted", content=raw, headers=h).status_code == 200
    assert client.post("/the-line/dispatch-trusted", content=raw, headers=h).status_code == 401


def test_dispatch_bad_input_is_an_error_not_a_payout(client):
    raw, h = signed({"intent_id": "i", "task_input": {"text": "   "}})
    r = client.post("/the-line/dispatch-trusted", content=raw, headers=h)
    assert r.status_code == 500 and r.json()["success"] is False


def test_extractive_is_deterministic_bounded_and_adds_nothing():
    a = extractive_summary(TEXT, 30)
    assert a == extractive_summary(TEXT, 30)
    assert len(a.split()) <= 30
    src = set(TEXT.split())
    assert "…" not in a and all(w in src for w in a.split()[:-1])


def test_status_command_runs(monkeypatch, tmp_path, caplog):
    """`name` in the status payload must not collide with LogRecord.name."""
    monkeypatch.setenv("THE_LINE_KEYDIR", str(tmp_path))
    monkeypatch.setenv("THE_LINE_AGENT_ID", "agent-test")
    monkeypatch.setenv("THE_LINE_TRUSTED_SECRET", SECRET)
    for k in ("THE_LINE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AGENT_WALLET_PK",
              "AGENT_SIGNING_PRIV"):
        monkeypatch.delenv(k, raising=False)

    async def fake_get(self, path, params=None):
        assert path == "/agent/agent-test"
        return {
            "agent_id": "agent-test",
            "name": "Groshik",
            "status": "online",
            "endpoint_url": "https://agent.the1ine.com",
            "capability_verified": True,
            "capability_verify_error": None,
            "reputation_score": 0,
            "tempo_wallet_address": "0xabc",
        }

    monkeypatch.setattr("the_line_sdk.TheLineClient._get", fake_get)
    from main import cmd_status
    cfg = Config()
    caplog.set_level(logging.INFO, logger="groshik")
    assert asyncio.run(cmd_status(cfg)) == 0
    rec = next(r for r in caplog.records if r.getMessage() == "status")
    assert rec.agent_name == "Groshik"
    assert rec.name == "groshik"


def test_long_single_sentence_dispatch_has_no_ellipsis(client):
    task = {"text": "Providers on the network are paid in USDC only after the oracle checks their result, "
                    "which must carry a proof of execution, match the canonical output fields, and not "
                    "simply hand the input back to the buyer who asked for the work.", "max_words": 15}
    raw, h = signed({"intent_id": "intent_long", "task_input": task})
    r = client.post("/the-line/dispatch-trusted", content=raw, headers=h)
    s = r.json()["output"]["summary"]
    assert r.status_code == 200 and "…" not in s and s.endswith(".") and len(s.split()) <= 15
