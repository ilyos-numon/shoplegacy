import time
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from shoplegacy import auth
from shoplegacy.auth import (
    ANON,
    PRINCIPAL,
    ROLE_ORDER,
    AuthError,
    Principal,
    current_principal,
    decode_token,
    has_role,
    require_role,
)
from shoplegacy.config import Settings
from shoplegacy.store import MembershipRow

SECRET = "test-secret-that-is-long-enough-for-hs256"


def _settings(**kw: Any) -> Settings:
    base: dict[str, Any] = {
        "provider": "fake",
        "stt_provider": "fake",
        "data_dir": Path("./data"),
        "database_url": "postgresql://unused",
        "jwt_secret": SECRET,
    }
    base.update(kw)
    return Settings(**base)


def _token(key: Any = SECRET, alg: str = "HS256", headers: Any = None, **claims: Any) -> str:
    payload: dict[str, Any] = {
        "sub": "11111111-1111-1111-1111-111111111111",
        "aud": "authenticated",
        "exp": int(time.time()) + 300,
        "email": "u@example.test",
        "role": "authenticated",
    }
    payload.update(claims)
    return jwt.encode(payload, key, algorithm=alg, headers=headers)


def test_hs256_valid_token() -> None:
    claims = decode_token(_token(), _settings())
    assert claims["sub"] == "11111111-1111-1111-1111-111111111111"
    assert claims["email"] == "u@example.test"


def test_expired_token_rejected() -> None:
    with pytest.raises(AuthError):
        decode_token(_token(exp=int(time.time()) - 60), _settings())


def test_bad_signature_rejected() -> None:
    with pytest.raises(AuthError):
        decode_token(_token(key="another-secret-that-is-long-enough-x"), _settings())


def test_audience_mismatch_rejected() -> None:
    with pytest.raises(AuthError):
        decode_token(_token(aud="someone-else"), _settings())


def test_hs256_rejected_when_only_jwks_configured() -> None:
    with pytest.raises(AuthError):
        decode_token(_token(), _settings(jwt_secret="", jwks_url="https://jwks.invalid"))


def test_jwks_es256_with_fake_client(monkeypatch: pytest.MonkeyPatch) -> None:
    private = ec.generate_private_key(ec.SECP256R1())
    other = ec.generate_private_key(ec.SECP256R1())
    jwk = jwt.PyJWK.from_json(jwt.algorithms.ECAlgorithm.to_jwk(private.public_key()), "ES256")

    class FakeClient:
        def get_signing_key_from_jwt(self, token: str) -> jwt.PyJWK:
            assert jwt.get_unverified_header(token)["kid"] == "k1"
            return jwk

    monkeypatch.setattr(auth, "_jwks_client", lambda url: FakeClient())
    settings = _settings(jwt_secret="", jwks_url="https://jwks.invalid/.well-known/jwks.json")
    good = _token(key=private, alg="ES256", headers={"kid": "k1"})
    assert decode_token(good, settings)["sub"] == "11111111-1111-1111-1111-111111111111"
    forged = _token(key=other, alg="ES256", headers={"kid": "k1"})
    with pytest.raises(AuthError):
        decode_token(forged, settings)


def test_role_ordering() -> None:
    assert ROLE_ORDER["interviewer"] < ROLE_ORDER["reviewer"] < ROLE_ORDER["admin"]
    assert ROLE_ORDER["admin"] < ROLE_ORDER["owner"]
    assert has_role("owner", "admin")
    assert has_role("reviewer", "reviewer")
    assert not has_role("interviewer", "reviewer")
    assert not has_role(None, "interviewer")


class FakeStore:
    def __init__(self, memberships: list[MembershipRow]) -> None:
        self.memberships = memberships
        self.seen: list[Principal] = []

    def my_orgs(self, user_id: str) -> list[MembershipRow]:
        self.seen.append(PRINCIPAL.get())
        return self.memberships


ReviewerP = Annotated[Principal, Depends(require_role("reviewer"))]
ORG_A = "aaaaaaaa-0000-0000-0000-000000000001"
ORG_B = "bbbbbbbb-0000-0000-0000-000000000002"


def _client(memberships: list[MembershipRow]) -> tuple[TestClient, FakeStore]:
    store = FakeStore(memberships)
    app = FastAPI()
    app.state.shop = SimpleNamespace(settings=_settings(), store=store)

    @app.get("/who")
    def who(p: Annotated[Principal, Depends(current_principal)]) -> dict[str, Any]:
        seen = PRINCIPAL.get()
        return {"user_id": p.user_id, "org_id": p.org_id, "role": p.role, "ctx": seen.user_id}

    @app.get("/review")
    def review(p: ReviewerP) -> dict[str, str]:
        return {"ok": p.user_id}

    return TestClient(app), store


def test_no_bearer_is_anon() -> None:
    client, store = _client([])
    body = client.get("/who").json()
    assert body == {"user_id": "", "org_id": None, "role": None, "ctx": ""}
    assert store.seen == []
    assert ANON.is_anon


def test_invalid_token_is_401() -> None:
    client, _ = _client([])
    r = client.get("/who", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid token"}


def test_principal_resolves_single_org_and_sets_context() -> None:
    client, store = _client([MembershipRow(ORG_A, "A", "reviewer")])
    r = client.get("/who", headers={"Authorization": f"Bearer {_token()}"})
    assert r.json() == {
        "user_id": "11111111-1111-1111-1111-111111111111",
        "org_id": ORG_A,
        "role": "reviewer",
        "ctx": "11111111-1111-1111-1111-111111111111",
    }
    assert store.seen[0].user_id == "11111111-1111-1111-1111-111111111111"


def test_require_role_org_rules() -> None:
    two = [MembershipRow(ORG_A, "A", "reviewer"), MembershipRow(ORG_B, "B", "interviewer")]
    client, _ = _client(two)
    auth_h = {"Authorization": f"Bearer {_token()}"}
    assert client.get("/review").json() == {"detail": "login required"}
    r = client.get("/review", headers=auth_h)
    assert (r.status_code, r.json()) == (400, {"detail": "org_required"})
    r = client.get("/review", headers={**auth_h, "X-Org-Id": ORG_B})
    assert (r.status_code, r.json()) == (403, {"detail": "forbidden"})
    r = client.get("/review", headers={**auth_h, "X-Org-Id": ORG_A})
    assert r.status_code == 200
    r = client.get("/review", headers={**auth_h, "X-Org-Id": "cccccccc-0000-0000-0000-000000000003"})
    assert (r.status_code, r.json()) == (403, {"detail": "forbidden"})
