from __future__ import annotations

import logging
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
    ReplyKeyboardRemove,
)
from sqlalchemy.ext.asyncio import AsyncSession

from bot.callbacks import AccessRequestCb
from bot.config import Settings
from bot.keyboards import (
    DEFAULT_BANKS,
    kb_access_request,
    kb_developer_main,
    kb_developer_start,
    kb_developer_with_back,
    kb_developer_list,
    kb_developer_stats,
    kb_dev_forms_filter_menu,
    kb_dev_main_inline,
    kb_dev_users_list,
    kb_dev_forms_list,
    kb_dev_requests_list,
    kb_dev_users_list_beautiful,
    kb_dev_users_list_beautiful_with_sources,
    kb_dev_forms_list_beautiful,
    kb_dev_requests_list_beautiful,
    kb_dev_confirm,
    kb_dev_user_actions,
    kb_dev_form_actions,
    kb_dev_req_actions,
    kb_dev_edit_user,
    kb_dev_edit_form,
    kb_dev_team_leads_actions,
    kb_dev_groups_actions,
    kb_dev_groups_list,
    kb_dev_group_open,
    kb_dev_pick_forward_group,
    kb_dev_pick_user_role,
    kb_dev_pick_team_lead_source,
    kb_dev_pick_user_source,
    kb_dev_forms_filter_menu,
    kb_dev_back_main_inline,
    kb_dev_req_pick_role,
    kb_dev_req_pick_team_lead_source,
    kb_dev_req_pick_dm_source,
    kb_dev_req_pick_forward_group,
    kb_dev_team_lead_pick_source,
)
from bot.models import AccessRequestStatus, FormStatus, UserRole
from bot.utils import (
    format_access_status,
    format_bank_hashtag,
    format_form_status,
    unpack_media_item,
)
from bot.repositories import (
    create_forward_group,
    delete_forward_group,
    get_forward_group_by_id,
    add_team_lead,
    count_pending_access_requests,
    delete_team_lead,
    delete_access_request_by_user_id,
    delete_form,
    delete_form_by_user_id,
    delete_user_by_tg_id,
    get_form,
    get_form_counts_by_manager,
    get_user_by_username,
    get_next_pending_access_request,
    get_user_by_id,
    get_user_by_tg_id,
    list_forward_groups,
    list_team_leads,
    list_all_access_requests,
    list_all_forms,
    list_all_forms_in_range,
    list_forms_by_user_id,
    list_users,
    set_user_forward_group,
    update_forward_group_status,
    set_access_request_status,
    set_user_role,
)
from bot.states import DeveloperStates
from bot.middlewares import GroupMessageFilter

router = Router(name="developer")
# Apply group message filter to all handlers in this router
router.message.filter(GroupMessageFilter())


async def _render_groups_menu(cq_or_msg: CallbackQuery | Message, session: AsyncSession) -> None:
    groups = await list_forward_groups(session)
    lines: list[str] = ["👥 <b>Группы пересылки</b>\n"]
    if not groups:
        lines.append("Список пуст.")
    else:
        for g in groups:
            status = "✅" if getattr(g, "is_confirmed", False) else "❌"
            title = getattr(g, "title", None) or "—"
            chat_id = getattr(g, "chat_id", None)
            lines.append(f"- {status} <b>#{g.id}</b> | <code>{chat_id}</code> | {title}")

    text = "\n".join(lines)
    kb = kb_dev_groups_list(groups)
    if isinstance(cq_or_msg, CallbackQuery):
        await cq_or_msg.answer()
        if cq_or_msg.message:
            try:
                await cq_or_msg.message.edit_text(text, reply_markup=kb)
            except TelegramBadRequest as e:
                if "message is not modified" not in str(e).lower():
                    raise
        return
    await cq_or_msg.answer(text, reply_markup=kb)


async def _check_forward_group(bot, *, group_id: int, chat_id: int, session: AsyncSession) -> tuple[bool, str | None]:
    title: str | None = None
    ok = False
    try:
        chat = await bot.get_chat(chat_id)
        title = getattr(chat, "title", None)
        me = await bot.get_me()
        await bot.get_chat_member(chat_id, me.id)
        ok = True
    except Exception:
        ok = False
    await update_forward_group_status(session, group_id=group_id, is_confirmed=ok, title=title, checked_at=datetime.utcnow())
    return ok, title
log = logging.getLogger(__name__)


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
        start = end - timedelta(days=7)
        return start, end

    if p == "last30":
        end = datetime(today.year, today.month, today.day) + timedelta(days=1)
        start = end - timedelta(days=30)
        return start, end

    if p == "week":
        # Monday as start of week
        start_date = today - timedelta(days=today.weekday())
        start = datetime(start_date.year, start_date.month, start_date.day)
        return start, start + timedelta(days=7)

    if p == "month":
        start = datetime(today.year, today.month, 1)
        # next month start
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

    # custom stored separately in state
    return None, None


async def _load_forms_with_filter(session: AsyncSession, state: FSMContext) -> list:
    data = await state.get_data()
    period = (data.get("forms_period") or "today")
    if period == "custom":
        created_from = data.get("forms_created_from")
        created_to = data.get("forms_created_to")
        return await list_all_forms_in_range(session, created_from=created_from, created_to=created_to)
    created_from, created_to = _period_to_range(period)
    return await list_all_forms_in_range(session, created_from=created_from, created_to=created_to)


async def _render_team_leads_menu(cq_or_msg: CallbackQuery | Message, session: AsyncSession) -> None:
    tls = await list_team_leads(session)
    lines = ["👑 <b>Тим‑лиды</b>\n"]
    if not tls:
        lines.append("Список пуст.")
    else:
        for tl in tls:
            u = await get_user_by_tg_id(session, int(tl.tg_id))
            uname = f"@{u.username}" if u and u.username else "—"
            name = f"{(u.first_name or '') if u else ''} {(u.last_name or '') if u else ''}".strip() or "—"
            src = str(tl.source).split(".")[-1]
            src_icon = "✈️" if src == "TG" else "📘"
            lines.append(f"- 👑{src_icon} <code>{tl.tg_id}</code> | {uname} | {name}")
    text = "\n".join(lines)

    if isinstance(cq_or_msg, CallbackQuery):
        await cq_or_msg.answer()
        if cq_or_msg.message:
            try:
                await cq_or_msg.message.edit_text(text, reply_markup=kb_dev_team_leads_actions())
            except TelegramBadRequest as e:
                if "message is not modified" not in str(e).lower():
                    raise
        return
    await cq_or_msg.answer(text, reply_markup=kb_dev_team_leads_actions())


@router.message(F.text == "Старт")
async def dev_start(message: Message, session: AsyncSession, settings: Settings) -> None:
    # Ignore group messages
    if message.chat.type in ['group', 'supergroup']:
        return
    if not message.from_user:
        return
    if message.from_user.id not in settings.developer_id_set:
        return
    
    pending_cnt = await count_pending_access_requests(session)
    m = await message.answer("...", reply_markup=ReplyKeyboardRemove())
    try:
        await m.delete()
    except Exception:
        pass
    await message.answer(
        f"👨‍💻 <b>Панель разработчика</b>\n\n"
        f"Заявок в очереди: <b>{pending_cnt}</b>",
        reply_markup=kb_dev_main_inline(),
    )


@router.callback_query(F.data == "dev:back_to_main")
async def dev_back_to_main(cq: CallbackQuery, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not cq.from_user:
        return
    if cq.from_user.id not in settings.developer_id_set:
        await cq.answer("Нет прав", show_alert=True)
        return
    try:
        await cq.answer()
    except Exception:
        pass
    await state.clear()
    pending_cnt = await count_pending_access_requests(session)
    if cq.message:
        await cq.message.edit_text(
            f"👨‍💻 <b>Панель разработчика</b>\n\n"
            f"Заявок в очереди: <b>{pending_cnt}</b>",
            reply_markup=kb_dev_main_inline(),
        )


@router.callback_query(F.data.startswith("dev:menu:"))
async def dev_menu_router(cq: CallbackQuery, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not cq.from_user:
        return
    if cq.from_user.id not in settings.developer_id_set:
        await cq.answer("Нет прав", show_alert=True)
        return
    try:
        await cq.answer()
    except Exception:
        pass
    action = (cq.data or "").split(":")[-1]
    if action == "reqs":
        requests = await list_all_access_requests(session)
        if not requests:
            if cq.message:
                await cq.message.edit_text("📝 <b>Заявок нет</b>", reply_markup=kb_dev_back_main_inline())
            return
        await state.clear()
        await state.set_state(DeveloperStates.reqs_list)
        await state.update_data(requests=requests)
        text, inline_kb = kb_dev_requests_list_beautiful(requests)
        if cq.message:
            await cq.message.edit_text(text if len(text) <= 3500 else "📝 <b>ЗАЯВКИ НА ДОСТУП</b>", reply_markup=inline_kb)
        return

    if action == "users":
        users = await list_users(session)
        if not users:
            if cq.message:
                await cq.message.edit_text("👥 <b>Пользователей нет</b>", reply_markup=kb_dev_back_main_inline())
            return
        await state.clear()
        await state.set_state(DeveloperStates.users_list)
        await state.update_data(users=users)
        tls = await list_team_leads(session)
        tl_map = {int(tl.tg_id): str(tl.source).split(".")[-1] for tl in tls}
        await state.update_data(team_lead_sources=tl_map)
        text, inline_kb = kb_dev_users_list_beautiful_with_sources(users, team_lead_sources=tl_map)
        if cq.message:
            await cq.message.edit_text(text if len(text) <= 3500 else "👥 <b>ПОЛЬЗОВАТЕЛИ СИСТЕМЫ</b>", reply_markup=inline_kb)
        return

    if action == "forms":
        data = await state.get_data()
        if not data.get("forms_period"):
            await state.update_data(forms_period="today")
        forms = await _load_forms_with_filter(session, state)
        if not forms:
            # Still show filter menu even when empty
            text, inline_kb = kb_dev_forms_list_beautiful([])
            if cq.message:
                await cq.message.edit_text(text, reply_markup=inline_kb)
            return
        data = await state.get_data()
        await state.clear()
        await state.set_state(DeveloperStates.forms_list)
        await state.update_data(
            forms=forms,
            forms_period=(data.get("forms_period") or "today"),
            forms_created_from=data.get("forms_created_from"),
            forms_created_to=data.get("forms_created_to"),
        )
        text, inline_kb = kb_dev_forms_list_beautiful(forms)
        if cq.message:
            await cq.message.edit_text(text if len(text) <= 3500 else "📋 <b>АНКЕТЫ СИСТЕМЫ</b>", reply_markup=inline_kb)
        return

    if action == "stats":
        users = await list_users(session)
        stats = await get_form_counts_by_manager(session)
        lines = [_format_stats_header()]
        total_forms = 0
        total_in_progress = 0
        total_pending = 0
        total_approved = 0
        total_rejected = 0
        drop_managers = [u for u in users if u.role == UserRole.DROP_MANAGER]
        for u in drop_managers:
            cnts = stats.get(u.id, {})
            in_prog = cnts.get(FormStatus.IN_PROGRESS, 0)
            pending = cnts.get(FormStatus.PENDING, 0)
            approved = cnts.get(FormStatus.APPROVED, 0)
            rejected = cnts.get(FormStatus.REJECTED, 0)
            total = in_prog + pending + approved + rejected
            total_forms += total
            total_in_progress += in_prog
            total_pending += pending
            total_approved += approved
            total_rejected += rejected
        total_completed = total_approved + total_rejected
        total_efficiency = round((total_approved / total_completed * 100) if total_completed > 0 else 0, 1)
        lines.append(f"📈 <b>Общая статистика:</b>")
        lines.append(f"   🔸 Дроп-менеджеры: <b>{len(drop_managers)}</b>")
        lines.append(f"   🔸 Всего анкет: <b>{total_forms}</b>")
        lines.append(f"   🔸 В работе: <b>{total_in_progress}</b>")
        lines.append(f"   🔸 На проверке: <b>{total_pending}</b>")
        lines.append(f"   🔸 Одобрено: <b>{total_approved}</b>")
        lines.append(f"   🔸 Отклонено: <b>{total_rejected}</b>")
        lines.append(f"   🔸 Общая эффективность: <b>{total_efficiency}%</b>")
        lines.append(f"\n🎯 <b>Статистика по менеджерам:</b>")
        if not drop_managers:
            lines.append("   — пока нет дроп‑менеджеров.")
        else:
            sorted_managers = sorted(drop_managers, key=lambda u: sum(stats.get(u.id, {}).values()), reverse=True)
            for i, u in enumerate(sorted_managers, 1):
                cnts = stats.get(u.id, {})
                lines.append(f"\n{i}. {_format_user_stats(u, cnts)}")
        if cq.message:
            await cq.message.edit_text("\n".join(lines), reply_markup=kb_dev_back_main_inline())
        return

    if action == "groups":
        await state.clear()
        await state.set_state(DeveloperStates.groups_menu)
        await _render_groups_menu(cq, session)
        return

    if action == "tls":
        await state.clear()
        await state.set_state(DeveloperStates.team_leads_menu)
        await _render_team_leads_menu(cq, session)
        return


@router.callback_query(F.data == "dev:forms_filter_menu")
async def dev_forms_filter_menu_cb(cq: CallbackQuery, state: FSMContext) -> None:
    await cq.answer()
    data = await state.get_data()
    current = (data.get("forms_period") or "today")
    if cq.message:
        await cq.message.edit_text("📅 <b>Фильтр анкет</b>", reply_markup=kb_dev_forms_filter_menu(current=current))


@router.callback_query(F.data.startswith("dev:forms_filter_set:"))
async def dev_forms_filter_set_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await cq.answer()
    period = (cq.data or "").split(":")[-1]
    await state.update_data(forms_period=period, forms_created_from=None, forms_created_to=None)
    forms = await _load_forms_with_filter(session, state)
    await state.update_data(forms=forms)
    await state.set_state(DeveloperStates.forms_list)
    text, inline_kb = kb_dev_forms_list_beautiful(forms)
    if cq.message:
        await cq.message.edit_text(text if len(text) <= 3500 else "📋 <b>АНКЕТЫ СИСТЕМЫ</b>", reply_markup=inline_kb)


@router.callback_query(F.data == "dev:forms_filter_custom")
async def dev_forms_filter_custom_cb(cq: CallbackQuery, state: FSMContext) -> None:
    await cq.answer()
    await state.set_state(DeveloperStates.forms_filter_range)
    if cq.message:
        await cq.message.edit_text("Введите интервал дат в формате: <code>DD.MM.YYYY-DD.MM.YYYY</code>")


@router.message(DeveloperStates.forms_filter_range, F.text)
async def dev_forms_filter_range_msg(message: Message, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    # Ignore group messages
    if message.chat.type in ['group', 'supergroup']:
        return
    if not message.from_user:
        return
    if message.from_user.id not in settings.developer_id_set:
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

    await state.update_data(forms_period="custom", forms_created_from=created_from, forms_created_to=created_to)
    forms = await _load_forms_with_filter(session, state)
    await state.set_state(DeveloperStates.forms_list)
    await state.update_data(forms=forms)
    text, inline_kb = kb_dev_forms_list_beautiful(forms)
    await message.answer(text if len(text) <= 3500 else "📋 <b>АНКЕТЫ СИСТЕМЫ</b>", reply_markup=inline_kb)


@router.callback_query(F.data == "dev:forms_filter_back")
async def dev_forms_filter_back_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await cq.answer()
    forms = await _load_forms_with_filter(session, state)
    await state.set_state(DeveloperStates.forms_list)
    await state.update_data(forms=forms)
    text, inline_kb = kb_dev_forms_list_beautiful(forms)
    if cq.message:
        await cq.message.edit_text(text if len(text) <= 3500 else "📋 <b>АНКЕТЫ СИСТЕМЫ</b>", reply_markup=inline_kb)


@router.callback_query(F.data.startswith("dev:tls:add:"))
async def dev_tls_add_start(cq: CallbackQuery, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not cq.from_user:
        return
    if cq.from_user.id not in settings.developer_id_set:
        await cq.answer("Нет прав", show_alert=True)
        return
    src = cq.data.split(":")[-1].upper()
    if src not in {"TG", "FB"}:
        await cq.answer("Некорректный источник", show_alert=True)
        return
    await state.set_state(DeveloperStates.team_leads_add)
    await state.update_data(tl_source=src)
    await cq.answer()
    if cq.message:
        await cq.message.answer(f"Введите tg_id тим‑лида для <b>{src}</b>:")


@router.callback_query(F.data == "dev:tls:del")
async def dev_tls_del_start(cq: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not cq.from_user:
        return
    if cq.from_user.id not in settings.developer_id_set:
        await cq.answer("Нет прав", show_alert=True)
        return
    await state.set_state(DeveloperStates.team_leads_delete)
    await cq.answer()
    if cq.message:
        await cq.message.answer("Введите tg_id тим‑лида для удаления:")


@router.callback_query(F.data == "dev:tls:edit_source")
async def dev_tls_edit_source_start(cq: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not cq.from_user:
        return
    if cq.from_user.id not in settings.developer_id_set:
        await cq.answer("Нет прав", show_alert=True)
        return
    await state.set_state(DeveloperStates.team_leads_edit_source)
    await cq.answer()
    if cq.message:
        await cq.message.answer("Введите tg_id тим‑лида для смены источника:")


@router.message(DeveloperStates.team_leads_add, F.text)
async def dev_tls_add_finish(message: Message, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not message.from_user:
        return
    if message.from_user.id not in settings.developer_id_set:
        return
    txt = (message.text or "").strip()
    if not txt.isdigit():
        await message.answer("Нужно число (tg_id). Пример: <code>755213716</code>")
        return
    data = await state.get_data()
    src = (data.get("tl_source") or "TG").upper()
    await add_team_lead(session, int(txt), src)
    await state.set_state(DeveloperStates.team_leads_menu)
    await message.answer("✅ Добавлено/обновлено.")
    await _render_team_leads_menu(message, session)


@router.message(DeveloperStates.team_leads_delete, F.text)
async def dev_tls_del_finish(message: Message, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not message.from_user:
        return
    if message.from_user.id not in settings.developer_id_set:
        return
    txt = (message.text or "").strip()
    if not txt.isdigit():
        await message.answer("Нужно число (tg_id). Пример: <code>755213716</code>")
        return
    n = await delete_team_lead(session, int(txt))
    await state.set_state(DeveloperStates.team_leads_menu)
    await message.answer("✅ Удалено." if n else "Не найдено.")
    await _render_team_leads_menu(message, session)


@router.message(DeveloperStates.team_leads_edit_source, F.text)
async def dev_tls_edit_source_finish(message: Message, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not message.from_user:
        return
    if message.from_user.id not in settings.developer_id_set:
        return
    txt = (message.text or "").strip()
    if not txt.isdigit():
        await message.answer("Нужно число (tg_id). Пример: <code>755213716</code>")
        return
    tg_id = int(txt)
    tl = await get_team_lead_by_tg_id(session, tg_id)
    if not tl:
        await message.answer("Тим‑лид не найден.")
        return
    await state.set_state(DeveloperStates.team_leads_edit_source)
    await state.update_data(tl_edit_tg_id=tg_id)
    await message.answer("Выберите новый источник:", reply_markup=kb_dev_team_lead_pick_source(tg_id))


@router.callback_query(F.data.startswith("dev:tls:set_source:"))
async def dev_tls_set_source_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not cq.from_user:
        return
    if cq.from_user.id not in settings.developer_id_set:
        await cq.answer("Нет прав", show_alert=True)
        return
    parts = (cq.data or "").split(":")
    if len(parts) < 5:
        await cq.answer("Некорректная кнопка", show_alert=True)
        return
    try:
        tg_id = int(parts[-2])
    except Exception:
        await cq.answer("Некорректный ID", show_alert=True)
        return
    src = parts[-1].upper()
    if src not in {"TG", "FB"}:
        await cq.answer("Некорректный источник", show_alert=True)
        return
    await add_team_lead(session, tg_id, src)
    await cq.answer("Сохранено")
    await state.set_state(DeveloperStates.team_leads_menu)
    await _render_team_leads_menu(cq, session)


def _format_user_line(u, group_line: str | None = None) -> str:
    uname = f"@{u.username}" if u.username else "—"
    name = f"{u.first_name or ''} {u.last_name or ''}".strip() or "—"
    mtag = u.manager_tag or "—"
    src = getattr(u, "manager_source", None) or "—"
    
    # Emoji для ролей
    role_emoji = {
        UserRole.DEVELOPER: "👨‍💻",
        UserRole.TEAM_LEAD: "👑",
        UserRole.DROP_MANAGER: "🎯",
        UserRole.WICTORY: "🧩",
        UserRole.PENDING: "⏳"
    }
    
    emoji = role_emoji.get(u.role, "❓")
    
    group_line = group_line or "—"
    return (
        f"{emoji} <b>{u.role}</b> | id: <code>{u.tg_id}</code>\n"
        f"   👤 <b>{name}</b>\n"
        f"   📞 {uname}\n"
        f"   🏷️ <b>{mtag}</b>\n"
        f"   🌐 <b>{src}</b>\n"
        f"   🗂️ <b>Группа:</b> {group_line}"
    )


@router.callback_query(F.data.startswith("dev:edit_user:"))
async def dev_edit_user_start(cq: CallbackQuery, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not cq.from_user:
        return
    if cq.from_user.id not in settings.developer_id_set:
        await cq.answer("Нет прав", show_alert=True)
        return

    parts = (cq.data or "").split(":")
    # Supports:
    # - dev:edit_user:<tg_id>
    # - dev:edit_user:<tg_id>:role (legacy/back-compat)
    if len(parts) < 3:
        await cq.answer("Некорректная кнопка", show_alert=True)
        return
    try:
        tg_id = int(parts[2])
    except Exception:
        await cq.answer("Некорректная кнопка", show_alert=True)
        return

    user = await get_user_by_tg_id(session, tg_id)
    if not user:
        await cq.answer("Пользователь не найден", show_alert=True)
        return

    # Back-compat: some keyboards use dev:edit_user:<tg_id>:role as back target
    if len(parts) >= 4 and parts[3] == "role":
        await cq.answer()
        if cq.message:
            try:
                await cq.message.edit_text("Выберите роль:", reply_markup=kb_dev_pick_user_role(tg_id))
            except TelegramBadRequest as e:
                if "message is not modified" not in str(e).lower():
                    raise
        return

    await cq.answer()
    await state.set_state(DeveloperStates.user_edit_field)
    await state.update_data(current_user_id=tg_id, edit_field=None)
    if cq.message:
        await cq.message.edit_text(
            f"✏️ <b>РЕДАКТИРОВАНИЕ ПОЛЬЗОВАТЕЛЯ #{tg_id}</b>\n\nВыберите поле:",
            reply_markup=kb_dev_edit_user(tg_id),
        )


@router.callback_query(F.data.startswith("dev:edit_user_field:"))
async def dev_edit_user_field(cq: CallbackQuery, state: FSMContext, session: AsyncSession, settings: Settings) -> None:
    if not cq.from_user:
        return
    if cq.from_user.id not in settings.developer_id_set:
        await cq.answer("Нет прав", show_alert=True)
        return
    parts = cq.data.split(":")
    tg_id = int(parts[2])
    field = parts[3]
    await cq.answer()

    if field == "role":
        if cq.message:
            await cq.message.edit_text("Выберите роль:", reply_markup=kb_dev_pick_user_role(tg_id))
        return
    if field == "manager_source":
        if cq.message:
            await cq.message.edit_text("Выберите источник:", reply_markup=kb_dev_pick_user_source(tg_id))
        return

    await state.set_state(DeveloperStates.user_edit_field)
    await state.update_data(current_user_id=tg_id, edit_field=field)

    prompts = {
        "first_name": "Введите имя:",
        "last_name": "Введите фамилию:",
        "username": "Введите username (без @):",
        "manager_tag": "Введите тег менеджера:",
    }
    prompt = prompts.get(field, "Введите новое значение:")
    if cq.message:
        await cq.message.edit_text(prompt)


@router.callback_query(F.data.startswith("dev:set_user_role:"))
async def dev_set_user_role_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not cq.from_user:
        return
    if cq.from_user.id not in settings.developer_id_set:
        await cq.answer("Нет прав", show_alert=True)
        return
    parts = cq.data.split(":")
    # Expected: dev:set_user_role:<tg_id>:<role>
    # Tolerate older formats if they exist
    if len(parts) < 4:
        await cq.answer("Некорректная кнопка", show_alert=True)
        return
    tg_id = int(parts[-2])
    role = parts[-1].upper()
    if role not in {"PENDING", "DROP_MANAGER", "TEAM_LEAD", "DEVELOPER", "WICTORY"}:
        await cq.answer("Некорректная роль", show_alert=True)
        return
    user = await get_user_by_tg_id(session, tg_id)
    if not user:
        await cq.answer("Пользователь не найден", show_alert=True)
        return

    # If switching away from TEAM_LEAD, remove from team_leads table
    if user.role == UserRole.TEAM_LEAD and role != "TEAM_LEAD":
        await delete_team_lead(session, int(user.tg_id))

    user.role = UserRole(role)

    # If TEAM_LEAD selected, ask for TG/FB source and create TeamLead record after that
    if role == "TEAM_LEAD":
        await cq.answer("Роль сохранена")
        await state.set_state(DeveloperStates.user_view)
        await state.update_data(current_user_id=tg_id)
        if cq.message:
            try:
                await cq.message.edit_text(
                    f"👤 <b>ПОЛЬЗОВАТЕЛЬ #{tg_id}</b>\n\nВыберите источник тим‑лида:",
                    reply_markup=kb_dev_pick_team_lead_source(tg_id),
                )
            except TelegramBadRequest as e:
                if "message is not modified" not in str(e).lower():
                    raise
        return

    await cq.answer("Сохранено")
    await state.set_state(DeveloperStates.user_view)
    await state.update_data(current_user_id=tg_id)
    if cq.message:
        group_line = "—"
        if getattr(user, "forward_group_id", None):
            g = await get_forward_group_by_id(session, int(user.forward_group_id))
            if g:
                title = getattr(g, "title", None) or "—"
                group_line = f"#{g.id} <code>{g.chat_id}</code> {title}"
        details = _format_user_line(user, group_line)
        try:
            await cq.message.edit_text(
                f"👤 <b>ПОЛЬЗОВАТЕЛЬ #{tg_id}</b>\n\n{details}\n\nВыберите действие:",
                reply_markup=kb_dev_user_actions(tg_id),
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise


@router.callback_query(F.data.startswith("dev:set_team_lead_source:"))
async def dev_set_team_lead_source_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not cq.from_user:
        return
    if cq.from_user.id not in settings.developer_id_set:
        await cq.answer("Нет прав", show_alert=True)
        return

    parts = cq.data.split(":")
    if len(parts) < 4:
        await cq.answer("Некорректная кнопка", show_alert=True)
        return
    tg_id = int(parts[-2])
    src = parts[-1].upper()
    if src not in {"TG", "FB"}:
        await cq.answer("Некорректный источник", show_alert=True)
        return

    user = await get_user_by_tg_id(session, tg_id)
    if not user:
        await cq.answer("Пользователь не найден", show_alert=True)
        return
    if user.role != UserRole.TEAM_LEAD:
        await cq.answer("Роль не TEAM_LEAD", show_alert=True)
        return

    await add_team_lead(session, int(user.tg_id), src)
    await cq.answer("Сохранено")
    await state.set_state(DeveloperStates.user_view)
    await state.update_data(current_user_id=tg_id)
    if cq.message:
        details = _format_user_line(user)
        await cq.message.edit_text(
            f"👤 <b>ПОЛЬЗОВАТЕЛЬ #{tg_id}</b>\n\n{details}\n\nВыберите действие:",
            reply_markup=kb_dev_user_actions(tg_id),
        )


@router.callback_query(F.data.startswith("dev:set_user_source:"))
async def dev_set_user_source_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not cq.from_user:
        return
    if cq.from_user.id not in settings.developer_id_set:
        await cq.answer("Нет прав", show_alert=True)
        return
    parts = cq.data.split(":")
    # Expected: dev:set_user_source:<tg_id>:<TG|FB|NONE>
    if len(parts) < 4:
        await cq.answer("Некорректная кнопка", show_alert=True)
        return
    tg_id = int(parts[-2])
    src = parts[-1].upper()
    if src not in {"TG", "FB", "NONE"}:
        await cq.answer("Некорректный источник", show_alert=True)
        return
    user = await get_user_by_tg_id(session, tg_id)
    if not user:
        await cq.answer("Пользователь не найден", show_alert=True)
        return
    user.manager_source = None if src == "NONE" else src
    await cq.answer("Сохранено")
    await state.set_state(DeveloperStates.user_view)
    await state.update_data(current_user_id=tg_id)
    if cq.message:
        details = _format_user_line(user)
        await cq.message.edit_text(
            f"👤 <b>ПОЛЬЗОВАТЕЛЬ #{tg_id}</b>\n\n{details}\n\nВыберите действие:",
            reply_markup=kb_dev_user_actions(tg_id),
        )


@router.message(DeveloperStates.user_edit_field, F.text)
async def dev_save_user_field(message: Message, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    # Ignore group messages
    if message.chat.type in ['group', 'supergroup']:
        return
    if not message.from_user:
        return
    if message.from_user.id not in settings.developer_id_set:
        return

    data = await state.get_data()
    tg_id = data.get("current_user_id")
    field = data.get("edit_field")
    if not tg_id or not field:
        await state.clear()
        await message.answer("Ошибка состояния.", reply_markup=kb_dev_main_inline())
        return
    user = await get_user_by_tg_id(session, int(tg_id))
    if not user:
        await state.clear()
        await message.answer("Пользователь не найден.", reply_markup=kb_dev_main_inline())
        return

    val = (message.text or "").strip()
    if field == "username":
        val = val.lstrip("@").strip()
        user.username = val or None
    elif field in {"first_name", "last_name", "manager_tag"}:
        setattr(user, field, val or None)
    else:
        setattr(user, field, val)

    await session.commit()
    await state.set_state(DeveloperStates.user_view)
    await state.update_data(current_user_id=int(tg_id))
    details = _format_user_line(user)
    await message.answer(
        f"✅ <b>Поле обновлено!</b>\n\n👤 <b>ПОЛЬЗОВАТЕЛЬ #{tg_id}</b>\n\n{details}\n\nВыберите действие:",
        reply_markup=kb_dev_user_actions(int(tg_id)),
    )


def _format_stats_header() -> str:
    return "📊 <b>СТАТИСТИКА СИСТЕМЫ</b> 📊\n" + "="*30


def _format_user_stats(u, counts) -> str:
    tag = u.manager_tag or (f"@{u.username}" if u.username else str(u.tg_id))
    in_prog = counts.get(FormStatus.IN_PROGRESS, 0)
    pending = counts.get(FormStatus.PENDING, 0)
    approved = counts.get(FormStatus.APPROVED, 0)
    rejected = counts.get(FormStatus.REJECTED, 0)
    total = in_prog + pending + approved + rejected
    
    # Эффективность (процент одобренных от всех завершенных)
    completed = approved + rejected
    efficiency = round((approved / completed * 100) if completed > 0 else 0, 1)
    
    return (
        f"🎯 <b>{tag}</b> | id: <code>{u.tg_id}</code>\n"
        f"   📈 Всего анкет: <b>{total}</b>\n"
        f"   ⏳ В работе: <b>{in_prog}</b>\n"
        f"   📨 На проверке: <b>{pending}</b>\n"
        f"   ✅ Одобрено: <b>{approved}</b>\n"
        f"   ❌ Отклонено: <b>{rejected}</b>\n"
        f"   📊 Эффективность: <b>{efficiency}%</b>"
    )


def _format_form_summary(form) -> str:
    """Format a form summary for the list."""
    status_emoji = {
        FormStatus.IN_PROGRESS: "⏳",
        FormStatus.PENDING: "📨",
        FormStatus.APPROVED: "✅",
        FormStatus.REJECTED: "❌"
    }
    
    emoji = status_emoji.get(form.status, "❓")
    traffic = "Прямой" if form.traffic_type == "DIRECT" else "Сарафан" if form.traffic_type == "REFERRAL" else "—"
    bank = format_bank_hashtag(getattr(form, "bank_name", None))
    
    return (
        f"{emoji} <b>ID: {form.id}</b> | {format_form_status(form.status)}\n"
        f"   🏦 Банк: {bank}\n"
        f"   📊 Тип клиента: {traffic}\n"
        f"   📞 Телефон: {form.phone or '—'}\n"
        f"   📸 Скриншоты: {len(form.screenshots or [])}\n"
        f"   📅 Создана: {form.created_at.strftime('%d.%m.%Y %H:%M')}"
    )


def _format_form_details(form) -> str:
    """Format detailed form information."""
    status_emoji = {
        FormStatus.IN_PROGRESS: "⏳",
        FormStatus.PENDING: "📨",
        FormStatus.APPROVED: "✅",
        FormStatus.REJECTED: "❌"
    }
    
    emoji = status_emoji.get(form.status, "❓")
    traffic = "Прямой" if form.traffic_type == "DIRECT" else "Сарафан" if form.traffic_type == "REFERRAL" else "—"
    bank = format_bank_hashtag(getattr(form, "bank_name", None))
    
    details = (
        f"{emoji} <b>АНКЕТА #{form.id}</b>\n"
        f"Статус: <b>{format_form_status(form.status)}</b>\n\n"
        f"📊 <b>Информация:</b>\n"
        f"Менеджер ID: <code>{form.manager_id}</code>\n"
        f"Тип клиента: <b>{traffic}</b>\n"
        f"Банк: <b>{bank}</b>\n"
        f"Телефон: <code>{form.phone or '—'}</code>\n"
        f"Пароль: <code>{form.password or '—'}</code>\n"
        f"Скриншоты: <b>{len(form.screenshots or [])}</b> шт.\n"
        f"Комментарий: {form.comment or '—'}\n\n"
        f"📅 <b>Даты:</b>\n"
        f"Создана: {form.created_at.strftime('%d.%m.%Y %H:%M')}\n"
        f"Обновлена: {form.updated_at.strftime('%d.%m.%Y %H:%M')}"
    )
    
    if form.team_lead_comment:
        details += f"\n\n💬 <b>Комментарий тим-лида:</b>\n{form.team_lead_comment}"
    
    return details


async def _send_form_details_with_actions(
    *,
    bot: object,
    chat_id: int,
    form,
    form_id: int,
    reply_markup,
) -> None:
    photos = list(getattr(form, "screenshots", None) or [])
    details = _format_form_details(form)
    if photos:
        photos = photos[:10]
        caption = f"📋 <b>АНКЕТА #{form_id}</b>\n\n{details}"
        if len(photos) == 1:
            try:
                kind, fid = unpack_media_item(str(photos[0]))
                if kind == "doc":
                    await bot.send_document(chat_id, fid, caption=caption, parse_mode="HTML")
                elif kind == "video":
                    await bot.send_video(chat_id, fid, caption=caption, parse_mode="HTML")
                else:
                    await bot.send_photo(chat_id, fid, caption=caption, parse_mode="HTML")
            except Exception:
                return
        else:
            media: list[InputMediaPhoto | InputMediaDocument | InputMediaVideo] = []
            first_kind, first_fid = unpack_media_item(str(photos[0]))
            if first_kind == "doc":
                media.append(InputMediaDocument(media=first_fid, caption=caption, parse_mode="HTML"))
            elif first_kind == "video":
                media.append(InputMediaVideo(media=first_fid, caption=caption, parse_mode="HTML"))
            else:
                media.append(InputMediaPhoto(media=first_fid, caption=caption, parse_mode="HTML"))
            for raw in photos[1:]:
                kind, fid = unpack_media_item(str(raw))
                if kind == "doc":
                    media.append(InputMediaDocument(media=fid))
                elif kind == "video":
                    media.append(InputMediaVideo(media=fid))
                else:
                    media.append(InputMediaPhoto(media=fid))
            try:
                await bot.send_media_group(chat_id, media)
            except Exception:
                return
    else:
        try:
            await bot.send_message(chat_id, f"📋 <b>АНКЕТА #{form_id}</b>\n\n{details}", parse_mode="HTML")
        except Exception:
            return
    try:
        await bot.send_message(chat_id, "Выберите действие:", reply_markup=reply_markup)
    except Exception:
        pass


def _format_request_summary(req) -> str:
    """Format access request summary for list."""
    status_emoji = {
        "PENDING": "⏳",
        "APPROVED": "✅",
        "REJECTED": "❌"
    }
    
    emoji = status_emoji.get(req.status, "❓")
    return (
        f"{emoji} <b>ID: {req.user_id}</b>\n"
        f"   Статус: <b>{req.status}</b>\n"
        f"   📅 Создана: {req.created_at.strftime('%d.%m.%Y %H:%M')}"
    )


def _format_request_details(req) -> str:
    """Format detailed access request information."""
    status_emoji = {
        "PENDING": "⏳",
        "APPROVED": "✅", 
        "REJECTED": "❌"
    }

    status_key = str(getattr(req, "status", "")).split(".")[-1]
    emoji = status_emoji.get(status_key, "❓")
    created_at = getattr(req, "created_at", None)
    return (
        f"{emoji} <b>ЗАЯВКА #{req.user_id}</b>\n"
        f"Статус: <b>{format_access_status(getattr(req, 'status', None))}</b>\n\n"
        f"📊 <b>Информация:</b>\n"
        f"User ID: <code>{req.user_id}</code>\n"
        f"📅 Создана: {(created_at.strftime('%d.%m.%Y %H:%M') if created_at else '—')}"
    )


async def _send_next_access_request(message_or_bot: Message | object, chat_id: int, session: AsyncSession) -> None:
    pending_cnt = await count_pending_access_requests(session)
    req = await get_next_pending_access_request(session)
    if not req:
        try:
            await message_or_bot.send_message(chat_id, "✅ Очередь заявок пустая.", reply_markup=kb_dev_main_inline())
        except TelegramNetworkError:
            return
        return
    # best effort: load user info
    u = await get_user_by_id(session, int(req.user_id))
    uname = f"@{u.username}" if u and u.username else "—"
    name = f"{(u.first_name or '') if u else ''} {(u.last_name or '') if u else ''}".strip() or "—"
    tg_id = u.tg_id if u else "—"
    try:
        await message_or_bot.send_message(
            chat_id,
            "🧾 <b>Заявка на доступ</b>\n"
            f"- tg_id: <code>{tg_id}</code>\n"
            f"- username: {uname}\n"
            f"- name: {name}\n"
            f"\nВ очереди: <b>{pending_cnt}</b>",
            reply_markup=kb_access_request(int(tg_id)) if tg_id != "—" else kb_dev_main_inline(),
        )
    except TelegramNetworkError:
        return


@router.callback_query(F.data == "dev:groups:add")
async def dev_groups_add_cb(cq: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not cq.from_user:
        return
    if cq.from_user.id not in settings.developer_id_set:
        await cq.answer("Нет прав", show_alert=True)
        return
    await cq.answer()
    await state.set_state(DeveloperStates.groups_add)
    if cq.message:
        try:
            await cq.message.edit_text(
                "Введите chat_id группы (пример: <code>-1001234567890</code>)\n"
                "Можно добавить название после пробела.",
                reply_markup=kb_dev_groups_actions(),
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise


@router.message(DeveloperStates.groups_add, F.text)
async def dev_groups_add_finish(message: Message, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not message.from_user:
        return
    if message.from_user.id not in settings.developer_id_set:
        return
    txt = (message.text or "").strip()
    if not txt:
        return
    parts = txt.split(maxsplit=1)
    try:
        chat_id = int(parts[0])
    except Exception:
        await message.answer("Нужно число (chat_id). Пример: <code>-1001234567890</code>")
        return
    title = parts[1].strip() if len(parts) > 1 else None
    await create_forward_group(session, chat_id=chat_id, title=title)

    data = await state.get_data()
    req_bind_tg_id = data.get("req_bind_tg_id")
    if req_bind_tg_id:
        # continue request onboarding flow
        groups = await list_forward_groups(session)
        await state.set_state(DeveloperStates.reqs_list)
        await state.update_data(req_bind_tg_id=None)
        await message.answer(
            f"🏷 <b>Привязка группы</b> для <code>{int(req_bind_tg_id)}</code>\n\nВыберите группу:",
            reply_markup=kb_dev_req_pick_forward_group(tg_id=int(req_bind_tg_id), groups=groups),
        )
        return

    await state.set_state(DeveloperStates.groups_menu)
    await message.answer("✅ Добавлено/обновлено.")
    await _render_groups_menu(message, session)


@router.callback_query(F.data == "dev:groups:del")
async def dev_groups_del_cb(cq: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not cq.from_user:
        return
    if cq.from_user.id not in settings.developer_id_set:
        await cq.answer("Нет прав", show_alert=True)
        return
    await cq.answer()
    await state.set_state(DeveloperStates.groups_delete)
    if cq.message:
        await cq.message.edit_text("Введите ID группы для удаления (пример: <code>1</code>)", reply_markup=kb_dev_groups_actions())


@router.message(DeveloperStates.groups_delete, F.text)
async def dev_groups_del_finish(message: Message, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not message.from_user:
        return
    if message.from_user.id not in settings.developer_id_set:
        return
    txt = (message.text or "").strip()
    if not txt.isdigit():
        await message.answer("Нужно число (ID группы). Пример: <code>1</code>")
        return
    ok = await delete_forward_group(session, int(txt))
    await state.set_state(DeveloperStates.groups_menu)
    await message.answer("✅ Удалено." if ok else "Не найдено.")
    await _render_groups_menu(message, session)


@router.callback_query(F.data == "dev:groups:check")
async def dev_groups_check_all_cb(cq: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if not cq.from_user:
        return
    if cq.from_user.id not in settings.developer_id_set:
        await cq.answer("Нет прав", show_alert=True)
        return
    groups = await list_forward_groups(session)
    if not groups:
        await cq.answer("Список пуст", show_alert=True)
        return
    for g in groups:
        await _check_forward_group(cq.bot, group_id=int(g.id), chat_id=int(g.chat_id), session=session)
    await cq.answer("Готово")
    await _render_groups_menu(cq, session)


@router.callback_query(F.data.startswith("dev:group:open:"))
async def dev_group_open_cb(cq: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if not cq.from_user:
        return
    if cq.from_user.id not in settings.developer_id_set:
        await cq.answer("Нет прав", show_alert=True)
        return
    try:
        group_id = int((cq.data or "").split(":")[-1])
    except Exception:
        await cq.answer("Некорректная кнопка", show_alert=True)
        return
    g = await get_forward_group_by_id(session, group_id)
    if not g:
        await cq.answer("Группа не найдена", show_alert=True)
        return
    status = "✅" if getattr(g, "is_confirmed", False) else "❌"
    title = getattr(g, "title", None) or "—"
    last = getattr(g, "last_checked_at", None)
    last_s = last.strftime("%d.%m.%Y %H:%M") if last else "—"
    text = (
        "👥 <b>Группа пересылки</b>\n\n"
        f"- id: <code>{g.id}</code>\n"
        f"- chat_id: <code>{g.chat_id}</code>\n"
        f"- title: {title}\n"
        f"- status: {status}\n"
        f"- last_check: {last_s}"
    )
    await cq.answer()
    if cq.message:
        await cq.message.edit_text(text, reply_markup=kb_dev_group_open(group_id))


@router.callback_query(F.data.startswith("dev:group:check:"))
async def dev_group_check_one_cb(cq: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if not cq.from_user:
        return
    if cq.from_user.id not in settings.developer_id_set:
        await cq.answer("Нет прав", show_alert=True)
        return
    try:
        group_id = int((cq.data or "").split(":")[-1])
    except Exception:
        await cq.answer("Некорректная кнопка", show_alert=True)
        return
    g = await get_forward_group_by_id(session, group_id)
    if not g:
        await cq.answer("Группа не найдена", show_alert=True)
        return
    ok, _ = await _check_forward_group(cq.bot, group_id=int(g.id), chat_id=int(g.chat_id), session=session)
    await cq.answer("✅ Ок" if ok else "❌ Нет доступа", show_alert=True)
    await dev_group_open_cb(cq, session, settings)


@router.callback_query(F.data.startswith("dev:group:del:"))
async def dev_group_delete_one_cb(cq: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if not cq.from_user:
        return
    if cq.from_user.id not in settings.developer_id_set:
        await cq.answer("Нет прав", show_alert=True)
        return
    try:
        group_id = int((cq.data or "").split(":")[-1])
    except Exception:
        await cq.answer("Некорректная кнопка", show_alert=True)
        return
    ok = await delete_forward_group(session, group_id)
    await cq.answer("✅ Удалено" if ok else "Не найдено", show_alert=True)
    await _render_groups_menu(cq, session)


@router.callback_query(F.data.startswith("dev:user_group:"))
async def dev_user_group_pick_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not cq.from_user:
        return
    if cq.from_user.id not in settings.developer_id_set:
        await cq.answer("Нет прав", show_alert=True)
        return
    try:
        tg_id = int((cq.data or "").split(":")[-1])
    except Exception:
        await cq.answer("Некорректная кнопка", show_alert=True)
        return
    u = await get_user_by_tg_id(session, tg_id)
    if not u:
        await cq.answer("Пользователь не найден", show_alert=True)
        return
    groups = await list_forward_groups(session)
    await cq.answer()
    await state.set_state(DeveloperStates.groups_bind_pick)
    await state.update_data(current_user_id=tg_id)
    cur = "—"
    if getattr(u, "forward_group_id", None):
        g = await get_forward_group_by_id(session, int(u.forward_group_id))
        if g:
            cur = f"#{g.id} <code>{g.chat_id}</code> {getattr(g, 'title', None) or '—'}"
    text = (
        f"🏷 <b>Группа пересылки</b> для <code>{tg_id}</code>\n\n"
        f"Текущая: {cur}\n\n"
        "Выберите группу:" if groups else "Сначала добавьте группы в меню 'Группы'."
    )
    if cq.message:
        await cq.message.edit_text(text, reply_markup=kb_dev_pick_forward_group(tg_id=tg_id, groups=groups, include_skip=False))


@router.callback_query(F.data.startswith("dev:user_group_set:"))
async def dev_user_group_set_cb(cq: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if not cq.from_user:
        return
    if cq.from_user.id not in settings.developer_id_set:
        await cq.answer("Нет прав", show_alert=True)
        return
    parts = (cq.data or "").split(":")
    if len(parts) < 4:
        await cq.answer("Некорректная кнопка", show_alert=True)
        return
    tg_id = int(parts[2])
    val = parts[3]
    u = await get_user_by_tg_id(session, tg_id)
    if not u:
        await cq.answer("Пользователь не найден", show_alert=True)
        return
    if val == "NONE":
        await set_user_forward_group(session, u.id, None)
    else:
        try:
            gid = int(val)
        except Exception:
            await cq.answer("Некорректная группа", show_alert=True)
            return
        await set_user_forward_group(session, u.id, gid)

    await session.commit()

    await cq.answer("Сохранено")

    # If this was called from approve-flow prompt, continue queue
    if cq.message and cq.message.text and "🏷 <b>Привязка группы" in cq.message.text:
        await _send_next_access_request(cq.bot, cq.message.chat.id, session)
        return

    # Otherwise return to user view
    # Reload user to reflect new group binding
    u = await get_user_by_tg_id(session, tg_id)
    group_line = "—"
    if u and getattr(u, "forward_group_id", None):
        g = await get_forward_group_by_id(session, int(u.forward_group_id))
        if g:
            title = getattr(g, "title", None) or "—"
            group_line = f"#{g.id} <code>{g.chat_id}</code> {title}"
    details = _format_user_line(u, group_line) if u else "Пользователь не найден"
    if cq.message:
        await cq.message.edit_text(
            f"👤 <b>ПОЛЬЗОВАТЕЛЬ #{tg_id}</b>\n\n{details}\n\nВыберите действие:",
            reply_markup=kb_dev_user_actions(tg_id),
        )


@router.callback_query(F.data.startswith("dev:user_group_skip:"))
async def dev_user_group_skip_cb(cq: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if not cq.from_user:
        return
    if cq.from_user.id not in settings.developer_id_set:
        await cq.answer("Нет прав", show_alert=True)
        return
    await cq.answer("Ок")
    if cq.message and cq.message.text and "🏷 <b>Привязка группы" in cq.message.text:
        await _send_next_access_request(cq.bot, cq.message.chat.id, session)


@router.callback_query(AccessRequestCb.filter())
async def dev_access_request_decision(
    cq: CallbackQuery,
    callback_data: AccessRequestCb,
    session: AsyncSession,
    state: FSMContext,
    settings: Settings,
) -> None:
    if not cq.from_user:
        return
    if cq.from_user.id not in settings.developer_id_set:
        await cq.answer("Нет прав", show_alert=True)
        return

    target_tg_id = int(callback_data.tg_id)
    target_user = await get_user_by_tg_id(session, target_tg_id)
    if not target_user:
        await cq.answer("Пользователь не найден", show_alert=True)
        return

    dev_user = await get_user_by_tg_id(session, cq.from_user.id)
    processed_by_user_id = dev_user.id if dev_user else None

    if callback_data.action == "approve":
        await set_access_request_status(
            session,
            target_user_id=target_user.id,
            status=AccessRequestStatus.APPROVED,
            processed_by_user_id=processed_by_user_id,
        )
        await cq.answer("Ок")
        await state.update_data(req_target_tg_id=target_tg_id)
        if cq.message:
            await cq.message.edit_text(
                f"✅ Одобрено: <code>{target_tg_id}</code>\n\nВыберите роль:",
                reply_markup=kb_dev_req_pick_role(target_tg_id),
            )
        return

    if callback_data.action == "reject":
        await set_access_request_status(
            session,
            target_user_id=target_user.id,
            status=AccessRequestStatus.REJECTED,
            processed_by_user_id=processed_by_user_id,
        )
        try:
            await cq.bot.send_message(target_tg_id, "❌ Доступ отклонён. Если это ошибка — отправьте заявку повторно через /start")
        except TelegramNetworkError:
            pass
        except Exception:
            pass
        await cq.answer("Отклонено")
        if cq.message:
            await cq.message.edit_text(
                f"❌ Отклонено: <code>{target_tg_id}</code>\n\nОткрываю следующую заявку…",
                reply_markup=kb_dev_main_inline(),
            )
        await _send_next_access_request(cq.bot, cq.message.chat.id if cq.message else cq.from_user.id, session)
        return

    await cq.answer("Неизвестное действие", show_alert=True)


@router.callback_query(F.data.startswith("dev:req_back_role:"))
async def dev_req_back_role_cb(cq: CallbackQuery) -> None:
    await cq.answer()
    try:
        tg_id = int(cq.data.split(":")[-1])
    except Exception:
        return
    if cq.message:
        await cq.message.edit_text(
            f"Выберите роль для <code>{tg_id}</code>:",
            reply_markup=kb_dev_req_pick_role(tg_id),
        )


@router.callback_query(F.data.startswith("dev:req_set_role:"))
async def dev_req_set_role_cb(cq: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if not cq.from_user:
        return
    if cq.from_user.id not in settings.developer_id_set:
        await cq.answer("Нет прав", show_alert=True)
        return
    parts = (cq.data or "").split(":")
    if len(parts) < 4:
        await cq.answer("Некорректная кнопка", show_alert=True)
        return
    tg_id = int(parts[-2])
    role = parts[-1].upper()
    if role not in {"DROP_MANAGER", "TEAM_LEAD"}:
        await cq.answer("Некорректная роль", show_alert=True)
        return
    await set_user_role(session, tg_id, UserRole(role))
    await session.commit()
    await cq.answer("Ок")
    if cq.message:
        if role == "TEAM_LEAD":
            await cq.message.edit_text(
                f"👑 Роль: TEAM_LEAD для <code>{tg_id}</code>\n\nВыберите источник:",
                reply_markup=kb_dev_req_pick_team_lead_source(tg_id),
            )
        else:
            await cq.message.edit_text(
                f"🎯 Роль: DROP_MANAGER для <code>{tg_id}</code>\n\nВыберите источник:",
                reply_markup=kb_dev_req_pick_dm_source(tg_id),
            )


@router.callback_query(F.data.startswith("dev:req_set_tl_source:"))
async def dev_req_set_tl_source_cb(cq: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if not cq.from_user:
        return
    if cq.from_user.id not in settings.developer_id_set:
        await cq.answer("Нет прав", show_alert=True)
        return
    parts = (cq.data or "").split(":")
    if len(parts) < 4:
        await cq.answer("Некорректная кнопка", show_alert=True)
        return
    tg_id = int(parts[-2])
    src = parts[-1].upper()
    if src not in {"TG", "FB"}:
        await cq.answer("Некорректный источник", show_alert=True)
        return
    await add_team_lead(session, tg_id, src)
    await session.commit()
    try:
        await cq.bot.send_message(tg_id, "✅ Доступ одобрен. Вам выдана роль: Тим-лид. Нажмите /start")
    except Exception:
        pass
    await cq.answer("Сохранено")
    if cq.message:
        await cq.message.edit_text("✅ Готово. Открываю следующую заявку…", reply_markup=kb_dev_main_inline())
        await _send_next_access_request(cq.bot, cq.message.chat.id, session)


@router.callback_query(F.data.startswith("dev:req_back_dm_source:"))
async def dev_req_back_dm_source_cb(cq: CallbackQuery) -> None:
    await cq.answer()
    try:
        tg_id = int(cq.data.split(":")[-1])
    except Exception:
        return
    if cq.message:
        await cq.message.edit_text(
            f"Выберите источник для <code>{tg_id}</code>:",
            reply_markup=kb_dev_req_pick_dm_source(tg_id),
        )


@router.callback_query(F.data.startswith("dev:req_set_dm_source:"))
async def dev_req_set_dm_source_cb(cq: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if not cq.from_user:
        return
    if cq.from_user.id not in settings.developer_id_set:
        await cq.answer("Нет прав", show_alert=True)
        return
    parts = (cq.data or "").split(":")
    if len(parts) < 4:
        await cq.answer("Некорректная кнопка", show_alert=True)
        return
    tg_id = int(parts[-2])
    src = parts[-1].upper()
    if src not in {"TG", "FB"}:
        await cq.answer("Некорректный источник", show_alert=True)
        return
    u = await get_user_by_tg_id(session, tg_id)
    if not u:
        await cq.answer("Пользователь не найден", show_alert=True)
        return
    u.manager_source = src
    await session.commit()
    groups = await list_forward_groups(session)
    await cq.answer("Ок")
    if not cq.message:
        return
    if not groups:
        await cq.message.edit_text(
            "Сначала добавьте группы в меню 'Группы'.",
            reply_markup=kb_dev_main_inline(),
        )
        await _send_next_access_request(cq.bot, cq.message.chat.id, session)
        return
    await cq.message.edit_text(
        f"🏷 <b>Привязка группы</b> для <code>{tg_id}</code>\n\nВыберите группу:",
        reply_markup=kb_dev_req_pick_forward_group(tg_id=tg_id, groups=groups),
    )


@router.callback_query(F.data.startswith("dev:req_group_add:"))
async def dev_req_group_add_cb(cq: CallbackQuery, state: FSMContext, settings: Settings) -> None:
    if not cq.from_user:
        return
    if cq.from_user.id not in settings.developer_id_set:
        await cq.answer("Нет прав", show_alert=True)
        return
    try:
        tg_id = int(cq.data.split(":")[-1])
    except Exception:
        await cq.answer("Некорректная кнопка", show_alert=True)
        return
    await cq.answer()
    await state.set_state(DeveloperStates.groups_add)
    await state.update_data(req_bind_tg_id=tg_id)
    if cq.message:
        await cq.message.edit_text(
            "Введите chat_id группы (пример: <code>-1001234567890</code>)\n"
            "Можно добавить название после пробела.",
            reply_markup=kb_dev_groups_actions(),
        )


@router.message(F.text == "Заявки")
async def dev_requests(message: Message, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    # Ignore group messages
    if message.chat.type in ['group', 'supergroup']:
        return
    if not message.from_user:
        return
    if message.from_user.id not in settings.developer_id_set:
        return
    
    requests = await list_all_access_requests(session)
    if not requests:
        await message.answer("📝 <b>Заявок нет</b>", reply_markup=kb_dev_back_main_inline())
        return
    
    await state.clear()
    await state.set_state(DeveloperStates.reqs_list)
    await state.update_data(requests=requests)
    
    # Показываем список заявок с красивым форматированием
    text, inline_kb = kb_dev_requests_list_beautiful(requests)
    
    if len(text) <= 3500:
        await message.answer(text, reply_markup=inline_kb)
    else:
        await message.answer("📝 <b>ЗАЯВКИ НА ДОСТУП</b>", reply_markup=inline_kb)
        # Отправляем по частям
        chunk = []
        size = 0
        for line in text.split("\n")[2:]:  # Пропускаем заголовок
            if size + len(line) + 1 > 3500:
                await message.answer("\n".join(chunk))
                chunk = []
                size = 0
            chunk.append(line)
            size += len(line) + 1
        if chunk:
            await message.answer("\n".join(chunk))


@router.message(DeveloperStates.reqs_list, F.text)
async def dev_requests_select(message: Message, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    # Ignore group messages
    if message.chat.type in ['group', 'supergroup']:
        return
    if not message.from_user:
        return
    if message.from_user.id not in settings.developer_id_set:
        return
    
    txt = (message.text or "").strip()
    
    if not txt.isdigit():
        await message.answer("Нужно число (ID заявки). Пример: <code>123</code>")
        return
    
    req_id = int(txt)
    data = await state.get_data()
    requests = data.get("requests", [])
    
    # Ищем заявку в списке
    req = next((r for r in requests if r.user_id == req_id), None)
    if not req:
        await message.answer("Заявка с таким ID не найдена. Попробуйте еще раз:")
        return
    
    await state.set_state(DeveloperStates.req_view)
    await state.update_data(current_req_id=req_id)
    
    # Показываем детали заявки
    details = _format_request_details(req)
    await message.answer(
        f"📝 <b>ЗАЯВКА #{req_id}</b>\n\n{details}\n\n"
        "Выберите действие:",
        reply_markup=kb_dev_req_actions(req_id)
    )


@router.callback_query(F.data == "dev:back_to_reqs")
async def dev_back_to_reqs(cq: CallbackQuery, state: FSMContext) -> None:
    await cq.answer()
    data = await state.get_data()
    requests = data.get("requests", [])
    
    if not requests:
        await state.clear()
        if cq.message:
            await cq.message.edit_text("Список заявок пуст.", reply_markup=kb_dev_back_main_inline())
        return
    
    await state.set_state(DeveloperStates.reqs_list)
    
    # Показываем список снова с красивым форматированием
    text, inline_kb = kb_dev_requests_list_beautiful(requests)
    
    if len(text) <= 3500:
        await cq.message.edit_text(text, reply_markup=inline_kb)
    else:
        await cq.message.edit_text("📝 <b>ЗАЯВКИ НА ДОСТУП</b>", reply_markup=inline_kb)
        # Отправляем по частям
        chunk = []
        size = 0
        for line in text.split("\n")[2:]:
            if size + len(line) + 1 > 3500:
                await cq.message.answer("\n".join(chunk))
                chunk = []
                size = 0
            chunk.append(line)
            size += len(line) + 1
        if chunk:
            await cq.message.answer("\n".join(chunk))


@router.message(F.text == "Пользователи")
async def dev_users(message: Message, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    # Ignore group messages
    if message.chat.type in ['group', 'supergroup']:
        return
    if not message.from_user:
        return
    if message.from_user.id not in settings.developer_id_set:
        return
    
    users = await list_users(session)
    if not users:
        await message.answer("👥 <b>Пользователей нет</b>", reply_markup=kb_dev_back_main_inline())
        return
    
    await state.clear()
    await state.set_state(DeveloperStates.users_list)
    await state.update_data(users=users)
    
    # Показываем список пользователей с красивым форматированием
    text, inline_kb = kb_dev_users_list_beautiful(users)
    
    if len(text) <= 3500:
        await message.answer(text, reply_markup=inline_kb)
    else:
        await message.answer("👥 <b>ПОЛЬЗОВАТЕЛИ СИСТЕМЫ</b>", reply_markup=inline_kb)
        # Отправляем по частям
        chunk = []
        size = 0
        for line in text.split("\n")[2:]:
            if size + len(line) + 1 > 3500:
                await message.answer("\n".join(chunk))
                chunk = []
                size = 0
            chunk.append(line)
            size += len(line) + 1
        if chunk:
            await message.answer("\n".join(chunk))


@router.message(DeveloperStates.users_list, F.text)
async def dev_users_select(message: Message, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    # Ignore group messages
    if message.chat.type in ['group', 'supergroup']:
        return
    if not message.from_user:
        return
    if message.from_user.id not in settings.developer_id_set:
        return
    
    txt = (message.text or "").strip()
    data = await state.get_data()
    users = data.get("users", [])

    user = None
    tg_id: int | None = None

    if txt.isdigit():
        tg_id = int(txt)
        user = next((u for u in users if int(getattr(u, "tg_id", 0) or 0) == tg_id), None)
        if not user:
            user = await get_user_by_tg_id(session, tg_id)
    else:
        uname = txt.lstrip("@").strip()
        if uname:
            user = next(
                (u for u in users if getattr(u, "username", None) and str(u.username).lower() == uname.lower()),
                None,
            )
            if not user:
                user = await get_user_by_username(session, uname)
        if user:
            tg_id = int(getattr(user, "tg_id", 0) or 0)

    if not user or not tg_id:
        await message.answer("Нужен tg_id или username (пример: <code>755213716</code> или <code>@username</code>)")
        return
    
    await state.set_state(DeveloperStates.user_view)
    await state.update_data(current_user_id=tg_id)
    
    # Показываем детали пользователя
    details = _format_user_line(user)
    await message.answer(
        f"👤 <b>ПОЛЬЗОВАТЕЛЬ #{tg_id}</b>\n\n{details}\n\n"
        "Выберите действие:",
        reply_markup=kb_dev_user_actions(tg_id)
    )


@router.callback_query(F.data == "dev:back_to_users")
async def dev_back_to_users(cq: CallbackQuery, state: FSMContext) -> None:
    await cq.answer()
    data = await state.get_data()
    users = data.get("users", [])
    
    if not users:
        await state.clear()
        if cq.message:
            await cq.message.edit_text("Список пользователей пуст.", reply_markup=kb_dev_back_main_inline())
        return
    
    await state.set_state(DeveloperStates.users_list)
    
    # Показываем список снова с красивым форматированием
    tls = data.get("team_lead_sources")
    if tls is None:
        text, inline_kb = kb_dev_users_list_beautiful(users)
    else:
        text, inline_kb = kb_dev_users_list_beautiful_with_sources(users, team_lead_sources=tls)
    
    if len(text) <= 3500:
        await cq.message.edit_text(text, reply_markup=inline_kb)
    else:
        await cq.message.edit_text("👥 <b>ПОЛЬЗОВАТЕЛИ СИСТЕМЫ</b>", reply_markup=inline_kb)
        # Отправляем по частям
        chunk = []
        size = 0
        for line in text.split("\n")[2:]:
            if size + len(line) + 1 > 3500:
                await cq.message.answer("\n".join(chunk))
                chunk = []
                size = 0
            chunk.append(line)
            size += len(line) + 1
        if chunk:
            await cq.message.answer("\n".join(chunk))


@router.message(F.text == "Анкеты")
async def dev_forms(message: Message, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    # Ignore group messages
    if message.chat.type in ['group', 'supergroup']:
        return
    if not message.from_user:
        return
    if message.from_user.id not in settings.developer_id_set:
        return
    
    data = await state.get_data()
    if not data.get("forms_period"):
        await state.update_data(forms_period="today")
    forms = await _load_forms_with_filter(session, state)
    if not forms:
        await message.answer("📋 <b>Анкет нет</b>", reply_markup=kb_dev_back_main_inline())
        return
    
    await state.clear()
    await state.set_state(DeveloperStates.forms_list)
    data = await state.get_data()
    await state.update_data(
        forms=forms,
        forms_period=(data.get("forms_period") or "today"),
        forms_created_from=data.get("forms_created_from"),
        forms_created_to=data.get("forms_created_to"),
    )
    
    # Показываем список анкет с красивым форматированием
    text, inline_kb = kb_dev_forms_list_beautiful(forms)
    
    if len(text) <= 3500:
        await message.answer(text, reply_markup=inline_kb)
    else:
        await message.answer("📋 <b>АНКЕТЫ СИСТЕМЫ</b>", reply_markup=inline_kb)
        # Отправляем по частям
        chunk = []
        size = 0
        for line in text.split("\n")[2:]:
            if size + len(line) + 1 > 3500:
                await message.answer("\n".join(chunk))
                chunk = []
                size = 0
            chunk.append(line)
            size += len(line) + 1
        if chunk:
            await message.answer("\n".join(chunk))


@router.message(DeveloperStates.forms_list, F.text)
async def dev_forms_select(message: Message, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    # Ignore group messages
    if message.chat.type in ['group', 'supergroup']:
        return
    if not message.from_user:
        return
    if message.from_user.id not in settings.developer_id_set:
        return
    
    txt = (message.text or "").strip()
    
    if not txt.isdigit():
        await message.answer("Нужно число (ID анкеты). Пример: <code>123</code>")
        return
    
    form_id = int(txt)
    data = await state.get_data()
    forms = data.get("forms", [])
    
    # Ищем анкету в списке
    form = next((f for f in forms if f.id == form_id), None)
    if not form:
        await message.answer("Анкета с таким ID не найдена. Попробуйте еще раз:")
        return
    
    await state.set_state(DeveloperStates.form_view)
    await state.update_data(current_form_id=form_id)
    
    await _send_form_details_with_actions(
        bot=message.bot,
        chat_id=int(message.chat.id),
        form=form,
        form_id=form_id,
        reply_markup=kb_dev_form_actions(form_id),
    )


@router.callback_query(F.data.startswith("dev:back_to_form:"))
async def dev_back_to_form_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not cq.from_user:
        return
    if cq.from_user.id not in settings.developer_id_set:
        await cq.answer("Нет прав", show_alert=True)
        return
    try:
        form_id = int((cq.data or "").split(":")[-1])
    except Exception:
        await cq.answer("Некорректная кнопка", show_alert=True)
        return

    form = await get_form(session, form_id)
    if not form:
        await cq.answer("Анкета не найдена", show_alert=True)
        return

    await cq.answer()
    await state.set_state(DeveloperStates.form_view)
    await state.update_data(current_form_id=form_id)
    if cq.message:
        chat_id = int(cq.message.chat.id)
    else:
        chat_id = int(cq.from_user.id)
    await _send_form_details_with_actions(
        bot=cq.bot,
        chat_id=chat_id,
        form=form,
        form_id=form_id,
        reply_markup=kb_dev_form_actions(form_id),
    )


@router.callback_query(F.data == "dev:back_to_forms")
async def dev_back_to_forms(cq: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    await cq.answer()
    data = await state.get_data()
    forms = data.get("forms", [])
    
    if not forms:
        await state.clear()
        if cq.message:
            await cq.message.edit_text("Список анкет пуст.", reply_markup=kb_dev_back_main_inline())
        return
    
    await state.set_state(DeveloperStates.forms_list)

    # Перезагружаем из БД с учетом фильтра, чтобы список всегда был актуальным
    if cq.message and (data.get("forms_period") or data.get("forms_created_from") or data.get("forms_created_to")):
        forms = await _load_forms_with_filter(session, state)
        await state.update_data(forms=forms)
    text, inline_kb = kb_dev_forms_list_beautiful(forms)
    
    if len(text) <= 3500:
        await cq.message.edit_text(text, reply_markup=inline_kb)
    else:
        await cq.message.edit_text("📋 <b>АНКЕТЫ СИСТЕМЫ</b>", reply_markup=inline_kb)
        # Отправляем по частям
        chunk = []
        size = 0
        for line in text.split("\n")[2:]:
            if size + len(line) + 1 > 3500:
                await cq.message.answer("\n".join(chunk))
                chunk = []
                size = 0
            chunk.append(line)
            size += len(line) + 1
        if chunk:
            await cq.message.answer("\n".join(chunk))


@router.callback_query(F.data.startswith("dev:edit_form:"))
async def dev_edit_form_start(cq: CallbackQuery, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not cq.from_user:
        return
    if cq.from_user.id not in settings.developer_id_set:
        await cq.answer("Нет прав", show_alert=True)
        return
    
    form_id = int(cq.data.split(":")[-1])
    form = await get_form(session, form_id)
    
    if not form:
        await cq.answer("Анкета не найдена", show_alert=True)
        return
    
    await cq.answer()
    await state.set_state(DeveloperStates.form_edit_field)
    await state.update_data(current_form_id=form_id)
    
    if cq.message:
        await cq.message.edit_text(
            f"✏️ <b>РЕДАКТИРОВАНИЕ АНКЕТЫ #{form_id}</b>\n\n"
            f"Выберите поле для изменения:",
            reply_markup=kb_dev_edit_form(form_id)
        )


@router.callback_query(F.data.startswith("dev:edit_form_field:"))
async def dev_edit_form_field(cq: CallbackQuery, state: FSMContext) -> None:
    if not cq.from_user:
        return
    
    parts = cq.data.split(":")
    form_id = int(parts[2])
    field = parts[3]
    
    await cq.answer()
    await state.update_data(edit_field=field)
    
    field_prompts = {
        "traffic_type": "Введите тип клиента (DIRECT/REFERRAL):",
        "phone": "Введите номер телефона:",
        "bank_name": "Введите название банка:",
        "password": "Введите пароль (5 цифр):",
        "comment": "Введите комментарий:",
        "status": "Введите статус (IN_PROGRESS/PENDING/APPROVED/REJECTED):"
    }
    
    prompt = field_prompts.get(field, "Введите новое значение:")
    
    if cq.message:
        await cq.message.edit_text(prompt)


@router.message(DeveloperStates.form_edit_field, F.text)
async def dev_save_form_field(message: Message, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    # Ignore group messages
    if message.chat.type in ['group', 'supergroup']:
        return
    if not message.from_user:
        return
    if message.from_user.id not in settings.developer_id_set:
        return
    
    data = await state.get_data()
    form_id = data.get("current_form_id")
    field = data.get("edit_field")
    
    if not form_id or not field:
        await state.clear()
        await message.answer("Ошибка состояния.", reply_markup=kb_dev_main_inline())
        return
    
    form = await get_form(session, form_id)
    if not form:
        await state.clear()
        await message.answer("Анкета не найдена.", reply_markup=kb_dev_main_inline())
        return
    
    # Обновляем поле
    new_value = message.text.strip()
    
    if field == "status":
        if new_value not in ["IN_PROGRESS", "PENDING", "APPROVED", "REJECTED"]:
            await message.answer("Неверный статус. Используйте: IN_PROGRESS/PENDING/APPROVED/REJECTED")
            return
        form.status = FormStatus(new_value)
    elif field == "traffic_type":
        if new_value not in ["DIRECT", "REFERRAL"]:
            await message.answer("Неверный тип клиента. Используйте: DIRECT/REFERRAL")
            return
        form.traffic_type = new_value
    else:
        setattr(form, field, new_value)
    
    await session.commit()
    
    await state.set_state(DeveloperStates.form_view)
    
    await message.answer("✅ <b>Поле обновлено!</b>")
    await _send_form_details_with_actions(
        bot=message.bot,
        chat_id=int(message.chat.id),
        form=form,
        form_id=form_id,
        reply_markup=kb_dev_form_actions(form_id),
    )


@router.message(F.text == "Статистика")
async def dev_stats(message: Message, session: AsyncSession, settings: Settings) -> None:
    # Ignore group messages
    if message.chat.type in ['group', 'supergroup']:
        return
    if not message.from_user:
        return
    if message.from_user.id not in settings.developer_id_set:
        return

    users = await list_users(session)
    stats = await get_form_counts_by_manager(session)
    
    lines = [_format_stats_header()]
    
    # Общая статистика по системе
    total_forms = 0
    total_in_progress = 0
    total_pending = 0
    total_approved = 0
    total_rejected = 0
    
    drop_managers = [u for u in users if u.role == UserRole.DROP_MANAGER]
    
    for u in drop_managers:
        cnts = stats.get(u.id, {})
        in_prog = cnts.get(FormStatus.IN_PROGRESS, 0)
        pending = cnts.get(FormStatus.PENDING, 0)
        approved = cnts.get(FormStatus.APPROVED, 0)
        rejected = cnts.get(FormStatus.REJECTED, 0)
        total = in_prog + pending + approved + rejected
        
        total_forms += total
        total_in_progress += in_prog
        total_pending += pending
        total_approved += approved
        total_rejected += rejected
    
    # Общая эффективность
    total_completed = total_approved + total_rejected
    total_efficiency = round((total_approved / total_completed * 100) if total_completed > 0 else 0, 1)
    
    lines.append(f"📈 <b>Общая статистика:</b>")
    lines.append(f"   🔸 Дроп-менеджеры: <b>{len(drop_managers)}</b>")
    lines.append(f"   🔸 Всего анкет: <b>{total_forms}</b>")
    lines.append(f"   🔸 В работе: <b>{total_in_progress}</b>")
    lines.append(f"   🔸 На проверке: <b>{total_pending}</b>")
    lines.append(f"   🔸 Одобрено: <b>{total_approved}</b>")
    lines.append(f"   🔸 Отклонено: <b>{total_rejected}</b>")
    lines.append(f"   🔸 Общая эффективность: <b>{total_efficiency}%</b>")
    
    lines.append(f"\n🎯 <b>Статистика по менеджерам:</b>")
    
    if not drop_managers:
        lines.append("   — пока нет дроп‑менеджеров.")
    else:
        # Сортируем по общему количеству анкет
        sorted_managers = sorted(drop_managers, key=lambda u: sum(stats.get(u.id, {}).values()), reverse=True)
        
        for i, u in enumerate(sorted_managers, 1):
            cnts = stats.get(u.id, {})
            lines.append(f"\n{i}. {_format_user_stats(u, cnts)}")

    await message.answer("\n".join(lines), reply_markup=kb_dev_back_main_inline())


@router.callback_query(F.data.startswith("dev:select_user:"))
async def dev_select_user_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not cq.from_user:
        return
    if cq.from_user.id not in settings.developer_id_set:
        await cq.answer("Нет прав", show_alert=True)
        return
    
    tg_id = int(cq.data.split(":")[-1])
    user = await get_user_by_tg_id(session, tg_id)
    if not user:
        await cq.answer("Пользователь не найден", show_alert=True)
        return
    
    await cq.answer()
    await state.set_state(DeveloperStates.user_view)
    await state.update_data(current_user_id=tg_id)
    
    group_line = "—"
    if getattr(user, "forward_group_id", None):
        g = await get_forward_group_by_id(session, int(user.forward_group_id))
        if g:
            title = getattr(g, "title", None) or "—"
            group_line = f"#{g.id} <code>{g.chat_id}</code> {title}"

    # Показываем детали пользователя
    details = _format_user_line(user, group_line)
    if cq.message:
        await cq.message.edit_text(
            f"👤 <b>ПОЛЬЗОВАТЕЛЬ #{tg_id}</b>\n\n{details}\n\n"
            "Выберите действие:",
            reply_markup=kb_dev_user_actions(tg_id)
        )


@router.callback_query(F.data.startswith("dev:back_to_user:"))
async def dev_back_to_user_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not cq.from_user:
        return
    if cq.from_user.id not in settings.developer_id_set:
        await cq.answer("Нет прав", show_alert=True)
        return

    tg_id = int(cq.data.split(":")[-1])
    user = await get_user_by_tg_id(session, tg_id)
    if not user:
        await cq.answer("Пользователь не найден", show_alert=True)
        return

    await cq.answer()
    await state.set_state(DeveloperStates.user_view)
    await state.update_data(current_user_id=tg_id)

    group_line = "—"
    if getattr(user, "forward_group_id", None):
        g = await get_forward_group_by_id(session, int(user.forward_group_id))
        if g:
            title = getattr(g, "title", None) or "—"
            group_line = f"#{g.id} <code>{g.chat_id}</code> {title}"

    details = _format_user_line(user, group_line)
    if cq.message:
        try:
            await cq.message.edit_text(
                f"👤 <b>ПОЛЬЗОВАТЕЛЬ #{tg_id}</b>\n\n{details}\n\nВыберите действие:",
                reply_markup=kb_dev_user_actions(tg_id),
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                raise


@router.callback_query(F.data.startswith("dev:select_form:"))
async def dev_select_form_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not cq.from_user:
        return
    if cq.from_user.id not in settings.developer_id_set:
        await cq.answer("Нет прав", show_alert=True)
        return
    
    form_id = int(cq.data.split(":")[-1])
    data = await state.get_data()
    forms = data.get("forms", [])
    
    # Ищем анкету в списке
    form = next((f for f in forms if f.id == form_id), None)
    if not form:
        await cq.answer("Анкета не найдена", show_alert=True)
        return
    
    await cq.answer()
    await state.set_state(DeveloperStates.form_view)
    await state.update_data(current_form_id=form_id)
    
    if cq.message:
        chat_id = int(cq.message.chat.id)
    else:
        chat_id = int(cq.from_user.id)
    await _send_form_details_with_actions(
        bot=cq.bot,
        chat_id=chat_id,
        form=form,
        form_id=form_id,
        reply_markup=kb_dev_form_actions(form_id),
    )


@router.callback_query(F.data.startswith("dev:select_req:"))
async def dev_select_req_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not cq.from_user:
        return
    if cq.from_user.id not in settings.developer_id_set:
        await cq.answer("Нет прав", show_alert=True)
        return
    
    req_id = int(cq.data.split(":")[-1])
    data = await state.get_data()
    requests = data.get("requests", [])
    
    # Ищем заявку в списке
    req = next((r for r in requests if r.user_id == req_id), None)
    if not req:
        await cq.answer("Заявка не найдена", show_alert=True)
        return
    
    await cq.answer()
    await state.set_state(DeveloperStates.req_view)
    await state.update_data(current_req_id=req_id)
    
    # Показываем детали заявки
    details = _format_request_details(req)
    if cq.message:
        await cq.message.edit_text(
            f"📝 <b>ЗАЯВКА #{req_id}</b>\n\n{details}\n\n"
            "Выберите действие:",
            reply_markup=kb_dev_req_actions(req_id)
        )
@router.message(F.text == "Назад")
async def dev_back_button(message: Message, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    # Ignore group messages
    if message.chat.type in ['group', 'supergroup']:
        return
    if not message.from_user:
        return
    if message.from_user.id not in settings.developer_id_set:
        return
    
    current_state = await state.get_state()
    
    if current_state in [DeveloperStates.users_list, DeveloperStates.forms_list, DeveloperStates.reqs_list]:
        # Из списков возвращаем в главное меню
        await state.clear()
        pending_cnt = await count_pending_access_requests(session)
        m = await message.answer("...", reply_markup=ReplyKeyboardRemove())
        try:
            await m.delete()
        except Exception:
            pass
        await message.answer(
            f"👨‍💻 <b>Панель разработчика</b>\n\n"
            f"Заявок в очереди: <b>{pending_cnt}</b>",
            reply_markup=kb_dev_main_inline(),
        )
    elif current_state in [DeveloperStates.user_view, DeveloperStates.form_view, DeveloperStates.req_view]:
        # Из деталей возвращаем в соответствующий список
        if current_state == DeveloperStates.user_view:
            users = await list_users(session)
            await state.set_state(DeveloperStates.users_list)
            await state.update_data(users=users)
            
            # Показываем список снова с красивым форматированием
            text, inline_kb = kb_dev_users_list_beautiful(users)
            
            if len(text) <= 3500:
                await message.answer(text, reply_markup=inline_kb)
            else:
                await message.answer("👥 <b>ПОЛЬЗОВАТЕЛИ СИСТЕМЫ</b>", reply_markup=inline_kb)
                # Отправляем по частям
                chunk = []
                size = 0
                for line in text.split("\n")[2:]:
                    if size + len(line) + 1 > 3500:
                        await message.answer("\n".join(chunk), reply_markup=inline_kb)
                        chunk = []
                        size = 0
                    chunk.append(line)
                    size += len(line) + 1
                if chunk:
                    await message.answer("\n".join(chunk), reply_markup=inline_kb)
            
        elif current_state == DeveloperStates.form_view:
            data = await state.get_data()
            if not data.get("forms_period"):
                await state.update_data(forms_period="today")
            forms = await _load_forms_with_filter(session, state)
            await state.set_state(DeveloperStates.forms_list)
            await state.update_data(forms=forms)
            
            # Показываем список снова с красивым форматированием
            text, inline_kb = kb_dev_forms_list_beautiful(forms)
            
            if len(text) <= 3500:
                await message.answer(text, reply_markup=inline_kb)
            else:
                await message.answer("📋 <b>АНКЕТЫ СИСТЕМЫ</b>", reply_markup=inline_kb)
                # Отправляем по частям
                chunk = []
                size = 0
                for line in text.split("\n")[2:]:
                    if size + len(line) + 1 > 3500:
                        await message.answer("\n".join(chunk), reply_markup=inline_kb)
                        chunk = []
                        size = 0
                    chunk.append(line)
                    size += len(line) + 1
                if chunk:
                    await message.answer("\n".join(chunk), reply_markup=inline_kb)
            
        elif current_state == DeveloperStates.req_view:
            requests = await list_all_access_requests(session)
            await state.set_state(DeveloperStates.reqs_list)
            await state.update_data(requests=requests)
            
            # Показываем список снова с красивым форматированием
            text, inline_kb = kb_dev_requests_list_beautiful(requests)
            
            if len(text) <= 3500:
                await message.answer(text, reply_markup=inline_kb)
            else:
                await message.answer("📝 <b>ЗАЯВКИ НА ДОСТУП</b>", reply_markup=inline_kb)
                # Отправляем по частям
                chunk = []
                size = 0
                for line in text.split("\n")[2:]:
                    if size + len(line) + 1 > 3500:
                        await message.answer("\n".join(chunk), reply_markup=inline_kb)
                        chunk = []
                        size = 0
                    chunk.append(line)
                    size += len(line) + 1
                if chunk:
                    await message.answer("\n".join(chunk), reply_markup=inline_kb)
            
    elif current_state in [DeveloperStates.user_edit_field, DeveloperStates.form_edit_field, DeveloperStates.req_edit_field]:
        # Из редактирования возвращаем к деталям
        data = await state.get_data()
        
        if current_state == DeveloperStates.user_edit_field:
            tg_id = data.get("current_user_id")
            if tg_id:
                user = await get_user_by_tg_id(session, tg_id)
                if user:
                    await state.set_state(DeveloperStates.user_view)
                    group_line = "—"
                    if getattr(user, "forward_group_id", None):
                        g = await get_forward_group_by_id(session, int(user.forward_group_id))
                        if g:
                            title = getattr(g, "title", None) or "—"
                            group_line = f"#{g.id} <code>{g.chat_id}</code> {title}"
                    details = _format_user_line(user, group_line)
                    await message.answer(
                        f"👤 <b>ПОЛЬЗОВАТЕЛЬ #{tg_id}</b>\n\n{details}",
                        reply_markup=kb_dev_user_actions(tg_id)
                    )
                    return
        
        elif current_state == DeveloperStates.form_edit_field:
            form_id = data.get("current_form_id")
            if form_id:
                form = await get_form(session, form_id)
                if form:
                    await state.set_state(DeveloperStates.form_view)
                    await _send_form_details_with_actions(
                        bot=message.bot,
                        chat_id=int(message.chat.id),
                        form=form,
                        form_id=form_id,
                        reply_markup=kb_dev_form_actions(form_id),
                    )
                    return
        
        elif current_state == DeveloperStates.req_edit_field:
            req_id = data.get("current_req_id")
            if req_id:
                # For now, just go back to req_view since req editing isn't fully implemented
                requests = await list_all_access_requests(session)
                req = next((r for r in requests if r.user_id == req_id), None)
                if req:
                    await state.set_state(DeveloperStates.req_view)
                    details = _format_request_details(req)
                    await message.answer(
                        f"📝 <b>ЗАЯВКА #{req_id}</b>\n\n{details}",
                        reply_markup=kb_dev_req_actions(req_id)
                    )
                    return
        
        # Если что-то пошло не так, возвращаем в главное меню
        await state.clear()
        pending_cnt = await count_pending_access_requests(session)
        m = await message.answer("...", reply_markup=ReplyKeyboardRemove())
        try:
            await m.delete()
        except Exception:
            pass
        await message.answer(
            f"👨‍💻 <b>Панель разработчика</b>\n\n"
            f"Заявок в очереди: <b>{pending_cnt}</b>",
            reply_markup=kb_dev_main_inline(),
        )
        
    else:
        # Для всех остальных состояний возвращаем в главное меню
        await state.clear()
        pending_cnt = await count_pending_access_requests(session)
        m = await message.answer("...", reply_markup=ReplyKeyboardRemove())
        try:
            await m.delete()
        except Exception:
            pass
        await message.answer(
            f"👨‍💻 <b>Панель разработчика</b>\n\n"
            f"Заявок в очереди: <b>{pending_cnt}</b>",
            reply_markup=kb_dev_main_inline(),
        )


@router.callback_query(F.data.startswith("dev:select_user:"))
async def dev_select_user_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    if not cq.from_user:
        return
    if cq.from_user.id not in settings.developer_id_set:
        await cq.answer("Нет прав", show_alert=True)
        return
    
    tg_id = int(cq.data.split(":")[-1])
    data = await state.get_data()
    users = data.get("users", [])
    
    # Ищем пользователя в списке
    user = next((u for u in users if u.tg_id == tg_id), None)
    if not user:
        await cq.answer("Пользователь не найден", show_alert=True)
        return
    
    await cq.answer()
    await state.set_state(DeveloperStates.user_view)
    await state.update_data(current_user_id=tg_id)
    
    group_line = "—"
    if getattr(user, "forward_group_id", None):
        g = await get_forward_group_by_id(session, int(user.forward_group_id))
        if g:
            title = getattr(g, "title", None) or "—"
            group_line = f"#{g.id} <code>{g.chat_id}</code> {title}"

    # Показываем детали пользователя
    details = _format_user_line(user, group_line)
    if cq.message:
        await cq.message.edit_text(
            f"👤 <b>ПОЛЬЗОВАТЕЛЬ #{tg_id}</b>\n\n{details}\n\n"
            "Выберите действие:",
            reply_markup=kb_dev_user_actions(tg_id),
        )


@router.callback_query(F.data.startswith("dev:edit_req:"))
async def dev_edit_req_not_implemented_cb(cq: CallbackQuery) -> None:
    await cq.answer("Редактирование заявки пока не реализовано", show_alert=True)


@router.callback_query(F.data.startswith("dev:del_req:"))
async def dev_confirm_delete_req_cb(cq: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if not cq.from_user:
        return
    if cq.from_user.id not in settings.developer_id_set:
        await cq.answer("Нет прав", show_alert=True)
        return

    await cq.answer()
    tg_id = int(cq.data.split(":")[-1])
    ok = await delete_access_request_by_user_id(session, tg_id)
    await session.commit()
    pending_cnt = await count_pending_access_requests(session)

    if cq.message:
        if ok:
            await cq.message.edit_text(
                "✅ Заявка удалена.\n\n"
                "👨‍💻 <b>Панель разработчика</b>\n\n"
                f"Заявок в очереди: <b>{pending_cnt}</b>",
                reply_markup=kb_dev_main_inline(),
            )
        else:
            await cq.message.edit_text(
                "❌ Заявка не найдена.\n\n"
                "👨‍💻 <b>Панель разработчика</b>\n\n"
                f"Заявок в очереди: <b>{pending_cnt}</b>",
                reply_markup=kb_dev_main_inline(),
            )


@router.callback_query(F.data == "dev:cancel")
async def dev_cancel_cb(cq: CallbackQuery, session: AsyncSession, state: FSMContext, settings: Settings) -> None:
    await dev_back_to_main(cq, session, state, settings)


@router.callback_query(F.data.startswith("dev:del_form:"))
async def dev_confirm_delete_form_cb(cq: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if not cq.from_user:
        return
    if cq.from_user.id not in settings.developer_id_set:
        await cq.answer("Нет прав", show_alert=True)
        return
    
    await cq.answer()
    form_id = int(cq.data.split(":")[-1])
    ok = await delete_form(session, form_id)
    await session.commit()
    pending_cnt = await count_pending_access_requests(session)
    
    if cq.message:
        if ok:
            await cq.message.edit_text(
                "✅ Анкета удалена.\n\n"
                "👨‍💻 <b>Панель разработчика</b>\n\n"
                f"Заявок в очереди: <b>{pending_cnt}</b>",
                reply_markup=kb_dev_main_inline(),
            )
        else:
            await cq.message.edit_text(
                "❌ Анкета не найдена.\n\n"
                "👨‍💻 <b>Панель разработчика</b>\n\n"
                f"Заявок в очереди: <b>{pending_cnt}</b>",
                reply_markup=kb_dev_main_inline(),
            )


@router.callback_query(F.data.startswith("dev:del_user:"))
async def dev_confirm_delete_user_cb(cq: CallbackQuery, session: AsyncSession, settings: Settings) -> None:
    if not cq.from_user:
        return
    if cq.from_user.id not in settings.developer_id_set:
        await cq.answer("Нет прав", show_alert=True)
        return

    await cq.answer()
    tg_id = int(cq.data.split(":")[-1])
    ok = await delete_user_by_tg_id(session, tg_id)
    await session.commit()
    pending_cnt = await count_pending_access_requests(session)

    if cq.message:
        if ok:
            await cq.message.edit_text(
                "✅ Пользователь удалён.\n\n"
                "👨‍💻 <b>Панель разработчика</b>\n\n"
                f"Заявок в очереди: <b>{pending_cnt}</b>",
                reply_markup=kb_dev_main_inline(),
            )
        else:
            await cq.message.edit_text(
                "❌ Пользователь не найден.\n\n"
                "👨‍💻 <b>Панель разработчика</b>\n\n"
                f"Заявок в очереди: <b>{pending_cnt}</b>",
                reply_markup=kb_dev_main_inline(),
            )


