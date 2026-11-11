"""Shop consent records (recording, storage, AI processing, publication)."""

import uuid
from datetime import UTC, datetime
from typing import Literal, cast

from pydantic import BaseModel

from shoplegacy.store import PRINCIPAL, ConsentRow, Store

ConsentKind = Literal["recording", "storage", "ai_processing", "publication"]
ConsentMethod = Literal["verbal", "written", "link"]


class Consent(BaseModel):
    consent_id: str
    shop_id: str
    kind: ConsentKind
    method: ConsentMethod
    note: str | None
    granted_at: datetime
    revoked_at: datetime | None
    revoke_reason: str | None


class ConsentExistsError(ValueError):
    """The shop already has an active (unrevoked) consent of this kind."""


class ConsentRevokedError(ValueError):
    """The consent has already been revoked."""


class ConsentNotFoundError(LookupError):
    """The consent is missing or not visible to the current org."""


def _to_model(row: ConsentRow) -> Consent:
    return Consent(
        consent_id=row.consent_id,
        shop_id=row.shop_id,
        kind=cast(ConsentKind, row.kind),
        method=cast(ConsentMethod, row.method),
        note=row.note,
        granted_at=row.granted_at,
        revoked_at=row.revoked_at,
        revoke_reason=row.revoke_reason,
    )


class ConsentService:
    """Grant/revoke shop consents and write ``consent.*`` audit entries.

    The ``consents_audit`` DB trigger already logs every row change, but only
    with the raw ``INSERT``/``UPDATE`` operation; the named ``consent.grant`` /
    ``consent.revoke`` entries are written here, once per action.
    """

    def __init__(self, store: Store) -> None:
        self._store = store

    def grant(
        self, shop_id: str, kind: ConsentKind, method: ConsentMethod, note: str | None
    ) -> Consent:
        if kind in self._store.active_consent_kinds(shop_id):
            raise ConsentExistsError(f"active {kind} consent exists for shop {shop_id}")
        principal = PRINCIPAL.get()
        row = ConsentRow(
            consent_id=uuid.uuid4().hex,
            org_id=principal.org_id or "",
            shop_id=shop_id,
            kind=kind,
            method=method,
            note=note,
            granted_by=principal.user_id,
            granted_at=datetime.now(UTC),
            revoked_at=None,
            revoke_reason=None,
        )
        self._store.insert_consent(row)
        self._store.add_audit(
            "consent.grant",
            "consents",
            row.consent_id,
            {"shop_id": shop_id, "kind": kind, "method": method},
        )
        return _to_model(row)

    def revoke(self, consent_id: str, reason: str) -> Consent:
        row = self._store.get_consent(consent_id)
        if row is None:
            raise ConsentNotFoundError(consent_id)
        if row.revoked_at is not None:
            raise ConsentRevokedError(f"consent {consent_id} already revoked")
        row.revoked_at = datetime.now(UTC)
        row.revoke_reason = reason
        self._store.update_consent(row)
        self._store.add_audit(
            "consent.revoke",
            "consents",
            consent_id,
            {"shop_id": row.shop_id, "kind": row.kind, "reason": reason},
        )
        return _to_model(row)

    def list(self, shop_id: str) -> list[Consent]:
        return [_to_model(r) for r in self._store.list_consents(shop_id)]

    def active_kinds(self, shop_id: str) -> set[str]:
        return self._store.active_consent_kinds(shop_id)
