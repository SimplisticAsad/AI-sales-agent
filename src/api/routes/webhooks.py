"""Signature-verified provider webhooks. Raw bytes are verified before anything is parsed or trusted."""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from src.integrations.payments.base import InvalidSignature, PaymentError
from src.integrations.retell.controller import RetellCallController
from src.services.payments import PaymentService
from src.workflows.pipeline import Pipeline

router = APIRouter(prefix="/webhooks")


@router.post("/payment")
async def payment_webhook(request: Request):
    from src.api.deps import get_services

    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    svc = get_services(request)

    def work():
        s = request.app.state.session_factory()
        try:
            pipe = Pipeline(s, svc)
            res = PaymentService(s, svc.payment_provider, svc.settings.payment_currency).handle_webhook(body, headers, pipe.on_paid)
            s.commit()
            return res
        except Exception:
            s.rollback()  # un-record the event id so the provider's retry is processed
            raise
        finally:
            s.close()

    try:
        return await run_in_threadpool(work)
    except InvalidSignature:
        raise HTTPException(400, "invalid signature")
    except PaymentError as e:  # provider API unreachable/failing: non-2xx so the provider retries; event id not recorded
        raise HTTPException(503, f"payment verification unavailable: {e}")


@router.post("/retell")
async def retell_webhook(request: Request):
    from src.api.deps import get_services

    body = await request.body()
    svc = get_services(request)
    if svc.retell is None:
        raise HTTPException(503, "Retell is not configured")
    if not svc.retell.verify_signature(body, request.headers.get("x-retell-signature", "")):
        raise HTTPException(401, "invalid signature")
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(400, "invalid JSON")

    def work():
        s = request.app.state.session_factory()
        try:
            res = RetellCallController(s, svc).handle_webhook(payload)
            s.commit()
            return res
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    return await run_in_threadpool(work)
