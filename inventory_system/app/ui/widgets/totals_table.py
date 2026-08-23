"""A QTableWidget with a pinned, always-visible totals row beneath it —
used by the Purchases and Sales transaction-register pages.

The totals row is a *second*, single-row QTableWidget rather than an extra
row inside the main table, for two reasons:

1. AsyncContentArea replaces its whole content widget on every reload
   (app/ui/widgets/async_content.py) — an extra row baked into the main
   table would vanish and reappear on every refresh exactly like the data
   rows do, when it should read as a fixed footer.
2. An extra row inside the main table would participate in selection and
   row-index math (``self._current_rows[row]`` in the pages that use this
   widget), which the Actions menu depends on being exactly the data rows,
   nothing more.

Column widths and horizontal scroll position are kept in sync between the
two tables so the totals stay aligned under their columns even while the
user scrolls or resizes a column.
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableWidget, QVBoxLayout, QWidget


class TotalsTable(QWidget):
    """Emits ``sort_requested(column)`` on a header click and
    ``selection_changed()`` when the selected row changes. Callers build
    columns via ``set_columns`` (once) and rows via ``set_rows``/
    ``set_totals_row`` (on every reload).
    """

    sort_requested = Signal(int)
    selection_changed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.table = QTableWidget()
        # Scopes the header hover affordance in theme.qss to tables that
        # actually respond to a header click (sort_requested below) —
        # other list pages' plain QTableWidgets don't pick this rule up.
        self.table.setObjectName("sortableTransactionTable")
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setMinimumSectionSize(70)
        self.table.horizontalHeader().sectionClicked.connect(self.sort_requested)
        self.table.itemSelectionChanged.connect(self.selection_changed)
        layout.addWidget(self.table, stretch=1)

        # Header hidden — this table shows exactly one row, the totals,
        # and never accepts selection or a header click.
        self._totals = QTableWidget(1, 0)
        self._totals.setObjectName("totalsRow")
        self._totals.horizontalHeader().setVisible(False)
        self._totals.verticalHeader().setVisible(False)
        self._totals.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._totals.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._totals.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._totals.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._totals.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._totals.setFixedHeight(40)
        layout.addWidget(self._totals)

        self.table.horizontalHeader().sectionResized.connect(self._sync_column_width)
        self.table.horizontalScrollBar().valueChanged.connect(
            self._totals.horizontalScrollBar().setValue)

    # -- setup ----------------------------------------------------------- #
    def set_columns(self, labels: list[str], *, stretch_column: int,
                    right_aligned: set[int], fixed: dict[int, int] | None = None) -> None:
        """``stretch_column`` (typically Supplier/Customer) is the single
        flexible column; every other column resizes to its contents, so
        horizontal scrolling appears only once real content outgrows the
        viewport rather than the table always being unnecessarily wide.
        """
        fixed = fixed or {}
        self.table.setColumnCount(len(labels))
        self.table.setHorizontalHeaderLabels(labels)
        self._totals.setColumnCount(len(labels))
        self._right_aligned = right_aligned

        header = self.table.horizontalHeader()
        for col in range(len(labels)):
            if col == stretch_column:
                header.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
            elif col in fixed:
                header.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
                self.table.setColumnWidth(col, fixed[col])
            else:
                header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSortingEnabled(False)  # sorting is server-side — see the pages

    def enable_sort_indicator(self, column: int, ascending: bool) -> None:
        self.table.horizontalHeader().setSortIndicatorShown(True)
        order = Qt.SortOrder.AscendingOrder if ascending else Qt.SortOrder.DescendingOrder
        self.table.horizontalHeader().setSortIndicator(column, order)

    # -- rows -------------------------------------------------------------#
    def set_row_count(self, count: int) -> None:
        self.table.setRowCount(count)

    def set_item(self, row: int, col: int, item) -> None:
        if col in getattr(self, "_right_aligned", set()):
            item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.table.setItem(row, col, item)

    def set_cell_widget(self, row: int, col: int, widget: QWidget) -> None:
        self.table.setCellWidget(row, col, widget)

    def set_totals_row(self, items) -> None:
        """``items`` is a list of QTableWidgetItem, one per column, already
        bold/right-aligned by the caller (see purchases_page/
        sales_orders_page's ``_totals_row_items``).
        """
        for col, item in enumerate(items):
            self._totals.setItem(0, col, item)
        self._sync_all_column_widths()

    def selected_row(self) -> int | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return rows[0].row() if rows else None

    # -- keep the totals row aligned under the main table ----------------#
    def _sync_column_width(self, col: int, _old: int, new: int) -> None:
        self._totals.setColumnWidth(col, new)

    def _sync_all_column_widths(self) -> None:
        for col in range(self.table.columnCount()):
            self._totals.setColumnWidth(col, self.table.columnWidth(col))
