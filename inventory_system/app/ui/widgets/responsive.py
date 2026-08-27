"""Helpers that keep windows and dialogs usable on small and scaled screens.

The problem these solve is concrete. The target hardware includes 1366x768
laptops, and Windows commonly runs those at 125% scaling, which leaves Qt
with a *logical* desktop of roughly 1092x614. Against that, the main
window's old `setMinimumSize(1080, 700)` was taller than the screen -- the
bottom of the window, including whatever it contained, simply could not be
reached, and because it was a *minimum* the user could not resize out of
it. Several dialogs had the same shape, and the ones whose footers sit
outside a scroll area put Save and Cancel off-screen with them.

So: never let a minimum size exceed the screen, and scroll the body of a
form rather than the whole thing, so the buttons stay put.
"""
import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QFrame, QScrollArea, QWidget

from app.ui.theme import scale

_logger = logging.getLogger(__name__)

# Leaves room for the taskbar and the window frame itself; availableGeometry
# already excludes the taskbar, but a window sized to exactly 100% of it
# still ends up with its title bar under the screen edge on some setups.
_MAX_FRACTION = 0.92


def available_size(widget: QWidget | None = None) -> tuple[int, int]:
    """Usable width/height in logical pixels on the screen this widget is on
    (the primary screen if it is not shown yet)."""
    screen = None
    if widget is not None and widget.screen() is not None:
        screen = widget.screen()
    if screen is None:
        screen = QGuiApplication.primaryScreen()
    if screen is None:  # pragma: no cover - offscreen platform with no screen
        return 1024, 768
    geometry = screen.availableGeometry()
    return geometry.width(), geometry.height()


def fit_to_screen(widget: QWidget, preferred_width: int, preferred_height: int,
                  *, minimum_width: int | None = None,
                  minimum_height: int | None = None) -> None:
    """Sizes `widget` to its preferred size, clamped to what fits.

    Both the preferred size and the minimum are capped: a minimum larger
    than the screen is the specific bug that makes a window impossible to
    use, since the user cannot resize below it. Sizes are passed through
    theme.scale() first, so they track the UI font as well as the DPI.
    """
    max_width, max_height = available_size(widget)
    max_width = int(max_width * _MAX_FRACTION)
    max_height = int(max_height * _MAX_FRACTION)

    width = min(scale(preferred_width), max_width)
    height = min(scale(preferred_height), max_height)

    floor_width = scale(minimum_width) if minimum_width is not None else min(width, scale(320))
    floor_height = scale(minimum_height) if minimum_height is not None else min(height, scale(240))
    widget.setMinimumSize(min(floor_width, max_width), min(floor_height, max_height))
    widget.resize(width, height)


def constrain_dialog(dialog: QWidget, preferred_width: int,
                     preferred_height: int | None = None) -> None:
    """For a dialog that sizes itself to its content.

    Most form dialogs here set only a minimum width and let the layout
    decide the height. That is fine, and worth keeping -- but two things can
    still go wrong on a small or heavily-scaled screen, and both are fixed
    here without disturbing how the dialog lays itself out:

    * a minimum wider than the screen cannot be resized out of;
    * content taller than the screen pushes the footer buttons past the
      bottom edge, where they cannot be clicked.

    So the minimum is scaled *and* capped, and a maximum height is imposed
    so the dialog can never grow past what is visible.
    """
    max_width, max_height = available_size(dialog)
    max_width = int(max_width * _MAX_FRACTION)
    max_height = int(max_height * _MAX_FRACTION)

    dialog.setMinimumWidth(min(scale(preferred_width), max_width))
    dialog.setMaximumHeight(max_height)
    if preferred_height is not None:
        dialog.resize(min(scale(preferred_width), max_width),
                      min(scale(preferred_height), max_height))


def keep_on_screen(widget: QWidget) -> None:
    """Brings an already-positioned window fully back onto the screen.

    Two cases. Qt centres a dialog on its parent, which pushes part of it
    off the edge when the parent sits near one. And restored geometry can
    come from a *different* display than the one now attached — a laptop
    docked to a 1920px monitor yesterday, undocked to its own 1366px screen
    today — so the size is clamped as well as the position. Clamping only
    the position would leave a window wider than the screen, with its
    right-hand edge (and whatever is on it) unreachable.
    """
    screen = widget.screen() or QGuiApplication.primaryScreen()
    if screen is None:  # pragma: no cover - offscreen platform with no screen
        return
    bounds = screen.availableGeometry()

    max_width = int(bounds.width() * _MAX_FRACTION)
    max_height = int(bounds.height() * _MAX_FRACTION)
    if widget.width() > max_width or widget.height() > max_height:
        widget.resize(min(widget.width(), max_width), min(widget.height(), max_height))

    frame = widget.frameGeometry()
    x = min(max(frame.x(), bounds.left()), max(bounds.left(), bounds.right() - frame.width()))
    y = min(max(frame.y(), bounds.top()), max(bounds.top(), bounds.bottom() - frame.height()))
    if (x, y) != (frame.x(), frame.y()):
        widget.move(x, y)


def wrap_in_scroll(body: QWidget) -> QScrollArea:
    """Puts `body` in a transparent, resizable scroll area.

    Intended for the *body* of a form only. Keeping the footer outside is
    the whole point: when the window is shorter than the form, the fields
    scroll and the Save/Cancel buttons stay visible. Wrapping the footer too
    is what pushed them off-screen.
    """
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    area.setViewportMargins(0, 0, 0, 0)
    # Without this the scroll area paints its own default background over
    # the themed one, showing a grey panel behind every wrapped form.
    area.setStyleSheet("QScrollArea { background: transparent; }")
    body.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    area.setWidget(body)
    return area
