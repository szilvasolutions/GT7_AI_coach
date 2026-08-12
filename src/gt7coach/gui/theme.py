"""Dark pit-wall theme for the GUI.

One QSS sheet applied at the QApplication level (on top of the Fusion
style so the base widget metrics are identical on Windows and Linux).
Colours live in module constants so widgets that need to style
programmatically (e.g. the connection dot) stay consistent with the
sheet.
"""

from __future__ import annotations

from pathlib import Path

_ASSETS = Path(__file__).resolve().parent / "assets"

# Palette ---------------------------------------------------------------

BG = "#14171c"  # window background
SURFACE = "#1c2128"  # panels, group boxes, tabs
SURFACE_ALT = "#232a33"  # hover / alternate rows
BORDER = "#2e3642"
TEXT = "#e8eaed"
TEXT_DIM = "#98a2b3"
ACCENT = "#e5484d"  # racing red — selection, slider, highlights
GREEN = "#46a758"  # start / connected
AMBER = "#f5b950"  # warnings / silent receiver
RED = "#e5484d"  # stop / errors / disconnected
CONSOLE_BG = "#0f1216"
CONSOLE_TEXT = "#c7d0da"

# Log-line colours used by the console's colorizer.
LOG_COLOR_ERROR = "#ff7373"
LOG_COLOR_WARNING = AMBER
LOG_COLOR_ADVICE = "#7bd88f"


def build_stylesheet() -> str:
    # QSS url() wants forward slashes even on Windows.
    arrow = (_ASSETS / "arrow_down.svg").as_posix()
    return f"""
QWidget {{
    background-color: {BG};
    color: {TEXT};
    font-size: 10pt;
}}

/* ---- Toolbar ------------------------------------------------------- */
QToolBar {{
    background: {SURFACE};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 5px;
    spacing: 4px;
}}
QToolBar QLabel {{
    background: transparent;
    color: {TEXT_DIM};
}}
QToolButton {{
    background: {SURFACE_ALT};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 5px 12px;
}}
QToolButton:hover {{ background: #2b333e; }}
QToolButton:disabled {{ color: #5a6372; background: {SURFACE}; }}
QToolButton#startButton {{
    background: {GREEN};
    color: white;
    font-weight: bold;
    border: none;
}}
QToolButton#startButton:hover {{ background: #3d9450; }}
QToolButton#startButton:disabled {{ background: #2a3a2e; color: #6b7a6e; }}
QToolButton#stopButton {{
    background: {RED};
    color: white;
    font-weight: bold;
    border: none;
}}
QToolButton#stopButton:hover {{ background: #d13f44; }}
QToolButton#stopButton:disabled {{ background: #3a2a2b; color: #7a6b6c; }}

/* ---- Inputs -------------------------------------------------------- */
QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox {{
    background: {SURFACE_ALT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 4px 8px;
    selection-background-color: {ACCENT};
}}
QComboBox:hover, QLineEdit:focus {{ border-color: {TEXT_DIM}; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox::down-arrow {{ image: url({arrow}); width: 10px; height: 6px; }}
QComboBox QAbstractItemView {{
    background: {SURFACE_ALT};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT};
}}

QSlider::groove:horizontal {{
    height: 4px;
    background: {BORDER};
    border-radius: 2px;
}}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    width: 14px;
    margin: -6px 0;
    border-radius: 7px;
    background: {TEXT};
}}

/* ---- Tabs ---------------------------------------------------------- */
QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-radius: 4px;
    background: {SURFACE};
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: {TEXT_DIM};
    padding: 7px 18px;
    border: none;
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{
    color: {TEXT};
    border-bottom: 2px solid {ACCENT};
}}
QTabBar::tab:hover {{ color: {TEXT}; }}

/* ---- Group boxes ---------------------------------------------------- */
QGroupBox {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 6px;
    margin-top: 12px;
    padding: 8px 4px 4px 4px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: {TEXT_DIM};
    font-weight: bold;
    text-transform: uppercase;
}}
QGroupBox QLabel {{ background: transparent; }}

/* Round "?" badge next to each Config dialog field. Visible enough to
   invite a hover, quiet enough not to compete with the inputs. */
QLabel#helpIcon {{
    background: {SURFACE_ALT};
    color: {TEXT_DIM};
    border: 1px solid {BORDER};
    border-radius: 9px;
    font-weight: bold;
    font-size: 10pt;
}}
QLabel#helpIcon:hover {{
    background: {ACCENT};
    color: {TEXT};
    border-color: {ACCENT};
}}

/* ---- Console -------------------------------------------------------- */
QWidget#consolePanel {{
    background: {CONSOLE_BG};
    border: 1px solid {BORDER};
    border-radius: 4px;
}}
QWidget#consoleHeader {{ background: {SURFACE}; border-bottom: 1px solid {BORDER}; }}
QLabel#consoleTitle {{
    background: transparent;
    color: {TEXT_DIM};
    font-weight: bold;
    letter-spacing: 1px;
}}
QToolButton#consoleButton {{
    background: transparent;
    border: none;
    padding: 2px 8px;
    color: {TEXT_DIM};
}}
QToolButton#consoleButton:hover {{ color: {TEXT}; background: {SURFACE_ALT}; }}
QPlainTextEdit#consoleView {{
    background: {CONSOLE_BG};
    color: {CONSOLE_TEXT};
    border: none;
    selection-background-color: {ACCENT};
}}

/* ---- Status panel accents --------------------------------------------- */
QLabel#adviceLabel {{ color: {LOG_COLOR_ADVICE}; font-size: 10.5pt; }}
QLabel#bestLapLabel {{ color: {GREEN}; font-weight: bold; }}

/* ---- Item views ------------------------------------------------------ */
QListWidget, QTableWidget, QTreeView {{
    background: {SURFACE};
    alternate-background-color: {SURFACE_ALT};
    border: 1px solid {BORDER};
    border-radius: 4px;
}}
QHeaderView::section {{
    background: {SURFACE_ALT};
    color: {TEXT_DIM};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 5px 8px;
    font-weight: bold;
}}
QTableWidget::item:selected, QListWidget::item:selected {{ background: {ACCENT}; }}

/* ---- Menus / status bar ---------------------------------------------- */
QMenuBar {{ background: {BG}; border-bottom: 1px solid {BORDER}; }}
QMenuBar::item {{ padding: 5px 10px; background: transparent; }}
QMenuBar::item:selected {{ background: {SURFACE_ALT}; border-radius: 4px; }}
QMenu {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    padding: 4px;
}}
QMenu::item {{ padding: 5px 24px; border-radius: 4px; }}
QMenu::item:selected {{ background: {ACCENT}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}
QStatusBar {{
    background: {SURFACE};
    color: {TEXT_DIM};
    border-top: 1px solid {BORDER};
}}

/* ---- Splitter / scrollbars ------------------------------------------- */
QSplitter::handle {{ background: {BG}; width: 6px; }}
QSplitter::handle:hover {{ background: {BORDER}; }}
QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {BORDER}; border-radius: 5px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {TEXT_DIM}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{
    background: transparent; height: 10px; margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: {BORDER}; border-radius: 5px; min-width: 30px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

/* ---- Dialogs ---------------------------------------------------------- */
QDialog {{ background: {BG}; }}
QPushButton {{
    background: {SURFACE_ALT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 6px 16px;
}}
QPushButton:hover {{ background: #2b333e; }}
QPushButton:default {{ background: {ACCENT}; border: none; color: white; font-weight: bold; }}
"""


def apply_theme(app) -> None:
    """Apply Fusion + the dark sheet to a QApplication."""
    app.setStyle("Fusion")
    app.setStyleSheet(build_stylesheet())
