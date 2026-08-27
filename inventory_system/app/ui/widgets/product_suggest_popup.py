"""A rich, keyboard-friendly product-suggestion popup — Name / SKU / HSN /
Stock / Price per row, plus a pinned "+ Add Product" row when the host
allows creating products from here. Used by TransactionItemsTable (shared
by New Bill, Sales Order, and Purchase Order) as the dropdown shown while
the user types in the product search box.

This widget renders and reports selection only — it never calls a service,
never talks to the database, and never opens a dialog. TransactionItemsTable
owns the search Worker and decides what "+ Add Product" (or Enter/click on
a row) actually does; that keeps this widget reusable and trivially
testable without QThreadPool or a real ProductService.

Positioning/focus: shown as a frameless, always-on-top *tool* window with
WA_ShowWithoutActivating so it never steals keyboard focus from the search
box — the host is expected to forward Up/Down/Enter/Escape key presses
from its QLineEdit into move_selection()/activate_selection()/hide_popup()
(see TransactionItemsTable's event filter on its search box). Left-clicking
outside the popup closes it via an application-wide event filter installed
only while the popup is visible.
"""
import uuid
from decimal import Decimal

from PySide6.QtCore import QEvent, QPoint, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.schemas.inventory import InventoryLevel
from app.schemas.product import ProductOut
from app.ui.theme import ACCENT, MUTED, RED, scale

# Sentinel stored in a QListWidgetItem's Qt.UserRole to mark the pinned
# "+ Add Product" row, distinguishing it from a row carrying a real
# product's uuid.
_ADD_PRODUCT_MARKER = "__add_product__"


def _money(value: Decimal) -> str:
    return f"{value:,.2f}"


class _SuggestionRow(QWidget):
    """One product's Name/SKU/HSN/Stock/Price, laid out to line up as
    columns across rows (fixed-width SKU/HSN/Stock/Price, Name stretches).
    """

    def __init__(self, product: ProductOut, price_field: str,
                level: InventoryLevel | None, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(12)

        name = QLabel(product.name)
        name.setStyleSheet("font-weight: 600;")
        layout.addWidget(name, stretch=1)

        sku = QLabel(product.sku)
        sku.setStyleSheet(f"color: {MUTED};")
        sku.setMinimumWidth(scale(90))
        layout.addWidget(sku)

        hsn = QLabel(product.hsn_code or "—")
        hsn.setStyleSheet(f"color: {MUTED};")
        hsn.setMinimumWidth(scale(70))
        layout.addWidget(hsn)

        stock = QLabel(self._stock_text(level))
        stock.setMinimumWidth(scale(90))
        stock.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        stock.setStyleSheet(f"color: {self._stock_color(level)};")
        layout.addWidget(stock)

        price = QLabel(_money(getattr(product, price_field)))
        price.setMinimumWidth(scale(80))
        price.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(price)

    @staticmethod
    def _stock_text(level: InventoryLevel | None) -> str:
        if level is None:
            return "—"
        return f"{level.quantity_available:g} in stock"

    @staticmethod
    def _stock_color(level: InventoryLevel | None) -> str:
        if level is None:
            return MUTED
        return RED if level.quantity_available <= 0 else MUTED


class _AddProductRow(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        label = QLabel("+ Add Product")
        label.setStyleSheet(f"color: {ACCENT}; font-weight: 600;")
        layout.addWidget(label)
        layout.addStretch()


class _MessageRow(QWidget):
    def __init__(self, text: str, *, color: str, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        label = QLabel(text)
        label.setStyleSheet(f"color: {color};")
        layout.addWidget(label)


class ProductSuggestPopup(QFrame):
    product_selected = Signal(object)  # ProductOut
    add_product_requested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("productSuggestPopup")
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFrameShape(QFrame.Shape.StyledPanel)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(0)

        self._list = QListWidget()
        self._list.setObjectName("productSuggestList")
        self._list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        self._list.setUniformItemSizes(False)
        self._list.itemClicked.connect(self._on_item_clicked)
        layout.addWidget(self._list)

        self._products_by_id: dict[uuid.UUID, ProductOut] = {}
        self._anchor: QWidget | None = None
        self.setMinimumWidth(scale(420))
        self.hide()

        # Installed on the QApplication only while visible — closes the
        # popup on a click outside it (see eventFilter below).
        self._outside_click_filter_installed = False

    # -- content ----------------------------------------------------------#
    def show_products(self, products: list[ProductOut], price_field: str,
                      stock_by_product: dict[uuid.UUID, InventoryLevel] | None,
                      *, show_add_product: bool) -> None:
        self._list.clear()
        self._products_by_id = {p.id: p for p in products}
        stock_by_product = stock_by_product or {}

        for product in products:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, product.id)
            row = _SuggestionRow(product, price_field, stock_by_product.get(product.id))
            item.setSizeHint(row.sizeHint())
            self._list.addItem(item)
            self._list.setItemWidget(item, row)

        if not products:
            message = QListWidgetItem()
            message.setFlags(Qt.ItemFlag.NoItemFlags)
            row = _MessageRow("No product found", color=MUTED)
            message.setSizeHint(row.sizeHint())
            self._list.addItem(message)
            self._list.setItemWidget(message, row)

        if show_add_product:
            add_item = QListWidgetItem()
            add_item.setData(Qt.ItemDataRole.UserRole, _ADD_PRODUCT_MARKER)
            row = _AddProductRow()
            add_item.setSizeHint(row.sizeHint())
            self._list.addItem(add_item)
            self._list.setItemWidget(add_item, row)

        self._select_first_selectable()
        self._resize_to_content()

    def show_loading(self) -> None:
        self._list.clear()
        self._products_by_id = {}
        item = QListWidgetItem()
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        row = _MessageRow("Searching…", color=MUTED)
        item.setSizeHint(row.sizeHint())
        self._list.addItem(item)
        self._list.setItemWidget(item, row)
        self._resize_to_content()

    def show_error(self, message: str, *, show_add_product: bool) -> None:
        self._list.clear()
        self._products_by_id = {}
        item = QListWidgetItem()
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        row = _MessageRow(message, color=RED)
        item.setSizeHint(row.sizeHint())
        self._list.addItem(item)
        self._list.setItemWidget(item, row)

        if show_add_product:
            add_item = QListWidgetItem()
            add_item.setData(Qt.ItemDataRole.UserRole, _ADD_PRODUCT_MARKER)
            add_row = _AddProductRow()
            add_item.setSizeHint(add_row.sizeHint())
            self._list.addItem(add_item)
            self._list.setItemWidget(add_item, add_row)

        self._select_first_selectable()
        self._resize_to_content()

    # -- selection ----------------------------------------------------------#
    def _select_first_selectable(self) -> None:
        for row in range(self._list.count()):
            item = self._list.item(row)
            if item.flags() & Qt.ItemFlag.ItemIsSelectable:
                self._list.setCurrentRow(row)
                return
        self._list.setCurrentRow(-1)

    def move_selection(self, delta: int) -> None:
        count = self._list.count()
        if count == 0:
            return
        current = self._list.currentRow()
        step = 1 if delta > 0 else -1
        row = current
        for _ in range(count):
            row = (row + step) % count
            item = self._list.item(row)
            if item.flags() & Qt.ItemFlag.ItemIsSelectable:
                self._list.setCurrentRow(row)
                return

    def activate_selection(self) -> None:
        item = self._list.currentItem()
        if item is None:
            return
        self._activate_item(item)

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        self._activate_item(item)

    def _activate_item(self, item: QListWidgetItem) -> None:
        marker = item.data(Qt.ItemDataRole.UserRole)
        if marker is None:
            return  # an informational row ("No product found"/"Searching…")
        if marker == _ADD_PRODUCT_MARKER:
            self.add_product_requested.emit()
            return
        product = self._products_by_id.get(marker)
        if product is not None:
            self.product_selected.emit(product)

    # -- visibility / positioning -------------------------------------------#
    def reposition_below(self, anchor: QWidget) -> None:
        """Drops the popup under `anchor`, or above it when there is no room.

        The previous version moved to the anchor's bottom-left with no
        reference to the screen at all. On a short display -- or simply with
        the search box low in a long form -- the list opened past the bottom
        edge, so the suggestions it existed to show were not visible.
        """
        self._anchor = anchor
        self.setFixedWidth(max(anchor.width(), self.minimumWidth()))

        screen = self.screen() or anchor.screen() or QGuiApplication.primaryScreen()
        below = anchor.mapToGlobal(QPoint(0, anchor.height()))
        if screen is None:  # pragma: no cover - no screen (offscreen platform)
            self.move(below)
            return

        bounds = screen.availableGeometry()
        height = self.height() or self.sizeHint().height()

        # Flip above the anchor when the space below cannot hold the popup
        # but the space above can.
        space_below = bounds.bottom() - below.y()
        above_y = anchor.mapToGlobal(QPoint(0, 0)).y() - height
        if space_below < height and above_y >= bounds.top():
            below.setY(above_y)
        else:
            below.setY(min(below.y(), max(bounds.top(), bounds.bottom() - height)))

        # And never let it run off the right-hand edge.
        below.setX(min(max(below.x(), bounds.left()),
                       max(bounds.left(), bounds.right() - self.width())))
        self.move(below)

    def _resize_to_content(self) -> None:
        total_height = sum(self._list.sizeHintForRow(r) for r in range(self._list.count()))
        # Capped so a large result page never grows the popup off-screen;
        # the list itself scrolls beyond this. The screen is part of the cap
        # because 320 design pixels can still exceed the space available on
        # a short display at high scaling.
        screen = self.screen() or QGuiApplication.primaryScreen()
        limit = scale(320)
        if screen is not None:
            limit = min(limit, int(screen.availableGeometry().height() * 0.5))
        self.setFixedHeight(min(total_height + scale(8), limit))

    def show_popup(self) -> None:
        if not self.isVisible():
            self.show()
        if not self._outside_click_filter_installed:
            QApplication.instance().installEventFilter(self)
            self._outside_click_filter_installed = True

    def hide_popup(self) -> None:
        if self._outside_click_filter_installed:
            QApplication.instance().removeEventFilter(self)
            self._outside_click_filter_installed = False
        self.hide()

    def is_open(self) -> bool:
        return self.isVisible()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt override
        if event.type() == QEvent.Type.MouseButtonPress:
            pos = event.globalPosition().toPoint()
            in_popup = self.geometry().contains(pos)
            in_anchor = (self._anchor is not None
                        and self._anchor.rect().contains(self._anchor.mapFromGlobal(pos)))
            if not in_popup and not in_anchor:
                self.hide_popup()
        return False

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._outside_click_filter_installed:
            QApplication.instance().removeEventFilter(self)
            self._outside_click_filter_installed = False
        super().hideEvent(event)
