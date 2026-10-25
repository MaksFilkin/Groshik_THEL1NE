"""Configuration. Every value comes from the environment (agent.env); nothing secret lives in code."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

AGENT_NAME = "Groshik — THE LINE Protocol (the1ine) reference agent"
CAPABILITY = "text_summary"
DEFAULT_ENDPOINT = "https://agent.the1ine.com"
DEFAULT_PRICE_USDC = 0.05  # bottom of the protocol's text_summary price band (0.05–1.00)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _float(name: str, default: float) -> float:
    raw = _env(name)
    return float(raw) if raw else default


def _int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw else default


@dataclass
class Config:
    the_line_url: str = field(default_factory=lambda: _env("THE_LINE_URL", "https://api.the1ine.com"))
    endpoint_url: str = field(default_factory=lambda: _env("AGENT_ENDPOINT_URL", DEFAULT_ENDPOINT))
    host: str = field(default_factory=lambda: _env("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _int("PORT", 8001))
    owner_email: str = field(default_factory=lambda: _env("AGENT_OWNER_EMAIL"))
    price_usdc: float = field(default_factory=lambda: _float("AGENT_PRICE_USDC", DEFAULT_PRICE_USDC))
    keydir: Path = field(default_factory=lambda: Path(_env("THE_LINE_KEYDIR", "./state")).resolve())
    heartbeat_interval: float = field(default_factory=lambda: _float("HEARTBEAT_INTERVAL_S", 30.0))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO").upper())
    # Registration results. Normally read from $THE_LINE_KEYDIR/registration.json written by
    # `python main.py register`; env values, when set, take precedence.
    agent_id: str = field(default_factory=lambda: _env("THE_LINE_AGENT_ID"))
    api_key: str = field(default_factory=lambda: _env("THE_LINE_API_KEY"))
    trusted_secret: str = field(default_factory=lambda: _env("THE_LINE_TRUSTED_SECRET"))
    # Optional LLM backends. Without a key the agent uses its deterministic extractive summarizer.
    llm_mode: str = field(default_factory=lambda: _env("LLM_MODE", "hard").lower())
    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", "auto").lower())
    openai_api_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY"))
    openai_model: str = field(default_factory=lambda: _env("OPENAI_MODEL", "gpt-4o-mini"))
    openai_base_url: str = field(default_factory=lambda: _env("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    anthropic_model: str = field(default_factory=lambda: _env("ANTHROPIC_MODEL", "claude-3-5-haiku-latest"))
    llm_timeout_s: float = field(default_factory=lambda: _float("LLM_TIMEOUT_S", 20.0))
