from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.sqlite import JSON as SQLITE_JSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class UserRole(str, enum.Enum):
    PENDING = "PENDING"
    DROP_MANAGER = "DROP_MANAGER"
    TEAM_LEAD = "TEAM_LEAD"
    DEVELOPER = "DEVELOPER"
    WICTORY = "WICTORY"


class FormStatus(str, enum.Enum):
    IN_PROGRESS = "IN_PROGRESS"
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class AccessRequestStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class TeamLeadSource(str, enum.Enum):
    TG = "TG"
    FB = "FB"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.PENDING, index=True)
    manager_tag: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manager_source: Mapped[str | None] = mapped_column(String(8), nullable=True)

    forward_group_id: Mapped[int | None] = mapped_column(ForeignKey("forward_groups.id"), nullable=True, index=True)

    last_private_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_private_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    shifts: Mapped[list["Shift"]] = relationship(back_populates="manager")
    forms: Mapped[list["Form"]] = relationship(back_populates="manager")
    forward_group: Mapped["ForwardGroup | None"] = relationship(back_populates="drop_managers")
    access_request: Mapped["AccessRequest | None"] = relationship(
        back_populates="user",
        uselist=False,
        foreign_keys="AccessRequest.user_id",
    )
    duplicate_reports: Mapped[list["DuplicateReport"]] = relationship(back_populates="manager")


class DuplicateReport(Base):
    __tablename__ = "duplicate_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    manager_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    manager_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manager_source: Mapped[str | None] = mapped_column(String(8), nullable=True, index=True)
    phone: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    bank_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    manager: Mapped["User"] = relationship(back_populates="duplicate_reports")


class ForwardGroup(Base):
    __tablename__ = "forward_groups"
    __table_args__ = (UniqueConstraint("chat_id", name="uq_forward_groups_chat_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    title: Mapped[str | None] = mapped_column(String(128), nullable=True)

    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_confirmed: Mapped[bool] = mapped_column(Integer, default=0)  # sqlite bool

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    drop_managers: Mapped[list["User"]] = relationship(back_populates="forward_group")


class Shift(Base):
    __tablename__ = "shifts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    manager_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    comment_of_day: Mapped[str | None] = mapped_column(Text, nullable=True)
    dialogs_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    manager: Mapped["User"] = relationship(back_populates="shifts")
    forms: Mapped[list["Form"]] = relationship(back_populates="shift")


class BankCondition(Base):
    __tablename__ = "bank_conditions"
    __table_args__ = (UniqueConstraint("name", name="uq_bank_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), index=True)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    required_screens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    instructions_tg: Mapped[str | None] = mapped_column(Text, nullable=True)
    instructions_fb: Mapped[str | None] = mapped_column(Text, nullable=True)
    required_screens_tg: Mapped[int | None] = mapped_column(Integer, nullable=True)
    required_screens_fb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    template_screens: Mapped[list[str]] = mapped_column(SQLITE_JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Form(Base):
    __tablename__ = "forms"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    manager_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    shift_id: Mapped[int | None] = mapped_column(ForeignKey("shifts.id"), nullable=True, index=True)

    status: Mapped[FormStatus] = mapped_column(Enum(FormStatus), default=FormStatus.IN_PROGRESS, index=True)

    traffic_type: Mapped[str | None] = mapped_column(String(16), nullable=True)  # DIRECT / REFERRAL
    direct_user: Mapped[dict[str, Any] | None] = mapped_column(SQLITE_JSON, nullable=True)
    referral_user: Mapped[dict[str, Any] | None] = mapped_column(SQLITE_JSON, nullable=True)

    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    bank_name: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    password: Mapped[str | None] = mapped_column(String(16), nullable=True)
    screenshots: Mapped[list[str]] = mapped_column(SQLITE_JSON, default=list)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    team_lead_comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    payment_done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    manager: Mapped["User"] = relationship(back_populates="forms")
    shift: Mapped["Shift | None"] = relationship(back_populates="forms")


class AccessRequest(Base):
    """Persistent queue of access requests to become DROP_MANAGER."""

    __tablename__ = "access_requests"
    __table_args__ = (UniqueConstraint("user_id", name="uq_access_requests_user_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[AccessRequestStatus] = mapped_column(
        Enum(AccessRequestStatus),
        default=AccessRequestStatus.PENDING,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)

    user: Mapped["User"] = relationship(back_populates="access_request", foreign_keys=[user_id])
    processed_by: Mapped["User | None"] = relationship(foreign_keys=[processed_by_id])


class TeamLead(Base):
    __tablename__ = "team_leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    source: Mapped[TeamLeadSource] = mapped_column(Enum(TeamLeadSource), default=TeamLeadSource.TG, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ResourceType(str, enum.Enum):
    LINK = "link"
    ESIM = "esim"
    LINK_ESIM = "link_esim"


class ResourceStatus(str, enum.Enum):
    FREE = "free"
    ASSIGNED = "assigned"
    USED = "used"
    INVALID = "invalid"


class ResourcePool(Base):
    __tablename__ = "resource_pool"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(8), index=True)
    bank_id: Mapped[int] = mapped_column(ForeignKey("bank_conditions.id"), index=True)
    type: Mapped[ResourceType] = mapped_column(Enum(ResourceType), index=True)
    status: Mapped[ResourceStatus] = mapped_column(Enum(ResourceStatus), default=ResourceStatus.FREE, index=True)

    text_data: Mapped[str | None] = mapped_column(Text, nullable=True)
    screenshots: Mapped[list[str]] = mapped_column(SQLITE_JSON, default=list)

    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    assigned_to_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    invalid_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    used_with_form_id: Mapped[int | None] = mapped_column(ForeignKey("forms.id"), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
