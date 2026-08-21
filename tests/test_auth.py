from __future__ import annotations

import time
import urllib.error
from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from jose import jwt
from jose.backends import RSAKey
from jose.constants import ALGORITHMS

from app import auth
from app.config import settings
from app.main import app
from app.routes import auth_routes

ISSUER = f"https://cognito-idp.us-east-1.amazonaws.com/{settings.cognito_user_pool_id}"


def _new_key(kid: str) -> tuple[str, dict]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    jwk = RSAKey(pem, ALGORITHMS.RS256).public_key().to_dict()
    jwk = {k: (v.decode() if isinstance(v, bytes) else v) for k, v in jwk.items()}
    jwk["kid"] = kid
    return pem, jwk


@pytest.fixture
def signing_key():
    auth.reset_jwks_cache()
    pem, jwk = _new_key("test-kid")
    with patch.object(auth, "_get_jwks", return_value={"keys": [jwk]}):
        yield pem
    auth.reset_jwks_cache()


def _token(pem: str, kid: str = "test-kid", **overrides) -> str:
    claims = {
        "sub": "admin-1",
        "aud": settings.cognito_app_client_id,
        "iss": ISSUER,
        "token_use": "id",
        "email": "admin@example.com",
        "exp": int(time.time()) + 3600,
        "iat": int(time.time()),
    }
    claims.update(overrides)
    return jwt.encode(claims, pem, algorithm="RS256", headers={"kid": kid})


def test_valid_id_token_is_accepted(signing_key):
    claims = auth.verify_cognito_token(_token(signing_key))
    assert claims["sub"] == "admin-1"
    assert claims["email"] == "admin@example.com"


def test_token_signed_by_a_different_key_is_rejected(signing_key):
    """The whole point of JWKS verification: a well-formed forgery must fail."""
    attacker_pem, _ = _new_key("test-kid")  # same kid, wrong key
    with pytest.raises(HTTPException) as exc:
        auth.verify_cognito_token(_token(attacker_pem))
    assert exc.value.status_code == 401


def test_expired_token_is_rejected(signing_key):
    with pytest.raises(HTTPException) as exc:
        auth.verify_cognito_token(_token(signing_key, exp=int(time.time()) - 60))
    assert exc.value.status_code == 401


def test_token_for_another_app_client_is_rejected(signing_key):
    with pytest.raises(HTTPException) as exc:
        auth.verify_cognito_token(_token(signing_key, aud="some-other-client"))
    assert exc.value.status_code == 401


def test_token_from_another_user_pool_is_rejected(signing_key):
    with pytest.raises(HTTPException) as exc:
        auth.verify_cognito_token(
            _token(signing_key, iss="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_evil")
        )
    assert exc.value.status_code == 401


def test_access_token_is_rejected(signing_key):
    """Cognito access tokens have no `aud`; accepting them widens the gate."""
    with pytest.raises(HTTPException) as exc:
        auth.verify_cognito_token(_token(signing_key, token_use="access"))
    assert exc.value.status_code == 401


def test_unknown_kid_is_rejected(signing_key):
    with pytest.raises(HTTPException) as exc:
        auth.verify_cognito_token(_token(signing_key, kid="not-in-jwks"))
    assert exc.value.status_code == 401


def test_garbage_token_is_rejected(signing_key):
    with pytest.raises(HTTPException) as exc:
        auth.verify_cognito_token("this is not a jwt")
    assert exc.value.status_code == 401


def test_jwks_fetch_uses_a_timeout_and_caches():
    auth.reset_jwks_cache()
    payload = b'{"keys": []}'
    fake = MagicMock()
    fake.__enter__.return_value.read.return_value = payload
    with patch.object(auth.urllib.request, "urlopen", return_value=fake) as mock_open:
        auth._get_jwks()
        auth._get_jwks()
    # A Lambda with a 15s timeout cannot afford an untimed network call.
    assert mock_open.call_count == 1, "JWKS must be cached across calls"
    assert mock_open.call_args.kwargs["timeout"] == auth.JWKS_TIMEOUT_SECONDS
    auth.reset_jwks_cache()


def _callback_client_with_pkce() -> TestClient:
    """A client carrying a valid, unexpired PKCE cookie for /admin/callback."""
    client = TestClient(app)
    client.cookies.set(
        auth_routes.PKCE_COOKIE, auth_routes._pkce_serializer().dumps("the-verifier")
    )
    return client


def test_callback_survives_a_failed_token_exchange(monkeypatch):
    """A replayed or expired code is routine (back button, double submit).

    Cognito answers those with an HTTP error status, which makes urlopen raise.
    Unguarded, the admin sees a 500 and a stack trace instead of a way back.
    """
    error = urllib.error.HTTPError(
        url="https://cognito/oauth2/token", code=400, msg="Bad Request", hdrs=None, fp=None
    )
    monkeypatch.setattr(auth_routes.urllib.request, "urlopen", MagicMock(side_effect=error))

    resp = _callback_client_with_pkce().get("/admin/callback?code=already-redeemed",
                                            follow_redirects=False)

    assert resp.status_code == 303, "a routine failure must not surface as a 500"
    assert resp.headers["location"] == "/admin/login"


def test_callback_survives_an_unreachable_token_endpoint(monkeypatch):
    monkeypatch.setattr(
        auth_routes.urllib.request, "urlopen",
        MagicMock(side_effect=urllib.error.URLError("connection refused")),
    )

    resp = _callback_client_with_pkce().get("/admin/callback?code=abc",
                                            follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"


def test_callback_survives_a_malformed_token_response(monkeypatch):
    """A 200 carrying something that isn't JSON must not 500 either."""
    fake = MagicMock()
    fake.__enter__.return_value.read.return_value = b"<html>gateway error</html>"
    monkeypatch.setattr(auth_routes.urllib.request, "urlopen", MagicMock(return_value=fake))

    resp = _callback_client_with_pkce().get("/admin/callback?code=abc",
                                            follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/login"


def _request(cookies: dict | None = None, accept: str = "text/html") -> Request:
    headers = [(b"accept", accept.encode())]
    if cookies:
        raw = "; ".join(f"{k}={v}" for k, v in cookies.items())
        headers.append((b"cookie", raw.encode()))
    return Request({"type": "http", "headers": headers, "method": "GET", "path": "/admin/events"})


def test_session_cookie_round_trips():
    signed = auth.issue_session("the-id-token")
    request = _request({settings.session_cookie_name: signed})
    assert auth.read_session(request) == "the-id-token"


def test_tampered_session_cookie_is_rejected():
    signed = auth.issue_session("the-id-token")
    request = _request({settings.session_cookie_name: signed[:-3] + "aaa"})
    assert auth.read_session(request) is None


def test_require_admin_redirects_browsers_to_login():
    with pytest.raises(HTTPException) as exc:
        auth.require_admin(_request(accept="text/html"))
    assert exc.value.status_code == 303
    assert exc.value.headers["Location"] == "/admin/login"


def test_require_admin_401s_api_clients():
    with pytest.raises(HTTPException) as exc:
        auth.require_admin(_request(accept="application/json"))
    assert exc.value.status_code == 401
