# Operations & Compliance Notes

*Engineering notes, not legal advice. Have counsel review before sending real outreach or making calls.*

## Rollout order
1. `python -m src.cli demo` and `pytest` (no keys).
2. Set `GROQ_API_KEY`; keep `OUTREACH_DRY_RUN=true`. Run a market + a few prospects through `/pipeline/:id/run` and
   read `GET /deals/:id/history`. Check dossiers for `FACT`s with real quotes and that pains are well supported.
   Tune `/prompts/*/system.md` (Git-versioned; each AgentRun records the prompt hash).
3. Configure `SENDER_*` (name, company, email, **postal address**) and SMTP. Review dry-run emails. Then set
   `OUTREACH_DRY_RUN=false`. Start with a handful of prospects.
4. Payments: `PAYMENT_PROVIDER=stripe` with test keys; point a Stripe webhook at `POST /webhooks/payment`
   (events: `checkout.session.completed`, `checkout.session.async_payment_succeeded|failed`, `checkout.session.expired`).
5. Retell: see below. Keep `RETELL_DRY_RUN=true` until the call flow is rehearsed.

## Retell setup
* Create a Retell agent using **Custom LLM** pointing at `wss://<host>/retell/llm` (Retell appends `/{call_id}`).
* Webhook URL: `https://<host>/webhooks/retell`; verified with `RETELL_API_KEY` (`x-retell-signature`).
* Outbound calls are created by `POST /calls/:id`; the call is registered in our DB first, so the WebSocket only
  serves calls we know about. Inbound callers are matched by phone to an existing contact; unknown callers are refused.
* Opening line (AI disclosure) is sent by our socket when `CALL_DISCLOSURE_MODE=always` (default).
  `if_asked` still answers truthfully when asked. There is intentionally no "never disclose" mode.
* The signature scheme and WebSocket message shapes in `src/integrations/retell` follow Retell's documented
  design as best known when written; **verify against current docs** before production.

## Chatterbox
* Local: `pip install ".[chatterbox]"`, set `CHATTERBOX_DEVICE` (cuda/cpu), optional `CHATTERBOX_VOICE_PROMPT_PATH`.
  The model loads lazily on first use (slow, GPU strongly recommended). Synthesis runs in a worker thread with a
  timeout and never blocks or fails the conversation (`try_synthesize` returns `None`).
* HTTP: run a Chatterbox server exposing an OpenAI-style `/v1/audio/speech`, set `CHATTERBOX_MODE=http`, `CHATTERBOX_URL`.
* Retell speaks live calls with its own voices. Chatterbox is used for audio *we* generate (see README).

## Compliance behaviour built in (and its limits)
| Area | What the code does | What it does **not** do |
|---|---|---|
| Email (CAN-SPAM) | needs sender identity + postal address; appends AI-disclosure + opt-out footer; `List-Unsubscribe`; only emails addresses with a recorded source; suppression list | does not verify your consent basis or non-US law (GDPR/CASL) |
| Opt-outs | phrase detection on every inbound message → `DO_NOT_CONTACT`, email+phone(+domain) suppression, no further replies | cannot see opt-outs sent through other channels |
| Phone (TCPA) | outbound calls only to contacts with `call_consent=true` (`CALL_REQUIRE_CONSENT`), weekday window in the company's timezone, suppression check, AI disclosure | consent must be recorded by a human with a real basis; the FCC treats AI-generated voices as "artificial" voices - **cold AI calls to mobile numbers without prior consent are likely unlawful**; check state recording-consent laws too |
| Website forms | creates a *manual task* for a human; never auto-submits | |
| Scraping | the research fetcher honours robots.txt, identifies itself, blocks private/loopback IPs (SSRF), size/time limits; no directory scraper ships | |
| Honesty | direct "are you an AI?" always answered truthfully; fake testimonials/clients/guarantees blocked | |

## Human-in-the-loop
Escalation reason codes: `PRICE_BELOW_MINIMUM`, `PAYMENT_TERMS_OUTSIDE_POLICY`, `SCOPE_OUTSIDE_APPROVED`,
`UNUSUAL_CONTRACT_TERMS`, `REFUND_REQUEST`, `GUARANTEE_REQUEST` (SLA/warranty language), `HUMAN_REQUESTED`,
`GUARDRAIL_VIOLATION`, `PAYMENT_VERIFICATION_FAILED`, `AGENT_REQUESTED_APPROVAL`, `UNEXPECTED_INBOUND`.
List with `GET /escalations`, resolve with `POST /escalations/:id/resolve`
(`approved_price_usd` records an explicit, audited human price exception; the AI itself can never go below the floor).
While a deal is `ESCALATED` the agent only sends a holding message.

## Money
All amounts are integer cents. Quote = target price. Floor never leaves the server (prospect views mask it; replies
are guard-checked). One-time upfront only; there is no subscription code path (Stripe `mode=payment`).

## Known sharp edges
* `Pipeline.advance` runs a step synchronously inside the request. Long LLM chains should move to a worker/queue (Phase 4).
* SQLite is the default and is fine for one process; use Postgres for anything concurrent (`docker compose up`).
* No migrations yet (`create_all`). Introduce Alembic before the first schema change.
* The mock LLM is keyword-based and only proves plumbing; judge real behaviour with real Groq traffic.
