from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from aqt import gui_hooks, mw
from aqt.qt import (
    QAbstractItemView,
    QAction,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
    Qt,
    qconnect,
)
from aqt.utils import showText, showWarning, tooltip

from .backend import (
    KimchiAddonError,
    KimchiSyncBackend,
    SyncPreview,
    SyncRunResult,
    SyncSettings,
)


MENU_LABEL = "Sync Deck with Kimchi Reader"


@dataclass(frozen=True)
class AddonConfig:
    mature_interval_days: int
    max_korean_tokens: int
    kimchi_lemmatize_batch_size: int
    kimchi_status_batch_size: int


class DeckSelectionDialog(QDialog):
    def __init__(self, deck_names: Sequence[str], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(MENU_LABEL)
        self.setMinimumWidth(520)
        self.setMinimumHeight(420)

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Choose the Anki deck(s) you want to sync to Kimchi Reader.",
                self,
            )
        )

        self.deck_list = QListWidget(self)
        self.deck_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        for deck_name in deck_names:
            item = QListWidgetItem(deck_name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.deck_list.addItem(item)
        layout.addWidget(self.deck_list)

        selection_row = QHBoxLayout()
        self.select_all_button = QPushButton("Select All", self)
        self.clear_all_button = QPushButton("Clear All", self)
        selection_row.addWidget(self.select_all_button)
        selection_row.addWidget(self.clear_all_button)
        selection_row.addStretch(1)
        layout.addLayout(selection_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        layout.addWidget(buttons)

        qconnect(self.select_all_button.clicked, self._select_all)
        qconnect(self.clear_all_button.clicked, self._clear_all)
        qconnect(buttons.accepted, self.accept)
        qconnect(buttons.rejected, self.reject)

    def selected_deck_names(self) -> List[str]:
        selected = []
        for index in range(self.deck_list.count()):
            item = self.deck_list.item(index)
            if item.checkState() == Qt.CheckState.Checked:
                selected.append(item.text())
        return selected

    def _select_all(self) -> None:
        for index in range(self.deck_list.count()):
            self.deck_list.item(index).setCheckState(Qt.CheckState.Checked)

    def _clear_all(self) -> None:
        for index in range(self.deck_list.count()):
            self.deck_list.item(index).setCheckState(Qt.CheckState.Unchecked)


class KimchiAuthDialog(QDialog):
    def __init__(
        self,
        backend: KimchiSyncBackend,
        default_email: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.backend = backend
        self.code_requested = False

        self.setWindowTitle("Kimchi Reader Login")
        self.setModal(True)
        self.setMinimumWidth(640)
        self.setMinimumHeight(360)
        self.resize(700, 420)

        self.setStyleSheet(
            """
            QFrame#kimchiAuthCard {
                border: 1px solid palette(midlight);
                border-radius: 12px;
                background: palette(base);
            }
            QLabel#kimchiAuthTitle {
                font-size: 18px;
                font-weight: 600;
            }
            QLabel#kimchiAuthSubtitle {
                color: palette(mid);
            }
            QLabel#kimchiAuthStatus {
                padding-top: 4px;
            }
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)

        title = QLabel("Sign in to Kimchi Reader", self)
        title.setObjectName("kimchiAuthTitle")
        layout.addWidget(title)

        subtitle = QLabel(
            "Enter your email to receive a one-time code. "
            "The add-on stores your Kimchi session locally so you should only need "
            "to do this occasionally.",
            self,
        )
        subtitle.setObjectName("kimchiAuthSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        card = QFrame(self)
        card.setObjectName("kimchiAuthCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 16, 16, 16)
        card_layout.setSpacing(12)
        layout.addWidget(card)

        form_layout = QFormLayout()
        form_layout.setContentsMargins(0, 0, 0, 0)
        form_layout.setSpacing(10)
        form_layout.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow
        )
        form_layout.setLabelAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        card_layout.addLayout(form_layout)

        email_row = QHBoxLayout()
        email_row.setSpacing(8)
        self.email_input = QLineEdit(default_email, card)
        self.email_input.setPlaceholderText("you@example.com")
        self.email_input.setClearButtonEnabled(True)
        self.email_input.setMinimumWidth(320)
        email_row.addWidget(self.email_input, 1)

        self.send_code_button = QPushButton("Send Code", card)
        self.send_code_button.setMinimumWidth(120)
        self.send_code_button.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Fixed,
        )
        email_row.addWidget(self.send_code_button)
        form_layout.addRow("Email", email_row)

        self.otp_input = QLineEdit(card)
        self.otp_input.setPlaceholderText("Enter the one-time code from your email")
        self.otp_input.setClearButtonEnabled(True)
        form_layout.addRow("Code", self.otp_input)

        helper = QLabel(
            "No password is needed here. Kimchi Reader will email you a temporary login code.",
            card,
        )
        helper.setWordWrap(True)
        helper.setObjectName("kimchiAuthSubtitle")
        card_layout.addWidget(helper)

        self.status_label = QLabel("", card)
        self.status_label.setObjectName("kimchiAuthStatus")
        self.status_label.setWordWrap(True)
        self.status_label.hide()
        card_layout.addWidget(self.status_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self.sign_in_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.sign_in_button.setText("Sign In")
        self.sign_in_button.setEnabled(False)
        layout.addWidget(buttons)

        qconnect(self.send_code_button.clicked, self._request_code)
        qconnect(self.email_input.textChanged, self._update_buttons)
        qconnect(self.otp_input.textChanged, self._update_buttons)
        qconnect(buttons.accepted, self._authenticate)
        qconnect(buttons.rejected, self.reject)

        self.email_input.setFocus()
        if default_email:
            self.otp_input.setFocus()

        self._ensure_fits()

    def _set_status(self, message: str, error: bool = False) -> None:
        if not message:
            self.status_label.clear()
            self.status_label.hide()
            self._ensure_fits()
            return

        color = "#b3261e" if error else "#2e7d32"
        self.status_label.setText(
            '<span style="color: %s;">%s</span>' % (color, message)
        )
        self.status_label.show()
        self._ensure_fits()

    def _ensure_fits(self) -> None:
        self.layout().invalidate()
        self.layout().activate()
        hint = self.sizeHint()
        self.resize(
            max(self.width(), 700, hint.width()),
            max(self.height(), 420, hint.height()),
        )

    def _update_buttons(self, *_args: object) -> None:
        email_ready = bool(self.email_input.text().strip())
        otp_ready = bool(self.otp_input.text().strip())
        self.send_code_button.setEnabled(email_ready)
        self.sign_in_button.setEnabled(email_ready and otp_ready)
        if self.status_label.isHidden():
            return
        if not email_ready:
            self._set_status("")

    def _request_code(self) -> None:
        email = self.email_input.text().strip()
        if not email:
            self._set_status("Enter your email address first.", error=True)
            self.email_input.setFocus()
            return

        self.send_code_button.setEnabled(False)
        self.send_code_button.setText("Sending...")
        try:
            self.backend.request_email_otp(email)
        except KimchiAddonError as exc:
            self._set_status(str(exc), error=True)
            self.send_code_button.setText("Send Code")
            self._update_buttons()
            return

        self.code_requested = True
        self._set_status(
            "Code sent to %s. Check your inbox and paste the code below." % email
        )
        self.send_code_button.setText("Resend Code")
        self._update_buttons()
        self.otp_input.setFocus()

    def _authenticate(self) -> None:
        email = self.email_input.text().strip()
        otp = self.otp_input.text().strip()

        if not email:
            self._set_status("Enter your email address first.", error=True)
            self.email_input.setFocus()
            return

        if not otp:
            self._set_status("Enter the one-time code from your email.", error=True)
            self.otp_input.setFocus()
            return

        self.sign_in_button.setEnabled(False)
        self.sign_in_button.setText("Signing In...")
        try:
            self.backend.authenticate_with_otp(email, otp)
        except KimchiAddonError as exc:
            self._set_status(str(exc), error=True)
            self.sign_in_button.setText("Sign In")
            self._update_buttons()
            return

        self.accept()


def kimchi_panel_stylesheet() -> str:
    return """
        QDialog#kimchiPanelDialog {
            background: #171717;
            color: #f5f5f5;
        }
        QFrame#kimchiResultsCard {
            background: #101010;
            border: 1px solid #262626;
            border-radius: 18px;
        }
        QFrame#kimchiResultsDot {
            background: #4ade80;
            border-radius: 10px;
        }
        QFrame#kimchiMetricChip {
            background: #1a1a1a;
            border: 1px solid #2f2f2f;
            border-radius: 12px;
        }
        QFrame#kimchiWordRow {
            background: #141414;
            border: 1px solid #232323;
            border-radius: 12px;
        }
        QLabel#kimchiResultsTitle {
            font-size: 16px;
            font-weight: 700;
            color: #d4d4d4;
        }
        QLabel#kimchiResultsCardTitle {
            font-size: 18px;
            font-weight: 700;
            color: #f5f5f5;
        }
        QLabel#kimchiResultsSubtitle {
            font-size: 13px;
            color: #a3a3a3;
        }
        QLabel#kimchiResultsCount {
            font-size: 17px;
            font-weight: 700;
            color: #4ade80;
        }
        QLabel#kimchiMetricValue {
            font-size: 18px;
            font-weight: 700;
            color: #f5f5f5;
        }
        QLabel#kimchiMetricLabel {
            font-size: 12px;
            color: #8f8f8f;
        }
        QLabel#kimchiSectionTitle {
            font-size: 12px;
            font-weight: 700;
            color: #9ca3af;
        }
        QLabel#kimchiDeckLabel {
            font-size: 13px;
            color: #cfcfcf;
        }
        QLabel#kimchiWordText {
            font-size: 18px;
            font-weight: 600;
            color: #ececec;
        }
        QLabel#kimchiStatusKnown {
            color: #9be220;
            font-size: 16px;
            font-weight: 700;
        }
        QLabel#kimchiStatusSeen {
            color: #ff9d3f;
            font-size: 16px;
            font-weight: 700;
        }
        QLabel#kimchiArtifactsLabel {
            font-size: 12px;
            color: #8f8f8f;
        }
        QLabel#kimchiArtifactsPath {
            font-size: 13px;
            color: #d6d6d6;
        }
        QScrollArea {
            border: none;
            background: transparent;
        }
        QScrollBar:vertical {
            background: #111111;
            width: 10px;
            margin: 4px 0 4px 0;
        }
        QScrollBar::handle:vertical {
            background: #333333;
            min-height: 28px;
            border-radius: 5px;
        }
        QScrollBar::add-line:vertical,
        QScrollBar::sub-line:vertical {
            height: 0px;
        }
    """


def build_metric_chip(label: str, value: str, parent: QWidget) -> QFrame:
    chip = QFrame(parent)
    chip.setObjectName("kimchiMetricChip")
    chip_layout = QVBoxLayout(chip)
    chip_layout.setContentsMargins(14, 12, 14, 12)
    chip_layout.setSpacing(2)

    value_label = QLabel(value, chip)
    value_label.setObjectName("kimchiMetricValue")
    chip_layout.addWidget(value_label)

    label_widget = QLabel(label, chip)
    label_widget.setObjectName("kimchiMetricLabel")
    chip_layout.addWidget(label_widget)
    return chip


def build_word_row(word: str, status: str, parent: QWidget) -> QFrame:
    row = QFrame(parent)
    row.setObjectName("kimchiWordRow")
    row_layout = QHBoxLayout(row)
    row_layout.setContentsMargins(16, 14, 16, 14)
    row_layout.setSpacing(12)

    word_label = QLabel(word, row)
    word_label.setObjectName("kimchiWordText")
    word_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    row_layout.addWidget(word_label, 1)

    status_label = QLabel(status, row)
    if status.lower() == "known":
        status_label.setObjectName("kimchiStatusKnown")
    else:
        status_label.setObjectName("kimchiStatusSeen")
    row_layout.addWidget(
        status_label,
        0,
        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
    )
    return row


def build_plan_summary_label(preview_or_result) -> str:
    plan = preview_or_result.plan
    return (
        "%d planned known, %d planned seen, %d unstudied-only expressions, %d extraction skips."
        % (
            len(plan.known_expressions),
            len(plan.seen_expressions),
            len(plan.planning_skips),
            len(plan.skipped),
        )
    )


class SyncPreviewDialog(QDialog):
    def __init__(self, preview: SyncPreview, parent=None) -> None:
        super().__init__(parent)
        self.preview = preview

        self.setObjectName("kimchiPanelDialog")
        self.setWindowTitle("Review Kimchi Reader Changes")
        self.setModal(True)
        self.setMinimumWidth(860)
        self.setMinimumHeight(640)
        self.resize(980, 760)
        self.setStyleSheet(kimchi_panel_stylesheet())

        pending_rows = [("Seen", word) for word in preview.pending_seen_words]
        pending_rows.extend(("Known", word) for word in preview.pending_known_words)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 22, 28, 22)
        layout.setSpacing(18)

        header_title = QLabel("Review Changes", self)
        header_title.setObjectName("kimchiResultsTitle")
        layout.addWidget(header_title)

        card = QFrame(self)
        card.setObjectName("kimchiResultsCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(20, 18, 20, 18)
        card_layout.setSpacing(18)
        layout.addWidget(card, 1)

        top_row = QHBoxLayout()
        top_row.setSpacing(12)
        top_row.setAlignment(Qt.AlignmentFlag.AlignTop)
        card_layout.addLayout(top_row)

        left_header = QHBoxLayout()
        left_header.setSpacing(12)
        left_header.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        top_row.addLayout(left_header, 1)

        dot = QFrame(card)
        dot.setObjectName("kimchiResultsDot")
        dot.setFixedSize(20, 20)
        left_header.addWidget(dot, 0, Qt.AlignmentFlag.AlignTop)

        title_column = QVBoxLayout()
        title_column.setSpacing(4)
        left_header.addLayout(title_column, 1)

        title = QLabel(
            "Confirm before syncing"
            if pending_rows
            else "No new word changes needed",
            card,
        )
        title.setObjectName("kimchiResultsCardTitle")
        title_column.addWidget(title)

        subtitle = QLabel(
            "These are the Kimchi Reader status changes that will be sent if you continue."
            if pending_rows
            else "Everything from the selected decks is already up to date or currently unavailable in Kimchi Reader.",
            card,
        )
        subtitle.setObjectName("kimchiResultsSubtitle")
        subtitle.setWordWrap(True)
        title_column.addWidget(subtitle)

        count_label = QLabel(
            "%d pending change%s"
            % (len(pending_rows), "" if len(pending_rows) == 1 else "s"),
            card,
        )
        count_label.setObjectName("kimchiResultsCount")
        top_row.addWidget(
            count_label,
            0,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
        )

        metrics_row = QHBoxLayout()
        metrics_row.setSpacing(12)
        card_layout.addLayout(metrics_row)
        metrics_row.addWidget(build_metric_chip("Pending", str(len(pending_rows)), card))
        metrics_row.addWidget(
            build_metric_chip("Known", str(len(preview.pending_known_words)), card)
        )
        metrics_row.addWidget(
            build_metric_chip("Seen", str(len(preview.pending_seen_words)), card)
        )
        metrics_row.addWidget(
            build_metric_chip("Already Marked", str(preview.total_already_marked), card)
        )
        metrics_row.addWidget(
            build_metric_chip("Unavailable", str(preview.total_cached_invalid), card)
        )
        metrics_row.addStretch(1)

        decks_title = QLabel("SELECTED DECKS", card)
        decks_title.setObjectName("kimchiSectionTitle")
        card_layout.addWidget(decks_title)

        decks_label = QLabel("  ".join(preview.selected_decks), card)
        decks_label.setObjectName("kimchiDeckLabel")
        decks_label.setWordWrap(True)
        card_layout.addWidget(decks_label)

        summary_title = QLabel("PLAN SUMMARY", card)
        summary_title.setObjectName("kimchiSectionTitle")
        card_layout.addWidget(summary_title)

        summary_label = QLabel(build_plan_summary_label(preview), card)
        summary_label.setObjectName("kimchiDeckLabel")
        summary_label.setWordWrap(True)
        card_layout.addWidget(summary_label)

        changes_title = QLabel("PENDING CHANGES", card)
        changes_title.setObjectName("kimchiSectionTitle")
        card_layout.addWidget(changes_title)

        scroll = QScrollArea(card)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        card_layout.addWidget(scroll, 1)

        scroll_content = QWidget(scroll)
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.setSpacing(10)
        scroll.setWidget(scroll_content)

        if pending_rows:
            for status, word in pending_rows:
                scroll_layout.addWidget(build_word_row(word, status, scroll_content))
        else:
            empty_state = QLabel(
                "No new words would be sent right now. You can close this dialog safely.",
                scroll_content,
            )
            empty_state.setObjectName("kimchiResultsSubtitle")
            empty_state.setWordWrap(True)
            scroll_layout.addWidget(empty_state)

        scroll_layout.addStretch(1)

        buttons = QDialogButtonBox(parent=self)
        if pending_rows:
            buttons.setStandardButtons(
                QDialogButtonBox.StandardButton.Cancel
                | QDialogButtonBox.StandardButton.Ok
            )
            apply_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
            apply_button.setText("Apply Changes")
        else:
            buttons.setStandardButtons(QDialogButtonBox.StandardButton.Close)
        qconnect(buttons.rejected, self.reject)
        qconnect(buttons.accepted, self.accept)
        layout.addWidget(buttons)


class SyncResultsDialog(QDialog):
    def __init__(self, result: SyncRunResult, parent=None) -> None:
        super().__init__(parent)
        self.result = result

        self.setObjectName("kimchiPanelDialog")
        self.setWindowTitle("Kimchi Reader Status Changes")
        self.setModal(True)
        self.setMinimumWidth(860)
        self.setMinimumHeight(640)
        self.resize(980, 760)
        self.setStyleSheet(kimchi_panel_stylesheet())

        changed_rows = [("Seen", word) for word in result.submitted_seen_words]
        changed_rows.extend(("Known", word) for word in result.submitted_known_words)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 22, 28, 22)
        layout.setSpacing(18)

        header_title = QLabel("Processing Results", self)
        header_title.setObjectName("kimchiResultsTitle")
        layout.addWidget(header_title)

        card = QFrame(self)
        card.setObjectName("kimchiResultsCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(20, 18, 20, 18)
        card_layout.setSpacing(18)
        layout.addWidget(card, 1)

        top_row = QHBoxLayout()
        top_row.setSpacing(12)
        top_row.setAlignment(Qt.AlignmentFlag.AlignTop)
        card_layout.addLayout(top_row)

        left_header = QHBoxLayout()
        left_header.setSpacing(12)
        left_header.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        top_row.addLayout(left_header, 1)

        dot = QFrame(card)
        dot.setObjectName("kimchiResultsDot")
        dot.setFixedSize(20, 20)
        left_header.addWidget(dot, 0, Qt.AlignmentFlag.AlignTop)

        title_column = QVBoxLayout()
        title_column.setSpacing(4)
        left_header.addLayout(title_column, 1)

        title = QLabel(
            "Status changes applied" if changed_rows else "No new word changes needed",
            card,
        )
        title.setObjectName("kimchiResultsCardTitle")
        title_column.addWidget(title)

        subtitle = QLabel(
            "Kimchi Reader sync finished for %d selected deck(s)." % len(result.selected_decks),
            card,
        )
        subtitle.setObjectName("kimchiResultsSubtitle")
        title_column.addWidget(subtitle)

        count_label = QLabel(
            "%d status change%s"
            % (len(changed_rows), "" if len(changed_rows) == 1 else "s"),
            card,
        )
        count_label.setObjectName("kimchiResultsCount")
        top_row.addWidget(count_label, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)

        metrics_row = QHBoxLayout()
        metrics_row.setSpacing(12)
        card_layout.addLayout(metrics_row)
        metrics_row.addWidget(build_metric_chip("Changed", str(len(changed_rows)), card))
        metrics_row.addWidget(
            build_metric_chip(
                "Known",
                str(len(result.submitted_known_words)),
                card,
            )
        )
        metrics_row.addWidget(
            build_metric_chip(
                "Seen",
                str(len(result.submitted_seen_words)),
                card,
            )
        )
        metrics_row.addWidget(
            build_metric_chip(
                "Skipped",
                str(len(result.plan.skipped) + len(result.plan.planning_skips)),
                card,
            )
        )
        metrics_row.addStretch(1)

        decks_title = QLabel("SELECTED DECKS", card)
        decks_title.setObjectName("kimchiSectionTitle")
        card_layout.addWidget(decks_title)

        decks_label = QLabel("  ".join(result.selected_decks), card)
        decks_label.setObjectName("kimchiDeckLabel")
        decks_label.setWordWrap(True)
        card_layout.addWidget(decks_label)

        summary_title = QLabel("PLAN SUMMARY", card)
        summary_title.setObjectName("kimchiSectionTitle")
        card_layout.addWidget(summary_title)

        summary_label = QLabel(build_plan_summary_label(result), card)
        summary_label.setObjectName("kimchiDeckLabel")
        summary_label.setWordWrap(True)
        card_layout.addWidget(summary_label)

        changes_title = QLabel("STATUS CHANGES", card)
        changes_title.setObjectName("kimchiSectionTitle")
        card_layout.addWidget(changes_title)

        scroll = QScrollArea(card)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        card_layout.addWidget(scroll, 1)

        scroll_content = QWidget(scroll)
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(0, 0, 0, 0)
        scroll_layout.setSpacing(10)
        scroll.setWidget(scroll_content)

        if changed_rows:
            for status, word in changed_rows:
                scroll_layout.addWidget(build_word_row(word, status, scroll_content))
        else:
            empty_state = QLabel(
                "Everything from the selected decks already matched Kimchi Reader's current statuses, so nothing new had to be submitted.",
                scroll_content,
            )
            empty_state.setObjectName("kimchiResultsSubtitle")
            empty_state.setWordWrap(True)
            scroll_layout.addWidget(empty_state)

        scroll_layout.addStretch(1)

        artifacts_title = QLabel("ARTIFACTS", card)
        artifacts_title.setObjectName("kimchiArtifactsLabel")
        card_layout.addWidget(artifacts_title)

        artifacts_path = QLabel(result.artifacts_dir, card)
        artifacts_path.setObjectName("kimchiArtifactsPath")
        artifacts_path.setWordWrap(True)
        artifacts_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        card_layout.addWidget(artifacts_path)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=self)
        qconnect(buttons.rejected, self.reject)
        qconnect(buttons.accepted, self.accept)
        layout.addWidget(buttons)


def addon_config() -> AddonConfig:
    payload = mw.addonManager.getConfig(__name__) or {}
    return AddonConfig(
        mature_interval_days=int(payload.get("mature_interval_days", 21)),
        max_korean_tokens=int(payload.get("max_korean_tokens", 2)),
        kimchi_lemmatize_batch_size=int(payload.get("kimchi_lemmatize_batch_size", 500)),
        kimchi_status_batch_size=int(payload.get("kimchi_status_batch_size", 500)),
    )


def addon_dir() -> Path:
    return Path(__file__).resolve().parent


def addon_user_files_dir() -> Path:
    path = addon_dir() / "user_files"
    path.mkdir(parents=True, exist_ok=True)
    return path


def current_profile_dir() -> Path:
    return Path(mw.pm.profileFolder()).resolve()


def current_deck_names() -> List[str]:
    if mw.col is None:
        return []

    deck_names = []
    try:
        entries = mw.col.decks.all_names_and_ids()
    except Exception:
        entries = []

    for entry in entries:
        deck_name = extract_deck_name(entry)
        if deck_name:
            deck_names.append(deck_name)

    return sorted(set(deck_names), key=lambda name: name.lower())


def extract_deck_name(entry: Any) -> Optional[str]:
    if isinstance(entry, dict):
        value = entry.get("name")
        return str(value) if value else None

    if hasattr(entry, "name"):
        value = getattr(entry, "name")
        return str(value) if value else None

    if isinstance(entry, (tuple, list)) and entry:
        first = entry[0]
        if isinstance(first, str):
            return first

    return None


def build_backend() -> KimchiSyncBackend:
    config = addon_config()
    settings = SyncSettings(
        mature_interval_days=config.mature_interval_days,
        max_korean_tokens=config.max_korean_tokens,
        kimchi_lemmatize_batch_size=config.kimchi_lemmatize_batch_size,
        kimchi_status_batch_size=config.kimchi_status_batch_size,
    )
    return KimchiSyncBackend(
        profile_dir=current_profile_dir(),
        user_files_dir=addon_user_files_dir(),
        settings=settings,
    )


def ensure_authenticated(backend: KimchiSyncBackend) -> bool:
    try:
        if backend.ensure_authenticated():
            return True
    except KimchiAddonError:
        pass

    default_email = backend.current_email() or ""
    dialog = KimchiAuthDialog(backend, default_email, mw)
    return dialog.exec() == QDialog.DialogCode.Accepted


def present_sync_result(result: SyncRunResult) -> None:
    tooltip("Kimchi Reader sync finished.")
    dialog = SyncResultsDialog(result, mw)
    dialog.exec()


def present_sync_preview(preview: SyncPreview) -> bool:
    dialog = SyncPreviewDialog(preview, mw)
    dialog.exec()
    return preview.total_pending_words > 0 and dialog.result() == int(
        QDialog.DialogCode.Accepted
    )


def on_sync_menu_triggered() -> None:
    if mw.col is None:
        showWarning("Open a collection before syncing to Kimchi Reader.")
        return

    deck_names = current_deck_names()
    if not deck_names:
        showWarning("No decks were found in the current Anki collection.")
        return

    dialog = DeckSelectionDialog(deck_names, parent=mw)
    if dialog.exec() != int(QDialog.DialogCode.Accepted):
        return

    selected_decks = dialog.selected_deck_names()
    if not selected_decks:
        showWarning("Choose at least one deck to sync.")
        return

    backend = build_backend()
    if not ensure_authenticated(backend):
        return

    mw.progress.start(
        immediate=True,
        label="Preparing Kimchi Reader sync preview...",
    )

    def preview_task() -> SyncPreview:
        return backend.prepare_sync(selected_decks)

    def apply_preview(preview: SyncPreview) -> None:
        mw.progress.start(
            immediate=True,
            label="Applying Kimchi Reader status changes...",
        )

        def apply_task() -> SyncRunResult:
            return backend.run_prepared_sync(preview)

        def on_apply_done(future) -> None:
            mw.progress.finish()
            try:
                result = future.result()
            except Exception as exc:  # pragma: no cover - defensive UI handling
                showText(
                    "Kimchi Reader sync failed.\n\n%s" % exc,
                    title="Kimchi Reader Sync Failed",
                )
                return
            present_sync_result(result)

        mw.taskman.run_in_background(apply_task, on_apply_done)

    def on_preview_done(future) -> None:
        mw.progress.finish()
        try:
            preview = future.result()
        except Exception as exc:  # pragma: no cover - defensive UI handling
            showText(
                "Kimchi Reader preview failed.\n\n%s" % exc,
                title="Kimchi Reader Sync Failed",
            )
            return
        if not present_sync_preview(preview):
            return
        apply_preview(preview)

    mw.taskman.run_in_background(preview_task, on_preview_done)


def install_menu_action() -> None:
    action = QAction(MENU_LABEL, mw)
    qconnect(action.triggered, lambda _checked=False: on_sync_menu_triggered())
    mw.form.menuTools.addAction(action)


gui_hooks.main_window_did_init.append(install_menu_action)
