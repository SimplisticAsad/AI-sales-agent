# Architecture Proposal

## 0. Repository inspection

The repository was empty (no commits, no code), so nothing is reused and nothing is rewritten. Existing
"orchestrator / frontend / backend / DB / QA agents" are **not** in this repo; the delivery side is therefore a
clean interface (`DeliveryTarget`) with a mock and an HTTP implementation, ready to be pointed at them.

## 1. Stack

| Concern | Choice | Why |
|---|---|---|
| Language / API | Python 3.11+, FastAPI | typed, async-capable, great webhook + WebSocket support (Retell custom-LLM) |
| Validation | Pydantic v2 | every agent output is schema-validated |
| DB | SQLAlchemy 2 (SQLite default, Postgres via `DATABASE_URL`) | proper relational state, resumable |
| HTTP | httpx | Groq / Retell / Stripe via thin clients (no vendor SDK lock-in) |
| Tests | pytest | |

Missing dependencies were installed: `fastapi sqlalchemy pydantic-settings httpx uvicorn websockets pytest`.
Optional (not installed by default): `chatterbox-tts` + `torch` for local TTS, `psycopg` for Postgres.

## 2. Layering

```
api/  ─▶  workflows/pipeline  ─▶  agents/*  ─▶  services/llm ─▶ integrations/groq
                │                    │                └────▶ services/prompts ─▶ /prompts/*/system.md
                │                    └─▶ tools/*  ─▶ deterministic services (pricing, negotiation, compliance,
                │                                      proposals, payments, delivery)
                └─▶ services/deals (state machine + event log)  ─▶ database/models
integrations/{retell,chatterbox,payments,outreach,research,prospects,delivery}: adapters behind interfaces
```

**Principle:** LLMs *propose*; deterministic code *disposes*. Prices, discounts, payment terms, state
transitions, paid-status, compliance and "is this claim a FACT" are never decided by an LLM.

## 3. Deterministic policy layer (the part the LLM cannot override)

* `PricingPolicy` – target / floor (`max(DEFAULT_MIN_PRICE, target·(1-maxDiscount))`) / max discount / one-time upfront.
* `NegotiationEngine` – concession ladder; counters never go below floor; out-of-policy requests
  (price < floor after holding, non one-time payment, scope outside the solution, contract terms, refunds,
  legal claims) → `escalate`.
* `ReplyGuard` – every outbound agent text is scanned: dollar amounts must be in the authorised set,
  no guarantees/refund promises/human impersonation. Violations → one regeneration, then safe fallback + escalation.
* `Evidence verifier` – a research claim stays `FACT` only if its quote is found in the fetched source text.
* `ComplianceService` – suppression list, opt-out detection, call consent/quiet hours, AI disclosure.
* Payment `PAID` only from a signature-verified provider webhook whose amount/currency match the stored Payment.

## 4. Database schema

`MarketProfile, Company, Contact, Prospect, Deal, Research, PainPoint, Solution, Offer, Conversation, Message,
Negotiation, Proposal, Payment, DeliveryProject, AgentRun` (as requested) plus operational tables
`DealEvent` (state history), `Escalation` (human-in-the-loop queue), `Suppression` (opt-outs),
`ProcessedWebhook` (idempotency). Money is stored as integer cents. See `src/database/models.py`.

## 5. Agent interface

```python
class BaseAgent:
    name: str; prompt_name: str
    def trace(prospect_id, deal_id, input_summary) -> AgentRun context   # latency, tokens, errors, tool calls
    def ask(trace, variables, user, output_model) -> validated pydantic   # PromptManager + LLMService
```
Each concrete agent adds a deterministic post-processor (validate evidence, compute score, clamp price, …).

## 6. State machine (`src/services/state_machine.py`)

```
DISCOVERED → RESEARCHING → QUALIFIED → PAIN_IDENTIFIED → SOLUTION_READY → OUTREACH_SENT → ENGAGED → DISCOVERY
   → PROPOSAL ⇄ NEGOTIATION → ACCEPTED → PAYMENT_PENDING → PAID → DELIVERY → COMPLETED
side states: DISQUALIFIED, LOST, DO_NOT_CONTACT, ESCALATED(resume_state), FAILED(resume_state, retry)
```
Transitions are validated against a table and recorded in `DealEvent`. The pipeline is re-entrant: each step
inspects `deal.state`, is idempotent, and commits, so a crash resumes from the last committed state.

## 7. Prompts

`/prompts/<agent>/system.md`, loaded by `PromptManager` (`{{variable}}` injection, strict about missing
variables, content hash recorded on every `AgentRun` as the prompt version). All ten prompts follow
ROLE / OBJECTIVE / AVAILABLE CONTEXT / TOOLS / RULES / PROHIBITED BEHAVIOR / DECISION PROCESS / OUTPUT FORMAT /
ESCALATION CONDITIONS. Untrusted text (web pages, prospect messages) is fenced in `<untrusted_data>`.

## 8. Voice note (Retell + Chatterbox)

Retell owns telephony, STT and – in its "custom LLM" mode – TTS for live calls. Our app is the "brain" behind
a Retell WebSocket (`/retell/llm/{call_id}`) and the Retell webhook. **Chatterbox cannot be injected into
Retell's live audio path**; it is wired behind `TTSService` and used wherever *we* produce audio
(voicemail/voice-note assets, demos, or if telephony is later swapped to one that accepts our audio).
This is called out so no one assumes live Retell calls are spoken by Chatterbox.

## 9. Phases

1. Prospect → research → pain → solution → pricing → sales simulation (mock LLM/sources) — **done**
2. Groq, Retell, Chatterbox, outreach adapters — **implemented behind interfaces; need your credentials to run live**
3. Proposal, payment (Stripe + mock), webhook, delivery handoff — **done**
4. Observability, escalations, history API — **done**; dashboard UI and a durable retry queue are **not** built.
