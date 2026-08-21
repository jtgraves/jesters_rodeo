from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import urllib.error
import urllib.parse
import urllib.request

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.auth import clear_session_cookie, set_session_cookie, verify_cognito_token
from app.config import settings

logger = logging.getLogger(__name__)
router = APIRouter()

PKCE_COOKIE = "jr_pkce"
PKCE_MAX_AGE_SECONDS = 600
TOKEN_TIMEOUT_SECONDS = 10


def _restart_login() -> RedirectResponse:
    """Send the admin back to the start of the flow with a usable next step."""
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(PKCE_COOKIE, path="/admin")
    return response


def _pkce_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_secret, salt="jr-pkce")


def _redirect_uri() -> str:
    return f"{settings.base_url}/admin/callback"


@router.get("/admin/login")
def login() -> RedirectResponse:
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    params = urllib.parse.urlencode({
        "client_id": settings.cognito_app_client_id,
        "response_type": "code",
        "scope": "openid email",
        "redirect_uri": _redirect_uri(),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    response = RedirectResponse(
        f"https://{settings.cognito_domain}/oauth2/authorize?{params}", status_code=303
    )
    response.set_cookie(
        PKCE_COOKIE,
        _pkce_serializer().dumps(verifier),
        max_age=PKCE_MAX_AGE_SECONDS,
        httponly=True,
        secure=settings.base_url.startswith("https"),
        samesite="lax",
        path="/admin",
    )
    return response


@router.get("/admin/callback")
def callback(request: Request, code: str = "", error: str = "") -> RedirectResponse:
    if error or not code:
        raise HTTPException(status_code=400, detail="Sign-in failed. Please try again.")

    raw = request.cookies.get(PKCE_COOKIE)
    if not raw:
        raise HTTPException(status_code=400, detail="Sign-in expired. Please start again.")
    try:
        verifier = _pkce_serializer().loads(raw, max_age=PKCE_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=400, detail="Sign-in failed. Please try again.")

    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": settings.cognito_app_client_id,
        "code": code,
        "redirect_uri": _redirect_uri(),
        "code_verifier": verifier,
    }).encode()
    token_request = urllib.request.Request(
        f"https://{settings.cognito_domain}/oauth2/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    # Cognito answers with an HTTP error status for a replayed, expired or
    # already-redeemed code — which the back button and a double submit both
    # produce routinely. Unhandled, urlopen raises and the admin gets a 500
    # with a stack trace instead of a chance to try again.
    try:
        with urllib.request.urlopen(token_request, timeout=TOKEN_TIMEOUT_SECONDS) as resp:
            tokens = json.loads(resp.read())
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        logger.warning("Cognito token exchange failed: %s", exc)
        return _restart_login()
    except Exception:
        logger.exception("Unexpected failure during Cognito token exchange")
        return _restart_login()

    id_token = tokens.get("id_token")
    if not id_token:
        raise HTTPException(status_code=400, detail="Sign-in failed. Please try again.")

    # Verify before trusting it, even though it came straight from Cognito.
    verify_cognito_token(id_token)

    response = RedirectResponse("/admin/events", status_code=303)
    set_session_cookie(response, id_token)
    response.delete_cookie(PKCE_COOKIE, path="/admin")
    return response


@router.get("/admin/logout")
def logout() -> RedirectResponse:
    params = urllib.parse.urlencode({
        "client_id": settings.cognito_app_client_id,
        "logout_uri": f"{settings.base_url}/",
    })
    response = RedirectResponse(
        f"https://{settings.cognito_domain}/logout?{params}", status_code=303
    )
    clear_session_cookie(response)
    return response
