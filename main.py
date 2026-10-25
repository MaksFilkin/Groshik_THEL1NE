#!/usr/bin/env python3
"""Groshik — THE LINE Protocol (the1ine) reference agent, run by THE LINE team.

    python main.py                 # serve (default): HOST:PORT, 127.0.0.1:8001 by default
    python main.py register        # one-time, idempotent registration (service must be running)
    python main.py register --force
    python main.py status          # registration + verification status (no secrets printed)
    python main.py verify          # ask the protocol to re-send the reference task
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_env_file() -> None:
    """Read KEY=VALUE lines from $AGENT_ENV_FILE or ./agent.env. Real env vars win."""
    path = Path(os.environ.get("AGENT_ENV_FILE") or ROOT / "agent.env")
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
            val = val[1:-1]
        os.environ.setdefault(key, val)


load_env_file()

from groshik_agent import state  # noqa: E402
from groshik_agent.agent import GroshikAgent  # noqa: E402
from groshik_agent.config import Config  # noqa: E402
from groshik_agent.logging_setup import setup_logging  # noqa: E402

log = logging.getLogger("groshik")


def cmd_serve(cfg: Config) -> int:
    agent = GroshikAgent(cfg)
    registered = agent.prepare_serve()
    log.info("starting", extra={
        "host": cfg.host, "port": cfg.port, "registered": registered, "agent_id": agent.agent_id,
        "price_usdc": cfg.price_usdc, "signing_key_loaded": bool(agent._signing_pub),
    })
    if not registered:
        log.warning("not registered yet: /health, /the-line/ping and /the-line/quote work; "
                    "dispatch is refused until `python main.py register` (picked up without restart)")
    agent.run(host=cfg.host, port=cfg.port, heartbeat_interval=cfg.heartbeat_interval)
    return 0


async def _endpoint_ok(url: str) -> tuple[bool, str]:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.get(url.rstrip("/") + "/health")
        if r.status_code == 200 and r.json().get("status") == "online":
            from groshik_agent.config import AGENT_NAME
            if r.json().get("agent") == AGENT_NAME:
                return True, "ok"
            return False, f"another agent answers there: {r.json().get('agent')!r}"
        return False, f"HTTP {r.status_code}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


async def cmd_register(cfg: Config, force: bool, skip_check: bool, wait: float) -> int:
    from the_line_sdk import TheLineError
    if not cfg.owner_email:
        log.error("AGENT_OWNER_EMAIL is required for registration")
        return 2
    agent = GroshikAgent(cfg)
    existing = state.load(cfg.keydir)
    if existing and existing.get("agent_id") and not force:
        agent.apply_registration(existing)
        try:
            info = await agent.client._get(f"/agent/{existing['agent_id']}")
            log.info("already registered — nothing to do (use --force to re-register)", extra={
                "agent_id": existing["agent_id"], "endpoint": existing.get("endpoint_url"),
                "capability_verified": info.get("capability_verified"),
                "verify_error": info.get("capability_verify_error"),
            })
            return 0
        except TheLineError as exc:
            if exc.status_code != 404:
                log.error("could not check existing registration", extra={"error": str(exc)})
                return 1
            log.warning("saved agent_id not found on the protocol — registering again")

    if not skip_check:
        ok, why = await _endpoint_ok(cfg.endpoint_url)
        if not ok:
            log.error("endpoint is not serving this agent — start the service first "
                      "(or pass --skip-endpoint-check)", extra={"endpoint": cfg.endpoint_url, "reason": why})
            return 1

    try:
        resp = await agent.register(endpoint_url=cfg.endpoint_url)
    except TheLineError as exc:
        log.error("registration failed", extra={"status": exc.status_code, "error": str(exc)})
        return 1
    reg = {
        "agent_id": agent.agent_id,
        "api_key": agent.client.api_key,
        "trusted_secret": agent._trusted_secret,
        "tempo_wallet_address": agent.tempo_wallet_address,
        "signing_public_key": agent._signing_pub,
        "endpoint_url": cfg.endpoint_url,
        "the_line_url": cfg.the_line_url,
        "registered_at": int(time.time()),
    }
    path = state.save(cfg.keydir, reg)
    log.info("registered; state saved (mode 600)", extra={
        "agent_id": agent.agent_id, "wallet": agent.tempo_wallet_address, "state_file": str(path),
        "has_trusted_secret": bool(agent._trusted_secret), "status": resp.get("status"),
    })
    if wait <= 0:
        return 0
    # The running service reloads the state file on the next dispatch; give the protocol a
    # fresh reference task now that it can be answered, then report the verdict.
    await asyncio.sleep(3)
    ok, why = await agent.wait_verified(timeout=8)
    if not ok:
        try:
            await agent.request_verification()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not request verification", extra={"error": str(exc)})
        ok, why = await agent.wait_verified(timeout=wait)
    if ok:
        log.info("verified — eligible for tasks", extra={"agent_id": agent.agent_id})
        return 0
    log.error("NOT verified yet", extra={"agent_id": agent.agent_id, "reason": why})
    return 3


async def cmd_status(cfg: Config) -> int:
    agent = GroshikAgent(cfg)
    if not agent.load_registration() and not agent.agent_id:
        log.info("not registered", extra={"state_file": str(state.path(cfg.keydir))})
        return 1
    info = await agent.client._get(f"/agent/{agent.agent_id}")
    safe = {k: info.get(k) for k in ("agent_id", "name", "status", "endpoint_url", "capability_verified",
                                     "capability_verify_error", "reputation_score", "tempo_wallet_address")}
    log.info("status", extra=safe)
    return 0


async def cmd_verify(cfg: Config) -> int:
    agent = GroshikAgent(cfg)
    if not agent.load_registration():
        log.error("not registered")
        return 1
    await agent.request_verification()
    ok, why = await agent.wait_verified(timeout=120)
    log.info("verification", extra={"verified": ok, "reason": why})
    return 0 if ok else 3


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the HTTP server (default)")
    r = sub.add_parser("register", help="register on THE LINE (idempotent)")
    r.add_argument("--force", action="store_true", help="register again even if state exists")
    r.add_argument("--skip-endpoint-check", action="store_true")
    r.add_argument("--wait", type=float, default=120.0, help="seconds to wait for verification (0 = skip)")
    sub.add_parser("status", help="show registration / verification status")
    sub.add_parser("verify", help="ask the protocol to re-run the reference task")
    args = p.parse_args(argv)

    cfg = Config()
    setup_logging(cfg.log_level)
    try:
        if args.cmd in (None, "serve"):
            return cmd_serve(cfg)
        if args.cmd == "register":
            return asyncio.run(cmd_register(cfg, args.force, args.skip_endpoint_check, args.wait))
        if args.cmd == "status":
            return asyncio.run(cmd_status(cfg))
        if args.cmd == "verify":
            return asyncio.run(cmd_verify(cfg))
    except KeyboardInterrupt:
        return 130
    except Exception:  # noqa: BLE001
        log.exception("fatal error")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
