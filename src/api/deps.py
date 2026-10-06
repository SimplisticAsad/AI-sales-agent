from __future__ import annotations

import hmac
from collections.abc import Iterator

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from src.services.container import Services


def get_db(request: Request) -> Iterator[Session]:
    s = request.app.state.session_factory()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_services(request: Request) -> Services:
    st = request.app.state
    if st.services is None:  # built lazily so /health works without provider credentials
        st.services = st.build_services()
    return st.services


def require_admin(request: Request, x_api_key: str | None = Header(default=None),
                  authorization: str | None = Header(default=None)) -> None:
    key = request.app.state.settings.admin_api_key
    if key is None or not key.get_secret_value():
        raise HTTPException(503, "ADMIN_API_KEY is not configured; admin endpoints are disabled")
    supplied = x_api_key or (authorization[7:] if authorization and authorization.lower().startswith("bearer ") else "")
    if not hmac.compare_digest(supplied.encode(), key.get_secret_value().encode()):
        raise HTTPException(401, "invalid API key")


def is_admin(request: Request, x_api_key: str | None = Header(default=None),
             authorization: str | None = Header(default=None)) -> bool:
    try:
        require_admin(request, x_api_key, authorization)
        return True
    except HTTPException:
        return False


DB = Depends(get_db)
SVC = Depends(get_services)
