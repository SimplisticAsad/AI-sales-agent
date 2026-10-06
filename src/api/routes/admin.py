"""Authenticated admin API (X-API-Key). `id` is a prospect id for prospect-lifecycle endpoints and a deal id for
commercial endpoints; both are accepted everywhere via resolve_deal()."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.agents.market_selector.agent import MarketSelectorAgent
from src.agents.proposal.agent import ProposalAgent
from src.agents.prospect_hunter.agent import ProspectHunterAgent
from src.api.deps import DB, SVC
from src.api.serialize import row, rows
from src.database.models import (
    AgentRun, Company, Contact, Conversation, Deal, DealEvent, DeliveryProject, Escalation, MarketProfile, Message,
    Negotiation, PainPoint, Payment, Proposal, Prospect, Research, Solution, Offer,
)
from src.models.enums import DealState as S
from src.models.schemas import RawCandidate
from src.services import deals as D
from src.services.calls import CallService
from src.services.container import Services
from src.services.escalation import resolve_escalation
from src.services.outreach import OutreachBlocked, OutreachService
from src.services.payments import PaymentService
from src.services.pricing import usd
from src.services.proposals import ProposalService
from src.services.queries import (
    latest_offer, latest_proposal, latest_research, latest_solution, primary_contact,
)
from src.services.tts import VoiceSettings
from src.workflows.conversation import ConversationWorkflow
from src.workflows.pipeline import Pipeline

router = APIRouter()


def resolve_deal(s: Session, any_id: str) -> Deal:
    d = s.get(Deal, any_id)
    return d if d else D.deal_for_prospect(s, any_id)


# ------------------------------------------------------------------ markets
class MarketIn(BaseModel):
    business_goal: str = Field(min_length=5)
    constraints: str = ""


@router.post("/markets", status_code=201)
def create_market(body: MarketIn, db: Session = DB, svc: Services = SVC):
    return row(MarketSelectorAgent(db, svc).run(body.business_goal, body.constraints))


@router.get("/markets")
def list_markets(db: Session = DB):
    return rows(db.scalars(select(MarketProfile).order_by(MarketProfile.created_at.desc())))


# ------------------------------------------------------------------ prospects
class ProspectIn(RawCandidate):
    market_profile_id: str | None = None


@router.post("/prospects", status_code=201)
def add_prospect(body: ProspectIn, db: Session = DB, svc: Services = SVC):
    market = db.get(MarketProfile, body.market_profile_id) if body.market_profile_id else \
        db.scalar(select(MarketProfile).order_by(MarketProfile.created_at.desc()))
    raw = RawCandidate(**body.model_dump(exclude={"market_profile_id"}))
    p = ProspectHunterAgent(db, svc).ingest(raw, market)
    return prospect_view(db, p)


class BulkIn(BaseModel):
    market_profile_id: str | None = None
    candidates: list[RawCandidate] = Field(max_length=200)


@router.post("/prospects/bulk", status_code=201)
def import_prospects(body: BulkIn, db: Session = DB, svc: Services = SVC):
    market = db.get(MarketProfile, body.market_profile_id) if body.market_profile_id else None
    agent = ProspectHunterAgent(db, svc)
    ids = []
    for c in body.candidates:
        ids.append(agent.ingest(c, market).id)
        db.commit()
    return {"imported": len(ids), "prospect_ids": ids}


def prospect_view(db: Session, p: Prospect) -> dict:
    company = db.get(Company, p.company_id)
    deal = D.deal_for_prospect(db, p.id)
    r = latest_research(db, p.id)
    sol = latest_solution(db, p.id)
    offer = latest_offer(db, deal.id)
    return {
        "prospect": row(p), "company": row(company),
        "contacts": rows(db.scalars(select(Contact).where(Contact.company_id == company.id))),
        "deal": row(deal), "research": row(r) if r else None,
        "pain_points": rows(db.scalars(select(PainPoint).where(PainPoint.prospect_id == p.id).order_by(PainPoint.priority_score.desc()))),
        "solution": row(sol) if sol else None,
        "offer": ({**row(offer), "min_price_cents": "hidden"} if offer else None),  # the floor is never exposed via prospect views
    }


@router.get("/prospects")
def list_prospects(db: Session = DB, limit: int = 50):
    return [{"id": p.id, "company": db.get(Company, p.company_id).name, "score": p.qualification_score,
             "state": D.deal_for_prospect(db, p.id).state}
            for p in db.scalars(select(Prospect).order_by(Prospect.created_at.desc()).limit(min(limit, 200)))]


@router.get("/prospects/{id}")
def get_prospect(id: str, db: Session = DB):
    p = db.get(Prospect, id)
    if not p:
        raise LookupError("prospect not found")
    return prospect_view(db, p)


# ------------------------------------------------------------------ pipeline steps
@router.post("/research/{id}")
def run_research(id: str, db: Session = DB, svc: Services = SVC):
    deal = resolve_deal(db, id)
    r = Pipeline(db, svc).research(deal)
    return {"deal_state": deal.state, "research": row(r)}


@router.post("/qualify/{id}")
def run_qualify(id: str, run_on: bool = False, db: Session = DB, svc: Services = SVC):
    deal = resolve_deal(db, id)
    pipe = Pipeline(db, svc)
    if S(deal.state) == S.DISCOVERED:
        pipe.research(deal)
    ok = pipe.qualify(deal)
    out = {"qualified": ok, "deal_state": deal.state, "qualification": db.get(Prospect, deal.prospect_id).qualification}
    db.commit()
    if ok and run_on:
        out["steps"] = [r.__dict__ for r in pipe.run_until_blocked(deal)]
        out["deal_state"] = D.get_deal(db, deal.id).state
    return out


@router.post("/pipeline/{id}/advance")
def advance(id: str, db: Session = DB, svc: Services = SVC):
    return Pipeline(db, svc).advance(resolve_deal(db, id)).__dict__


@router.post("/pipeline/{id}/run")
def run(id: str, db: Session = DB, svc: Services = SVC):
    return [r.__dict__ for r in Pipeline(db, svc).run_until_blocked(resolve_deal(db, id))]


@router.post("/pipeline/{id}/retry")
def retry(id: str, db: Session = DB, svc: Services = SVC):
    return Pipeline(db, svc).retry(resolve_deal(db, id)).__dict__


@router.post("/outreach/{id}")
def outreach(id: str, db: Session = DB, svc: Services = SVC):
    deal = resolve_deal(db, id)
    try:
        msg = OutreachService(db, svc).send_initial(deal)
    except OutreachBlocked:
        db.commit()  # keep the compliance event, then surface the block
        raise
    return {"deal_state": deal.state, "message": row(msg), "dry_run": svc.settings.outreach_dry_run}




@router.post("/calls/{id}")
def start_call(id: str, db: Session = DB, svc: Services = SVC):
    deal = resolve_deal(db, id)
    try:
        return CallService(db, svc).start_outbound(deal)
    except Exception:
        db.commit()  # keep the compliance/failure event, then surface the error
        raise


# ------------------------------------------------------------------ conversations (simulation / web / email replies)
class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    channel: str = "email"


@router.post("/conversations/{id}/messages")
def inbound_message(id: str, body: MessageIn, db: Session = DB, svc: Services = SVC):
    deal = resolve_deal(db, id)
    res = ConversationWorkflow(db, svc).handle_inbound(deal.id, body.text, body.channel)
    return res.__dict__


# ------------------------------------------------------------------ deals
@router.get("/deals")
def list_deals(state: str | None = None, db: Session = DB, limit: int = 100):
    q = select(Deal).order_by(Deal.updated_at.desc()).limit(min(limit, 500))
    if state:
        q = q.where(Deal.state == state)
    out = []
    for d in db.scalars(q):
        p = db.get(Prospect, d.prospect_id)
        out.append({"id": d.id, "prospect_id": d.prospect_id, "company": db.get(Company, p.company_id).name, "state": d.state,
                    "quoted_price_cents": d.quoted_price_cents, "agreed_price_cents": d.agreed_price_cents,
                    "retry_count": d.retry_count, "last_error": d.last_error, "updated_at": d.updated_at.isoformat()})
    return out


@router.get("/deals/{id}")
def get_deal(id: str, db: Session = DB):
    d = resolve_deal(db, id)
    return {**row(d), "latest_proposal": row(latest_proposal(db, d.id), {"token_hash"}) if latest_proposal(db, d.id) else None}


@router.get("/deals/{id}/history")
def deal_history(id: str, db: Session = DB):
    """Complete, auditable history: why contacted, why this price, why rejected/escalated."""
    d = resolve_deal(db, id)
    p = db.get(Prospect, d.prospect_id)
    convs = list(db.scalars(select(Conversation).where(Conversation.deal_id == d.id).order_by(Conversation.created_at)))
    return {
        "deal": row(d), "prospect": prospect_view(db, p),
        "offer": row(latest_offer(db, d.id)) if latest_offer(db, d.id) else None,  # includes floor: admin-only
        "events": rows(db.scalars(select(DealEvent).where(DealEvent.deal_id == d.id).order_by(DealEvent.created_at, DealEvent.id))),
        "conversations": [{**row(c), "messages": rows(c.messages)} for c in convs],
        "negotiations": rows(db.scalars(select(Negotiation).where(Negotiation.deal_id == d.id).order_by(Negotiation.round))),
        "proposals": rows(db.scalars(select(Proposal).where(Proposal.deal_id == d.id).order_by(Proposal.version))),
        "payments": rows(db.scalars(select(Payment).where(Payment.deal_id == d.id))),
        "delivery": rows(db.scalars(select(DeliveryProject).where(DeliveryProject.deal_id == d.id))),
        "escalations": rows(db.scalars(select(Escalation).where(Escalation.deal_id == d.id).order_by(Escalation.created_at))),
        "agent_runs": rows(db.scalars(select(AgentRun).where((AgentRun.deal_id == d.id) | (AgentRun.prospect_id == d.prospect_id))
                                      .order_by(AgentRun.started_at))),
    }


# ------------------------------------------------------------------ proposal / payment / delivery
class ProposalIn(BaseModel):
    agreed_price_usd: float | None = None


@router.post("/proposals/{id}", status_code=201)
def create_proposal(id: str, body: ProposalIn | None = None, db: Session = DB, svc: Services = SVC):
    """Manually (re)generate a proposal. The price must be inside the authorised range or a human override."""
    deal = resolve_deal(db, id)
    price = usd(body.agreed_price_usd) if body and body.agreed_price_usd else (deal.agreed_price_cents or deal.quoted_price_cents)
    if not price:
        raise HTTPException(422, "no price available")
    if S(deal.state) in (S.ENGAGED, S.DISCOVERY, S.NEGOTIATION):
        D.transition(db, deal, S.PROPOSAL, actor="admin", reason="proposal generated manually")
    company = db.get(Company, db.get(Prospect, deal.prospect_id).company_id)
    narrative = ProposalAgent(db, svc).run(deal, company, price)
    ps = ProposalService(db, svc.settings)
    prop, token = ps.generate(deal, narrative, price)
    return {"proposal": row(prop, {"token_hash"}), "acceptance_url": ps.public_url(prop, token)}


@router.post("/payments/{id}", status_code=201)
def create_payment(id: str, db: Session = DB, svc: Services = SVC):
    deal = resolve_deal(db, id)
    pay = PaymentService(db, svc.payment_provider, svc.settings.payment_currency).create_for_deal(deal)
    return row(pay)


@router.post("/deals/{id}/handoff")
def handoff(id: str, db: Session = DB, svc: Services = SVC):
    deal = resolve_deal(db, id)
    if S(deal.state) not in (S.PAID, S.DELIVERY):
        raise HTTPException(409, "deal is not PAID")
    proj = Pipeline(db, svc).handoff(deal)
    return row(proj)


@router.post("/deals/{id}/complete")
def complete(id: str, db: Session = DB):
    deal = resolve_deal(db, id)
    D.transition(db, deal, S.COMPLETED, actor="delivery", reason="delivery completed")
    proj = db.scalar(select(DeliveryProject).where(DeliveryProject.deal_id == deal.id))
    if proj:
        proj.status = "completed"
    return row(deal)


# ------------------------------------------------------------------ escalations / observability
@router.get("/escalations")
def list_escalations(status: str | None = "open", db: Session = DB):
    q = select(Escalation).order_by(Escalation.created_at.desc())
    if status:
        q = q.where(Escalation.status == status)
    return rows(db.scalars(q))


class ResolveIn(BaseModel):
    approved: bool
    note: str = ""
    approved_price_usd: float | None = None
    mark_lost: bool = False


@router.post("/escalations/{id}/resolve")
def resolve(id: str, body: ResolveIn, db: Session = DB):
    return row(resolve_escalation(db, id, approved=body.approved, note=body.note,
                                  approved_price_usd=body.approved_price_usd, mark_lost=body.mark_lost))


@router.get("/agent-runs")
def agent_runs(deal_id: str | None = None, prospect_id: str | None = None, agent: str | None = None,
               limit: int = 100, db: Session = DB):
    q = select(AgentRun).order_by(AgentRun.started_at.desc()).limit(min(limit, 500))
    if deal_id:
        q = q.where(AgentRun.deal_id == deal_id)
    if prospect_id:
        q = q.where(AgentRun.prospect_id == prospect_id)
    if agent:
        q = q.where(AgentRun.agent == agent)
    return rows(db.scalars(q))


class TTSIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    key: str = "preview"
    exaggeration: float | None = None
    cfg_weight: float | None = None
    voice_id: str | None = None


@router.post("/tts/preview")
def tts_preview(body: TTSIn, svc: Services = SVC):
    v = VoiceSettings(voice_id=body.voice_id, exaggeration=body.exaggeration if body.exaggeration is not None else svc.tts.default_voice.exaggeration,
                      cfg_weight=body.cfg_weight if body.cfg_weight is not None else svc.tts.default_voice.cfg_weight)
    a = svc.tts.try_synthesize(body.text, key=body.key, voice=v)
    if not a:
        raise HTTPException(503, "TTS unavailable")
    return {"path": str(a.path), "sample_rate": a.sample_rate, "duration_s": a.duration_s, "provider": a.provider}
