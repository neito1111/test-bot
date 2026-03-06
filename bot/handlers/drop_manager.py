from __future__ import annotations

import asyncio
import html
import logging
import re
from datetime import datetime, timedelta
from typing import Any
import time

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InputMediaDocument, InputMediaPhoto, InputMediaVideo, Message, ReplyKeyboardRemove
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError

from bot.callbacks import FormEditCb
from bot.config import Settings
from bot.keyboards import (
    DEFAULT_BANKS,
    kb_back,
    kb_dm_back_cancel_inline,
    kb_dm_back_to_menu_inline,
    kb_dm_bank_select_inline_from_items,
    kb_dm_done_inline,
    kb_dm_edit_bank_select_inline_from_items,
    kb_dm_edit_done_inline,
    kb_dm_edit_actions_inline,
    kb_dm_edit_screens_inline,
    kb_dm_main_inline,
    kb_dm_payment_card_with_back,
    kb_dm_approved_attach_type_pick,
    kb_dm_payment_next_actions,
    kb_dm_shift_comment_inline,
    kb_dm_source_pick_inline,
    kb_dm_resource_menu,
    kb_dm_resource_banks,
    kb_dm_resource_bank_actions,
    kb_dm_resource_empty_bank,
    kb_dm_resource_type_pick,
    kb_dm_resource_active_list,
    kb_dm_resource_active_actions,
    kb_dm_resource_attach_forms,
    kb_dm_resource_used_list,
    kb_dm_resource_used_actions,
    kb_dm_traffic_type_inline,
    kb_dm_forms_filter_menu,
    kb_dm_my_forms_list,
    kb_dm_my_form_open,
    kb_dm_duplicate_bank_phone_inline,
    kb_edit_open,
    kb_edit_fields,
    kb_form_confirm,
    kb_form_confirm_with_edit,
    kb_tl_duplicate_notice,
    kb_traffic_type,
    kb_traffic_type_with_back,
    kb_yes_no,
    kb_back_with_main,
)
from bot.middlewares import GroupMessageFilter
from bot.models import Form, FormStatus, ResourceType, Shift, User, UserRole
from bot.repositories import (
    create_bank,
    create_duplicate_report,
    create_form,
    delete_form,
    ensure_default_banks,
    list_banks,
    list_team_lead_ids_by_source,
    end_shift,
    get_active_shift,
    get_bank,
    get_bank_by_name,
    get_forward_group_by_id,
    get_form,
    get_user_by_id,
    get_user_by_tg_id,
    list_users,
    find_forms_by_phone,
    list_dm_approved_without_payment,
    list_dm_active_pool_items,
    list_free_pool_items_for_bank,
    assign_pool_item_to_dm,
    count_dm_active_pool_items,
    count_dm_active_pool_items_for_bank,
    get_pool_item,
    release_pool_item,
    mark_pool_item_invalid,
    mark_pool_item_used_with_form,
    list_dm_used_pool_items,
    form_has_linked_pool_item,
    list_user_forms_in_range,
    list_rejected_forms_by_user_id,
    count_rejected_forms_by_user_id,
    mark_form_payment_done,
    start_shift,
)
from bot.states import (
    DropManagerEditStates,
    DropManagerFormStates,
    DropManagerRejectedStates,
    DropManagerShiftStates,
    DropManagerMyFormsStates,
    DropManagerPaymentStates,
    DropManagerResourceStates,
)
from bot.utils import (
    extract_forward_payload,
    format_bank_hashtag,
    format_timedelta_seconds,
    format_user_payload,
    is_valid_phone,
    normalize_phone,
    pack_media_item,
    pop_dm_approved_notices,
    pop_dm_reject_notice,
    unpack_media_item,
    register_tl_duplicate_notice,
    register_tl_form_notice,
)

router = Router(name="drop_manager")
# Apply group message filter to all handlers in this router
router.message.filter(GroupMessageFilter())
log = logging.getLogger(__name__)

_album_ack_tasks: dict[tuple[int, str], asyncio.Task] = {}
_album_counts: dict[tuple[int, str], int] = {}
_album_last_sent_ts: dict[tuple[int, str], float] = {}


def _format_payment_phone(phone: str | None) -> str:
    if not phone:
        return "—"
    return normalize_phone(phone)


def _normalize_card(s: str) -> str:
    return "".join(ch for ch in (s or "") if ch.isdigit())


def _bank_duplicate_key(bank_name: str | None) -> str:
    txt = (bank_name or "").strip()
    if not txt:
        return ""

    # New format: core bank name in quotes -> use quoted core only
    m = re.search(r'["«](.*?)["»]', txt)
    if m:
        core = re.sub(r"\s+", " ", (m.group(1) or "").strip())
        return core.lower()

    # Legacy fallback: normalize old names like "Альянс 50к" / "Альянс-500"
    base = re.sub(r"\s+", " ", txt.split("-", 1)[0]).strip()
    token = (base.split(" ", 1)[0] if base else "").strip()
    m2 = re.match(r"([A-Za-zА-Яа-яЁёІіЇїЄє]+)", token)
    if m2:
        token = m2.group(1)
    return token.lower()


async def _phone_bank_duplicate_exists_by_key(
    session: AsyncSession,
    *,
    phone: str,
    bank_name: str,
    exclude_form_id: int | None = None,
) -> Form | None:
    key = _bank_duplicate_key(bank_name)
    if not key:
        return None
    forms = await find_forms_by_phone(session, phone)
    for f in forms:
        if exclude_form_id is not None and int(f.id) == int(exclude_form_id):
            continue
        if _bank_duplicate_key(getattr(f, "bank_name", None)) == key:
            return f
    return None


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


async def _send_shift_report_to_forward_group(*, bot: Any, session: AsyncSession, user: Any, report: str) -> None:
    group_id = getattr(user, "forward_group_id", None)
    if not group_id:
        try:
            await bot.send_message(int(user.tg_id), "Группа пересылки не привязана — отчет не отправлен. Попросите разработчика привязать группу.")
        except Exception:
            pass
        return
    g = await get_forward_group_by_id(session, int(group_id))
    if not g:
        try:
            await bot.send_message(int(user.tg_id), "Группа пересылки не найдена — отчет не отправлен. Попросите разработчика перепривязать группу.")
        except Exception:
            pass
        return
    try:
        await bot.send_message(int(g.chat_id), report, parse_mode="HTML")
    except Exception:
        try:
            await bot.send_message(int(user.tg_id), "Не удалось отправить отчет в группу. Проверьте, что бот добавлен в группу и имеет права.")
        except Exception:
            pass


async def _maybe_send_team_total_report(*, bot: Any, session: AsyncSession, source: str | None) -> None:
    src = (source or "TG").upper()

    # Send team summary only when no active DM shifts remain for this source.
    active_res = await session.execute(
        select(func.count(Shift.id))
        .select_from(Shift)
        .join(User, User.id == Shift.manager_id)
        .where(
            and_(
                Shift.ended_at.is_(None),
                User.role == UserRole.DROP_MANAGER,
                func.upper(func.coalesce(User.manager_source, "TG")) == src,
            )
        )
    )
    if int(active_res.scalar() or 0) > 0:
        return

    now = datetime.utcnow()
    start = datetime(now.year, now.month, now.day)
    end = start + timedelta(days=1)

    agg = await session.execute(
        select(Form.bank_name, Form.traffic_type, func.count(Form.id))
        .select_from(Form)
        .join(User, User.id == Form.manager_id)
        .where(
            and_(
                Form.created_at >= start,
                Form.created_at < end,
                Form.status != FormStatus.IN_PROGRESS,
                Form.bank_name.is_not(None),
                func.trim(Form.bank_name) != "",
                User.role == UserRole.DROP_MANAGER,
                func.upper(func.coalesce(User.manager_source, "TG")) == src,
            )
        )
        .group_by(Form.bank_name, Form.traffic_type)
        .order_by(Form.bank_name.asc())
    )
    rows = list(agg.all())
    if not rows:
        return

    bank_map: dict[str, dict[str, int]] = {}
    for bank_name, traffic_type, cnt in rows:
        bn = (bank_name or "").strip() or "Без банка"
        tt = (traffic_type or "—").strip() or "—"
        bank_map.setdefault(bn, {})[tt] = int(cnt or 0)

    seen = set(bank_map.keys())
    bank_order = [b for b in DEFAULT_BANKS if b in seen] + [b for b in sorted(seen) if b not in DEFAULT_BANKS]
    if "Без банка" in seen:
        bank_order = [b for b in bank_order if b != "Без банка"] + ["Без банка"]

    total_direct = 0
    total_referral = 0
    lines = [
        "📊 <b>Общий отчет команды за день</b>",
        f"Источник: <b>{src}</b>",
        "",
    ]
    for bank in bank_order:
        direct = bank_map.get(bank, {}).get("DIRECT", 0) + bank_map.get(bank, {}).get("—", 0)
        referral = bank_map.get(bank, {}).get("REFERRAL", 0)
        if direct == 0 and referral == 0:
            continue
        total_direct += direct
        total_referral += referral
        lines.append(f"{bank}:")
        lines.append(f"Прямой - <b>{direct}</b>")
        lines.append(f"Сарафан - <b>{referral}</b>")
        lines.append("")

    if lines and lines[-1] == "":
        lines.pop()
    lines.extend(["", "<b>Суммарно за день:</b>", f"Прямой - <b>{total_direct}</b>", f"Сарафан - <b>{total_referral}</b>"])

    report = "\n".join(lines)
    tl_ids = await list_team_lead_ids_by_source(session, src)
    for tl_tg_id in tl_ids:
        try:
            await bot.send_message(int(tl_tg_id), report)
        except Exception:
            continue


async def _best_effort_cleanup_recent_messages(*, bot: Any, chat_id: int, around_message_id: int | None, limit: int = 80) -> None:
    if not around_message_id:
        return
    start = max(1, int(around_message_id) - int(limit))
    end = int(around_message_id)
    for mid in range(end, start - 1, -1):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            continue


async def _load_my_forms(session: AsyncSession, *, user_id: int, state: FSMContext) -> list[Form]:
    data = await state.get_data()
    period = (data.get("my_forms_period") or "today")
    if period == "custom":
        created_from = data.get("my_forms_created_from")
        created_to = data.get("my_forms_created_to")
        return await list_user_forms_in_range(session, user_id=user_id, created_from=created_from, created_to=created_to)
    created_from, created_to = _period_to_range(period)
    return await list_user_forms_in_range(session, user_id=user_id, created_from=created_from, created_to=created_to)


async def _render_my_forms(cq_or_msg: Message | CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    u = cq_or_msg.from_user
    if not u:
        return
    user = await get_user_by_tg_id(session, u.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        return
    forms = await _load_my_forms(session, user_id=user.id, state=state)
    await state.set_state(DropManagerMyFormsStates.forms_list)
    await state.update_data(my_forms=[{"id": int(f.id)} for f in forms])
    header = f"📋 <b>Мои анкеты</b>\n\nВсего: <b>{len(forms)}</b>"
    kb = kb_dm_my_forms_list(forms)
    if isinstance(cq_or_msg, CallbackQuery):
        await cq_or_msg.answer()
        if cq_or_msg.message:
            await _safe_edit_message(message=cq_or_msg.message, text=header, reply_markup=kb)
        return
    await cq_or_msg.answer(header, reply_markup=kb)


async def _upsert_prompt_message(
    *,
    message: Message,
    state: FSMContext,
    state_key: str,
    text: str,
    reply_markup,
) -> None:
    data = await state.get_data()
    msg_id = data.get(state_key)
    if msg_id:
        try:
            await message.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=int(msg_id),
                text=text,
                reply_markup=reply_markup,
            )
            return
        except Exception:
            pass
    try:
        m = await message.answer(text, reply_markup=reply_markup)
    except TelegramNetworkError:
        return
    await state.update_data(**{state_key: m.message_id})


async def _cleanup_my_form_view(*, bot: Any, chat_id: int, state: FSMContext) -> None:
    data = await state.get_data()
    msg_ids = list(data.get("my_form_msg_ids") or [])
    if not msg_ids:
        return
    for msg_id in msg_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=int(msg_id))
        except Exception:
            continue
    await state.update_data(my_form_msg_ids=[])


async def _cleanup_edit_preview(*, bot: Any, chat_id: int, state: FSMContext) -> None:
    data = await state.get_data()
    msg_ids = list(data.get("dm_edit_preview_msg_ids") or [])
    if not msg_ids:
        return
    for msg_id in msg_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=int(msg_id))
        except Exception:
            continue
    await state.update_data(dm_edit_preview_msg_ids=[])


async def _cleanup_edit_prompt(*, bot: Any, chat_id: int, state: FSMContext) -> None:
    data = await state.get_data()
    msg_id = data.get("dm_edit_prompt_msg_id")
    if not msg_id:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=int(msg_id))
    except Exception:
        pass
    await state.update_data(dm_edit_prompt_msg_id=None)


async def _set_edit_prompt_message(
    *,
    message: Message,
    state: FSMContext,
    text: str,
    reply_markup,
) -> None:
    await _cleanup_edit_prompt(bot=message.bot, chat_id=int(message.chat.id), state=state)
    try:
        sent = await message.answer(text, reply_markup=reply_markup)
    except TelegramNetworkError:
        return
    await state.update_data(dm_edit_prompt_msg_id=sent.message_id)


async def _send_album_ack(
    *,
    bot: Any,
    chat_id: int,
    accepted: int,
    total: int,
    expected: int | None,
    reply_markup,
) -> None:
    if accepted <= 0:
        return
    if expected and expected > 0:
        text = f"✅ Принято {accepted} фото (итого {total}/{expected})."
    else:
        text = f"✅ Принято {accepted} фото (итого {total})."
    try:
        await bot.send_message(chat_id, text, reply_markup=reply_markup)
    except TelegramNetworkError:
        log.info("Album ack failed (network). chat_id=%s accepted=%s total=%s expected=%s", chat_id, accepted, total, expected)
        return


def _schedule_album_ack(
    *,
    bot: Any,
    chat_id: int,
    media_group_id: str,
    accepted_total: int,
    expected: int | None,
    reply_markup,
) -> None:
    key = (chat_id, media_group_id)
    # avoid duplicate acks if Telegram delivers late updates
    last_ts = _album_last_sent_ts.get(key)
    if last_ts and (time.time() - last_ts) < 2.0:
        return
    prev = _album_ack_tasks.get(key)
    if prev and not prev.done():
        prev.cancel()

    async def _runner() -> None:
        try:
            await asyncio.sleep(1.6)
        except asyncio.CancelledError:
            return
        accepted = _album_counts.pop(key, 0)
        _album_last_sent_ts[key] = time.time()
        await _send_album_ack(
            bot=bot,
            chat_id=chat_id,
            accepted=accepted,
            total=accepted_total,
            expected=expected,
            reply_markup=reply_markup,
        )

    _album_ack_tasks[key] = asyncio.create_task(_runner())


async def _render_dm_menu(message_or_cq: Message | CallbackQuery, session: AsyncSession) -> None:
    if isinstance(message_or_cq, CallbackQuery):
        u = message_or_cq.from_user
        chat_id = message_or_cq.message.chat.id if message_or_cq.message else None
    else:
        u = message_or_cq.from_user
        chat_id = message_or_cq.chat.id

    if not u or chat_id is None:
        return

    user = await get_user_by_tg_id(session, u.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        return

    if not user.manager_tag:
        if isinstance(message_or_cq, CallbackQuery):
            await message_or_cq.answer("Сначала введите тег менеджера", show_alert=True)
        else:
            await message_or_cq.answer("Сначала введите тег менеджера")
        return

    shift = await get_active_shift(session, user.id)
    src = getattr(user, "manager_source", None) or "—"
    text = (
        f"👤 <b>Дроп‑менеджер</b>: <b>{user.manager_tag}</b>\n"
        f"Источник: <b>{src}</b>\n"
        f"Смена: <b>{'активна' if shift else 'не активна'}</b>"
    )
    kb = await _build_dm_main_kb(session=session, user_id=int(user.id), shift_active=bool(shift))

    try:
        m = await (message_or_cq.message if isinstance(message_or_cq, CallbackQuery) else message_or_cq).answer(
            "...",
            reply_markup=ReplyKeyboardRemove(),
        )
        await m.delete()
    except Exception:
        pass

    if isinstance(message_or_cq, CallbackQuery):
        await message_or_cq.answer()
        if message_or_cq.message:
            try:
                await message_or_cq.message.answer(text, reply_markup=kb)
            except Exception:
                pass
            try:
                await message_or_cq.message.delete()
            except Exception:
                pass
        return

    await message_or_cq.answer(text, reply_markup=kb)


async def _build_dm_main_kb(*, session: AsyncSession, user_id: int, shift_active: bool) -> InlineKeyboardMarkup:
    if not shift_active:
        return kb_dm_main_inline(shift_active=False)
    rejected_count = await count_rejected_forms_by_user_id(session, user_id)
    return kb_dm_main_inline(shift_active=True, rejected_count=rejected_count)


def _kb_dm_approved_no_pay_list_inline(forms: list[Form]) -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    for f in forms[:30]:
        bank = format_bank_hashtag(getattr(f, "bank_name", None))
        b.button(text=f"✅ #{int(f.id)} {bank}", callback_data=f"dm:approved_no_pay_open:{int(f.id)}")
    b.button(text="⬅️ Назад", callback_data="dm:menu")
    b.adjust(1)
    return b


async def _render_dm_approved_no_pay(cq_or_msg: Message | CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    u = cq_or_msg.from_user
    if not u:
        return
    user = await get_user_by_tg_id(session, u.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        return
    notice_ids = pop_dm_approved_notices(int(user.tg_id))
    if notice_ids:
        for msg_id in notice_ids:
            try:
                await cq_or_msg.bot.delete_message(chat_id=int(user.tg_id), message_id=int(msg_id))
            except Exception:
                pass
    forms = await list_dm_approved_without_payment(session, manager_user_id=int(user.id), limit=30)
    header = f"✅ <b>Подтверждённые без номеров</b>\n\nВсего: <b>{len(forms)}</b>"
    kb = _kb_dm_approved_no_pay_list_inline(forms).as_markup()
    await state.update_data(dm_approved_no_pay_ids=[int(f.id) for f in forms])

    if isinstance(cq_or_msg, CallbackQuery):
        await cq_or_msg.answer()
        if cq_or_msg.message:
            await _safe_edit_message(message=cq_or_msg.message, text=header, reply_markup=kb)
        return
    await cq_or_msg.answer(header, reply_markup=kb)


async def _send_form_preview_only(*, bot: Any, chat_id: int, text: str, photos: list[str]) -> None:
    photos = list(photos or [])
    if not photos:
        await bot.send_message(chat_id, text, parse_mode="HTML")
        return
    photos = photos[:10]
    if len(photos) == 1:
        kind, fid = unpack_media_item(str(photos[0]))
        if kind == "doc":
            await bot.send_document(chat_id, fid, caption=text, parse_mode="HTML")
        elif kind == "video":
            await bot.send_video(chat_id, fid, caption=text, parse_mode="HTML")
        else:
            await bot.send_photo(chat_id, fid, caption=text, parse_mode="HTML")
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
                media.append(InputMediaVideo(media=first_fid, caption=text, parse_mode="HTML"))
            else:
                media.append(InputMediaPhoto(media=first_fid, caption=text, parse_mode="HTML"))
            for raw in media_items[1:]:
                kind, fid = unpack_media_item(str(raw))
                if kind == "video":
                    media.append(InputMediaVideo(media=fid))
                else:
                    media.append(InputMediaPhoto(media=fid))
            sent = list(await bot.send_media_group(chat_id, media) or [])
            reply_to_message_id: int | None = int(sent[0].message_id) if sent else None

            for raw in docs:
                _, fid = unpack_media_item(str(raw))
                await bot.send_document(chat_id, fid, reply_to_message_id=reply_to_message_id)
            return

        # Only docs
        first_kind, first_fid = unpack_media_item(str(docs[0]))
        first_msg = await bot.send_document(chat_id, first_fid, caption=text, parse_mode="HTML")
        reply_to_message_id = int(first_msg.message_id)
        for raw in docs[1:]:
            _, fid = unpack_media_item(str(raw))
            await bot.send_document(chat_id, fid, reply_to_message_id=reply_to_message_id)
    except TelegramNetworkError:
        return


async def _finish_payment(
    *,
    message: Message,
    session: AsyncSession,
    state: FSMContext,
    form: Form,
    payment_text: str | list[str],
    actor_tg_id: int | None = None,
) -> None:
    actor_id = int(actor_tg_id or getattr(getattr(message, "from_user", None), "id", 0) or 0)
    user = await get_user_by_tg_id(session, actor_id) if actor_id else None
    manager_tag = (getattr(user, "manager_tag", None) or "—") if user else "—"
    manager_source = (getattr(user, "manager_source", None) or None) if user else None
    try:
        form_text = _format_form_text(form, manager_tag, manager_source=manager_source)
    except Exception:
        form_text = f"📄 <b>Анкета</b>\nID: <code>{form.id}</code>"

    try:
        await _send_form_preview_only(
            bot=message.bot,
            chat_id=int(message.chat.id),
            text=form_text,
            photos=list(form.screenshots or []),
        )
    except Exception:
        pass

    try:
        if isinstance(payment_text, list):
            for item in payment_text:
                await message.answer(item, parse_mode="HTML")
        else:
            await message.answer(payment_text, parse_mode="HTML")
    except Exception:
        pass

    try:
        await message.answer(
            "Вам успешно подтвердили анкету, в случае ошибки веденых данных карты и данных анкет, "
            "вы несете отвественость."
        )
    except Exception:
        pass

    await mark_form_payment_done(session, form_id=int(form.id))
    await state.clear()

    src = (getattr(user, "manager_source", None) or "").upper() if user else ""
    # TG: after finishing payment always return to main menu as requested
    if src == "TG":
        try:
            if actor_id:
                dm_user = await get_user_by_tg_id(session, actor_id)
                if dm_user and dm_user.role == UserRole.DROP_MANAGER:
                    shift = await get_active_shift(session, dm_user.id)
                    src_line = getattr(dm_user, "manager_source", None) or "—"
                    text = (
                        f"👤 <b>Дроп‑менеджер</b>: <b>{dm_user.manager_tag or '—'}</b>\n"
                        f"Источник: <b>{src_line}</b>\n"
                        f"Смена: <b>{'активна' if shift else 'не активна'}</b>"
                    )
                    kb = await _build_dm_main_kb(session=session, user_id=int(dm_user.id), shift_active=bool(shift))
                    await message.bot.send_message(int(actor_id), text, reply_markup=kb)
                    return
        except Exception:
            pass
        await _render_dm_menu(message, session)
        return

    forms_left = await list_dm_approved_without_payment(session, manager_user_id=int(user.id) if user else 0, limit=30)
    forms_left = [f for f in forms_left if int(getattr(f, "id", 0)) != int(form.id)]
    if forms_left:
        await _render_dm_approved_no_pay(message, session, state)
        return
    await _render_dm_menu(message, session)


async def _edit_text_or_caption(message: Message, text: str, reply_markup=None) -> None:
    try:
        if message.photo or message.document or message.video:
            await message.edit_caption(caption=text, parse_mode="HTML", reply_markup=reply_markup)
        else:
            await message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        # best effort
        pass


async def _safe_edit_message(*, message: Message, text: str, reply_markup=None) -> None:
    """Edit message text/caption when possible; otherwise send a new message.

    Needed because Telegram doesn't allow edit_text for media-only messages.
    """
    try:
        if getattr(message, "text", None):
            await message.edit_text(text, reply_markup=reply_markup)
            return
    except Exception:
        pass

    try:
        # Works for photos/videos/documents (and also for text messages with captions)
        await message.edit_caption(caption=text, parse_mode="HTML", reply_markup=reply_markup)
        return
    except Exception:
        pass

    await message.answer(text, reply_markup=reply_markup)


def _kb_dm_rejected_list_inline(forms: list[Form]) -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    for f in forms[:30]:
        b.button(text=f"❌ #{f.id}", callback_data=f"dm:rej:{int(f.id)}")
    b.button(text="⬅️ Назад", callback_data="dm:menu")
    b.adjust(3, 3, 3, 3, 3, 3, 1)
    return b


def _kb_dm_rejected_detail_inline(form_id: int) -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    b.button(text="Редактировать", callback_data=f"drop_edit_form:{form_id}")
    b.button(text="⬅️ Назад", callback_data="dm:rejected")
    b.adjust(1)
    return b


def _format_form_text(form: Form, manager_tag: str, manager_source: str | None = None) -> str:
    traffic = "—"
    if form.traffic_type == "DIRECT":
        traffic = "Прямой"
    elif form.traffic_type == "REFERRAL":
        traffic = "Сарафан"

    src_raw = (manager_source or getattr(getattr(form, "manager", None), "manager_source", None) or "—")
    src = str(src_raw).upper()

    status_line = "❌ <b>Не подтверждена</b>"
    if form.status == FormStatus.APPROVED:
        status_line = "✅ <b>Подтверждено тим лидом</b>"
    if form.status == FormStatus.REJECTED:
        status_line = "❌ <b>Отклонено</b>"

    direct_user = html.escape(format_user_payload(form.direct_user))
    referral_user = html.escape(format_user_payload(form.referral_user))
    ref_line = f"Привел: {referral_user}\n" if form.traffic_type == "REFERRAL" else ""

    tl_comment = html.escape((form.team_lead_comment or "").strip() or "Комментария нет")
    tl_comment_line = f"Комментарий TL: <b>{tl_comment}</b>\n" if form.status == FormStatus.REJECTED else ""
    bank_tag = html.escape(format_bank_hashtag(getattr(form, "bank_name", None)))
    safe_manager_tag = html.escape(manager_tag or "—")
    safe_phone = html.escape(form.phone or "—")
    safe_password = html.escape(form.password or "—")
    safe_comment = html.escape(form.comment or "—")
    source_line = f"Источник: <b>{html.escape(src)}</b>\n"
    traffic_line = "" if src == "TG" else f"Тип клиента: <b>{traffic}</b>\n"

    client_lines = "" if src == "TG" else f"Клиент: {direct_user}\n{ref_line}"

    return (
        f"{status_line}\n\n"
        "📄 <b>Анкета</b>\n"
        f"ID: <code>{form.id}</code>\n"
        f"{source_line}"
        f"Тег менеджера: <b>{safe_manager_tag}</b>\n"
        f"{traffic_line}"
        f"{client_lines}"
        f"{tl_comment_line}"
        f"Номер: <code>{safe_phone}</code>\n"
        f"Банк: <b>{bank_tag}</b>\n"
        f"Пароль: <code>{safe_password}</code>\n"
        f"Комментарий: {safe_comment}"
    )


async def _send_form_photos(message_or_bot: Any, chat_id: int, photos: list[str]) -> None:
    if not photos:
        return
    if len(photos) == 1:
        kind, fid = unpack_media_item(str(photos[0]))
        try:
            if kind == "doc":
                await message_or_bot.send_document(chat_id, fid)
            elif kind == "video":
                await message_or_bot.send_video(chat_id, fid)
            else:
                await message_or_bot.send_photo(chat_id, fid)
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
            await message_or_bot.send_media_group(chat_id, media)

        if docs:
            docs_media: list[InputMediaDocument] = []
            for raw in docs:
                _, fid = unpack_media_item(str(raw))
                docs_media.append(InputMediaDocument(media=fid))
            await message_or_bot.send_media_group(chat_id, docs_media)
    except TelegramNetworkError:
        return


async def _send_form_preview_with_keyboard(
    *,
    bot: Any,
    chat_id: int,
    text: str,
    photos: list[str],
    reply_markup,
    state: FSMContext | None = None,
    buttons_text: str = "Выберите действие:",
) -> None:
    """
    Send form preview with buttons in a separate message.
    - 0 photos: text, then buttons as separate message
    - 1 photo: photo with caption, then buttons as separate message
    - 2+ photos: album with caption, then buttons as separate message
    """
    photos = photos[:10]
    if not photos:
        try:
            if state:
                await _cleanup_edit_preview(bot=bot, chat_id=chat_id, state=state)
            main_msg = await bot.send_message(chat_id, text, parse_mode="HTML")
            buttons_msg = await bot.send_message(chat_id, buttons_text, reply_markup=reply_markup, parse_mode="HTML")
            if state:
                await state.update_data(dm_edit_preview_msg_ids=[main_msg.message_id, buttons_msg.message_id])
        except TelegramNetworkError:
            return
        return
    if len(photos) == 1:
        try:
            if state:
                await _cleanup_edit_preview(bot=bot, chat_id=chat_id, state=state)
            kind, fid = unpack_media_item(str(photos[0]))
            if kind == "doc":
                main_msg = await bot.send_document(chat_id, fid, caption=text, parse_mode="HTML")
            elif kind == "video":
                main_msg = await bot.send_video(chat_id, fid, caption=text, parse_mode="HTML")
            else:
                main_msg = await bot.send_photo(chat_id, fid, caption=text, parse_mode="HTML")
            buttons_msg = await bot.send_message(chat_id, buttons_text, reply_markup=reply_markup, parse_mode="HTML")
            if state:
                await state.update_data(dm_edit_preview_msg_ids=[main_msg.message_id, buttons_msg.message_id])
        except TelegramNetworkError:
            return
        return

    # Multiple photos: send album with caption, then buttons as a separate message
    try:
        if state:
            await _cleanup_edit_preview(bot=bot, chat_id=chat_id, state=state)
        docs: list[str] = []
        media_items: list[str] = []
        for raw in photos:
            kind, _ = unpack_media_item(str(raw))
            if kind == "doc":
                docs.append(str(raw))
            else:
                media_items.append(str(raw))

        album_msgs: list[Any] = []
        reply_to_message_id: int | None = None
        # Prefer sending photo/video first with caption; docs are sent without mixing.
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
            sent = list(await bot.send_media_group(chat_id, media) or [])
            album_msgs.extend(sent)
            if sent:
                reply_to_message_id = int(sent[0].message_id)

        if docs:
            if not album_msgs:
                # If there are only docs, keep caption on first document.
                first_kind, first_fid = unpack_media_item(str(docs[0]))
                first_msg = await bot.send_document(chat_id, first_fid, caption=text, parse_mode="HTML")
                album_msgs.append(first_msg)
                reply_to_message_id = int(first_msg.message_id)
                for raw in docs[1:]:
                    _, fid = unpack_media_item(str(raw))
                    m = await bot.send_document(chat_id, fid, reply_to_message_id=reply_to_message_id)
                    album_msgs.append(m)
            else:
                # Make docs look "attached" by replying to first album message.
                for raw in docs:
                    _, fid = unpack_media_item(str(raw))
                    m = await bot.send_document(chat_id, fid, reply_to_message_id=reply_to_message_id)
                    album_msgs.append(m)

        buttons_msg = await bot.send_message(chat_id, buttons_text, reply_markup=reply_markup, parse_mode="HTML")
        if state:
            album_ids = [int(m.message_id) for m in (album_msgs or [])]
            await state.update_data(dm_edit_preview_msg_ids=[*album_ids, buttons_msg.message_id])
    except TelegramNetworkError:
        return


async def _after_dm_edit_show_form(*, message: Message, session: AsyncSession, state: FSMContext, form: Form) -> None:
    user = await get_user_by_tg_id(session, int(message.from_user.id)) if message.from_user else None
    manager_tag = (user.manager_tag if user and user.manager_tag else "—")
    manager_source = (getattr(user, "manager_source", None) if user else None)
    text = _format_form_text(form, manager_tag, manager_source)

    chat_id = int(message.chat.id)
    await _cleanup_edit_prompt(bot=message.bot, chat_id=chat_id, state=state)
    await _cleanup_edit_preview(bot=message.bot, chat_id=chat_id, state=state)

    data = await state.get_data()
    edit_return_mode = data.get("edit_return_mode")
    await state.set_state(DropManagerEditStates.choose_field)
    await state.update_data(form_id=form.id, edit_return_mode=edit_return_mode)

    await _send_form_preview_with_keyboard(
        bot=message.bot,
        chat_id=chat_id,
        text=text,
        photos=list(form.screenshots or []),
        reply_markup=kb_dm_edit_actions_inline(form.id),
        state=state,
    )


async def _send_photos_simple(message_or_bot: Any, chat_id: int, photos: list[str]) -> None:
    if not photos:
        return
    if len(photos) == 1:
        kind, fid = unpack_media_item(str(photos[0]))
        try:
            if kind == "doc":
                await message_or_bot.send_document(chat_id, fid)
            elif kind == "video":
                await message_or_bot.send_video(chat_id, fid)
            else:
                await message_or_bot.send_photo(chat_id, fid)
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
            await message_or_bot.send_media_group(chat_id, media)

        for raw in docs:
            _, fid = unpack_media_item(str(raw))
            await message_or_bot.send_document(chat_id, fid)
    except TelegramNetworkError:
        return


async def _send_photos_with_caption(message_or_bot: Any, chat_id: int, photos: list[str], caption: str) -> None:
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
                await message_or_bot.send_document(chat_id, fid, caption=caption, parse_mode="HTML")
            elif kind == "video":
                await message_or_bot.send_video(chat_id, fid, caption=caption, parse_mode="HTML")
            else:
                await message_or_bot.send_photo(chat_id, fid, caption=caption, parse_mode="HTML")
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

        # Prefer sending photo/video first with caption; docs are sent without mixing.
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
            await message_or_bot.send_media_group(chat_id, media)

            if docs:
                docs_media: list[InputMediaDocument] = []
                for raw in docs:
                    _, fid = unpack_media_item(str(raw))
                    docs_media.append(InputMediaDocument(media=fid))
                await message_or_bot.send_media_group(chat_id, docs_media)
            return

        # Only docs: keep caption on the first document.
        docs_media: list[InputMediaDocument] = []
        first_kind, first_fid = unpack_media_item(str(docs[0]))
        docs_media.append(InputMediaDocument(media=first_fid, caption=caption, parse_mode="HTML"))
        for raw in docs[1:]:
            _, fid = unpack_media_item(str(raw))
            docs_media.append(InputMediaDocument(media=fid))
        await message_or_bot.send_media_group(chat_id, docs_media)
    except TelegramNetworkError:
        return


async def _list_banks_for_dm_source(session: AsyncSession, manager_source: str | None) -> list:
    src = (manager_source or "TG").upper()
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
        # Fallback for inconsistent data: show all non-TG-only banks instead of empty list
        return [b for b in banks if _has_fb(b) or not _has_tg(b)]

    picked = [b for b in banks if _has_tg(b) or (_has_legacy(b) and not _has_fb(b))]
    if picked:
        return picked
    # Fallback for inconsistent data: show all non-FB-only banks instead of empty list
    return [b for b in banks if _has_tg(b) or not _has_fb(b)]


def _dm_bank_items_with_source(banks: list, manager_source: str | None) -> list[tuple[int, str]]:
    src = (manager_source or "TG").upper()
    suffix = "FB" if src == "FB" else "TG"
    items: list[tuple[int, str]] = []
    for b in banks:
        name = str(getattr(b, "name", "") or "").strip()
        if not name:
            continue
        items.append((int(b.id), f"{name} ({suffix})"))
    return items


async def _get_bank_instructions_text(session: AsyncSession, *, user: User | None, bank_name: str) -> str | None:
    bank = await get_bank_by_name(session, bank_name)
    if not bank:
        return None
    src = (getattr(user, "manager_source", None) or "").upper() if user else ""
    instructions_src = None
    if src == "FB":
        instructions_src = getattr(bank, "instructions_fb", None)
    elif src == "TG":
        instructions_src = getattr(bank, "instructions_tg", None)
    instructions = (instructions_src or getattr(bank, "instructions", None) or "").strip()
    if not instructions:
        return None
    return f"📌 <b>Условия ({format_bank_hashtag(bank_name)})</b>:\n<blockquote expandable>{instructions or '—'}</blockquote>"


async def _notify_team_leads_new_form(
    bot: Any,
    settings: Settings,
    session: AsyncSession,
    form: Form,
    manager_tag: str,
) -> None:
    # strict route by manager source (TG/FB) from users table
    dm = await get_user_by_id(session, form.manager_id)
    src = (getattr(dm, "manager_source", None) or "TG").upper() or "TG"

    tl_ids = await list_team_lead_ids_by_source(session, src)
    if not tl_ids:
        return
    dm_username = f"@{dm.username}" if dm and dm.username else "—"
    text = f"От кого заявка: <b>{dm_username}</b>\n\n" + _format_form_text(form, manager_tag)
    for tl_id in tl_ids:
        try:
            bank = format_bank_hashtag(getattr(form, "bank_name", None))
            b = InlineKeyboardBuilder()
            b.button(text="Перейти", callback_data=f"tl:live_open:{int(form.id)}")
            b.adjust(1)
            notice = await bot.send_message(
                tl_id,
                (
                    f"🆕 <b>Анкета {form.id}</b>\n"
                    f"Банк: <b>{bank}</b>\n"
                    f"ДМ: <b>{dm_username}</b>"
                ),
                parse_mode="HTML",
                reply_markup=b.as_markup(),
            )
            register_tl_form_notice(int(tl_id), int(form.id), int(notice.message_id))
        except Exception:
            log.exception("Failed to notify team lead %s", tl_id)


async def _notify_team_leads_duplicate_bank_phone(
    *,
    bot: Any,
    session: AsyncSession,
    dm_user: User,
    phone: str,
    bank_name: str,
) -> None:
    try:
        await create_duplicate_report(
            session,
            manager_id=int(dm_user.id),
            manager_username=dm_user.username,
            manager_source=getattr(dm_user, "manager_source", None),
            phone=phone,
            bank_name=bank_name,
        )
    except Exception:
        pass
    src = (getattr(dm_user, "manager_source", None) or "TG").upper() or "TG"
    tl_ids = await list_team_lead_ids_by_source(session, src)
    if not tl_ids:
        return
    dm_username = f"@{dm_user.username}" if getattr(dm_user, "username", None) else "—"
    bank_tag = format_bank_hashtag(bank_name)
    text = (
        "⚠️ Дубликат: Банк+Номер\n"
        f"ДМ: <b>{dm_username}</b>\n"
        f"Номер: <code>{phone}</code>\n"
        f"Банк: <b>{bank_tag}</b>"
    )
    for tl_id in tl_ids:
        try:
            await bot.send_message(tl_id, text, parse_mode="HTML")
        except Exception:
            pass


@router.message(F.text == "Начать работу")
async def start_work(message: Message, session: AsyncSession) -> None:
    # Ignore group messages
    if message.chat.type in ['group', 'supergroup']:
        return
    if not message.from_user:
        return
    user = await get_user_by_tg_id(session, message.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        return
    if not getattr(user, "forward_group_id", None):
        await message.answer(
            "Сначала разработчик должен привязать вас к группе пересылки.\n"
            "Напишите разработчику и попросите привязать группу.",
            reply_markup=kb_dm_main_inline(shift_active=False),
        )
        return
    if not user.manager_tag:
        await message.answer("Сначала введите тег менеджера:")
        return
    active = await get_active_shift(session, user.id)
    if active:
        kb = await _build_dm_main_kb(session=session, user_id=int(user.id), shift_active=True)
        await message.answer("У вас уже есть активная смена.", reply_markup=kb)
        return
    await start_shift(session, user.id)
    kb = await _build_dm_main_kb(session=session, user_id=int(user.id), shift_active=True)
    await message.answer("✅ Смена начата.", reply_markup=kb)


@router.callback_query(F.data == "dm:menu")
async def dm_menu_cb(cq: CallbackQuery, session: AsyncSession) -> None:
    await _render_dm_menu(cq, session)


@router.callback_query(F.data == "dm:my_forms")
async def dm_my_forms_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    if cq.message:
        await _cleanup_my_form_view(bot=cq.bot, chat_id=int(cq.message.chat.id), state=state)
    await _render_my_forms(cq, session, state)


@router.callback_query(F.data.startswith("dm:pay_card:"))
async def dm_pay_card_start_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    if not cq.from_user:
        return
    user = await get_user_by_tg_id(session, cq.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        await cq.answer("Нет прав", show_alert=True)
        return
    try:
        form_id = int((cq.data or "").split(":")[-1])
    except Exception:
        await cq.answer("Некорректная кнопка", show_alert=True)
        return
    form = await get_form(session, form_id)
    if not form or form.manager_id != user.id:
        await cq.answer("Анкета не найдена", show_alert=True)
        return

    # optional: only after TL approval
    if form.status != FormStatus.APPROVED:
        await cq.answer("Доступно только для подтвержденных анкет", show_alert=True)
        return

    if getattr(form, "payment_done_at", None) is not None:
        await cq.answer("Оплата уже заполнена", show_alert=True)
        return

    await cq.answer()
    await state.clear()
    await state.set_state(DropManagerPaymentStates.card_main)
    await state.update_data(pay_form_id=int(form.id), pay_traffic=str(form.traffic_type), pay_items=[])
    if cq.message:
        await cq.message.answer("Напишите или перешлите номер карты Прямого клиента за оплату банка")


@router.callback_query(F.data == "dm:approved_no_pay")
async def dm_approved_no_pay_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await _render_dm_approved_no_pay(cq, session, state)


@router.callback_query(F.data.startswith("dm:approved_no_pay_open:"))
async def dm_approved_no_pay_open_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    if not cq.from_user:
        return
    user = await get_user_by_tg_id(session, cq.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        await cq.answer("Нет прав", show_alert=True)
        return
    try:
        form_id = int((cq.data or "").split(":")[-1])
    except Exception:
        await cq.answer("Некорректная кнопка", show_alert=True)
        return
    form = await get_form(session, form_id)
    if not form or form.manager_id != user.id or form.status != FormStatus.APPROVED:
        await cq.answer("Анкета не найдена", show_alert=True)
        return
    if getattr(form, "payment_done_at", None) is not None:
        await cq.answer("Оплата уже заполнена", show_alert=True)
        return

    await cq.answer()

    manager_tag = user.manager_tag or "—"
    try:
        text = _format_form_text(form, manager_tag)
    except Exception:
        text = f"📄 <b>Анкета</b>\nID: <code>{form.id}</code>"

    try:
        await _send_form_preview_only(
            bot=cq.bot,
            chat_id=int(cq.message.chat.id if cq.message else cq.from_user.id),
            text=text,
            photos=list(form.screenshots or []),
        )
    except Exception:
        pass

    can_attach_resource = False
    try:
        source = (getattr(user, "manager_source", None) or "TG")
        banks_for_source = await _list_banks_for_dm_source(session, source)
        bank_name_norm = str(form.bank_name or "").strip().lower()
        bank = next((b for b in banks_for_source if str(getattr(b, "name", "") or "").strip().lower() == bank_name_norm), None)
        if bank:
            free_items = await list_free_pool_items_for_bank(session, bank_id=int(bank.id), source=source)
            can_attach_resource = len(free_items) > 0
    except Exception:
        can_attach_resource = False

    if cq.message:
        await cq.message.answer("Выберите действие:", reply_markup=kb_dm_payment_card_with_back(int(form.id), can_attach_resource=can_attach_resource))


@router.callback_query(F.data.startswith("dm:approved_attach:"))
async def dm_approved_attach_start_cb(cq: CallbackQuery, session: AsyncSession) -> None:
    if not cq.from_user:
        return
    user = await get_user_by_tg_id(session, cq.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        await cq.answer("Нет прав", show_alert=True)
        return
    try:
        form_id = int((cq.data or "").split(":")[-1])
    except Exception:
        await cq.answer("Некорректная кнопка", show_alert=True)
        return

    form = await get_form(session, form_id)
    if not form or int(form.manager_id or 0) != int(user.id) or form.status != FormStatus.APPROVED:
        await cq.answer("Анкета не найдена", show_alert=True)
        return
    if await form_has_linked_pool_item(session, form_id=int(form.id)):
        await cq.answer("К анкете уже привязан ресурс", show_alert=True)
        return

    source = (getattr(user, "manager_source", None) or "TG")
    banks_for_source = await _list_banks_for_dm_source(session, source)
    bank_name_norm = str(form.bank_name or "").strip().lower()
    bank = next((b for b in banks_for_source if str(getattr(b, "name", "") or "").strip().lower() == bank_name_norm), None)
    if not bank:
        await cq.answer("Для этого банка нет доступных ресурсов", show_alert=True)
        return

    free_items = await list_free_pool_items_for_bank(session, bank_id=int(bank.id), source=source)
    counts = _free_pool_counts_by_type(free_items)
    available_types = [t for t in ("link", "esim", "link_esim") if int(counts.get(t, 0)) > 0]
    if not available_types:
        await cq.answer("На этот банк нет ресурсов от WICTORY", show_alert=True)
        return

    await cq.answer()
    if cq.message:
        await _safe_edit_message(
            message=cq.message,
            text=f"Анкета <code>#{int(form.id)}</code> · <b>{form.bank_name or '—'}</b>\nВыберите тип ресурса для быстрой привязки:",
            reply_markup=kb_dm_approved_attach_type_pick(int(form.id), available_types),
        )


@router.callback_query(F.data.startswith("dm:approved_attach_type:"))
async def dm_approved_attach_type_cb(cq: CallbackQuery, session: AsyncSession) -> None:
    if not cq.from_user:
        return
    user = await get_user_by_tg_id(session, cq.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        await cq.answer("Нет прав", show_alert=True)
        return

    try:
        _, _, form_id_s, rtype = (cq.data or "").split(":", 3)
        form_id = int(form_id_s)
    except Exception:
        await cq.answer("Некорректная кнопка", show_alert=True)
        return

    form = await get_form(session, form_id)
    if not form or int(form.manager_id or 0) != int(user.id) or form.status != FormStatus.APPROVED:
        await cq.answer("Анкета не найдена", show_alert=True)
        return
    if await form_has_linked_pool_item(session, form_id=int(form.id)):
        await cq.answer("К анкете уже привязан ресурс", show_alert=True)
        return

    source = (getattr(user, "manager_source", None) or "TG")
    banks_for_source = await _list_banks_for_dm_source(session, source)
    bank_name_norm = str(form.bank_name or "").strip().lower()
    bank = next((b for b in banks_for_source if str(getattr(b, "name", "") or "").strip().lower() == bank_name_norm), None)
    if not bank:
        await cq.answer("Для этого банка нет доступных ресурсов", show_alert=True)
        return

    free_items = await list_free_pool_items_for_bank(session, bank_id=int(bank.id), source=source)
    picked = next((x for x in free_items if str(getattr(getattr(x, "type", None), "value", "") or "").lower() == str(rtype).lower()), None)
    if not picked:
        await cq.answer("Ресурс этого типа закончился", show_alert=True)
        return

    assigned = await assign_pool_item_to_dm(session, item_id=int(picked.id), dm_user_id=int(user.id))
    if not assigned:
        await cq.answer("Не удалось взять ресурс", show_alert=True)
        return

    used = await mark_pool_item_used_with_form(session, item_id=int(assigned.id), dm_user_id=int(user.id), form_id=int(form.id))
    if not used:
        await cq.answer("Не удалось привязать ресурс", show_alert=True)
        return

    await cq.answer("Ресурс привязан")
    if cq.message:
        await _safe_edit_message(
            message=cq.message,
            text=f"✅ Ресурс <code>#{int(used.id)}</code> ({_pool_type_ru(str(getattr(getattr(used, 'type', None), 'value', '') or ''))}) привязан к анкете <code>#{int(form.id)}</code>",
            reply_markup=kb_dm_payment_card_with_back(int(form.id)),
        )


@router.message(DropManagerPaymentStates.card_main, F.text)
async def dm_pay_card_main_msg(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not message.from_user:
        return
    card = _normalize_card(message.text)
    if len(card) < 12 or len(card) > 19:
        await message.answer("Некорректный номер карты. Введите ещё раз:")
        return
    data = await state.get_data()
    form = await get_form(session, int(data.get("pay_form_id") or 0))
    if not form:
        await state.clear()
        return
    await state.update_data(pay_card_main=card)
    await state.set_state(DropManagerPaymentStates.amount_main)
    await message.answer("Напишите или перешлите сумму оплаты")


@router.message(DropManagerPaymentStates.amount_main, F.text)
async def dm_pay_amount_main_msg(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not message.from_user:
        return
    amount_raw = (message.text or "").strip().replace(" ", "")
    if not amount_raw.isdigit():
        await message.answer("Некорректная сумма. Введите число:")
        return
    data = await state.get_data()
    form = await get_form(session, int(data.get("pay_form_id") or 0))
    if not form:
        await state.clear()
        return

    card = str(data.get("pay_card_main") or "")
    traffic = (data.get("pay_traffic") or "").split(".")[-1]

    user = await get_user_by_tg_id(session, message.from_user.id)
    src = (getattr(user, "manager_source", None) or "").upper() if user else ""
    if src == "TG":
        pay_items = list(data.get("pay_items") or [])
        pay_items.append({"card": card, "amount": amount_raw})
        await state.update_data(pay_items=pay_items, pay_card_main=None)
        await state.set_state(DropManagerPaymentStates.next_action)
        await message.answer("Карта добавлена. Что дальше?", reply_markup=kb_dm_payment_next_actions())
        return

    if traffic == "REFERRAL":
        await state.update_data(pay_amount_main=amount_raw)
        await state.set_state(DropManagerPaymentStates.card_bonus)
        await message.answer("Напишите или перешлите номер карты того, кто привёл")
        return

    payment_text = (
        f"Оплата {_format_payment_phone(form.phone)}\n\n"
        f"{card}\n\n"
        f"{amount_raw}"
    )
    await _finish_payment(message=message, session=session, state=state, form=form, payment_text=payment_text)
    return


@router.callback_query(F.data == "dm:pay_add_card")
async def dm_pay_add_card_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    if not cq.from_user:
        return
    data = await state.get_data()
    form = await get_form(session, int(data.get("pay_form_id") or 0))
    if not form:
        await cq.answer("Анкета не найдена", show_alert=True)
        await state.clear()
        return
    await cq.answer()
    await state.set_state(DropManagerPaymentStates.card_main)
    if cq.message:
        await cq.message.answer("Напишите или перешлите номер карты")


@router.message(DropManagerPaymentStates.next_action, F.text)
async def dm_pay_next_action_text(message: Message, session: AsyncSession, state: FSMContext) -> None:
    txt = (message.text or "").strip().lower()

    if txt in {"добавить карту", "добавить"}:
        await state.set_state(DropManagerPaymentStates.card_main)
        await message.answer("Напишите или перешлите номер карты")
        return

    if txt in {"финал", "final", "готово"}:
        data = await state.get_data()
        form = await get_form(session, int(data.get("pay_form_id") or 0))
        if not form:
            await state.clear()
            return
        pay_items = list(data.get("pay_items") or [])
        if not pay_items:
            await message.answer("Добавьте хотя бы одну карту", reply_markup=kb_dm_payment_next_actions())
            return
        payment_text: list[str] = []
        for item in pay_items:
            card = str(item.get("card") or "").strip()
            amount = str(item.get("amount") or "").strip()
            if not card or not amount:
                continue
            payment_text.append(f"Оплата {_format_payment_phone(form.phone)}\n\n{card}\n\n{amount}")
        if not payment_text:
            await message.answer("Некорректные данные оплаты", reply_markup=kb_dm_payment_next_actions())
            return
        await _finish_payment(message=message, session=session, state=state, form=form, payment_text=payment_text)
        return

    # Convenience: if DM sends card directly instead of pressing button, continue flow
    card = _normalize_card(message.text)
    if 12 <= len(card) <= 19:
        await state.update_data(pay_card_main=card)
        await state.set_state(DropManagerPaymentStates.amount_main)
        await message.answer("Напишите или перешлите сумму оплаты")
        return

    await message.answer("Нажмите «Добавить карту» или «Финал»", reply_markup=kb_dm_payment_next_actions())


@router.callback_query(F.data == "dm:pay_finish")
async def dm_pay_finish_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    if not cq.from_user:
        return
    data = await state.get_data()
    form = await get_form(session, int(data.get("pay_form_id") or 0))
    if not form:
        await cq.answer("Анкета не найдена", show_alert=True)
        await state.clear()
        return
    pay_items = list(data.get("pay_items") or [])
    if not pay_items:
        await cq.answer("Добавьте хотя бы одну карту", show_alert=True)
        return

    payment_text: list[str] = []
    for item in pay_items:
        card = str(item.get("card") or "").strip()
        amount = str(item.get("amount") or "").strip()
        if not card or not amount:
            continue
        payment_text.append(
            f"Оплата {_format_payment_phone(form.phone)}\n\n{card}\n\n{amount}"
        )

    if not payment_text:
        await cq.answer("Некорректные данные оплаты", show_alert=True)
        return

    await cq.answer()
    if cq.message:
        await _finish_payment(
            message=cq.message,
            session=session,
            state=state,
            form=form,
            payment_text=payment_text,
            actor_tg_id=int(cq.from_user.id),
        )


@router.message(DropManagerPaymentStates.phone_bonus, F.text)
async def dm_pay_phone_bonus_msg(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not message.from_user:
        return
    if not is_valid_phone(message.text):
        await message.answer("Некорректный номер телефона. Пример: <code>944567892</code> или <code>+380991112233</code>")
        return
    data = await state.get_data()
    form = await get_form(session, int(data.get("pay_form_id") or 0))
    if not form:
        await state.clear()
        return
    phone = normalize_phone(message.text)
    await state.update_data(pay_phone_bonus=phone)
    await state.set_state(DropManagerPaymentStates.card_bonus)
    await message.answer("Напишите или перешлите номер карты того, кто привёл")


@router.message(DropManagerPaymentStates.card_bonus, F.text)
async def dm_pay_card_bonus_msg(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not message.from_user:
        return
    card = _normalize_card(message.text)
    if len(card) < 12 or len(card) > 19:
        await message.answer("Некорректный номер карты. Введите ещё раз:")
        return
    data = await state.get_data()
    form = await get_form(session, int(data.get("pay_form_id") or 0))
    if not form:
        await state.clear()
        return
    await state.update_data(pay_card_bonus=card)
    await state.set_state(DropManagerPaymentStates.amount_bonus)
    await message.answer("Напишите или перешлите сумму оплаты")


@router.message(DropManagerPaymentStates.amount_bonus, F.text)
async def dm_pay_amount_bonus_msg(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not message.from_user:
        return
    amount_raw = (message.text or "").strip().replace(" ", "")
    if not amount_raw.isdigit():
        await message.answer("Некорректная сумма. Введите число:")
        return
    data = await state.get_data()
    form = await get_form(session, int(data.get("pay_form_id") or 0))
    if not form:
        await state.clear()
        return

    card_bonus = str(data.get("pay_card_bonus") or "")
    card_main = str(data.get("pay_card_main") or "")
    amount_main = str(data.get("pay_amount_main") or "")
    payment_text = [
        (
            f"Оплата {_format_payment_phone(form.phone)}\n\n"
            f"{card_main}\n\n"
            f"{amount_main}"
        ),
        (
            f"Бонус {_format_payment_phone(form.phone)}\n\n"
            f"{card_bonus}\n\n"
            f"{amount_raw}"
        ),
    ]

    await _finish_payment(message=message, session=session, state=state, form=form, payment_text=payment_text)


@router.callback_query(F.data == "dm:my_forms_filter")
async def dm_my_forms_filter_menu_cb(cq: CallbackQuery, state: FSMContext) -> None:
    await cq.answer()
    data = await state.get_data()
    current = (data.get("my_forms_period") or "today")
    if cq.message:
        await cq.message.edit_text("📅 <b>Фильтр моих анкет</b>", reply_markup=kb_dm_forms_filter_menu(current=current))


@router.callback_query(F.data.startswith("dm:my_forms_filter_set:"))
async def dm_my_forms_filter_set_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await cq.answer()
    period = (cq.data or "").split(":")[-1]
    await state.update_data(my_forms_period=period, my_forms_created_from=None, my_forms_created_to=None)
    await _render_my_forms(cq, session, state)


@router.callback_query(F.data == "dm:my_forms_filter_custom")
async def dm_my_forms_filter_custom_cb(cq: CallbackQuery, state: FSMContext) -> None:
    await cq.answer()
    await state.set_state(DropManagerMyFormsStates.forms_filter_range)
    if cq.message:
        await cq.message.edit_text("Введите интервал дат в формате: <code>DD.MM.YYYY-DD.MM.YYYY</code>")


@router.message(DropManagerMyFormsStates.forms_filter_range, F.text)
async def dm_my_forms_filter_range_msg(message: Message, session: AsyncSession, state: FSMContext) -> None:
    # Ignore group messages
    if message.chat.type in ['group', 'supergroup']:
        return
    if not message.from_user:
        return
    user = await get_user_by_tg_id(session, message.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
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

    await state.update_data(my_forms_period="custom", my_forms_created_from=created_from, my_forms_created_to=created_to)
    await _render_my_forms(message, session, state)


@router.callback_query(F.data.startswith("dm:my_form_open:"))
async def dm_my_form_open_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    if not cq.from_user:
        return
    user = await get_user_by_tg_id(session, cq.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        await cq.answer("Нет прав", show_alert=True)
        return
    try:
        form_id = int((cq.data or "").split(":")[-1])
    except Exception:
        await cq.answer("Некорректная кнопка", show_alert=True)
        return
    form = await get_form(session, form_id)
    if not form or form.manager_id != user.id:
        await cq.answer("Анкета не найдена", show_alert=True)
        return
    await cq.answer()
    await state.set_state(DropManagerMyFormsStates.form_view)
    await state.update_data(my_form_id=form_id)

    manager_tag = user.manager_tag or "—"
    try:
        text = _format_form_text(form, manager_tag)
    except Exception:
        text = f"📋 <b>АНКЕТА #{form.id}</b>"

    if cq.message:
        await _cleanup_my_form_view(bot=cq.bot, chat_id=int(cq.message.chat.id), state=state)
        photos = list(form.screenshots or [])
        chat_id = int(cq.message.chat.id)
        in_progress = form.status == FormStatus.IN_PROGRESS
        if photos:
            # Send album with caption, then buttons as separate message
            try:
                media = [InputMediaPhoto(media=photos[0], caption=text, parse_mode="HTML")]
                for photo_id in photos[1:10]:
                    media.append(InputMediaPhoto(media=photo_id))
                album_msgs = await cq.bot.send_media_group(chat_id, media)
                buttons_msg = await cq.bot.send_message(
                    chat_id,
                    "Выберите действие:",
                    reply_markup=kb_dm_my_form_open(form_id, in_progress=in_progress),
                )
                album_ids = [int(m.message_id) for m in (album_msgs or [])]
                await state.update_data(my_form_msg_ids=[*album_ids, buttons_msg.message_id])
                try:
                    await cq.message.delete()
                except Exception:
                    pass
                return
            except TelegramNetworkError:
                pass
            except Exception:
                pass

        # No screenshots (or failed to send): send text, then buttons
        try:
            main_msg = await cq.bot.send_message(chat_id, text, parse_mode="HTML")
            buttons_msg = await cq.bot.send_message(
                chat_id,
                "Выберите действие:",
                reply_markup=kb_dm_my_form_open(form_id, in_progress=form.status == FormStatus.IN_PROGRESS),
            )
            await state.update_data(my_form_msg_ids=[main_msg.message_id, buttons_msg.message_id])
            try:
                await cq.message.delete()
            except Exception:
                pass
        except TelegramNetworkError:
            pass


@router.callback_query(F.data.startswith("dm:my_form_send:"))
async def dm_my_form_send_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not cq.from_user:
        return
    user = await get_user_by_tg_id(session, cq.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        await cq.answer("Нет прав", show_alert=True)
        return
    try:
        form_id = int((cq.data or "").split(":")[-1])
    except Exception:
        await cq.answer("Некорректная кнопка", show_alert=True)
        return
    form = await get_form(session, form_id)
    if not form or form.manager_id != user.id:
        await cq.answer("Анкета не найдена", show_alert=True)
        return

    form.status = FormStatus.PENDING
    form.team_lead_comment = None

    manager_tag = user.manager_tag or "—"
    text = _format_form_text(form, manager_tag)
    await cq.answer("Отправлено")
    if cq.message:
        await _cleanup_my_form_view(bot=cq.bot, chat_id=int(cq.message.chat.id), state=state)
        await _render_dm_menu(cq, session)

    await _notify_team_leads_new_form(cq.bot, settings, session, form, manager_tag)


@router.callback_query(F.data.startswith("dm:my_form_resume:"))
async def dm_my_form_resume_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    if not cq.from_user:
        return
    user = await get_user_by_tg_id(session, cq.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        await cq.answer("Нет прав", show_alert=True)
        return
    form_id = int(cq.data.split(":")[-1])
    form = await get_form(session, form_id)
    if not form or form.manager_id != user.id or form.status != FormStatus.IN_PROGRESS:
        await cq.answer("Анкета не найдена", show_alert=True)
        return

    await cq.answer()
    if cq.message:
        await _cleanup_my_form_view(bot=cq.bot, chat_id=int(cq.message.chat.id), state=state)
    await state.clear()
    await state.update_data(form_id=form.id)

    async def _prompt(text: str, reply_markup) -> None:
        if cq.message:
            await _safe_edit_message(message=cq.message, text=text, reply_markup=reply_markup)
        else:
            await cq.bot.send_message(int(cq.from_user.id), text, reply_markup=reply_markup)

    traffic_type = (form.traffic_type or "").upper()
    user_source = (getattr(user, "manager_source", None) or "").upper()
    if not traffic_type:
        if user_source == "TG":
            await state.set_state(DropManagerFormStates.phone)
            await _prompt(
                "Напишите номер и перешлите его в бота.\nСообщение должно содержать <b>только номер</b> и ничего больше:",
                kb_dm_back_cancel_inline(back_cb="dm:cancel_form"),
            )
        else:
            await state.set_state(DropManagerFormStates.traffic_type)
            await _prompt("Выберите тип клиента:", kb_dm_traffic_type_inline())
        return

    if traffic_type == "DIRECT":
        if not form.direct_user:
            await state.set_state(DropManagerFormStates.direct_forward)
            await _prompt(
                "Перешлите сообщение клиента (форвард):",
                kb_dm_back_cancel_inline(back_cb="dm:back_to_traffic"),
            )
            return
    if traffic_type == "REFERRAL":
        if not form.direct_user:
            await state.set_state(DropManagerFormStates.referral_forward_1)
            await _prompt(
                "1) Перешлите сообщение клиента (на кого анкета):",
                kb_dm_back_cancel_inline(back_cb="dm:back_to_traffic"),
            )
            return
        if not form.referral_user:
            await state.set_state(DropManagerFormStates.referral_forward_2)
            await _prompt(
                "2) Перешлите сообщение пользователя, который привёл клиента:",
                kb_dm_back_cancel_inline(back_cb="dm:back_to_ref1"),
            )
            return

    if not form.phone:
        await state.set_state(DropManagerFormStates.phone)
        back_cb = "dm:cancel_form" if user_source == "TG" else "dm:back_to_forward"
        await _prompt(
            "Напишите номер и перешлите его в бота.\nСообщение должно содержать <b>только номер</b> и ничего больше:",
            kb_dm_back_cancel_inline(back_cb=back_cb),
        )
        return

    if not form.bank_name:
        await state.set_state(DropManagerFormStates.bank_select)
        banks = await _list_banks_for_dm_source(session, getattr(user, "manager_source", None) if user else None)
        bank_items = _dm_bank_items_with_source(banks, getattr(user, "manager_source", None) if user else None)
        await _prompt("Выберите банк:", kb_dm_bank_select_inline_from_items(bank_items))
        return

    if not form.password:
        await state.set_state(DropManagerFormStates.password)
        instr = await _get_bank_instructions_text(session, user=user, bank_name=form.bank_name or "")
        if instr:
            await cq.bot.send_message(int(cq.from_user.id), instr, parse_mode="HTML")
        if instr:
            await cq.bot.send_message(
                int(cq.from_user.id),
                "Введите пароль/инкод:",
                reply_markup=kb_dm_back_cancel_inline(back_cb="dm:back_to_bank_select"),
            )
        else:
            await _prompt("Введите пароль/инкод:", kb_dm_back_cancel_inline(back_cb="dm:back_to_bank_select"))
        return

    if not form.screenshots:
        await state.set_state(DropManagerFormStates.screenshots)
        bank = await get_bank_by_name(session, form.bank_name or "")
        src = (getattr(user, "manager_source", None) or "").upper()
        required = None
        if bank:
            if src == "FB":
                required = getattr(bank, "required_screens_fb", None)
            elif src == "TG":
                required = getattr(bank, "required_screens_tg", None)
            if required is None:
                required = getattr(bank, "required_screens", None)
        await state.update_data(expected_screens=required, collected_screens=[])
        if required and required > 0:
            await _prompt(
                f"Перешлите скрины от банка как на запросе. Нужно <b>{required}</b> фото.\n\n"
                f"Отправьте скрин 1/{required}:",
                kb_dm_done_inline(),
            )
        else:
            await _prompt(
                "Перешлите скрины от банка как на запросе. Когда закончите — нажмите <b>Готово</b>.\n\n"
                "Отправляйте фото/видео/файлы:",
                kb_dm_done_inline(),
            )
        return

    if not form.comment:
        await state.set_state(DropManagerFormStates.comment)
        await _prompt("Комментарий к анкете:", kb_dm_back_cancel_inline(back_cb="dm:back_to_screens"))
        return

    await state.set_state(DropManagerFormStates.confirm)
    text = _format_form_text(form, user.manager_tag or "—")
    await _send_form_preview_with_keyboard(
        bot=cq.bot,
        chat_id=int(cq.from_user.id),
        text=text,
        photos=list(form.screenshots or []),
        reply_markup=kb_form_confirm_with_edit(form.id),
        state=state,
    )


@router.callback_query(F.data.startswith("dm:my_form_delete:"))
async def dm_my_form_delete_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    if not cq.from_user:
        return
    user = await get_user_by_tg_id(session, cq.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        await cq.answer("Нет прав", show_alert=True)
        return
    try:
        form_id = int((cq.data or "").split(":")[-1])
    except Exception:
        await cq.answer("Некорректная кнопка", show_alert=True)
        return
    form = await get_form(session, form_id)
    if not form or form.manager_id != user.id:
        await cq.answer("Анкета не найдена", show_alert=True)
        return

    await delete_form(session, form_id)
    await cq.answer("Удалено")
    if cq.message:
        await _cleanup_my_form_view(bot=cq.bot, chat_id=int(cq.message.chat.id), state=state)
    await _render_my_forms(cq, session, state)


@router.callback_query(F.data == "dm:start_shift")
async def dm_start_shift_cb(cq: CallbackQuery, session: AsyncSession) -> None:
    if not cq.from_user:
        return
    user = await get_user_by_tg_id(session, cq.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        await cq.answer("Нет прав", show_alert=True)
        return
    if not getattr(user, "forward_group_id", None):
        await cq.answer("Вас еще не привязали к группе пересылки", show_alert=True)
        if cq.message:
            src = getattr(user, "manager_source", None) or "—"
            await _safe_edit_message(
                message=cq.message,
                text=(
                    f"👤 <b>Дроп‑менеджер</b>: <b>{user.manager_tag or '—'}</b>\n"
                    f"Источник: <b>{src}</b>\n"
                    "Группа пересылки: <b>не привязана</b>\n\n"
                    "Сначала разработчик должен привязать вам группу."
                ),
                reply_markup=kb_dm_main_inline(shift_active=False),
            )
        return
    if not user.manager_tag:
        await cq.answer("Сначала введите тег менеджера", show_alert=True)
        return
    active = await get_active_shift(session, user.id)
    if active:
        await cq.answer("Смена уже активна", show_alert=True)
        return
    await start_shift(session, user.id)
    await cq.answer("✅ Смена начата")
    if cq.message:
        src = getattr(user, "manager_source", None) or "—"
        await _safe_edit_message(
            message=cq.message,
            text=(
                f"👤 <b>Дроп‑менеджер</b>: <b>{user.manager_tag}</b>\n"
                f"Источник: <b>{src}</b>\n"
                "Смена: <b>активна</b>"
            ),
            reply_markup=await _build_dm_main_kb(
                session=session,
                user_id=int(user.id),
                shift_active=True,
            ),
        )


async def _get_end_shift_block_reason(*, session: AsyncSession, user_id: int, shift_id: int, settings: Settings) -> str | None:
    # Require all shift forms to be approved by TL before shift end.
    pending_res = await session.execute(
        select(func.count(Form.id)).where(
            and_(
                Form.shift_id == int(shift_id),
                Form.status != FormStatus.APPROVED,
            )
        )
    )
    not_approved = int(pending_res.scalar() or 0)
    if not_approved > 0:
        return f"Нельзя завершить смену: есть {not_approved} анкет(а/ы) без апрува ТЛ."

    # Test bot stricter rule: no active links/esim assigned to DM.
    if (getattr(settings, "bot_token", "") or "").startswith("8115544053:"):
        active_links = await count_dm_active_pool_items(session, dm_user_id=int(user_id))
        if active_links > 0:
            return f"Нельзя завершить смену: у вас {active_links} активных ссылок/Esim. Сначала сдайте их."
    return None


@router.callback_query(F.data == "dm:end_shift")
async def dm_end_shift_prompt_cb(cq: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if not cq.from_user:
        return
    user = await get_user_by_tg_id(session, cq.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        await cq.answer("Нет прав", show_alert=True)
        return
    shift = await get_active_shift(session, user.id)
    if not shift:
        await cq.answer("У вас нет активной смены", show_alert=True)
        if cq.message:
            src = getattr(user, "manager_source", None) or "—"
            await _safe_edit_message(
                message=cq.message,
                text=(
                    f"👤 <b>Дроп‑менеджер</b>: <b>{user.manager_tag or '—'}</b>\n"
                    f"Источник: <b>{src}</b>\n"
                    "Смена: <b>не активна</b>"
                ),
                reply_markup=kb_dm_main_inline(shift_active=False),
            )
        return
    reason = await _get_end_shift_block_reason(
        session=session,
        user_id=int(user.id),
        shift_id=int(shift.id),
        settings=settings,
    )
    if reason:
        await cq.answer(reason, show_alert=True)
        if cq.message:
            await _safe_edit_message(message=cq.message, text=reason)
        return

    await cq.answer()
    if cq.message:
        await _safe_edit_message(
            message=cq.message,
            text="После подтверждения вы окончите смену и бот сформирует ваш отчет.",
            reply_markup=kb_yes_no(f"shift_end_confirm:{shift.id}", f"shift_end_cancel:{shift.id}"),
        )


@router.message(F.text == "Закончить работу")
async def end_work_prompt(message: Message, session: AsyncSession, settings: Settings) -> None:
    # Ignore group messages
    if message.chat.type in ['group', 'supergroup']:
        return
    if not message.from_user:
        return
    user = await get_user_by_tg_id(session, message.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        return
    shift = await get_active_shift(session, user.id)
    if not shift:
        await message.answer("У вас нет активной смены.", reply_markup=kb_dm_main_inline(shift_active=False))
        return
    reason = await _get_end_shift_block_reason(
        session=session,
        user_id=int(user.id),
        shift_id=int(shift.id),
        settings=settings,
    )
    if reason:
        await message.answer(reason)
        return

    await message.answer(
        "После нажатия кнопки вы окончите смену и бот сформирует ваш отчет.",
        reply_markup=kb_yes_no(f"shift_end_confirm:{shift.id}", f"shift_end_cancel:{shift.id}"),
    )


@router.callback_query(F.data.startswith("shift_end_cancel:"))
async def end_work_cancel(cq: CallbackQuery) -> None:
    await cq.answer("Ок")
    if cq.message:
        await _safe_edit_message(message=cq.message, text="Ок, смена продолжается.")


@router.callback_query(F.data.startswith("shift_end_confirm:"))
async def end_work_confirm(cq: CallbackQuery, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not cq.from_user:
        return
    user = await get_user_by_tg_id(session, cq.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        await cq.answer("Нет прав", show_alert=True)
        return

    shift_id = int(cq.data.split(":")[1])
    res = await session.execute(select(Shift).where(Shift.id == shift_id))
    shift = res.scalar_one_or_none()
    if not shift or shift.manager_id != user.id or shift.ended_at is not None:
        await cq.answer("Смена не найдена", show_alert=True)
        return

    reason = await _get_end_shift_block_reason(
        session=session,
        user_id=int(user.id),
        shift_id=int(shift.id),
        settings=settings,
    )
    if reason:
        await cq.answer(reason, show_alert=True)
        if cq.message:
            await _safe_edit_message(message=cq.message, text=reason)
        return

    await cq.answer()
    await state.set_state(DropManagerShiftStates.dialogs_count)
    await state.update_data(shift_id=shift.id)
    if cq.message:
        await _safe_edit_message(
            message=cq.message,
            text="Введите количество диалогов за сегодняшний день:",
            reply_markup=None,
        )


async def _finalize_shift_with_comment(
    *,
    session: AsyncSession,
    shift: Shift,
    manager_tag: str,
    manager_username: str | None,
    dialogs_count: int | None,
    comment_of_day: str | None,
) -> str:
    shift.ended_at = datetime.utcnow()
    shift.comment_of_day = (comment_of_day or None)
    shift.dialogs_count = dialogs_count

    duration = int((shift.ended_at - shift.started_at).total_seconds())

    agg = await session.execute(
        select(Form.bank_name, Form.traffic_type, func.count(Form.id))
        .where(
            and_(
                Form.shift_id == shift.id,
                Form.status != FormStatus.IN_PROGRESS,
                Form.bank_name.is_not(None),
                func.trim(Form.bank_name) != "",
            )
        )
        .group_by(Form.bank_name, Form.traffic_type)
        .order_by(Form.bank_name.asc())
    )
    rows = list(agg.all())
    bank_map: dict[str, dict[str, int]] = {}
    for bank_name, traffic_type, cnt in rows:
        bn = (bank_name or "").strip()
        if not bn:
            bn = "Без банка"
        tt = (traffic_type or "—").strip() or "—"
        bank_map.setdefault(bn, {})[tt] = int(cnt or 0)

    uname = html.escape(f"@{manager_username}" if manager_username else "—")
    dialogs = str(dialogs_count) if dialogs_count is not None else "—"
    safe_manager_tag = html.escape(manager_tag or "—")
    lines: list[str] = [
        "🧾 <b>Отчёт по смене:</b>",
        f"Менеджер - <b>{uname}</b>",
        f"Тег - <b>{safe_manager_tag}</b>",
        f"Количество диалогов - <b>{dialogs}</b>",
        f"Время в работе - <b>{format_timedelta_seconds(duration)}</b>",
        "",
    ]

    seen = set(bank_map.keys())
    bank_order = [b for b in DEFAULT_BANKS if b in seen] + [b for b in sorted(seen) if b not in DEFAULT_BANKS]
    if "Без банка" in seen:
        bank_order = [b for b in bank_order if b != "Без банка"] + ["Без банка"]
    total_direct = 0
    total_referral = 0
    for bank in bank_order:
        # TG source can store traffic_type as None/"—" (no split by direct/referral),
        # count such rows as direct in shift report.
        direct = bank_map.get(bank, {}).get("DIRECT", 0) + bank_map.get(bank, {}).get("—", 0)
        referral = bank_map.get(bank, {}).get("REFERRAL", 0)
        if direct == 0 and referral == 0:
            continue
        total_direct += direct
        total_referral += referral
        bank_display = bank if bank == "Без банка" else (bank[:1].upper() + bank[1:].lower()) if bank else "—"
        lines.append(f"{bank_display}:")
        lines.append(f"Прямой - <b>{direct}</b>")
        lines.append(f"Сарафан - <b>{referral}</b>")
        lines.append("")

    if lines and lines[-1] == "":
        lines.pop()

    lines.extend([
        "",
        "<b>Суммарно за день:</b>",
        f"Прямой - <b>{total_direct}</b>",
        f"Сарафан - <b>{total_referral}</b>",
    ])

    com = html.escape((comment_of_day or "").strip() or "—")
    lines.extend(["", "<b>Комментарий дня:</b>", com])
    await session.commit()
    return "\n".join(lines)


@router.callback_query(F.data.startswith("shift_comment_skip:"))
async def dm_shift_comment_skip(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    if not cq.from_user:
        return
    user = await get_user_by_tg_id(session, cq.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        await cq.answer("Нет прав", show_alert=True)
        return

    shift_id = int(cq.data.split(":")[1])
    res = await session.execute(select(Shift).where(Shift.id == shift_id))
    shift = res.scalar_one_or_none()
    if not shift or shift.manager_id != user.id or shift.ended_at is not None:
        await cq.answer("Смена не найдена", show_alert=True)
        return

    data = await state.get_data()
    dialogs_count = data.get("dialogs_count")
    report = await _finalize_shift_with_comment(
        session=session,
        shift=shift,
        manager_tag=user.manager_tag or "—",
        manager_username=user.username,
        dialogs_count=int(dialogs_count) if dialogs_count is not None else None,
        comment_of_day=None,
    )

    await _send_shift_report_to_forward_group(bot=cq.bot, session=session, user=user, report=report)
    await _maybe_send_team_total_report(bot=cq.bot, session=session, source=getattr(user, "manager_source", None))
    around_id = int(cq.message.message_id) if cq.message else None
    await _best_effort_cleanup_recent_messages(bot=cq.bot, chat_id=int(cq.from_user.id), around_message_id=around_id, limit=120)

    await state.clear()
    await cq.answer("Смена закрыта")
    if cq.message:
        try:
            await cq.message.delete()
        except Exception:
            pass
        await cq.message.answer(report)
        await cq.message.answer("Главное меню:", reply_markup=kb_dm_main_inline(shift_active=False))


@router.callback_query(F.data.startswith("shift_comment_back:"))
async def dm_shift_comment_back(cq: CallbackQuery, session: AsyncSession) -> None:
    if not cq.from_user:
        return
    user = await get_user_by_tg_id(session, cq.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        await cq.answer("Нет прав", show_alert=True)
        return

    shift_id = int(cq.data.split(":")[1])
    shift = await get_active_shift(session, user.id)
    if not shift or shift.id != shift_id:
        await cq.answer("Смена не найдена", show_alert=True)
        return

    await cq.answer()
    if cq.message:
        await cq.message.edit_text(
            "После подтверждения вы окончите смену и бот сформирует ваш отчет.",
            reply_markup=kb_yes_no(f"shift_end_confirm:{shift.id}", f"shift_end_cancel:{shift.id}"),
        )


@router.message(DropManagerShiftStates.dialogs_count, F.text)
async def dm_shift_dialogs_count_message(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not message.from_user:
        return
    user = await get_user_by_tg_id(session, message.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        return

    shift = await get_active_shift(session, user.id)
    if not shift:
        await state.clear()
        await message.answer("У вас нет активной смены.", reply_markup=kb_dm_main_inline(shift_active=False))
        return

    raw = (message.text or "").strip().replace(" ", "")
    if not raw.isdigit():
        await message.answer("Введите число (например: 12)")
        return
    dialogs_count = int(raw)
    await state.update_data(dialogs_count=dialogs_count)
    await state.set_state(DropManagerShiftStates.comment_of_day)
    await message.answer(
        "Введите <b>Комментарий дня</b> (одним сообщением):",
        reply_markup=kb_dm_shift_comment_inline(shift_id=int(shift.id)),
    )
    return


@router.message(DropManagerShiftStates.comment_of_day, F.text)
async def dm_shift_comment_message(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not message.from_user:
        return
    user = await get_user_by_tg_id(session, message.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        return

    shift = await get_active_shift(session, user.id)
    if not shift:
        await state.clear()
        await message.answer("У вас нет активной смены.", reply_markup=kb_dm_main_inline(shift_active=False))
        return

    txt = (message.text or "").strip()
    if not txt:
        await message.answer("Введите комментарий текстом одним сообщением.")
        return

    data = await state.get_data()
    dialogs_count = data.get("dialogs_count")
    report = await _finalize_shift_with_comment(
        session=session,
        shift=shift,
        manager_tag=user.manager_tag or "—",
        manager_username=user.username,
        dialogs_count=int(dialogs_count) if dialogs_count is not None else None,
        comment_of_day=txt,
    )

    await _send_shift_report_to_forward_group(bot=message.bot, session=session, user=user, report=report)
    await _maybe_send_team_total_report(bot=message.bot, session=session, source=getattr(user, "manager_source", None))
    await _best_effort_cleanup_recent_messages(bot=message.bot, chat_id=int(message.chat.id), around_message_id=int(message.message_id), limit=120)

    await state.clear()
    await message.answer(report)
    await message.answer("Главное меню:", reply_markup=kb_dm_main_inline(shift_active=False))


@router.message(F.text == "Создать анкету")
async def create_form_entry(message: Message, session: AsyncSession, state: FSMContext) -> None:
    # Ignore group messages
    if message.chat.type in ['group', 'supergroup']:
        return
    if not message.from_user:
        return
    user = await get_user_by_tg_id(session, message.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        return
    if not user.manager_tag:
        await message.answer("Сначала введите тег менеджера:")
        return
    shift = await get_active_shift(session, user.id)
    if not shift:
        await message.answer("Сначала нажмите <b>Начать работу</b>.", reply_markup=kb_dm_main_inline(shift_active=False))
        return
    form = await create_form(session, user.id, shift.id)
    await state.update_data(form_id=form.id)
    if (getattr(user, "manager_source", None) or "").upper() == "TG":
        form.traffic_type = None
        form.direct_user = None
        form.referral_user = None
        await state.set_state(DropManagerFormStates.phone)
        await message.answer(
            "Напишите номер и перешлите его в бота.\nСообщение должно содержать <b>только номер</b> и ничего больше:",
            reply_markup=kb_dm_back_cancel_inline(back_cb="dm:cancel_form"),
        )
    else:
        await state.set_state(DropManagerFormStates.traffic_type)
        await message.answer("Выберите тип клиента:", reply_markup=kb_dm_traffic_type_inline())


@router.callback_query(F.data == "dm:create_form")
async def dm_create_form_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    if not cq.from_user:
        return
    user = await get_user_by_tg_id(session, cq.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        await cq.answer("Нет прав", show_alert=True)
        return
    if not user.manager_tag:
        await cq.answer("Сначала введите тег менеджера", show_alert=True)
        return
    shift = await get_active_shift(session, user.id)
    if not shift:
        await cq.answer("Смена не активна", show_alert=True)
        if cq.message:
            src = getattr(user, "manager_source", None) or "—"
            await cq.message.edit_text(
                f"👤 <b>Дроп‑менеджер</b>: <b>{user.manager_tag}</b>\n"
                f"Источник: <b>{src}</b>\n"
                "Смена: <b>не активна</b>",
                reply_markup=kb_dm_main_inline(shift_active=False),
            )
        return

    form = await create_form(session, user.id, shift.id)
    await state.update_data(form_id=form.id)
    await cq.answer()
    if (getattr(user, "manager_source", None) or "").upper() == "TG":
        form.traffic_type = None
        form.direct_user = None
        form.referral_user = None
        await state.set_state(DropManagerFormStates.phone)
        if cq.message:
            await cq.message.edit_text(
                "Напишите номер и перешлите его в бота.\nСообщение должно содержать <b>только номер</b> и ничего больше:",
                reply_markup=kb_dm_back_cancel_inline(back_cb="dm:cancel_form"),
            )
    else:
        await state.set_state(DropManagerFormStates.traffic_type)
        if cq.message:
            await cq.message.edit_text("Выберите тип клиента:", reply_markup=kb_dm_traffic_type_inline())


@router.callback_query(F.data.startswith("dm:traffic:"))
async def dm_traffic_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    if not cq.from_user:
        return
    data = await state.get_data()
    form_id = data.get("form_id")
    if not form_id:
        await cq.answer("Нет анкеты", show_alert=True)
        return
    form = await get_form(session, int(form_id))
    if not form:
        await state.clear()
        await cq.answer("Анкета не найдена", show_alert=True)
        return

    choice = cq.data.split(":")[-1]
    if choice == "DIRECT":
        form.traffic_type = "DIRECT"
        form.referral_user = None
        await state.set_state(DropManagerFormStates.direct_forward)
        await cq.answer()
        if cq.message:
            await cq.message.edit_text(
                "Перешлите сообщение клиента (форвард):",
                reply_markup=kb_dm_back_cancel_inline(back_cb="dm:back_to_traffic"),
            )
        return
    if choice == "REFERRAL":
        form.traffic_type = "REFERRAL"
        await state.set_state(DropManagerFormStates.referral_forward_1)
        await cq.answer()
        if cq.message:
            await cq.message.edit_text(
                "1) Перешлите сообщение клиента (на кого анкета):",
                reply_markup=kb_dm_back_cancel_inline(back_cb="dm:back_to_traffic"),
            )
        return

    await cq.answer("Некорректный выбор", show_alert=True)


@router.callback_query(F.data == "dm:back_to_traffic")
async def dm_back_to_traffic_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await cq.answer()
    user = await get_user_by_tg_id(session, int(cq.from_user.id)) if cq.from_user else None
    if user and (getattr(user, "manager_source", None) or "").upper() == "TG":
        await state.set_state(DropManagerFormStates.phone)
        if cq.message:
            await cq.message.edit_text(
                "Напишите номер и перешлите его в бота.\nСообщение должно содержать <b>только номер</b> и ничего больше:",
                reply_markup=kb_dm_back_cancel_inline(back_cb="dm:cancel_form"),
            )
        return
    await state.set_state(DropManagerFormStates.traffic_type)
    if cq.message:
        await cq.message.edit_text("Выберите тип клиента:", reply_markup=kb_dm_traffic_type_inline())


@router.callback_query(F.data.in_({"dm:cancel_form", "dm:cancel"}))
async def dm_cancel_form_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await cq.answer()
    data = await state.get_data()
    form_id = data.get("form_id")
    if form_id:
        form = await get_form(session, int(form_id))
        if form:
            await session.delete(form)
    await state.clear()
    await _render_dm_menu(cq, session)


@router.message(DropManagerFormStates.traffic_type, F.text, F.text != "Назад")
async def form_traffic(message: Message, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    form = await get_form(session, int(data["form_id"]))
    if not form:
        await state.clear()
        return
    if message.text == "Прямой":
        form.traffic_type = "DIRECT"
        await state.set_state(DropManagerFormStates.direct_forward)
        await message.answer(
            "Перешлите сообщение клиента (форвард):",
            reply_markup=kb_dm_back_cancel_inline(back_cb="dm:back_to_traffic"),
        )
        return
    if message.text == "Сарафан":
        form.traffic_type = "REFERRAL"
        await state.set_state(DropManagerFormStates.referral_forward_1)
        await message.answer(
            "1) Перешлите сообщение клиента (на кого анкета):",
            reply_markup=kb_dm_back_cancel_inline(back_cb="dm:back_to_traffic"),
        )
        return
    await message.answer("Выберите кнопкой: Прямой / Сарафан")


def _forward_payload_missing_fields(payload: dict[str, Any]) -> tuple[bool, bool, bool]:
    username_missing = not str(payload.get("username") or "").strip()
    phone_missing = not str(payload.get("contact_phone") or "").strip()
    name_missing = not (
        str(payload.get("sender_user_name") or "").strip()
        or str(payload.get("first_name") or "").strip()
        or str(payload.get("last_name") or "").strip()
    )
    return username_missing, phone_missing, name_missing


async def _enrich_forward_payload_tg_id(*, bot: Any, payload: dict[str, Any]) -> dict[str, Any]:
    tg_id = payload.get("tg_id")
    if tg_id:
        return payload
    username = str(payload.get("username") or "").strip().lstrip("@").strip()
    if not username or username == ".":
        return payload
    try:
        chat = await bot.get_chat(f"@{username}")
        if getattr(chat, "id", None):
            payload["tg_id"] = int(chat.id)
    except Exception:
        return payload
    return payload


async def _continue_after_forward_capture(
    *,
    message: Message,
    session: AsyncSession,
    state: FSMContext,
    form: Form,
    payload_field: str,
    next_state: Any,
    next_prompt_text: str,
    next_reply_markup: Any,
) -> None:
    payload = dict(getattr(form, payload_field) or {})
    username_missing, phone_missing, name_missing = _forward_payload_missing_fields(payload)
    if username_missing:
        await state.update_data(
            forward_payload_field=payload_field,
            forward_next_state=next_state,
            forward_next_prompt_text=next_prompt_text,
            forward_next_reply_markup=next_reply_markup,
        )
        await state.set_state(DropManagerFormStates.forward_manual_username)
        await message.answer(
            "1) Впишите username, если он вам виден, если нет поставьте точку:",
            reply_markup=kb_dm_back_cancel_inline(back_cb="dm:back_to_forward"),
        )
        return
    if phone_missing:
        await state.update_data(
            forward_payload_field=payload_field,
            forward_next_state=next_state,
            forward_next_prompt_text=next_prompt_text,
            forward_next_reply_markup=next_reply_markup,
        )
        await state.set_state(DropManagerFormStates.forward_manual_phone)
        await message.answer(
            "2) Впишите номер телефона, если он вам виден, если нет поставьте точку:",
            reply_markup=kb_dm_back_cancel_inline(back_cb="dm:back_to_forward"),
        )
        return
    if name_missing:
        await state.update_data(
            forward_payload_field=payload_field,
            forward_next_state=next_state,
            forward_next_prompt_text=next_prompt_text,
            forward_next_reply_markup=next_reply_markup,
        )
        await state.set_state(DropManagerFormStates.forward_manual_name)
        await message.answer(
            "3) Впишите имя профиля, как подписан клиент (если нет — поставьте точку):",
            reply_markup=kb_dm_back_cancel_inline(back_cb="dm:back_to_forward"),
        )
        return

    await state.set_state(next_state)
    await message.answer(next_prompt_text, reply_markup=next_reply_markup)


async def _continue_after_forward_capture_edit(
    *,
    message: Message,
    session: AsyncSession,
    state: FSMContext,
    form: Form,
    payload_field: str,
    next_state: Any | None = None,
    next_prompt_text: str | None = None,
    next_reply_markup: Any | None = None,
) -> None:
    payload = dict(getattr(form, payload_field) or {})
    username_missing, phone_missing, name_missing = _forward_payload_missing_fields(payload)
    if username_missing:
        await state.update_data(
            forward_payload_field=payload_field,
            forward_next_state=next_state,
            forward_next_prompt_text=next_prompt_text,
            forward_next_reply_markup=next_reply_markup,
        )
        await state.set_state(DropManagerEditStates.forward_manual_username)
        await message.answer(
            "1) Впишите username, если он вам виден, если нет поставьте точку:",
            reply_markup=kb_dm_back_cancel_inline(back_cb=f"dm_edit:back:{int(form.id)}"),
        )
        return
    if phone_missing:
        await state.update_data(
            forward_payload_field=payload_field,
            forward_next_state=next_state,
            forward_next_prompt_text=next_prompt_text,
            forward_next_reply_markup=next_reply_markup,
        )
        await state.set_state(DropManagerEditStates.forward_manual_phone)
        await message.answer(
            "2) Впишите номер телефона, если он вам виден, если нет поставьте точку:",
            reply_markup=kb_dm_back_cancel_inline(back_cb=f"dm_edit:back:{int(form.id)}"),
        )
        return
    if name_missing:
        await state.update_data(
            forward_payload_field=payload_field,
            forward_next_state=next_state,
            forward_next_prompt_text=next_prompt_text,
            forward_next_reply_markup=next_reply_markup,
        )
        await state.set_state(DropManagerEditStates.forward_manual_name)
        await message.answer(
            "3) Впишите имя профиля, как подписан клиент (если нет — поставьте точку):",
            reply_markup=kb_dm_back_cancel_inline(back_cb=f"dm_edit:back:{int(form.id)}"),
        )
        return

    if next_state is not None:
        await state.set_state(next_state)
        await message.answer(str(next_prompt_text or ""), reply_markup=next_reply_markup)
        return

    await _after_dm_edit_show_form(message=message, session=session, state=state, form=form)


@router.message(DropManagerFormStates.direct_forward, F.text != "Назад")
async def form_direct_forward(message: Message, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    form = await get_form(session, int(data["form_id"]))
    if not form:
        await state.clear()
        return
    user = await get_user_by_tg_id(session, int(message.from_user.id)) if message.from_user else None
    if user and (getattr(user, "manager_source", None) or "").upper() == "TG":
        form.traffic_type = None
        form.direct_user = None
        form.referral_user = None
        await state.set_state(DropManagerFormStates.phone)
        await message.answer(
            "Напишите номер и перешлите его в бота.\nСообщение должно содержать <b>только номер</b> и ничего больше:",
            reply_markup=kb_dm_back_cancel_inline(back_cb="dm:cancel_form"),
        )
        return
    payload = extract_forward_payload(message)
    payload = await _enrich_forward_payload_tg_id(bot=message.bot, payload=payload)
    form.direct_user = payload
    await _continue_after_forward_capture(
        message=message,
        session=session,
        state=state,
        form=form,
        payload_field="direct_user",
        next_state=DropManagerFormStates.phone,
        next_prompt_text=(
            "Напишите номер и перешлите его в бота.\n"
            "Сообщение должно содержать <b>только номер</b> и ничего больше:"
        ),
        next_reply_markup=kb_dm_back_cancel_inline(back_cb="dm:back_to_forward"),
    )


@router.message(DropManagerFormStates.referral_forward_1, F.text != "Назад")
async def form_referral_forward_1(message: Message, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    form = await get_form(session, int(data["form_id"]))
    if not form:
        await state.clear()
        return
    payload = extract_forward_payload(message)
    payload = await _enrich_forward_payload_tg_id(bot=message.bot, payload=payload)
    form.direct_user = payload
    await _continue_after_forward_capture(
        message=message,
        session=session,
        state=state,
        form=form,
        payload_field="direct_user",
        next_state=DropManagerFormStates.referral_forward_2,
        next_prompt_text="2) Перешлите сообщение пользователя, который привёл клиента:",
        next_reply_markup=kb_dm_back_cancel_inline(back_cb="dm:back_to_ref1"),
    )


@router.message(DropManagerFormStates.referral_forward_2, F.text != "Назад")
async def form_referral_forward_2(message: Message, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    form = await get_form(session, int(data["form_id"]))
    if not form:
        await state.clear()
        return
    payload = extract_forward_payload(message)
    payload = await _enrich_forward_payload_tg_id(bot=message.bot, payload=payload)
    form.referral_user = payload
    await _continue_after_forward_capture(
        message=message,
        session=session,
        state=state,
        form=form,
        payload_field="referral_user",
        next_state=DropManagerFormStates.phone,
        next_prompt_text=(
            "Напишите номер и перешлите его в бота.\n"
            "Сообщение должно содержать <b>только номер</b> и ничего больше:"
        ),
        next_reply_markup=kb_dm_back_cancel_inline(back_cb="dm:back_to_forward"),
    )


@router.message(DropManagerFormStates.forward_manual_username, F.text)
async def form_forward_manual_username(message: Message, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    form = await get_form(session, int(data.get("form_id") or 0))
    if not form:
        await state.clear()
        return
    payload_field = str(data.get("forward_payload_field") or "direct_user")
    payload = dict(getattr(form, payload_field) or {})
    raw = (message.text or "").strip()
    if raw == ".":
        payload["username"] = "."
    elif raw:
        payload["username"] = raw.lstrip("@").strip()
    payload = await _enrich_forward_payload_tg_id(bot=message.bot, payload=payload)
    setattr(form, payload_field, payload)
    data = await state.get_data()
    await _continue_after_forward_capture(
        message=message,
        session=session,
        state=state,
        form=form,
        payload_field=payload_field,
        next_state=data.get("forward_next_state") or DropManagerFormStates.phone,
        next_prompt_text=str(data.get("forward_next_prompt_text") or ""),
        next_reply_markup=data.get("forward_next_reply_markup")
        or kb_dm_back_cancel_inline(back_cb="dm:back_to_forward"),
    )


@router.message(DropManagerFormStates.forward_manual_phone, F.text)
async def form_forward_manual_phone(message: Message, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    form = await get_form(session, int(data.get("form_id") or 0))
    if not form:
        await state.clear()
        return
    payload_field = str(data.get("forward_payload_field") or "direct_user")
    payload = dict(getattr(form, payload_field) or {})
    raw = (message.text or "").strip()
    if raw == ".":
        payload["contact_phone"] = "."
    elif raw:
        if not is_valid_phone(raw):
            await message.answer("Некорректный номер. Введите ещё раз или поставьте точку:")
            return
        payload["contact_phone"] = raw
    setattr(form, payload_field, payload)
    data = await state.get_data()
    await _continue_after_forward_capture(
        message=message,
        session=session,
        state=state,
        form=form,
        payload_field=payload_field,
        next_state=data.get("forward_next_state") or DropManagerFormStates.phone,
        next_prompt_text=str(data.get("forward_next_prompt_text") or ""),
        next_reply_markup=data.get("forward_next_reply_markup")
        or kb_dm_back_cancel_inline(back_cb="dm:back_to_forward"),
    )


@router.message(DropManagerFormStates.forward_manual_name, F.text)
async def form_forward_manual_name(message: Message, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    form = await get_form(session, int(data.get("form_id") or 0))
    if not form:
        await state.clear()
        return
    payload_field = str(data.get("forward_payload_field") or "direct_user")
    payload = dict(getattr(form, payload_field) or {})
    raw = (message.text or "").strip()
    if raw == ".":
        payload["sender_user_name"] = "."
    elif raw:
        payload["sender_user_name"] = raw
    setattr(form, payload_field, payload)
    data = await state.get_data()
    next_state = data.get("forward_next_state")
    next_prompt_text = data.get("forward_next_prompt_text")
    next_reply_markup = data.get("forward_next_reply_markup")
    if next_state:
        await state.set_state(next_state)
        await message.answer(str(next_prompt_text or ""), reply_markup=next_reply_markup)
        return
    await state.set_state(DropManagerFormStates.phone)
    await message.answer(
        "Напишите номер и перешлите его в бота.\n"
        "Сообщение должно содержать <b>только номер</b> и ничего больше:",
        reply_markup=kb_dm_back_cancel_inline(back_cb="dm:back_to_forward"),
    )


@router.callback_query(F.data == "dm:back_to_ref1")
async def dm_back_to_ref1_cb(cq: CallbackQuery, state: FSMContext) -> None:
    await cq.answer()
    await state.set_state(DropManagerFormStates.referral_forward_1)
    if cq.message:
        await cq.message.edit_text(
            "1) Перешлите сообщение клиента (на кого анкета):",
            reply_markup=kb_dm_back_cancel_inline(back_cb="dm:back_to_traffic"),
        )


@router.callback_query(F.data == "dm:back_to_forward")
async def dm_back_to_forward_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    form_id = data.get("form_id")
    if not form_id:
        await cq.answer("Нет анкеты", show_alert=True)
        return
    form = await get_form(session, int(form_id))
    if not form:
        await state.clear()
        await cq.answer("Анкета не найдена", show_alert=True)
        return

    await cq.answer()
    if form.traffic_type == "DIRECT":
        await state.set_state(DropManagerFormStates.direct_forward)
        if cq.message:
            await cq.message.edit_text(
                "Перешлите сообщение клиента (форвард):",
                reply_markup=kb_dm_back_cancel_inline(back_cb="dm:back_to_traffic"),
            )
        return
    if form.traffic_type == "REFERRAL":
        await state.set_state(DropManagerFormStates.referral_forward_2)
        if cq.message:
            await cq.message.edit_text(
                "2) Перешлите сообщение пользователя, который привёл клиента:",
                reply_markup=kb_dm_back_cancel_inline(back_cb="dm:back_to_ref1"),
            )
        return


@router.message(DropManagerFormStates.phone, F.text)
async def form_phone(message: Message, session: AsyncSession, state: FSMContext) -> None:
    phone_text = message.text.strip()
    if not is_valid_phone(phone_text):
        await message.answer("Сообщение должно содержать только номер. Пример: <code>944567892</code> или <code>+380991112233</code>")
        return
    data = await state.get_data()
    form = await get_form(session, int(data["form_id"]))
    if not form:
        await state.clear()
        return
    form.phone = normalize_phone(phone_text)

    # warn if phone already exists anywhere
    try:
        existing = await find_forms_by_phone(session, form.phone)
        existing = [f for f in existing if int(f.id) != int(form.id)]
        if existing:
            lines: list[str] = []
            for f in existing[:3]:
                other = await get_user_by_id(session, int(f.manager_id))
                other_tag = (other.manager_tag if other and other.manager_tag else "—")
                bank = format_bank_hashtag(getattr(f, "bank_name", None))
                lines.append(f"- #{f.id} | {bank} | {other_tag}")
            more = "" if len(existing) <= 3 else f"\n... и ещё <b>{len(existing) - 3}</b>"
            await message.answer("⚠️ Номер уже встречался в системе:\n" + "\n".join(lines) + more)
    except Exception:
        pass

    await ensure_default_banks(session)
    try:
        user = await get_user_by_tg_id(session, message.from_user.id) if message.from_user else None
        banks = await _list_banks_for_dm_source(session, getattr(user, "manager_source", None) if user else None)
        bank_items = _dm_bank_items_with_source(banks, getattr(user, "manager_source", None) if user else None)
    except Exception:
        bank_items = []
    await state.set_state(DropManagerFormStates.bank_select)
    await message.answer("Выберите банк:", reply_markup=kb_dm_bank_select_inline_from_items(bank_items))


@router.callback_query(F.data == "dm:back_to_phone")
async def dm_back_to_phone_cb(cq: CallbackQuery, state: FSMContext) -> None:
    await cq.answer()
    await state.set_state(DropManagerFormStates.phone)
    if cq.message:
        await _safe_edit_message(
            message=cq.message,
            text=(
                "Напишите номер и перешлите его в бота.\n"
                "Сообщение должно содержать <b>только номер</b> и ничего больше:"
            ),
            reply_markup=kb_dm_back_cancel_inline(back_cb="dm:back_to_forward"),
        )


@router.callback_query(F.data.startswith("dm:bank_id:"))
async def dm_bank_pick_id_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    if not cq.data:
        return
    try:
        bank_id = int(cq.data.split(":", 2)[-1])
    except Exception:
        await cq.answer("Некорректный банк", show_alert=True)
        return
    bank = await get_bank(session, bank_id)
    if not bank:
        await cq.answer("Некорректный банк", show_alert=True)
        return
    user = await get_user_by_tg_id(session, cq.from_user.id)
    src = (getattr(user, "manager_source", None) or "TG").upper() if user else "TG"
    allowed = (
        ((getattr(bank, "instructions_fb", None) or "").strip() or getattr(bank, "required_screens_fb", None) is not None)
        if src == "FB"
        else ((getattr(bank, "instructions_tg", None) or "").strip() or getattr(bank, "required_screens_tg", None) is not None)
    )
    if not allowed:
        await cq.answer("Банк недоступен для вашего источника", show_alert=True)
        return
    bank_name = str(bank.name)

    data = await state.get_data()
    form_id = data.get("form_id")
    if not form_id:
        await cq.answer("Нет анкеты", show_alert=True)
        return
    form = await get_form(session, int(form_id))
    if not form:
        await state.clear()
        await cq.answer("Анкета не найдена", show_alert=True)
        return

    form.bank_name = bank_name

    if form.phone:
        dup = await _phone_bank_duplicate_exists_by_key(
            session,
            phone=str(form.phone),
            bank_name=str(form.bank_name),
            exclude_form_id=int(form.id),
        )
        if dup:
            other = await get_user_by_id(session, int(dup.manager_id))
            other_tag = (other.manager_tag if other and other.manager_tag else "—")
            dm_user = await get_user_by_tg_id(session, cq.from_user.id)
            if dm_user:
                await _notify_team_leads_duplicate_bank_phone(
                    bot=cq.bot,
                    session=session,
                    dm_user=dm_user,
                    phone=str(form.phone),
                    bank_name=str(form.bank_name),
                )
            await state.set_state(DropManagerFormStates.bank_select)
            if cq.message:
                await _safe_edit_message(
                    message=cq.message,
                    text=(
                        "❌ Такой номер уже есть для этого банка.\n"
                        f"Менеджер: <b>{other_tag}</b>, анкета <code>#{dup.id}</code>\n\n"
                        "Выберите действие:"
                    ),
                    reply_markup=kb_dm_duplicate_bank_phone_inline(),
                )
            return

    user = await get_user_by_tg_id(session, cq.from_user.id)
    instr = await _get_bank_instructions_text(session, user=user, bank_name=bank_name)
    await state.set_state(DropManagerFormStates.password)
    await cq.answer()
    if cq.message:
        if instr:
            await cq.bot.send_message(int(cq.from_user.id), instr, parse_mode="HTML")
            await cq.bot.send_message(
                int(cq.from_user.id),
                "Введите пароль/инкод:",
                reply_markup=kb_dm_back_cancel_inline(back_cb="dm:back_to_bank_select"),
            )
        else:
            await _safe_edit_message(
                message=cq.message,
                text="Введите пароль/инкод:",
                reply_markup=kb_dm_back_cancel_inline(back_cb="dm:back_to_bank_select"),
            )


@router.callback_query(F.data.startswith("dm:bank:"))
async def dm_bank_pick_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    bank_name = cq.data.split(":", 2)[-1]
    if not await get_bank_by_name(session, bank_name):
        await cq.answer("Некорректный банк", show_alert=True)
        return
    data = await state.get_data()
    form_id = data.get("form_id")
    if not form_id:
        await cq.answer("Нет анкеты", show_alert=True)
        return
    form = await get_form(session, int(form_id))
    if not form:
        await state.clear()
        await cq.answer("Анкета не найдена", show_alert=True)
        return

    form.bank_name = bank_name

    if form.phone:
        dup = await _phone_bank_duplicate_exists_by_key(
            session,
            phone=str(form.phone),
            bank_name=str(form.bank_name),
            exclude_form_id=int(form.id),
        )
        if dup:
            other = await get_user_by_id(session, int(dup.manager_id))
            other_tag = (other.manager_tag if other and other.manager_tag else "—")
            dm_user = await get_user_by_tg_id(session, cq.from_user.id)
            if dm_user:
                await _notify_team_leads_duplicate_bank_phone(
                    bot=cq.bot,
                    session=session,
                    dm_user=dm_user,
                    phone=str(form.phone),
                    bank_name=str(form.bank_name),
                )
            await state.set_state(DropManagerFormStates.bank_select)
            if cq.message:
                await _safe_edit_message(
                    message=cq.message,
                    text=(
                        "❌ Такой номер уже есть для этого банка.\n"
                        f"Менеджер: <b>{other_tag}</b>, анкета <code>#{dup.id}</code>\n\n"
                        "Выберите действие:"
                    ),
                    reply_markup=kb_dm_duplicate_bank_phone_inline(),
                )
            return

    user = await get_user_by_tg_id(session, cq.from_user.id)
    instr = await _get_bank_instructions_text(session, user=user, bank_name=bank_name)
    await state.set_state(DropManagerFormStates.password)
    await cq.answer()
    if cq.message:
        if instr:
            await cq.bot.send_message(int(cq.from_user.id), instr, parse_mode="HTML")
            await cq.bot.send_message(
                int(cq.from_user.id),
                "Введите пароль/инкод:",
                reply_markup=kb_dm_back_cancel_inline(back_cb="dm:back_to_bank_select"),
            )
        else:
            await _safe_edit_message(
                message=cq.message,
                text="Введите пароль/инкод:",
                reply_markup=kb_dm_back_cancel_inline(back_cb="dm:back_to_bank_select"),
            )


@router.callback_query(F.data == "dm:bank_custom")
async def dm_bank_custom_cb(cq: CallbackQuery, state: FSMContext) -> None:
    await cq.answer("Ручной ввод банка отключён", show_alert=True)
    await state.set_state(DropManagerFormStates.bank_select)


@router.callback_query(F.data == "dm:back_to_bank_select")
async def dm_back_to_bank_select_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await cq.answer()
    await state.set_state(DropManagerFormStates.bank_select)
    if cq.message:
        try:
            user = await get_user_by_tg_id(session, int(cq.from_user.id)) if cq.from_user else None
            banks = await _list_banks_for_dm_source(session, getattr(user, "manager_source", None) if user else None)
            bank_items = _dm_bank_items_with_source(banks, getattr(user, "manager_source", None) if user else None)
        except Exception:
            bank_items = []
        await _safe_edit_message(
            message=cq.message,
            text="Выберите банк:",
            reply_markup=kb_dm_bank_select_inline_from_items(bank_items),
        )


@router.message(DropManagerFormStates.bank_select, F.text)
async def form_bank_select(message: Message, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    form = await get_form(session, int(data["form_id"]))
    if not form:
        await state.clear()
        return
    txt = message.text.strip()
    if txt == "Написать название":
        await message.answer("Ручной ввод банка отключён. Выберите банк кнопкой.")
        return
    if not await get_bank_by_name(session, txt):
        await message.answer("Выберите банк кнопкой.")
        return
    form.bank_name = txt

    if form.phone:
        dup = await _phone_bank_duplicate_exists_by_key(
            session,
            phone=str(form.phone),
            bank_name=str(form.bank_name),
            exclude_form_id=int(form.id),
        )
        if dup:
            other = await get_user_by_id(session, int(dup.manager_id))
            other_tag = (other.manager_tag if other and other.manager_tag else "—")
            dm_user = await get_user_by_tg_id(session, message.from_user.id) if message.from_user else None
            if dm_user:
                await _notify_team_leads_duplicate_bank_phone(
                    bot=message.bot,
                    session=session,
                    dm_user=dm_user,
                    phone=str(form.phone),
                    bank_name=str(form.bank_name),
                )
            await state.set_state(DropManagerFormStates.bank_select)
            await message.answer(
                "❌ Такой номер уже есть для этого банка.\n"
                f"Менеджер: <b>{other_tag}</b>, анкета <code>#{dup.id}</code>\n\n"
                "Выберите действие:",
                reply_markup=kb_dm_duplicate_bank_phone_inline(),
            )
            return

    user = await get_user_by_tg_id(session, message.from_user.id)
    instr = await _get_bank_instructions_text(session, user=user, bank_name=txt)
    await state.set_state(DropManagerFormStates.password)
    if instr:
        await message.answer(instr, parse_mode="HTML")
    await message.answer(
        "Введите пароль/инкод:",
        reply_markup=kb_dm_back_cancel_inline(back_cb="dm:back_to_bank_select"),
    )


@router.message(DropManagerFormStates.bank_custom, F.text)
async def form_bank_custom(message: Message, session: AsyncSession, state: FSMContext) -> None:
    bank_name = message.text.strip()
    if not bank_name or len(bank_name) > 64:
        await message.answer("Название банка слишком длинное/пустое. Введите ещё раз:")
        return
    data = await state.get_data()
    form = await get_form(session, int(data["form_id"]))
    if not form:
        await state.clear()
        return
    form.bank_name = bank_name
    if form.phone:
        dup = await _phone_bank_duplicate_exists_by_key(
            session,
            phone=str(form.phone),
            bank_name=str(form.bank_name),
            exclude_form_id=int(form.id),
        )
        if dup:
            other = await get_user_by_id(session, int(dup.manager_id))
            other_tag = (other.manager_tag if other and other.manager_tag else "—")
            dm_user = await get_user_by_tg_id(session, message.from_user.id) if message.from_user else None
            if dm_user:
                await _notify_team_leads_duplicate_bank_phone(
                    bot=message.bot,
                    session=session,
                    dm_user=dm_user,
                    phone=str(form.phone),
                    bank_name=str(form.bank_name),
                )
            await state.set_state(DropManagerFormStates.bank_select)
            await message.answer(
                "❌ Такой номер уже есть для этого банка.\n"
                f"Менеджер: <b>{other_tag}</b>, анкета <code>#{dup.id}</code>\n\n"
                "Выберите действие:",
                reply_markup=kb_dm_duplicate_bank_phone_inline(),
            )
            return
    if not await get_bank_by_name(session, bank_name):
        await create_bank(session, bank_name)
    user = await get_user_by_tg_id(session, message.from_user.id)
    instr = await _get_bank_instructions_text(session, user=user, bank_name=bank_name)
    await state.set_state(DropManagerFormStates.password)
    if instr:
        await message.answer(instr, parse_mode="HTML")
    await message.answer(
        "Введите пароль/инкод:",
        reply_markup=kb_dm_back_cancel_inline(back_cb="dm:back_to_bank_select"),
    )


@router.message(DropManagerFormStates.password, F.text)
async def form_password(message: Message, session: AsyncSession, state: FSMContext) -> None:
    pwd = (message.text or "").strip()
    if not pwd:
        await message.answer("Введите пароль/инкод:")
        return
    data = await state.get_data()
    form = await get_form(session, int(data["form_id"]))
    if not form:
        await state.clear()
        return
    form.password = pwd

    bank = await get_bank_by_name(session, form.bank_name or "")
    user = await get_user_by_id(session, form.manager_id)
    src = (getattr(user, "manager_source", None) or "").upper() if user else ""
    required = None
    if bank:
        if src == "FB":
            required = getattr(bank, "required_screens_fb", None)
        elif src == "TG":
            required = getattr(bank, "required_screens_tg", None)
        if required is None:
            required = getattr(bank, "required_screens", None)
    await state.update_data(expected_screens=required, collected_screens=[])
    await state.set_state(DropManagerFormStates.screenshots)

    if required and required > 0:
        await message.answer(
            f"Перешлите скрины от банка как на запросе. Нужно <b>{required}</b> фото.\n\n"
            f"Отправьте скрин 1/{required}:",
            reply_markup=kb_dm_done_inline(),
        )
    else:
        await message.answer(
            "Перешлите скрины от банка как на запросе. Когда закончите — нажмите <b>Готово</b>.\n\n"
            "Отправляйте фото/видео/файлы:",
            reply_markup=kb_dm_done_inline(),
        )


@router.callback_query(F.data == "dm:back_to_password")
async def dm_back_to_password_cb(cq: CallbackQuery, state: FSMContext) -> None:
    await cq.answer()
    await state.set_state(DropManagerFormStates.password)
    if cq.message:
        await _safe_edit_message(
            message=cq.message,
            text="Введите пароль/инкод:",
            reply_markup=kb_dm_back_cancel_inline(back_cb="dm:back_to_bank_select"),
        )


@router.callback_query(F.data == "dm:screens_done")
async def dm_screens_done_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    collected: list[str] = list(data.get("collected_screens") or [])
    if not collected:
        await cq.answer("Сначала отправьте хотя бы 1 файл.", show_alert=True)
        return
    form = await get_form(session, int(data["form_id"]))
    if not form:
        await state.clear()
        await cq.answer("Анкета не найдена", show_alert=True)
        return
    form.screenshots = collected
    await state.set_state(DropManagerFormStates.comment)
    await cq.answer()
    if cq.message:
        await _safe_edit_message(
            message=cq.message,
            text="Комментарий к анкете:",
            reply_markup=kb_dm_back_cancel_inline(back_cb="dm:back_to_screens"),
        )


@router.callback_query(F.data == "dm:back_to_screens")
async def dm_back_to_screens_cb(cq: CallbackQuery, state: FSMContext) -> None:
    await cq.answer()
    await state.set_state(DropManagerFormStates.screenshots)
    if cq.message:
        data = await state.get_data()
        expected = data.get("expected_screens")
        if expected and int(expected) > 0:
            text = (
                f"Перешлите скрины от банка как на запросе. Нужно <b>{int(expected)}</b> фото.\n\n"
                f"Отправьте следующий скрин:")
        else:
            text = (
                "Перешлите скрины от банка как на запросе. Когда закончите — нажмите <b>Готово</b>.\n\n"
                "Отправляйте фото/видео/файлы:")
        await _safe_edit_message(message=cq.message, text=text, reply_markup=kb_dm_done_inline())


@router.message(DropManagerFormStates.screenshots, F.photo)
async def form_screenshot_add(message: Message, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    expected = data.get("expected_screens")
    collected: list[str] = list(data.get("collected_screens") or [])

    file_id = pack_media_item("photo", message.photo[-1].file_id)
    media_group_id = getattr(message, "media_group_id", None)
    if media_group_id:
        key = (int(message.chat.id), str(media_group_id))
        _album_counts[key] = _album_counts.get(key, 0) + 1
    if len(collected) < 20:
        collected.append(file_id)
    await state.update_data(collected_screens=collected)

    if expected and expected > 0:
        if len(collected) < expected:
            if media_group_id:
                _schedule_album_ack(
                    bot=message.bot,
                    chat_id=int(message.chat.id),
                    media_group_id=str(media_group_id),
                    accepted_total=len(collected),
                    expected=int(expected),
                    reply_markup=kb_dm_done_inline(),
                )
                return
            await _upsert_prompt_message(
                message=message,
                state=state,
                state_key="screens_prompt_msg_id",
                text=f"Отправьте скрин {len(collected)+1}/{expected}:",
                reply_markup=kb_dm_done_inline(),
            )
            return
        # save to db and move on
        form = await get_form(session, int(data["form_id"]))
        if not form:
            await state.clear()
            return
        form.screenshots = collected
        await state.set_state(DropManagerFormStates.comment)
        try:
            await message.answer(
                "Комментарий к анкете:",
                reply_markup=kb_dm_back_cancel_inline(back_cb="dm:back_to_screens"),
            )
        except TelegramNetworkError:
            return
        return

    # free mode (no expected)
    if media_group_id:
        # do a single ack per album
        _schedule_album_ack(
            bot=message.bot,
            chat_id=int(message.chat.id),
            media_group_id=str(media_group_id),
            accepted_total=len(collected),
            expected=None,
            reply_markup=kb_dm_done_inline(),
        )
        if len(collected) >= 20:
            form = await get_form(session, int(data["form_id"]))
            if not form:
                await state.clear()
                return
            form.screenshots = collected
            await state.set_state(DropManagerFormStates.comment)
            try:
                await message.answer(
                    "Комментарий к анкете:",
                    reply_markup=kb_dm_back_cancel_inline(back_cb="dm:back_to_screens"),
                )
            except TelegramNetworkError:
                return
        return

    await _upsert_prompt_message(
        message=message,
        state=state,
        state_key="screens_prompt_msg_id",
        text=f"✅ Скрин {len(collected)} принят. Отправляйте ещё или нажмите <b>Готово</b>.",
        reply_markup=kb_dm_done_inline(),
    )


@router.message(DropManagerFormStates.screenshots, F.document)
async def form_screenshot_add_document(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not getattr(message, "document", None):
        return
    data = await state.get_data()
    expected = data.get("expected_screens")
    collected: list[str] = list(data.get("collected_screens") or [])

    file_id = pack_media_item("doc", message.document.file_id)
    media_group_id = getattr(message, "media_group_id", None)
    if media_group_id:
        key = (int(message.chat.id), str(media_group_id))
        _album_counts[key] = _album_counts.get(key, 0) + 1
    if len(collected) < 20:
        collected.append(file_id)
    await state.update_data(collected_screens=collected)

    if expected and expected > 0:
        if len(collected) < expected:
            if media_group_id:
                _schedule_album_ack(
                    bot=message.bot,
                    chat_id=int(message.chat.id),
                    media_group_id=str(media_group_id),
                    accepted_total=len(collected),
                    expected=int(expected),
                    reply_markup=kb_dm_done_inline(),
                )
                return
            await _upsert_prompt_message(
                message=message,
                state=state,
                state_key="screens_prompt_msg_id",
                text=f"Отправьте скрин {len(collected)+1}/{expected}:",
                reply_markup=kb_dm_done_inline(),
            )
            return
        form = await get_form(session, int(data["form_id"]))
        if not form:
            await state.clear()
            return
        form.screenshots = collected
        await state.set_state(DropManagerFormStates.comment)
        try:
            await message.answer(
                "Комментарий к анкете:",
                reply_markup=kb_dm_back_cancel_inline(back_cb="dm:back_to_screens"),
            )
        except TelegramNetworkError:
            return
        return

    if media_group_id:
        _schedule_album_ack(
            bot=message.bot,
            chat_id=int(message.chat.id),
            media_group_id=str(media_group_id),
            accepted_total=len(collected),
            expected=None,
            reply_markup=kb_dm_done_inline(),
        )
        if len(collected) >= 20:
            form = await get_form(session, int(data["form_id"]))
            if not form:
                await state.clear()
                return
            form.screenshots = collected
            await state.set_state(DropManagerFormStates.comment)
            try:
                await message.answer(
                    "Комментарий к анкете:",
                    reply_markup=kb_dm_back_cancel_inline(back_cb="dm:back_to_screens"),
                )
            except TelegramNetworkError:
                return
        return

    await _upsert_prompt_message(
        message=message,
        state=state,
        state_key="screens_prompt_msg_id",
        text=f"✅ Скрин {len(collected)} принят. Отправляйте ещё или нажмите <b>Готово</b>.",
        reply_markup=kb_dm_done_inline(),
    )


@router.message(DropManagerFormStates.screenshots, F.video)
async def form_screenshot_add_video(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not getattr(message, "video", None):
        return
    data = await state.get_data()
    expected = data.get("expected_screens")
    collected: list[str] = list(data.get("collected_screens") or [])

    file_id = pack_media_item("video", message.video.file_id)
    media_group_id = getattr(message, "media_group_id", None)
    if media_group_id:
        key = (int(message.chat.id), str(media_group_id))
        _album_counts[key] = _album_counts.get(key, 0) + 1
    if len(collected) < 20:
        collected.append(file_id)
    await state.update_data(collected_screens=collected)

    if expected and expected > 0:
        if len(collected) < expected:
            if media_group_id:
                _schedule_album_ack(
                    bot=message.bot,
                    chat_id=int(message.chat.id),
                    media_group_id=str(media_group_id),
                    accepted_total=len(collected),
                    expected=int(expected),
                    reply_markup=kb_dm_done_inline(),
                )
                return
            await _upsert_prompt_message(
                message=message,
                state=state,
                state_key="screens_prompt_msg_id",
                text=f"Отправьте скрин {len(collected)+1}/{expected}:",
                reply_markup=kb_dm_done_inline(),
            )
            return
        form = await get_form(session, int(data["form_id"]))
        if not form:
            await state.clear()
            return
        form.screenshots = collected
        await state.set_state(DropManagerFormStates.comment)
        try:
            await message.answer(
                "Комментарий к анкете:",
                reply_markup=kb_dm_back_cancel_inline(back_cb="dm:back_to_screens"),
            )
        except TelegramNetworkError:
            return
        return

    if media_group_id:
        _schedule_album_ack(
            bot=message.bot,
            chat_id=int(message.chat.id),
            media_group_id=str(media_group_id),
            accepted_total=len(collected),
            expected=None,
            reply_markup=kb_dm_done_inline(),
        )
        if len(collected) >= 20:
            form = await get_form(session, int(data["form_id"]))
            if not form:
                await state.clear()
                return
            form.screenshots = collected
            await state.set_state(DropManagerFormStates.comment)
            try:
                await message.answer(
                    "Комментарий к анкете:",
                    reply_markup=kb_dm_back_cancel_inline(back_cb="dm:back_to_screens"),
                )
            except TelegramNetworkError:
                return
        return

    await _upsert_prompt_message(
        message=message,
        state=state,
        state_key="screens_prompt_msg_id",
        text=f"✅ Скрин {len(collected)} принят. Отправляйте ещё или нажмите <b>Готово</b>.",
        reply_markup=kb_dm_done_inline(),
    )


@router.message(DropManagerFormStates.screenshots)
async def form_screenshot_wrong(message: Message) -> None:
    await message.answer("Нужно отправить <b>фото</b>, <b>видео</b> или <b>файл</b> (скрин).")


@router.message(DropManagerFormStates.comment, F.text)
async def form_comment(message: Message, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    form = await get_form(session, int(data["form_id"]))
    if not form:
        await state.clear()
        return
    form.comment = message.text.strip()
    await state.set_state(DropManagerFormStates.confirm)
    user = await get_user_by_tg_id(session, message.from_user.id)
    text = _format_form_text(form, user.manager_tag or "—")
    photos = list(form.screenshots or [])
    await _send_form_preview_with_keyboard(
        bot=message.bot,
        chat_id=int(message.chat.id),
        text=text,
        photos=photos,
        reply_markup=kb_form_confirm_with_edit(form.id),
        state=state,
    )


@router.callback_query(F.data.in_({"form_submit", "form_cancel"}))
async def form_confirm_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    data = await state.get_data()
    form_id = data.get("form_id")
    if not form_id:
        await cq.answer("Нет анкеты", show_alert=True)
        return
    form = await get_form(session, int(form_id))
    if not form:
        await state.clear()
        return

    if cq.data == "form_submit" and not (getattr(form, "bank_name", None) or "").strip():
        await cq.answer("Выберите банк", show_alert=True)
        await state.set_state(DropManagerFormStates.bank_select)
        if cq.message:
            try:
                user = await get_user_by_tg_id(session, int(cq.from_user.id)) if cq.from_user else None
                banks = await _list_banks_for_dm_source(session, getattr(user, "manager_source", None) if user else None)
                bank_items = _dm_bank_items_with_source(banks, getattr(user, "manager_source", None) if user else None)
            except Exception:
                bank_items = []
            await _safe_edit_message(
                message=cq.message,
                text="Выберите банк:",
                reply_markup=kb_dm_bank_select_inline_from_items(bank_items),
            )
        return

    # hard-block duplicate phone+bank
    if cq.data == "form_submit" and form.phone and form.bank_name:
        dup = await _phone_bank_duplicate_exists_by_key(
            session,
            phone=str(form.phone),
            bank_name=str(form.bank_name),
            exclude_form_id=int(form.id),
        )
        if dup:
            other = await get_user_by_id(session, int(dup.manager_id))
            other_tag = (other.manager_tag if other and other.manager_tag else "—")
            await cq.answer(
                f"❌ Такой номер уже есть для этого банка (менеджер: {other_tag}, анкета #{dup.id})",
                show_alert=True,
            )
            return

    if cq.data == "form_cancel":
        await delete_form(session, int(form_id))
        await state.clear()
        await cq.answer("Отменено")
        if cq.message:
            shift = await get_active_shift(session, form.manager_id)
            await _safe_edit_message(
                message=cq.message,
                text="Анкета отменена.",
                reply_markup=await _build_dm_main_kb(
                    session=session,
                    user_id=int(form.manager_id),
                    shift_active=bool(shift),
                ),
            )
        return

    form.status = FormStatus.PENDING
    await state.clear()

    await cq.answer("Отправлено")
    if cq.message:
        shift = await get_active_shift(session, form.manager_id)
        await _safe_edit_message(
            message=cq.message,
            text="✅ Анкета отправлена тим‑лиду на проверку.",
            reply_markup=await _build_dm_main_kb(
                session=session,
                user_id=int(form.manager_id),
                shift_active=bool(shift),
            ),
        )

    # notify TL
    manager = await get_user_by_tg_id(session, cq.from_user.id)
    await _notify_team_leads_new_form(cq.bot, settings, session, form, manager.manager_tag or "—")


@router.callback_query(FormEditCb.filter(F.action == "open"))
async def edit_open(cq: CallbackQuery, callback_data: FormEditCb, session: AsyncSession, state: FSMContext) -> None:
    if not cq.from_user:
        return
    user = await get_user_by_tg_id(session, cq.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        await cq.answer("Нет прав", show_alert=True)
        return
    form = await get_form(session, callback_data.form_id)
    if not form or form.manager_id != user.id:
        await cq.answer("Анкета не найдена", show_alert=True)
        return
    await cq.answer()

    # Remember where edit was opened from so "Назад" returns to the previous menu
    source_text = ""
    try:
        if cq.message and getattr(cq.message, "text", None):
            source_text = str(cq.message.text or "")
        elif cq.message and getattr(cq.message, "caption", None):
            source_text = str(cq.message.caption or "")
    except Exception:
        source_text = ""
    edit_return_mode = "edit_actions"
    if source_text.strip().lower().startswith("выберите действие"):
        edit_return_mode = "my_forms_actions"
    if (cq.data or "").startswith("dm:rej:") or ("отклоненные анкеты" in source_text.strip().lower()):
        edit_return_mode = "rejected_list"
    if "отклонена" in source_text.strip().lower():
        edit_return_mode = "dm_menu"

    notice_id = pop_dm_reject_notice(int(cq.from_user.id), int(form.id))
    if notice_id:
        try:
            await cq.bot.delete_message(chat_id=int(cq.from_user.id), message_id=int(notice_id))
        except Exception:
            pass

    # Keep the form message, just swap inline keyboard to edit-actions
    if cq.message:
        await _cleanup_my_form_view(bot=cq.bot, chat_id=int(cq.message.chat.id), state=state)
        await _cleanup_edit_preview(bot=cq.bot, chat_id=int(cq.message.chat.id), state=state)
        await state.clear()
        await state.set_state(DropManagerEditStates.choose_field)
        await state.update_data(form_id=form.id, edit_return_mode=edit_return_mode)
        manager_tag = user.manager_tag or "—"
        text = _format_form_text(form, manager_tag)
        buttons_text = "Выберите действие:"
        if form.status == FormStatus.REJECTED:
            tl_comment = (form.team_lead_comment or "").strip() or "Комментария нет"
            buttons_text = f"Комментарий TL: <b>{tl_comment}</b>\n\n{buttons_text}"
        await _send_form_preview_with_keyboard(
            bot=cq.bot,
            chat_id=int(cq.message.chat.id),
            text=text,
            photos=list(form.screenshots or []),
            reply_markup=kb_dm_edit_actions_inline(form.id),
            buttons_text=buttons_text,
            state=state,
        )
        if edit_return_mode not in {"rejected_list", "dm_menu"}:
            try:
                await cq.message.delete()
            except Exception:
                pass
        return

    await state.clear()
    await state.set_state(DropManagerEditStates.choose_field)
    await state.update_data(form_id=form.id, edit_return_mode=edit_return_mode)


@router.callback_query(F.data.startswith("dm_edit:back:"))
async def dm_edit_back_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await cq.answer()
    parts = cq.data.split(":")
    form_id = int(parts[-1])
    form = await get_form(session, form_id)
    if not form:
        await state.clear()
        return

    data = await state.get_data()
    edit_return_mode = (data.get("edit_return_mode") or "").strip()

    # If edit was opened from "Мои анкеты" action menu, return back to that menu (without re-sending the form)
    if edit_return_mode == "my_forms_actions":
        await state.set_state(DropManagerMyFormsStates.form_view)
        await state.update_data(my_form_id=form.id)
        if cq.message:
            await _cleanup_edit_preview(bot=cq.bot, chat_id=int(cq.message.chat.id), state=state)
            await _cleanup_edit_prompt(bot=cq.bot, chat_id=int(cq.message.chat.id), state=state)
            try:
                await cq.message.delete()
            except Exception:
                pass

        user = await get_user_by_tg_id(session, cq.from_user.id) if cq.from_user else None
        manager_tag = (user.manager_tag if user and user.manager_tag else "—")
        try:
            text = _format_form_text(form, manager_tag)
        except Exception:
            text = f"📄 <b>Анкета</b>\nID: <code>{form.id}</code>"

        await _cleanup_my_form_view(bot=cq.bot, chat_id=int(cq.message.chat.id), state=state)
        photos = list(form.screenshots or [])
        chat_id = int(cq.message.chat.id) if cq.message else int(cq.from_user.id)
        if photos:
            # Send album with caption, then buttons as separate message
            try:
                media = [InputMediaPhoto(media=photos[0], caption=text, parse_mode="HTML")]
                for p in photos[1:10]:
                    media.append(InputMediaPhoto(media=p))
                album_msgs = await cq.bot.send_media_group(chat_id, media)
                buttons_msg = await cq.bot.send_message(
                    chat_id,
                    "Выберите действие:",
                    reply_markup=kb_dm_my_form_open(int(form.id)),
                )
                album_ids = [int(m.message_id) for m in (album_msgs or [])]
                await state.update_data(my_form_msg_ids=[*album_ids, buttons_msg.message_id])
            except Exception:
                # fallback: send text, then buttons as separate messages if album fails
                try:
                    main_msg = await cq.bot.send_message(chat_id, text, parse_mode="HTML")
                    buttons_msg = await cq.bot.send_message(
                        chat_id,
                        "Выберите действие:",
                        reply_markup=kb_dm_my_form_open(int(form.id)),
                    )
                    await state.update_data(my_form_msg_ids=[main_msg.message_id, buttons_msg.message_id])
                except Exception:
                    pass
        else:
            # No photos: send text, then buttons as separate message
            try:
                main_msg = await cq.bot.send_message(chat_id, text, parse_mode="HTML")
                buttons_msg = await cq.bot.send_message(
                    chat_id,
                    "Выберите действие:",
                    reply_markup=kb_dm_my_form_open(int(form.id)),
                )
                await state.update_data(my_form_msg_ids=[main_msg.message_id, buttons_msg.message_id])
            except Exception:
                pass
        return

    if edit_return_mode == "rejected_list":
        if cq.message:
            await _cleanup_edit_preview(bot=cq.bot, chat_id=int(cq.message.chat.id), state=state)
            await _cleanup_edit_prompt(bot=cq.bot, chat_id=int(cq.message.chat.id), state=state)
        await state.clear()
        await dm_rejected_cb(cq, session, state)
        return

    if edit_return_mode == "dm_menu":
        if cq.message:
            await _cleanup_edit_preview(bot=cq.bot, chat_id=int(cq.message.chat.id), state=state)
            await _cleanup_edit_prompt(bot=cq.bot, chat_id=int(cq.message.chat.id), state=state)
        await state.clear()
        await _render_dm_menu(cq, session)
        return

    # back target depends on form status: draft -> confirm keyboard, otherwise -> edit-actions
    user = await get_user_by_tg_id(session, cq.from_user.id) if cq.from_user else None
    manager_tag = (user.manager_tag if user and user.manager_tag else "—")
    text = _format_form_text(form, manager_tag)
    chat_id = int(cq.message.chat.id) if cq.message else int(cq.from_user.id)
    await _cleanup_edit_preview(bot=cq.bot, chat_id=chat_id, state=state)
    if cq.message:
        try:
            await cq.message.delete()
        except Exception:
            pass
    if form.status == FormStatus.IN_PROGRESS:
        await state.set_state(DropManagerFormStates.confirm)
        await state.update_data(form_id=form.id)
        await _send_form_preview_with_keyboard(
            bot=cq.bot,
            chat_id=chat_id,
            text=text,
            photos=list(form.screenshots or []),
            reply_markup=kb_form_confirm_with_edit(form.id),
            state=state,
        )
    else:
        await state.set_state(DropManagerEditStates.choose_field)
        await state.update_data(form_id=form.id)
        await _send_form_preview_with_keyboard(
            bot=cq.bot,
            chat_id=chat_id,
            text=text,
            photos=list(form.screenshots or []),
            reply_markup=kb_dm_edit_actions_inline(form.id),
            state=state,
        )


@router.callback_query(F.data == "dm_edit:cancel")
async def dm_edit_cancel_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await cq.answer()
    if cq.message:
        await _cleanup_edit_preview(bot=cq.bot, chat_id=int(cq.message.chat.id), state=state)
    await state.clear()
    try:
        if cq.message:
            await cq.message.delete()
    except Exception:
        pass
    await _render_dm_menu(cq, session)


@router.callback_query(F.data.startswith("dm_edit:resubmit:"))
async def dm_edit_resubmit_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not cq.from_user:
        return
    parts = cq.data.split(":")
    form_id = int(parts[-1])
    form = await get_form(session, form_id)
    if not form:
        await cq.answer("Анкеты нету", show_alert=True)
        await state.clear()
        return
    user = await get_user_by_tg_id(session, cq.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER or form.manager_id != user.id:
        await cq.answer("Нет прав", show_alert=True)
        return

    if not (getattr(form, "bank_name", None) or "").strip():
        await cq.answer("Выберите банк", show_alert=True)
        await state.set_state(DropManagerEditStates.bank_select)
        await state.update_data(form_id=form.id)
        if cq.message:
            await _cleanup_edit_preview(bot=cq.bot, chat_id=int(cq.message.chat.id), state=state)
            try:
                banks = await _list_banks_for_dm_source(session, getattr(user, "manager_source", None))
                bank_items = _dm_bank_items_with_source(banks, getattr(user, "manager_source", None) if user else None)
            except Exception:
                bank_items = []
            await _safe_edit_message(
                message=cq.message,
                text="Выберите банк:",
                reply_markup=kb_dm_edit_bank_select_inline_from_items(form_id=form.id, items=bank_items),
            )
        return

    # hard-block duplicate phone+bank
    if form.phone and form.bank_name:
        dup = await _phone_bank_duplicate_exists_by_key(
            session,
            phone=str(form.phone),
            bank_name=str(form.bank_name),
            exclude_form_id=int(form.id),
        )
        if dup:
            other = await get_user_by_id(session, int(dup.manager_id))
            other_tag = (other.manager_tag if other and other.manager_tag else "—")
            await cq.answer(
                f"❌ Такой номер уже есть для этого банка (менеджер: {other_tag}, анкета #{dup.id})",
                show_alert=True,
            )
            return

    form.status = FormStatus.PENDING
    form.team_lead_comment = None
    await cq.answer("Отправлено")
    if cq.message:
        await _cleanup_edit_preview(bot=cq.bot, chat_id=int(cq.message.chat.id), state=state)
    await state.clear()
    if cq.message:
        shift = await get_active_shift(session, user.id)
        await _safe_edit_message(
            message=cq.message,
            text="✅ Отправлено тим‑лиду на повторную проверку.",
            reply_markup=await _build_dm_main_kb(
                session=session,
                user_id=int(form.manager_id),
                shift_active=bool(shift),
            ),
        )
    await _notify_team_leads_new_form(cq.bot, settings, session, form, user.manager_tag or "—")


@router.callback_query(F.data.startswith("dm_edit:screens:"))
async def dm_edit_screens_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await cq.answer()
    form_id = int(cq.data.split(":")[-1])
    form = await get_form(session, form_id)
    if not form:
        await state.clear()
        return
    await state.set_state(DropManagerEditStates.screenshots)
    await state.update_data(form_id=form.id)
    shots = list(form.screenshots or [])
    cnt = len(shots)
    if cq.message:
        await _cleanup_edit_preview(bot=cq.bot, chat_id=int(cq.message.chat.id), state=state)
        try:
            await cq.message.delete()
        except Exception:
            pass
        if cnt <= 0:
            await state.update_data(expected_screens=None, collected_screens=[])
            await cq.message.answer(
                "Нет скринов в анкете. Отправляйте фото/видео/файлы:",
                reply_markup=kb_dm_edit_done_inline(form.id),
            )
        else:
            await cq.message.answer(
                "Выберите, какой скрин заменить:",
                reply_markup=kb_dm_edit_screens_inline(form.id, shots),
            )


@router.callback_query(F.data.startswith("dm_edit:screens_done:"))
async def dm_edit_screens_done_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await cq.answer()
    parts = (cq.data or "").split(":")
    if len(parts) != 3:
        return
    form_id = int(parts[-1])
    data = await state.get_data()
    collected: list[str] = list(data.get("collected_screens") or [])
    if not collected:
        await cq.answer("Сначала отправьте фото.", show_alert=True)
        return
    form = await get_form(session, form_id)
    if not form:
        await state.clear()
        return
    form.screenshots = collected
    manager = await get_user_by_id(session, form.manager_id)
    manager_tag = (manager.manager_tag if manager and manager.manager_tag else "—")
    text = _format_form_text(form, manager_tag)

    if form.status == FormStatus.IN_PROGRESS:
        await state.set_state(DropManagerFormStates.confirm)
        await state.update_data(form_id=form.id)
        await _send_form_preview_with_keyboard(
            bot=cq.bot,
            chat_id=int(cq.message.chat.id) if cq.message else int(cq.from_user.id),
            text=text,
            photos=list(form.screenshots or []),
            reply_markup=kb_form_confirm_with_edit(form.id),
            state=state,
        )
        return

    await state.set_state(DropManagerEditStates.choose_field)
    await _send_form_preview_with_keyboard(
        bot=cq.bot,
        chat_id=int(cq.message.chat.id) if cq.message else int(cq.from_user.id),
        text=text,
        photos=list(form.screenshots or []),
        reply_markup=kb_dm_edit_actions_inline(form.id),
        state=state,
    )


@router.callback_query(F.data.startswith("dm_edit:screen_pick:"))
async def dm_edit_screen_pick_cb(cq: CallbackQuery, state: FSMContext) -> None:
    await cq.answer()
    parts = (cq.data or "").split(":")
    if len(parts) != 4:
        await cq.answer("Некорректная кнопка", show_alert=True)
        return
    _, _, form_id_raw, idx_raw = parts
    await state.set_state(DropManagerEditStates.screenshot_replace)
    await state.update_data(form_id=int(form_id_raw), replace_index=int(idx_raw))
    if cq.message:
        await _set_edit_prompt_message(
            message=cq.message,
            state=state,
            text=f"Отправьте новый скрин <b>{int(idx_raw)+1}</b> (фото):",
            reply_markup=kb_dm_back_cancel_inline(back_cb=f"dm_edit:screens:{form_id_raw}", cancel_cb="dm_edit:cancel"),
        )


@router.callback_query(F.data.startswith("dm_edit:screen_add:"))
async def dm_edit_screen_add_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await cq.answer()
    form_id = int((cq.data or "").split(":")[-1])
    form = await get_form(session, form_id)
    if not form:
        await state.clear()
        return
    collected = list(form.screenshots or [])
    await state.set_state(DropManagerEditStates.screenshots)
    await state.update_data(form_id=form.id, expected_screens=None, collected_screens=collected)
    if cq.message:
        await _cleanup_edit_preview(bot=cq.bot, chat_id=int(cq.message.chat.id), state=state)
        try:
            await cq.message.delete()
        except Exception:
            pass
        await cq.message.answer(
            "Отправляйте фото/видео/файлы:",
            reply_markup=kb_dm_edit_done_inline(form.id),
        )


@router.callback_query(F.data.startswith("dm_edit:field:"))
async def dm_edit_field_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    if not cq.from_user:
        return
    parts = cq.data.split(":")
    if len(parts) != 4:
        await cq.answer("Некорректная кнопка", show_alert=True)
        return
    _, _, form_id_raw, field = parts
    form_id = int(form_id_raw)

    user = await get_user_by_tg_id(session, cq.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        await cq.answer("Нет прав", show_alert=True)
        return

    form = await get_form(session, form_id)
    if not form or form.manager_id != user.id:
        await cq.answer("Анкета не найдена", show_alert=True)
        await state.clear()
        return

    await cq.answer()
    await state.update_data(form_id=form.id)
    if cq.message:
        await _cleanup_edit_preview(bot=cq.bot, chat_id=int(cq.message.chat.id), state=state)
        try:
            await cq.message.delete()
        except Exception:
            pass

    # Route to existing message handlers by setting state and asking for input
    if field == "traffic_type":
        await state.set_state(DropManagerEditStates.traffic_type)
        if cq.message:
            await _set_edit_prompt_message(
                message=cq.message,
                state=state,
                text="Выберите тип клиента:",
                reply_markup=kb_dm_traffic_type_inline(),
            )
        return

    if field == "phone":
        await state.set_state(DropManagerEditStates.phone)
        if cq.message:
            await _set_edit_prompt_message(
                message=cq.message,
                state=state,
                text="Введите номер (только номер):",
                reply_markup=kb_dm_back_cancel_inline(back_cb=f"dm_edit:back:{form.id}", cancel_cb="dm_edit:cancel"),
            )
        return

    if field == "bank":
        await state.set_state(DropManagerEditStates.bank_select)
        if cq.message:
            try:
                banks = await _list_banks_for_dm_source(session, getattr(user, "manager_source", None))
                bank_items = _dm_bank_items_with_source(banks, getattr(user, "manager_source", None) if user else None)
            except Exception:
                bank_items = []
            await _set_edit_prompt_message(
                message=cq.message,
                state=state,
                text="Выберите банк:",
                reply_markup=kb_dm_edit_bank_select_inline_from_items(form_id=form.id, items=bank_items),
            )
        return

    if field == "password":
        await state.set_state(DropManagerEditStates.password)
        if cq.message:
            await _set_edit_prompt_message(
                message=cq.message,
                state=state,
                text="Введите пароль/инкод:",
                reply_markup=kb_dm_back_cancel_inline(back_cb=f"dm_edit:back:{form.id}", cancel_cb="dm_edit:cancel"),
            )
        return

    if field == "comment":
        await state.set_state(DropManagerEditStates.comment)
        if cq.message:
            await _set_edit_prompt_message(
                message=cq.message,
                state=state,
                text="Введите комментарий:",
                reply_markup=kb_dm_back_cancel_inline(back_cb=f"dm_edit:back:{form.id}", cancel_cb="dm_edit:cancel"),
            )
        return

    if field == "forwards":
        # Keep current behavior: ask to forward messages.
        if form.traffic_type == "REFERRAL":
            await state.set_state(DropManagerEditStates.referral_forward_1)
            if cq.message:
                await _set_edit_prompt_message(
                    message=cq.message,
                    state=state,
                    text="1) Перешлите сообщение клиента:",
                    reply_markup=kb_dm_back_cancel_inline(back_cb=f"dm_edit:back:{form.id}", cancel_cb="dm_edit:cancel"),
                )
        else:
            await state.set_state(DropManagerEditStates.direct_forward)
            if cq.message:
                await _set_edit_prompt_message(
                    message=cq.message,
                    state=state,
                    text="Перешлите сообщение клиента:",
                    reply_markup=kb_dm_back_cancel_inline(back_cb=f"dm_edit:back:{form.id}", cancel_cb="dm_edit:cancel"),
                )
        return

    await cq.answer("Неизвестное поле", show_alert=True)


@router.message(DropManagerEditStates.screenshot_replace, F.photo)
async def dm_edit_screen_replace_photo(message: Message, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    form_id = int(data["form_id"])
    idx = int(data["replace_index"])
    form = await get_form(session, form_id)
    if not form:
        await state.clear()
        return
    shots = list(form.screenshots or [])
    if idx < 0 or idx >= len(shots):
        await state.clear()
        await message.answer("Некорректный номер скрина.")
        return
    shots[idx] = pack_media_item("photo", message.photo[-1].file_id)
    form.screenshots = shots
    manager = await get_user_by_id(session, form.manager_id)
    manager_tag = (manager.manager_tag if manager and manager.manager_tag else "—")
    text = _format_form_text(form, manager_tag)

    if form.status == FormStatus.IN_PROGRESS:
        await state.set_state(DropManagerFormStates.confirm)
        await state.update_data(form_id=form.id)
        await _send_form_preview_with_keyboard(
            bot=message.bot,
            chat_id=int(message.chat.id),
            text=text,
            photos=list(form.screenshots or []),
            reply_markup=kb_form_confirm_with_edit(form.id),
            state=state,
        )
        return

    await state.set_state(DropManagerEditStates.choose_field)
    await _send_form_preview_with_keyboard(
        bot=message.bot,
        chat_id=int(message.chat.id),
        text=text,
        photos=list(form.screenshots or []),
        reply_markup=kb_dm_edit_actions_inline(form.id),
        state=state,
    )


@router.message(DropManagerEditStates.screenshot_replace, F.document)
async def dm_edit_screen_replace_document(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not getattr(message, "document", None):
        return
    data = await state.get_data()
    form_id = int(data["form_id"])
    idx = int(data["replace_index"])
    form = await get_form(session, form_id)
    if not form:
        await state.clear()
        return
    shots = list(form.screenshots or [])
    if idx < 0 or idx >= len(shots):
        await state.clear()
        await message.answer("Некорректный номер скрина.")
        return
    shots[idx] = pack_media_item("doc", message.document.file_id)
    form.screenshots = shots
    await _after_dm_edit_show_form(message=message, session=session, state=state, form=form)


@router.message(DropManagerEditStates.screenshot_replace, F.video)
async def dm_edit_screen_replace_video(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not getattr(message, "video", None):
        return
    data = await state.get_data()
    form_id = int(data["form_id"])
    idx = int(data["replace_index"])
    form = await get_form(session, form_id)
    if not form:
        await state.clear()
        return
    shots = list(form.screenshots or [])
    if idx < 0 or idx >= len(shots):
        await state.clear()
        await message.answer("Некорректный номер скрина.")
        return
    shots[idx] = pack_media_item("video", message.video.file_id)
    form.screenshots = shots
    await _after_dm_edit_show_form(message=message, session=session, state=state, form=form)


@router.message(DropManagerEditStates.screenshot_replace)
async def dm_edit_screen_replace_wrong(message: Message) -> None:
    await message.answer("Нужно отправить <b>фото</b>, <b>видео</b> или <b>файл</b>.")


@router.message(DropManagerEditStates.choose_field, F.text)
async def edit_choose_field(message: Message, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    data = await state.get_data()
    form = await get_form(session, int(data["form_id"]))
    if not form:
        await state.clear()
        return

    user = await get_user_by_tg_id(session, message.from_user.id) if message.from_user else None
    choice = message.text.strip()
    if choice == "Отправить заново":
        if not (getattr(form, "bank_name", None) or "").strip():
            await state.set_state(DropManagerEditStates.bank_select)
            try:
                banks = await _list_banks_for_dm_source(session, getattr(user, "manager_source", None) if user else None)
                bank_items = _dm_bank_items_with_source(banks, getattr(user, "manager_source", None) if user else None)
            except Exception:
                bank_items = []
            await _set_edit_prompt_message(
                message=message,
                state=state,
                text="Выберите банк:",
                reply_markup=kb_dm_edit_bank_select_inline_from_items(form_id=form.id, items=bank_items),
            )
            return
        form.status = FormStatus.PENDING
        form.team_lead_comment = None
        await state.clear()
        user = await get_user_by_tg_id(session, message.from_user.id)
        shift = await get_active_shift(session, user.id) if user else None
        await message.answer(
            "✅ Отправлено тим‑лиду на повторную проверку.",
            reply_markup=await _build_dm_main_kb(
                session=session,
                user_id=int(form.manager_id),
                shift_active=bool(shift),
            ),
        )
        # notify TL
        await _notify_team_leads_new_form(message.bot, settings, session, form, user.manager_tag or "—")
        return

    if choice == "Тип клиента":
        await state.set_state(DropManagerEditStates.traffic_type)
        await _set_edit_prompt_message(
            message=message,
            state=state,
            text="Выберите тип клиента:",
            reply_markup=kb_dm_traffic_type_inline(),
        )
        return
    if choice == "Форварды":
        if form.traffic_type == "REFERRAL":
            await state.set_state(DropManagerEditStates.referral_forward_1)
            await _set_edit_prompt_message(
                message=message,
                state=state,
                text="1) Перешлите сообщение клиента:",
                reply_markup=kb_dm_back_cancel_inline(back_cb=f"dm_edit:back:{form.id}"),
            )
        else:
            await state.set_state(DropManagerEditStates.direct_forward)
            await _set_edit_prompt_message(
                message=message,
                state=state,
                text="Перешлите сообщение клиента:",
                reply_markup=kb_dm_back_cancel_inline(back_cb=f"dm_edit:back:{form.id}"),
            )
        return
    if choice == "Номер":
        await state.set_state(DropManagerEditStates.phone)
        await _set_edit_prompt_message(
            message=message,
            state=state,
            text="Введите номер (только номер):",
            reply_markup=kb_dm_back_cancel_inline(back_cb=f"dm_edit:back:{form.id}"),
        )
        return
    if choice == "Банк":
        await state.set_state(DropManagerEditStates.bank_select)
        user = await get_user_by_id(session, form.manager_id)
        banks = await _list_banks_for_dm_source(session, getattr(user, "manager_source", None) if user else None)
        bank_items = _dm_bank_items_with_source(banks, getattr(user, "manager_source", None) if user else None)
        await _set_edit_prompt_message(
            message=message,
            state=state,
            text="Выберите банк:",
            reply_markup=kb_dm_edit_bank_select_inline_from_items(form_id=form.id, items=bank_items),
        )
        return
    if choice == "Пароль":
        await state.set_state(DropManagerEditStates.password)
        await _set_edit_prompt_message(
            message=message,
            state=state,
            text="Введите пароль/инкод:",
            reply_markup=kb_dm_back_cancel_inline(back_cb=f"dm_edit:back:{form.id}"),
        )
        return
    if choice == "Скрины":
        bank = await get_bank_by_name(session, form.bank_name or "")
        user = await get_user_by_id(session, form.manager_id)
        src = (getattr(user, "manager_source", None) or "").upper() if user else ""
        required = None
        if bank:
            if src == "FB":
                required = getattr(bank, "required_screens_fb", None)
            elif src == "TG":
                required = getattr(bank, "required_screens_tg", None)
            if required is None:
                required = getattr(bank, "required_screens", None)
        await state.update_data(expected_screens=required, collected_screens=[])
        await state.set_state(DropManagerEditStates.screenshots)
        if required and required > 0:
            await _set_edit_prompt_message(
                message=message,
                state=state,
                text=f"Нужно <b>{required}</b> фото/видео/файлов. Отправьте скрин 1/{required}:",
                reply_markup=kb_dm_edit_done_inline(form.id),
            )
        else:
            await _set_edit_prompt_message(
                message=message,
                state=state,
                text="Отправляйте фото/видео/файлы:",
                reply_markup=kb_dm_edit_done_inline(form.id),
            )
        return
    if choice == "Комментарий":
        await state.set_state(DropManagerEditStates.comment)
        await _set_edit_prompt_message(
            message=message,
            state=state,
            text="Введите комментарий:",
            reply_markup=kb_dm_back_cancel_inline(back_cb=f"dm_edit:back:{form.id}"),
        )
        return

    await message.answer("Выберите пункт кнопкой.")


@router.message(DropManagerEditStates.traffic_type, F.text)
async def edit_traffic(message: Message, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    form = await get_form(session, int(data["form_id"]))
    if not form:
        await state.clear()
        return
    if message.text in {"Прямой", "Прямой трафик"}:
        form.traffic_type = "DIRECT"
        form.referral_user = None
        await _after_dm_edit_show_form(message=message, session=session, state=state, form=form)
        return
    if message.text == "Сарафан":
        form.traffic_type = "REFERRAL"
        await _after_dm_edit_show_form(message=message, session=session, state=state, form=form)
        return
    await message.answer("Выберите кнопкой.")


@router.message(DropManagerEditStates.direct_forward)
async def edit_direct_forward(message: Message, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    form = await get_form(session, int(data["form_id"]))
    if not form:
        await state.clear()
        return
    payload = extract_forward_payload(message)
    payload = await _enrich_forward_payload_tg_id(bot=message.bot, payload=payload)
    form.direct_user = payload
    await _continue_after_forward_capture_edit(
        message=message,
        session=session,
        state=state,
        form=form,
        payload_field="direct_user",
    )


@router.message(DropManagerEditStates.referral_forward_1)
async def edit_ref_forward_1(message: Message, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    form = await get_form(session, int(data["form_id"]))
    if not form:
        await state.clear()
        return
    payload = extract_forward_payload(message)
    payload = await _enrich_forward_payload_tg_id(bot=message.bot, payload=payload)
    form.direct_user = payload
    await _continue_after_forward_capture_edit(
        message=message,
        session=session,
        state=state,
        form=form,
        payload_field="direct_user",
        next_state=DropManagerEditStates.referral_forward_2,
        next_prompt_text="2) Перешлите сообщение того, кто привёл:",
        next_reply_markup=None,
    )


@router.message(DropManagerEditStates.referral_forward_2)
async def edit_ref_forward_2(message: Message, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    form = await get_form(session, int(data["form_id"]))
    if not form:
        await state.clear()
        return
    payload = extract_forward_payload(message)
    payload = await _enrich_forward_payload_tg_id(bot=message.bot, payload=payload)
    form.referral_user = payload
    await _continue_after_forward_capture_edit(
        message=message,
        session=session,
        state=state,
        form=form,
        payload_field="referral_user",
    )


@router.message(DropManagerEditStates.forward_manual_username, F.text)
async def edit_forward_manual_username(message: Message, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    form = await get_form(session, int(data.get("form_id") or 0))
    if not form:
        await state.clear()
        return
    payload_field = str(data.get("forward_payload_field") or "direct_user")
    payload = dict(getattr(form, payload_field) or {})
    raw = (message.text or "").strip()
    if raw == ".":
        payload["username"] = "."
    elif raw:
        payload["username"] = raw.lstrip("@").strip()
    payload = await _enrich_forward_payload_tg_id(bot=message.bot, payload=payload)
    setattr(form, payload_field, payload)
    data = await state.get_data()
    await _continue_after_forward_capture_edit(
        message=message,
        session=session,
        state=state,
        form=form,
        payload_field=payload_field,
        next_state=data.get("forward_next_state"),
        next_prompt_text=data.get("forward_next_prompt_text"),
        next_reply_markup=data.get("forward_next_reply_markup"),
    )


@router.message(DropManagerEditStates.forward_manual_phone, F.text)
async def edit_forward_manual_phone(message: Message, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    form = await get_form(session, int(data.get("form_id") or 0))
    if not form:
        await state.clear()
        return
    payload_field = str(data.get("forward_payload_field") or "direct_user")
    payload = dict(getattr(form, payload_field) or {})
    raw = (message.text or "").strip()
    if raw == ".":
        payload["contact_phone"] = "."
    elif raw:
        if not is_valid_phone(raw):
            await message.answer("Некорректный номер. Введите ещё раз или поставьте точку:")
            return
        payload["contact_phone"] = raw
    setattr(form, payload_field, payload)
    data = await state.get_data()
    await _continue_after_forward_capture_edit(
        message=message,
        session=session,
        state=state,
        form=form,
        payload_field=payload_field,
        next_state=data.get("forward_next_state"),
        next_prompt_text=data.get("forward_next_prompt_text"),
        next_reply_markup=data.get("forward_next_reply_markup"),
    )


@router.message(DropManagerEditStates.forward_manual_name, F.text)
async def edit_forward_manual_name(message: Message, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    form = await get_form(session, int(data.get("form_id") or 0))
    if not form:
        await state.clear()
        return
    payload_field = str(data.get("forward_payload_field") or "direct_user")
    payload = dict(getattr(form, payload_field) or {})
    raw = (message.text or "").strip()
    if raw == ".":
        payload["sender_user_name"] = "."
    elif raw:
        payload["sender_user_name"] = raw
    setattr(form, payload_field, payload)
    data = await state.get_data()
    next_state = data.get("forward_next_state")
    next_prompt_text = data.get("forward_next_prompt_text")
    next_reply_markup = data.get("forward_next_reply_markup")
    if next_state:
        await state.set_state(next_state)
        await message.answer(str(next_prompt_text or ""), reply_markup=next_reply_markup)
        return
    await _after_dm_edit_show_form(message=message, session=session, state=state, form=form)


@router.message(DropManagerEditStates.phone, F.text)
async def edit_phone(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not is_valid_phone(message.text):
        await message.answer("Только номер. Пример: <code>944567892</code> или <code>+380991112233</code>")
        return
    data = await state.get_data()
    form = await get_form(session, int(data["form_id"]))
    if not form:
        await state.clear()
        return
    form.phone = normalize_phone(message.text)
    await _after_dm_edit_show_form(message=message, session=session, state=state, form=form)


@router.message(DropManagerEditStates.bank_select, F.text)
async def edit_bank_select(message: Message, session: AsyncSession, state: FSMContext) -> None:
    await message.answer("Выберите банк кнопкой.")


@router.message(DropManagerEditStates.bank_custom, F.text)
async def edit_bank_custom(message: Message, session: AsyncSession, state: FSMContext) -> None:
    name = message.text.strip()
    if not name or len(name) > 64:
        await message.answer("Введите корректное название банка:")
        return
    data = await state.get_data()
    form = await get_form(session, int(data["form_id"]))
    if not form:
        await state.clear()
        return
    form.bank_name = name
    if not await get_bank_by_name(session, name):
        await create_bank(session, name)
    await _after_dm_edit_show_form(message=message, session=session, state=state, form=form)


@router.callback_query(F.data.startswith("dm_edit:bank_pick_id:"))
async def dm_edit_bank_pick_id_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    if not cq.from_user:
        return
    parts = (cq.data or "").split(":")
    if len(parts) != 5:
        await cq.answer("Некорректная кнопка", show_alert=True)
        return
    try:
        form_id = int(parts[3])
        bank_id = int(parts[4])
    except Exception:
        await cq.answer("Некорректная кнопка", show_alert=True)
        return

    user = await get_user_by_tg_id(session, cq.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        await cq.answer("Нет прав", show_alert=True)
        return
    form = await get_form(session, form_id)
    if not form or form.manager_id != user.id:
        await cq.answer("Анкета не найдена", show_alert=True)
        return

    bank = await get_bank(session, bank_id)
    if not bank:
        await cq.answer("Некорректный банк", show_alert=True)
        return
    src = (getattr(user, "manager_source", None) or "TG").upper()
    allowed = (
        ((getattr(bank, "instructions_fb", None) or "").strip() or getattr(bank, "required_screens_fb", None) is not None)
        if src == "FB"
        else ((getattr(bank, "instructions_tg", None) or "").strip() or getattr(bank, "required_screens_tg", None) is not None)
    )
    if not allowed:
        await cq.answer("Банк недоступен для вашего источника", show_alert=True)
        return

    form.bank_name = str(bank.name)
    await cq.answer("Сохранено")

    await state.set_state(DropManagerEditStates.choose_field)
    await state.update_data(form_id=form.id)
    manager_tag = user.manager_tag or "—"
    text = _format_form_text(form, manager_tag)
    photos = list(form.screenshots or [])
    await _send_form_preview_with_keyboard(
        bot=cq.bot,
        chat_id=int(cq.message.chat.id) if cq.message else int(cq.from_user.id),
        text=text,
        photos=photos,
        reply_markup=kb_dm_edit_actions_inline(form.id),
        state=state,
    )


@router.callback_query(F.data.startswith("dm_edit:bank_pick:"))
async def dm_edit_bank_pick_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    if not cq.from_user:
        return
    parts = (cq.data or "").split(":", 3)
    if len(parts) != 4:
        await cq.answer("Некорректная кнопка", show_alert=True)
        return
    _, _, form_id_raw, bank_name = parts
    try:
        form_id = int(form_id_raw)
    except Exception:
        await cq.answer("Некорректная кнопка", show_alert=True)
        return

    user = await get_user_by_tg_id(session, cq.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        await cq.answer("Нет прав", show_alert=True)
        return
    form = await get_form(session, form_id)
    if not form or form.manager_id != user.id:
        await cq.answer("Анкета не найдена", show_alert=True)
        return

    if not await get_bank_by_name(session, bank_name):
        await cq.answer("Некорректный банк", show_alert=True)
        return

    form.bank_name = bank_name
    await cq.answer("Сохранено")

    await state.set_state(DropManagerEditStates.choose_field)
    await state.update_data(form_id=form.id)
    manager_tag = user.manager_tag or "—"
    text = _format_form_text(form, manager_tag)
    photos = list(form.screenshots or [])
    await _send_form_preview_with_keyboard(
        bot=cq.bot,
        chat_id=int(cq.message.chat.id) if cq.message else int(cq.from_user.id),
        text=text,
        photos=photos,
        reply_markup=kb_dm_edit_actions_inline(form.id),
        state=state,
    )


@router.callback_query(F.data.startswith("dm_edit:bank_custom:"))
async def dm_edit_bank_custom_prompt_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await cq.answer("Ручной ввод банка отключён", show_alert=True)


@router.message(DropManagerEditStates.password, F.text)
async def edit_password(message: Message, session: AsyncSession, state: FSMContext) -> None:
    pwd = (message.text or "").strip()
    if not pwd:
        await message.answer("Введите пароль/инкод:")
        return
    data = await state.get_data()
    form = await get_form(session, int(data["form_id"]))
    if not form:
        await state.clear()
        return
    form.password = pwd
    await _after_dm_edit_show_form(message=message, session=session, state=state, form=form)


@router.message(DropManagerEditStates.screenshots, F.photo)
async def edit_screens_add(message: Message, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    expected = data.get("expected_screens")
    collected: list[str] = list(data.get("collected_screens") or [])
    media_group_id = getattr(message, "media_group_id", None)
    if media_group_id:
        key = (int(message.chat.id), str(media_group_id))
        _album_counts[key] = _album_counts.get(key, 0) + 1
    if len(collected) < 20:
        collected.append(pack_media_item("photo", message.photo[-1].file_id))
    await state.update_data(collected_screens=collected)

    if expected and expected > 0:
        if len(collected) < expected:
            if media_group_id:
                _schedule_album_ack(
                    bot=message.bot,
                    chat_id=int(message.chat.id),
                    media_group_id=str(media_group_id),
                    accepted_total=len(collected),
                    expected=int(expected),
                    reply_markup=kb_dm_edit_done_inline(int(data["form_id"])),
                )
                return
            await _upsert_prompt_message(
                message=message,
                state=state,
                state_key="edit_screens_prompt_msg_id",
                text=f"Отправьте скрин {len(collected)+1}/{expected}:",
                reply_markup=kb_dm_edit_done_inline(int(data["form_id"])),
            )
            return
        form = await get_form(session, int(data["form_id"]))
        if not form:
            await state.clear()
            return
        form.screenshots = collected
        await state.set_state(DropManagerEditStates.choose_field)
        try:
            manager = await get_user_by_id(session, form.manager_id)
            manager_tag = (manager.manager_tag if manager and manager.manager_tag else "—")
            text = _format_form_text(form, manager_tag)
            await message.answer(text, reply_markup=kb_dm_edit_actions_inline(form.id))
        except TelegramNetworkError:
            return


@router.message(DropManagerEditStates.screenshots, F.document)
async def edit_screens_add_document(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not getattr(message, "document", None):
        return
    data = await state.get_data()
    expected = data.get("expected_screens")
    collected: list[str] = list(data.get("collected_screens") or [])
    media_group_id = getattr(message, "media_group_id", None)
    if media_group_id:
        key = (int(message.chat.id), str(media_group_id))
        _album_counts[key] = _album_counts.get(key, 0) + 1
    if len(collected) < 20:
        collected.append(pack_media_item("doc", message.document.file_id))
    await state.update_data(collected_screens=collected)

    if expected and expected > 0:
        if len(collected) < expected:
            if media_group_id:
                _schedule_album_ack(
                    bot=message.bot,
                    chat_id=int(message.chat.id),
                    media_group_id=str(media_group_id),
                    accepted_total=len(collected),
                    expected=int(expected),
                    reply_markup=kb_dm_edit_done_inline(int(data["form_id"])),
                )
                return
            await _upsert_prompt_message(
                message=message,
                state=state,
                state_key="edit_screens_prompt_msg_id",
                text=f"Отправьте скрин {len(collected)+1}/{expected}:",
                reply_markup=kb_dm_edit_done_inline(int(data["form_id"])),
            )
            return
        form = await get_form(session, int(data["form_id"]))
        if not form:
            await state.clear()
            return
        form.screenshots = collected
        await state.set_state(DropManagerEditStates.choose_field)
        try:
            manager = await get_user_by_id(session, form.manager_id)
            manager_tag = (manager.manager_tag if manager and manager.manager_tag else "—")
            text = _format_form_text(form, manager_tag)
            await message.answer(text, reply_markup=kb_dm_edit_actions_inline(form.id))
        except TelegramNetworkError:
            return
        return

    if media_group_id:
        _schedule_album_ack(
            bot=message.bot,
            chat_id=int(message.chat.id),
            media_group_id=str(media_group_id),
            accepted_total=len(collected),
            expected=None,
            reply_markup=kb_dm_edit_done_inline(int(data["form_id"])),
        )

    await _upsert_prompt_message(
        message=message,
        state=state,
        state_key="edit_screens_prompt_msg_id",
        text=f"✅ Скрин {len(collected)} принят. Отправляйте ещё или нажмите <b>Готово</b>.",
        reply_markup=kb_dm_edit_done_inline(int(data["form_id"])),
    )


@router.message(DropManagerEditStates.screenshots, F.video)
async def edit_screens_add_video(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not getattr(message, "video", None):
        return
    data = await state.get_data()
    expected = data.get("expected_screens")
    collected: list[str] = list(data.get("collected_screens") or [])
    media_group_id = getattr(message, "media_group_id", None)
    if media_group_id:
        key = (int(message.chat.id), str(media_group_id))
        _album_counts[key] = _album_counts.get(key, 0) + 1
    if len(collected) < 20:
        collected.append(pack_media_item("video", message.video.file_id))
    await state.update_data(collected_screens=collected)

    if expected and expected > 0:
        if len(collected) < expected:
            if media_group_id:
                _schedule_album_ack(
                    bot=message.bot,
                    chat_id=int(message.chat.id),
                    media_group_id=str(media_group_id),
                    accepted_total=len(collected),
                    expected=int(expected),
                    reply_markup=kb_dm_edit_done_inline(int(data["form_id"])),
                )
                return
            await _upsert_prompt_message(
                message=message,
                state=state,
                state_key="edit_screens_prompt_msg_id",
                text=f"Отправьте скрин {len(collected)+1}/{expected}:",
                reply_markup=kb_dm_edit_done_inline(int(data["form_id"])),
            )
            return
        form = await get_form(session, int(data["form_id"]))
        if not form:
            await state.clear()
            return
        form.screenshots = collected
        await state.set_state(DropManagerEditStates.choose_field)
        try:
            manager = await get_user_by_id(session, form.manager_id)
            manager_tag = (manager.manager_tag if manager and manager.manager_tag else "—")
            text = _format_form_text(form, manager_tag)
            await message.answer(text, reply_markup=kb_dm_edit_actions_inline(form.id))
        except TelegramNetworkError:
            return
        return

    if media_group_id:
        _schedule_album_ack(
            bot=message.bot,
            chat_id=int(message.chat.id),
            media_group_id=str(media_group_id),
            accepted_total=len(collected),
            expected=None,
            reply_markup=kb_dm_edit_done_inline(int(data["form_id"])),
        )

    await _upsert_prompt_message(
        message=message,
        state=state,
        state_key="edit_screens_prompt_msg_id",
        text=f"✅ Скрин {len(collected)} принят. Отправляйте ещё или нажмите <b>Готово</b>.",
        reply_markup=kb_dm_edit_done_inline(int(data["form_id"])),
    )
    return


@router.message(DropManagerEditStates.comment, F.text)
async def edit_comment(message: Message, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    form = await get_form(session, int(data["form_id"]))
    if not form:
        await state.clear()
        return
    form.comment = message.text.strip()
    await _after_dm_edit_show_form(message=message, session=session, state=state, form=form)


# Back navigation handlers for form creation
@router.message(DropManagerFormStates.traffic_type, F.text == "Назад")
async def form_back_to_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Создание анкеты отменено.")

@router.message(DropManagerFormStates.direct_forward, F.text == "Назад")
async def form_back_to_traffic_type(message: Message, state: FSMContext) -> None:
    await state.set_state(DropManagerFormStates.traffic_type)
    await message.answer("Выберите тип клиента:", reply_markup=kb_dm_traffic_type_inline())

@router.message(DropManagerFormStates.referral_forward_1, F.text == "Назад")
async def form_back_to_traffic_type_from_ref1(message: Message, state: FSMContext) -> None:
    await state.set_state(DropManagerFormStates.traffic_type)
    await message.answer("Выберите тип клиента:", reply_markup=kb_dm_traffic_type_inline())

@router.message(DropManagerFormStates.referral_forward_2, F.text == "Назад")
async def form_back_to_referral_forward_1(message: Message, state: FSMContext) -> None:
    await state.set_state(DropManagerFormStates.referral_forward_1)
    await message.answer(
        "1) Перешлите сообщение клиента (на кого анкета):",
        reply_markup=kb_dm_back_cancel_inline(back_cb="dm:back_to_traffic"),
    )

@router.message(DropManagerFormStates.phone, F.text == "Назад")
async def form_back_to_previous_forward(message: Message, session: AsyncSession, state: FSMContext) -> None:
    data = await state.get_data()
    form = await get_form(session, int(data["form_id"]))
    if not form:
        await state.clear()
        return
    
    if form.traffic_type == "DIRECT":
        await state.set_state(DropManagerFormStates.direct_forward)
        await message.answer(
            "Перешлите сообщение клиента (форвард):",
            reply_markup=kb_dm_back_cancel_inline(back_cb="dm:back_to_traffic"),
        )
    elif form.traffic_type == "REFERRAL":
        await state.set_state(DropManagerFormStates.referral_forward_2)
        await message.answer(
            "2) Перешлите сообщение того, кто привёл:",
            reply_markup=kb_dm_back_cancel_inline(back_cb="dm:back_to_ref1"),
        )

@router.message(DropManagerFormStates.bank_select, F.text == "Назад")
async def form_back_to_phone(message: Message, state: FSMContext) -> None:
    await state.set_state(DropManagerFormStates.phone)
    await message.answer(
        "Напишите номер и перешлите его в бота.\n"
        "Сообщение должно содержать <b>только номер</b> и ничего больше:",
        reply_markup=kb_dm_back_cancel_inline(back_cb="dm:back_to_forward")
    )

@router.message(DropManagerFormStates.bank_custom, F.text == "Назад")
async def form_back_to_bank_select(message: Message, session: AsyncSession, state: FSMContext) -> None:
    await state.set_state(DropManagerFormStates.bank_select)
    try:
        user = await get_user_by_tg_id(session, message.from_user.id) if message.from_user else None
        banks = await _list_banks_for_dm_source(session, getattr(user, "manager_source", None) if user else None)
        bank_items = _dm_bank_items_with_source(banks, getattr(user, "manager_source", None) if user else None)
    except Exception:
        bank_items = []
    await message.answer("Выберите банк:", reply_markup=kb_dm_bank_select_inline_from_items(bank_items))

@router.message(DropManagerFormStates.password, F.text == "Назад")
async def form_back_to_bank_select_from_password(message: Message, session: AsyncSession, state: FSMContext) -> None:
    await state.set_state(DropManagerFormStates.bank_select)
    try:
        user = await get_user_by_tg_id(session, message.from_user.id) if message.from_user else None
        banks = await _list_banks_for_dm_source(session, getattr(user, "manager_source", None) if user else None)
        bank_items = _dm_bank_items_with_source(banks, getattr(user, "manager_source", None) if user else None)
    except Exception:
        bank_items = []
    await message.answer("Выберите банк:", reply_markup=kb_dm_bank_select_inline_from_items(bank_items))

@router.message(DropManagerFormStates.screenshots, F.text == "Назад")
async def form_back_to_password(message: Message, state: FSMContext) -> None:
    await state.set_state(DropManagerFormStates.password)
    await message.answer(
        "Введите пароль/инкод:",
        reply_markup=kb_dm_back_cancel_inline(back_cb="dm:back_to_bank_select"),
    )

@router.message(DropManagerFormStates.comment, F.text == "Назад")
async def form_back_to_screenshots(message: Message, state: FSMContext) -> None:
    await state.set_state(DropManagerFormStates.screenshots)
    await message.answer("Отправляйте фото/видео/файлы:", reply_markup=kb_dm_done_inline())

@router.message(DropManagerFormStates.confirm, F.text == "Назад")
async def form_back_to_comment(message: Message, state: FSMContext) -> None:
    await state.set_state(DropManagerFormStates.comment)
    await message.answer(
        "Добавьте комментарий (необязательно):",
        reply_markup=kb_dm_back_cancel_inline(back_cb="dm:back_to_screens"),
    )


# Fallback: capture manager tag (must be last to avoid intercepting other text handlers)
@router.message(StateFilter(None), F.text)
async def capture_manager_tag_if_needed(message: Message, session: AsyncSession) -> None:
    if not message.from_user or not message.text:
        return
    if message.text.startswith("/"):
        return
    user = await get_user_by_tg_id(session, message.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        return
    if user.manager_tag:
        return
    tag = message.text.strip()
    if not tag or len(tag) > 64:
        await message.answer("Тег слишком длинный/пустой. Введите тег ещё раз:")
        return
    user.manager_tag = tag
    shift = await get_active_shift(session, user.id)
    # Ask source/category once
    if not user.manager_source:
        await message.answer(
            f"✅ Сохранено. Вы Дроп‑Менеджер — ваш никнейм <b>{user.manager_tag}</b>.\n\nВыберите источник:",
            reply_markup=kb_dm_source_pick_inline(),
        )
        return
    await message.answer(
        f"✅ Сохранено. Вы Дроп‑Менеджер — ваш никнейм <b>{user.manager_tag}</b>.",
        reply_markup=await _build_dm_main_kb(
            session=session,
            user_id=int(user.id),
            shift_active=bool(shift),
        ),
    )


@router.callback_query(F.data.startswith("dm:src:"))
async def dm_pick_source_cb(cq: CallbackQuery, session: AsyncSession) -> None:
    if not cq.from_user:
        return
    src = cq.data.split(":")[-1].upper()
    if src not in {"TG", "FB"}:
        await cq.answer("Некорректный источник", show_alert=True)
        return
    user = await get_user_by_tg_id(session, cq.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        await cq.answer("Нет прав", show_alert=True)
        return
    user.manager_source = src
    shift = await get_active_shift(session, user.id)
    await cq.answer("Сохранено")
    if cq.message:
        await cq.message.edit_text(
            f"✅ Сохранено. Источник: <b>{src}</b>\n\nГлавное меню:",
            reply_markup=await _build_dm_main_kb(
                session=session,
                user_id=int(user.id),
                shift_active=bool(shift),
            ),
        )


def _format_rejected_form_summary(form) -> str:
    """Format a form summary for the rejected list."""
    traffic = "Прямой" if form.traffic_type == "DIRECT" else "Сарафан" if form.traffic_type == "REFERRAL" else "—"
    
    bank = format_bank_hashtag(getattr(form, "bank_name", None))
    return (
        f"❌ <b>ID: {form.id}</b>\n"
        f"   🏦 Банк: {bank}\n"
        f"   📊 Тип клиента: {traffic}\n"
        f"   📞 Телефон: {form.phone or '—'}\n"
        f"   📅 Создана: {form.created_at.strftime('%d.%m.%Y %H:%M')}\n"
        f"   💬 Причина: {form.team_lead_comment or '—'}"
    )


def _format_rejected_form_details(form) -> str:
    """Format detailed form information for rejected form."""
    traffic = "Прямой" if form.traffic_type == "DIRECT" else "Сарафан" if form.traffic_type == "REFERRAL" else "—"
    
    bank = format_bank_hashtag(getattr(form, "bank_name", None))
    details = (
        f"❌ <b>ОТКЛОНЕННАЯ АНКЕТА #{form.id}</b>\n\n"
        f"📊 <b>Информация:</b>\n"
        f"Тип клиента: <b>{traffic}</b>\n"
        f"Банк: <b>{bank}</b>\n"
        f"Телефон: <code>{form.phone or '—'}</code>\n"
        f"Пароль: <code>{form.password or '—'}</code>\n"
        f"Скриншоты: <b>{len(form.screenshots or [])}</b> шт.\n"
        f"Комментарий: {form.comment or '—'}\n\n"
        f"📅 <b>Даты:</b>\n"
        f"Создана: {form.created_at.strftime('%d.%m.%Y %H:%M')}\n"
        f"Обновлена: {form.updated_at.strftime('%d.%m.%Y %H:%M')}\n\n"
        f"💬 <b>Комментарий тим-лида:</b>\n{form.team_lead_comment or '—'}"
    )
    
    return details


@router.message(F.text == "Неапрувнутые анкеты")
async def rejected_forms_start(message: Message, session: AsyncSession, state: FSMContext) -> None:
    # Ignore group messages
    if message.chat.type in ['group', 'supergroup']:
        return
    if not message.from_user:
        return
    
    user = await get_user_by_tg_id(session, message.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        return
    
    await state.clear()
    await dm_rejected_cb(message, session, state)


@router.callback_query(F.data == "dm:rejected")
async def dm_rejected_cb(cq_or_msg: Message | CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    u = cq_or_msg.from_user if isinstance(cq_or_msg, CallbackQuery) else cq_or_msg.from_user
    if not u:
        return
    user = await get_user_by_tg_id(session, u.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        return

    rejected_forms = await list_rejected_forms_by_user_id(session, user.id)
    if not rejected_forms:
        text = "У вас нет неапрувнутых анкет"
        if isinstance(cq_or_msg, CallbackQuery):
            await cq_or_msg.answer(text, show_alert=True)
            if cq_or_msg.message:
                try:
                    await cq_or_msg.message.answer(text, reply_markup=kb_dm_back_to_menu_inline())
                except Exception:
                    pass
                try:
                    await cq_or_msg.message.delete()
                except Exception:
                    pass
        else:
            await cq_or_msg.answer(text, reply_markup=kb_dm_back_to_menu_inline())
        return

    await state.clear()
    await state.set_state(DropManagerRejectedStates.view_list)
    await state.update_data(forms=[{"id": int(f.id)} for f in rejected_forms])

    header = f"❌ <b>Отклоненные анкеты</b>\n\nВсего: <b>{len(rejected_forms)}</b>\n\nВыберите анкету:"
    kb = _kb_dm_rejected_list_inline(rejected_forms).as_markup()

    if isinstance(cq_or_msg, CallbackQuery):
        await cq_or_msg.answer()
        if cq_or_msg.message:
            try:
                await cq_or_msg.message.answer(header, reply_markup=kb)
            except Exception:
                pass
            try:
                await cq_or_msg.message.delete()
            except Exception:
                pass
    else:
        await cq_or_msg.answer(header, reply_markup=kb)


@router.callback_query(F.data.startswith("dm:rej:"))
async def dm_rejected_open_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    if not cq.from_user:
        return
    user = await get_user_by_tg_id(session, cq.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        await cq.answer("Нет прав", show_alert=True)
        return

    form_id = int(cq.data.split(":")[-1])
    form = await get_form(session, form_id)
    if not form or form.manager_id != user.id or form.status != FormStatus.REJECTED:
        await cq.answer("Анкета не найдена", show_alert=True)
        return

    await cq.answer()
    await state.clear()
    await edit_open(cq, FormEditCb(action="open", form_id=form_id), session, state)





@router.callback_query(F.data.startswith("drop_edit_form:"))
async def edit_rejected_form(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    if not cq.from_user:
        return
    
    form_id = int((cq.data or "").split(":")[-1])
    form = await get_form(session, form_id)
    
    if not form:
        await cq.answer("Анкета не найдена", show_alert=True)
        return
    
    user = await get_user_by_tg_id(session, cq.from_user.id)
    if not user or form.manager_id != user.id:
        await cq.answer("Нет прав", show_alert=True)
        return
    
    await cq.answer()
    await state.clear()
    
    # Перенаправляем в существующую систему редактирования
    await state.set_state(DropManagerEditStates.choose_field)
    await state.update_data(form_id=form_id)
    
    if cq.message:
        await cq.message.edit_text("Выберите что хотите изменить:", reply_markup=kb_dm_edit_actions_inline(form_id))


def _pool_type_ru(t: str) -> str:
    return {
        "link": "Ссылка",
        "esim": "Esim",
        "link_esim": "Ссылка + Esim",
    }.get((t or "").lower(), t or "—")


def _free_pool_counts_by_type(items: list) -> dict[str, int]:
    out = {"link": 0, "esim": 0, "link_esim": 0}
    for x in items:
        t = str(getattr(getattr(x, "type", None), "value", "") or "").lower()
        if t in out:
            out[t] += 1
    return out


@router.callback_query(F.data == "dm:resource_menu")
async def dm_resource_menu(cq: CallbackQuery, session: AsyncSession) -> None:
    if not cq.from_user:
        return
    user = await get_user_by_tg_id(session, cq.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        return
    await cq.answer()
    if cq.message:
        await _safe_edit_message(message=cq.message, text="<b>Запрос ссылки</b>", reply_markup=kb_dm_resource_menu())


@router.callback_query(F.data == "dm:resource_create_bank")
async def dm_resource_create_bank_stub(cq: CallbackQuery) -> None:
    await cq.answer("Эта кнопка в разработке", show_alert=True)


@router.callback_query(F.data == "dm:resource_banks")
async def dm_resource_banks(cq: CallbackQuery, session: AsyncSession) -> None:
    if not cq.from_user:
        return
    user = await get_user_by_tg_id(session, cq.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        return
    banks = await _list_banks_for_dm_source(session, getattr(user, "manager_source", None))
    source = (getattr(user, "manager_source", None) or "TG")
    items: list[tuple[int, str]] = []
    for b in banks:
        free_items = await list_free_pool_items_for_bank(session, bank_id=int(b.id), source=source)
        total_free = len(free_items)
        if total_free <= 0:
            continue
        items.append((int(b.id), f"{getattr(b, 'name', '—')} ({total_free})"))
    await cq.answer()
    if cq.message:
        if not items:
            await _safe_edit_message(message=cq.message, text="Нет доступных ресурсов для вашего источника", reply_markup=kb_dm_resource_menu())
            return
        await _safe_edit_message(message=cq.message, text="Выберите банк:", reply_markup=kb_dm_resource_banks(items))


@router.callback_query(F.data.startswith("dm:resource_bank:"))
async def dm_resource_bank_open(cq: CallbackQuery, session: AsyncSession) -> None:
    if not cq.from_user:
        return
    user = await get_user_by_tg_id(session, cq.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        return
    bank_id = int((cq.data or "").split(":")[-1])
    bank = await get_bank(session, bank_id)
    if not bank:
        await cq.answer("Банк не найден", show_alert=True)
        return
    free_items = await list_free_pool_items_for_bank(session, bank_id=bank_id, source=(getattr(user, "manager_source", None) or "TG"))
    await cq.answer()
    if cq.message:
        if not free_items:
            await _safe_edit_message(
                message=cq.message,
                text="Для этого банка нету ссылок или есим",
                reply_markup=kb_dm_resource_empty_bank(bank_id),
            )
            return
        counts = _free_pool_counts_by_type(free_items)
        total = sum(counts.values())
        lines = [f"<b>{bank.name}</b>", f"Доступно всего: <b>{total}</b>"]
        for t in ("link", "esim", "link_esim"):
            cnt = int(counts.get(t, 0))
            if cnt > 0:
                lines.append(f"• {_pool_type_ru(t)}: <b>{cnt}</b>")
        await _safe_edit_message(message=cq.message, text="\n".join(lines), reply_markup=kb_dm_resource_bank_actions(bank_id))


@router.callback_query(F.data.startswith("dm:resource_take:"))
async def dm_resource_take(cq: CallbackQuery, session: AsyncSession) -> None:
    if not cq.from_user:
        return
    user = await get_user_by_tg_id(session, cq.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        return
    bank_id = int((cq.data or "").split(":")[-1])
    active_cnt_bank = await count_dm_active_pool_items_for_bank(session, dm_user_id=int(user.id), bank_id=bank_id)
    if active_cnt_bank >= 5:
        await cq.answer("Лимит 5 активных ресурсов на этот банк уже достигнут", show_alert=True)
        await dm_resource_active(cq, session)
        return
    free_items = await list_free_pool_items_for_bank(session, bank_id=bank_id, source=(getattr(user, "manager_source", None) or "TG"))
    counts = _free_pool_counts_by_type(free_items)
    available_types = [t for t in ("esim", "link", "link_esim") if int(counts.get(t, 0)) > 0]
    await cq.answer()
    if cq.message:
        if not available_types:
            await _safe_edit_message(message=cq.message, text="В этом банке сейчас нет доступных ресурсов", reply_markup=kb_dm_resource_empty_bank(bank_id))
            return
        await _safe_edit_message(message=cq.message, text="Что взять?", reply_markup=kb_dm_resource_type_pick(bank_id, available_types))


@router.callback_query(F.data.startswith("dm:resource_take_type:"))
async def dm_resource_take_type(cq: CallbackQuery, session: AsyncSession) -> None:
    if not cq.from_user:
        return
    user = await get_user_by_tg_id(session, cq.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        return
    parts = (cq.data or "").split(":")
    if len(parts) != 4:
        await cq.answer("Некорректная кнопка", show_alert=True)
        return
    try:
        bank_id = int(parts[2])
    except Exception:
        await cq.answer("Некорректная кнопка", show_alert=True)
        return
    rtype = (parts[3] or "").strip().lower()
    if rtype not in {"link", "esim", "link_esim"}:
        await cq.answer("Некорректный тип", show_alert=True)
        return
    active_cnt_bank = await count_dm_active_pool_items_for_bank(session, dm_user_id=int(user.id), bank_id=bank_id)
    if active_cnt_bank >= 5:
        await cq.answer("Лимит 5 активных ресурсов на этот банк уже достигнут", show_alert=True)
        await dm_resource_active(cq, session)
        return
    free_items = await list_free_pool_items_for_bank(session, bank_id=bank_id, source=(getattr(user, "manager_source", None) or "TG"))
    picked = next((x for x in free_items if getattr(getattr(x, 'type', None), 'value', '') == rtype), None)
    if not picked:
        await cq.answer("Нет доступных записей этого типа", show_alert=True)
        return
    assigned = await assign_pool_item_to_dm(session, item_id=int(picked.id), dm_user_id=int(user.id))
    if not assigned:
        await cq.answer("Запись уже занята", show_alert=True)
        return
    bank = await get_bank(session, int(assigned.bank_id))
    txt = (
        f"✅ Взято в работу\n\n"
        f"ID ресурса: <code>{int(assigned.id)}</code>\n"
        f"Банк: <b>{bank.name if bank else '—'}</b>\n"
        f"Тип: <b>{_pool_type_ru(getattr(assigned.type, 'value', ''))}</b>\n"
        f"Данные: <code>{assigned.text_data or '—'}</code>"
    )
    await cq.answer()
    if cq.message:
        shots = list(getattr(assigned, "screenshots", None) or [])
        if shots:
            kind, fid = unpack_media_item(str(shots[0]))
            if kind == "photo":
                await cq.message.answer_photo(fid, caption=txt, parse_mode="HTML", reply_markup=kb_dm_resource_active_actions(int(assigned.id)))
            elif kind == "video":
                await cq.message.answer_video(fid, caption=txt, parse_mode="HTML", reply_markup=kb_dm_resource_active_actions(int(assigned.id)))
            else:
                await cq.message.answer_document(fid, caption=txt, parse_mode="HTML", reply_markup=kb_dm_resource_active_actions(int(assigned.id)))
        else:
            await _safe_edit_message(message=cq.message, text=txt, reply_markup=kb_dm_resource_active_actions(int(assigned.id)))


@router.callback_query(F.data == "dm:resource_active")
async def dm_resource_active(cq: CallbackQuery, session: AsyncSession) -> None:
    if not cq.from_user:
        return
    user = await get_user_by_tg_id(session, cq.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        return
    items = await list_dm_active_pool_items(session, dm_user_id=int(user.id))
    packed: list[tuple[int, str]] = []
    for it in items:
        bank = await get_bank(session, int(it.bank_id))
        packed.append((int(it.id), f"#{int(it.id)} {bank.name if bank else '—'} | {_pool_type_ru(getattr(it.type, 'value', ''))}"))
    await cq.answer()
    if cq.message:
        if not packed:
            await _safe_edit_message(message=cq.message, text="Активных ссылок/Esim нет", reply_markup=kb_dm_resource_menu())
            return
        await _safe_edit_message(message=cq.message, text="Активные ссылки / Esim:", reply_markup=kb_dm_resource_active_list(packed))


@router.callback_query(F.data.startswith("dm:resource_active_open:"))
async def dm_resource_active_open(cq: CallbackQuery, session: AsyncSession) -> None:
    if not cq.from_user:
        return
    user = await get_user_by_tg_id(session, cq.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        return
    item_id = int((cq.data or "").split(":")[-1])
    it = await get_pool_item(session, item_id)
    if not it or int(it.assigned_to_user_id or 0) != int(user.id):
        await cq.answer("Кейс не найден", show_alert=True)
        return
    bank = await get_bank(session, int(it.bank_id))
    txt = (
        f"ID ресурса: <code>{int(it.id)}</code>\n"
        f"Банк: <b>{bank.name if bank else '—'}</b>\n"
        f"Тип: <b>{_pool_type_ru(getattr(it.type, 'value', ''))}</b>\n"
        f"Данные: <code>{it.text_data or '—'}</code>"
    )
    await cq.answer()
    if cq.message:
        shots = list(getattr(it, "screenshots", None) or [])
        if shots:
            kind, fid = unpack_media_item(str(shots[0]))
            if kind == "photo":
                await cq.message.answer_photo(fid, caption=txt, parse_mode="HTML", reply_markup=kb_dm_resource_active_actions(int(it.id)))
            elif kind == "video":
                await cq.message.answer_video(fid, caption=txt, parse_mode="HTML", reply_markup=kb_dm_resource_active_actions(int(it.id)))
            else:
                await cq.message.answer_document(fid, caption=txt, parse_mode="HTML", reply_markup=kb_dm_resource_active_actions(int(it.id)))
        else:
            await _safe_edit_message(message=cq.message, text=txt, reply_markup=kb_dm_resource_active_actions(int(it.id)))


@router.callback_query(F.data.startswith("dm:resource_release:"))
async def dm_resource_release(cq: CallbackQuery, session: AsyncSession) -> None:
    if not cq.from_user:
        return
    user = await get_user_by_tg_id(session, cq.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        return
    item_id = int((cq.data or "").split(":")[-1])
    ok = await release_pool_item(session, item_id=item_id, dm_user_id=int(user.id))
    await cq.answer("Готово" if ok else "Не удалось", show_alert=not ok)
    if cq.message and ok:
        await _safe_edit_message(message=cq.message, text=f"Ресурс <code>#{item_id}</code> отправлен в общий пул. Если ошиблись — возьмите его снова.", reply_markup=kb_dm_resource_menu())


@router.callback_query(F.data.startswith("dm:resource_invalid:"))
async def dm_resource_invalid_start(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    if not cq.from_user:
        return
    user = await get_user_by_tg_id(session, cq.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        await cq.answer("Нет прав", show_alert=True)
        return
    item_id = int((cq.data or "").split(":")[-1])
    it = await get_pool_item(session, item_id)
    if not it or int(it.assigned_to_user_id or 0) != int(user.id) or str(getattr(it.status, "value", "")) != "assigned":
        await cq.answer("Ресурс недоступен", show_alert=True)
        return
    await state.set_state(DropManagerResourceStates.invalid_comment)
    await state.update_data(resource_item_id=item_id)
    await cq.answer()
    if cq.message:
        await cq.message.answer(
            f"Введите комментарий для ресурса ID <code>{item_id}</code>",
            reply_markup=kb_dm_back_cancel_inline(
                back_cb=f"dm:resource_active_open:{int(item_id)}",
                cancel_cb="dm:resource_invalid_cancel",
            ),
        )


@router.callback_query(F.data == "dm:resource_invalid_cancel")
async def dm_resource_invalid_cancel(cq: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cq.answer("Отменено")
    if cq.message:
        await _safe_edit_message(message=cq.message, text="Действие отменено", reply_markup=kb_dm_resource_menu())


@router.message(DropManagerResourceStates.invalid_comment, F.text)
async def dm_resource_invalid_comment(message: Message, session: AsyncSession, state: FSMContext) -> None:
    if not message.from_user:
        return
    user = await get_user_by_tg_id(session, message.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        await message.answer("Нет прав")
        return

    log.info("DM invalid comment received: tg_id=%s text_len=%s", getattr(user, "tg_id", None), len((message.text or "").strip()))

    raw_comment = (message.text or "").strip()
    if not raw_comment:
        await message.answer("Комментарий пустой. Введите текстом одним сообщением.")
        return

    data = await state.get_data()
    item_id_raw = data.get("resource_item_id")
    try:
        item_id = int(item_id_raw or 0)
    except (TypeError, ValueError):
        item_id = 0

    if item_id <= 0:
        log.warning("DM invalid-comment state without resource_item_id: tg_id=%s state=%s", getattr(user, "tg_id", None), data)
        await state.clear()
        await message.answer("Не удалось определить ссылку. Попробуйте снова через 'Активные ссылки / Esim'.", reply_markup=kb_dm_resource_menu())
        return

    it = await mark_pool_item_invalid(session, item_id=item_id, dm_user_id=int(user.id), comment=raw_comment)
    await state.clear()
    log.info("DM invalid mark result: item_id=%s dm_user_id=%s ok=%s", item_id, user.id, bool(it))
    if not it:
        log.warning("mark_pool_item_invalid returned None: item_id=%s dm_user_id=%s", item_id, user.id)
        await message.answer("Не удалось сохранить комментарий. Возможно, ссылка уже недоступна.", reply_markup=kb_dm_resource_menu())
        return

    notified_wictory = False
    sent_tg_ids: set[int] = set()
    safe_comment = html.escape(raw_comment)
    type_ru = _pool_type_ru(str(getattr(getattr(it, "type", None), "value", "") or ""))
    notice_text = (
        "⚠️ Ресурс помечен как невалидный\n"
        f"ID ресурса: <code>{item_id}</code>\n"
        f"Тип: <b>{html.escape(type_ru)}</b>\n"
        f"Комментарий DM: {safe_comment}"
    )

    try:
        owner_id = int(it.created_by_user_id)
        wictory_owner = await get_user_by_id(session, owner_id)
    except Exception:
        wictory_owner = None

    targets: list[User] = []
    if wictory_owner and wictory_owner.role == UserRole.WICTORY:
        targets.append(wictory_owner)

    # fallback: notify all WICTORY users so the event never gets lost
    if not targets:
        try:
            all_users = await list_users(session)
            targets = [u for u in all_users if u.role == UserRole.WICTORY and getattr(u, "tg_id", None)]
        except Exception:
            targets = []

    for wu in targets:
        try:
            tg_id = int(getattr(wu, "tg_id", 0) or 0)
            if tg_id <= 0 or tg_id in sent_tg_ids:
                continue
            kb = InlineKeyboardBuilder()
            kb.button(text="Перейти", callback_data=f"wictory:invalid:open:{int(item_id)}")
            kb.adjust(1)
            await message.bot.send_message(tg_id, notice_text, parse_mode="HTML", reply_markup=kb.as_markup())
            sent_tg_ids.add(tg_id)
            notified_wictory = True
        except Exception:
            log.exception("Failed to notify WICTORY about invalid pool item: item_id=%s owner_tg_id=%s", item_id, getattr(wu, "tg_id", None))

    if notified_wictory:
        await message.answer(f"Комментарий сохранён. Ресурс <code>#{item_id}</code> отправлен обратно как невалидный, WICTORY уведомлён.", reply_markup=kb_dm_resource_menu())
    else:
        await message.answer(f"Комментарий сохранён. Ресурс <code>#{item_id}</code> отправлен обратно как невалидный, но уведомление WICTORY не доставлено.", reply_markup=kb_dm_resource_menu())


@router.message(DropManagerResourceStates.invalid_comment)
async def dm_resource_invalid_comment_non_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        item_id = int(data.get("resource_item_id") or 0)
    except Exception:
        item_id = 0
    await message.answer(
        "Введите комментарий текстом одним сообщением.",
        reply_markup=kb_dm_back_cancel_inline(
            back_cb=f"dm:resource_active_open:{int(item_id)}" if item_id > 0 else "dm:resource_active",
            cancel_cb="dm:resource_invalid_cancel",
        ),
    )


@router.callback_query(F.data.startswith("dm:resource_attach:"))
async def dm_resource_attach_start(cq: CallbackQuery, session: AsyncSession) -> None:
    if not cq.from_user:
        return
    user = await get_user_by_tg_id(session, cq.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        return
    item_id = int((cq.data or "").split(":")[-1])
    it = await get_pool_item(session, item_id)
    if not it or int(it.assigned_to_user_id or 0) != int(user.id) or str(getattr(it.status, 'value', '')) != 'assigned':
        await cq.answer("Кейс не найден", show_alert=True)
        return
    bank = await get_bank(session, int(it.bank_id))
    bank_name = str(getattr(bank, "name", "") or "").strip().lower()
    created_from, created_to = _period_to_range("today")
    all_forms = await list_user_forms_in_range(session, user_id=int(user.id), created_from=created_from, created_to=created_to)
    forms = [
        f for f in all_forms
        if f.status == FormStatus.APPROVED and str(f.bank_name or "").strip().lower() == bank_name
    ]
    filtered_forms: list[Form] = []
    for f in forms:
        if not await form_has_linked_pool_item(session, form_id=int(f.id)):
            filtered_forms.append(f)
    await cq.answer()
    if cq.message:
        if not filtered_forms:
            await _safe_edit_message(message=cq.message, text="Нет APPROVED анкет этого банка без подвязанного ресурса", reply_markup=kb_dm_resource_active_actions(item_id))
            return
        await _safe_edit_message(message=cq.message, text="Выберите анкету:", reply_markup=kb_dm_resource_attach_forms(item_id, filtered_forms))


@router.callback_query(F.data.startswith("dm:resource_attach_pick:"))
async def dm_resource_attach_pick(cq: CallbackQuery, session: AsyncSession) -> None:
    if not cq.from_user:
        return
    user = await get_user_by_tg_id(session, cq.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        return
    parts = (cq.data or "").split(":")
    if len(parts) != 4:
        await cq.answer("Некорректная кнопка", show_alert=True)
        return
    try:
        item_id = int(parts[2])
        form_id = int(parts[3])
    except Exception:
        await cq.answer("Некорректная кнопка", show_alert=True)
        return
    form = await get_form(session, form_id)
    item = await get_pool_item(session, item_id)
    if not form or int(form.manager_id or 0) != int(user.id):
        await cq.answer("Анкета не найдена", show_alert=True)
        return
    if form.status != FormStatus.APPROVED:
        await cq.answer("Можно подвязывать только APPROVED анкеты", show_alert=True)
        return
    if await form_has_linked_pool_item(session, form_id=form_id):
        # stale list protection: silently refresh available forms instead of showing an error
        created_from, created_to = _period_to_range("today")
        bank = await get_bank(session, int(item.bank_id))
        bank_name = str(getattr(bank, "name", "") or "").strip().lower()
        all_forms = await list_user_forms_in_range(session, user_id=int(user.id), created_from=created_from, created_to=created_to)
        forms = [
            f for f in all_forms
            if f.status == FormStatus.APPROVED and str(f.bank_name or "").strip().lower() == bank_name
        ]
        filtered_forms: list[Form] = []
        for f in forms:
            if not await form_has_linked_pool_item(session, form_id=int(f.id)):
                filtered_forms.append(f)
        await cq.answer()
        if cq.message:
            if not filtered_forms:
                await _safe_edit_message(message=cq.message, text="Нет APPROVED анкет этого банка без подвязанного ресурса", reply_markup=kb_dm_resource_active_actions(item_id))
            else:
                await _safe_edit_message(message=cq.message, text="Выберите анкету:", reply_markup=kb_dm_resource_attach_forms(item_id, filtered_forms))
        return
    if not item:
        await cq.answer("Ресурс не найден", show_alert=True)
        return
    bank = await get_bank(session, int(item.bank_id))
    item_bank_name = str(getattr(bank, "name", "") or "").strip().lower()
    if str(form.bank_name or "").strip().lower() != item_bank_name:
        await cq.answer("Ресурс можно подвязать только к анкете того же банка", show_alert=True)
        return

    it = await mark_pool_item_used_with_form(session, item_id=item_id, dm_user_id=int(user.id), form_id=form_id)
    await cq.answer("Подтянуто")
    if not it:
        return
    wictory_owner = await get_user_by_id(session, int(it.created_by_user_id)) if it else None
    if wictory_owner and wictory_owner.role == UserRole.WICTORY:
        try:
            caption = (
                f"✅ Ресурс подтянут\n"
                f"ID ресурса: <code>{item_id}</code>\n"
                f"Форма #{form_id} · {form.bank_name if form else '—'}"
            )
            shots = list(getattr(it, "screenshots", None) or [])
            if shots:
                kind, fid = unpack_media_item(str(shots[0]))
                if kind == "photo":
                    await cq.bot.send_photo(int(wictory_owner.tg_id), fid, caption=caption, parse_mode="HTML")
                elif kind == "video":
                    await cq.bot.send_video(int(wictory_owner.tg_id), fid, caption=caption, parse_mode="HTML")
                else:
                    await cq.bot.send_document(int(wictory_owner.tg_id), fid, caption=caption, parse_mode="HTML")
            else:
                await cq.bot.send_message(
                    int(wictory_owner.tg_id),
                    caption,
                    disable_web_page_preview=True,
                )
        except Exception:
            pass
    if cq.message:
        await _safe_edit_message(message=cq.message, text=f"Ресурс <code>#{item_id}</code> привязан к анкете <code>#{form_id}</code> и удален из активных", reply_markup=kb_dm_resource_menu())


@router.callback_query(F.data == "dm:resource_used")
async def dm_resource_used(cq: CallbackQuery, session: AsyncSession) -> None:
    if not cq.from_user:
        return
    user = await get_user_by_tg_id(session, cq.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        return
    items = await list_dm_used_pool_items(session, dm_user_id=int(user.id), limit=100)
    packed: list[tuple[int, str]] = []
    for it in items:
        bank = await get_bank(session, int(it.bank_id))
        form_suffix = f" → анкета #{int(it.used_with_form_id)}" if getattr(it, "used_with_form_id", None) else ""
        packed.append((int(it.id), f"#{int(it.id)} {bank.name if bank else '—'} | {_pool_type_ru(getattr(it.type, 'value', ''))}{form_suffix}"))
    await cq.answer()
    if cq.message:
        if not packed:
            await _safe_edit_message(message=cq.message, text="У вас пока нет подтянутых ссылок/Esim", reply_markup=kb_dm_resource_menu())
            return
        await _safe_edit_message(message=cq.message, text="Мои подтянутые ссылки:", reply_markup=kb_dm_resource_used_list(packed))


@router.callback_query(F.data.startswith("dm:resource_used_open:"))
async def dm_resource_used_open(cq: CallbackQuery, session: AsyncSession) -> None:
    if not cq.from_user:
        return
    user = await get_user_by_tg_id(session, cq.from_user.id)
    if not user or user.role != UserRole.DROP_MANAGER:
        return
    item_id = int((cq.data or "").split(":")[-1])
    it = await get_pool_item(session, item_id)
    if not it or str(getattr(it.status, "value", "")) != "used":
        await cq.answer("Ресурс не найден", show_alert=True)
        return
    form = await get_form(session, int(it.used_with_form_id or 0)) if getattr(it, "used_with_form_id", None) else None
    if not form or int(form.manager_id or 0) != int(user.id):
        await cq.answer("Нет доступа", show_alert=True)
        return
    bank = await get_bank(session, int(it.bank_id))
    txt = (
        f"<b>Подтянутый ресурс</b>\n"
        f"ID ресурса: <code>{int(it.id)}</code>\n"
        f"Банк: <b>{bank.name if bank else '—'}</b>\n"
        f"Тип: <b>{_pool_type_ru(getattr(it.type, 'value', ''))}</b>\n"
        f"Анкета: <code>#{int(form.id)}</code>\n"
        f"Данные: <code>{it.text_data or '—'}</code>"
    )
    await cq.answer()
    if cq.message:
        await _safe_edit_message(message=cq.message, text=txt, reply_markup=kb_dm_resource_used_actions())


@router.callback_query()
async def dm_unhandled_callback_fallback(cq: CallbackQuery) -> None:
    data = cq.data or ""
    try:
        log.warning("UNHANDLED_CALLBACK data=%s from=%s", data, getattr(cq.from_user, "id", None))
    except Exception:
        pass
    await cq.answer("Эта кнопка неактивна, напишите разработчику", show_alert=True)


