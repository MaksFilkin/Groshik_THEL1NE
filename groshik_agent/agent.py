"""Groshik provider agent: the SDK's AgentServer with a text_summary execute().

HTTP routes, HMAC verification of trusted dispatch, Ed25519 result signing, heartbeat and
verification reporting all come from the_line_sdk.AgentServer — nothing is reimplemented.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from the_line_sdk import AgentServer, TheLineClient
from the_line_sdk.proof import deterministic_proof

from . import __version__, state
from .config import AGENT_NAME, CAPABILITY, Config
from .summarizer import EXTRACTIVE_MODEL_ID, EXTRACTIVE_TEMPLATE, InputError, parse_input, summarize

log = logging.getLogger("groshik.agent")

DESCRIPTION = (
    "Reference provider agent operated by THE LINE Protocol team (the1ine.com). "
    "Summarizes text; deterministic extractive summarizer by default (adds nothing the input "
    "lacks), optional LLM. Testnet."
)


class GroshikAgent(AgentServer):
    def __init__(self, cfg: Config, client: Optional[TheLineClient] = None):
        self.cfg = cfg
        super().__init__(
            client=client or TheLineClient(cfg.the_line_url, api_key=cfg.api_key or None),
            name=AGENT_NAME,
            email=cfg.owner_email or "unset@the1ine.com",
            capabilities=[{
                "capability_name": CAPABILITY,
                "price_usdc_per_unit": cfg.price_usdc,
                "description": DESCRIPTION,
            }],
            port=cfg.port,
            model_id=EXTRACTIVE_MODEL_ID,  # fallback for the SDK's attach_if_missing
            prompt_template=EXTRACTIVE_TEMPLATE,
            model_card={
                "model_id": EXTRACTIVE_MODEL_ID,
                "model_provider": "THE LINE team (the1ine.com)",
                "model_version": __version__,
                "description": DESCRIPTION,
                "operator": "THE LINE Protocol team",
                "links": {"protocol": "https://the1ine.com", "endpoint": cfg.endpoint_url},
            },
        )
        self._key_dir = cfg.keydir  # SDK keeps .agent_wallet / .agent_signing_priv here
        self._state_mtime: float = 0.0

    # ── work ────────────────────────────────────────────────────────────────
    async def execute(self, task_input: dict) -> dict:
        t0 = time.perf_counter()
        try:
            text, max_words = parse_input(task_input)
        except InputError as exc:
            log.warning("bad input", extra={"error": str(exc)})
            raise
        s = await summarize(self.cfg, text, max_words)
        log.info("task done", extra={
            "capability": CAPABILITY, "model_id": s.model_id, "in_words": len(text.split()),
            "out_words": len(s.summary.split()), "ms": round((time.perf_counter() - t0) * 1000, 1),
        })
        return {
            "summary": s.summary,
            # Proof covers the task_input exactly as received and the model that actually ran.
            "proof": deterministic_proof(task_input, model_id=s.model_id, prompt_template=s.prompt_template),
        }

    # ── registration state ──────────────────────────────────────────────────
    def apply_registration(self, reg: dict) -> None:
        if reg.get("agent_id"):
            self.agent_id = reg["agent_id"]
        if reg.get("trusted_secret"):
            self._trusted_secret = reg["trusted_secret"]
        key = reg.get("api_key")
        if key:
            self.client.api_key = key
            self.client._headers["X-API-Key"] = key
        if reg.get("tempo_wallet_address"):
            self.tempo_wallet_address = reg["tempo_wallet_address"]

    def load_registration(self) -> bool:
        """Env values win; otherwise read $THE_LINE_KEYDIR/registration.json."""
        reg = state.load(self.cfg.keydir) or {}
        p = state.path(self.cfg.keydir)
        self._state_mtime = p.stat().st_mtime if p.exists() else 0.0
        merged = {
            "agent_id": self.cfg.agent_id or reg.get("agent_id"),
            "api_key": self.cfg.api_key or reg.get("api_key"),
            "trusted_secret": self.cfg.trusted_secret or reg.get("trusted_secret"),
            "tempo_wallet_address": reg.get("tempo_wallet_address"),
        }
        self.apply_registration(merged)
        return bool(self.agent_id and self._trusted_secret)

    def _maybe_reload(self) -> None:
        """Pick up a registration made while the service is already running (no restart)."""
        p = state.path(self.cfg.keydir)
        try:
            m = p.stat().st_mtime
        except OSError:
            return
        if m != self._state_mtime:
            self.load_registration()
            self._load_keys(create=False)
            log.info("registration state reloaded", extra={"agent_id": self.agent_id})

    def _load_keys(self, create: bool) -> None:
        """Load the wallet/signing keys used to sign results. In serve mode never create new
        ones: keys that differ from the registered public key would break provenance."""
        from the_line_sdk.wallet import get_or_create_signing_keypair, get_or_create_wallet
        import os
        kd: Path = self.cfg.keydir
        have_sign = (kd / ".agent_signing_priv").exists() or os.environ.get("AGENT_SIGNING_PRIV")
        have_wallet = (kd / ".agent_wallet").exists() or os.environ.get("AGENT_WALLET_PK")
        if not create and not (have_sign and have_wallet):
            return
        kd.mkdir(parents=True, exist_ok=True)
        if not self.tempo_wallet_address:
            self.tempo_wallet_address, _ = get_or_create_wallet(kd)
        self._signing_priv, self._signing_pub = get_or_create_signing_keypair(kd)

    def _verify_trusted(self, headers, raw_body: bytes):  # noqa: D401 — SDK hook
        if not self._trusted_secret:
            self._maybe_reload()
        ok, err = super()._verify_trusted(headers, raw_body)
        if not ok and "trusted_secret" not in err:
            # A fresh `register` rotates the secret; retry once with the new one.
            before = self._trusted_secret
            self._maybe_reload()
            if self._trusted_secret != before:
                ok, err = super()._verify_trusted(headers, raw_body)
        if not ok:
            log.warning("dispatch rejected", extra={"reason": err})
        return ok, err

    async def _heartbeat_loop(self, interval: float = 30.0) -> None:
        import asyncio
        while True:
            await asyncio.sleep(interval)
            self._maybe_reload()
            if self.agent_id:
                try:
                    await self.client.send_heartbeat(self.agent_id)
                except Exception as exc:  # noqa: BLE001
                    log.warning("heartbeat failed", extra={"error": str(exc)})

    # ── serving ─────────────────────────────────────────────────────────────
    def prepare_serve(self) -> bool:
        registered = self.load_registration()
        self._load_keys(create=False)
        return registered
