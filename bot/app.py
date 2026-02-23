from __future__ import annotations

import logging
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy import text

from bot.config import Settings
from bot.db import make_engine, make_sessionmaker
from bot.handlers.common import router as common_router
from bot.handlers.developer import router as developer_router
from bot.handlers.drop_manager import router as drop_router
from bot.handlers.team_lead import router as team_lead_router
from bot.handlers.wictory import router as wictory_router
from bot.logging_setup import setup_logging
from bot.middlewares import DBSessionMiddleware, GroupChatRestrictionMiddleware, LastPrivateMessageTrackerMiddleware
from bot.models import Base
from bot.keyboards import kb_dev_main_inline, kb_dm_main_inline, kb_dm_source_pick_inline, kb_pending_main, kb_team_lead_inline_main, kb_wictory_main_inline
from bot.models import UserRole
from bot.repositories import count_pending_forms, count_rejected_forms_by_user_id, get_active_shift, list_users


async def _init_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # Lightweight SQLite migrations (best-effort)
        try:
            res = await conn.execute(text("PRAGMA table_info(users)"))
            cols = {row[1] for row in res.fetchall()}
            if "manager_source" not in cols:
                await conn.execute(text("ALTER TABLE users ADD COLUMN manager_source VARCHAR(8)"))
        except Exception:
            # best effort: do not fail startup
            pass

        # Ensure team_leads table exists for older DBs
        try:
            res = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='team_leads'"))
            exists = res.fetchone() is not None
            if not exists:
                await conn.execute(
                    text(
                        "CREATE TABLE team_leads ("
                        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                        "tg_id BIGINT UNIQUE NOT NULL,"
                        "source VARCHAR(2) NOT NULL DEFAULT 'TG',"
                        "created_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                        ")"
                    )
                )
                await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_team_leads_tg_id ON team_leads (tg_id)"))
                await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_team_leads_source ON team_leads (source)"))
        except Exception:
            pass

        # Ensure shifts.comment_of_day exists for older DBs
        try:
            res = await conn.execute(text("PRAGMA table_info(shifts)"))
            cols = {row[1] for row in res.fetchall()}
            if "comment_of_day" not in cols:
                await conn.execute(text("ALTER TABLE shifts ADD COLUMN comment_of_day TEXT"))
            if "dialogs_count" not in cols:
                await conn.execute(text("ALTER TABLE shifts ADD COLUMN dialogs_count INTEGER"))
        except Exception:
            pass

        # Ensure forward_groups table exists for multi-group forwarding
        try:
            res = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='forward_groups'"))
            exists = res.fetchone() is not None
            if not exists:
                await conn.execute(
                    text(
                        "CREATE TABLE forward_groups ("
                        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                        "chat_id BIGINT UNIQUE NOT NULL,"
                        "title VARCHAR(128),"
                        "last_checked_at DATETIME,"
                        "is_confirmed INTEGER NOT NULL DEFAULT 0,"
                        "created_at DATETIME DEFAULT CURRENT_TIMESTAMP,"
                        "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP"
                        ")"
                    )
                )
                await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_forward_groups_chat_id ON forward_groups (chat_id)"))
        except Exception:
            pass

        # Ensure users.forward_group_id exists for multi-group forwarding
        try:
            res = await conn.execute(text("PRAGMA table_info(users)"))
            cols = {row[1] for row in res.fetchall()}
            if "forward_group_id" not in cols:
                await conn.execute(text("ALTER TABLE users ADD COLUMN forward_group_id INTEGER"))
                await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_users_forward_group_id ON users (forward_group_id)"))
        except Exception:
            pass

        # Ensure users.last_private_message_id/at exist for scheduled cleanup
        try:
            res = await conn.execute(text("PRAGMA table_info(users)"))
            cols = {row[1] for row in res.fetchall()}
            if "last_private_message_id" not in cols:
                await conn.execute(text("ALTER TABLE users ADD COLUMN last_private_message_id INTEGER"))
            if "last_private_message_at" not in cols:
                await conn.execute(text("ALTER TABLE users ADD COLUMN last_private_message_at DATETIME"))
        except Exception:
            pass

        # Ensure duplicate_reports table exists for TL duplicate reports
        try:
            res = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='duplicate_reports'"))
            exists = res.fetchone() is not None
            if not exists:
                await conn.execute(
                    text(
                        "CREATE TABLE duplicate_reports ("
                        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                        "manager_id INTEGER NOT NULL,"
                        "manager_username VARCHAR(64),"
                        "manager_source VARCHAR(8),"
                        "phone VARCHAR(32) NOT NULL,"
                        "bank_name VARCHAR(64) NOT NULL,"
                        "created_at DATETIME DEFAULT CURRENT_TIMESTAMP,"
                        "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,"
                        "FOREIGN KEY(manager_id) REFERENCES users (id)"
                        ")"
                    )
                )
                await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_duplicate_reports_manager_id ON duplicate_reports (manager_id)"))
                await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_duplicate_reports_manager_source ON duplicate_reports (manager_source)"))
                await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_duplicate_reports_phone ON duplicate_reports (phone)"))
                await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_duplicate_reports_bank_name ON duplicate_reports (bank_name)"))
                await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_duplicate_reports_created_at ON duplicate_reports (created_at)"))
        except Exception:
            pass

        # Ensure per-source bank condition fields exist for older DBs
        try:
            res = await conn.execute(text("PRAGMA table_info(bank_conditions)"))
            cols = {row[1] for row in res.fetchall()}
            if "instructions_tg" not in cols:
                await conn.execute(text("ALTER TABLE bank_conditions ADD COLUMN instructions_tg TEXT"))
            if "instructions_fb" not in cols:
                await conn.execute(text("ALTER TABLE bank_conditions ADD COLUMN instructions_fb TEXT"))
            if "required_screens_tg" not in cols:
                await conn.execute(text("ALTER TABLE bank_conditions ADD COLUMN required_screens_tg INTEGER"))
            if "required_screens_fb" not in cols:
                await conn.execute(text("ALTER TABLE bank_conditions ADD COLUMN required_screens_fb INTEGER"))
        except Exception:
            pass

        # Ensure forms.payment_done_at exists for one-time payment flow tracking
        try:
            res = await conn.execute(text("PRAGMA table_info(forms)"))
            cols = {row[1] for row in res.fetchall()}
            if "payment_done_at" not in cols:
                await conn.execute(text("ALTER TABLE forms ADD COLUMN payment_done_at DATETIME"))
                await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_forms_payment_done_at ON forms (payment_done_at)"))
        except Exception:
            pass


async def _run_daily_private_cleanup(*, bot: Bot, session_maker, hour: int = 3, minute: int = 0) -> None:
    tz = ZoneInfo("Europe/Kyiv")
    while True:
        now = datetime.now(tz)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target = target + timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())

        try:
            async with session_maker() as session:
                users = await list_users(session)
                for u in users:
                    try:
                        last_mid = int(getattr(u, "last_private_message_id", 0) or 0)
                        if last_mid <= 0:
                            continue
                        chat_id = int(getattr(u, "tg_id", 0) or 0)
                        if chat_id <= 0:
                            continue
                        # best-effort: delete a large tail that typically covers the last 24h of activity
                        start_mid = max(1, last_mid - 2500)
                        for mid in range(last_mid, start_mid - 1, -1):
                            try:
                                await bot.delete_message(chat_id=chat_id, message_id=mid)
                            except Exception:
                                pass
                            await asyncio.sleep(0.01)
                    except Exception:
                        continue
        except Exception:
            pass


async def main(settings: Settings) -> None:
    setup_logging(settings.log_level)
    log = logging.getLogger("bot")

    engine = make_engine(settings.db_url)
    await _init_db(engine)
    session_maker = make_sessionmaker(engine)

    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())

    dp.update.middleware(GroupChatRestrictionMiddleware())
    dp.update.middleware(DBSessionMiddleware(session_maker))
    dp.update.middleware(LastPrivateMessageTrackerMiddleware())

    dp.include_router(common_router)
    dp.include_router(developer_router)
    dp.include_router(team_lead_router)
    dp.include_router(wictory_router)
    dp.include_router(drop_router)

    # Best-effort greeting broadcast to all known users on each restart
    try:
        async with session_maker() as session:
            users = await list_users(session)
            for u in users:
                try:
                    rm = kb_pending_main()
                    if int(getattr(u, "tg_id", 0)) in settings.developer_id_set:
                        rm = kb_dev_main_inline()
                    elif u.role == UserRole.TEAM_LEAD:
                        live_cnt = await count_pending_forms(session)
                        rm = kb_team_lead_inline_main(live_count=live_cnt)
                    elif u.role == UserRole.DROP_MANAGER:
                        if not getattr(u, "manager_source", None):
                            rm = kb_dm_source_pick_inline()
                        else:
                            shift = await get_active_shift(session, u.id)
                            rejected_count = await count_rejected_forms_by_user_id(session, u.id) if shift else None
                            rm = kb_dm_main_inline(shift_active=bool(shift), rejected_count=rejected_count)
                    elif u.role == UserRole.WICTORY:
                        rm = kb_wictory_main_inline()
                    await bot.send_message(int(u.tg_id), "👋 Привет! Бот перезапущен и снова в работе.", reply_markup=rm)
                except (TelegramForbiddenError, TelegramBadRequest, TelegramNetworkError):
                    continue
                except Exception:
                    continue
    except Exception:
        pass

    log.info("Bot started")
    asyncio.create_task(_run_daily_private_cleanup(bot=bot, session_maker=session_maker, hour=3, minute=0))
    await dp.start_polling(bot, settings=settings)


