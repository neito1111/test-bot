from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from bot.keyboards import kb_dm_my_forms_list
from bot.models import DuplicateReport, Form, FormStatus, User, UserRole
from bot.repositories import list_user_forms_in_range, phone_bank_duplicate_exists, update_bank
from bot.utils import format_form_status, is_valid_phone, normalize_phone


def test_normalize_phone_variants() -> None:
    assert normalize_phone("0991234567") == "+380 991234567"
    assert normalize_phone("+380991234567") == "+380 991234567"
    assert normalize_phone("991234567") == "+380 991234567"


def test_is_valid_phone_rejects_letters() -> None:
    assert is_valid_phone("+380991234567")
    assert not is_valid_phone("+38099abc456")


def test_format_form_status_values() -> None:
    assert format_form_status(FormStatus.IN_PROGRESS) == "В работе"
    assert format_form_status("APPROVED") == "Подтверждена"


@pytest.mark.asyncio
async def test_phone_bank_duplicate_exists(session) -> None:
    u1 = User(tg_id=101, role=UserRole.DROP_MANAGER)
    u2 = User(tg_id=102, role=UserRole.DROP_MANAGER)
    session.add_all([u1, u2])
    await session.flush()

    f1 = Form(manager_id=u1.id, status=FormStatus.PENDING, phone="+380 991234567", bank_name="Моно")
    f2 = Form(manager_id=u2.id, status=FormStatus.PENDING, phone="+380 991234567", bank_name="Моно")
    session.add_all([f1, f2])
    await session.flush()

    dup = await phone_bank_duplicate_exists(session, phone=f1.phone, bank_name=f1.bank_name, exclude_form_id=f1.id)
    assert dup is not None
    assert dup.id == f2.id


@pytest.mark.asyncio
async def test_list_user_forms_in_range_filters(session) -> None:
    u1 = User(tg_id=201, role=UserRole.DROP_MANAGER)
    session.add(u1)
    await session.flush()

    now = datetime.now(timezone.utc)
    f1 = Form(manager_id=u1.id, status=FormStatus.PENDING, created_at=now - timedelta(days=2))
    f2 = Form(manager_id=u1.id, status=FormStatus.PENDING, created_at=now - timedelta(days=1))
    session.add_all([f1, f2])
    await session.flush()

    res = await list_user_forms_in_range(
        session,
        user_id=u1.id,
        created_from=now - timedelta(days=1, hours=1),
        created_to=now + timedelta(days=1),
    )
    assert [f.id for f in res] == [f2.id]


def test_kb_dm_my_forms_list_builds_buttons() -> None:
    forms = [
        SimpleNamespace(id=1, bank_name="Моно", status=FormStatus.PENDING),
        SimpleNamespace(id=2, bank_name=None, status=FormStatus.APPROVED),
    ]
    kb = kb_dm_my_forms_list(forms)
    assert kb.inline_keyboard[0][0].text.startswith("#1")
    assert kb.inline_keyboard[1][0].text.startswith("#2")


@pytest.mark.asyncio
async def test_update_bank_renames_related_records(session) -> None:
    u = User(tg_id=301, role=UserRole.DROP_MANAGER)
    session.add(u)
    await session.flush()

    from bot.repositories import create_bank
    bank = await create_bank(session, "Моно")
    f = Form(manager_id=u.id, status=FormStatus.PENDING, phone="+380 991234567", bank_name="Моно")
    d = DuplicateReport(manager_id=u.id, manager_source="TG", phone="+380 991234567", bank_name="Моно")
    session.add_all([f, d])
    await session.flush()

    await update_bank(session, bank.id, name="Mono")
    await session.flush()

    assert bank.name == "Mono"
    assert f.bank_name == "Mono"
    assert d.bank_name == "Mono"
