"""Unauthenticated endpoints: health, and the token-protected proposal view/accept used by the prospect."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.api.deps import DB, SVC, is_admin
from src.database.models import Proposal
from src.services.container import Services
from src.services.proposals import ProposalService
from src.workflows.pipeline import Pipeline

router = APIRouter()


@router.get("/health")
def health(request: Request):
    return {"status": "ok", "db": "up", "llm_provider": request.app.state.settings.llm_provider,
            "payment_provider": request.app.state.settings.payment_provider}


@router.get("/proposals/{proposal_id}/public")
def view_proposal(proposal_id: str, token: str = Query(...), db: Session = DB, svc: Services = SVC):
    p = db.get(Proposal, proposal_id)
    if not p or not ProposalService(db, svc.settings).token_ok(p, token):
        raise PermissionError("invalid proposal link")
    return {"id": p.id, "version": p.version, "status": p.status, "content": p.content}


class AcceptBody(BaseModel):
    token: str | None = None


@router.post("/proposals/{proposal_id}/accept")
def accept_proposal(proposal_id: str, body: AcceptBody, admin: bool = Depends(is_admin), db: Session = DB, svc: Services = SVC):
    """Client accepts with the secret link token (or an admin accepts on their behalf). Creates the payment request."""
    return Pipeline(db, svc).accept_proposal(proposal_id, token=body.token, admin=admin)
