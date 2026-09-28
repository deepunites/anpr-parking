"""Фаза 2 §1.4: HMAC детерминирован, AES-GCM недетерминирован и привязан к tenant."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from parking.platform.crypto import EnvKeyProvider, PlateCrypto
from parking.shared.plate import PlateNumber


def _provider(*tenants):  # type: ignore[no-untyped-def]
    return EnvKeyProvider(json.dumps({str(t): EnvKeyProvider.generate_entry() for t in tenants}))


def test_hash_is_deterministic_and_tenant_specific() -> None:
    t1, t2 = uuid4(), uuid4()
    crypto = PlateCrypto(_provider(t1, t2))
    plate = PlateNumber.parse("01 A 123 AA")
    assert crypto.hash(t1, plate) == crypto.hash(t1, PlateNumber.parse("01a123aa"))
    assert crypto.hash(t1, plate) != crypto.hash(t2, plate)
    assert len(crypto.hash(t1, plate)) == 32


def test_encrypt_roundtrip_nondeterministic_and_bound_to_tenant() -> None:
    t1, t2 = uuid4(), uuid4()
    crypto = PlateCrypto(_provider(t1, t2))
    plate = PlateNumber.parse("01A123AA")
    blob1, version = crypto.encrypt(t1, plate)
    blob2, _ = crypto.encrypt(t1, plate)
    assert blob1 != blob2 and len(blob1) == 12 + len("01A123AA") + 16 and version == 1
    assert crypto.decrypt(t1, blob1, version) == plate
    with pytest.raises(Exception):  # noqa: B017 — InvalidTag из cryptography
        crypto.decrypt(t2, blob1, version)
    with pytest.raises(ValueError, match="version"):
        crypto.decrypt(t1, blob1, 2)


def test_unknown_tenant_has_no_keys() -> None:
    crypto = PlateCrypto(_provider())
    with pytest.raises(KeyError):
        crypto.hash(uuid4(), PlateNumber.parse("01A123AA"))
