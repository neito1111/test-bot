from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards import (
    kb_wictory_back_cancel,
    kb_wictory_banks,
    kb_wictory_edit,
    kb_wictory_invalid_actions,
    kb_wictory_invalid_list,
    kb_wictory_item_actions,
    kb_wictory_item_edit_back_cancel,
    kb_wictory_items_list,
    kb_wictory_main_inline,
    kb_wictory_pick_source,
    kb_wictory_preview,
)
from bot.middlewares import GroupMessageFilter
from bot.models import ResourceStatus, UserRole
from bot.repositories import (
    create_resource_pool_item,
    get_bank,
    get_pool_item,
    get_user_by_tg_id,
    list_banks,
    list_invalid_pool_items_for_wictory,
    list_pool_stats_by_bank,
    list_wictory_pool_items,
    wictory_delete_item,
    wictory_update_invalid_item,
    wictory_update_item,
)
from bot.states import WictoryStates
from bot.utils import pack_media_item


async def _list_banks_for_source(session: AsyncSession, source: str | None) -> list:
    src = (source or "TG").upper()
    banks = await list_banks(session)

    def _has_fb(bank) -> bool:
        return bool((getattr(bank, "instructions_fb", None) or "").strip()) or getattr(bank, "required_screens_fb", None) is not None

    def _has_tg(bank) -> bool:
        return bool((getattr(bank, "instructions_tg", None) or "").strip()) or getattr(bank, "required_screens_tg", None) is not None

    def _has_legacy(bank) -> bool:
        return bool((getattr(bank, "instructions", None) or "").strip()) or getattr(bank, "required_screens", None) is not None

    if src == "FB":
        picked = [b for b in banks if _has_fb(b) or (_has_legacy(b) and not _has_tg(b))]
        if picked:
            return picked
        return [b for b in banks if _has_fb(b) or not _has_tg(b)]

    picked = [b for b in banks if _has_tg(b) or (_has_legacy(b) and not _has_fb(b))]
    if picked:
        return picked
    return [b for b in banks if _has_tg(b) or not _has_fb(b)]


def _bank_items_with_source(banks: list, source: str | None) -> list[tuple[int, str]]:
    src = (source or "TG").upper()
    suffix = "FB" if src == "FB" else "TG"
    out: list[tuple[int, str]] = []
    for b in banks:
        nm = str(getattr(b, "name", "") or "").strip()
        if not nm:
            continue
        out.append((int(b.id), f"{nm} ({suffix})"))
    return out

router = Router(name="wictory")
router.message.filter(GroupMessageFilter())


def _status_icon(status: str) -> str:
    s = (status or "").lower()
    return {
        "free": "🟢",
        "assigned": "🟡",
        "used": "✅",
        "invalid": "🔴",
    }.get(s, "⚪")


def _render_preview(data: dict) -> str:
    rtype = str(data.get("resource_type") or "")
    bank_name = data.get("bank_name") or "—"
    link = data.get("text_data") or "—"
    screens = data.get("screenshots") or []

    type_ru = {
        "link": "Ссылка",
        "esim": "Esim",
        "link_esim": "Ссылка + Esim",
    }.get(rtype, rtype or "—")

    lines = [
        "<b>Готовый запрос</b>",
        "",
        f"Источник: <b>{data.get('resource_source') or 'TG'}</b>",
        f"Тип: <b>{type_ru}</b>",
        f"Банк: <b>{bank_name}</b>",
    ]

    if rtype in {"link", "link_esim"}:
        lines.append(f"Ссылка: <code>{link}</code>")
    if rtype in {"esim", "link_esim"}:
        lines.append(f"Файлов Esim: <b>{len(screens)}</b>")

    return "\n".join(lines)


async def _wictory_guard(cq_or_msg: CallbackQuery | Message, session: AsyncSession):
    u = cq_or_msg.from_user
    if not u:
        return None
    user = await get_user_by_tg_id(session, int(u.id))
    if not user or user.role != UserRole.WICTORY:
        return None
    return user


@router.callback_query(F.data == "wictory:home")
async def wictory_home(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    await state.clear()
    await cq.answer()
    if cq.message:
        await cq.message.edit_text("Меню <b>WICTORY</b>", reply_markup=kb_wictory_main_inline())


@router.callback_query(F.data == "wictory:cancel_create")
async def wictory_cancel_create(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    await state.clear()
    await cq.answer("Создание отменено")
    if cq.message:
        await cq.message.edit_text("Меню <b>WICTORY</b>", reply_markup=kb_wictory_main_inline())


@router.callback_query(F.data.startswith("wictory:back:"))
async def wictory_back(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    stage = (cq.data or "").split(":", 2)[-1]
    data = await state.get_data()
    rtype = str(data.get("resource_type") or "")

    if stage == "home":
        await state.clear()
        await cq.answer()
        if cq.message:
            await cq.message.edit_text("Меню <b>WICTORY</b>", reply_markup=kb_wictory_main_inline())
        return

    if stage == "bank":
        await cq.answer()
        if cq.message:
            await cq.message.edit_text("Выберите источник для этой записи:", reply_markup=kb_wictory_pick_source(back_cb="wictory:back:home"))
        return

    if stage == "upload":
        await state.set_state(WictoryStates.upload_screenshot)
        await cq.answer()
        if cq.message:
            await cq.message.edit_text(
                "Отправьте файл Esim (фото/док/видео), до 10 шт., затем напишите 'Готово'",
                reply_markup=kb_wictory_back_cancel(back_cb="wictory:back:bank"),
            )
        return

    if stage == "data":
        await state.set_state(WictoryStates.enter_data)
        await cq.answer()
        if cq.message:
            await cq.message.edit_text(
                "Введите ссылку",
                reply_markup=kb_wictory_back_cancel(back_cb=("wictory:back:upload" if rtype == "link_esim" else "wictory:back:bank")),
            )
        return

    if stage == "preview":
        await state.set_state(WictoryStates.preview)
        await cq.answer()
        if cq.message:
            await cq.message.edit_text(_render_preview(data), reply_markup=kb_wictory_preview())
        return


@router.callback_query(F.data.startswith("wictory:add:"))
async def wictory_add_start(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    resource_type = (cq.data or "").split(":")[-1]
    await state.clear()
    await state.update_data(resource_type=resource_type)
    await cq.answer()
    if cq.message:
        await cq.message.edit_text("Выберите источник для этой записи:", reply_markup=kb_wictory_pick_source(back_cb="wictory:back:home"))


@router.callback_query(F.data.startswith("wictory:src:"))
async def wictory_pick_source(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    src = (cq.data or "").split(":")[-1].upper()
    if src not in {"TG", "FB"}:
        await cq.answer("Некорректный источник", show_alert=True)
        return
    banks = await _list_banks_for_source(session, src)
    items = _bank_items_with_source(banks, src)
    await state.set_state(WictoryStates.pick_bank)
    await state.update_data(resource_source=src)
    await cq.answer()
    if cq.message:
        await cq.message.edit_text(f"Источник: <b>{src}</b>\nВыберите банк:", reply_markup=kb_wictory_banks(items, back_cb="wictory:back:home"))


@router.callback_query(WictoryStates.pick_bank, F.data.startswith("wictory:bank:"))
async def wictory_pick_bank(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    bank_id = int((cq.data or "").split(":")[-1])
    bank = await get_bank(session, bank_id)
    if not bank:
        await cq.answer("Банк не найден", show_alert=True)
        return
    data = await state.get_data()
    rtype = data.get("resource_type")
    await state.update_data(bank_id=bank_id, bank_name=bank.name)
    if rtype in {"esim", "link_esim"}:
        await state.set_state(WictoryStates.upload_screenshot)
        await cq.answer()
        if cq.message:
            await cq.message.edit_text(
                "Отправьте файл Esim (фото/док/видео), до 10 шт., затем напишите 'Готово'",
                reply_markup=kb_wictory_back_cancel(back_cb="wictory:back:bank"),
            )
        return
    await state.set_state(WictoryStates.enter_data)
    await cq.answer()
    if cq.message:
        await cq.message.edit_text("Введите ссылку", reply_markup=kb_wictory_back_cancel(back_cb="wictory:back:bank"))


@router.message(WictoryStates.upload_screenshot, F.photo | F.document | F.video)
async def wictory_upload_screenshot(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(message, session)
    if not user:
        return
    data = await state.get_data()
    shots: list[str] = list(data.get("screenshots") or [])
    if len(shots) >= 10:
        await message.answer("Можно добавить максимум 10 файлов. Напишите 'Готово'.")
        return
    if message.photo:
        shots.append(pack_media_item("photo", message.photo[-1].file_id))
    elif message.document:
        shots.append(pack_media_item("doc", message.document.file_id))
    elif message.video:
        shots.append(pack_media_item("video", message.video.file_id))
    await state.update_data(screenshots=shots)
    item_edit_id = data.get("item_edit_id")
    kb = kb_wictory_item_edit_back_cancel(int(item_edit_id)) if data.get("item_edit_mode") == "media" and item_edit_id else kb_wictory_back_cancel(back_cb="wictory:back:bank")
    await message.answer(
        f"Принято: {len(shots)}/10. Отправьте ещё файл или напишите 'Готово'.",
        reply_markup=kb,
    )


@router.message(WictoryStates.upload_screenshot, F.text)
async def wictory_upload_screenshot_done(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(message, session)
    if not user:
        return
    if (message.text or "").strip().lower() != "готово":
        data = await state.get_data()
        item_edit_id = data.get("item_edit_id")
        kb = kb_wictory_item_edit_back_cancel(int(item_edit_id)) if data.get("item_edit_mode") == "media" and item_edit_id else kb_wictory_back_cancel(back_cb="wictory:back:bank")
        await message.answer(
            "Отправьте файл (фото/док/видео) или напишите 'Готово'.",
            reply_markup=kb,
        )
        return
    data = await state.get_data()
    shots = list(data.get("screenshots") or [])
    if not shots:
        await message.answer("Нужно добавить хотя бы 1 файл.")
        return
    if data.get("invalid_edit_mode") == "media" and data.get("invalid_item_id"):
        await wictory_update_invalid_item(
            session,
            item_id=int(data.get("invalid_item_id")),
            wictory_user_id=int(user.id),
            screenshots=shots,
        )
        await state.clear()
        await message.answer("Медиа обновлены", reply_markup=kb_wictory_main_inline())
        return

    if data.get("item_edit_mode") == "media" and data.get("item_edit_id"):
        await wictory_update_item(
            session,
            item_id=int(data.get("item_edit_id")),
            wictory_user_id=int(user.id),
            screenshots=shots,
        )
        await state.clear()
        await message.answer("Esim обновлены", reply_markup=kb_wictory_main_inline())
        return

    rtype = str(data.get("resource_type") or "")
    if rtype == "esim":
        await state.set_state(WictoryStates.preview)
        data = await state.get_data()
        await message.answer(_render_preview(data), reply_markup=kb_wictory_preview())
        return

    await state.set_state(WictoryStates.enter_data)
    await message.answer("Введите ссылку", reply_markup=kb_wictory_back_cancel(back_cb="wictory:back:bank"))


@router.message(WictoryStates.enter_data, F.text)
async def wictory_enter_data(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(message, session)
    if not user:
        return
    data = await state.get_data()
    txt = (message.text or "").strip()
    if data.get("invalid_edit_mode") == "data" and data.get("invalid_item_id"):
        await wictory_update_invalid_item(
            session,
            item_id=int(data.get("invalid_item_id")),
            wictory_user_id=int(user.id),
            text_data=txt,
        )
        await state.clear()
        await message.answer("Данные обновлены", reply_markup=kb_wictory_main_inline())
        return

    if data.get("item_edit_mode") == "data" and data.get("item_edit_id"):
        await wictory_update_item(
            session,
            item_id=int(data.get("item_edit_id")),
            wictory_user_id=int(user.id),
            text_data=txt,
        )
        await state.clear()
        await message.answer("Ссылка обновлена", reply_markup=kb_wictory_main_inline())
        return

    rtype = str(data.get("resource_type") or "")
    if rtype in {"link", "link_esim"}:
        if not txt:
            await message.answer("Ссылка обязательна. Введите ссылку.")
            return

    await state.update_data(text_data=txt)
    await state.set_state(WictoryStates.preview)
    data = await state.get_data()
    await message.answer(_render_preview(data), reply_markup=kb_wictory_preview())


@router.callback_query(WictoryStates.preview, F.data == "wictory:edit")
async def wictory_edit(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    await state.set_state(WictoryStates.edit_pick)
    await cq.answer()
    if cq.message:
        await cq.message.edit_reply_markup(reply_markup=kb_wictory_edit())


@router.callback_query(WictoryStates.edit_pick, F.data.startswith("wictory:edit:"))
async def wictory_edit_pick(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    action = (cq.data or "").split(":")[-1]
    data = await state.get_data()
    if action == "bank":
        src = str(data.get("resource_source") or "TG")
        banks = await _list_banks_for_source(session, src)
        items = _bank_items_with_source(banks, src)
        await state.set_state(WictoryStates.pick_bank)
        await cq.answer()
        if cq.message:
            await cq.message.edit_text("Выберите банк:", reply_markup=kb_wictory_banks(items, back_cb="wictory:preview"))
        return
    if action == "data":
        await state.set_state(WictoryStates.enter_data)
        await cq.answer()
        if cq.message:
            await cq.message.edit_text("Введите нужные данные")
        return
    if action == "screen":
        await state.set_state(WictoryStates.upload_screenshot)
        await cq.answer()
        if cq.message:
            await cq.message.edit_text("Отправьте скриншот")
        return
    await state.set_state(WictoryStates.preview)
    await cq.answer()
    if cq.message:
        await cq.message.edit_text(_render_preview(data), reply_markup=kb_wictory_preview())


@router.callback_query(F.data == "wictory:preview")
async def wictory_preview_show(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    data = await state.get_data()
    await state.set_state(WictoryStates.preview)
    await cq.answer()
    if cq.message:
        await cq.message.edit_text(_render_preview(data), reply_markup=kb_wictory_preview())


@router.callback_query(WictoryStates.preview, F.data == "wictory:confirm")
async def wictory_confirm(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    data = await state.get_data()
    if not data.get("bank_id"):
        await cq.answer("Не выбран банк", show_alert=True)
        return
    resource_type = str(data.get("resource_type") or "link")
    if resource_type not in {"link", "esim", "link_esim"}:
        await cq.answer("Некорректный тип ресурса", show_alert=True)
        return
    await create_resource_pool_item(
        session,
        source=(data.get("resource_source") or getattr(user, "manager_source", None) or "TG"),
        bank_id=int(data["bank_id"]),
        resource_type=resource_type,
        text_data=data.get("text_data"),
        screenshots=list(data.get("screenshots") or []),
        created_by_user_id=int(user.id),
    )
    await state.clear()
    await cq.answer("Сохранено")
    if cq.message:
        await cq.message.edit_text("✅ Запись добавлена в пул", reply_markup=kb_wictory_main_inline())


@router.callback_query(F.data == "wictory:invalid:list")
async def wictory_invalid_list(cq: CallbackQuery, session: AsyncSession) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    items = await list_invalid_pool_items_for_wictory(session, wictory_user_id=int(user.id))
    packed: list[tuple[int, str]] = []
    for it in items:
        bank = await get_bank(session, int(it.bank_id))
        packed.append((int(it.id), f"{bank.name if bank else '—'} | {getattr(it.type, 'value', '—')}"))
    await cq.answer()
    if cq.message:
        if not packed:
            await cq.message.edit_text("Невалидных записей нет", reply_markup=kb_wictory_main_inline())
            return
        await cq.message.edit_text("Невалидные записи:", reply_markup=kb_wictory_invalid_list(packed))


@router.callback_query(F.data.startswith("wictory:invalid:open:"))
async def wictory_invalid_open(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    item_id = int((cq.data or "").split(":")[-1])
    it = await get_pool_item(session, item_id)
    if not it or int(it.created_by_user_id) != int(user.id) or it.status != ResourceStatus.INVALID:
        await cq.answer("Запись не найдена", show_alert=True)
        return
    bank = await get_bank(session, int(it.bank_id))
    txt = (
        f"<b>Невалидная запись</b>\n"
        f"Банк: <b>{bank.name if bank else '—'}</b>\n"
        f"Тип: <b>{getattr(it.type, 'value', '—')}</b>\n"
        f"Данные: <code>{it.text_data or '—'}</code>\n"
        f"Комментарий DM: {it.invalid_comment or '—'}"
    )
    await state.update_data(invalid_item_id=item_id)
    await cq.answer()
    if cq.message:
        await cq.message.edit_text(txt, reply_markup=kb_wictory_invalid_actions(item_id))


@router.callback_query(F.data.startswith("wictory:invalid:edit_data:"))
async def wictory_invalid_edit_data_start(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    item_id = int((cq.data or "").split(":")[-1])
    await state.update_data(invalid_item_id=item_id, invalid_edit_mode="data")
    await state.set_state(WictoryStates.enter_data)
    await cq.answer()
    if cq.message:
        await cq.message.edit_text("Введите новые данные")


@router.callback_query(F.data.startswith("wictory:invalid:edit_media:"))
async def wictory_invalid_edit_media_start(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    item_id = int((cq.data or "").split(":")[-1])
    await state.update_data(invalid_item_id=item_id, invalid_edit_mode="media", screenshots=[])
    await state.set_state(WictoryStates.upload_screenshot)
    await cq.answer()
    if cq.message:
        await cq.message.edit_text("Отправьте файлы (до 10), затем напишите 'Готово'")


@router.callback_query(F.data.startswith("wictory:invalid:return:"))
async def wictory_invalid_return(cq: CallbackQuery, session: AsyncSession) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    item_id = int((cq.data or "").split(":")[-1])
    it = await wictory_update_invalid_item(session, item_id=item_id, wictory_user_id=int(user.id), set_free=True)
    await cq.answer("Возвращено в общий пул" if it else "Не удалось", show_alert=not bool(it))
    if cq.message:
        await cq.message.edit_text("Запись возвращена в общий пул", reply_markup=kb_wictory_main_inline())


@router.callback_query(F.data == "wictory:items:list")
async def wictory_items_list(cq: CallbackQuery, session: AsyncSession) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    items = await list_wictory_pool_items(session, wictory_user_id=int(user.id), limit=100)
    packed: list[tuple[int, str]] = []
    for it in items:
        bank = await get_bank(session, int(it.bank_id))
        st = str(getattr(it.status, 'value', '—'))
        icon = _status_icon(st)
        packed.append((
            int(it.id),
            f"{icon} #{int(it.id)} {it.source} | {bank.name if bank else '—'} | {getattr(it.type, 'value', '—')}",
        ))
    await cq.answer()
    if cq.message:
        if not packed:
            await cq.message.edit_text("У вас пока нет записей", reply_markup=kb_wictory_main_inline())
            return
        legend = (
            "<b>Мои записи</b>\n"
            "<blockquote expandable>"
            "Расшифровка статусов:\n"
            "🟢 free — свободна, можно выдавать DM\n"
            "🟡 assigned — сейчас в работе у DM\n"
            "✅ used — уже использована\n"
            "🔴 invalid — помечена невалидной"
            "</blockquote>"
        )
        await cq.message.edit_text(legend, reply_markup=kb_wictory_items_list(packed))


@router.callback_query(F.data.startswith("wictory:item:open:"))
async def wictory_item_open(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    item_id = int((cq.data or "").split(":")[-1])
    it = await get_pool_item(session, item_id)
    if not it or int(it.created_by_user_id) != int(user.id):
        await cq.answer("Запись не найдена", show_alert=True)
        return
    bank = await get_bank(session, int(it.bank_id))
    txt = (
        f"<b>Запись #{int(it.id)}</b>\n"
        f"Источник: <b>{it.source}</b>\n"
        f"Банк: <b>{bank.name if bank else '—'}</b>\n"
        f"Тип: <b>{getattr(it.type, 'value', '—')}</b>\n"
        f"Статус: <b>{getattr(it.status, 'value', '—')}</b>\n"
        f"Ссылка: <code>{it.text_data or '—'}</code>\n"
        f"Esim файлов: <b>{len(list(it.screenshots or []))}</b>"
    )
    can_data = getattr(it.type, "value", "") in {"link", "link_esim"}
    can_media = getattr(it.type, "value", "") in {"esim", "link_esim"}
    can_delete = getattr(it.status, "value", "") != "assigned"
    await state.update_data(item_edit_id=item_id)
    await cq.answer()
    if cq.message:
        await cq.message.edit_text(
            txt,
            reply_markup=kb_wictory_item_actions(item_id, can_edit_data=can_data, can_edit_media=can_media, can_delete=can_delete),
        )


@router.callback_query(F.data.startswith("wictory:item:edit_data:"))
async def wictory_item_edit_data_start(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    item_id = int((cq.data or "").split(":")[-1])
    it = await get_pool_item(session, item_id)
    if not it or int(it.created_by_user_id) != int(user.id):
        await cq.answer("Запись не найдена", show_alert=True)
        return
    await state.update_data(item_edit_id=item_id, item_edit_mode="data")
    await state.set_state(WictoryStates.enter_data)
    await cq.answer()
    if cq.message:
        await cq.message.edit_text("Введите новую ссылку", reply_markup=kb_wictory_item_edit_back_cancel(item_id))


@router.callback_query(F.data.startswith("wictory:item:edit_media:"))
async def wictory_item_edit_media_start(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    item_id = int((cq.data or "").split(":")[-1])
    it = await get_pool_item(session, item_id)
    if not it or int(it.created_by_user_id) != int(user.id):
        await cq.answer("Запись не найдена", show_alert=True)
        return
    await state.update_data(item_edit_id=item_id, item_edit_mode="media", screenshots=[])
    await state.set_state(WictoryStates.upload_screenshot)
    await cq.answer()
    if cq.message:
        await cq.message.edit_text("Отправьте новые файлы Esim (до 10), затем напишите 'Готово'", reply_markup=kb_wictory_item_edit_back_cancel(item_id))


@router.callback_query(F.data.startswith("wictory:item:delete:"))
async def wictory_item_delete(cq: CallbackQuery, session: AsyncSession) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    item_id = int((cq.data or "").split(":")[-1])
    ok = await wictory_delete_item(session, item_id=item_id, wictory_user_id=int(user.id))
    await cq.answer("Удалено" if ok else "Нельзя удалить (в работе или не найдено)", show_alert=not ok)
    if cq.message:
        await cq.message.edit_text("Готово", reply_markup=kb_wictory_main_inline())


@router.callback_query(F.data == "wictory:item:cancel_edit")
async def wictory_item_cancel_edit(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    await state.clear()
    await cq.answer("Редактирование отменено")
    if cq.message:
        await cq.message.edit_text("Меню <b>WICTORY</b>", reply_markup=kb_wictory_main_inline())


@router.callback_query(F.data == "wictory:stats")
async def wictory_stats(cq: CallbackQuery, session: AsyncSession) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    src = (getattr(user, "manager_source", None) or "TG")
    stats = await list_pool_stats_by_bank(session, source=src)

    total_link = total_esim = total_combo = 0
    total_free = total_assigned = total_used = total_invalid = 0

    lines = [f"🏦 <b>Пул по банкам</b> · Источник: <b>{src}</b>"]
    shown = 0
    for bank, st in stats:
        if int(st.get("total", 0)) <= 0:
            continue
        shown += 1
        total_link += int(st.get("link", 0))
        total_esim += int(st.get("esim", 0))
        total_combo += int(st.get("link_esim", 0))
        total_free += int(st.get("status_free", 0))
        total_assigned += int(st.get("status_assigned", 0))
        total_used += int(st.get("status_used", 0))
        total_invalid += int(st.get("status_invalid", 0))

        lines.extend([
            "",
            f"<b>{shown}. {bank.name}</b>",
            f"• Типы: 🔗 {st['link']} | 📱 {st['esim']} | 🔗+📱 {st['link_esim']}",
            f"• Статусы: 🟢 free {st['status_free']} | 🟡 in work {st['status_assigned']} | ✅ used {st['status_used']} | 🔴 invalid {st['status_invalid']}",
        ])

    if shown == 0:
        lines.append("\nПул пуст.")
    else:
        lines.extend([
            "",
            "━━━━━━━━━━━━━━",
            "<b>ИТОГО</b>",
            f"Типы: 🔗 {total_link} | 📱 {total_esim} | 🔗+📱 {total_combo}",
            f"Статусы: 🟢 {total_free} | 🟡 {total_assigned} | ✅ {total_used} | 🔴 {total_invalid}",
        ])

    await cq.answer()
    if cq.message:
        await cq.message.edit_text("\n".join(lines), reply_markup=kb_wictory_main_inline())
