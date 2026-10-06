"""Explicit deal state machine. State is never decided by an LLM."""
from __future__ import annotations

from src.models.enums import DealState as S

PIPELINE_ORDER = [
    S.DISCOVERED, S.RESEARCHING, S.QUALIFIED, S.PAIN_IDENTIFIED, S.SOLUTION_READY, S.OUTREACH_SENT,
    S.ENGAGED, S.DISCOVERY, S.PROPOSAL, S.NEGOTIATION, S.ACCEPTED, S.PAYMENT_PENDING, S.PAID,
    S.DELIVERY, S.COMPLETED,
]

TRANSITIONS: dict[S, set[S]] = {
    S.DISCOVERED: {S.RESEARCHING},
    S.RESEARCHING: {S.QUALIFIED, S.DISQUALIFIED},
    S.QUALIFIED: {S.PAIN_IDENTIFIED, S.DISQUALIFIED},
    S.PAIN_IDENTIFIED: {S.SOLUTION_READY, S.DISQUALIFIED},
    S.SOLUTION_READY: {S.OUTREACH_SENT},
    S.OUTREACH_SENT: {S.ENGAGED, S.LOST},
    S.ENGAGED: {S.DISCOVERY, S.PROPOSAL, S.NEGOTIATION, S.LOST},
    S.DISCOVERY: {S.PROPOSAL, S.NEGOTIATION, S.LOST},
    S.PROPOSAL: {S.NEGOTIATION, S.ACCEPTED, S.LOST},
    S.NEGOTIATION: {S.PROPOSAL, S.ACCEPTED, S.LOST},
    S.ACCEPTED: {S.PAYMENT_PENDING, S.LOST},
    S.PAYMENT_PENDING: {S.PAID, S.LOST},
    S.PAID: {S.DELIVERY},
    S.DELIVERY: {S.COMPLETED},
    S.COMPLETED: set(),
    S.DISQUALIFIED: set(),
    S.LOST: set(),
    S.DO_NOT_CONTACT: set(),
    S.ESCALATED: set(),  # resolved through resume_state / explicit human decision (see below)
    S.FAILED: set(),
}

# States from which a human can be asked to step in, and from which we can be told to stop contacting.
ESCALATABLE = {
    S.ENGAGED, S.DISCOVERY, S.PROPOSAL, S.NEGOTIATION, S.ACCEPTED, S.PAYMENT_PENDING, S.OUTREACH_SENT,
    S.PAID, S.DELIVERY,
}
CONTACT_STATES = {
    S.DISCOVERED, S.RESEARCHING, S.QUALIFIED, S.PAIN_IDENTIFIED, S.SOLUTION_READY, S.OUTREACH_SENT,
    S.ENGAGED, S.DISCOVERY, S.PROPOSAL, S.NEGOTIATION, S.ACCEPTED, S.PAYMENT_PENDING, S.ESCALATED, S.FAILED,
}
FAILABLE = {S.DISCOVERED, S.RESEARCHING, S.QUALIFIED, S.PAIN_IDENTIFIED, S.SOLUTION_READY, S.PAID, S.DELIVERY}
TERMINAL = {S.COMPLETED, S.DISQUALIFIED, S.LOST, S.DO_NOT_CONTACT}


class InvalidTransition(Exception):
    pass


def can_transition(frm: S, to: S, resume_state: S | None = None) -> bool:
    if frm == to:
        return False
    if to == S.DO_NOT_CONTACT:
        return frm in CONTACT_STATES
    if to == S.ESCALATED:
        return frm in ESCALATABLE
    if to == S.FAILED:
        return frm in FAILABLE
    if frm in (S.ESCALATED, S.FAILED):
        return to == resume_state or (frm == S.ESCALATED and to in (S.LOST, S.DISQUALIFIED))
    return to in TRANSITIONS.get(frm, set())
