from __future__ import annotations

import json
import logging
import time
import urllib.request
from typing import Any

from fastapi import HTTPException, Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from jose import jwt

from app.config import settings

logger = logging.getLogger(__name__)

JWKS_TIMEOUT_SECONDS = 5
JWKS_CACHE_TTL_SECONDS = 3600
SESSION_MAX_AGE_SECONDS = 3600

_jwks_cache: dict | None = None
_jwks_fetched_at: float = 0.0


def issuer() -> str:
    return (
        f"https://cognito-idp.{settings.aws_region}.amazonaws.com/"
        f"{settings.cognito_user_pool_id}"
    )


def reset_jwks_cache() -> None:
    global _jwks_cache, _jwks_fetched_at
    _jwks_cache = None
    _jwks_fetched_at = 0.0


def _get_jwks() -> dict:
    """Fetch and cache the user pool's public keys.

    The timeout is not optional: this runs inside a Lambda with a 15 second
    budget, and `urlopen` without one blocks until the socket gives up, which
    turns a Cognito blip into a hung function.
    """
    global _jwks_cache, _jwks_fetched_at
    now = time.time()
    if _jwks_cache is None or now - _jwks_fetched_at > JWKS_CACHE_TTL_SECONDS:
        with urllib.request.urlopen(
            f"{issuer()}/.well-known/jwks.json", timeout=JWKS_TIMEOUT_SECONDS
        ) as resp:
            _jwks_cache = json.loads(resp.read())
        _jwks_fetched_at = now
    return _jwks_cache


def verify_cognito_token(token: str) -> dict:
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

    key = next((k for k in _get_jwks().get("keys", []) if k.get("kid") == kid), None)
    if key is None:
        raise HTTPException(status_code=401, detail="Invalid token")

    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=settings.cognito_app_client_id,
            issuer=issuer(),
            options={"verify_exp": True, "verify_aud": True, "verify_iss": True},
        )
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

    # Access tokens carry `client_id` rather than `aud` and grant a different
    # scope of authority. Only an ID token proves "this human signed in".
    if claims.get("token_use") != "id":
        raise HTTPException(status_code=401, detail="Invalid token")

    return claims


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_secret, salt="jr-admin-session")


def issue_session(id_token: str) -> str:
    return _serializer().dumps({"id_token": id_token})


def read_session(request: Request) -> str | None:
    raw = request.cookies.get(settings.session_cookie_name)
    if not raw:
        return None
    try:
        data: Any = _serializer().loads(raw, max_age=SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    return data.get("id_token") if isinstance(data, dict) else None


def set_session_cookie(response: Response, id_token: str) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        issue_session(id_token),
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        secure=settings.base_url.startswith("https"),
        # SameSite=Lax is what stops a form on another site from POSTing to
        # /admin/... as the logged-in admin. It is this app's CSRF defence.
        samesite="lax",
        path="/admin",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(settings.session_cookie_name, path="/admin")


def _not_authenticated(request: Request) -> HTTPException:
    if "text/html" in request.headers.get("accept", ""):
        return HTTPException(status_code=303, headers={"Location": "/admin/login"})
    return HTTPException(status_code=401, detail="Not authenticated")


def require_admin(request: Request) -> dict:
    token = read_session(request)
    if token is None:
        raise _not_authenticated(request)
    try:
        return verify_cognito_token(token)
    except HTTPException:
        raise _not_authenticated(request)
