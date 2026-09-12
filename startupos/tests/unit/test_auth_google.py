"""Google id_token verification against a locally generated RSA key and a fake JWKS (no network)."""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from auth import google

CLIENT_ID = "test-client-id.apps.googleusercontent.com"


@pytest.fixture(scope="module")
def keypair():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = priv.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(priv.public_key(), as_dict=True)
    jwk.update({"kid": "kid-1", "use": "sig", "alg": "RS256"})
    return pem, jwk


@pytest.fixture(autouse=True)
def _env(monkeypatch, keypair):
    _, jwk = keypair
    monkeypatch.setenv("GOOGLE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "shh")
    monkeypatch.setenv("STARTUPOS_SESSION_SECRET", "unit-test-secret")
    monkeypatch.setenv("STARTUPOS_PUBLIC_URL", "https://api.example.test")
    google.reset_caches()
    fetches = {"jwks": 0}

    def fake_jwks(client=None, *, force=False):
        fetches["jwks"] += 1
        return {"keys": [jwk]}

    monkeypatch.setattr(google, "jwks", fake_jwks)
    monkeypatch.setattr(
        google,
        "discovery",
        lambda client=None: {
            "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_endpoint": "https://oauth2.googleapis.com/token",
            "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
        },
    )
    return fetches


def mint(pem, **over):
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "1234567890",
        "email": "kamal@unitone.ai",
        "email_verified": True,
        "name": "Kamal",
        "iat": now,
        "exp": now + 300,
        "nonce": "n-1",
    }
    claims.update(over)
    return jwt.encode(claims, pem, algorithm="RS256", headers={"kid": over.get("_kid", "kid-1")})


def test_valid_token(keypair):
    pem, _ = keypair
    claims = google.verify_id_token(mint(pem), "n-1")
    assert claims["email"] == "kamal@unitone.ai" and claims["sub"] == "1234567890"
    # nonce optional (sign-up path)
    assert google.verify_id_token(mint(pem))["email"] == "kamal@unitone.ai"
    assert google.verify_id_token(mint(pem, iss="accounts.google.com"), "n-1")


@pytest.mark.parametrize(
    "over",
    [
        {"aud": "someone-else"},
        {"iss": "https://evil.example"},
        {"exp": int(time.time()) - 10},
        {"email_verified": False},
        {"nonce": "wrong"},
        {"_kid": "kid-unknown"},
    ],
)
def test_rejected(keypair, over):
    pem, _ = keypair
    with pytest.raises(google.GoogleAuthError):
        google.verify_id_token(mint(pem, **over), "n-1")


def test_wrong_key_rejected(keypair):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = other.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    with pytest.raises(google.GoogleAuthError):
        google.verify_id_token(mint(pem), "n-1")
    with pytest.raises(google.GoogleAuthError):
        google.verify_id_token("not.a.jwt", "n-1")


def test_build_auth_url_and_oauth_cookie():
    url = google.build_auth_url("st", "nn")
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client_id=" + CLIENT_ID.replace(".", "%2E") in url or f"client_id={CLIENT_ID}" in url
    assert "scope=openid+email+profile" in url and "state=st" in url and "nonce=nn" in url
    assert "redirect_uri=https%3A%2F%2Fapi.example.test%2Fauth%2Fgoogle%2Fcallback" in url
    cookie = google.make_oauth_cookie("st", "nn")
    assert google.read_oauth_cookie(cookie) == ("st", "nn")
    assert google.read_oauth_cookie(cookie + "x") is None
    assert google.read_oauth_cookie(None) is None


def test_exchange_code_uses_injected_client():
    class Resp:
        status_code = 200

        def json(self):
            return {"id_token": "x.y.z", "access_token": "a"}

    class Client:
        def post(self, url, data):
            assert url == "https://oauth2.googleapis.com/token"
            assert data["code"] == "c0de" and data["grant_type"] == "authorization_code"
            return Resp()

    assert google.exchange_code("c0de", client=Client())["id_token"] == "x.y.z"

    class Bad(Client):
        def post(self, url, data):
            r = Resp()
            r.status_code = 400
            return r

    with pytest.raises(google.GoogleAuthError):
        google.exchange_code("c0de", client=Bad())
