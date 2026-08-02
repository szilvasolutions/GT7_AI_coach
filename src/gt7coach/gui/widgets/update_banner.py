"""Non-blocking 'Update available' banner shown at the top of MainWindow.

Two buttons:

* **View release** opens the GitHub release page in the user's default
  browser.
* **Download & install** triggers the Phase D self-updater. For users
  who installed via ``pip``, this button is disabled with a tooltip
  pointing at ``pip install -U`` / ``git pull``.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QWidget,
)

from gt7coach.gui.updater import UpdateInfo, is_frozen_bundle


class UpdateBanner(QFrame):
    """Light-blue strip across the top of the window. Hidden by default."""

    download_clicked = Signal(object)  # UpdateInfo

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(
            "QFrame { background-color: #1d3049; border: 1px solid #2f5379; border-radius: 4px; }"
            "QLabel { background: transparent; color: #cfe2f7; }"
        )
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setVisible(False)

        self._label = QLabel("Update available")
        self._label.setTextFormat(Qt.TextFormat.RichText)
        self._label.setTextInteractionFlags(
            Qt.TextInteractionFlag.LinksAccessibleByMouse
            | Qt.TextInteractionFlag.TextSelectableByMouse
        )

        self._view_btn = QPushButton("View release")
        self._view_btn.clicked.connect(self._on_view)
        self._download_btn = QPushButton("Download && install")
        self._download_btn.clicked.connect(self._on_download)
        self._dismiss_btn = QPushButton("✕")
        self._dismiss_btn.setFixedWidth(28)
        self._dismiss_btn.setToolTip("Dismiss this notification")
        self._dismiss_btn.clicked.connect(lambda: self.setVisible(False))

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.addWidget(self._label, stretch=1)
        layout.addWidget(self._view_btn)
        layout.addWidget(self._download_btn)
        layout.addWidget(self._dismiss_btn)

        self._info: UpdateInfo | None = None
        self._frozen = is_frozen_bundle()

    def show_for(self, info: UpdateInfo) -> None:
        self._info = info
        self._label.setText(
            f"<b>Update available:</b> {info.tag} — {info.name}. <i>Restart after install.</i>"
        )
        if self._frozen and info.zip_url:
            self._download_btn.setEnabled(True)
            self._download_btn.setToolTip("Download and install the new build, then relaunch.")
        else:
            self._download_btn.setEnabled(False)
            if not info.zip_url:
                self._download_btn.setToolTip(
                    "No Windows installer asset on this release. "
                    "Use 'View release' for download options."
                )
            else:
                self._download_btn.setToolTip(
                    "This binary was installed from source (pip).\n"
                    "Update with:  git pull && pip install -e ."
                )
        self.setVisible(True)

    def _on_view(self) -> None:
        if self._info and self._info.html_url:
            from PySide6.QtCore import QUrl

            QDesktopServices.openUrl(QUrl(self._info.html_url))

    def _on_download(self) -> None:
        if self._info is not None:
            self.download_clicked.emit(self._info)
