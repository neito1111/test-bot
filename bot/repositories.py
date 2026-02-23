from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import (
    AccessRequest,
    AccessRequestStatus,
    BankCondition,
    DuplicateReport,
    Form,
    FormStatus,
    ForwardGroup,
    ResourcePool,
    ResourceStatus,
    ResourceType,
    Shift,
    TeamLead,
    TeamLeadSource,
    User,
    UserRole,
)


DEFAULT_BANKS = ["Пумб", "Моно", "Альянс", "Фрибанк", "Майбанк"]


async def ensure_default_banks(session: AsyncSession) -> None:
    # Defaults are intentionally NOT auto-created.
    # Banks must be created manually via bot UI.
    return


async def upsert_user_from_tg(
    session: AsyncSession,
    tg_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
) -> User:
    res = await session.execute(select(User).where(User.tg_id == tg_id))
    user = res.scalar_one_or_none()
    if user is None:
        user = User(tg_id=tg_id, username=username, first_name=first_name, last_name=last_name)
        session.add(user)
        await session.flush()
        return user
    user.username = username
    user.first_name = first_name
    user.last_name = last_name
    return user


async def set_user_role(session: AsyncSession, tg_id: int, role: UserRole) -> None:
    res = await session.execute(select(User).where(User.tg_id == tg_id))
    user = res.scalar_one()
    user.role = role


async def get_user_by_tg_id(session: AsyncSession, tg_id: int) -> User | None:
    res = await session.execute(select(User).where(User.tg_id == tg_id))
    return res.scalar_one_or_none()


async def get_user_by_username(session: AsyncSession, username: str) -> User | None:
    uname = (username or "").strip().lstrip("@").lower()
    if not uname:
        return None
    res = await session.execute(select(User).where(func.lower(User.username) == uname))
    return res.scalar_one_or_none()


async def get_user_by_id(session: AsyncSession, user_id: int) -> User | None:
    res = await session.execute(select(User).where(User.id == user_id))
    return res.scalar_one_or_none()


async def set_user_forward_group(session: AsyncSession, user_id: int, group_id: int | None) -> None:
    user = await get_user_by_id(session, user_id)
    if not user:
        return
    user.forward_group_id = group_id


async def list_users(session: AsyncSession) -> list[User]:
    res = await session.execute(select(User).order_by(User.id.asc()))
    return list(res.scalars().all())


async def get_team_lead_by_tg_id(session: AsyncSession, tg_id: int) -> TeamLead | None:
    res = await session.execute(select(TeamLead).where(TeamLead.tg_id == tg_id))
    return res.scalar_one_or_none()


async def is_team_lead(session: AsyncSession, tg_id: int) -> bool:
    return (await get_team_lead_by_tg_id(session, tg_id)) is not None


async def list_team_leads(session: AsyncSession) -> list[TeamLead]:
    res = await session.execute(select(TeamLead).order_by(TeamLead.source.asc(), TeamLead.tg_id.asc()))
    return list(res.scalars().all())


async def list_team_lead_ids_by_source(session: AsyncSession, source: str) -> list[int]:
    src = (source or "TG").upper()
    src_enum = TeamLeadSource.FB if src == "FB" else TeamLeadSource.TG
    res = await session.execute(select(TeamLead.tg_id).where(TeamLead.source == src_enum).order_by(TeamLead.tg_id.asc()))
    return [int(x) for x in res.scalars().all()]


async def add_team_lead(session: AsyncSession, tg_id: int, source: str) -> TeamLead:
    src = (source or "TG").upper()
    src_enum = TeamLeadSource.FB if src == "FB" else TeamLeadSource.TG
    existing = await get_team_lead_by_tg_id(session, tg_id)
    if existing:
        existing.source = src_enum
        return existing
    tl = TeamLead(tg_id=tg_id, source=src_enum)
    session.add(tl)
    await session.flush()
    return tl


async def delete_team_lead(session: AsyncSession, tg_id: int) -> int:
    res = await session.execute(delete(TeamLead).where(TeamLead.tg_id == tg_id))
    return int(res.rowcount or 0)


async def upsert_access_request(session: AsyncSession, user_id: int) -> bool:
    """
    Creates or resets access request to PENDING.
    Returns True if a new request was created OR re-opened (so we can notify developer once).
    """
    res = await session.execute(select(AccessRequest).where(AccessRequest.user_id == user_id))
    req = res.scalar_one_or_none()
    if req is None:
        session.add(AccessRequest(user_id=user_id, status=AccessRequestStatus.PENDING))
        await session.flush()
        return True
    if req.status != AccessRequestStatus.PENDING:
        req.status = AccessRequestStatus.PENDING
        req.created_at = datetime.utcnow()
        req.processed_at = None
        req.processed_by_id = None
        return True
    return False


async def count_pending_access_requests(session: AsyncSession) -> int:
    res = await session.execute(select(func.count(AccessRequest.id)).where(AccessRequest.status == AccessRequestStatus.PENDING))
    return int(res.scalar() or 0)


async def get_next_pending_access_request(session: AsyncSession) -> AccessRequest | None:
    res = await session.execute(
        select(AccessRequest)
        .where(AccessRequest.status == AccessRequestStatus.PENDING)
        .order_by(AccessRequest.created_at.asc(), AccessRequest.id.asc())
        .limit(1)
    )
    return res.scalar_one_or_none()


async def set_access_request_status(
    session: AsyncSession,
    *,
    target_user_id: int,
    status: AccessRequestStatus,
    processed_by_user_id: int | None,
) -> None:
    res = await session.execute(select(AccessRequest).where(AccessRequest.user_id == target_user_id))
    req = res.scalar_one_or_none()
    if req is None:
        req = AccessRequest(user_id=target_user_id, status=status)
        session.add(req)
        await session.flush()
    req.status = status
    req.processed_at = datetime.utcnow()
    req.processed_by_id = processed_by_user_id


async def delete_access_request_by_user_id(session: AsyncSession, user_id: int) -> int:
    res = await session.execute(delete(AccessRequest).where(AccessRequest.user_id == user_id))
    return int(res.rowcount or 0)


async def delete_form_by_user_id(session: AsyncSession, user_id: int) -> int:
    res = await session.execute(delete(Form).where(Form.manager_id == user_id))
    return int(res.rowcount or 0)


async def delete_user_by_tg_id(session: AsyncSession, tg_id: int) -> bool:
    """
    Hard-delete user and dependent records:
    - access_requests
    - forms
    - shifts
    Returns True if user existed and was deleted.
    """
    user = await get_user_by_tg_id(session, tg_id)
    if not user:
        return False
    user_id = int(user.id)
    # dependent rows
    await session.execute(delete(AccessRequest).where(AccessRequest.user_id == user_id))
    await session.execute(delete(Form).where(Form.manager_id == user_id))
    await session.execute(delete(Shift).where(Shift.manager_id == user_id))
    await session.execute(delete(DuplicateReport).where(DuplicateReport.manager_id == user_id))
    # user
    await session.delete(user)
    return True


async def get_active_shift(session: AsyncSession, manager_user_id: int) -> Shift | None:
    res = await session.execute(
        select(Shift).where(and_(Shift.manager_id == manager_user_id, Shift.ended_at.is_(None))).order_by(Shift.id.desc())
    )
    return res.scalar_one_or_none()


async def start_shift(session: AsyncSession, manager_user_id: int) -> Shift:
    shift = Shift(manager_id=manager_user_id, started_at=datetime.utcnow(), ended_at=None)
    session.add(shift)
    await session.flush()
    return shift


async def end_shift(session: AsyncSession, shift: Shift) -> None:
    shift.ended_at = datetime.utcnow()


async def create_form(session: AsyncSession, manager_user_id: int, shift_id: int | None) -> Form:
    form = Form(manager_id=manager_user_id, shift_id=shift_id, status=FormStatus.IN_PROGRESS, screenshots=[])
    session.add(form)
    await session.flush()
    return form


async def get_form(session: AsyncSession, form_id: int) -> Form | None:
    res = await session.execute(select(Form).where(Form.id == form_id))
    return res.scalar_one_or_none()


async def list_pending_forms(session: AsyncSession, limit: int = 10) -> list[Form]:
    res = await session.execute(select(Form).where(Form.status == FormStatus.PENDING).order_by(Form.id.desc()).limit(limit))
    return list(res.scalars().all())


async def count_pending_forms(session: AsyncSession) -> int:
    res = await session.execute(select(func.count(Form.id)).where(Form.status == FormStatus.PENDING))
    return int(res.scalar() or 0)


async def list_dm_approved_without_payment(session: AsyncSession, *, manager_user_id: int, limit: int = 30) -> list[Form]:
    res = await session.execute(
        select(Form)
        .where(
            and_(
                Form.manager_id == manager_user_id,
                Form.status == FormStatus.APPROVED,
                Form.payment_done_at.is_(None),
            )
        )
        .order_by(Form.id.desc())
        .limit(limit)
    )
    return list(res.scalars().all())


async def mark_form_payment_done(session: AsyncSession, *, form_id: int) -> None:
    form = await get_form(session, form_id)
    if not form:
        return
    form.payment_done_at = datetime.utcnow()


async def set_form_status(session: AsyncSession, form_id: int, status: FormStatus, team_lead_comment: str | None = None) -> None:
    form = await get_form(session, form_id)
    if not form:
        return
    form.status = status
    form.team_lead_comment = team_lead_comment


async def get_bank_by_name(session: AsyncSession, name: str) -> BankCondition | None:
    res = await session.execute(select(BankCondition).where(BankCondition.name == name))
    return res.scalar_one_or_none()


async def get_bank(session: AsyncSession, bank_id: int) -> BankCondition | None:
    res = await session.execute(select(BankCondition).where(BankCondition.id == bank_id))
    return res.scalar_one_or_none()


async def list_banks(session: AsyncSession) -> list[BankCondition]:
    res = await session.execute(select(BankCondition).order_by(BankCondition.name.asc()))
    return list(res.scalars().all())


async def delete_bank_condition(session: AsyncSession, bank_id: int) -> bool:
    bank = await get_bank(session, bank_id)
    if not bank:
        return False
    await session.delete(bank)
    return True


async def list_forward_groups(session: AsyncSession) -> list[ForwardGroup]:
    res = await session.execute(select(ForwardGroup).order_by(ForwardGroup.id.asc()))
    return list(res.scalars().all())


async def get_forward_group_by_id(session: AsyncSession, group_id: int) -> ForwardGroup | None:
    res = await session.execute(select(ForwardGroup).where(ForwardGroup.id == group_id))
    return res.scalar_one_or_none()


async def get_forward_group_by_chat_id(session: AsyncSession, chat_id: int) -> ForwardGroup | None:
    res = await session.execute(select(ForwardGroup).where(ForwardGroup.chat_id == chat_id))
    return res.scalar_one_or_none()


async def create_forward_group(session: AsyncSession, *, chat_id: int, title: str | None = None) -> ForwardGroup:
    existing = await get_forward_group_by_chat_id(session, chat_id)
    if existing:
        existing.title = title
        return existing
    g = ForwardGroup(chat_id=chat_id, title=title, is_confirmed=False)
    session.add(g)
    await session.flush()
    return g


async def delete_forward_group(session: AsyncSession, group_id: int) -> bool:
    g = await get_forward_group_by_id(session, group_id)
    if not g:
        return False
    # unbind users
    res = await session.execute(select(User).where(User.forward_group_id == group_id))
    for u in res.scalars().all():
        u.forward_group_id = None
    await session.delete(g)
    return True


async def update_forward_group_status(
    session: AsyncSession,
    *,
    group_id: int,
    is_confirmed: bool,
    title: str | None = None,
    checked_at: datetime | None = None,
) -> None:
    g = await get_forward_group_by_id(session, group_id)
    if not g:
        return
    g.is_confirmed = bool(is_confirmed)
    if title is not None:
        g.title = title
    if checked_at is not None:
        g.last_checked_at = checked_at


async def create_bank(session: AsyncSession, name: str) -> BankCondition:
    bank = BankCondition(
        name=name,
        instructions=None,
        required_screens=None,
        instructions_tg=None,
        instructions_fb=None,
        required_screens_tg=None,
        required_screens_fb=None,
        template_screens=[],
    )
    session.add(bank)
    await session.flush()
    return bank


async def update_bank(
    session: AsyncSession,
    bank_id: int,
    *,
    name: str | None | Any = ...,
    instructions: str | None | Any = ...,
    instructions_tg: str | None | Any = ...,
    instructions_fb: str | None | Any = ...,
    required_screens: int | None | Any = ...,
    required_screens_tg: int | None | Any = ...,
    required_screens_fb: int | None | Any = ...,
    template_screens: list[str] | Any = ...,
) -> None:
    bank = await get_bank(session, bank_id)
    if not bank:
        return
    if name is not ...:
        old_name = str(bank.name)
        new_name = str(name or "").strip()
        if new_name and new_name != old_name:
            bank.name = new_name
            res_forms = await session.execute(select(Form).where(Form.bank_name == old_name))
            for f in res_forms.scalars().all():
                f.bank_name = new_name
            res_dups = await session.execute(select(DuplicateReport).where(DuplicateReport.bank_name == old_name))
            for r in res_dups.scalars().all():
                r.bank_name = new_name
    if instructions is not ...:
        bank.instructions = instructions
    if instructions_tg is not ...:
        bank.instructions_tg = instructions_tg
    if instructions_fb is not ...:
        bank.instructions_fb = instructions_fb
    if required_screens is not ...:
        bank.required_screens = required_screens
    if required_screens_tg is not ...:
        bank.required_screens_tg = required_screens_tg
    if required_screens_fb is not ...:
        bank.required_screens_fb = required_screens_fb
    if template_screens is not ...:
        bank.template_screens = template_screens


def iter_team_lead_ids(team_lead_ids: Iterable[int]) -> list[int]:
    return list(team_lead_ids)


async def get_form_counts_by_manager(session: AsyncSession) -> dict[int, dict[FormStatus, int]]:
    """
    Returns counts of forms grouped by manager_id and status.
    Only includes managers that have at least one form.
    """
    res = await session.execute(
        select(Form.manager_id, Form.status, func.count(Form.id))
        .group_by(Form.manager_id, Form.status)
        .order_by(Form.manager_id.asc())
    )
    out: dict[int, dict[FormStatus, int]] = {}
    for manager_id, status, cnt in res.all():
        out.setdefault(int(manager_id), {})[status] = int(cnt)
    return out


async def list_forms_by_user_id(session: AsyncSession, user_id: int) -> list[Form]:
    """Get all forms for a specific user ID."""
    res = await session.execute(
        select(Form).where(Form.manager_id == user_id).order_by(Form.id.desc())
    )
    return list(res.scalars().all())


async def delete_form(session: AsyncSession, form_id: int) -> bool:
    """Delete a form by ID. Returns True if deleted, False if not found."""
    form = await get_form(session, form_id)
    if not form:
        return False
    await session.delete(form)
    return True


async def list_rejected_forms_by_user_id(session: AsyncSession, user_id: int) -> list[Form]:
    """Get all rejected forms for a specific user ID."""
    res = await session.execute(
        select(Form).where(
            Form.manager_id == user_id,
            Form.status == FormStatus.REJECTED,
            Form.team_lead_comment.isnot(None)
        ).order_by(Form.id.desc())
    )
    return list(res.scalars().all())


async def count_rejected_forms_by_user_id(session: AsyncSession, user_id: int) -> int:
    res = await session.execute(
        select(func.count(Form.id)).where(
            Form.manager_id == user_id,
            Form.status == FormStatus.REJECTED,
            Form.team_lead_comment.isnot(None),
        )
    )
    return int(res.scalar() or 0)


async def list_all_forms(session: AsyncSession) -> list[Form]:
    """Get all forms in the system."""
    res = await session.execute(
        select(Form).order_by(Form.id.desc())
    )
    return list(res.scalars().all())


async def list_all_forms_in_range(
    session: AsyncSession,
    *,
    created_from: datetime | None,
    created_to: datetime | None,
) -> list[Form]:
    q = select(Form)
    if created_from is not None:
        q = q.where(Form.created_at >= created_from)
    if created_to is not None:
        q = q.where(Form.created_at < created_to)
    res = await session.execute(q.order_by(Form.id.desc()))
    return list(res.scalars().all())


async def list_user_forms_in_range(
    session: AsyncSession,
    *,
    user_id: int,
    created_from: datetime | None,
    created_to: datetime | None,
) -> list[Form]:
    q = select(Form).where(Form.manager_id == user_id)
    if created_from is not None:
        q = q.where(Form.created_at >= created_from)
    if created_to is not None:
        q = q.where(Form.created_at < created_to)
    res = await session.execute(q.order_by(Form.id.desc()))
    return list(res.scalars().all())


async def find_forms_by_phone(session: AsyncSession, phone: str) -> list[Form]:
    res = await session.execute(select(Form).where(Form.phone == phone).order_by(Form.id.desc()))
    return list(res.scalars().all())


async def phone_bank_duplicate_exists(session: AsyncSession, *, phone: str, bank_name: str, exclude_form_id: int | None = None) -> Form | None:
    q = select(Form).where(and_(Form.phone == phone, Form.bank_name == bank_name))
    if exclude_form_id is not None:
        q = q.where(Form.id != exclude_form_id)
    res = await session.execute(q.order_by(Form.id.desc()).limit(1))
    return res.scalar_one_or_none()


async def create_duplicate_report(
    session: AsyncSession,
    *,
    manager_id: int,
    manager_username: str | None,
    manager_source: str | None,
    phone: str,
    bank_name: str,
) -> DuplicateReport:
    report = DuplicateReport(
        manager_id=manager_id,
        manager_username=manager_username,
        manager_source=manager_source,
        phone=phone,
        bank_name=bank_name,
    )
    session.add(report)
    await session.flush()
    return report


async def list_duplicate_reports_in_range(
    session: AsyncSession,
    *,
    manager_source: str | None,
    created_from: datetime | None,
    created_to: datetime | None,
    limit: int = 200,
) -> list[DuplicateReport]:
    q = select(DuplicateReport)
    if manager_source:
        q = q.where(DuplicateReport.manager_source == manager_source)
    if created_from is not None:
        q = q.where(DuplicateReport.created_at >= created_from)
    if created_to is not None:
        q = q.where(DuplicateReport.created_at < created_to)
    res = await session.execute(q.order_by(DuplicateReport.id.desc()).limit(limit))
    return list(res.scalars().all())


async def list_all_access_requests(session: AsyncSession) -> list[AccessRequest]:
    """Get all access requests in the system."""
    res = await session.execute(
        select(AccessRequest).order_by(AccessRequest.created_at.desc())
    )
    return list(res.scalars().all())


async def create_resource_pool_item(
    session: AsyncSession,
    *,
    source: str,
    bank_id: int,
    resource_type: str,
    text_data: str | None,
    screenshots: list[str] | None,
    created_by_user_id: int,
) -> ResourcePool:
    t = ResourceType(resource_type)
    item = ResourcePool(
        source=(source or "TG").upper(),
        bank_id=int(bank_id),
        type=t,
        status=ResourceStatus.FREE,
        text_data=text_data,
        screenshots=list(screenshots or []),
        created_by_user_id=int(created_by_user_id),
    )
    session.add(item)
    await session.flush()
    return item


async def list_pool_stats_by_bank(session: AsyncSession, *, source: str | None = None) -> list[tuple[BankCondition, dict[str, int]]]:
    banks = await list_banks(session)
    out: list[tuple[BankCondition, dict[str, int]]] = []
    for b in banks:
        stats: dict[str, int] = {
            "link": 0,
            "esim": 0,
            "link_esim": 0,
            "status_free": 0,
            "status_assigned": 0,
            "status_used": 0,
            "status_invalid": 0,
            "total": 0,
        }
        for t in (ResourceType.LINK, ResourceType.ESIM, ResourceType.LINK_ESIM):
            q = select(func.count(ResourcePool.id)).where(
                ResourcePool.bank_id == int(b.id),
                ResourcePool.type == t,
            )
            if source:
                q = q.where(ResourcePool.source == source.upper())
            res = await session.execute(q)
            stats[t.value] = int(res.scalar() or 0)
        for s in (ResourceStatus.FREE, ResourceStatus.ASSIGNED, ResourceStatus.USED, ResourceStatus.INVALID):
            q = select(func.count(ResourcePool.id)).where(
                ResourcePool.bank_id == int(b.id),
                ResourcePool.status == s,
            )
            if source:
                q = q.where(ResourcePool.source == source.upper())
            res = await session.execute(q)
            stats[f"status_{s.value}"] = int(res.scalar() or 0)
        stats["total"] = stats["status_free"] + stats["status_assigned"] + stats["status_used"] + stats["status_invalid"]
        out.append((b, stats))
    return out


async def count_dm_active_pool_items(session: AsyncSession, *, dm_user_id: int) -> int:
    res = await session.execute(
        select(func.count(ResourcePool.id)).where(
            ResourcePool.assigned_to_user_id == int(dm_user_id),
            ResourcePool.status == ResourceStatus.ASSIGNED,
        )
    )
    return int(res.scalar() or 0)


async def list_dm_active_pool_items(session: AsyncSession, *, dm_user_id: int) -> list[ResourcePool]:
    res = await session.execute(
        select(ResourcePool)
        .where(ResourcePool.assigned_to_user_id == int(dm_user_id), ResourcePool.status == ResourceStatus.ASSIGNED)
        .order_by(ResourcePool.updated_at.desc(), ResourcePool.id.desc())
    )
    return list(res.scalars().all())


async def get_pool_item(session: AsyncSession, item_id: int) -> ResourcePool | None:
    res = await session.execute(select(ResourcePool).where(ResourcePool.id == int(item_id)))
    return res.scalar_one_or_none()


async def list_free_pool_items_for_bank(session: AsyncSession, *, bank_id: int, source: str) -> list[ResourcePool]:
    res = await session.execute(
        select(ResourcePool)
        .where(
            ResourcePool.bank_id == int(bank_id),
            ResourcePool.source == source.upper(),
            ResourcePool.status == ResourceStatus.FREE,
        )
        .order_by(ResourcePool.id.asc())
    )
    return list(res.scalars().all())


async def assign_pool_item_to_dm(session: AsyncSession, *, item_id: int, dm_user_id: int) -> ResourcePool | None:
    item = await get_pool_item(session, int(item_id))
    if not item or item.status != ResourceStatus.FREE:
        return None
    item.status = ResourceStatus.ASSIGNED
    item.assigned_to_user_id = int(dm_user_id)
    item.assigned_at = datetime.utcnow()
    return item


async def release_pool_item(session: AsyncSession, *, item_id: int, dm_user_id: int) -> bool:
    item = await get_pool_item(session, int(item_id))
    if not item or item.status != ResourceStatus.ASSIGNED or int(item.assigned_to_user_id or 0) != int(dm_user_id):
        return False
    item.status = ResourceStatus.FREE
    item.assigned_to_user_id = None
    item.assigned_at = None
    return True


async def mark_pool_item_invalid(session: AsyncSession, *, item_id: int, dm_user_id: int, comment: str) -> ResourcePool | None:
    item = await get_pool_item(session, int(item_id))
    if not item or item.status != ResourceStatus.ASSIGNED or int(item.assigned_to_user_id or 0) != int(dm_user_id):
        return None
    item.invalid_comment = (comment or "").strip()
    item.status = ResourceStatus.INVALID
    item.assigned_to_user_id = None
    item.assigned_at = None
    return item


async def mark_pool_item_used_with_form(session: AsyncSession, *, item_id: int, dm_user_id: int, form_id: int) -> ResourcePool | None:
    item = await get_pool_item(session, int(item_id))
    if not item or item.status != ResourceStatus.ASSIGNED or int(item.assigned_to_user_id or 0) != int(dm_user_id):
        return None
    item.status = ResourceStatus.USED
    item.used_with_form_id = int(form_id)
    item.assigned_to_user_id = None
    item.assigned_at = None
    return item


async def list_invalid_pool_items_for_wictory(session: AsyncSession, *, wictory_user_id: int) -> list[ResourcePool]:
    res = await session.execute(
        select(ResourcePool)
        .where(ResourcePool.created_by_user_id == int(wictory_user_id), ResourcePool.status == ResourceStatus.INVALID)
        .order_by(ResourcePool.updated_at.desc(), ResourcePool.id.desc())
    )
    return list(res.scalars().all())


async def wictory_update_invalid_item(
    session: AsyncSession,
    *,
    item_id: int,
    wictory_user_id: int,
    text_data: str | None = None,
    screenshots: list[str] | None = None,
    set_free: bool = False,
) -> ResourcePool | None:
    item = await get_pool_item(session, int(item_id))
    if not item or int(item.created_by_user_id) != int(wictory_user_id) or item.status != ResourceStatus.INVALID:
        return None
    if text_data is not None:
        item.text_data = text_data
    if screenshots is not None:
        item.screenshots = list(screenshots)
    if set_free:
        item.status = ResourceStatus.FREE
        item.invalid_comment = None
    return item

