from __future__ import annotations

from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InputMediaDocument, InputMediaPhoto, InputMediaVideo, Message
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy.ext.asyncio import AsyncSession

from bot.keyboards import (
    kb_wictory_back_cancel,
    kb_wictory_banks,
    kb_wictory_bank_actions,
    kb_wictory_bulk_next_actions,
    kb_wictory_edit,
    kb_wictory_invalid_actions,
    kb_wictory_invalid_edit_back_cancel,
    kb_wictory_invalid_list,
    kb_wictory_item_actions,
    kb_wictory_item_banks,
    kb_wictory_item_edit_back_cancel,
    kb_wictory_item_media_manage,
    kb_wictory_item_pick_source,
    kb_wictory_items_list,
    kb_wictory_main_inline,
    kb_wictory_pick_source,
    kb_wictory_preview,
    kb_wictory_stats_filter_bank,
    kb_wictory_stats_filter_date,
    kb_wictory_stats_filter_source,
    kb_wictory_stats_filter_status,
    kb_wictory_stats_filter_type,
    kb_wictory_stats_filters_main,
    kb_wictory_stats_main,
    kb_wictory_upload_actions,
)
from bot.middlewares import GroupMessageFilter
from bot.models import ResourceStatus, UserRole
from bot.repositories import (
    create_resource_pool_item,
    get_bank,
    get_pool_item,
    get_user_by_id,
    get_user_by_tg_id,
    list_banks,
    list_invalid_pool_items_for_wictory,
    list_pool_items_filtered,
    list_wictory_pool_items,
    wictory_delete_item,
    wictory_update_invalid_item,
    wictory_update_item,
)
from bot.states import WictoryStates
from bot.utils import pack_media_item, unpack_media_item


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


def _source_label(src: str | None) -> str:
    s = (src or "TG").upper()
    if s == "ALL":
        return "Общее"
    return s


def _norm_bank_name(name: str | None) -> str:
    return " ".join((name or "").strip().lower().split())

router = Router(name="wictory")
router.message.filter(GroupMessageFilter())


def _preview_caption(data: dict) -> str:
    text = _render_preview(data)
    shots = list(data.get("screenshots") or [])
    if len(shots) > 1:
        text += f"\nДоп. файлов: <b>{len(shots) - 1}</b>"
    return text


async def _send_preview_message(msg: Message, data: dict) -> None:
    text = _preview_caption(data)
    shots = list(data.get("screenshots") or [])
    if not shots:
        await msg.answer(text, reply_markup=kb_wictory_preview())
        return

    kind, fid = unpack_media_item(str(shots[0]))
    if kind == "photo":
        await msg.answer_photo(fid, caption=text, parse_mode="HTML", reply_markup=kb_wictory_preview())
    elif kind == "video":
        await msg.answer_video(fid, caption=text, parse_mode="HTML", reply_markup=kb_wictory_preview())
    else:
        await msg.answer_document(fid, caption=text, parse_mode="HTML", reply_markup=kb_wictory_preview())


async def _safe_edit_or_answer(cq: CallbackQuery, text: str, reply_markup=None) -> None:
    if not cq.message:
        return
    try:
        await cq.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest:
        await cq.message.answer(text, reply_markup=reply_markup)


async def _show_preview_from_callback(cq: CallbackQuery, data: dict) -> None:
    if not cq.message:
        return
    shots = list(data.get("screenshots") or [])
    if not shots:
        await _safe_edit_or_answer(cq, _render_preview(data), reply_markup=kb_wictory_preview())
        return

    kind, fid = unpack_media_item(str(shots[0]))
    caption = _preview_caption(data)
    try:
        if kind == "photo":
            await cq.message.edit_media(InputMediaPhoto(media=fid, caption=caption, parse_mode="HTML"), reply_markup=kb_wictory_preview())
        elif kind == "video":
            await cq.message.edit_media(InputMediaVideo(media=fid, caption=caption, parse_mode="HTML"), reply_markup=kb_wictory_preview())
        else:
            await cq.message.edit_media(InputMediaDocument(media=fid, caption=caption, parse_mode="HTML"), reply_markup=kb_wictory_preview())
    except TelegramBadRequest:
        await _send_preview_message(cq.message, data)


def _status_icon(status: str) -> str:
    s = (status or "").lower()
    return {
        "free": "🟡",
        "assigned": "🟢",
        "used": "✅",
        "invalid": "🔴",
    }.get(s, "⚪")


def _resource_ident(item_id: int) -> str:
    return f"RID-{int(item_id)}"


def _split_link_comment(raw: str | None) -> tuple[str | None, str | None]:
    txt = str(raw or "").strip()
    if not txt:
        return None, None
    lines = [x.strip() for x in txt.splitlines() if x.strip()]
    links = [x for x in lines if x.startswith("http://") or x.startswith("https://")]
    comments = [x for x in lines if x not in links]
    link = "\n".join(links).strip() or None
    comment = "\n".join(comments).strip() or None
    return link, comment


def _compose_link_esim_payload(comment: str | None, link: str | None) -> str:
    # Store as a single text_data field: comment lines + blank line + link(s).
    c = (comment or "").strip()
    l = (link or "").strip()
    if c and l:
        return f"{c}\n\n{l}"
    return c or l


def _render_preview(data: dict) -> str:
    rtype = str(data.get("resource_type") or "")
    bank_name = data.get("bank_name") or "—"
    bank_name_fb = data.get("bank_name_fb") or bank_name
    bank_name_tg = data.get("bank_name_tg") or "—"
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
        f"Источник: <b>{_source_label(data.get('resource_source'))}</b>",
        f"Тип: <b>{type_ru}</b>",
    ]
    if str(data.get("resource_source") or "").upper() == "ALL":
        lines.append(f"Банк FB: <b>{bank_name_fb}</b>")
        lines.append(f"Банк TG: <b>{bank_name_tg}</b>")
    else:
        lines.append(f"Банк: <b>{bank_name}</b>")

    if rtype == "link":
        lines.append(f"Ссылка: <code>{link}</code>")
    elif rtype == "esim":
        lines.append(f"Комментарий: <code>{link}</code>" if link != "—" else "Комментарий: —")
    elif rtype == "link_esim":
        lnk, comment = _split_link_comment(link)
        lines.append(f"Комментарий: <code>{comment or '—'}</code>")
        lines.append(f"Ссылка: <code>{lnk or '—'}</code>")

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


async def _pool_item_is_wictory_created(session: AsyncSession, it) -> bool:
    try:
        creator = await get_user_by_id(session, int(getattr(it, "created_by_user_id", 0) or 0))
        return bool(creator and creator.role == UserRole.WICTORY)
    except Exception:
        return False


@router.callback_query(F.data == "wictory:home")
async def wictory_home(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    await state.clear()
    await cq.answer()
    if cq.message:
        try:
            await _safe_edit_or_answer(cq, "Меню <b>WICTORY</b>", reply_markup=kb_wictory_main_inline())
        except TelegramBadRequest:
            await cq.message.answer("Меню <b>WICTORY</b>", reply_markup=kb_wictory_main_inline())


@router.callback_query(F.data == "wictory:cancel_create")
async def wictory_cancel_create(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    await state.clear()
    await cq.answer("Создание отменено")
    if cq.message:
        try:
            await _safe_edit_or_answer(cq, "Меню <b>WICTORY</b>", reply_markup=kb_wictory_main_inline())
        except TelegramBadRequest:
            await cq.message.answer("Меню <b>WICTORY</b>", reply_markup=kb_wictory_main_inline())


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
            try:
                await _safe_edit_or_answer(cq, "Меню <b>WICTORY</b>", reply_markup=kb_wictory_main_inline())
            except TelegramBadRequest:
                await cq.message.answer("Меню <b>WICTORY</b>", reply_markup=kb_wictory_main_inline())
        return

    if stage == "bank_list":
        src = str(data.get("resource_source") or "TG").upper()
        if src == "ALL":
            banks = await _list_banks_for_source(session, "FB")
            items = _bank_items_with_source(banks, "FB")
        else:
            banks = await _list_banks_for_source(session, src)
            items = _bank_items_with_source(banks, src)
        await state.set_state(WictoryStates.pick_bank)
        await cq.answer()
        if cq.message:
            await _safe_edit_or_answer(
                cq,
                f"Источник: <b>{_source_label(src)}</b>\nВыберите банк:",
                reply_markup=kb_wictory_banks(items, back_cb="wictory:back:home"),
            )
        return

    if stage == "bank":
        bank_name = str(data.get("bank_name") or "").strip() or "—"
        await cq.answer()
        if cq.message:
            await _safe_edit_or_answer(
                cq,
                f"Банк: <b>{bank_name}</b>\nВыберите режим добавления:",
                reply_markup=kb_wictory_bank_actions(),
            )
        return

    if stage == "upload":
        await state.set_state(WictoryStates.upload_screenshot)
        await cq.answer()
        if cq.message:
            await _safe_edit_or_answer(cq, 
                "Отправьте файл Esim (фото/док/видео), до 10 шт., затем нажмите '✅ Готово' или напишите 'Готово'",
                reply_markup=kb_wictory_upload_actions(back_cb="wictory:back:bank"),
            )
        return

    if stage == "data":
        await state.set_state(WictoryStates.enter_data)
        await cq.answer()
        if cq.message:
            await _safe_edit_or_answer(cq, 
                "Введите ссылку",
                reply_markup=kb_wictory_back_cancel(back_cb=("wictory:back:upload" if rtype == "link_esim" else "wictory:back:bank")),
            )
        return

    if stage == "preview":
        await state.set_state(WictoryStates.preview)
        await cq.answer("Превью отправлено ниже")
        if cq.message:
            await _show_preview_from_callback(cq, data)
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
        await _safe_edit_or_answer(
            cq,
            "Выберите источник для этой записи (TG/FB/Общее):",
            reply_markup=kb_wictory_pick_source(back_cb="wictory:back:home"),
        )


@router.callback_query(F.data.startswith("wictory:src:"))
async def wictory_pick_source(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    src = (cq.data or "").split(":")[-1].upper()
    if src not in {"TG", "FB", "ALL"}:
        await cq.answer("Некорректный источник", show_alert=True)
        return
    if src == "ALL":
        banks = await _list_banks_for_source(session, "FB")
        items = _bank_items_with_source(banks, "FB")
    else:
        banks = await _list_banks_for_source(session, src)
        items = _bank_items_with_source(banks, src)
    await state.set_state(WictoryStates.pick_bank)
    await state.update_data(resource_source=src)
    await cq.answer()
    if cq.message:
        await _safe_edit_or_answer(
            cq,
            f"Источник: <b>{_source_label(src)}</b>\nВыберите банк:",
            reply_markup=kb_wictory_banks(items, back_cb="wictory:back:home"),
        )


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
    src = str(data.get("resource_source") or "TG").upper()
    if src == "ALL":
        bank_name_fb = str(bank.name or "").strip() or "—"
        tg_banks = await _list_banks_for_source(session, "TG")
        tg_match = next((b for b in tg_banks if _norm_bank_name(getattr(b, "name", None)) == _norm_bank_name(bank.name)), None)
        if tg_match:
            await state.update_data(
                bank_id=bank_id,
                bank_name=bank_name_fb,
                bank_id_fb=bank_id,
                bank_name_fb=bank_name_fb,
                bank_id_tg=int(tg_match.id),
                bank_name_tg=str(getattr(tg_match, "name", "") or "—"),
            )
        else:
            await state.update_data(
                bank_id=bank_id,
                bank_name=bank_name_fb,
                bank_id_fb=bank_id,
                bank_name_fb=bank_name_fb,
                bank_id_tg=None,
                bank_name_tg=None,
            )
            items_tg = _bank_items_with_source(tg_banks, "TG")
            await state.set_state(WictoryStates.pick_bank_tg)
            await cq.answer()
            if cq.message:
                await _safe_edit_or_answer(
                    cq,
                    f"Банк FB: <b>{bank_name_fb}</b>\nДля TG выберите банк:",
                    reply_markup=kb_wictory_banks(items_tg, back_cb="wictory:back:bank_list"),
                )
            return
    else:
        await state.update_data(bank_id=bank_id, bank_name=bank.name)
    data = await state.get_data()
    await cq.answer()
    if data.get("bank_edit_mode"):
        await state.update_data(bank_edit_mode=None)
        await state.set_state(WictoryStates.preview)
        if cq.message:
            await _show_preview_from_callback(cq, await state.get_data())
        return
    if cq.message:
        bank_label = bank.name
        if src == "ALL":
            bank_label = f"FB: {data.get('bank_name_fb') or bank.name} / TG: {data.get('bank_name_tg') or '—'}"
        await _safe_edit_or_answer(
            cq,
            f"Банк: <b>{bank_label}</b>\nВыберите режим добавления:",
            reply_markup=kb_wictory_bank_actions(),
        )


@router.callback_query(WictoryStates.pick_bank_tg, F.data.startswith("wictory:bank:"))
async def wictory_pick_bank_tg(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    bank_id = int((cq.data or "").split(":")[-1])
    bank = await get_bank(session, bank_id)
    if not bank:
        await cq.answer("Банк не найден", show_alert=True)
        return

    data = await state.get_data()
    await state.update_data(
        bank_id_tg=bank_id,
        bank_name_tg=str(getattr(bank, "name", "") or "—"),
    )
    await state.set_state(WictoryStates.pick_bank)
    await cq.answer()
    if data.get("bank_edit_mode"):
        await state.update_data(bank_edit_mode=None)
        await state.set_state(WictoryStates.preview)
        if cq.message:
            await _show_preview_from_callback(cq, await state.get_data())
        return

    if cq.message:
        fb_name = str(data.get("bank_name_fb") or data.get("bank_name") or "—")
        tg_name = str(getattr(bank, "name", "") or "—")
        await _safe_edit_or_answer(
            cq,
            f"Банк: <b>FB: {fb_name} / TG: {tg_name}</b>\nВыберите режим добавления:",
            reply_markup=kb_wictory_bank_actions(),
        )


@router.callback_query(F.data == "wictory:bank_mode:single")
async def wictory_bank_mode_single(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    data = await state.get_data()
    rtype = str(data.get("resource_type") or "")
    if rtype in {"esim", "link_esim"}:
        await state.set_state(WictoryStates.upload_screenshot)
        await state.update_data(bulk_mode=False)
        await cq.answer()
        if cq.message:
            await _safe_edit_or_answer(
                cq,
                "Отправьте файл Esim (фото/док/видео), до 10 шт., затем нажмите '✅ Готово' или напишите 'Готово'",
                reply_markup=kb_wictory_upload_actions(back_cb="wictory:back:bank"),
            )
        return
    await state.set_state(WictoryStates.enter_data)
    await state.update_data(bulk_mode=False)
    await cq.answer()
    if cq.message:
        await _safe_edit_or_answer(cq, "Введите ссылку", reply_markup=kb_wictory_back_cancel(back_cb="wictory:back:bank"))


@router.callback_query(F.data == "wictory:bank_mode:bulk")
async def wictory_bank_mode_bulk(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    data = await state.get_data()
    rtype = str(data.get("resource_type") or "")
    await state.update_data(bulk_mode=True)
    await cq.answer()
    if rtype in {"esim", "link_esim"}:
        await state.set_state(WictoryStates.upload_screenshot)
        if cq.message:
            media_help = (
                "Массовый режим добавления\n\n"
                "<blockquote expandable>"
                "Как правильно добавлять:\n"
                "• Каждое медиа-сообщение = 1 ресурс\n"
                "• Под картинкой имеется в виду: фото / файл / видео\n"
                "• Для ESIM: отправляйте медиа + в подписи комментарий\n"
                "• Для Ссылка+ESIM: отправляйте медиа + в подписи сначала комментарий, потом ссылку\n"
                "• Пример подписи для Ссылка+ESIM:\n"
                "Андрей Альянс 45\n"
                "https://example.com/...\n"
                "• Всё это должно быть в одном сообщении с медиа"
                "</blockquote>"
            )
            await _safe_edit_or_answer(
                cq,
                media_help,
                reply_markup=kb_wictory_back_cancel(back_cb="wictory:back:bank"),
            )
        return
    await state.set_state(WictoryStates.enter_bulk)
    if cq.message:
        await _safe_edit_or_answer(
            cq,
            "Массовый режим добавления\n\n"
            "<blockquote expandable>"
            "Как правильно добавлять:\n"
            "• Можно отправить 1 сообщение = 1 ресурс\n"
            "• Можно отправить один большой текст с несколькими ресурсами\n"
            "• Каждый ресурс считается завершённым после строки со ссылкой\n"
            "• Пример одного ресурса:\n"
            "Артур Альянс 43 новый\n\n"
            "https://cloud.vmoscloud.com/screen/share/XXXX\n\n"
            "• После этого можно сразу писать следующий ресурс в таком же формате"
            "</blockquote>",
            reply_markup=kb_wictory_back_cancel(back_cb="wictory:back:bank"),
        )


@router.message(WictoryStates.upload_screenshot, F.photo | F.document | F.video)
async def wictory_upload_screenshot(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(message, session)
    if not user:
        return
    data = await state.get_data()
    shots: list[str] = list(data.get("screenshots") or [])
    item_edit_id = data.get("item_edit_id")

    new_item = None
    if message.photo:
        new_item = pack_media_item("photo", message.photo[-1].file_id)
    elif message.document:
        new_item = pack_media_item("doc", message.document.file_id)
    elif message.video:
        new_item = pack_media_item("video", message.video.file_id)

    if data.get("item_edit_mode") == "media" and item_edit_id:
        replace_index = data.get("replace_index")
        if replace_index is not None and 0 <= int(replace_index) < len(shots):
            shots[int(replace_index)] = str(new_item)
        else:
            if len(shots) >= 10:
                await message.answer("Можно добавить максимум 10 файлов.")
                return
            shots.append(str(new_item))
        await state.update_data(screenshots=shots, replace_index=None)
        await message.answer(
            f"Готово. Файлов сейчас: {len(shots)}/10",
            reply_markup=kb_wictory_item_media_manage(int(item_edit_id), len(shots)),
        )
        return

    if data.get("bulk_mode"):
        rtype = str(data.get("resource_type") or "")
        src = str(data.get("resource_source") or "TG").upper()
        bank_id = int(data.get("bank_id") or 0)
        bank_id_fb = int(data.get("bank_id_fb") or bank_id or 0)
        bank_id_tg = int(data.get("bank_id_tg") or 0)
        caption_txt = (message.caption or "").strip()

        if rtype == "link_esim" and not caption_txt:
            await message.answer("Для LINK+ESIM в массовом режиме нужна подпись с ссылкой/текстом в сообщении.")
            return

        if rtype == "esim":
            text_data = caption_txt or None  # For ESIM: caption is stored as comment/text payload
        else:
            text_data = caption_txt or None

        if src == "ALL":
            if not bank_id_fb or not bank_id_tg:
                await message.answer("Не удалось определить банки для TG/FB. Начните создание заново.")
                return
            item = await create_resource_pool_item(
                session,
                source="ALL",
                bank_id=bank_id_fb,
                tg_bank_id=bank_id_tg,
                resource_type=rtype,
                text_data=text_data,
                screenshots=[str(new_item)],
                created_by_user_id=int(user.id),
            )
            await message.answer(
                f"Добавлено: <code>{_resource_ident(int(item.id))}</code>",
                reply_markup=kb_wictory_bulk_next_actions(),
            )
        else:
            item = await create_resource_pool_item(
                session,
                source=src,
                bank_id=bank_id,
                resource_type=rtype,
                text_data=text_data,
                screenshots=[str(new_item)],
                created_by_user_id=int(user.id),
            )
            await message.answer(f"Добавлено: <code>{_resource_ident(int(item.id))}</code>", reply_markup=kb_wictory_bulk_next_actions())
        return

    if len(shots) >= 10:
        await message.answer("Можно добавить максимум 10 файлов. Напишите 'Готово'.")
        return
    shots.append(str(new_item))

    # For regular ESIM/LINK+ESIM creation, preserve media caption as resource text/comment.
    caption_txt = (message.caption or "").strip()
    update_payload = {"screenshots": shots}
    if caption_txt and not data.get("item_edit_mode") and not data.get("invalid_edit_mode"):
        current_text = str(data.get("text_data") or "").strip()
        if not current_text:
            update_payload["text_data"] = caption_txt

    await state.update_data(**update_payload)
    invalid_item_id = data.get("invalid_item_id")
    if data.get("invalid_edit_mode") == "media" and invalid_item_id:
        kb = kb_wictory_invalid_edit_back_cancel(int(invalid_item_id))
    else:
        kb = kb_wictory_upload_actions(back_cb="wictory:back:bank")
    await message.answer(
        f"Принято: {len(shots)}/10. Отправьте ещё файл или напишите 'Готово'.",
        reply_markup=kb,
    )


@router.callback_query(F.data == "wictory:upload_done")
async def wictory_upload_screenshot_done_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    if await state.get_state() != WictoryStates.upload_screenshot.state:
        await cq.answer("Сейчас это недоступно", show_alert=True)
        return

    data = await state.get_data()
    shots = list(data.get("screenshots") or [])
    if not shots:
        await cq.answer("Нужно добавить хотя бы 1 файл.", show_alert=True)
        return

    if data.get("invalid_edit_mode") == "media" and data.get("invalid_item_id"):
        await wictory_update_invalid_item(
            session,
            item_id=int(data.get("invalid_item_id")),
            wictory_user_id=int(user.id),
            screenshots=shots,
        )
        await state.clear()
        await cq.answer("Медиа обновлены")
        if cq.message:
            await _safe_edit_or_answer(cq, "Медиа обновлены", reply_markup=kb_wictory_main_inline())
        return

    if data.get("item_edit_mode") == "media" and data.get("item_edit_id"):
        await wictory_update_item(
            session,
            item_id=int(data.get("item_edit_id")),
            wictory_user_id=int(user.id),
            screenshots=shots,
        )
        await state.clear()
        await cq.answer("Esim обновлены")
        if cq.message:
            await _safe_edit_or_answer(cq, "Esim обновлены", reply_markup=kb_wictory_main_inline())
        return

    rtype = str(data.get("resource_type") or "")
    if rtype == "esim":
        await state.set_state(WictoryStates.preview)
        data = await state.get_data()
        await cq.answer("Превью отправлено ниже")
        if cq.message:
            await _show_preview_from_callback(cq, data)
        return

    await state.set_state(WictoryStates.enter_data)
    await cq.answer()
    if cq.message:
        await _safe_edit_or_answer(cq, "Введите ссылку", reply_markup=kb_wictory_back_cancel(back_cb="wictory:back:bank"))


@router.message(WictoryStates.upload_screenshot, F.text)
async def wictory_upload_screenshot_done(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(message, session)
    if not user:
        return
    if (message.text or "").strip().lower() != "готово":
        data = await state.get_data()
        item_edit_id = data.get("item_edit_id")
        invalid_item_id = data.get("invalid_item_id")
        if data.get("item_edit_mode") == "media" and item_edit_id:
            kb = kb_wictory_item_media_manage(int(item_edit_id), len(list(data.get("screenshots") or [])))
        elif data.get("invalid_edit_mode") == "media" and invalid_item_id:
            kb = kb_wictory_invalid_edit_back_cancel(int(invalid_item_id))
        else:
            kb = kb_wictory_upload_actions(back_cb="wictory:back:bank")
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
        await _send_preview_message(message, data)
        return

    await state.set_state(WictoryStates.enter_data)
    await message.answer("Введите ссылку", reply_markup=kb_wictory_back_cancel(back_cb="wictory:back:bank"))


@router.callback_query(F.data == "wictory:bulk:add_more")
async def wictory_bulk_add_more(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    data = await state.get_data()
    rtype = str(data.get("resource_type") or "")
    bulk_mode = bool(data.get("bulk_mode"))
    await cq.answer()
    if not bulk_mode:
        if cq.message:
            await _safe_edit_or_answer(cq, "Меню <b>WICTORY</b>", reply_markup=kb_wictory_main_inline())
        return
    if rtype in {"esim", "link_esim"}:
        await state.set_state(WictoryStates.upload_screenshot)
        if cq.message:
            await _safe_edit_or_answer(cq, "Отправьте следующий файл/фото/видео.", reply_markup=kb_wictory_back_cancel(back_cb="wictory:back:bank"))
        return
    await state.set_state(WictoryStates.enter_bulk)
    if cq.message:
        await _safe_edit_or_answer(cq, "Отправьте следующий ресурс текстом.", reply_markup=kb_wictory_back_cancel(back_cb="wictory:back:bank"))


@router.callback_query(F.data == "wictory:bulk:finish")
async def wictory_bulk_finish(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    await state.clear()
    await cq.answer()
    if cq.message:
        await _safe_edit_or_answer(cq, "Меню <b>WICTORY</b>", reply_markup=kb_wictory_main_inline())


@router.message(WictoryStates.enter_bulk, F.text)
async def wictory_enter_bulk(message: Message, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(message, session)
    if not user:
        return
    data = await state.get_data()
    rtype = str(data.get("resource_type") or "")
    src = str(data.get("resource_source") or "TG").upper()
    bank_id = int(data.get("bank_id") or 0)
    bank_id_fb = int(data.get("bank_id_fb") or bank_id or 0)
    bank_id_tg = int(data.get("bank_id_tg") or 0)
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("Пустое сообщение. Отправьте текст с данными.")
        return

    # Bulk text supports either:
    # 1) one whole incoming message = one resource
    # 2) multiple resources in one message, where each resource ends with a URL line
    blocks: list[str] = []
    lines = [ln.rstrip() for ln in raw.splitlines()]
    current: list[str] = []
    for ln in lines:
        if not ln.strip() and not current:
            continue
        current.append(ln)
        if "http://" in ln or "https://" in ln:
            block = "\n".join(x for x in current).strip()
            if block:
                blocks.append(block)
            current = []
    tail = "\n".join(x for x in current).strip()
    if tail:
        if blocks:
            blocks[-1] = (blocks[-1] + "\n\n" + tail).strip()
        else:
            blocks.append(tail)

    created = 0
    for block in blocks:
        if src == "ALL":
            if not bank_id_fb or not bank_id_tg:
                await message.answer("Не удалось определить банки для TG/FB. Начните создание заново.")
                return
            await create_resource_pool_item(
                session,
                source="ALL",
                bank_id=bank_id_fb,
                tg_bank_id=bank_id_tg,
                resource_type=rtype,
                text_data=block,
                screenshots=[],
                created_by_user_id=int(user.id),
            )
            created += 1
        else:
            await create_resource_pool_item(
                session,
                source=src,
                bank_id=bank_id,
                resource_type=rtype,
                text_data=block,
                screenshots=[],
                created_by_user_id=int(user.id),
            )
            created += 1

    await message.answer(f"Добавлено массово: <b>{created}</b>", reply_markup=kb_wictory_bulk_next_actions())


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

    item_edit_id = int(data.get("item_edit_id") or 0)
    item_edit_mode = str(data.get("item_edit_mode") or "")
    if item_edit_id and item_edit_mode in {"data", "link", "comment"}:
        it = await get_pool_item(session, item_edit_id)
        if it and await _pool_item_is_wictory_created(session, it):
            tval = str(getattr(it.type, "value", "") or "").lower()
            new_text = txt

            # For link_esim we keep link/comment parts separate on edit.
            if tval == "link_esim":
                old_link, old_comment = _split_link_comment(getattr(it, "text_data", None))
                if item_edit_mode == "comment":
                    new_text = _compose_link_esim_payload(txt, old_link)
                elif item_edit_mode == "link":
                    new_text = _compose_link_esim_payload(old_comment, txt)

            await wictory_update_item(
                session,
                item_id=item_edit_id,
                wictory_user_id=int(user.id),
                text_data=new_text,
            )
        await state.clear()
        await message.answer("Данные обновлены", reply_markup=kb_wictory_main_inline())
        return

    rtype = str(data.get("resource_type") or "")
    if rtype in {"link", "link_esim"}:
        if not txt:
            await message.answer("Ссылка обязательна. Введите ссылку.")
            return

    await state.update_data(text_data=txt)
    await state.set_state(WictoryStates.preview)
    data = await state.get_data()
    await _send_preview_message(message, data)


@router.callback_query(WictoryStates.preview, F.data == "wictory:edit")
async def wictory_edit(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    await state.set_state(WictoryStates.edit_pick)
    await cq.answer()
    if cq.message:
        try:
            await cq.message.edit_reply_markup(reply_markup=kb_wictory_edit())
        except TelegramBadRequest:
            await _safe_edit_or_answer(cq, "Что изменить?", reply_markup=kb_wictory_edit())


@router.callback_query(WictoryStates.edit_pick, F.data.startswith("wictory:edit:"))
async def wictory_edit_pick(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    action = (cq.data or "").split(":")[-1]
    data = await state.get_data()
    if action == "bank":
        src = str(data.get("resource_source") or "TG")
        if src.upper() == "ALL":
            banks = await _list_banks_for_source(session, "FB")
            items = _bank_items_with_source(banks, "FB")
        else:
            banks = await _list_banks_for_source(session, src)
            items = _bank_items_with_source(banks, src)
        await state.set_state(WictoryStates.pick_bank)
        await state.update_data(bank_edit_mode=True)
        await cq.answer()
        if cq.message:
            await cq.message.answer("Выберите банк:", reply_markup=kb_wictory_banks(items, back_cb="wictory:preview"))
        return
    if action == "data":
        await state.set_state(WictoryStates.enter_data)
        await cq.answer()
        if cq.message:
            await cq.message.answer(
                "Введите нужные данные",
                reply_markup=kb_wictory_back_cancel(back_cb="wictory:preview"),
            )
        return
    if action == "screen":
        await state.set_state(WictoryStates.upload_screenshot)
        await cq.answer()
        if cq.message:
            await cq.message.answer(
                "Отправьте скриншот",
                reply_markup=kb_wictory_upload_actions(back_cb="wictory:preview"),
            )
        return
    await state.set_state(WictoryStates.preview)
    await cq.answer("Превью отправлено ниже")
    if cq.message:
        await _show_preview_from_callback(cq, data)


@router.callback_query(F.data == "wictory:preview")
async def wictory_preview_show(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    data = await state.get_data()
    await state.set_state(WictoryStates.preview)
    await cq.answer("Превью отправлено ниже")
    if cq.message:
        await _show_preview_from_callback(cq, data)


@router.callback_query(F.data == "wictory:confirm")
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
    src = str(data.get("resource_source") or getattr(user, "manager_source", None) or "TG").upper()
    if src == "ALL":
        bank_id_fb = int(data.get("bank_id_fb") or data.get("bank_id") or 0)
        bank_id_tg = int(data.get("bank_id_tg") or 0)
        if not bank_id_fb or not bank_id_tg:
            await cq.answer("Не удалось определить банки для TG/FB", show_alert=True)
            return
        await create_resource_pool_item(
            session,
            source="ALL",
            bank_id=bank_id_fb,
            tg_bank_id=bank_id_tg,
            resource_type=resource_type,
            text_data=data.get("text_data"),
            screenshots=list(data.get("screenshots") or []),
            created_by_user_id=int(user.id),
        )
    else:
        await create_resource_pool_item(
            session,
            source=src,
            bank_id=int(data["bank_id"]),
            resource_type=resource_type,
            text_data=data.get("text_data"),
            screenshots=list(data.get("screenshots") or []),
            created_by_user_id=int(user.id),
        )
    await state.clear()
    await cq.answer()
    if cq.message:
        await cq.message.answer("Меню <b>WICTORY</b>", reply_markup=kb_wictory_main_inline())


@router.callback_query(F.data == "wictory:invalid:list")
async def wictory_invalid_list(cq: CallbackQuery, session: AsyncSession) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    items = await list_invalid_pool_items_for_wictory(session, wictory_user_id=int(user.id))
    packed: list[tuple[int, str]] = []
    for it in items:
        bank = await get_bank(session, int(it.bank_id))
        packed.append((int(it.id), f"{_resource_ident(int(it.id))} | {bank.name if bank else '—'} | {getattr(it.type, 'value', '—')}"))
    await cq.answer()
    if cq.message:
        if not packed:
            await _safe_edit_or_answer(cq, "Невалидных записей нет", reply_markup=kb_wictory_main_inline())
            return
        await _safe_edit_or_answer(cq, "Невалидные записи:", reply_markup=kb_wictory_invalid_list(packed))


@router.callback_query(F.data.startswith("wictory:invalid:open:"))
async def wictory_invalid_open(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    item_id = int((cq.data or "").split(":")[-1])
    it = await get_pool_item(session, item_id)
    if not it or it.status != ResourceStatus.INVALID or not await _pool_item_is_wictory_created(session, it):
        await cq.answer("Запись не найдена", show_alert=True)
        return
    bank = await get_bank(session, int(it.bank_id))
    history_chain = str(getattr(it, "usage_history", "") or "").strip() or "—"
    txt = (
        f"<b>Невалидная запись</b>\n"
        f"ID ресурса: <code>{int(it.id)}</code>\n"
        f"Код ресурса: <code>{_resource_ident(int(it.id))}</code>\n"
        f"Банк: <b>{bank.name if bank else '—'}</b>\n"
        f"Тип: <b>{getattr(it.type, 'value', '—')}</b>\n"
        f"История пользования: <code>{history_chain}</code>\n"
        f"Данные: <code>{it.text_data or '—'}</code>\n"
        f"Комментарий DM: {it.invalid_comment or '—'}"
    )
    await state.update_data(invalid_item_id=item_id)
    await cq.answer()
    if cq.message:
        tval = str(getattr(it.type, 'value', '') or '').lower()
        tlabel = {
            'link': 'ссылку',
            'esim': 'esim',
            'link_esim': 'ссылку+esim',
        }.get(tval, 'ресурс')
        await _safe_edit_or_answer(cq, txt, reply_markup=kb_wictory_invalid_actions(item_id, type_label=tlabel))


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
        await _safe_edit_or_answer(cq, "Введите новые данные", reply_markup=kb_wictory_invalid_edit_back_cancel(item_id))


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
        await _safe_edit_or_answer(
            cq,
            "Отправьте файлы (до 10), затем напишите 'Готово'",
            reply_markup=kb_wictory_invalid_edit_back_cancel(item_id),
        )


@router.callback_query(F.data.startswith("wictory:invalid:return:"))
async def wictory_invalid_return(cq: CallbackQuery, session: AsyncSession) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    item_id = int((cq.data or "").split(":")[-1])
    it = await wictory_update_invalid_item(session, item_id=item_id, wictory_user_id=int(user.id), set_free=True)
    await cq.answer("Возвращено в общий пул" if it else "Не удалось", show_alert=not bool(it))
    if cq.message:
        await _safe_edit_or_answer(cq, "Запись возвращена в общий пул", reply_markup=kb_wictory_main_inline())


@router.callback_query(F.data.startswith("wictory:invalid:delete:"))
async def wictory_invalid_delete(cq: CallbackQuery, session: AsyncSession) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    item_id = int((cq.data or "").split(":")[-1])
    ok = await wictory_delete_item(session, item_id=item_id, wictory_user_id=int(user.id))
    await cq.answer("Удалено" if ok else "Не удалось удалить", show_alert=not bool(ok))
    if cq.message and ok:
        await _safe_edit_or_answer(cq, "Невалидная запись удалена", reply_markup=kb_wictory_main_inline())


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
            f"{icon} {_resource_ident(int(it.id))} | {it.source} | {bank.name if bank else '—'} | {getattr(it.type, 'value', '—')} | {st.upper()}",
        ))
    await cq.answer()
    if cq.message:
        if not packed:
            try:
                await _safe_edit_or_answer(cq, "У вас пока нет записей", reply_markup=kb_wictory_main_inline())
            except TelegramBadRequest:
                await cq.message.answer("У вас пока нет записей", reply_markup=kb_wictory_main_inline())
            return
        legend = (
            "<b>Мои записи</b>\n"
            "<blockquote expandable>"
            "Расшифровка статусов:\n"
            "🟡 FREE — свободна, можно выдавать DM\n"
            "🟢 ASSIGNED — сейчас в работе у DM\n"
            "✅ USED — уже использована\n"
            "🔴 INVALID — помечена невалидной"
            "</blockquote>"
        )
        try:
            await _safe_edit_or_answer(cq, legend, reply_markup=kb_wictory_items_list(packed))
        except TelegramBadRequest:
            await cq.message.answer(legend, reply_markup=kb_wictory_items_list(packed))


@router.callback_query(F.data == "wictory:items:legend")
async def wictory_items_legend(cq: CallbackQuery, session: AsyncSession) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    await cq.answer()
    if cq.message:
        await cq.message.answer(
            "<blockquote expandable>"
            "Расшифровка статусов:\n"
            "🟡 FREE — свободна, можно выдавать DM\n"
            "🟢 ASSIGNED — сейчас в работе у DM\n"
            "✅ USED — уже использована\n"
            "🔴 INVALID — помечена невалидной"
            "</blockquote>"
        )


@router.callback_query(F.data.startswith("wictory:item:open:"))
async def wictory_item_open(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    item_id = int((cq.data or "").split(":")[-1])
    it = await get_pool_item(session, item_id)
    if not it or not await _pool_item_is_wictory_created(session, it):
        await cq.answer("Запись не найдена", show_alert=True)
        return
    bank = await get_bank(session, int(it.bank_id))
    history_chain = str(getattr(it, "usage_history", "") or "").strip() or "—"
    type_value = str(getattr(it.type, 'value', '—') or '—')
    extra_lines: list[str] = []
    if type_value == 'esim':
        data_line = f"Комментарий: <code>{it.text_data or '—'}</code>"
    elif type_value == 'link_esim':
        lnk, comment = _split_link_comment(it.text_data)
        data_line = f"Комментарий: <code>{comment or '—'}</code>"
        extra_lines.append(f"Ссылка: <code>{lnk or '—'}</code>")
    else:
        data_line = f"Ссылка: <code>{it.text_data or '—'}</code>"
    txt = (
        f"<b>Запись #{int(it.id)}</b>\n"
        f"Код ресурса: <code>{_resource_ident(int(it.id))}</code>\n"
        f"Источник: <b>{it.source}</b>\n"
        f"Банк: <b>{bank.name if bank else '—'}</b>\n"
        f"Тип: <b>{type_value}</b>\n"
        f"Статус: <b>{getattr(it.status, 'value', '—')}</b>\n"
        f"История пользования: <code>{history_chain}</code>\n"
        f"{data_line}"
    )
    if extra_lines:
        txt += "\n" + "\n".join(extra_lines)
    txt += f"\nEsim файлов: <b>{len(list(it.screenshots or []))}</b>"
    st_val = getattr(it.status, "value", "")
    can_edit_by_status = st_val in {"free", "invalid"}
    tval = str(getattr(it.type, "value", "") or "").lower()
    can_edit_link = can_edit_by_status and tval in {"link", "link_esim"}
    can_edit_comment = can_edit_by_status and tval in {"esim", "link_esim"}
    can_media = can_edit_by_status and getattr(it.type, "value", "") in {"esim", "link_esim"}
    can_delete = st_val != "assigned"
    can_edit_meta = can_edit_by_status
    kb = kb_wictory_item_actions(
        item_id,
        can_edit_link=can_edit_link,
        can_edit_comment=can_edit_comment,
        can_edit_media=can_media,
        can_delete=can_delete,
        can_edit_meta=can_edit_meta,
    )
    await state.update_data(item_edit_id=item_id)
    await cq.answer()
    if cq.message:
        shots = list(it.screenshots or [])
        if shots:
            kind, fid = unpack_media_item(str(shots[0]))
            if kind == "photo":
                await cq.message.answer_photo(fid, caption=txt, parse_mode="HTML", reply_markup=kb)
            elif kind == "video":
                await cq.message.answer_video(fid, caption=txt, parse_mode="HTML", reply_markup=kb)
            else:
                await cq.message.answer_document(fid, caption=txt, parse_mode="HTML", reply_markup=kb)
        else:
            await _safe_edit_or_answer(cq, txt, reply_markup=kb)


@router.callback_query(F.data.startswith("wictory:item:edit_data:"))
async def wictory_item_edit_data_start(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    item_id = int((cq.data or "").split(":")[-1])
    it = await get_pool_item(session, item_id)
    if not it or not await _pool_item_is_wictory_created(session, it):
        await cq.answer("Запись не найдена", show_alert=True)
        return
    if getattr(it.status, "value", "") not in {"free", "invalid"}:
        await cq.answer("Редактирование доступно только для FREE/INVALID", show_alert=True)
        return
    tval = str(getattr(it.type, "value", "") or "").lower()
    await state.update_data(item_edit_id=item_id, item_edit_mode="data")
    await state.set_state(WictoryStates.enter_data)
    await cq.answer()
    if cq.message:
        prompt = "Введите новые данные"
        if tval == "esim":
            prompt = "Введите новый комментарий"
        elif tval == "link":
            prompt = "Введите новую ссылку"
        elif tval == "link_esim":
            prompt = "Введите новый комментарий и ссылку"
        await _safe_edit_or_answer(cq, prompt, reply_markup=kb_wictory_item_edit_back_cancel(item_id))


@router.callback_query(F.data.startswith("wictory:item:edit_comment:"))
async def wictory_item_edit_comment_start(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    item_id = int((cq.data or "").split(":")[-1])
    it = await get_pool_item(session, item_id)
    if not it or not await _pool_item_is_wictory_created(session, it):
        await cq.answer("Запись не найдена", show_alert=True)
        return
    if getattr(it.status, "value", "") not in {"free", "invalid"}:
        await cq.answer("Редактирование доступно только для FREE/INVALID", show_alert=True)
        return
    await state.update_data(item_edit_id=item_id, item_edit_mode="comment")
    await state.set_state(WictoryStates.enter_data)
    await cq.answer()
    if cq.message:
        await _safe_edit_or_answer(cq, "Введите новый комментарий", reply_markup=kb_wictory_item_edit_back_cancel(item_id))


@router.callback_query(F.data.startswith("wictory:item:edit_link:"))
async def wictory_item_edit_link_start(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    item_id = int((cq.data or "").split(":")[-1])
    it = await get_pool_item(session, item_id)
    if not it or not await _pool_item_is_wictory_created(session, it):
        await cq.answer("Запись не найдена", show_alert=True)
        return
    if getattr(it.status, "value", "") not in {"free", "invalid"}:
        await cq.answer("Редактирование доступно только для FREE/INVALID", show_alert=True)
        return
    await state.update_data(item_edit_id=item_id, item_edit_mode="link")
    await state.set_state(WictoryStates.enter_data)
    await cq.answer()
    if cq.message:
        await _safe_edit_or_answer(cq, "Введите новую ссылку", reply_markup=kb_wictory_item_edit_back_cancel(item_id))


@router.callback_query(F.data.startswith("wictory:item:edit_media:"))
async def wictory_item_edit_media_start(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    item_id = int((cq.data or "").split(":")[-1])
    it = await get_pool_item(session, item_id)
    if not it or not await _pool_item_is_wictory_created(session, it):
        await cq.answer("Запись не найдена", show_alert=True)
        return
    if getattr(it.status, "value", "") not in {"free", "invalid"}:
        await cq.answer("Редактирование доступно только для FREE/INVALID", show_alert=True)
        return
    shots = list(it.screenshots or [])
    await state.update_data(item_edit_id=item_id, item_edit_mode="media", screenshots=shots, replace_index=None)
    await state.set_state(WictoryStates.upload_screenshot)
    await cq.answer()
    if cq.message:
        await _safe_edit_or_answer(
            cq,
            "Выберите, какой файл заменить, или добавьте новый:",
            reply_markup=kb_wictory_item_media_manage(item_id, len(shots)),
        )


@router.callback_query(F.data.startswith("wictory:item:media_pick:"))
async def wictory_item_media_pick(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    parts = (cq.data or "").split(":")
    if len(parts) < 5:
        await cq.answer("Некорректная кнопка", show_alert=True)
        return
    item_id = int(parts[3])
    idx = int(parts[4])
    data = await state.get_data()
    shots = list(data.get("screenshots") or [])
    if idx < 0 or idx >= len(shots):
        await cq.answer("Некорректный номер файла", show_alert=True)
        return
    await state.update_data(item_edit_id=item_id, item_edit_mode="media", replace_index=idx)
    await cq.answer()
    if cq.message:
        await _safe_edit_or_answer(
            cq,
            f"Отправьте новый файл для замены #{idx+1} (фото/видео/файл)",
            reply_markup=kb_wictory_item_edit_back_cancel(item_id),
        )


@router.callback_query(F.data.startswith("wictory:item:media_add:"))
async def wictory_item_media_add(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    item_id = int((cq.data or "").split(":")[-1])
    await state.update_data(item_edit_id=item_id, item_edit_mode="media", replace_index=None)
    await cq.answer()
    if cq.message:
        await _safe_edit_or_answer(
            cq,
            "Отправьте новый файл для добавления (фото/видео/файл)",
            reply_markup=kb_wictory_item_edit_back_cancel(item_id),
        )


@router.callback_query(F.data.startswith("wictory:item:edit_source:"))
async def wictory_item_edit_source_start(cq: CallbackQuery, session: AsyncSession) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    item_id = int((cq.data or "").split(":")[-1])
    it = await get_pool_item(session, item_id)
    if not it or not await _pool_item_is_wictory_created(session, it):
        await cq.answer("Запись не найдена", show_alert=True)
        return
    if getattr(it.status, "value", "") not in {"free", "invalid"}:
        await cq.answer("Редактирование доступно только для FREE/INVALID", show_alert=True)
        return
    await cq.answer()
    if cq.message:
        await _safe_edit_or_answer(cq, "Выберите новый источник:", reply_markup=kb_wictory_item_pick_source(item_id))


@router.callback_query(F.data.startswith("wictory:item:set_source:"))
async def wictory_item_set_source(cq: CallbackQuery, session: AsyncSession) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    parts = (cq.data or "").split(":")
    if len(parts) < 5:
        await cq.answer("Некорректные данные", show_alert=True)
        return
    item_id = int(parts[3])
    src = (parts[4] or "").upper()
    if src not in {"TG", "FB"}:
        await cq.answer("Некорректный источник", show_alert=True)
        return
    it = await get_pool_item(session, item_id)
    if not it or not await _pool_item_is_wictory_created(session, it):
        await cq.answer("Запись не найдена", show_alert=True)
        return
    if getattr(it.status, "value", "") not in {"free", "invalid"}:
        await cq.answer("Редактирование доступно только для FREE/INVALID", show_alert=True)
        return
    await wictory_update_item(session, item_id=item_id, wictory_user_id=int(user.id), source=src)
    await cq.answer("Источник обновлён")
    if cq.message:
        await _safe_edit_or_answer(cq, "Источник обновлён", reply_markup=kb_wictory_main_inline())


@router.callback_query(F.data.startswith("wictory:item:edit_bank:"))
async def wictory_item_edit_bank_start(cq: CallbackQuery, session: AsyncSession) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    item_id = int((cq.data or "").split(":")[-1])
    it = await get_pool_item(session, item_id)
    if not it or not await _pool_item_is_wictory_created(session, it):
        await cq.answer("Запись не найдена", show_alert=True)
        return
    if getattr(it.status, "value", "") not in {"free", "invalid"}:
        await cq.answer("Редактирование доступно только для FREE/INVALID", show_alert=True)
        return
    banks = await _list_banks_for_source(session, getattr(it, "source", None))
    items = _bank_items_with_source(banks, getattr(it, "source", None))
    await cq.answer()
    if cq.message:
        await _safe_edit_or_answer(cq, "Выберите новый банк:", reply_markup=kb_wictory_item_banks(items, item_id=item_id))


@router.callback_query(F.data.startswith("wictory:item:set_bank:"))
async def wictory_item_set_bank(cq: CallbackQuery, session: AsyncSession) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    parts = (cq.data or "").split(":")
    if len(parts) < 5:
        await cq.answer("Некорректные данные", show_alert=True)
        return
    item_id = int(parts[3])
    bank_id = int(parts[4])
    it = await get_pool_item(session, item_id)
    if not it or not await _pool_item_is_wictory_created(session, it):
        await cq.answer("Запись не найдена", show_alert=True)
        return
    if getattr(it.status, "value", "") not in {"free", "invalid"}:
        await cq.answer("Редактирование доступно только для FREE/INVALID", show_alert=True)
        return
    await wictory_update_item(session, item_id=item_id, wictory_user_id=int(user.id), bank_id=bank_id)
    await cq.answer("Банк обновлён")
    if cq.message:
        await _safe_edit_or_answer(cq, "Банк обновлён", reply_markup=kb_wictory_main_inline())


@router.callback_query(F.data.startswith("wictory:item:delete:"))
async def wictory_item_delete(cq: CallbackQuery, session: AsyncSession) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    item_id = int((cq.data or "").split(":")[-1])
    ok = await wictory_delete_item(session, item_id=item_id, wictory_user_id=int(user.id))
    await cq.answer("Удалено" if ok else "Нельзя удалить (в работе или не найдено)", show_alert=not ok)
    if cq.message:
        await _safe_edit_or_answer(cq, "Готово", reply_markup=kb_wictory_main_inline())


@router.callback_query(F.data == "wictory:item:cancel_edit")
async def wictory_item_cancel_edit(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    await state.clear()
    await cq.answer("Редактирование отменено")
    if cq.message:
        try:
            await _safe_edit_or_answer(cq, "Меню <b>WICTORY</b>", reply_markup=kb_wictory_main_inline())
        except TelegramBadRequest:
            await cq.message.answer("Меню <b>WICTORY</b>", reply_markup=kb_wictory_main_inline())


def _read_stats_filters(data: dict) -> tuple[set[str], set[int], str, set[str], set[str]]:
    src = {str(x).upper() for x in (data.get("stats_sources") or [])}
    banks = {int(x) for x in (data.get("stats_bank_ids") or [])}
    date = str(data.get("stats_date") or "all")
    statuses = {str(x).lower() for x in (data.get("stats_statuses") or [])}
    types = {str(x).lower() for x in (data.get("stats_types") or [])}
    return src, banks, date, statuses, types


async def _render_stats_text(session: AsyncSession, data: dict) -> str:
    src, banks, date_mode, statuses, types = _read_stats_filters(data)
    now = datetime.utcnow()
    created_from = None
    if date_mode == "today":
        created_from = datetime(now.year, now.month, now.day)
    elif date_mode == "7d":
        created_from = now - timedelta(days=7)
    elif date_mode == "30d":
        created_from = now - timedelta(days=30)

    items = await list_pool_items_filtered(
        session,
        sources=list(src) or None,
        bank_ids=list(banks) or None,
        statuses=list(statuses) or None,
        types=list(types) or None,
        created_from=created_from,
        limit=3000,
    )

    bank_map = {int(b.id): b for b in await list_banks(session)}
    grouped: dict[tuple[int, str], dict[str, int]] = {}
    for it in items:
        key = (int(it.bank_id), str(getattr(it, "source", "TG")).upper())
        st = grouped.setdefault(key, {"link": 0, "esim": 0, "link_esim": 0, "free": 0, "assigned": 0, "used": 0, "invalid": 0, "total": 0})
        st[str(getattr(it.type, "value", "link"))] += 1
        st[str(getattr(it.status, "value", "free"))] += 1
        st["total"] += 1

    filt_lines = ["<b>Фильтры</b>"]
    filt_lines.append(f"• Источник: {', '.join(sorted(src)) if src else 'все'}")
    filt_lines.append(f"• Банк: {', '.join(str(x) for x in sorted(banks)) if banks else 'все'}")
    filt_lines.append(f"• Дата: {date_mode}")
    filt_lines.append(f"• Статус: {', '.join(sorted(statuses)) if statuses else 'все'}")
    filt_lines.append(f"• Тип: {', '.join(sorted(types)) if types else 'все'}")

    lines = ["🏦 <b>Пул по банкам</b>", "<blockquote expandable>" + "\n".join(filt_lines) + "</blockquote>"]
    if not grouped:
        lines.append("\nПул пуст по выбранным фильтрам.")
        return "\n".join(lines)

    total_link = total_esim = total_combo = 0
    total_free = total_assigned = total_used = total_invalid = 0
    idx = 0
    for (bank_id, source), st in sorted(grouped.items(), key=lambda x: (-x[1]["total"], x[0][0], x[0][1])):
        idx += 1
        bank_name = getattr(bank_map.get(bank_id), "name", "—")
        total_link += st["link"]
        total_esim += st["esim"]
        total_combo += st["link_esim"]
        total_free += st["free"]
        total_assigned += st["assigned"]
        total_used += st["used"]
        total_invalid += st["invalid"]
        lines.extend([
            "",
            f"<b>{idx}. {bank_name} ({source})</b>",
            f"• Типы: 🔗 {st['link']} | 📱 {st['esim']} | 🔗+📱 {st['link_esim']}",
            f"• Статусы: 🟡 {st['free']} | 🟢 {st['assigned']} | ✅ {st['used']} | 🔴 {st['invalid']}",
        ])

    lines.extend([
        "",
        "━━━━━━━━━━━━━━",
        "<b>ИТОГО</b>",
        f"Типы: 🔗 {total_link} | 📱 {total_esim} | 🔗+📱 {total_combo}",
        f"Статусы: 🟡 {total_free} | 🟢 {total_assigned} | ✅ {total_used} | 🔴 {total_invalid}",
    ])
    return "\n".join(lines)


@router.callback_query(F.data == "wictory:stats")
async def wictory_stats(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    data = await state.get_data()
    txt = await _render_stats_text(session, data)
    await cq.answer()
    if cq.message:
        await _safe_edit_or_answer(cq, txt, reply_markup=kb_wictory_stats_main())


def _filters_summary_text(data: dict) -> str:
    src, banks, date_mode, statuses, types = _read_stats_filters(data)
    lines = [
        "<b>Настройка фильтров</b>",
        "<blockquote expandable>",
        f"Источник: {', '.join(sorted(src)) if src else 'все'}",
        f"Банк: {', '.join(str(x) for x in sorted(banks)) if banks else 'все'}",
        f"Дата: {date_mode}",
        f"Статус: {', '.join(sorted(statuses)) if statuses else 'все'}",
        f"Тип: {', '.join(sorted(types)) if types else 'все'}",
        "</blockquote>",
    ]
    return "\n".join(lines)


@router.callback_query(F.data == "wictory:stats:filters")
async def wictory_stats_filters(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    await cq.answer()
    if cq.message:
        await _safe_edit_or_answer(cq, _filters_summary_text(await state.get_data()), reply_markup=kb_wictory_stats_filters_main())


@router.callback_query(F.data == "wictory:stats:filters:source")
async def wictory_stats_filters_source(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    data = await state.get_data()
    src, _, _, _, _ = _read_stats_filters(data)
    await cq.answer()
    if cq.message:
        await _safe_edit_or_answer(cq, _filters_summary_text(data), reply_markup=kb_wictory_stats_filter_source(src))


@router.callback_query(F.data == "wictory:stats:filters:status")
async def wictory_stats_filters_status(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    data = await state.get_data()
    _, _, _, statuses, _ = _read_stats_filters(data)
    await cq.answer()
    if cq.message:
        await _safe_edit_or_answer(cq, _filters_summary_text(data), reply_markup=kb_wictory_stats_filter_status(statuses))


@router.callback_query(F.data == "wictory:stats:filters:type")
async def wictory_stats_filters_type(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    data = await state.get_data()
    _, _, _, _, types = _read_stats_filters(data)
    await cq.answer()
    if cq.message:
        await _safe_edit_or_answer(cq, _filters_summary_text(data), reply_markup=kb_wictory_stats_filter_type(types))


@router.callback_query(F.data == "wictory:stats:filters:date")
async def wictory_stats_filters_date(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    data = await state.get_data()
    _, _, date_mode, _, _ = _read_stats_filters(data)
    await cq.answer()
    if cq.message:
        await _safe_edit_or_answer(cq, _filters_summary_text(data), reply_markup=kb_wictory_stats_filter_date(date_mode))


@router.callback_query(F.data == "wictory:stats:filters:bank")
async def wictory_stats_filters_bank(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    data = await state.get_data()
    _, bank_ids, _, _, _ = _read_stats_filters(data)
    banks = await list_banks(session)
    items = [(int(b.id), b.name) for b in banks]
    await cq.answer()
    if cq.message:
        await _safe_edit_or_answer(cq, _filters_summary_text(data), reply_markup=kb_wictory_stats_filter_bank(items, bank_ids))


@router.callback_query(F.data.startswith("wictory:stats:toggle:source:"))
async def wictory_stats_toggle_source(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    val = (cq.data or "").split(":")[-1].upper()
    data = await state.get_data()
    src, _, _, _, _ = _read_stats_filters(data)
    if val in src:
        src.remove(val)
    else:
        src.add(val)
    await state.update_data(stats_sources=sorted(src))
    await cq.answer("Обновлено")
    if cq.message:
        data = await state.get_data()
        await _safe_edit_or_answer(cq, _filters_summary_text(data), reply_markup=kb_wictory_stats_filter_source(src))


@router.callback_query(F.data.startswith("wictory:stats:toggle:status:"))
async def wictory_stats_toggle_status(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    val = (cq.data or "").split(":")[-1].lower()
    data = await state.get_data()
    _, _, _, statuses, _ = _read_stats_filters(data)
    if val in statuses:
        statuses.remove(val)
    else:
        statuses.add(val)
    await state.update_data(stats_statuses=sorted(statuses))
    await cq.answer("Обновлено")
    if cq.message:
        data = await state.get_data()
        await _safe_edit_or_answer(cq, _filters_summary_text(data), reply_markup=kb_wictory_stats_filter_status(statuses))


@router.callback_query(F.data.startswith("wictory:stats:toggle:type:"))
async def wictory_stats_toggle_type(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    val = (cq.data or "").split(":")[-1].lower()
    data = await state.get_data()
    _, _, _, _, types = _read_stats_filters(data)
    if val in types:
        types.remove(val)
    else:
        types.add(val)
    await state.update_data(stats_types=sorted(types))
    await cq.answer("Обновлено")
    if cq.message:
        data = await state.get_data()
        await _safe_edit_or_answer(cq, _filters_summary_text(data), reply_markup=kb_wictory_stats_filter_type(types))


@router.callback_query(F.data.startswith("wictory:stats:toggle:bank:"))
async def wictory_stats_toggle_bank(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    bid = int((cq.data or "").split(":")[-1])
    data = await state.get_data()
    _, bank_ids, _, _, _ = _read_stats_filters(data)
    if bid in bank_ids:
        bank_ids.remove(bid)
    else:
        bank_ids.add(bid)
    await state.update_data(stats_bank_ids=sorted(bank_ids))
    banks = await list_banks(session)
    items = [(int(b.id), b.name) for b in banks]
    await cq.answer("Обновлено")
    if cq.message:
        data = await state.get_data()
        await _safe_edit_or_answer(cq, _filters_summary_text(data), reply_markup=kb_wictory_stats_filter_bank(items, bank_ids))


@router.callback_query(F.data.startswith("wictory:stats:set_date:"))
async def wictory_stats_set_date(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    mode = (cq.data or "").split(":")[-1]
    if mode not in {"all", "today", "7d", "30d"}:
        await cq.answer("Некорректная дата", show_alert=True)
        return
    await state.update_data(stats_date=mode)
    await cq.answer("Дата обновлена")
    if cq.message:
        data = await state.get_data()
        await _safe_edit_or_answer(cq, _filters_summary_text(data), reply_markup=kb_wictory_stats_filter_date(mode))


@router.callback_query(F.data == "wictory:stats:reset")
async def wictory_stats_reset(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    await state.update_data(stats_sources=[], stats_bank_ids=[], stats_date="all", stats_statuses=[], stats_types=[])
    await cq.answer("Фильтры сброшены")
    if cq.message:
        await _safe_edit_or_answer(cq, _filters_summary_text(await state.get_data()), reply_markup=kb_wictory_stats_filters_main())


@router.callback_query(F.data == "wictory:stats:apply")
async def wictory_stats_apply(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    user = await _wictory_guard(cq, session)
    if not user:
        return
    txt = await _render_stats_text(session, await state.get_data())
    await cq.answer("Готово")
    if cq.message:
        await _safe_edit_or_answer(cq, txt, reply_markup=kb_wictory_stats_main())
