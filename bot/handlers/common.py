from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.exceptions import TelegramNetworkError
from aiogram.types import Message, ReplyKeyboardRemove
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.keyboards import (
    kb_access_request,
    kb_dev_main_inline,
    kb_dm_main_inline,
    kb_dm_source_pick_inline,
    kb_team_lead_inline_main,
    kb_wictory_main_inline,
)
from bot.models import UserRole
from bot.repositories import (
    count_pending_access_requests,
    count_pending_forms,
    count_rejected_forms_by_user_id,
    ensure_default_banks,
    get_active_shift,
    is_team_lead,
    upsert_access_request,
    upsert_user_from_tg,
)
from bot.middlewares import GroupMessageFilter

router = Router(name="common")
# Apply group message filter to all handlers in this router
router.message.filter(GroupMessageFilter())
log = logging.getLogger(__name__)


async def _render_start(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not message.from_user:
        return
    u = message.from_user

    user = await upsert_user_from_tg(session, u.id, u.username, u.first_name, u.last_name)

    m = await message.answer("...", reply_markup=ReplyKeyboardRemove())
    try:
        await m.delete()
    except Exception:
        pass

    # bootstrap roles from env
    if u.id in settings.developer_id_set:
        user.role = UserRole.DEVELOPER
    elif await is_team_lead(session, u.id):
        user.role = UserRole.TEAM_LEAD
    else:
        # avoid stale DB role granting dev access after env changes
        if user.role == UserRole.DEVELOPER:
            user.role = UserRole.PENDING

    await ensure_default_banks(session)

    if user.role == UserRole.DEVELOPER:
        pending_cnt = await count_pending_access_requests(session)
        await message.answer(
            f"👨‍💻 <b>Панель разработчика</b>\n\n"
            f"Заявок в очереди: <b>{pending_cnt}</b>",
            reply_markup=kb_dev_main_inline(),
        )
        return

    if user.role == UserRole.TEAM_LEAD:
        live_cnt = await count_pending_forms(session)
        await message.answer("Вы <b>Тим‑лид</b>.", reply_markup=kb_team_lead_inline_main(live_count=live_cnt))
        return

    if user.role == UserRole.DROP_MANAGER:
        if not user.manager_tag:
            await message.answer("Введите ваш <b>тег менеджера</b> (это спрашивается один раз):")
            return
        if not getattr(user, "manager_source", None):
            await message.answer(
                "Выберите источник (категория):",
                reply_markup=kb_dm_source_pick_inline(),
            )
            return
        shift = await get_active_shift(session, user.id)
        rejected_count = await count_rejected_forms_by_user_id(session, user.id) if shift else None
        await message.answer(
            f"Вы получили одобрение как <b>Дроп‑Менеджер</b> — ваш никнейм <b>{user.manager_tag}</b>.",
            reply_markup=kb_dm_main_inline(shift_active=bool(shift), rejected_count=rejected_count),
        )
        return

    if user.role == UserRole.WICTORY:
        await message.answer("Вы в меню <b>WICTORY</b>.", reply_markup=kb_wictory_main_inline())
        return

    if not settings.developer_id_set:
        log.warning("No DEVELOPER_IDS configured; cannot route access requests")
        return

    # PENDING / unknown → request access to developer(s)
    user.role = UserRole.PENDING
    is_new = await upsert_access_request(session, user.id)
    try:
        await message.answer(
            "⏳ Ваша заявка на доступ создана.\n"
            "Ожидайте апрува разработчика — после этого отправьте <b>/start</b> или напишите <b>Старт</b>.",
        )
    except TelegramNetworkError:
        return

    # notify developers only on a new request (or re-opened after being processed)
    if is_new:
        pending_cnt = await count_pending_access_requests(session)
        for dev_id in settings.developer_id_set:
            try:
                await message.bot.send_message(
                    dev_id,
                    "🆕 <b>Новая заявка на доступ</b>\n"
                    f"- id: <code>{u.id}</code>\n"
                    f"- username: @{u.username if u.username else '—'}\n"
                    f"- name: {(u.first_name or '')} {(u.last_name or '')}".strip()
                    + f"\n\nВ очереди сейчас: <b>{pending_cnt}</b>",
                    reply_markup=kb_access_request(u.id),
                )
            except TelegramNetworkError:
                continue
            except Exception:
                log.exception("Failed to notify developer %s", dev_id)


@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession, settings: Settings) -> None:
    # Ignore group messages
    if message.chat.type in ['group', 'supergroup']:
        return
    await _render_start(message, session, settings)


@router.message(F.text == "Старт")
async def start_button(message: Message, session: AsyncSession, settings: Settings) -> None:
    # Ignore group messages
    if message.chat.type in ['group', 'supergroup']:
        return
    await _render_start(message, session, settings)


@router.message(F.text == "Запросить доступ")
async def request_access_button(message: Message, session: AsyncSession, settings: Settings) -> None:
    # Ignore group messages
    if message.chat.type in ['group', 'supergroup']:
        return
    # same flow as /start for PENDING users
    await _render_start(message, session, settings)


