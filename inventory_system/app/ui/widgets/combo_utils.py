"""QComboBox.findData() compares item data by identity, not value, for any
Python object Qt doesn't have a native QVariant conversion for — uuid.UUID
included. A UUID fetched from a separate query (same value, different
object) silently fails to match, so findData() returns -1 even though the
"same" id is right there in the combo. This does a plain Python ``==``
scan instead, which is what every caller actually means by "find the item
with this id".

Doesn't affect Enum-valued combos (enum members are singletons, so
identity and equality coincide there) — safe to use everywhere findData()
would otherwise be used, not just for UUIDs.
"""
from PySide6.QtWidgets import QComboBox


def find_data_index(combo: QComboBox, value) -> int:
    for i in range(combo.count()):
        if combo.itemData(i) == value:
            return i
    return -1


def select_by_data(combo: QComboBox, value) -> None:
    index = find_data_index(combo, value)
    if index >= 0:
        combo.setCurrentIndex(index)
