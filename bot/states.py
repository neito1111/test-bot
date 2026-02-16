from aiogram.fsm.state import State, StatesGroup


class DropManagerFormStates(StatesGroup):
    traffic_type = State()
    direct_forward = State()
    referral_forward_1 = State()
    referral_forward_2 = State()
    forward_manual_username = State()
    forward_manual_phone = State()
    forward_manual_name = State()
    phone = State()
    bank_select = State()
    bank_custom = State()
    password = State()
    screenshots = State()
    comment = State()
    confirm = State()


class TeamLeadStates(StatesGroup):
    reject_comment = State()
    duplicates_filter_range = State()
    bank_custom_name = State()
    bank_rename_name = State()
    bank_instructions = State()
    bank_required_screens = State()
    bank_templates = State()


class DropManagerEditStates(StatesGroup):
    choose_field = State()
    traffic_type = State()
    direct_forward = State()
    referral_forward_1 = State()
    referral_forward_2 = State()
    forward_manual_username = State()
    forward_manual_phone = State()
    forward_manual_name = State()
    phone = State()
    bank_select = State()
    bank_custom = State()
    password = State()
    screenshots = State()
    screenshot_replace = State()
    comment = State()


class DropManagerRejectedStates(StatesGroup):
    view_list = State()
    view_form = State()


class DropManagerMyFormsStates(StatesGroup):
    forms_list = State()
    forms_filter_range = State()
    form_view = State()


class DropManagerPaymentStates(StatesGroup):
    card_main = State()
    amount_main = State()
    phone_bonus = State()
    card_bonus = State()
    amount_bonus = State()


class DropManagerShiftStates(StatesGroup):
    dialogs_count = State()
    comment_of_day = State()


class DeveloperStates(StatesGroup):
    delete_user_tg_id = State()
    delete_request_tg_id = State()
    delete_form_user_id = State()
    delete_form_id = State()
    
    # New states for management
    users_list = State()
    user_view = State()
    user_edit_field = State()
    
    forms_list = State()
    form_view = State()
    form_edit_field = State()
    forms_filter_range = State()
    
    reqs_list = State()
    req_view = State()
    req_edit_field = State()

    team_leads_menu = State()
    team_leads_add = State()
    team_leads_delete = State()
    team_leads_edit_source = State()

    groups_menu = State()
    groups_add = State()
    groups_delete = State()
    groups_bind_pick = State()

