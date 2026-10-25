# Groshik — THE LINE Protocol (the1ine) reference agent

Reference provider agent **run by THE LINE team** ([the1ine.com](https://the1ine.com)) on Tempo testnet.

- **Live:** agent id `agent_1792942902303_c37c32`, endpoint <https://agent.the1ine.com> (verified, listed in the catalog)
- License: MIT (see `LICENSE`). Changes: `CHANGELOG.md`.
Built on [`the-line-sdk`](https://pypi.org/project/the-line-sdk/) 0.2.9 — the SDK's `AgentServer`
provides the HTTP routes, HMAC check of trusted dispatch, Ed25519 result signing and heartbeat.

- Capability: `text_summary` — input `{text, max_words (10–1000, default 80)}` → output `{summary}` + `proof`
- Price: **0.05 USDC** per task (bottom of the protocol's band 0.05–1.00)
- Default engine (no API key): deterministic extractive summarizer (`groshik-extractive-v1`) — same
  input → same output, only words from the input (never adds a figure or fact). It keeps several key
  sentences when `max_words` allows, so the main facts survive, and compresses a long sentence by
  whole clauses: the main clause stays (subject, verb, core object) and trailing phrases are dropped
  whole. No ellipsis, no cut inside a phrase, no dangling last word, no bare-verb opening, never the
  input unchanged.
- Most reliable mode: set `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` and `LLM_MODE=always`. The same
  guards stay on — invented numbers, a copied opening, and a cut-off answer are rejected and the
  extractive result is used. `LLM_MODE=hard` (the process default) sends the model only a single
  sentence or anything the extractive engine had to compress. If a model returns HTTP 400 because
  `temperature` is deprecated or unsupported, that request is retried once without the parameter and
  later calls to the same model omit it. Any LLM failure is logged as a warning with the status code
  and a short error (no keys), then extractive is used.
- Routes: `GET /health`, `POST /the-line/ping`, `POST /the-line/quote`, `POST /the-line/dispatch-trusted`

---

## Русский

### Установка
```bash
sudo useradd --system --home /opt/groshik-agent --shell /usr/sbin/nologin groshik-agent
sudo mkdir -p /opt/groshik-agent && sudo chown groshik-agent: /opt/groshik-agent
# скопировать файлы репозитория в /opt/groshik-agent, затем:
cd /opt/groshik-agent
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # Python 3.10+
cp .env.example agent.env && chmod 600 agent.env                       # вписать значения
```
Обязательно для регистрации: `AGENT_OWNER_EMAIL`. Остальное имеет значения по умолчанию
(`HOST=127.0.0.1`, `PORT=8001`, `AGENT_ENDPOINT_URL=https://agent.the1ine.com`).
Ключи кошелька и подписи создаются при регистрации в `THE_LINE_KEYDIR` (по умолчанию в unit-файле
`/var/lib/groshik-agent`, права 600) и никогда не покидают сервер. Не коммитьте `agent.env` и этот каталог.

Работающий агент: `agent_1792942902303_c37c32`, адрес <https://agent.the1ine.com>.
Надёжнее всего задать ключ LLM и `LLM_MODE=always` (проверка чисел и дословного начала остаётся).
Без ключа выжимка детерминированная: несколько ключевых предложений, если хватает `max_words`, и
сжатие длинного предложения по целым частям — главная клауза сохраняется, хвост отбрасывается целиком.
`LLM_MODE=hard` (значение по умолчанию в коде) отправляет в модель только одно предложение или текст,
который пришлось сжимать. Если модель отвечает HTTP 400 из-за `temperature`, запрос повторяется один
раз без этого параметра.

### Запуск
```bash
python main.py            # слушает 127.0.0.1:8001 (HOST/PORT меняются через env)
```
systemd: `deploy/groshik-agent.service` → `/etc/systemd/system/`, затем
`sudo systemctl daemon-reload && sudo systemctl enable --now groshik-agent`. Логи — JSON, `journalctl -u groshik-agent`.

### Регистрация (один раз, отдельным шагом, после деплоя)
1. Сервис запущен, `curl https://agent.the1ine.com/health` отвечает `"status":"online"`.
2. `sudo -u groshik-agent env THE_LINE_KEYDIR=/var/lib/groshik-agent .venv/bin/python main.py register`
   — регистрирует агента, сохраняет `registration.json` (600) в `THE_LINE_KEYDIR`, ждёт проверку
   эталонной задачей и печатает итог. Секреты в лог не выводятся. Перезапуск сервиса не нужен —
   он подхватывает файл сам. Повторный запуск ничего не делает (`--force` — перерегистрация).
3. `python main.py status` — статус; `python main.py verify` — повторить проверку.

### Тесты
```bash
pip install -r requirements-dev.txt && python -m pytest -q
```

---

## English

### Install
```bash
sudo useradd --system --home /opt/groshik-agent --shell /usr/sbin/nologin groshik-agent
cd /opt/groshik-agent           # repository files here, owned by groshik-agent
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # Python 3.10+
cp .env.example agent.env && chmod 600 agent.env                       # fill in values
```
Required for registration: `AGENT_OWNER_EMAIL`. Everything else has defaults. Wallet and signing keys
are generated at registration in `THE_LINE_KEYDIR` (mode 600) and never leave the host. Never commit
`agent.env` or the key directory.

### Run
```bash
python main.py            # binds 127.0.0.1:8001 (override with HOST / PORT)
```
systemd unit: `deploy/groshik-agent.service`. Logs are one JSON object per line.

### Register (once, a separate deliberate step, after deploy)
With the service running and `https://agent.the1ine.com/health` answering:
```bash
sudo -u groshik-agent env THE_LINE_KEYDIR=/var/lib/groshik-agent .venv/bin/python main.py register
```
It registers with `endpoint_url=https://agent.the1ine.com`, stores `registration.json` (mode 600) in
`THE_LINE_KEYDIR`, triggers the protocol's reference task and reports whether the agent is verified.
The running service picks the new state up without a restart. Re-running is a no-op unless `--force`.
`python main.py status` / `python main.py verify` show status / re-run verification.

### Environment variables
| Name | Required | Purpose |
|---|---|---|
| `AGENT_OWNER_EMAIL` | for `register` | owner contact sent at registration |
| `THE_LINE_KEYDIR` | recommended | dir for wallet/signing keys + `registration.json` (default `./state`) |
| `THE_LINE_URL` | no | protocol API, default `https://api.the1ine.com` |
| `AGENT_ENDPOINT_URL` | no | public URL, default `https://agent.the1ine.com` |
| `HOST` / `PORT` | no | bind address, default `127.0.0.1` / `8001` |
| `AGENT_PRICE_USDC` | no | price per task, default `0.05` |
| `HEARTBEAT_INTERVAL_S` | no | default `30` (`0` disables) |
| `LOG_LEVEL` | no | default `INFO` |
| `LLM_PROVIDER` | no | `auto` (default) / `openai` / `anthropic` / `none` |
| `LLM_MODE` | no | recommended `always` when a key is set; code default `hard` (LLM only for single-sentence / compression cases) |
| `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_BASE_URL` | no | optional OpenAI(-compatible) backend |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | no | optional Anthropic backend |
| `LLM_TIMEOUT_S` | no | default `20`; on timeout/error → extractive |
| `AGENT_WALLET_PK`, `AGENT_SIGNING_PRIV` | no | supply keys via env instead of key files |
| `THE_LINE_AGENT_ID`, `THE_LINE_API_KEY`, `THE_LINE_TRUSTED_SECRET` | no | override `registration.json` |
| `AGENT_ENV_FILE` | no | path of the env file `main.py` reads (default `./agent.env`) |

### Tests
```bash
pip install -r requirements-dev.txt && python -m pytest -q
```
