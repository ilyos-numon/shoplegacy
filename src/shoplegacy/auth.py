"""JWT verification, the request ``Principal`` and role dependencies.

``Principal``, ``ANON``, ``PRINCIPAL`` and ``Role`` live in ``store`` (the Store
reads ``PRINCIPAL`` per transaction) and are re-exported here.
"""

from collections.abc import Callable
from functools import lru_cache
from typing import Annotated, Any

import jwt
from fastapi import Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from shoplegacy.app_state import State, get_state
from shoplegacy.config import Settings
from shoplegacy.store import ANON, PRINCIPAL, Principal, Role

__all__ = [
    "ANON",
    "PRINCIPAL",
    "ROLE_ORDER",
    "AdminDep",
    "AuthError",
    "InterviewerDep",
    "Principal",
    "ReviewerDep",
    "Role",
    "UserDep",
    "current_principal",
    "decode_token",
    "has_role",
    "require_role",
    "require_user",
]

ROLE_ORDER: dict[Role, int] = {"interviewer": 1, "reviewer": 2, "admin": 3, "owner": 4}
_ASYMMETRIC_ALGORITHMS = ["ES256", "RS256"]

StateDep = Annotated[State, Depends(get_state)]


class AuthError(Exception):
    """The bearer token could not be verified."""


@lru_cache(maxsize=4)
def _jwks_client(url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(url)


def decode_token(token: str, settings: Settings) -> dict[str, Any]:
    """Verify ``token`` with HS256 (``jwt_secret``) or JWKS (``jwks_url``)."""
    try:
        alg = jwt.get_unverified_header(token).get("alg")
        key: Any
        if alg == "HS256":
            if not settings.jwt_secret:
                raise AuthError("HS256 tokens are not accepted")
            key, algorithms = settings.jwt_secret, ["HS256"]
        elif alg in _ASYMMETRIC_ALGORITHMS:
            if not settings.jwks_url:
                raise AuthError(f"{alg} tokens are not accepted")
            key = _jwks_client(settings.jwks_url).get_signing_key_from_jwt(token)
            algorithms = [alg]
        else:
            raise AuthError(f"unsupported alg: {alg}")
        claims: dict[str, Any] = jwt.decode(
            token,
            key,
            algorithms=algorithms,
            audience=settings.jwt_audience,
            options={"require": ["exp", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise AuthError(str(exc)) from exc
    if not isinstance(claims.get("sub"), str) or not claims["sub"]:
        raise AuthError("missing sub")
    return claims


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


async def current_principal(request: Request, state: StateDep) -> Principal:
    """Resolve the caller and set ``PRINCIPAL`` for the rest of the request.

    Async on purpose: a ContextVar set here (in the request task) is copied into
    the threadpool that runs sync endpoints, so the Store sees the principal.
    """
    token = _bearer(request)
    if token is None:
        PRINCIPAL.set(ANON)
        return ANON
    try:
        claims = decode_token(token, state.settings)
    except AuthError:
        raise HTTPException(status_code=401, detail="invalid token") from None
    email = claims.get("email")
    user = Principal(
        user_id=claims["sub"],
        email=email if isinstance(email, str) and email else None,
        org_id=None,
        role=None,
        claims=claims,
    )
    PRINCIPAL.set(user)
    memberships = await run_in_threadpool(state.store.my_orgs, user.user_id)
    requested = request.headers.get("x-org-id", "").strip().lower()
    org_id: str | None = None
    role: Role | None = None
    if requested:
        org_id = requested
        role = next((m.role for m in memberships if m.org_id.lower() == requested), None)
    elif len(memberships) == 1:
        org_id, role = memberships[0].org_id, memberships[0].role
    principal = Principal(
        user_id=user.user_id, email=user.email, org_id=org_id, role=role, claims=claims
    )
    PRINCIPAL.set(principal)
    return principal


def require_user(p: Annotated[Principal, Depends(current_principal)]) -> Principal:
    if p.is_anon:
        raise HTTPException(status_code=401, detail="login required")
    return p


def has_role(role: Role | None, minimum: Role) -> bool:
    return role is not None and ROLE_ORDER[role] >= ROLE_ORDER[minimum]


def require_role(minimum: Role) -> Callable[..., Principal]:
    def dependency(p: Annotated[Principal, Depends(require_user)]) -> Principal:
        if p.org_id is None:
            raise HTTPException(status_code=400, detail="org_required")
        if not has_role(p.role, minimum):
            raise HTTPException(status_code=403, detail="forbidden")
        return p

    return dependency


UserDep = Annotated[Principal, Depends(require_user)]
InterviewerDep = Annotated[Principal, Depends(require_role("interviewer"))]
ReviewerDep = Annotated[Principal, Depends(require_role("reviewer"))]
AdminDep = Annotated[Principal, Depends(require_role("admin"))]
