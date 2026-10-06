"""Retell 'custom LLM' WebSocket: Retell streams STT transcripts to us and speaks whatever text we return.

Protocol summary (verify against Retell's current docs when going live):
  Retell -> us: {"interaction_type": "response_required"|"update_only"|"ping_pong"|"call_details", "response_id": n,
                 "transcript": [{"role": "agent"|"user", "content": "..."}]}
  us -> Retell: {"response_type": "response", "response_id": n, "content": "...", "content_complete": true, "end_call": false}
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool

from src.integrations.retell.controller import RetellCallController

router = APIRouter()
log = logging.getLogger("retell.ws")


@router.websocket("/retell/llm/{call_id}")
async def retell_llm(ws: WebSocket, call_id: str):
    from src.api.deps import get_services

    st = ws.app.state
    svc = st.services or st.build_services()
    st.services = svc

    def with_ctrl(fn):
        def run():
            s = st.session_factory()
            try:
                r = fn(RetellCallController(s, svc))
                s.commit()
                return r
            except Exception:
                s.rollback()
                raise
            finally:
                s.close()
        return run

    # only calls we know about (outbound registered by us, or inbound matched by the call_started webhook)
    if not await run_in_threadpool(with_ctrl(lambda c: c.known_call(call_id))):
        await ws.close(code=4403)
        return
    await ws.accept()
    opening = await run_in_threadpool(with_ctrl(lambda c: c.opening_line(call_id)))
    if opening:
        await ws.send_json({"response_type": "response", "response_id": 0, "content": opening.content, "content_complete": True})
    try:
        while True:
            msg = await ws.receive_json()
            kind = msg.get("interaction_type")
            if kind == "ping_pong":
                await ws.send_json({"response_type": "ping_pong", "timestamp": msg.get("timestamp")})
            elif kind == "response_required" or kind == "reminder_required":
                transcript = msg.get("transcript", [])
                rid = msg.get("response_id", 0)
                try:
                    reply = await run_in_threadpool(with_ctrl(lambda c: c.handle_turn(call_id, transcript)))
                    payload = {"content": reply.content, "end_call": reply.end_call}
                except Exception:  # never leave the caller in silence; fail safe and honest
                    log.exception("voice turn failed for %s", call_id)
                    payload = {"content": "I'm sorry, I'm having a technical problem. Someone from our team will follow up.", "end_call": True}
                await ws.send_json({"response_type": "response", "response_id": rid, "content_complete": True, **payload})
    except WebSocketDisconnect:
        return
