from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.keyboards import DEFAULT_BANKS
from bot.models import BankCondition, Form, FormStatus, User, UserRole
from bot.repositories import create_form
from bot.states import DropManagerFormStates


class DummyState:
    def __init__(self) -> None:
        self._data: dict[str, object] = {}
        self._state: object | None = None

    async def get_data(self) -> dict[str, object]:
        return dict(self._data)

    async def update_data(self, **kwargs: object) -> None:
        self._data.update(kwargs)

    async def set_state(self, state: object) -> None:
        self._state = state

    async def clear(self) -> None:
        self._data.clear()
        self._state = None

    @property
    def state(self) -> object | None:
        return self._state


async def _ensure_bank(session, name: str) -> None:
    session.add(BankCondition(name=name))
    await session.flush()


class DummyCallbackQuery:
    def __init__(self, *, data: str, from_user_id: int) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=from_user_id)
        self.message = SimpleNamespace(chat=SimpleNamespace(id=from_user_id))
        self.bot = SimpleNamespace()
        self._answers: list[dict[str, object]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self._answers.append({"text": text, "show_alert": show_alert})

    @property
    def last_answer(self) -> dict[str, object] | None:
        return self._answers[-1] if self._answers else None


@pytest.mark.asyncio
async def test_duplicate_block_on_submit(session, monkeypatch) -> None:
    from bot.handlers import drop_manager

    user = User(tg_id=501, role=UserRole.DROP_MANAGER)
    session.add(user)
    await session.flush()

    form = await create_form(session, manager_user_id=user.id, shift_id=None)
    form.phone = "+380 991234567"
    form.bank_name = "Моно"

    other = Form(manager_id=user.id, status=FormStatus.PENDING, phone=form.phone, bank_name=form.bank_name)
    session.add(other)
    await session.flush()

    state = DummyState()
    await state.update_data(form_id=form.id)

    cq = DummyCallbackQuery(data="form_submit", from_user_id=user.tg_id)
    await drop_manager.form_confirm_cb(cq, session, state, SimpleNamespace())

    assert cq.last_answer is not None
    assert cq.last_answer["show_alert"] is True
    assert "Такой номер уже есть" in (cq.last_answer["text"] or "")
    assert form.status == FormStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_bank_pick_sets_password_state(session, monkeypatch) -> None:
    from bot.handlers import drop_manager

    async def _noop_safe_edit_message(*args, **kwargs):
        return None

    async def _fake_instr(*args, **kwargs):
        return None

    monkeypatch.setattr(drop_manager, "_safe_edit_message", _noop_safe_edit_message)
    monkeypatch.setattr(drop_manager, "_get_bank_instructions_text", _fake_instr)

    user = User(tg_id=601, role=UserRole.DROP_MANAGER)
    session.add(user)
    await session.flush()

    form = await create_form(session, manager_user_id=user.id, shift_id=None)
    form.phone = "+380 991234567"

    state = DummyState()
    await state.update_data(form_id=form.id)

    bank_name = DEFAULT_BANKS[0]
    await _ensure_bank(session, bank_name)
    cq = DummyCallbackQuery(data=f"dm:bank:{bank_name}", from_user_id=user.tg_id)

    await drop_manager.dm_bank_pick_cb(cq, session, state)

    assert state.state == DropManagerFormStates.password
    assert form.bank_name == bank_name


@pytest.mark.asyncio
async def test_bank_pick_duplicate_warning_text(session, monkeypatch) -> None:
    from bot.handlers import drop_manager

    captured: dict[str, object] = {}

    async def _capture_safe_edit_message(*, message, text: str, reply_markup=None):
        captured["text"] = text
        captured["reply_markup"] = reply_markup

    async def _fake_instr(*args, **kwargs):
        return None

    monkeypatch.setattr(drop_manager, "_safe_edit_message", _capture_safe_edit_message)
    monkeypatch.setattr(drop_manager, "_get_bank_instructions_text", _fake_instr)

    user = User(tg_id=701, role=UserRole.DROP_MANAGER, manager_tag="dm")
    session.add(user)
    await session.flush()

    form = await create_form(session, manager_user_id=user.id, shift_id=None)
    form.phone = "+380 991234567"

    dup = Form(manager_id=user.id, status=FormStatus.PENDING, phone=form.phone, bank_name="Моно")
    session.add(dup)
    await session.flush()

    state = DummyState()
    await state.update_data(form_id=form.id)

    await _ensure_bank(session, "Моно")
    cq = DummyCallbackQuery(data="dm:bank:Моно", from_user_id=user.tg_id)
    await drop_manager.dm_bank_pick_cb(cq, session, state)

    assert "Такой номер уже есть" in (captured.get("text") or "")
