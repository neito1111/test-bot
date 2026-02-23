from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards import (
    kb_wictory_banks,
    kb_wictory_edit,
    kb_wictory_invalid_actions,
    kb_wictory_invalid_list,
    kb_wictory_main_inline,
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
    wictory_update_invalid_item,
)
from bot.states import WictoryStates
from bot.utils import pack_media_item

router = Router(name="wictory")
router.message.filter(GroupMessageFilter())


def _render_preview(data: dict) -> str:
    rtype = data.get("resource_type")
    bank_name = data.get("bank_name") or "—"
    text_data = data.get("text_data") or "—"
    screens = data.get("screenshots") or []
    return (
        "<b>Готовый запрос</b>\n\n"
        f"Тип: <b>{rtype}</b>\n"
        f"Банк: <b>{bank_name}</b>\n"
        f"Данные: <code>{text_data}</code>\n"
        f"Скриншотов: <b>{len(screens)}</b>"
    )


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


@router.callback_query(F.data.startswith("wictory:add:"))
async def wictory_add_start(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    resource_type = (cq.data or "").split(":")[-1]
    banks = await list_banks(session)
    items = [(int(b.id), b.name) for b in banks]
    await state.clear()
    await state.set_state(WictoryStates.pick_bank)
    await state.update_data(resource_type=resource_type)
    await cq.answer()
    if cq.message:
        await cq.message.edit_text("Выберите банк:", reply_markup=kb_wictory_banks(items))


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
            await cq.message.edit_text("Отправьте скриншот")
        return
    await state.set_state(WictoryStates.enter_data)
    await cq.answer()
    if cq.message:
        await cq.message.edit_text("Введите нужные данные")


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
    await message.answer(f"Принято: {len(shots)}/10. Отправьте ещё файл или напишите 'Готово'.")


@router.message(WictoryStates.upload_screenshot, F.text)
async def wictory_upload_screenshot_done(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(message, session)
    if not user:
        return
    if (message.text or "").strip().lower() != "готово":
        await message.answer("Отправьте файл (фото/док/видео) или напишите 'Готово'.")
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
    await state.set_state(WictoryStates.enter_data)
    await message.answer("Введите нужные данные")


@router.message(WictoryStates.enter_data, F.text)
async def wictory_enter_data(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(message, session)
    if not user:
        return
    data = await state.get_data()
    txt = (message.text or "").strip()
    if data.get("invalid_edit_mode") == "data" and data.get("invalid_item_id"):
        it = await wictory_update_invalid_item(
            session,
            item_id=int(data.get("invalid_item_id")),
            wictory_user_id=int(user.id),
            text_data=txt,
        )
        await state.clear()
        await message.answer("Данные обновлены", reply_markup=kb_wictory_main_inline())
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
        banks = await list_banks(session)
        items = [(int(b.id), b.name) for b in banks]
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
        source=(getattr(user, "manager_source", None) or "TG"),
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


@router.callback_query(F.data == "wictory:stats")
async def wictory_stats(cq: CallbackQuery, session: AsyncSession) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    stats = await list_pool_stats_by_bank(session, source=(getattr(user, "manager_source", None) or "TG"))
    lines = ["<b>Пул по банкам</b>\n"]
    for bank, st in stats:
        lines.append(
            f"• <b>{bank.name}</b>: "
            f"ссылки={st['link']}, esim={st['esim']}, esim/ссылка={st['link_esim']} | "
            f"free={st['status_free']}, assigned={st['status_assigned']}, used={st['status_used']}, invalid={st['status_invalid']}"
        )
    await cq.answer()
    if cq.message:
        await cq.message.edit_text("\n".join(lines), reply_markup=kb_wictory_main_inline())
