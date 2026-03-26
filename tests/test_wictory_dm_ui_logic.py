from __future__ import annotations

import pytest

from bot.handlers.drop_manager import _split_link_comment as dm_split_link_comment
from bot.handlers.wictory import _render_preview, _split_link_comment as wictory_split_link_comment
from bot.keyboards import (
    kb_dm_approved_attach_item_pick,
    kb_dm_post_payment_actions,
    kb_wictory_bulk_next_actions,
    kb_wictory_item_actions,
)
from bot.models import ResourcePool, ResourceStatus, ResourceType, User, UserRole
from bot.repositories import list_invalid_pool_items_for_wictory, list_wictory_pool_items, wictory_delete_item, wictory_update_item


def test_split_link_comment_wictory_link_esim_payload() -> None:
    raw = "Артур Альянс 43 новый\n\nhttps://cloud.vmoscloud.com/screen/share/ABC"
    link, comment = wictory_split_link_comment(raw)
    assert comment == "Артур Альянс 43 новый"
    assert link == "https://cloud.vmoscloud.com/screen/share/ABC"


def test_split_link_comment_dm_link_esim_payload() -> None:
    raw = "Комментарий строка 1\nКомментарий строка 2\nhttps://example.com/x"
    link, comment = dm_split_link_comment(raw)
    assert comment == "Комментарий строка 1\nКомментарий строка 2"
    assert link == "https://example.com/x"


def test_render_preview_link_esim_has_separate_fields() -> None:
    txt = "Альянс Андрей 50\n\nhttps://cloud.vmoscloud.com/screen/share/F1C8"
    out = _render_preview(
        {
            "resource_type": "link_esim",
            "resource_source": "TG",
            "bank_name": "Альянс",
            "text_data": txt,
            "screenshots": ["photo:abc"],
        }
    )
    assert "Комментарий:" in out
    assert "Ссылка:" in out
    assert "Ссылка/комментарий" not in out


def test_kb_dm_post_payment_actions_buttons() -> None:
    kb_with_attach = kb_dm_post_payment_actions(12, can_attach=True)
    labels_with = [b.text for row in kb_with_attach.inline_keyboard for b in row]
    assert "🔗 Привязать анкету" in labels_with
    assert "Продолжить" in labels_with

    kb_without_attach = kb_dm_post_payment_actions(12, can_attach=False)
    labels_without = [b.text for row in kb_without_attach.inline_keyboard for b in row]
    assert "🔗 Привязать анкету" in labels_without
    assert "Продолжить" in labels_without


def test_kb_dm_approved_attach_item_pick_buttons() -> None:
    kb = kb_dm_approved_attach_item_pick(12, [(101, "RID-101 | test"), (102, "RID-102 | test")])
    labels = [b.text for row in kb.inline_keyboard for b in row]
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "RID-101 | test" in labels
    assert "RID-102 | test" in labels
    assert "⬅️ Назад" in labels
    assert "dm:approved_attach_pick:12:101" in callbacks
    assert "dm:approved_attach_pick:12:102" in callbacks


def test_kb_wictory_bulk_next_actions_buttons() -> None:
    kb = kb_wictory_bulk_next_actions()
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert labels == ["Добавить ещё", "Закончить создание"]


def test_kb_wictory_item_actions_link_esim_has_link_and_comment_buttons() -> None:
    kb = kb_wictory_item_actions(
        13,
        can_edit_link=True,
        can_edit_comment=True,
        can_edit_media=False,
        can_delete=False,
        can_edit_meta=False,
    )
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert "Редактировать ссылку" in labels
    assert "Редактировать комментарий" in labels


@pytest.mark.asyncio
async def test_wictory_lists_and_edits_shared_pool_across_all_wictory_users(session) -> None:
    w1 = User(tg_id=2001, role=UserRole.WICTORY)
    w2 = User(tg_id=2002, role=UserRole.WICTORY)
    dm = User(tg_id=2003, role=UserRole.DROP_MANAGER)
    session.add_all([w1, w2, dm])
    await session.flush()

    it_w1 = ResourcePool(
        source="TG",
        bank_id=1,
        type=ResourceType.LINK,
        status=ResourceStatus.FREE,
        text_data="w1",
        screenshots=[],
        created_by_user_id=int(w1.id),
    )
    it_w2 = ResourcePool(
        source="TG",
        bank_id=1,
        type=ResourceType.ESIM,
        status=ResourceStatus.FREE,
        text_data="w2",
        screenshots=[],
        created_by_user_id=int(w2.id),
    )
    it_dm_invalid = ResourcePool(
        source="TG",
        bank_id=1,
        type=ResourceType.LINK,
        status=ResourceStatus.INVALID,
        text_data="dm",
        screenshots=[],
        created_by_user_id=int(dm.id),
    )
    it_w2_invalid = ResourcePool(
        source="TG",
        bank_id=2,
        type=ResourceType.LINK,
        status=ResourceStatus.INVALID,
        text_data="w2-inv",
        screenshots=[],
        created_by_user_id=int(w2.id),
    )
    session.add_all([it_w1, it_w2, it_dm_invalid, it_w2_invalid])
    await session.flush()

    out = await list_wictory_pool_items(session, wictory_user_id=int(w1.id), limit=100)
    assert {int(x.created_by_user_id) for x in out} == {int(w1.id), int(w2.id)}

    inv = await list_invalid_pool_items_for_wictory(session, wictory_user_id=int(w1.id))
    assert [int(x.id) for x in inv] == [int(it_w2_invalid.id)]

    # Any WICTORY user can edit any WICTORY-created item; DM-created items are not editable via WICTORY ops.
    updated = await wictory_update_item(session, item_id=int(it_w2.id), wictory_user_id=int(w1.id), text_data="new")
    assert updated is not None
    assert updated.text_data == "new"
    not_updated = await wictory_update_item(session, item_id=int(it_dm_invalid.id), wictory_user_id=int(w1.id), text_data="x")
    assert not_updated is None


@pytest.mark.asyncio
async def test_wictory_cannot_delete_used_resource(session) -> None:
    w = User(tg_id=2101, role=UserRole.WICTORY)
    session.add(w)
    await session.flush()

    used_item = ResourcePool(
        source="TG",
        bank_id=1,
        type=ResourceType.LINK,
        status=ResourceStatus.USED,
        text_data="used",
        screenshots=[],
        created_by_user_id=int(w.id),
        used_with_form_id=99,
    )
    session.add(used_item)
    await session.flush()

    ok = await wictory_delete_item(session, item_id=int(used_item.id), wictory_user_id=int(w.id))

    assert ok is False
