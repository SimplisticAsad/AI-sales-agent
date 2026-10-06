# Autonomous AI Sales Agent

A modular, stateful sales pipeline: **find prospect → research → identify pain → design solution → price → contact →
sell → negotiate → proposal → one-time payment → verified PAID → delivery handoff.**

It is *not* one giant agent. Ten small agents (each: one prompt in `/prompts`, one schema, deterministic
post-processing) are orchestrated by an explicit, resumable state machine. **LLMs propose; deterministic code
disposes** - prices, discounts, payment terms, qualification, evidence status, compliance and "paid" are never
decided by a model.

> Status: Phases 1-3 are implemented and tested end-to-end against mocks (107 tests). Groq, Retell, Chatterbox,
> SMTP and Stripe adapters are written but **have not been run against the live services** (no credentials in
> the build environment) - see [What is verified](#what-is-verified-and-what-is-not).

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python -m src.cli demo            # whole lifecycle offline: mock LLM, in-memory DB, no keys needed
pytest -q                         # 107 tests
```

Real run:

```bash
cp .env.example .env              # set ADMIN_API_KEY, GROQ_API_KEY, GROQ_MODEL, SENDER_* ...
python -m src.cli serve           # or: uvicorn src.api.app:app ;  docker compose up  (Postgres)
curl -H "X-API-Key: $ADMIN_API_KEY" localhost:8000/health
```

Safe defaults: `OUTREACH_DRY_RUN=true`, `RETELL_DRY_RUN=true`, `AUTO_OUTREACH=false`, `CALL_REQUIRE_CONSENT=true`,
`PAYMENT_PROVIDER=mock`. Nothing is emailed or dialled until you flip them.

## Architecture

```
src/
  agents/        market_selector, prospect_hunter, researcher, pain_detector, solution_architect,
                 pricing, outreach, sales, negotiation, proposal   (BaseAgent: trace + ask)
  services/      llm (retry/JSON repair/cost) · prompts (PromptManager) · tts · state_machine · deals
                 pricing · negotiation · guardrails · compliance · evidence · qualification · roi
                 proposals · payments · delivery · outreach · calls · escalation · observability
  workflows/     pipeline.py (resumable steps) · conversation.py (every inbound message, any channel)
  tools/         registry.py (tools the Sales Agent may call; each re-checks its own preconditions)
  integrations/  groq · retell (client + controller) · chatterbox · payments (mock, stripe) · outreach
                 research (SSRF-safe fetcher) · prospects (CSV/static) · delivery · mock (LLM, TTS)
  database/      SQLAlchemy models (SQLite default, Postgres via DATABASE_URL)
  api/           FastAPI: admin (API key), public (health, token-guarded proposal), webhooks, Retell WebSocket
  config/        settings (env), capabilities.json (verified integrations), proposal_terms.json (legal template)
prompts/<agent>/system.md   all system prompts, Git-versioned, `{{variable}}` injection
```
Full proposal, schema and state machine: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
Operations/compliance: [docs/OPERATIONS.md](docs/OPERATIONS.md).

### State machine
`DISCOVERED → RESEARCHING → QUALIFIED → PAIN_IDENTIFIED → SOLUTION_READY → OUTREACH_SENT → ENGAGED → DISCOVERY →
PROPOSAL ⇄ NEGOTIATION → ACCEPTED → PAYMENT_PENDING → PAID → DELIVERY → COMPLETED`, plus `DISQUALIFIED`, `LOST`,
`DO_NOT_CONTACT`, `ESCALATED` (human review; resumes to `resume_state`), `FAILED` (sticky until `/pipeline/{id}/retry`).
Every transition is validated and logged to `deal_events`. `PAID` is reachable only from `PAYMENT_PENDING`, only by
the verified payment webhook.

### The guardrails that matter
| Risk | Control (in code, not prompt) |
|---|---|
| Discounting below the minimum | `PricingPolicy` floor = `max(DEFAULT_MIN_PRICE, target·(1-maxDiscount))`; `NegotiationEngine` ladder never goes below it; persistent below-floor demand → human |
| LLM states a rogue price / promise | `ReplyGuard`: every `$` amount must be in the authorised set; "guarantee/refund/human/testimonial" blocked; one rewrite then safe fallback + escalation |
| Out-of-policy asks (refund, legal, net-30, extra scope, unverified integration) | engine + regex backstop → `ESCALATED`, agent says "I'll confirm with a teammate" |
| Fabricated research | a `FACT` must carry a quote found verbatim in the fetched page, else downgraded to `INFERENCE` |
| Fabricated contacts | contact data comes only from the source record; LLM can't supply it |
| Unverified capabilities | `capabilities.json` allow-list; others flagged "needs validation", priced higher, never in deliverables |
| Invented legal terms | proposal terms come from `proposal_terms.json`; narrative with legal language falls back to a deterministic version |
| Fake payment | status only from signature-verified webhook **and** provider API re-check **and** amount/currency match; idempotent on event id |
| Spam / opt-out | suppression list, opt-out detection ends the thread, CAN-SPAM footer appended by code, contact must have a recorded source |
| Pretending to be human | a direct "are you an AI?" is answered truthfully by code regardless of LLM output |

## API (admin endpoints need `X-API-Key` or `Authorization: Bearer`)

| | |
|---|---|
| `POST /markets` `GET /markets` | Market Selector → ICP |
| `POST /prospects` · `POST /prospects/bulk` · `GET /prospects[/:id]` | add/import (Prospect Hunter ingest) |
| `POST /research/:id` · `POST /qualify/:id[?run_on=true]` | research; qualify (and continue to solution) |
| `POST /pipeline/:id/advance|run|retry` | run next step / until blocked / retry a FAILED deal |
| `POST /outreach/:id` | personalised first touch (dry-run by default) |
| `POST /calls/:id` | outbound Retell call (consent + calling-window gated) |
| `POST /conversations/:id/messages` | inbound message from any channel (email reply, web chat, simulation) |
| `GET /deals` · `GET /deals/:id` · `GET /deals/:id/history` | full audit: events, messages, negotiations, agent runs, decisions |
| `POST /proposals/:id` · `GET /proposals/:id/public?token=` · `POST /proposals/:id/accept` | proposal; client view/accept via secret token |
| `POST /payments/:id` | (idempotent) one-time payment request |
| `POST /webhooks/payment` · `POST /webhooks/retell` | signature-verified webhooks |
| `WS /retell/llm/:call_id` | Retell custom-LLM socket |
| `GET /escalations` · `POST /escalations/:id/resolve` | human-in-the-loop queue |
| `POST /deals/:id/handoff` · `POST /deals/:id/complete` | delivery handoff retry / completion |
| `GET /agent-runs` · `POST /tts/preview` · `GET /health` | observability, TTS check, liveness |

`:id` accepts a prospect id or a deal id.

## Answering "why did it…?"
`GET /deals/:id/history` returns, in one document: the ICP score breakdown (why it was contacted), each
`AgentRun` with `decision_summary`, prompt name+version hash, tool calls, tokens/cost/latency; the pricing
`breakdown` (why $X); each `Negotiation` round with reason codes; `Escalation`s with reason codes; and the ordered
state-change log. Secrets, emails and phone numbers are redacted from run logs.

## Voice: Retell + Chatterbox
Retell owns the phone number, telephony, STT and (in custom-LLM mode) the TTS of live calls; our WebSocket
(`/retell/llm/:call_id`) only exchanges text with `ConversationWorkflow`. **Chatterbox cannot replace Retell's live
voice**; it is wired behind `TTSService` and used for audio we produce ourselves (opening-line audio/voicemail
assets, previews) and is ready if telephony is ever swapped for something that accepts our audio. Details in
[docs/OPERATIONS.md](docs/OPERATIONS.md).

## What is verified and what is not
**Verified by tests (107):** all deterministic policy, state machine, prompts, agents with a deterministic mock
LLM, adversarial LLM outputs, compliance, webhooks (signature/duplicate/mismatch/outage), resumability, delivery
handoff, Groq/Stripe/Chatterbox-HTTP request handling against `httpx.MockTransport`. Six safety invariants were
mutation-checked (breaking them fails tests).

**Not verified (needs your credentials/infrastructure):** real Groq output quality (the mock returns schema-valid
JSON; real models will need prompt tuning, and the JSON-repair/guard paths will see real traffic), real Retell
signature scheme and WebSocket protocol (implemented from documentation recall - the code fails closed, but
check it against Retell's current docs), Stripe live mode, SMTP deliverability, local Chatterbox model loading,
Postgres (SQLite is what the tests use).

**Not built:** dashboard UI, a durable retry queue/worker (steps are resumable and `FAILED` is retryable via API,
but nothing schedules retries), inbound-email parsing, a licensed prospect-data connector (only CSV/static
sources ship - deliberately no scraper), a real downstream orchestrator (file outbox + HTTP target only), DB
migrations (tables are created with `create_all`; add Alembic before schema changes).

## Before going live
1. Edit `src/config/capabilities.json` to list **only** integrations you have actually built and tested.
2. Have counsel review `src/config/proposal_terms.json` and the outreach footer.
3. Read [docs/OPERATIONS.md](docs/OPERATIONS.md) on TCPA/CAN-SPAM; keep dry-run on until a human reviews samples.
