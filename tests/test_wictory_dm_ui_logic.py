from __future__ import annotations

from bot.handlers.drop_manager import _split_link_comment as dm_split_link_comment
from bot.handlers.wictory import _render_preview, _split_link_comment as wictory_split_link_comment
from bot.keyboards import kb_dm_post_payment_actions, kb_wictory_bulk_next_actions, kb_wictory_item_actions


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
    assert "🔗 Привязать анкету" not in labels_without
    assert "Продолжить" in labels_without


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
