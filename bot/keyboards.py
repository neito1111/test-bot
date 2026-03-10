from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from bot.callbacks import (
    AccessRequestCb,
    BankCb,
    BankEditCb,
    FormEditCb,
    FormReviewCb,
    TeamLeadMenuCb,
)
from bot.utils import format_access_status, format_bank_hashtag, format_form_status, unpack_media_item


def kb_drop_main() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.add(KeyboardButton(text="Начать работу"))
    b.add(KeyboardButton(text="Закончить работу"))
    b.adjust(2)
    return b.as_markup(resize_keyboard=True)


def kb_drop_shift_active() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.add(KeyboardButton(text="Создать анкету"))
    b.add(KeyboardButton(text="Неапрувнутые анкеты"))
    b.add(KeyboardButton(text="Закончить работу"))
    b.adjust(2)
    return b.as_markup(resize_keyboard=True)


def kb_yes_no(confirm_action: str, cancel_action: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.add(InlineKeyboardButton(text="Да", callback_data=confirm_action))
    b.add(InlineKeyboardButton(text="Нет", callback_data=cancel_action))
    b.adjust(2)
    return b.as_markup()


def kb_dm_forms_filter_menu(*, current: str | None) -> InlineKeyboardMarkup:
    cur = (current or "today").lower()
    b = InlineKeyboardBuilder()
    items = [
        ("Сегодня", "today"),
        ("Вчера", "yesterday"),
        ("Текущая неделя", "week"),
        ("Последние 7 дней", "last7"),
        ("Текущий месяц", "month"),
        ("Предыдущий месяц", "prev_month"),
        ("Последние 30 дней", "last30"),
        ("Текущий год", "year"),
        ("За все время", "all"),
    ]
    for title, key in items:
        prefix = "✅ " if key == cur else ""
        b.button(text=f"{prefix}{title}", callback_data=f"dm:my_forms_filter_set:{key}")
    b.button(text="Интервал дат", callback_data="dm:my_forms_filter_custom")
    b.button(text="⬅️ Назад", callback_data="dm:menu")
    b.adjust(1)
    return b.as_markup()


def kb_dm_my_forms_list(forms: list) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for f in forms[:40]:
        bank = getattr(f, "bank_name", None) or "—"
        status = format_form_status(getattr(f, "status", None))
        b.button(text=f"#{f.id} {format_bank_hashtag(bank)} ({status})", callback_data=f"dm:my_form_open:{int(f.id)}")
    b.button(text="📅 Фильтр", callback_data="dm:my_forms_filter")
    b.button(text="⬅️ Назад", callback_data="dm:menu")
    b.adjust(1)
    return b.as_markup()


def kb_dm_my_form_open(form_id: int, *, in_progress: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if in_progress:
        b.button(text="Дозакончить", callback_data=f"dm:my_form_resume:{int(form_id)}")
        b.button(text="Удалить", callback_data=f"dm:my_form_delete:{int(form_id)}")
        b.button(text="⬅️ Назад", callback_data="dm:my_forms")
        b.adjust(1, 2)
        return b.as_markup()
    b.button(text="Отправить", callback_data=f"dm:my_form_send:{int(form_id)}")
    b.button(text="Редактировать", callback_data=FormEditCb(action="open", form_id=form_id).pack())
    b.button(text="Удалить", callback_data=f"dm:my_form_delete:{int(form_id)}")
    b.button(text="⬅️ Назад", callback_data="dm:my_forms")
    b.adjust(2, 1, 1)
    return b.as_markup()


def kb_dm_payment_card(form_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="КАРТА ДЛЯ ОПЛАТЫ", callback_data=f"dm:pay_card:{int(form_id)}")
    b.adjust(1)
    return b.as_markup()


def kb_dm_payment_card_with_back(form_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="КАРТА ДЛЯ ОПЛАТЫ", callback_data=f"dm:pay_card:{int(form_id)}")
    b.button(text="⬅️ Назад", callback_data="dm:approved_no_pay")
    b.adjust(1)
    return b.as_markup()


def kb_dm_approved_attach_type_pick(form_id: int, available_types: list[str] | tuple[str, ...]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    picked = [str(t).lower() for t in (available_types or [])]
    if "link" in picked:
        b.button(text="Ссылка", callback_data=f"dm:approved_attach_type:{int(form_id)}:link")
    if "esim" in picked:
        b.button(text="Esim", callback_data=f"dm:approved_attach_type:{int(form_id)}:esim")
    if "link_esim" in picked:
        b.button(text="Ссылка + Esim", callback_data=f"dm:approved_attach_type:{int(form_id)}:link_esim")
    b.button(text="⬅️ Назад", callback_data=f"dm:approved_no_pay_open:{int(form_id)}")
    b.adjust(1)
    return b.as_markup()


def kb_dm_payment_next_actions(form_id: int | None = None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Добавить карту", callback_data="dm:pay_add_card")
    b.button(text="Финал", callback_data="dm:pay_finish")
    if form_id:
        b.button(text="🔗 Привязать анкету", callback_data=f"dm:approved_attach:{int(form_id)}")
    b.adjust(2, 1)
    return b.as_markup()


def kb_access_request(tg_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Апрув", callback_data=AccessRequestCb(action="approve", tg_id=tg_id).pack())
    b.button(text="❌ Отклонить", callback_data=AccessRequestCb(action="reject", tg_id=tg_id).pack())
    b.adjust(2)
    return b.as_markup()


def kb_dm_source_pick_inline() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="TG", callback_data="dm:src:TG")
    b.button(text="FB", callback_data="dm:src:FB")
    b.adjust(2)
    return b.as_markup()


def kb_traffic_type() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.add(KeyboardButton(text="Прямой"))
    b.add(KeyboardButton(text="Сарафан"))
    b.adjust(2)
    return b.as_markup(resize_keyboard=True)


DEFAULT_BANKS = ["Пумб", "Моно", "Альянс", "Фрибанк", "Майбанк"]


def kb_bank_select() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    for name in DEFAULT_BANKS:
        b.add(KeyboardButton(text=name))
    b.add(KeyboardButton(text="Написать название"))
    b.adjust(3)
    return b.as_markup(resize_keyboard=True)


def kb_done() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.add(KeyboardButton(text="Готово"))
    return b.as_markup(resize_keyboard=True)


def kb_back() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.add(KeyboardButton(text="Назад"))
    b.adjust(1)
    return b.as_markup(resize_keyboard=True, one_time_keyboard=True)


def kb_start_only() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.add(KeyboardButton(text="Старт"))
    return b.as_markup(resize_keyboard=True)


def kb_back_dm() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.add(KeyboardButton(text="Назад"))
    b.adjust(1)
    return b.as_markup(resize_keyboard=True, one_time_keyboard=True)


def kb_back_with_main() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.add(KeyboardButton(text="Назад"))
    b.adjust(1)
    return b.as_markup(resize_keyboard=True, one_time_keyboard=True)


def kb_traffic_type_with_back() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.add(KeyboardButton(text="Прямой"))
    b.add(KeyboardButton(text="Сарафан"))
    b.add(KeyboardButton(text="Назад"))
    b.adjust(2, 1)
    return b.as_markup(resize_keyboard=True)


def kb_bank_select_with_back() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    for name in DEFAULT_BANKS:
        b.add(KeyboardButton(text=name))
    b.add(KeyboardButton(text="Написать название"))
    b.add(KeyboardButton(text="Назад"))
    b.adjust(3, 1)
    return b.as_markup(resize_keyboard=True)


def kb_form_confirm() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.add(InlineKeyboardButton(text="Отправить", callback_data="form_submit"))
    b.add(InlineKeyboardButton(text="Отмена", callback_data="form_cancel"))
    b.adjust(2)
    return b.as_markup()


def kb_form_confirm_with_edit(form_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.add(InlineKeyboardButton(text="Отправить", callback_data="form_submit"))
    b.add(InlineKeyboardButton(text="Редактировать", callback_data=FormEditCb(action="open", form_id=form_id).pack()))
    b.add(InlineKeyboardButton(text="Отмена", callback_data="form_cancel"))
    b.adjust(2, 1)
    return b.as_markup()


def kb_form_review(form_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Подтвердить", callback_data=FormReviewCb(action="approve", form_id=form_id).pack())
    b.button(text="❌ Отклонить", callback_data=FormReviewCb(action="reject", form_id=form_id).pack())
    b.adjust(2)
    return b.as_markup()


def kb_form_review_with_back(form_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Подтвердить", callback_data=FormReviewCb(action="approve", form_id=form_id).pack())
    b.button(text="❌ На корректировку", callback_data=FormReviewCb(action="reject", form_id=form_id).pack())
    b.button(text="⬅️ Назад", callback_data=TeamLeadMenuCb(action="live").pack())
    b.adjust(2, 1)
    return b.as_markup()


def kb_tl_live_list(forms: list) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for f in forms[:30]:
        bank = getattr(f, "bank_name", None) or "—"
        b.button(text=f"#{int(f.id)} {bank}", callback_data=f"tl:live_open:{int(f.id)}")
    b.button(text="🏠 Меню", callback_data=TeamLeadMenuCb(action="home").pack())
    b.adjust(1)
    return b.as_markup()


def kb_edit_open(form_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Изменить", callback_data=FormEditCb(action="open", form_id=form_id).pack())
    return b.as_markup()


def kb_dm_reject_notice(form_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Перейти", callback_data=FormEditCb(action="open", form_id=form_id).pack())
    b.adjust(1)
    return b.as_markup()


def kb_dm_edit_actions_inline(form_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Тип клиента", callback_data=f"dm_edit:field:{form_id}:traffic_type")
    b.button(text="Форварды", callback_data=f"dm_edit:field:{form_id}:forwards")
    b.button(text="Номер", callback_data=f"dm_edit:field:{form_id}:phone")
    b.button(text="Банк", callback_data=f"dm_edit:field:{form_id}:bank")
    b.button(text="Пароль", callback_data=f"dm_edit:field:{form_id}:password")
    b.button(text="Скрины", callback_data=f"dm_edit:screens:{form_id}")
    b.button(text="Комментарий", callback_data=f"dm_edit:field:{form_id}:comment")
    b.button(text="Отправить заново", callback_data=f"dm_edit:resubmit:{form_id}")
    b.button(text="⬅️ Назад", callback_data=f"dm_edit:back:{form_id}")
    b.adjust(2, 2, 2, 2, 1)
    return b.as_markup()


def kb_dm_edit_screens_inline(form_id: int, screenshots: list[str]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    shot_i = 0
    doc_i = 0
    vid_i = 0
    for i, raw in enumerate(list(screenshots or [])):
        kind, _ = unpack_media_item(str(raw))
        if kind == "doc":
            doc_i += 1
            title = f"Файл {doc_i}"
        elif kind == "video":
            vid_i += 1
            title = f"Видео {vid_i}"
        else:
            shot_i += 1
            title = f"Скрин {shot_i}"
        b.button(text=title, callback_data=f"dm_edit:screen_pick:{form_id}:{i}")
    b.button(text="➕ Добавить скрин", callback_data=f"dm_edit:screen_add:{form_id}")
    b.button(text="⬅️ Назад", callback_data=f"dm_edit:back:{form_id}")
    b.adjust(3, 3, 3, 1, 1)
    return b.as_markup()


def kb_edit_fields() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.add(KeyboardButton(text="Тип клиента"))
    b.add(KeyboardButton(text="Форварды"))
    b.add(KeyboardButton(text="Номер"))
    b.add(KeyboardButton(text="Банк"))
    b.add(KeyboardButton(text="Пароль"))
    b.add(KeyboardButton(text="Скрины"))
    b.add(KeyboardButton(text="Комментарий"))
    b.add(KeyboardButton(text="Отправить заново"))
    b.adjust(2)
    return b.as_markup(resize_keyboard=True)


def kb_team_lead_main() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.add(KeyboardButton(text="Лайв анкеты"))
    b.add(KeyboardButton(text="Условия для сдачи"))
    b.adjust(2)
    return b.as_markup(resize_keyboard=True)


def kb_developer_main() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.add(KeyboardButton(text="Заявки"))
    b.add(KeyboardButton(text="Пользователи"))
    b.add(KeyboardButton(text="Анкеты"))
    b.add(KeyboardButton(text="Статистика"))
    b.adjust(2)
    return b.as_markup(resize_keyboard=True)


def kb_developer_start() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.add(KeyboardButton(text="Старт"))
    b.adjust(1)
    return b.as_markup(resize_keyboard=True)


def kb_developer_with_back() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.add(KeyboardButton(text="Заявки"))
    b.add(KeyboardButton(text="Пользователи"))
    b.add(KeyboardButton(text="Анкеты"))
    b.add(KeyboardButton(text="Статистика"))
    b.add(KeyboardButton(text="Назад"))
    b.adjust(2, 2, 1)
    return b.as_markup(resize_keyboard=True)


def kb_developer_list() -> ReplyKeyboardMarkup:
    """Клавиатура для списков - только Назад"""
    b = ReplyKeyboardBuilder()
    b.add(KeyboardButton(text="Назад"))
    b.adjust(1)
    return b.as_markup(resize_keyboard=True)


def kb_developer_stats() -> ReplyKeyboardMarkup:
    """Клавиатура для статистики - только Назад"""
    b = ReplyKeyboardBuilder()
    b.add(KeyboardButton(text="Назад"))
    b.adjust(1)
    return b.as_markup(resize_keyboard=True)


def kb_dev_back_main_inline() -> InlineKeyboardMarkup:
    """Inline кнопка назад в меню разработчика"""
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data="dev:back_to_main")
    b.adjust(1)
    return b.as_markup()


def kb_dev_req_pick_role(tg_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🎯 Дроп-менеджер", callback_data=f"dev:req_set_role:{tg_id}:DROP_MANAGER")
    b.button(text="👑 Тим-лид", callback_data=f"dev:req_set_role:{tg_id}:TEAM_LEAD")
    b.button(text="⬅️ Назад", callback_data="dev:menu:reqs")
    b.adjust(2, 1)
    return b.as_markup()


def kb_dev_req_pick_team_lead_source(tg_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="TG", callback_data=f"dev:req_set_tl_source:{tg_id}:TG")
    b.button(text="FB", callback_data=f"dev:req_set_tl_source:{tg_id}:FB")
    b.button(text="⬅️ Назад", callback_data=f"dev:req_back_role:{tg_id}")
    b.adjust(2, 1)
    return b.as_markup()


def kb_dev_req_pick_dm_source(tg_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="TG", callback_data=f"dev:req_set_dm_source:{tg_id}:TG")
    b.button(text="FB", callback_data=f"dev:req_set_dm_source:{tg_id}:FB")
    b.button(text="⬅️ Назад", callback_data=f"dev:req_back_role:{tg_id}")
    b.adjust(2, 1)
    return b.as_markup()


def kb_dev_req_pick_forward_group(*, tg_id: int, groups: list) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for g in groups[:40]:
        status = "✅" if getattr(g, "is_confirmed", False) else "❌"
        title = getattr(g, "title", None) or "—"
        b.button(text=f"{status} #{g.id} {title}", callback_data=f"dev:user_group_set:{tg_id}:{int(g.id)}")
    b.button(text="➕ Добавить группу", callback_data=f"dev:req_group_add:{tg_id}")
    b.button(text="⬅️ Назад", callback_data=f"dev:req_back_dm_source:{tg_id}")
    b.adjust(1)
    return b.as_markup()


def kb_dm_main_inline(*, shift_active: bool, rejected_count: int | None = None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if shift_active:
        b.button(text="Создать анкету", callback_data="dm:create_form")
        b.button(text="Мои анкеты", callback_data="dm:my_forms")
        b.button(text="✅ Подтверждённые без номеров", callback_data="dm:approved_no_pay")
        rej_text = "Неапрувнутые анкеты"
        if rejected_count is not None:
            rej_text = f"({int(rejected_count)}) {rej_text}"
        b.button(text=rej_text, callback_data="dm:rejected")
        b.button(text="Запрос ссылки", callback_data="dm:resource_menu")
        b.button(text="Закончить работу", callback_data="dm:end_shift")
        b.adjust(2, 2, 1, 1)
        return b.as_markup()
    b.button(text="Начать работу", callback_data="dm:start_shift")
    b.adjust(1)
    return b.as_markup()


def kb_dm_back_to_menu_inline() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data="dm:menu")
    b.adjust(1)
    return b.as_markup()


def kb_dm_back_cancel_inline(*, back_cb: str, cancel_cb: str = "dm:cancel") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data=back_cb)
    b.button(text="❌ Отмена", callback_data=cancel_cb)
    b.adjust(2)
    return b.as_markup()


def kb_dm_duplicate_bank_phone_inline() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data="dm:back_to_bank_select")
    b.button(text="🏦 Поменять банк", callback_data="dm:back_to_bank_select")
    b.button(text="❌ Закончить", callback_data="dm:cancel_form")
    b.adjust(2, 1)
    return b.as_markup()


def kb_dm_traffic_type_inline() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Прямой", callback_data="dm:traffic:DIRECT")
    b.button(text="Сарафан", callback_data="dm:traffic:REFERRAL")
    b.button(text="⬅️ Назад", callback_data="dm:cancel_form")
    b.adjust(2, 1)
    return b.as_markup()


def kb_dm_bank_select_inline() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for name in DEFAULT_BANKS:
        b.button(text=name, callback_data=f"dm:bank:{name}")
    b.button(text="⬅️ Назад", callback_data="dm:back_to_phone")
    b.adjust(3, 1)
    return b.as_markup()


def kb_dm_bank_select_inline_from_names(names: list[str]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for name in names:
        b.button(text=name, callback_data=f"dm:bank:{name}")
    b.button(text="⬅️ Назад", callback_data="dm:back_to_phone")
    b.adjust(3, 1)
    return b.as_markup()


def kb_dm_bank_select_inline_from_items(items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for bank_id, name in items:
        b.button(text=name, callback_data=f"dm:bank_id:{int(bank_id)}")
    b.button(text="⬅️ Назад", callback_data="dm:back_to_phone")
    b.adjust(3, 1)
    return b.as_markup()


def kb_dm_edit_bank_select_inline(*, form_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for name in DEFAULT_BANKS:
        b.button(text=name, callback_data=f"dm_edit:bank_pick:{int(form_id)}:{name}")
    b.button(text="⬅️ Назад", callback_data=f"dm_edit:back:{int(form_id)}")
    b.adjust(3, 1)
    return b.as_markup()


def kb_dm_edit_bank_select_inline_from_names(*, form_id: int, names: list[str]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for name in names:
        b.button(text=name, callback_data=f"dm_edit:bank_pick:{int(form_id)}:{name}")
    b.button(text="⬅️ Назад", callback_data=f"dm_edit:back:{int(form_id)}")
    b.adjust(3, 1)
    return b.as_markup()


def kb_dm_edit_bank_select_inline_from_items(*, form_id: int, items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for bank_id, name in items:
        b.button(text=name, callback_data=f"dm_edit:bank_pick_id:{int(form_id)}:{int(bank_id)}")
    b.button(text="⬅️ Назад", callback_data=f"dm_edit:back:{int(form_id)}")
    b.adjust(3, 1)
    return b.as_markup()


def kb_dm_done_inline() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Готово", callback_data="dm:screens_done")
    b.button(text="⬅️ Назад", callback_data="dm:back_to_password")
    b.adjust(2)
    return b.as_markup()


def kb_dm_edit_done_inline(form_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Готово", callback_data=f"dm_edit:screens_done:{form_id}")
    b.button(text="⬅️ Назад", callback_data=f"dm_edit:back:{form_id}")
    b.adjust(2)
    return b.as_markup()


def kb_dm_shift_comment_inline(*, shift_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Без комментария", callback_data=f"shift_comment_skip:{shift_id}")
    b.button(text="⬅️ Назад", callback_data=f"shift_comment_back:{shift_id}")
    b.adjust(1, 1)
    return b.as_markup()


def kb_dev_main_inline() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Заявки", callback_data="dev:menu:reqs")
    b.button(text="Пользователи", callback_data="dev:menu:users")
    b.button(text="Анкеты", callback_data="dev:menu:forms")
    b.button(text="Статистика", callback_data="dev:menu:stats")
    b.button(text="Группы", callback_data="dev:menu:groups")
    b.button(text="Тим‑лиды", callback_data="dev:menu:tls")
    b.adjust(2, 2, 2)
    return b.as_markup()


def kb_dev_team_leads_actions() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="➕ Добавить TG", callback_data="dev:tls:add:TG")
    b.button(text="➕ Добавить FB", callback_data="dev:tls:add:FB")
    b.button(text="➖ Удалить", callback_data="dev:tls:del")
    b.button(text="✏️ Сменить источник", callback_data="dev:tls:edit_source")
    b.button(text="⬅️ Назад", callback_data="dev:back_to_main")
    b.adjust(2, 2, 1)
    return b.as_markup()


def kb_dev_team_lead_pick_source(tg_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="TG", callback_data=f"dev:tls:set_source:{tg_id}:TG")
    b.button(text="FB", callback_data=f"dev:tls:set_source:{tg_id}:FB")
    b.button(text="⬅️ Назад", callback_data="dev:menu:tls")
    b.adjust(2, 1)
    return b.as_markup()


def kb_dev_groups_actions() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="➕ Добавить", callback_data="dev:groups:add")
    b.button(text="➖ Удалить", callback_data="dev:groups:del")
    b.button(text="🔄 Проверить", callback_data="dev:groups:check")
    b.button(text="⬅️ Назад", callback_data="dev:back_to_main")
    b.adjust(2, 1, 1)
    return b.as_markup()


def kb_dev_groups_list(groups: list) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for g in groups[:40]:
        status = "✅" if getattr(g, "is_confirmed", False) else "❌"
        title = getattr(g, "title", None) or "—"
        b.button(text=f"{status} #{g.id} {title}", callback_data=f"dev:group:open:{int(g.id)}")
    b.button(text="➕ Добавить", callback_data="dev:groups:add")
    b.button(text="🔄 Проверить", callback_data="dev:groups:check")
    b.button(text="⬅️ Назад", callback_data="dev:back_to_main")
    b.adjust(1)
    return b.as_markup()


def kb_dev_group_open(group_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Проверить", callback_data=f"dev:group:check:{group_id}")
    b.button(text="🗑 Удалить", callback_data=f"dev:group:del:{group_id}")
    b.button(text="⬅️ Назад", callback_data="dev:menu:groups")
    b.adjust(1)
    return b.as_markup()


def kb_dev_pick_forward_group(*, tg_id: int, groups: list, include_skip: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for g in groups[:40]:
        status = "✅" if getattr(g, "is_confirmed", False) else "❌"
        title = getattr(g, "title", None) or "—"
        b.button(text=f"{status} #{g.id} {title}", callback_data=f"dev:user_group_set:{tg_id}:{int(g.id)}")
    if include_skip:
        b.button(text="Пропустить", callback_data=f"dev:user_group_skip:{tg_id}")
    b.button(text="❎ Снять привязку", callback_data=f"dev:user_group_set:{tg_id}:NONE")
    b.button(text="⬅️ Назад", callback_data=f"dev:back_to_user:{tg_id}")
    b.adjust(1)
    return b.as_markup()


def kb_dev_users_list_beautiful(users: list) -> tuple[str, InlineKeyboardMarkup]:
    """Создает красивый список пользователей с inline кнопками"""
    lines = [
        f"👥 <b>ПОЛЬЗОВАТЕЛИ СИСТЕМЫ</b>\n",
        f"Всего: <b>{len(users)}</b>\n"
    ]
    
    b = InlineKeyboardBuilder()
    for i, user in enumerate(users, 1):
        name = f"{user.first_name or ''} {user.last_name or ''}".strip() or "—"
        username = f"@{user.username}" if user.username else "—"
        
        
        role_emoji = {
            "DEVELOPER": "👨‍💻",
            "TEAM_LEAD": "👑", 
            "DROP_MANAGER": "🎯",
            "PENDING": "⏳"
        }
        emoji = role_emoji.get(user.role, "❓")

        group_mark = ""
        try:
            if str(getattr(user, "role", "")).endswith("DROP_MANAGER"):
                group_mark = "✅" if getattr(user, "forward_group_id", None) else "❌"
        except Exception:
            group_mark = ""

        lines.append(f"\n{i}. {emoji}{group_mark} <code>{user.tg_id}</code> | <b>{name}</b> | {username}")

        src_icon = ""
        src = getattr(user, "manager_source", None)
        if src:
            s = str(src).upper()
            if s == "TG":
                src_icon = "✈️"
            elif s == "FB":
                src_icon = "📘"
        
        b.button(text=f"{emoji}{group_mark}{src_icon} {name} ({user.tg_id})", callback_data=f"dev:select_user:{user.tg_id}")
    b.button(text="⬅️ Назад", callback_data="dev:back_to_main")
    b.adjust(1)
    return "\n".join(lines), b.as_markup()


def kb_dev_forms_filter_menu(*, current: str | None) -> InlineKeyboardMarkup:
    cur = (current or "today").lower()
    b = InlineKeyboardBuilder()

    items = [
        ("Сегодня", "today"),
        ("Вчера", "yesterday"),
        ("Текущая неделя", "week"),
        ("Последние 7 дней", "last7"),
        ("Текущий месяц", "month"),
        ("Предыдущий месяц", "prev_month"),
        ("Последние 30 дней", "last30"),
        ("Текущий год", "year"),
        ("За все время", "all"),
    ]
    for title, key in items:
        prefix = "✅ " if key == cur else ""
        b.button(text=f"{prefix}{title}", callback_data=f"dev:forms_filter_set:{key}")

    b.button(text="Интервал дат", callback_data="dev:forms_filter_custom")
    b.button(text="⬅️ Назад", callback_data="dev:forms_filter_back")

    b.adjust(1)
    return b.as_markup()


def kb_dev_users_list_beautiful_with_sources(
    users: list,
    *,
    team_lead_sources: dict[int, str] | None,
) -> tuple[str, InlineKeyboardMarkup]:
    lines = [
        f"👥 <b>ПОЛЬЗОВАТЕЛИ СИСТЕМЫ</b>\n",
        f"Всего: <b>{len(users)}</b>\n",
    ]
    b = InlineKeyboardBuilder()
    for i, user in enumerate(users, 1):
        name = f"{user.first_name or ''} {user.last_name or ''}".strip() or "—"
        username = f"@{user.username}" if user.username else "—"

        role_emoji = {
            "DEVELOPER": "👨‍💻",
            "TEAM_LEAD": "👑",
            "DROP_MANAGER": "🎯",
            "PENDING": "⏳",
        }
        emoji = role_emoji.get(user.role, "❓")

        group_mark = ""
        try:
            if str(getattr(user, "role", "")).endswith("DROP_MANAGER"):
                group_mark = "✅" if getattr(user, "forward_group_id", None) else "❌"
        except Exception:
            group_mark = ""

        src_icon = ""
        if str(user.role) == "TEAM_LEAD" and team_lead_sources is not None:
            src = team_lead_sources.get(int(user.tg_id))
            if src == "TG":
                src_icon = "✈️"
            elif src == "FB":
                src_icon = "📘"
        else:
            src = getattr(user, "manager_source", None)
            if src:
                s = str(src).upper()
                if s == "TG":
                    src_icon = "✈️"
                elif s == "FB":
                    src_icon = "📘"

        lines.append(f"\n{i}. {emoji}{group_mark}{src_icon} <code>{user.tg_id}</code> | <b>{name}</b> | {username}")

        b.button(text=f"{emoji}{group_mark}{src_icon} {name} ({user.tg_id})", callback_data=f"dev:select_user:{user.tg_id}")

    b.button(text="⬅️ Назад", callback_data="dev:back_to_main")
    b.adjust(1)
    return "\n".join(lines), b.as_markup()


def kb_dev_forms_list_beautiful(forms: list) -> tuple[str, InlineKeyboardMarkup]:
    """Создает красивый список анкет с inline кнопками"""
    lines = [
        f"📋 <b>АНКЕТЫ СИСТЕМЫ</b>\n",
        f"Всего: <b>{len(forms)}</b>\n"
    ]
    
    b = InlineKeyboardBuilder()
    b.button(text="📅 Фильтр", callback_data="dev:forms_filter_menu")
    for i, form in enumerate(forms, 1):
        status_emoji = {
            "IN_PROGRESS": "⏳",
            "PENDING": "📨",
            "APPROVED": "✅",
            "REJECTED": "❌",
        }
        emoji = status_emoji.get(str(form.status), "❓")
        traffic = "Прямой" if form.traffic_type == "DIRECT" else "Сарафан" if form.traffic_type == "REFERRAL" else "—"
        
        status_label = format_form_status(getattr(form, "status", None))
        bank = format_bank_hashtag(getattr(form, "bank_name", None))
        lines.append(f"\n{i}. {emoji} <code>{form.id}</code> | <b>{bank}</b> | {traffic} | {status_label}")
        
        b.button(
            text=f"{emoji} Анкета #{form.id} - {bank} ({status_label})",
            callback_data=f"dev:select_form:{form.id}",
        )
    b.button(text="⬅️ Назад", callback_data="dev:back_to_main")
    b.adjust(1)
    return "\n".join(lines), b.as_markup()


def kb_dev_requests_list_beautiful(requests: list) -> tuple[str, InlineKeyboardMarkup]:
    """Создает красивый список заявок с inline кнопками"""
    lines = [
        f"📝 <b>ЗАЯВКИ НА ДОСТУП</b>\n",
        f"Всего: <b>{len(requests)}</b>\n"
    ]
    
    b = InlineKeyboardBuilder()
    for i, req in enumerate(requests, 1):
        status_emoji = {
            "PENDING": "⏳",
            "APPROVED": "✅",
            "REJECTED": "❌",
        }
        emoji = status_emoji.get(str(req.status), "❓")
        status_label = format_access_status(getattr(req, "status", None))

        lines.append(f"\n{i}. {emoji} <code>{req.user_id}</code> | <b>{status_label}</b>")

        b.button(
            text=f"{emoji} Заявка #{req.user_id} - {status_label}",
            callback_data=f"dev:select_req:{req.user_id}",
        )
    b.button(text="⬅️ Назад", callback_data="dev:back_to_main")
    b.adjust(1)
    return "\n".join(lines), b.as_markup()


def kb_dev_users_list(users: list) -> InlineKeyboardMarkup:
    """Inline клавиатура для списка пользователей"""
    b = InlineKeyboardBuilder()
    for user in users:
        b.add(InlineKeyboardButton(
            text=f"{user.first_name or ''} {user.last_name or ''} (@{user.username or '—'}) - {user.tg_id}",
            callback_data=f"dev:select_user:{user.tg_id}"
        ))
    b.adjust(1)
    return b.as_markup()


def kb_dev_forms_list(forms: list) -> InlineKeyboardMarkup:
    """Inline клавиатура для списка анкет"""
    b = InlineKeyboardBuilder()
    for form in forms:
        status_emoji = {
            "IN_PROGRESS": "⏳",
            "PENDING": "📨", 
            "APPROVED": "✅",
            "REJECTED": "❌"
        }
        emoji = status_emoji.get(form.status, "❓")
        bank = format_bank_hashtag(getattr(form, "bank_name", None))
        b.add(
            InlineKeyboardButton(
                text=f"{emoji} Анкета #{form.id} - {bank}",
                callback_data=f"dev:select_form:{form.id}",
            )
        )
    b.adjust(1)
    return b.as_markup()


def kb_dev_requests_list(requests: list) -> InlineKeyboardMarkup:
    """Inline клавиатура для списка заявок"""
    b = InlineKeyboardBuilder()
    for req in requests:
        status_emoji = {
            "PENDING": "⏳",
            "APPROVED": "✅",
            "REJECTED": "❌"
        }
        emoji = status_emoji.get(req.status, "❓")
        b.add(InlineKeyboardButton(
            text=f"{emoji} Заявка #{req.user_id} - {req.status}",
            callback_data=f"dev:select_req:{req.user_id}"
        ))
    b.adjust(1)
    return b.as_markup()


def kb_dev_confirm(kind: str, tg_id: int) -> InlineKeyboardMarkup:
    """
    kind: 'user' | 'req'
    """
    b = InlineKeyboardBuilder()
    if kind == "user":
        b.add(InlineKeyboardButton(text="🗑 Удалить пользователя", callback_data=f"dev:del_user:{tg_id}"))
    elif kind == "req":
        b.add(InlineKeyboardButton(text="🗑 Удалить заявку", callback_data=f"dev:del_req:{tg_id}"))
    b.add(InlineKeyboardButton(text="Отмена", callback_data="dev:cancel"))
    b.adjust(1)
    return b.as_markup()


def kb_dev_user_actions(tg_id: int) -> InlineKeyboardMarkup:
    """Keyboard for user actions"""
    b = InlineKeyboardBuilder()
    b.add(InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"dev:edit_user:{tg_id}"))
    b.add(InlineKeyboardButton(text="🏷 Группа пересылки", callback_data=f"dev:user_group:{tg_id}"))
    b.add(InlineKeyboardButton(text="🗑️ Удалить", callback_data=f"dev:del_user:{tg_id}"))
    b.add(InlineKeyboardButton(text="⬅️ Назад", callback_data="dev:back_to_users"))
    b.adjust(1)
    return b.as_markup()


def kb_dev_form_actions(form_id: int) -> InlineKeyboardMarkup:
    """Keyboard for form actions"""
    b = InlineKeyboardBuilder()
    b.add(InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"dev:edit_form:{form_id}"))
    b.add(InlineKeyboardButton(text="🗑️ Удалить", callback_data=f"dev:del_form:{form_id}"))
    b.add(InlineKeyboardButton(text="⬅️ Назад", callback_data="dev:back_to_forms"))
    b.adjust(1)
    return b.as_markup()


def kb_dev_req_actions(tg_id: int) -> InlineKeyboardMarkup:
    """Keyboard for request actions"""
    b = InlineKeyboardBuilder()
    b.add(InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"dev:edit_req:{tg_id}"))
    b.add(InlineKeyboardButton(text="🗑️ Удалить", callback_data=f"dev:del_req:{tg_id}"))
    b.add(InlineKeyboardButton(text="⬅️ Назад", callback_data="dev:back_to_reqs"))
    b.adjust(1)
    return b.as_markup()


def kb_dev_edit_user(tg_id: int) -> InlineKeyboardMarkup:
    """Keyboard for editing user fields"""
    b = InlineKeyboardBuilder()
    b.add(InlineKeyboardButton(text="📝 Имя", callback_data=f"dev:edit_user_field:{tg_id}:first_name"))
    b.add(InlineKeyboardButton(text="📝 Фамилия", callback_data=f"dev:edit_user_field:{tg_id}:last_name"))
    b.add(InlineKeyboardButton(text="📝 Username", callback_data=f"dev:edit_user_field:{tg_id}:username"))
    b.add(InlineKeyboardButton(text="📝 Роль", callback_data=f"dev:edit_user_field:{tg_id}:role"))
    b.add(InlineKeyboardButton(text="📝 Тег менеджера", callback_data=f"dev:edit_user_field:{tg_id}:manager_tag"))
    b.add(InlineKeyboardButton(text="📝 Источник (TG/FB)", callback_data=f"dev:edit_user_field:{tg_id}:manager_source"))
    b.add(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"dev:back_to_user:{tg_id}"))
    b.adjust(1)
    return b.as_markup()


def kb_dev_pick_user_role(tg_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="PENDING", callback_data=f"dev:set_user_role:{tg_id}:PENDING")
    b.button(text="DROP_MANAGER", callback_data=f"dev:set_user_role:{tg_id}:DROP_MANAGER")
    b.button(text="TEAM_LEAD", callback_data=f"dev:set_user_role:{tg_id}:TEAM_LEAD")
    b.button(text="DEVELOPER", callback_data=f"dev:set_user_role:{tg_id}:DEVELOPER")
    b.button(text="WICTORY", callback_data=f"dev:set_user_role:{tg_id}:WICTORY")
    b.button(text="⬅️ Назад", callback_data=f"dev:back_to_user:{tg_id}")
    b.adjust(2, 2, 1, 1)
    return b.as_markup()


def kb_dev_pick_team_lead_source(tg_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="TG", callback_data=f"dev:set_team_lead_source:{tg_id}:TG")
    b.button(text="FB", callback_data=f"dev:set_team_lead_source:{tg_id}:FB")
    b.button(text="⬅️ Назад", callback_data=f"dev:edit_user_field:{tg_id}:role")
    b.adjust(2, 1)
    return b.as_markup()


def kb_dev_pick_user_source(tg_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="TG", callback_data=f"dev:set_user_source:{tg_id}:TG")
    b.button(text="FB", callback_data=f"dev:set_user_source:{tg_id}:FB")
    b.button(text="Сброс", callback_data=f"dev:set_user_source:{tg_id}:NONE")
    b.button(text="⬅️ Назад", callback_data=f"dev:back_to_user:{tg_id}")
    b.adjust(2, 1, 1)
    return b.as_markup()


def kb_dev_edit_form(form_id: int) -> InlineKeyboardMarkup:
    """Keyboard for editing form fields"""
    b = InlineKeyboardBuilder()
    b.add(InlineKeyboardButton(text="📊 Тип клиента", callback_data=f"dev:edit_form_field:{form_id}:traffic_type"))
    b.add(InlineKeyboardButton(text="📞 Телефон", callback_data=f"dev:edit_form_field:{form_id}:phone"))
    b.add(InlineKeyboardButton(text="🏦 Банк", callback_data=f"dev:edit_form_field:{form_id}:bank_name"))
    b.add(InlineKeyboardButton(text="🔐 Пароль", callback_data=f"dev:edit_form_field:{form_id}:password"))
    b.add(InlineKeyboardButton(text="📝 Комментарий", callback_data=f"dev:edit_form_field:{form_id}:comment"))
    b.add(InlineKeyboardButton(text="📊 Статус", callback_data=f"dev:edit_form_field:{form_id}:status"))
    b.add(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"dev:back_to_form:{form_id}"))
    b.adjust(1)
    return b.as_markup()


def kb_dm_resource_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Активные ссылки / Esim", callback_data="dm:resource_active")
    b.button(text="Мои подтянутые ссылки", callback_data="dm:resource_used")
    b.button(text="Банки", callback_data="dm:resource_banks")
    b.button(text="Создать Банк", callback_data="dm:resource_create_bank")
    b.button(text="⬅️ Назад", callback_data="dm:menu")
    b.adjust(1)
    return b.as_markup()


def kb_dm_resource_banks(items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for bank_id, name in items[:50]:
        b.button(text=name, callback_data=f"dm:resource_bank:{int(bank_id)}")
    b.button(text="⬅️ Назад", callback_data="dm:resource_menu")
    b.adjust(1)
    return b.as_markup()


def kb_dm_resource_bank_actions(bank_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Взять ресурс", callback_data=f"dm:resource_take:{int(bank_id)}")
    b.button(text="Выйти на главную", callback_data="dm:menu")
    b.adjust(1)
    return b.as_markup()


def kb_dm_resource_empty_bank(bank_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Выбрать другой банк", callback_data="dm:resource_banks")
    b.button(text="В главное меню", callback_data="dm:menu")
    b.button(text="⬅️ Назад", callback_data="dm:resource_menu")
    b.adjust(1)
    return b.as_markup()


def kb_dm_resource_type_pick(bank_id: int, available_types: list[str] | tuple[str, ...] | None = None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    picked = [str(t).lower() for t in (available_types or ["esim", "link", "link_esim"])]
    if "esim" in picked:
        b.button(text="Esim", callback_data=f"dm:resource_take_type:{int(bank_id)}:esim")
    if "link" in picked:
        b.button(text="Ссылка", callback_data=f"dm:resource_take_type:{int(bank_id)}:link")
    if "link_esim" in picked:
        b.button(text="Ссылка + Esim", callback_data=f"dm:resource_take_type:{int(bank_id)}:link_esim")
    b.button(text="⬅️ Назад", callback_data=f"dm:resource_bank:{int(bank_id)}")
    b.adjust(1)
    return b.as_markup()


def kb_dm_resource_active_list(items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for item_id, title in items[:50]:
        b.button(text=title, callback_data=f"dm:resource_active_open:{int(item_id)}")
    b.button(text="⬅️ Назад", callback_data="dm:resource_menu")
    b.adjust(1)
    return b.as_markup()


def kb_dm_resource_active_actions(item_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Главная", callback_data="dm:menu")
    b.button(text="Вернуть обратно в запрос", callback_data=f"dm:resource_release:{int(item_id)}")
    b.button(text="Esim/Ссылка не Валид", callback_data=f"dm:resource_invalid:{int(item_id)}")
    b.button(text="Подтянуть анкетой", callback_data=f"dm:resource_attach:{int(item_id)}")
    b.adjust(1)
    return b.as_markup()


def kb_dm_resource_attach_forms(item_id: int, forms: list) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for f in forms[:40]:
        b.button(text=f"#{int(f.id)} {(getattr(f, 'bank_name', None) or '—')}", callback_data=f"dm:resource_attach_pick:{int(item_id)}:{int(f.id)}")
    b.button(text="⬅️ Назад", callback_data=f"dm:resource_active_open:{int(item_id)}")
    b.adjust(1)
    return b.as_markup()


def kb_dm_resource_used_list(items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for item_id, title in items[:50]:
        b.button(text=title, callback_data=f"dm:resource_used_open:{int(item_id)}")
    b.button(text="⬅️ Назад", callback_data="dm:resource_menu")
    b.adjust(1)
    return b.as_markup()


def kb_dm_resource_used_actions() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ К списку подтянутых", callback_data="dm:resource_used")
    b.button(text="🏠 В меню ресурсов", callback_data="dm:resource_menu")
    b.adjust(1)
    return b.as_markup()


def kb_wictory_main_inline() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Добавить ссылку", callback_data="wictory:add:link")
    b.button(text="Добавить Esim", callback_data="wictory:add:esim")
    b.button(text="Добавить ссылку + Esim", callback_data="wictory:add:link_esim")
    b.button(text="Мои записи", callback_data="wictory:items:list")
    b.button(text="Просмотр пула по банкам", callback_data="wictory:stats")
    b.button(text="Невалидные ссылки", callback_data="wictory:invalid:list")
    b.adjust(1)
    return b.as_markup()


def kb_wictory_pick_source(*, back_cb: str = "wictory:home") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="TG", callback_data="wictory:src:TG")
    b.button(text="FB", callback_data="wictory:src:FB")
    b.button(text="⬅️ Назад", callback_data=back_cb)
    b.adjust(2, 1)
    return b.as_markup()


def kb_wictory_banks(items: list[tuple[int, str]], *, back_cb: str = "wictory:home") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for bank_id, name in items[:50]:
        b.button(text=name, callback_data=f"wictory:bank:{int(bank_id)}")
    b.button(text="⬅️ Назад", callback_data=back_cb)
    b.adjust(1)
    return b.as_markup()


def kb_wictory_bank_actions() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Добавить массово", callback_data="wictory:bank_mode:bulk")
    b.button(text="Обычное добавление", callback_data="wictory:bank_mode:single")
    b.button(text="⬅️ Назад", callback_data="wictory:back:bank")
    b.adjust(1)
    return b.as_markup()


def kb_wictory_back_cancel(*, back_cb: str, cancel_cb: str = "wictory:cancel_create") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data=back_cb)
    b.button(text="❌ Отмена", callback_data=cancel_cb)
    b.adjust(2)
    return b.as_markup()


def kb_wictory_upload_actions(*, back_cb: str, done_cb: str = "wictory:upload_done", cancel_cb: str = "wictory:cancel_create") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Готово", callback_data=done_cb)
    b.button(text="⬅️ Назад", callback_data=back_cb)
    b.button(text="❌ Отмена", callback_data=cancel_cb)
    b.adjust(1, 2)
    return b.as_markup()


def kb_wictory_preview() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Редактировать", callback_data="wictory:edit")
    b.button(text="Подтвердить", callback_data="wictory:confirm")
    b.button(text="⬅️ Назад", callback_data="wictory:home")
    b.adjust(2, 1)
    return b.as_markup()


def kb_wictory_edit() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Изменить банк", callback_data="wictory:edit:bank")
    b.button(text="Изменить данные", callback_data="wictory:edit:data")
    b.button(text="Заменить скриншот", callback_data="wictory:edit:screen")
    b.button(text="⬅️ Назад", callback_data="wictory:preview")
    b.adjust(1)
    return b.as_markup()


def kb_wictory_invalid_list(items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for item_id, title in items[:50]:
        b.button(text=title, callback_data=f"wictory:invalid:open:{int(item_id)}")
    b.button(text="⬅️ Назад", callback_data="wictory:home")
    b.adjust(1)
    return b.as_markup()


def kb_wictory_invalid_actions(item_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Изменить данные", callback_data=f"wictory:invalid:edit_data:{int(item_id)}")
    b.button(text="Заменить медиа", callback_data=f"wictory:invalid:edit_media:{int(item_id)}")
    b.button(text="Вернуть в общий пул", callback_data=f"wictory:invalid:return:{int(item_id)}")
    b.button(text="⬅️ Назад", callback_data="wictory:invalid:list")
    b.adjust(1)
    return b.as_markup()


def kb_wictory_stats_main() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Фильтр", callback_data="wictory:stats:filters")
    b.button(text="⬅️ Назад", callback_data="wictory:home")
    b.adjust(2)
    return b.as_markup()


def kb_wictory_stats_filters_main() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Источник", callback_data="wictory:stats:filters:source")
    b.button(text="Банк", callback_data="wictory:stats:filters:bank")
    b.button(text="Дата", callback_data="wictory:stats:filters:date")
    b.button(text="Статус", callback_data="wictory:stats:filters:status")
    b.button(text="Тип", callback_data="wictory:stats:filters:type")
    b.button(text="🔎 Поиск", callback_data="wictory:stats:apply")
    b.button(text="Сбросить", callback_data="wictory:stats:reset")
    b.button(text="⬅️ Назад", callback_data="wictory:stats")
    b.adjust(2, 2, 1, 2, 1)
    return b.as_markup()


def kb_wictory_stats_filter_source(selected: set[str]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for s in ("TG", "FB"):
        mark = "✅ " if s in selected else ""
        b.button(text=f"{mark}{s}", callback_data=f"wictory:stats:toggle:source:{s}")
    b.button(text="⬅️ Назад", callback_data="wictory:stats:filters")
    b.adjust(2, 1)
    return b.as_markup()


def kb_wictory_stats_filter_status(selected: set[str]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for s in ("free", "assigned", "used", "invalid"):
        mark = "✅ " if s in selected else ""
        b.button(text=f"{mark}{s.upper()}", callback_data=f"wictory:stats:toggle:status:{s}")
    b.button(text="⬅️ Назад", callback_data="wictory:stats:filters")
    b.adjust(2, 2, 1)
    return b.as_markup()


def kb_wictory_stats_filter_type(selected: set[str]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for t in ("link", "esim", "link_esim"):
        mark = "✅ " if t in selected else ""
        b.button(text=f"{mark}{t}", callback_data=f"wictory:stats:toggle:type:{t}")
    b.button(text="⬅️ Назад", callback_data="wictory:stats:filters")
    b.adjust(1)
    return b.as_markup()


def kb_wictory_stats_filter_date(current: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for key, title in (("all", "За всё время"), ("today", "Сегодня"), ("7d", "7 дней"), ("30d", "30 дней")):
        mark = "✅ " if current == key else ""
        b.button(text=f"{mark}{title}", callback_data=f"wictory:stats:set_date:{key}")
    b.button(text="⬅️ Назад", callback_data="wictory:stats:filters")
    b.adjust(1)
    return b.as_markup()


def kb_wictory_stats_filter_bank(items: list[tuple[int, str]], selected: set[int]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for bid, title in items[:60]:
        mark = "✅ " if int(bid) in selected else ""
        b.button(text=f"{mark}{title}", callback_data=f"wictory:stats:toggle:bank:{int(bid)}")
    b.button(text="⬅️ Назад", callback_data="wictory:stats:filters")
    b.adjust(1)
    return b.as_markup()


def kb_wictory_items_list(items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for item_id, title in items[:50]:
        b.button(text=title, callback_data=f"wictory:item:open:{int(item_id)}")
    b.button(text="⬅️ Назад", callback_data="wictory:home")
    b.adjust(1)
    return b.as_markup()


def kb_wictory_item_actions(item_id: int, *, can_edit_data: bool, can_edit_media: bool, can_delete: bool, can_edit_meta: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if can_edit_data:
        b.button(text="Редактировать ссылку", callback_data=f"wictory:item:edit_data:{int(item_id)}")
    if can_edit_media:
        b.button(text="Редактировать Esim", callback_data=f"wictory:item:edit_media:{int(item_id)}")
    if can_edit_meta:
        b.button(text="Изменить банк", callback_data=f"wictory:item:edit_bank:{int(item_id)}")
        b.button(text="Изменить источник", callback_data=f"wictory:item:edit_source:{int(item_id)}")
    if can_delete:
        b.button(text="🗑 Удалить", callback_data=f"wictory:item:delete:{int(item_id)}")
    b.button(text="⬅️ Назад", callback_data="wictory:items:list")
    b.adjust(1)
    return b.as_markup()


def kb_wictory_item_edit_back_cancel(item_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data=f"wictory:item:open:{int(item_id)}")
    b.button(text="❌ Отмена", callback_data="wictory:item:cancel_edit")
    b.adjust(2)
    return b.as_markup()


def kb_wictory_invalid_edit_back_cancel(item_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data=f"wictory:invalid:open:{int(item_id)}")
    b.button(text="❌ Отмена", callback_data="wictory:item:cancel_edit")
    b.adjust(2)
    return b.as_markup()


def kb_wictory_item_pick_source(item_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="TG", callback_data=f"wictory:item:set_source:{int(item_id)}:TG")
    b.button(text="FB", callback_data=f"wictory:item:set_source:{int(item_id)}:FB")
    b.button(text="⬅️ Назад", callback_data=f"wictory:item:open:{int(item_id)}")
    b.adjust(2, 1)
    return b.as_markup()


def kb_wictory_item_banks(items: list[tuple[int, str]], *, item_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for bank_id, name in items[:50]:
        b.button(text=name, callback_data=f"wictory:item:set_bank:{int(item_id)}:{int(bank_id)}")
    b.button(text="⬅️ Назад", callback_data=f"wictory:item:open:{int(item_id)}")
    b.adjust(1)
    return b.as_markup()


def kb_wictory_item_media_manage(item_id: int, files_count: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for i in range(max(0, int(files_count))):
        b.button(text=f"Заменить файл {i+1}", callback_data=f"wictory:item:media_pick:{int(item_id)}:{i}")
    b.button(text="➕ Добавить файл", callback_data=f"wictory:item:media_add:{int(item_id)}")
    b.button(text="✅ Готово", callback_data="wictory:upload_done")
    b.button(text="⬅️ Назад", callback_data=f"wictory:item:open:{int(item_id)}")
    b.button(text="❌ Отмена", callback_data="wictory:item:cancel_edit")
    b.adjust(1)
    return b.as_markup()


def kb_pending_main() -> ReplyKeyboardMarkup:
    b = ReplyKeyboardBuilder()
    b.add(KeyboardButton(text="Запросить доступ"))
    b.adjust(1)
    return b.as_markup(resize_keyboard=True)


def kb_team_lead_inline_main(*, live_count: int | None = None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    suffix = f" ({int(live_count)})" if live_count is not None else ""
    b.button(text=f"Лайв анкеты{suffix}", callback_data=TeamLeadMenuCb(action="live").pack())
    b.button(text="Условия для сдачи", callback_data=TeamLeadMenuCb(action="banks").pack())
    b.button(text="Дубликаты", callback_data=TeamLeadMenuCb(action="duplicates").pack())
    b.button(text="Пользователи", callback_data=TeamLeadMenuCb(action="users").pack())
    b.adjust(2, 1, 1)
    return b.as_markup()


def kb_tl_duplicate_filter_menu(*, current: str | None) -> InlineKeyboardMarkup:
    cur = (current or "today").lower()
    b = InlineKeyboardBuilder()
    items = [
        ("Сегодня", "today"),
        ("Вчера", "yesterday"),
        ("Текущая неделя", "week"),
        ("Последние 7 дней", "last7"),
        ("Текущий месяц", "month"),
        ("Предыдущий месяц", "prev_month"),
        ("Последние 30 дней", "last30"),
        ("Текущий год", "year"),
        ("За все время", "all"),
    ]
    for title, key in items:
        prefix = "✅ " if key == cur else ""
        b.button(text=f"{prefix}{title}", callback_data=f"tl:dup_filter_set:{key}")
    b.button(text="Интервал дат", callback_data="tl:dup_filter_custom")
    b.button(text="⬅️ Назад", callback_data=TeamLeadMenuCb(action="duplicates").pack())
    b.adjust(1)
    return b.as_markup()


def kb_tl_duplicates_list() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📅 Фильтр", callback_data="tl:dup_filter")
    b.button(text="🏠 Меню", callback_data=TeamLeadMenuCb(action="home").pack())
    b.adjust(2)
    return b.as_markup()


def kb_tl_duplicate_notice() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Перейти", callback_data="tl:dup_notice_open")
    b.adjust(1)
    return b.as_markup()


def kb_tl_reject_back_inline() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data="tl:reject_back")
    b.adjust(1)
    return b.as_markup()


def kb_banks_list(bank_items: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for bank_id, name in bank_items:
        b.button(text=name, callback_data=BankCb(action="open", bank_id=bank_id).pack())
    b.button(text="Создать условия", callback_data=BankCb(action="create", bank_id=None).pack())
    b.button(text="Назад", callback_data=TeamLeadMenuCb(action="home").pack())
    # One button per row looks cleaner and "full-width" in Telegram clients
    b.adjust(1)
    return b.as_markup()


def kb_bank_open(bank_id: int, *, has_conditions: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if has_conditions:
        b.button(text="Редактировать", callback_data=BankCb(action="edit", bank_id=bank_id).pack())
    else:
        b.button(text="Создать условия", callback_data=BankCb(action="setup", bank_id=bank_id).pack())
    b.button(text="🗑 Удалить банк", callback_data=BankEditCb(action="delete", bank_id=bank_id).pack())
    b.button(text="Назад", callback_data=TeamLeadMenuCb(action="banks").pack())
    b.adjust(1)
    return b.as_markup()


def kb_bank_edit(bank_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="Текст условий (TG)", callback_data=BankEditCb(action="instructions_tg", bank_id=bank_id).pack())
    b.button(text="Текст условий (FB)", callback_data=BankEditCb(action="instructions_fb", bank_id=bank_id).pack())
    b.button(text="Кол-во скринов (TG)", callback_data=BankEditCb(action="required_tg", bank_id=bank_id).pack())
    b.button(text="Кол-во скринов (FB)", callback_data=BankEditCb(action="required_fb", bank_id=bank_id).pack())
    b.button(text="Назад", callback_data=BankEditCb(action="back", bank_id=bank_id).pack())
    b.adjust(1)
    return b.as_markup()


def kb_bank_edit_for_source(bank_id: int, *, source: str) -> InlineKeyboardMarkup:
    src = (source or "TG").upper()
    b = InlineKeyboardBuilder()
    b.button(text="Название банка", callback_data=BankEditCb(action="rename", bank_id=bank_id).pack())
    if src == "FB":
        b.button(text="Текст условий (FB)", callback_data=BankEditCb(action="instructions_fb", bank_id=bank_id).pack())
        b.button(text="Кол-во скринов (FB)", callback_data=BankEditCb(action="required_fb", bank_id=bank_id).pack())
    else:
        b.button(text="Текст условий (TG)", callback_data=BankEditCb(action="instructions_tg", bank_id=bank_id).pack())
        b.button(text="Кол-во скринов (TG)", callback_data=BankEditCb(action="required_tg", bank_id=bank_id).pack())
    b.button(text="🗑 Удалить банк", callback_data=BankEditCb(action="delete", bank_id=bank_id).pack())
    b.button(text="Назад", callback_data=BankEditCb(action="back", bank_id=bank_id).pack())
    b.adjust(1)
    return b.as_markup()


