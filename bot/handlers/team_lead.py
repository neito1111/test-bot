from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramNetworkError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InputMediaDocument, InputMediaPhoto, InputMediaVideo, Message, ReplyKeyboardRemove
from aiogram.utils.keyboard import InlineKeyboardBuilder
from datetime import datetime, timedelta
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.callbacks import BankCb, BankEditCb, FormReviewCb, TeamLeadMenuCb
from bot.config import Settings
from bot.keyboards import (
    kb_back,
    kb_dm_reject_notice,
    kb_banks_list,
    kb_bank_edit_for_source,
    kb_bank_open,
    kb_form_review_with_back,
    kb_team_lead_inline_main,
    kb_tl_live_list,
    kb_tl_duplicate_filter_menu,
    kb_tl_duplicates_list,
    kb_tl_reject_back_inline,
    kb_tl_duplicate_notice,
)
from bot.models import FormStatus, Shift, TeamLeadSource, User, UserRole
from bot.repositories import (
    create_bank,
    get_forward_group_by_id,
    get_active_shift,
    delete_bank_condition,
    get_bank,
    get_bank_by_name,
    get_form,
    get_team_lead_by_tg_id,
    get_user_by_id,
    get_user_by_tg_id,
    is_team_lead,
    count_pending_forms,
    list_banks,
    list_pending_forms,
    list_duplicate_reports_in_range,
    set_form_status,
    update_bank,
)
from bot.utils import (
    format_bank_hashtag,
    pop_tl_duplicate_notices,
    pop_tl_form_notice,
    register_dm_approved_notice,
    register_dm_reject_notice,
    unpack_media_item,
)
from bot.states import TeamLeadStates
from bot.utils import format_user_payload
from bot.middlewares import GroupMessageFilter

router = Router(name="team_lead")
# Apply group message filter to all handlers in this router
router.message.filter(GroupMessageFilter())
log = logging.getLogger(__name__)


async def _notify_active_dms_banks_updated(bot: any, session: AsyncSession) -> None:
    try:
        res = await session.execute(
            select(User.tg_id)
            .join(Shift, Shift.manager_id == User.id)
            .where(and_(User.role == UserRole.DROP_MANAGER, Shift.ended_at.is_(None)))
        )
        tg_ids = sorted({int(r[0]) for r in res.all() if r and r[0]})
        for tg_id in tg_ids:
            try:
                await bot.send_message(tg_id, "🔄 Обновлено")
            except Exception:
                continue
    except Exception:
        return


def _period_to_range(period: str | None) -> tuple[datetime | None, datetime | None]:
    p = (period or "today").lower()
    now = datetime.utcnow()
    today = now.date()
    if p == "all":
        return None, None
    if p == "today":
        start = datetime(today.year, today.month, today.day)
        return start, start + timedelta(days=1)
    if p == "yesterday":
        start = datetime(today.year, today.month, today.day) - timedelta(days=1)
        return start, start + timedelta(days=1)
    if p == "last7":
        end = datetime(today.year, today.month, today.day) + timedelta(days=1)
        return end - timedelta(days=7), end
    if p == "last30":
        end = datetime(today.year, today.month, today.day) + timedelta(days=1)
        return end - timedelta(days=30), end
    if p == "week":
        start_date = today - timedelta(days=today.weekday())
        start = datetime(start_date.year, start_date.month, start_date.day)
        return start, start + timedelta(days=7)
    if p == "month":
        start = datetime(today.year, today.month, 1)
        if today.month == 12:
            end = datetime(today.year + 1, 1, 1)
        else:
            end = datetime(today.year, today.month + 1, 1)
        return start, end
    if p == "prev_month":
        if today.month == 1:
            start = datetime(today.year - 1, 12, 1)
            end = datetime(today.year, 1, 1)
        else:
            start = datetime(today.year, today.month - 1, 1)
            end = datetime(today.year, today.month, 1)
        return start, end
    if p == "year":
        start = datetime(today.year, 1, 1)
        end = datetime(today.year + 1, 1, 1)
        return start, end
    return None, None


async def _render_tl_live_list(message_or_cq: Message | CallbackQuery, session: AsyncSession) -> None:
    forms = await list_pending_forms(session, limit=30)
    text = "📋 <b>Лайв анкеты</b>\n\n"
    if not forms:
        text += "Пока нет анкет в обработке."
    else:
        text += f"В очереди: <b>{len(forms)}</b>\n\nВыберите анкету:"
    kb = kb_tl_live_list(forms)
    if isinstance(message_or_cq, CallbackQuery):
        await message_or_cq.answer()
        if message_or_cq.message:
            try:
                await message_or_cq.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            except Exception:
                try:
                    await message_or_cq.message.answer(text, reply_markup=kb, parse_mode="HTML")
                except Exception:
                    pass
        return
    await message_or_cq.answer(text, reply_markup=kb, parse_mode="HTML")


async def _render_tl_duplicates_list(message_or_cq: Message | CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    period = (data.get("dup_period") or "today")
    created_from = data.get("dup_created_from")
    created_to = data.get("dup_created_to")
    if period != "custom":
        created_from, created_to = _period_to_range(period)

    src = None
    if isinstance(message_or_cq, CallbackQuery) and message_or_cq.from_user:
        tl = await get_team_lead_by_tg_id(session, int(message_or_cq.from_user.id))
        if tl:
            src = str(tl.source).split(".")[-1]
    elif isinstance(message_or_cq, Message) and message_or_cq.from_user:
        tl = await get_team_lead_by_tg_id(session, int(message_or_cq.from_user.id))
        if tl:
            src = str(tl.source).split(".")[-1]

    reports = await list_duplicate_reports_in_range(
        session,
        manager_source=src,
        created_from=created_from,
        created_to=created_to,
        limit=200,
    )

    lines = ["⚠️ <b>Дубликаты</b>\n"]
    if not reports:
        lines.append("Пока нет записей.")
    else:
        lines.append(f"Всего: <b>{len(reports)}</b>\n")
        for r in reports[:40]:
            dt = r.created_at.strftime("%d.%m.%Y %H:%M") if r.created_at else "—"
            uname = f"@{r.manager_username}" if r.manager_username else "—"
            lines.append(
                f"• <code>{dt}</code> | <b>{r.bank_name}</b> | <code>{r.phone}</code> | {uname}"
            )
        if len(reports) > 40:
            lines.append(f"\n...и ещё <b>{len(reports) - 40}</b>")
    text = "\n".join(lines)
    kb = kb_tl_duplicates_list()

    if isinstance(message_or_cq, CallbackQuery):
        await message_or_cq.answer()
        if message_or_cq.message:
            try:
                await message_or_cq.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
            except Exception:
                try:
                    await message_or_cq.message.answer(text, reply_markup=kb, parse_mode="HTML")
                except Exception:
                    pass
        return
    await message_or_cq.answer(text, reply_markup=kb, parse_mode="HTML")


async def _render_tl_live_list_to_message(*, bot: object, chat_id: int, message_id: int, session: AsyncSession) -> None:
    forms = await list_pending_forms(session, limit=30)
    text = "📋 <b>Лайв анкеты</b>\n\n"
    if not forms:
        text += "Пока нет анкет в обработке."
    else:
        text += f"В очереди: <b>{len(forms)}</b>\n\nВыберите анкету:"
    kb = kb_tl_live_list(forms)
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        try:
            await bot.send_message(chat_id, text, reply_markup=kb)
        except Exception:
            return


async def _send_tl_form_view(*, bot: object, chat_id: int, session: AsyncSession, form) -> tuple[list[int], int | None]:
    mgr = await get_user_by_id(session, form.manager_id)
    manager_tag = mgr.manager_tag if mgr else "—"
    try:
        from bot.handlers.drop_manager import _format_form_text

        form_text = _format_form_text(form, manager_tag)
    except Exception:
        form_text = _format_form_for_group(form, manager_tag)

    dm_username = f"@{mgr.username}" if mgr and getattr(mgr, "username", None) else "—"
    text = f"От кого заявка: {dm_username}\n\n{form_text}"
    photos = list(getattr(form, "screenshots", None) or [])
    form_msg_ids: list[int] = []
    if not photos:
        try:
            m = await bot.send_message(chat_id, text, parse_mode="HTML")
            form_msg_ids.append(int(m.message_id))
        except Exception:
            return [], None
    elif len(photos) == 1:
        try:
            kind, fid = unpack_media_item(str(photos[0]))
            if kind == "doc":
                m = await bot.send_document(chat_id, fid, caption=text, parse_mode="HTML")
            elif kind == "video":
                m = await bot.send_video(chat_id, fid, caption=text, parse_mode="HTML")
            else:
                m = await bot.send_photo(chat_id, fid, caption=text, parse_mode="HTML")
            form_msg_ids.append(int(m.message_id))
        except Exception:
            return [], None
    else:
        photos = photos[:10]
        try:
            docs: list[str] = []
            media_items: list[str] = []
            for raw in photos:
                kind, _ = unpack_media_item(str(raw))
                if kind == "doc":
                    docs.append(str(raw))
                else:
                    media_items.append(str(raw))

            reply_to_message_id: int | None = None
            if media_items:
                media: list[InputMediaPhoto | InputMediaVideo] = []
                first_kind, first_fid = unpack_media_item(str(media_items[0]))
                if first_kind == "video":
                    media.append(InputMediaVideo(media=first_fid, caption=text, parse_mode="HTML"))
                else:
                    media.append(InputMediaPhoto(media=first_fid, caption=text, parse_mode="HTML"))
                for raw in media_items[1:]:
                    kind, fid = unpack_media_item(str(raw))
                    if kind == "video":
                        media.append(InputMediaVideo(media=fid))
                    else:
                        media.append(InputMediaPhoto(media=fid))
                msgs = list(await bot.send_media_group(chat_id, media) or [])
                form_msg_ids.extend(int(m.message_id) for m in msgs)
                if msgs:
                    reply_to_message_id = int(msgs[0].message_id)

            if docs:
                if not form_msg_ids:
                    # Only documents: keep caption on the first one.
                    _, first_fid = unpack_media_item(str(docs[0]))
                    m = await bot.send_document(chat_id, first_fid, caption=text, parse_mode="HTML")
                    form_msg_ids.append(int(m.message_id))
                    reply_to_message_id = int(m.message_id)
                    for raw in docs[1:]:
                        _, fid = unpack_media_item(str(raw))
                        dm = await bot.send_document(chat_id, fid, reply_to_message_id=reply_to_message_id)
                        form_msg_ids.append(int(dm.message_id))
                else:
                    # Attach docs to first album message.
                    for raw in docs:
                        _, fid = unpack_media_item(str(raw))
                        dm = await bot.send_document(chat_id, fid, reply_to_message_id=reply_to_message_id)
                        form_msg_ids.append(int(dm.message_id))
        except Exception:
            return [], None

    conditions = None
    bank_name = (getattr(form, "bank_name", None) or "").strip()
    if bank_name:
        bank = await get_bank_by_name(session, bank_name)
        tl_source = await _get_team_lead_source(session, int(chat_id))
        conditions = _format_bank_conditions_for_tl(bank, tl_source)
    bank_display = format_bank_hashtag(bank_name)
    if not conditions:
        conditions = f"📌 <b>Условия ({bank_display})</b>:\n<blockquote expandable>Условий нет</blockquote>"
    else:
        conditions = f"📌 <b>Условия ({bank_display})</b>:\n<blockquote expandable>{conditions}</blockquote>"
    controls_text = f"{conditions}\n\nВыберите действие:"
    try:
        controls = await bot.send_message(chat_id, controls_text, reply_markup=kb_form_review_with_back(int(form.id)), parse_mode="HTML")
    except Exception:
        return form_msg_ids, None

    return form_msg_ids, int(controls.message_id)


async def _cleanup_open_form_messages(*, bot: object, chat_id: int, state: FSMContext) -> None:
    data = await state.get_data()
    ids = list(data.get("tl_form_msg_ids") or [])
    if ids:
        await _safe_delete_messages(bot=bot, chat_id=chat_id, message_ids=[int(x) for x in ids])
    await state.update_data(tl_form_msg_ids=[])


async def _safe_delete_messages(*, bot: object, chat_id: int, message_ids: list[int]) -> None:
    for mid in message_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=int(mid))
        except Exception:
            pass


async def _get_team_lead_source(session: AsyncSession, tg_id: int) -> TeamLeadSource:
    tl = await get_team_lead_by_tg_id(session, tg_id)
    return getattr(tl, "source", None) or TeamLeadSource.TG


async def _list_banks_for_tl_source(session: AsyncSession, source: TeamLeadSource) -> list:
    banks = await list_banks(session)
    src = (str(source).split(".")[-1] if source else "TG").upper()

    def _has_fb(bank) -> bool:
        return bool((getattr(bank, "instructions_fb", None) or "").strip()) or getattr(bank, "required_screens_fb", None) is not None

    def _has_tg(bank) -> bool:
        return bool((getattr(bank, "instructions_tg", None) or "").strip()) or getattr(bank, "required_screens_tg", None) is not None

    def _has_legacy(bank) -> bool:
        return bool((getattr(bank, "instructions", None) or "").strip()) or getattr(bank, "required_screens", None) is not None

    if src == "FB":
        return [b for b in banks if _has_fb(b) or (_has_legacy(b) and not _has_tg(b))]
    return [b for b in banks if _has_tg(b) or (_has_legacy(b) and not _has_fb(b))]


def _tl_bank_items_with_source(banks: list, source: TeamLeadSource) -> list[tuple[int, str]]:
    src = (str(source).split(".")[-1] if source else "TG").upper()
    suffix = "FB" if src == "FB" else "TG"
    items: list[tuple[int, str]] = []
    for b in banks:
        name = str(getattr(b, "name", "") or "").strip()
        if not name:
            continue
        items.append((int(b.id), f"{name} ({suffix})"))
    return items


def _format_bank_conditions_for_tl(bank, source: TeamLeadSource) -> str | None:
    if not bank:
        return None
    src = (str(source).split(".")[-1] if source else "TG").upper()
    instructions = None
    required_screens = None
    if src == "FB":
        instructions = (getattr(bank, "instructions_fb", None) or getattr(bank, "instructions", None) or "").strip()
        required_screens = (
            getattr(bank, "required_screens_fb", None)
            if getattr(bank, "required_screens_fb", None) is not None
            else getattr(bank, "required_screens", None)
        )
    else:
        instructions = (getattr(bank, "instructions_tg", None) or getattr(bank, "instructions", None) or "").strip()
        required_screens = (
            getattr(bank, "required_screens_tg", None)
            if getattr(bank, "required_screens_tg", None) is not None
            else getattr(bank, "required_screens", None)
        )
    if not instructions and required_screens is None:
        return None
    req = "—" if required_screens is None else str(required_screens)
    return (
        f"{instructions or '—'}\n"
        f"Кол-во скринов: <b>{req}</b>"
    )


def _format_bank(
    bank_name: str,
    *,
    instructions: str | None,
    required_screens: int | None,
    instructions_tg: str | None,
    instructions_fb: str | None,
    required_screens_tg: int | None,
    required_screens_fb: int | None,
) -> str:
    req_any = str(required_screens) if required_screens is not None else "—"
    req_tg = str(required_screens_tg) if required_screens_tg is not None else "—"
    req_fb = str(required_screens_fb) if required_screens_fb is not None else "—"
    return (
        f"🏦 <b>{bank_name}</b>\n"
        f"- кол-во скринов (legacy): <b>{req_any}</b>\n"
        f"- кол-во скринов (TG): <b>{req_tg}</b>\n"
        f"- кол-во скринов (FB): <b>{req_fb}</b>\n"
        "\n"
        "<b>TG</b>:\n"
        f"{(instructions_tg or '').strip() or '—'}\n\n"
        "<b>FB</b>:\n"
        f"{(instructions_fb or '').strip() or '—'}\n\n"
        "<b>Legacy</b>:\n"
        f"{(instructions or '').strip() or '—'}"
    )


async def _send_photos(bot, chat_id: int, photos: list[str]) -> None:
    if not photos:
        return
    if len(photos) == 1:
        kind, fid = unpack_media_item(str(photos[0]))
        try:
            if kind == "doc":
                await bot.send_document(chat_id, fid)
            elif kind == "video":
                await bot.send_video(chat_id, fid)
            else:
                await bot.send_photo(chat_id, fid)
        except TelegramNetworkError:
            return
        return
    try:
        docs: list[str] = []
        media_items: list[str] = []
        for raw in photos[:10]:
            kind, _ = unpack_media_item(str(raw))
            if kind == "doc":
                docs.append(str(raw))
            else:
                media_items.append(str(raw))

        if media_items:
            media: list[InputMediaPhoto | InputMediaVideo] = []
            for raw in media_items:
                kind, fid = unpack_media_item(str(raw))
                if kind == "video":
                    media.append(InputMediaVideo(media=fid))
                else:
                    media.append(InputMediaPhoto(media=fid))
            await bot.send_media_group(chat_id, media)

        for raw in docs:
            _, fid = unpack_media_item(str(raw))
            await bot.send_document(chat_id, fid)
    except TelegramNetworkError:
        return


def _has_conditions(bank) -> bool:
    return any(
        [
            bool((getattr(bank, "instructions", None) or "").strip()),
            bool((getattr(bank, "instructions_tg", None) or "").strip()),
            bool((getattr(bank, "instructions_fb", None) or "").strip()),
            getattr(bank, "required_screens", None) is not None,
            getattr(bank, "required_screens_tg", None) is not None,
            getattr(bank, "required_screens_fb", None) is not None,
        ]
    )


async def _send_photos_with_caption(bot: object, chat_id: int, photos: list[str], caption: str) -> None:
    """
    Sends as:
    - 1 photo => send_photo(caption)
    - 2+ photos => send_media_group(first has caption)
    """
    if not photos:
        return
    photos = photos[:10]
    if len(photos) == 1:
        kind, fid = unpack_media_item(str(photos[0]))
        try:
            if kind == "doc":
                await bot.send_document(chat_id, fid, caption=caption, parse_mode="HTML")
            elif kind == "video":
                await bot.send_video(chat_id, fid, caption=caption, parse_mode="HTML")
            else:
                await bot.send_photo(chat_id, fid, caption=caption, parse_mode="HTML")
        except TelegramNetworkError:
            return
        return
    try:
        docs: list[str] = []
        media_items: list[str] = []
        for raw in photos:
            kind, _ = unpack_media_item(str(raw))
            if kind == "doc":
                docs.append(str(raw))
            else:
                media_items.append(str(raw))

        if media_items:
            media: list[InputMediaPhoto | InputMediaVideo] = []
            first_kind, first_fid = unpack_media_item(str(media_items[0]))
            if first_kind == "video":
                media.append(InputMediaVideo(media=first_fid, caption=caption, parse_mode="HTML"))
            else:
                media.append(InputMediaPhoto(media=first_fid, caption=caption, parse_mode="HTML"))
            for raw in media_items[1:]:
                kind, fid = unpack_media_item(str(raw))
                if kind == "video":
                    media.append(InputMediaVideo(media=fid))
                else:
                    media.append(InputMediaPhoto(media=fid))
            await bot.send_media_group(chat_id, media)

            for raw in docs:
                _, fid = unpack_media_item(str(raw))
                await bot.send_document(chat_id, fid)
            return

        # Only docs
        _, first_fid = unpack_media_item(str(docs[0]))
        await bot.send_document(chat_id, first_fid, caption=caption, parse_mode="HTML")
        for raw in docs[1:]:
            _, fid = unpack_media_item(str(raw))
            await bot.send_document(chat_id, fid)
    except TelegramNetworkError:
        return


async def _edit_text_or_caption(message: Message, text: str) -> None:
    """
    Callback messages can be either plain text messages or media messages (photo with caption).
    """
    try:
        if message.photo or message.document or message.video:
            await message.edit_caption(caption=text, parse_mode="HTML")
        else:
            await message.edit_text(text)
    except Exception:
        # best effort: do not crash handler on edit failures (already edited/deleted/etc)
        pass


def _format_form_for_group(form, manager_tag: str) -> str:
    traffic = "—"
    if form.traffic_type == "DIRECT":
        traffic = "Прямой"
    elif form.traffic_type == "REFERRAL":
        traffic = "Сарафан"
    bank_tag = format_bank_hashtag(getattr(form, "bank_name", None))
    return (
        "✅ <b>Заявка подтверждена</b>\n"
        f"Банк: <b>{bank_tag}</b>\n"
        f"Тип клиента: <b>{traffic}</b>\n"
        f"Тег менеджера: <b>{manager_tag}</b>\n"
        f"Комментарий: {form.comment or '—'}"
    )


@router.message(F.text.in_({"Лайв анкеты", "Условия для сдачи"}))
async def team_lead_menu(message: Message, session: AsyncSession) -> None:
    # Ignore group messages
    if message.chat.type in ['group', 'supergroup']:
        return
    if not message.from_user:
        return
    user = await get_user_by_tg_id(session, message.from_user.id)
    if not user or user.role != UserRole.TEAM_LEAD:
        return

    if message.text == "Лайв анкеты":
        await _render_tl_live_list(message, session)
        return

    # banks
    src = await _get_team_lead_source(session, message.from_user.id)
    banks = await _list_banks_for_tl_source(session, src)
    items = _tl_bank_items_with_source(banks, src)
    await message.answer("🏦 <b>Условия для сдачи</b>", reply_markup=kb_banks_list(items))


@router.callback_query(TeamLeadMenuCb.filter(F.action == "home"))
async def tl_home(cq: CallbackQuery, session: AsyncSession) -> None:
    if not cq.from_user:
        return
    if not await is_team_lead(session, cq.from_user.id):
        await cq.answer("Нет прав", show_alert=True)
        return
    await cq.answer()
    if cq.message:
        live_cnt = await count_pending_forms(session)
        await cq.message.edit_text("Главное меню:", reply_markup=kb_team_lead_inline_main(live_count=live_cnt))


@router.callback_query(TeamLeadMenuCb.filter(F.action == "live"))
async def tl_live(cq: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if not cq.from_user:
        return
    if not await is_team_lead(session, cq.from_user.id):
        await cq.answer("Нет прав", show_alert=True)
        return

    await _render_tl_live_list(cq, session)
    return


@router.callback_query(TeamLeadMenuCb.filter(F.action == "duplicates"))
async def tl_duplicates(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    if not cq.from_user:
        return
    if not await is_team_lead(session, cq.from_user.id):
        await cq.answer("Нет прав", show_alert=True)
        return
    await _render_tl_duplicates_list(cq, session, state)


@router.callback_query(F.data == "tl:dup_notice_open")
async def tl_duplicate_notice_open(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    if not cq.from_user:
        return
    if not await is_team_lead(session, cq.from_user.id):
        await cq.answer("Нет прав", show_alert=True)
        return
    try:
        await cq.answer()
    except Exception:
        pass
    notice_ids = pop_tl_duplicate_notices(int(cq.from_user.id))
    if notice_ids:
        for msg_id in notice_ids:
            try:
                await cq.bot.delete_message(chat_id=int(cq.from_user.id), message_id=int(msg_id))
            except Exception:
                pass
    await _render_tl_duplicates_list(cq, session, state)


@router.callback_query(F.data == "tl:dup_filter")
async def tl_dup_filter_menu_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    if not cq.from_user:
        return
    if not await is_team_lead(session, cq.from_user.id):
        await cq.answer("Нет прав", show_alert=True)
        return
    await cq.answer()
    data = await state.get_data()
    current = (data.get("dup_period") or "today")
    if cq.message:
        await cq.message.edit_text("📅 <b>Фильтр дубликатов</b>", reply_markup=kb_tl_duplicate_filter_menu(current=current))


@router.callback_query(F.data.startswith("tl:dup_filter_set:"))
async def tl_dup_filter_set_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    if not cq.from_user:
        return
    if not await is_team_lead(session, cq.from_user.id):
        await cq.answer("Нет прав", show_alert=True)
        return
    await cq.answer()
    period = (cq.data or "").split(":")[-1]
    await state.update_data(dup_period=period, dup_created_from=None, dup_created_to=None)
    await _render_tl_duplicates_list(cq, session, state)


@router.callback_query(F.data == "tl:dup_filter_custom")
async def tl_dup_filter_custom_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    if not cq.from_user:
        return
    if not await is_team_lead(session, cq.from_user.id):
        await cq.answer("Нет прав", show_alert=True)
        return
    await cq.answer()
    await state.set_state(TeamLeadStates.duplicates_filter_range)
    if cq.message:
        await cq.message.edit_text("Введите интервал дат в формате: <code>DD.MM.YYYY-DD.MM.YYYY</code>")


@router.message(TeamLeadStates.duplicates_filter_range, F.text)
async def tl_dup_filter_range_msg(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not message.from_user:
        return
    if not await is_team_lead(session, message.from_user.id):
        return
    raw = (message.text or "").strip().replace(" ", "")
    if "-" not in raw:
        await message.answer("Неверный формат. Пример: <code>21.01.2026-31.01.2026</code>")
        return
    a, b = raw.split("-", 1)
    try:
        d1 = datetime.strptime(a, "%d.%m.%Y")
        d2 = datetime.strptime(b, "%d.%m.%Y")
    except ValueError:
        await message.answer("Неверный формат. Пример: <code>21.01.2026-31.01.2026</code>")
        return
    if d2 < d1:
        d1, d2 = d2, d1
    created_from = datetime(d1.year, d1.month, d1.day)
    created_to = datetime(d2.year, d2.month, d2.day) + timedelta(days=1)
    await state.update_data(dup_period="custom", dup_created_from=created_from, dup_created_to=created_to)
    await state.set_state(None)
    await _render_tl_duplicates_list(message, session, state)


@router.callback_query(F.data.startswith("tl:live_open:"))
async def tl_live_open_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not cq.from_user:
        return
    if not await is_team_lead(session, cq.from_user.id):
        await cq.answer("Нет прав", show_alert=True)
        return
    try:
        form_id = int((cq.data or "").split(":")[-1])
    except Exception:
        await cq.answer("Некорректная кнопка", show_alert=True)
        return

    form = await get_form(session, form_id)
    if not form or form.status != FormStatus.PENDING:
        await cq.answer("Анкета не найдена", show_alert=True)
        return

    await cq.answer()
    chat_id = int(cq.message.chat.id if cq.message else cq.from_user.id)

    # cleanup previous opened form messages (if any)
    await _cleanup_open_form_messages(bot=cq.bot, chat_id=chat_id, state=state)

    # delete short notice if exists
    notice_id = pop_tl_form_notice(int(cq.from_user.id), int(form.id))
    if notice_id:
        try:
            await cq.bot.delete_message(chat_id=chat_id, message_id=int(notice_id))
        except Exception:
            pass

    form_msg_ids, controls_msg_id = await _send_tl_form_view(bot=cq.bot, chat_id=chat_id, session=session, form=form)
    await state.update_data(
        tl_form_msg_ids=form_msg_ids,
        tl_controls_msg_id=int(controls_msg_id) if controls_msg_id else None,
    )


@router.callback_query(TeamLeadMenuCb.filter(F.action == "banks"))
async def tl_banks(cq: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    from bot.keyboards import kb_banks_list
    if not cq.from_user:
        return
    if not await is_team_lead(session, cq.from_user.id):
        await cq.answer("Нет прав", show_alert=True)
        return
    try:
        await cq.answer()
    except Exception:
        pass
    src = await _get_team_lead_source(session, cq.from_user.id)
    banks = await _list_banks_for_tl_source(session, src)
    items = _tl_bank_items_with_source(banks, src)
    if cq.message:
        await cq.message.edit_text("🏦 <b>Условия для сдачи</b>", reply_markup=kb_banks_list(items))


@router.callback_query(BankCb.filter(F.action == "open"))
async def bank_open(cq: CallbackQuery, callback_data: BankCb, session: AsyncSession, settings: Settings) -> None:
    if not cq.from_user:
        return
    if not await is_team_lead(session, cq.from_user.id):
        await cq.answer("Нет прав", show_alert=True)
        return
    bank = await get_bank(session, int(callback_data.bank_id))
    if not bank:
        await cq.answer("Банк не найден", show_alert=True)
        return
    src = await _get_team_lead_source(session, cq.from_user.id)
    cond = _format_bank_conditions_for_tl(bank, src)
    if cond is None:
        await cq.answer("Банк недоступен для вашего источника", show_alert=True)
        return
    text = f"🏦 <b>{bank.name}</b>\n\n{cond}"
    has_cond = _has_conditions(bank)
    await cq.answer()
    if cq.message:
        await cq.message.answer(text, reply_markup=kb_bank_open(bank.id, has_conditions=has_cond))


@router.callback_query(BankCb.filter(F.action == "edit"))
async def bank_edit_menu(cq: CallbackQuery, callback_data: BankCb, session: AsyncSession, settings: Settings) -> None:
    if not cq.from_user:
        return
    if not await is_team_lead(session, cq.from_user.id):
        await cq.answer("Нет прав", show_alert=True)
        return
    bank = await get_bank(session, int(callback_data.bank_id))
    if not bank:
        await cq.answer("Банк не найден", show_alert=True)
        return
    await cq.answer()
    src = await _get_team_lead_source(session, cq.from_user.id)
    if cq.message:
        await cq.message.edit_text(
            f"Редактирование <b>{bank.name}</b>:",
            reply_markup=kb_bank_edit_for_source(bank.id, source=str(src).split(".")[-1]),
        )


@router.callback_query(BankCb.filter(F.action == "setup"))
async def bank_setup_start(cq: CallbackQuery, callback_data: BankCb, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not cq.from_user:
        return
    if not await is_team_lead(session, cq.from_user.id):
        await cq.answer("Нет прав", show_alert=True)
        return
    bank = await get_bank(session, int(callback_data.bank_id))
    if not bank:
        await cq.answer("Банк не найден", show_alert=True)
        return
    await cq.answer()
    await state.clear()
    src = await _get_team_lead_source(session, cq.from_user.id)
    await state.update_data(bank_id=bank.id)
    await state.set_state(TeamLeadStates.bank_instructions)
    await state.update_data(edit_field=("instructions_fb" if src == TeamLeadSource.FB else "instructions_tg"))
    if cq.message:
        await cq.message.answer(
            "Напишите <b>пару предложений</b> с условиями и требованиями.",
            reply_markup=kb_back(),
        )
        await cq.message.answer(f"Условия для банка <b>{bank.name}</b>:")
    await state.update_data(return_to="bank_open")


@router.callback_query(BankCb.filter(F.action == "create"))
async def bank_create_start(cq: CallbackQuery, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not cq.from_user:
        return
    if not await is_team_lead(session, cq.from_user.id):
        await cq.answer("Нет прав", show_alert=True)
        return
    await cq.answer()
    await state.clear()
    src = await _get_team_lead_source(session, cq.from_user.id)
    await state.set_state(TeamLeadStates.bank_custom_name)
    await state.update_data(return_to="banks_list", edit_field=("instructions_fb" if src == TeamLeadSource.FB else "instructions_tg"))
    if cq.message:
        await cq.message.answer(
            "Сейчас создадим условия для банка:\n"
            "- сначала <b>название банка</b>\n"
            "- потом <b>2–3 предложения условий</b>",
            reply_markup=kb_back(),
        )
        await cq.message.answer("Введите название банка:")


@router.message(TeamLeadStates.bank_custom_name, F.text & (F.text != "Назад"))
async def bank_create_name(message: Message, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not message.from_user:
        return
    if not await is_team_lead(session, message.from_user.id):
        return
    name = message.text.strip()
    if not name or len(name) > 64:
        await message.answer("Введите корректное название (до 64 символов):")
        return
    existing = await get_bank_by_name(session, name)
    if existing:
        await state.clear()
        await message.answer(f"⚠️ Банк <b>{existing.name}</b> уже существует.")
        src = await _get_team_lead_source(session, message.from_user.id)
        banks = await _list_banks_for_tl_source(session, src)
        items = _tl_bank_items_with_source(banks, src)
        await message.answer("Выберите банк:", reply_markup=kb_banks_list(items))
        return

    data = await state.get_data()
    edit_field = data.get("edit_field")
    bank = await create_bank(session, name)
    await state.clear()
    # Immediately continue with conditions wizard
    await state.update_data(bank_id=bank.id)
    await state.set_state(TeamLeadStates.bank_instructions)
    if edit_field in {"instructions_fb", "instructions_tg"}:
        await state.update_data(edit_field=edit_field)
    await message.answer(f"✅ Создан банк <b>{bank.name}</b>.")
    await message.answer("Теперь отправьте текст условий (можно несколько строк):", reply_markup=kb_back())
    await state.update_data(return_to="banks_list")


@router.callback_query(BankEditCb.filter())
async def bank_edit_action(cq: CallbackQuery, callback_data: BankEditCb, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not cq.from_user:
        return
    if not await is_team_lead(session, cq.from_user.id):
        await cq.answer("Нет прав", show_alert=True)
        return
    bank = await get_bank(session, callback_data.bank_id)
    if not bank:
        await cq.answer("Банк не найден", show_alert=True)
        return

    src = await _get_team_lead_source(session, cq.from_user.id)
    allowed = {
        TeamLeadSource.TG: {"rename", "instructions_tg", "required_tg", "delete", "back"},
        TeamLeadSource.FB: {"rename", "instructions_fb", "required_fb", "delete", "back"},
    }
    if callback_data.action not in allowed.get(src, set()):
        await cq.answer("Нет прав", show_alert=True)
        return

    await cq.answer()
    if callback_data.action == "back":
        if cq.message:
            cond = _format_bank_conditions_for_tl(bank, src)
            text = f"🏦 <b>{bank.name}</b>\n\n{cond or '—'}"
            has_cond = _has_conditions(bank)
            await cq.message.edit_text(text, reply_markup=kb_bank_open(bank.id, has_conditions=has_cond))
        return

    if callback_data.action == "delete":
        ok = await delete_bank_condition(session, int(bank.id))
        await state.clear()
        if not ok:
            await cq.answer("Банк не найден", show_alert=True)
            return
        await cq.answer("Банк удалён")
        await _notify_active_dms_banks_updated(cq.bot, session)
        banks = await _list_banks_for_tl_source(session, src)
        items = _tl_bank_items_with_source(banks, src)
        if cq.message:
            await cq.message.answer("✅ Банк удалён.\n🏦 <b>Условия для сдачи</b>", reply_markup=kb_banks_list(items))
        return

    await state.clear()
    await state.update_data(bank_id=bank.id)

    if callback_data.action == "rename":
        await state.set_state(TeamLeadStates.bank_rename_name)
        await state.update_data(return_to="edit_menu")
        if cq.message:
            await cq.message.answer(
                f"Введите новое название банка (сейчас: <b>{bank.name}</b>):",
                reply_markup=kb_back(),
            )
        return

    if callback_data.action in {"instructions_tg", "instructions_fb"}:
        await state.set_state(TeamLeadStates.bank_instructions)
        await state.update_data(return_to="edit_menu", edit_field=callback_data.action)
        if cq.message:
            await cq.message.answer("Введите текст условий (можно несколько строк):", reply_markup=kb_back())
        return

    if callback_data.action in {"required_tg", "required_fb"}:
        await state.set_state(TeamLeadStates.bank_required_screens)
        await state.update_data(return_to="edit_menu", edit_field=callback_data.action)
        if cq.message:
            await cq.message.answer(
                "Введите число (сколько скринов нужно) или 0 чтобы снять требование:",
                reply_markup=kb_back(),
            )
        return

    await cq.answer("Неизвестное действие", show_alert=True)
    return


@router.message(TeamLeadStates.bank_rename_name, F.text & (F.text != "Назад"))
async def bank_rename_name(message: Message, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not message.from_user:
        return
    if not await is_team_lead(session, message.from_user.id):
        return

    data = await state.get_data()
    bank_id_raw = data.get("bank_id")
    if not bank_id_raw:
        await state.clear()
        await message.answer("⚠️ Сессия сбилась. Зайдите в 'Условия для сдачи' и выберите банк заново.")
        return

    name = (message.text or "").strip()
    if not name or len(name) > 64:
        await message.answer("Введите корректное название (до 64 символов):")
        return

    bank = await get_bank(session, int(bank_id_raw))
    if not bank:
        await state.clear()
        await message.answer("Банк не найден")
        return

    existing = await get_bank_by_name(session, name)
    if existing and int(existing.id) != int(bank.id):
        await message.answer("⚠️ Банк с таким названием уже существует. Введите другое название:")
        return

    await update_bank(session, int(bank.id), name=name)
    await state.clear()
    await _notify_active_dms_banks_updated(message.bot, session)
    await message.answer("✅ Название банка обновлено.", reply_markup=kb_team_lead_inline_main())


@router.message(TeamLeadStates.bank_rename_name, F.text == "Назад")
async def bank_rename_back(message: Message, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not message.from_user:
        return
    if not await is_team_lead(session, message.from_user.id):
        return

    data = await state.get_data()
    bank_id = data.get("bank_id")
    await state.clear()

    if bank_id:
        bank = await get_bank(session, int(bank_id))
        if bank:
            src = await _get_team_lead_source(session, message.from_user.id)
            await message.answer(
                f"Редактирование <b>{bank.name}</b>:",
                reply_markup=kb_bank_edit_for_source(bank.id, source=str(src).split(".")[-1]),
            )
            return

    src = await _get_team_lead_source(session, message.from_user.id)
    banks = await _list_banks_for_tl_source(session, src)
    items = _tl_bank_items_with_source(banks, src)
    await message.answer("🏦 <b>Условия для сдачи</b>", reply_markup=kb_banks_list(items))


@router.message(TeamLeadStates.bank_instructions, F.text & (F.text != "Назад"))
async def bank_set_instructions(message: Message, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not message.from_user:
        return
    if not await is_team_lead(session, message.from_user.id):
        return
    data = await state.get_data()
    bank_id_raw = data.get("bank_id")
    if not bank_id_raw:
        await state.clear()
        await message.answer("⚠️ Сессия сбилась. Зайдите в 'Условия для сдачи' и выберите банк заново.")
        return
    bank_id = int(bank_id_raw)
    data = await state.get_data()
    edit_field = data.get("edit_field")
    src = await _get_team_lead_source(session, message.from_user.id)
    if src == TeamLeadSource.FB and edit_field not in {None, "instructions_fb"}:
        await state.clear()
        await message.answer("Нет прав")
        return
    if src == TeamLeadSource.TG and edit_field not in {None, "instructions_tg"}:
        await state.clear()
        await message.answer("Нет прав")
        return
    txt = message.text.strip()
    if edit_field == "instructions_fb":
        await update_bank(session, bank_id, instructions_fb=txt)
    else:
        # default depends on source
        if src == TeamLeadSource.FB:
            await update_bank(session, bank_id, instructions_fb=txt)
        else:
            await update_bank(session, bank_id, instructions_tg=txt)
    await state.clear()
    await _notify_active_dms_banks_updated(message.bot, session)
    await message.answer("✅ Условия обновлены.", reply_markup=kb_team_lead_inline_main())


@router.message(TeamLeadStates.bank_instructions, F.text == "Назад")
async def bank_instructions_back(message: Message, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not message.from_user:
        return
    if not await is_team_lead(session, message.from_user.id):
        return
    data = await state.get_data()
    bank_id = data.get("bank_id")
    return_to = data.get("return_to")
    await state.clear()

    # Remove the "Назад" reply keyboard and render target screen
    if return_to == "edit_menu" and bank_id:
        bank = await get_bank(session, int(bank_id))
        if bank:
            src = await _get_team_lead_source(session, message.from_user.id)
            await message.answer(
                f"Редактирование <b>{bank.name}</b>:",
                reply_markup=kb_bank_edit_for_source(bank.id, source=str(src).split(".")[-1]),
            )
            return

    if return_to == "bank_open" and bank_id:
        bank = await get_bank(session, int(bank_id))
        if bank:
            src = await _get_team_lead_source(session, message.from_user.id)
            cond = _format_bank_conditions_for_tl(bank, src)
            text = f"🏦 <b>{bank.name}</b>\n\n{cond or '—'}"
            has_cond = _has_conditions(bank)
            await message.answer(text, reply_markup=kb_bank_open(bank.id, has_conditions=has_cond))
            return

    # default: banks list
    src = await _get_team_lead_source(session, message.from_user.id)
    banks = await _list_banks_for_tl_source(session, src)
    items = _tl_bank_items_with_source(banks, src)
    await message.answer("🏦 <b>Условия для сдачи</b>", reply_markup=kb_banks_list(items))


@router.message(TeamLeadStates.bank_required_screens, F.text & (F.text != "Назад"))
async def bank_set_required(message: Message, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not message.from_user:
        return
    if not await is_team_lead(session, message.from_user.id):
        return
    txt = message.text.strip()
    if not txt.isdigit():
        await message.answer("Нужно число. Пример: 3")
        return
    n = int(txt)
    if n < 0 or n > 20:
        await message.answer("Число должно быть 0..20")
        return
    data = await state.get_data()
    bank_id_raw = data.get("bank_id")
    if not bank_id_raw:
        await state.clear()
        await message.answer("⚠️ Сессия сбилась. Зайдите в 'Условия для сдачи' и выберите банк заново.")
        return
    bank_id = int(bank_id_raw)
    edit_field = data.get("edit_field")
    src = await _get_team_lead_source(session, message.from_user.id)
    if src == TeamLeadSource.FB and edit_field not in {None, "required_fb"}:
        await state.clear()
        await message.answer("Нет прав")
        return
    if src == TeamLeadSource.TG and edit_field not in {None, "required_tg"}:
        await state.clear()
        await message.answer("Нет прав")
        return
    val = None if n == 0 else n
    if edit_field == "required_fb":
        await update_bank(session, bank_id, required_screens_fb=val)
    else:
        # default depends on source
        if src == TeamLeadSource.FB:
            await update_bank(session, bank_id, required_screens_fb=val)
        else:
            await update_bank(session, bank_id, required_screens_tg=val)
    await state.clear()
    await _notify_active_dms_banks_updated(message.bot, session)
    await message.answer("✅ Кол-во скринов обновлено.", reply_markup=kb_team_lead_inline_main())


@router.message(TeamLeadStates.bank_required_screens, F.text == "Назад")
async def bank_required_back(message: Message, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not message.from_user:
        return
    if not await is_team_lead(session, message.from_user.id):
        return
    data = await state.get_data()
    bank_id = data.get("bank_id")
    return_to = data.get("return_to")
    await state.clear()

    if return_to == "edit_menu" and bank_id:
        bank = await get_bank(session, int(bank_id))
        if bank:
            src = await _get_team_lead_source(session, message.from_user.id)
            await message.answer(
                f"Редактирование <b>{bank.name}</b>:",
                reply_markup=kb_bank_edit_for_source(bank.id, source=str(src).split(".")[-1]),
            )
            return

    # default: open bank card if possible; otherwise banks list
    if bank_id:
        bank = await get_bank(session, int(bank_id))
        if bank:
            src = await _get_team_lead_source(session, message.from_user.id)
            cond = _format_bank_conditions_for_tl(bank, src)
            text = f"🏦 <b>{bank.name}</b>\n\n{cond or '—'}"
            has_cond = _has_conditions(bank)
            await message.answer(text, reply_markup=kb_bank_open(bank.id, has_conditions=has_cond))
            return

    src = await _get_team_lead_source(session, message.from_user.id)
    banks = await _list_banks_for_tl_source(session, src)
    items = _tl_bank_items_with_source(banks, src)
    await message.answer("🏦 <b>Условия для сдачи</b>", reply_markup=kb_banks_list(items))


@router.message(TeamLeadStates.bank_custom_name, F.text == "Назад")
async def bank_custom_name_back(message: Message, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not message.from_user:
        return
    if not await is_team_lead(session, message.from_user.id):
        return
    await state.clear()
    src = await _get_team_lead_source(session, message.from_user.id)
    banks = await _list_banks_for_tl_source(session, src)
    items = _tl_bank_items_with_source(banks, src)
    await message.answer("🏦 <b>Условия для сдачи</b>", reply_markup=kb_banks_list(items))


@router.callback_query(FormReviewCb.filter())
async def review_form(
    cq: CallbackQuery,
    callback_data: FormReviewCb,
    session: AsyncSession,
    state: FSMContext,
    settings: Settings,
) -> None:
    if not cq.from_user:
        return
    if not await is_team_lead(session, cq.from_user.id):
        await cq.answer("Нет прав", show_alert=True)
        return

    form = await get_form(session, callback_data.form_id)
    if not form:
        await cq.answer("Анкета не найдена", show_alert=True)
        return

    if callback_data.action == "approve":
        await set_form_status(session, form.id, FormStatus.APPROVED, team_lead_comment=None)
        manager = await get_user_by_id(session, form.manager_id)
        manager_tg_id = manager.tg_id if manager else None
        manager_tag = manager.manager_tag if manager and manager.manager_tag else "—"

        # notify manager with full form (and screenshots) + edit button
        if manager_tg_id:
            try:
                bank = format_bank_hashtag(getattr(form, "bank_name", None))
                b = InlineKeyboardBuilder()
                b.button(text="Перейти", callback_data=f"dm:approved_no_pay_open:{int(form.id)}")
                b.adjust(1)
                notice = await cq.bot.send_message(
                    manager_tg_id,
                    f"✅ Апрувнули анкету <code>{form.id}</code>\nБанк: <b>{bank}</b>",
                    parse_mode="HTML",
                    reply_markup=b.as_markup(),
                )
                register_dm_approved_notice(int(manager_tg_id), int(notice.message_id))
            except Exception:
                log.exception("Failed to notify manager about approval")

        # post to forwarding group bound to the drop manager
        target_chat_id: int | None = None
        if manager and getattr(manager, "forward_group_id", None):
            g = await get_forward_group_by_id(session, int(manager.forward_group_id))
            if g:
                target_chat_id = int(g.chat_id)

        # legacy fallback
        if target_chat_id is None and settings.group_chat_id:
            target_chat_id = int(settings.group_chat_id)

        if target_chat_id is not None:
            try:
                group_text = _format_form_for_group(form, manager_tag)
                await cq.bot.send_message(target_chat_id, group_text, reply_markup=None)
            except Exception:
                log.exception("Failed to post to group")

        # delete form messages (album/text) + controls from TL chat
        data = await state.get_data()
        ids = list(data.get("tl_form_msg_ids") or [])
        if ids and cq.message:
            await _safe_delete_messages(bot=cq.bot, chat_id=int(cq.message.chat.id), message_ids=[int(x) for x in ids])
        controls_msg_id = data.get("tl_controls_msg_id")
        if controls_msg_id:
            await _safe_delete_messages(bot=cq.bot, chat_id=int(cq.message.chat.id), message_ids=[int(controls_msg_id)])
        await state.update_data(tl_form_msg_ids=[], tl_controls_msg_id=None)

        notice_id = pop_tl_form_notice(int(cq.from_user.id), int(form.id))
        if notice_id:
            try:
                await cq.bot.delete_message(chat_id=int(cq.message.chat.id), message_id=int(notice_id))
            except Exception:
                pass

        await cq.answer("Подтверждено")
        if cq.message:
            try:
                await cq.message.answer(
                    "Вы успешно подтвердили анкету. "
                    "В случае ошибки проверки с вашей стороны, вы несете ответственность с менеджером."
                )
            except Exception:
                pass
        await _render_tl_live_list(cq, session)
        return

    if callback_data.action == "reject":
        await state.set_state(TeamLeadStates.reject_comment)
        await state.update_data(form_id=form.id)
        await cq.answer()
        if cq.message:
            m = await cq.message.answer(
                "Напишите комментарий, что не так (он уйдёт менеджеру):",
                reply_markup=kb_tl_reject_back_inline(),
            )
            await state.update_data(tl_reject_prompt_msg_id=int(m.message_id))
        return

    await cq.answer("Неизвестное действие", show_alert=True)


@router.callback_query(F.data == "tl:reject_back")
async def tl_reject_back_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await cq.answer()
    data = await state.get_data()
    prompt_id = data.get("tl_reject_prompt_msg_id")
    if prompt_id and cq.message:
        try:
            await cq.bot.delete_message(chat_id=int(cq.message.chat.id), message_id=int(prompt_id))
        except Exception:
            pass
    await state.clear()
    await _render_tl_live_list(cq, session)


@router.message(TeamLeadStates.reject_comment, F.text)
async def reject_comment(message: Message, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not message.from_user:
        return
    if not await is_team_lead(session, message.from_user.id):
        return
    data = await state.get_data()
    form_id = int(data["form_id"])
    comment = message.text.strip()

    form = await get_form(session, form_id)
    if not form:
        await state.clear()
        await message.answer("Анкета не найдена.", reply_markup=kb_team_lead_inline_main())
        return

    await set_form_status(session, form_id, FormStatus.REJECTED, team_lead_comment=comment)
    manager = await get_user_by_id(session, form.manager_id)
    if manager:
        try:
            tl_comment = (comment or "").strip() or "Комментария нет"
            text = f"❌ Отклонена: <code>{form.id}</code>\nКомментарий: {tl_comment}"
            notice = await message.bot.send_message(
                manager.tg_id,
                text,
                reply_markup=kb_dm_reject_notice(form.id),
                parse_mode="HTML",
            )
            register_dm_reject_notice(int(manager.tg_id), int(form.id), int(notice.message_id))
        except Exception:
            log.exception("Failed to notify manager about rejection")

    # delete form messages (album/text) + controls from TL chat
    data2 = await state.get_data()
    ids = list(data2.get("tl_form_msg_ids") or [])
    if ids:
        await _safe_delete_messages(bot=message.bot, chat_id=int(message.chat.id), message_ids=[int(x) for x in ids])

    controls_msg_id = data2.get("tl_controls_msg_id")
    if controls_msg_id:
        await _safe_delete_messages(bot=message.bot, chat_id=int(message.chat.id), message_ids=[int(controls_msg_id)])

    notice_id = pop_tl_form_notice(int(message.from_user.id), int(form.id))
    if notice_id:
        try:
            await message.bot.delete_message(chat_id=int(message.chat.id), message_id=int(notice_id))
        except Exception:
            pass

    prompt_id = data2.get("tl_reject_prompt_msg_id")
    if prompt_id:
        try:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=int(prompt_id))
        except Exception:
            pass

    await state.clear()
    if controls_msg_id:
        await _render_tl_live_list_to_message(bot=message.bot, chat_id=int(message.chat.id), message_id=int(controls_msg_id), session=session)
        return
    await _render_tl_live_list(message, session)


