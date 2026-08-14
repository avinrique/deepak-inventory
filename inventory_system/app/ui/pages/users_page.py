"""Users page — placeholder. UserService (create/activate/deactivate/reset
password) is real and fully tested (see docs/architecture.md), but there is
no "list users in an organization" repository method yet, so there's no
data this page could honestly list. Wiring a user-management table/forms
onto UserService is natural follow-up work, not done here.
"""
from app.ui.widgets.placeholder_page import PlaceholderPage


class UsersPage(PlaceholderPage):
    def __init__(self):
        super().__init__("Users", "Manage staff accounts and roles.", "👤",
                         "User management screens are coming in a future update.")
