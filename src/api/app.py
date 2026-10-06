from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, sessionmaker

from src.agents.outreach.agent import OutreachError
from src.api.deps import require_admin
from src.config.settings import Settings, get_settings
from src.database.base import init_db, make_engine, make_session_factory
from src.integrations.delivery.targets import DeliveryError
from src.integrations.outreach.providers import OutreachSendError
from src.integrations.payments.base import PaymentError
from src.integrations.retell.client import RetellError
from src.services.calls import CallBlocked
from src.services.container import Services, build_services
from src.services.escalation import EscalationError
from src.services.llm import LLMError
from src.services.outreach import OutreachBlocked
from src.services.proposals import ProposalError
from src.services.state_machine import InvalidTransition

log = logging.getLogger("api")

_ERRORS = (
    (LookupError, 404), (InvalidTransition, 409), (ProposalError, 409), (PaymentError, 409), (EscalationError, 409),
    (OutreachBlocked, 409), (CallBlocked, 409), (PermissionError, 403), (OutreachError, 422),
    (OutreachSendError, 502), (RetellError, 502), (DeliveryError, 502), (LLMError, 502),
)


def create_app(settings: Settings | None = None, services: Services | None = None,
               session_factory: sessionmaker[Session] | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="Autonomous AI Sales Agent", version="0.1.0")
    if session_factory is None:
        engine = make_engine(settings.database_url)
        init_db(engine)
        session_factory = make_session_factory(engine)
    app.state.settings = settings
    app.state.session_factory = session_factory
    app.state.services = services
    app.state.build_services = lambda: build_services(settings)

    def _handler(status: int):
        async def h(_: Request, exc: Exception):
            return JSONResponse({"detail": str(exc)}, status_code=status)
        return h

    for exc, code in _ERRORS:
        app.add_exception_handler(exc, _handler(code))

    from src.api.routes import admin, public, retell_ws, webhooks

    app.include_router(public.router)
    app.include_router(webhooks.router)
    app.include_router(retell_ws.router)
    app.include_router(admin.router, dependencies=[Depends(require_admin)])
    return app


def __getattr__(name: str):  # `uvicorn src.api.app:app` -> lazy module attribute (no import-time side effects)
    if name == "app":
        a = create_app()
        globals()["app"] = a
        return a
    raise AttributeError(name)
