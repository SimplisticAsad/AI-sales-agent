from __future__ import annotations

from enum import Enum


class DealState(str, Enum):
    DISCOVERED = "DISCOVERED"
    RESEARCHING = "RESEARCHING"
    QUALIFIED = "QUALIFIED"
    PAIN_IDENTIFIED = "PAIN_IDENTIFIED"
    SOLUTION_READY = "SOLUTION_READY"
    OUTREACH_SENT = "OUTREACH_SENT"
    ENGAGED = "ENGAGED"
    DISCOVERY = "DISCOVERY"
    PROPOSAL = "PROPOSAL"
    NEGOTIATION = "NEGOTIATION"
    ACCEPTED = "ACCEPTED"
    PAYMENT_PENDING = "PAYMENT_PENDING"
    PAID = "PAID"
    DELIVERY = "DELIVERY"
    COMPLETED = "COMPLETED"
    # side states
    DISQUALIFIED = "DISQUALIFIED"
    LOST = "LOST"
    DO_NOT_CONTACT = "DO_NOT_CONTACT"
    ESCALATED = "ESCALATED"
    FAILED = "FAILED"


class ClaimLabel(str, Enum):
    FACT = "FACT"
    INFERENCE = "INFERENCE"
    UNKNOWN = "UNKNOWN"


class Intent(str, Enum):
    QUESTION = "question"
    OBJECTION = "objection"
    INTEREST = "interest"
    PRICE_INQUIRY = "price_inquiry"
    COUNTER_OFFER = "counter_offer"
    ACCEPT = "accept"
    REJECT = "reject"
    OPT_OUT = "opt_out"
    REQUEST_HUMAN = "request_human"
    OTHER = "other"


class PaymentStatus(str, Enum):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    EXPIRED = "expired"
    MISMATCH = "mismatch"


class ProposalStatus(str, Enum):
    DRAFT = "draft"
    SENT = "sent"
    ACCEPTED = "accepted"
    SUPERSEDED = "superseded"
    VOID = "void"


class EscalationStatus(str, Enum):
    OPEN = "open"
    APPROVED = "approved"
    REJECTED = "rejected"
    RESOLVED = "resolved"
