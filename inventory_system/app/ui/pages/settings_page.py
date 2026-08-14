"""Settings page — placeholder; no settings/preferences entity exists yet
beyond app/config's environment-level configuration (not user-editable)."""
from app.ui.widgets.placeholder_page import PlaceholderPage


class SettingsPage(PlaceholderPage):
    def __init__(self):
        super().__init__("Settings", "Configure organization preferences.", "⚙️",
                         "Organization settings are coming in a future update.")
