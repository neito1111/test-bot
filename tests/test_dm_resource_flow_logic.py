from __future__ import annotations

import pytest

from bot.keyboards import kb_dm_resource_type_pick
from bot.models import Form, FormStatus, ResourcePool, ResourceStatus, ResourceType, User, UserRole
from bot.repositories import count_dm_active_pool_items_for_bank, form_has_linked_pool_item, list_dm_used_pool_items


async def _mk_user(session, tg_id: int, role: UserRole = UserRole.DROP_MANAGER) -> User:
    u = User(tg_id=tg_id, role=role)
    session.add(u)
    await session.flush()
    return u


async def _mk_form(session, manager_id: int, bank_name: str = "Моно", status: FormStatus = FormStatus.APPROVED) -> Form:
    f = Form(manager_id=manager_id, status=status, bank_name=bank_name, screenshots=[])
    session.add(f)
    await session.flush()
    return f


async def _mk_pool_item(
    session,
    *,
    bank_id: int,
    created_by_user_id: int,
    status: ResourceStatus,
    assigned_to_user_id: int | None = None,
    used_with_form_id: int | None = None,
) -> ResourcePool:
    it = ResourcePool(
        source="TG",
        bank_id=bank_id,
        type=ResourceType.LINK,
        status=status,
        text_data="x",
        screenshots=[],
        created_by_user_id=created_by_user_id,
        assigned_to_user_id=assigned_to_user_id,
        used_with_form_id=used_with_form_id,
    )
    session.add(it)
    await session.flush()
    return it


@pytest.mark.asyncio
async def test_count_dm_active_pool_items_for_bank(session) -> None:
    dm = await _mk_user(session, 1101)
    w = await _mk_user(session, 1102, role=UserRole.WICTORY)

    await _mk_pool_item(session, bank_id=1, created_by_user_id=w.id, status=ResourceStatus.ASSIGNED, assigned_to_user_id=dm.id)
    await _mk_pool_item(session, bank_id=1, created_by_user_id=w.id, status=ResourceStatus.ASSIGNED, assigned_to_user_id=dm.id)
    await _mk_pool_item(session, bank_id=2, created_by_user_id=w.id, status=ResourceStatus.ASSIGNED, assigned_to_user_id=dm.id)

    c1 = await count_dm_active_pool_items_for_bank(session, dm_user_id=dm.id, bank_id=1)
    c2 = await count_dm_active_pool_items_for_bank(session, dm_user_id=dm.id, bank_id=2)

    assert c1 == 2
    assert c2 == 1


@pytest.mark.asyncio
async def test_form_has_linked_pool_item_and_used_list(session) -> None:
    dm = await _mk_user(session, 1201)
    w = await _mk_user(session, 1202, role=UserRole.WICTORY)
    form = await _mk_form(session, manager_id=dm.id, bank_name="Моно")

    assert await form_has_linked_pool_item(session, form_id=form.id) is False

    item = await _mk_pool_item(
        session,
        bank_id=1,
        created_by_user_id=w.id,
        status=ResourceStatus.USED,
        used_with_form_id=form.id,
    )

    assert await form_has_linked_pool_item(session, form_id=form.id) is True

    used = await list_dm_used_pool_items(session, dm_user_id=dm.id)
    assert [x.id for x in used] == [item.id]


def test_kb_dm_resource_type_pick_only_available() -> None:
    kb = kb_dm_resource_type_pick(7, ["link_esim"])
    rows = kb.inline_keyboard
    labels = [btn.text for row in rows for btn in row]
    assert "Ссылка + Esim" in labels
    assert "Esim" not in labels
    assert "Ссылка" not in labels
