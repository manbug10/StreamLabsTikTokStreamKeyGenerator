from __future__ import annotations

import glob
import json
import os
import platform
import re
import sys
import threading
import traceback

from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                              QGroupBox, QPushButton, QLineEdit, QLabel, QCheckBox,
                              QListWidget, QMessageBox, QListWidgetItem, QSizePolicy)
from PySide6.QtCore import Signal, QTimer
from PySide6.QtGui import QDesktopServices
from Stream import Stream
from TokenRetriever import TokenRetriever
from Updater import VersionChecker

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

TOKEN_DEBOUNCE_MS = 800       # auto-load account info after the token stops changing
GAME_SEARCH_DEBOUNCE_MS = 300  # wait for the user to stop typing before searching


class StreamApp(QMainWindow):
    update_suggestions = Signal(list)
    _token_ready = Signal(str)
    _token_error = Signal(str)
    _restore_local_btn = Signal()
    _restore_online_btn = Signal()
    _account_ready = Signal(dict)
    _account_error = Signal(str)
    _search_error = Signal(str)
    _game_mask_ready = Signal(str)
    _update_ready = Signal(dict)

    def __init__(self):
        super().__init__()
        self.stream = None
        self.game_mask_id = ""
        self.suppress_donation_reminder = False
        self._account_gen = 0  # discard stale account-info responses
        self._search_gen = 0   # discard stale game-search responses
        self._mask_gen = 0     # discard stale game-mask responses

        self.init_ui()

        # Cross-thread signals — every GUI update from a worker must go
        # through one of these (Qt GUI objects are not thread-safe).
        self.update_suggestions.connect(self.update_suggestions_list)
        self._token_ready.connect(self._apply_token)
        self._token_error.connect(lambda msg: QMessageBox.critical(self, "Error", msg))
        self._restore_local_btn.connect(self._do_restore_local_btn)
        self._restore_online_btn.connect(self._do_restore_online_btn)
        self._account_ready.connect(self._apply_account_info)
        self._account_error.connect(self._on_account_error)
        self._search_error.connect(lambda msg: QMessageBox.warning(self, "Search Error", msg))
        self._game_mask_ready.connect(self._set_game_mask_id)
        self._update_ready.connect(self._show_update_dialog)

        # Debounced auto-load when the token is pasted/typed
        self._token_timer = QTimer(self)
        self._token_timer.setSingleShot(True)
        self._token_timer.setInterval(TOKEN_DEBOUNCE_MS)
        self._token_timer.timeout.connect(self.refresh_account_info)

        # Debounced game search while typing
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(GAME_SEARCH_DEBOUNCE_MS)
        self._search_timer.timeout.connect(self._start_game_search)

        self.load_config()

        QTimer.singleShot(3000, lambda: [
            self.show_donation_reminder(),
            QTimer.singleShot(3000, self.check_updates_on_startup)
        ])

    def init_ui(self):
        self.setWindowTitle("StreamLabs TikTok Stream Key Generator")
        self.setMinimumSize(800, 600)

        main_widget = QWidget()
        main_layout = QHBoxLayout()
        main_widget.setLayout(main_layout)
        self.setCentralWidget(main_widget)

        # Left column
        left_column = QVBoxLayout()
        main_layout.addLayout(left_column)

        # Token Section
        token_group = QGroupBox("Token Loader")
        token_group.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        left_column.addWidget(token_group)

        token_layout = QVBoxLayout()
        token_layout.setContentsMargins(8, 12, 8, 8)
        token_layout.setSpacing(6)
        token_group.setLayout(token_layout)

        # Token Entry Row
        token_entry_row = QHBoxLayout()
        token_entry_row.setSpacing(5)

        self.token_entry = QLineEdit()
        self.token_entry.setPlaceholderText("Paste token here or load below...")
        self.token_entry.setEchoMode(QLineEdit.Password)
        self.token_entry.setFixedHeight(28)
        self.token_entry.textChanged.connect(self.handle_token_change)
        self.token_entry.returnPressed.connect(self.refresh_account_info)
        token_entry_row.addWidget(self.token_entry)

        # Eye icon for password visibility
        self.toggle_token_btn = QPushButton()
        self.toggle_token_btn.setFixedSize(28, 28)
        self.toggle_token_btn.setText("👁️")
        self.toggle_token_btn.setToolTip("Show token")
        self.toggle_token_btn.clicked.connect(self.toggle_token_visibility)
        token_entry_row.addWidget(self.toggle_token_btn)

        token_layout.addLayout(token_entry_row)

        # Token Load Buttons Row
        load_buttons_row = QHBoxLayout()
        load_buttons_row.setSpacing(5)

        # Local Token Button
        self.load_local_btn = QPushButton("Load from PC")
        self.load_local_btn.setFixedHeight(30)
        self.load_local_btn.setToolTip("Load token from Streamlabs desktop app data")
        self.load_local_btn.clicked.connect(self.load_local_token)
        load_buttons_row.addWidget(self.load_local_btn)

        # Online Token Button
        self.load_online_btn = QPushButton("Load from Web")
        self.load_online_btn.setFixedHeight(30)
        self.load_online_btn.setToolTip("Get token through browser login")
        self.load_online_btn.clicked.connect(self.fetch_online_token)
        load_buttons_row.addWidget(self.load_online_btn)

        token_layout.addLayout(load_buttons_row)

        # Binary Location Input Row
        if platform.system() == "Linux":
            binary_row = QHBoxLayout()
            binary_row.setSpacing(5)

            self.binary_location_entry = QLineEdit()
            self.binary_location_entry.setPlaceholderText("Custom Chrome binary path (optional)")
            self.binary_location_entry.setFixedHeight(28)
            self.binary_location_entry.setToolTip("Leave empty to auto-detect Chrome path on Linux")
            binary_row.addWidget(self.binary_location_entry)

            token_layout.addLayout(binary_row)


        # Account Info Section
        account_info_label = QLabel("Account Information")
        account_info_label.setStyleSheet("font-weight: bold; margin-top: 8px;")
        token_layout.addWidget(account_info_label)

        # Username
        username_row = QHBoxLayout()
        username_row.setSpacing(5)
        username_label = QLabel("Username:")
        username_label.setFixedWidth(100)
        self.tiktok_username = QLineEdit()
        self.tiktok_username.setReadOnly(True)
        self.tiktok_username.setFixedHeight(26)
        username_row.addWidget(username_label)
        username_row.addWidget(self.tiktok_username)
        token_layout.addLayout(username_row)

        # Status
        status_row = QHBoxLayout()
        status_row.setSpacing(5)
        status_label = QLabel("Status:")
        status_label.setFixedWidth(100)
        self.app_status = QLineEdit()
        self.app_status.setReadOnly(True)
        self.app_status.setFixedHeight(26)
        status_row.addWidget(status_label)
        status_row.addWidget(self.app_status)
        token_layout.addLayout(status_row)

        # Go Live Status
        live_row = QHBoxLayout()
        live_row.setSpacing(5)
        live_label = QLabel("Can Go Live:")
        live_label.setFixedWidth(100)
        self.can_go_live = QLineEdit()
        self.can_go_live.setReadOnly(True)
        self.can_go_live.setFixedHeight(26)
        live_row.addWidget(live_label)
        live_row.addWidget(self.can_go_live)
        token_layout.addLayout(live_row)

        # Refresh account info button
        refresh_btn = QPushButton("Refresh Account Info")
        refresh_btn.setFixedHeight(30)
        refresh_btn.setToolTip("Refresh account information")
        refresh_btn.clicked.connect(self.refresh_account_info)
        token_layout.addWidget(refresh_btn)

        # Add stretch to prevent expansion
        token_layout.addStretch()

        # Stream Details
        stream_group = QGroupBox("Stream Details")
        left_column.addWidget(stream_group)
        stream_layout = QVBoxLayout()
        stream_layout.setContentsMargins(8, 8, 8, 8)  # Left, Top, Right, Bottom margins
        stream_layout.setSpacing(5)  # Reduced spacing between widgets
        stream_group.setLayout(stream_layout)

        # Stream Title
        title_label = QLabel("Stream Title:")
        title_label.setStyleSheet("font-weight: bold;")
        stream_layout.addWidget(title_label)
        self.stream_title = QLineEdit()
        self.stream_title.setFixedHeight(28)  # Slightly smaller height
        stream_layout.addWidget(self.stream_title)

        # Game Category
        game_label = QLabel("Game Category:")
        game_label.setStyleSheet("font-weight: bold;")
        stream_layout.addWidget(game_label)
        self.game_category = QLineEdit()
        self.game_category.setFixedHeight(28)
        self.game_category.textChanged.connect(self.handle_game_search)
        stream_layout.addWidget(self.game_category)

        # Suggestions List
        self.suggestions_list = QListWidget()
        self.suggestions_list.hide()
        self.suggestions_list.setFixedHeight(100)  # Limit height of suggestions
        self.suggestions_list.itemClicked.connect(self.handle_suggestion_selected)
        stream_layout.addWidget(self.suggestions_list)

        # Mature Checkbox
        self.mature_checkbox = QCheckBox("Enable mature content")
        self.mature_checkbox.setStyleSheet("padding: 2px;")  # Reduce internal padding
        stream_layout.addWidget(self.mature_checkbox)

        # Add stretch to push everything up
        stream_layout.addStretch()

        # Right column (Controls)
        control_group = QGroupBox("Stream Control")
        control_group.setMinimumWidth(250)  # Set a minimum width for the control column
        main_layout.addWidget(control_group)

        control_layout = QVBoxLayout()
        control_layout.setContentsMargins(8, 12, 8, 8)  # Tighter margins
        control_layout.setSpacing(6)  # Reduced spacing between widgets
        control_group.setLayout(control_layout)

        # Buttons row
        button_row = QHBoxLayout()
        button_row.setSpacing(5)  # Tight spacing between buttons

        self.go_live_btn = QPushButton("Go Live")
        self.go_live_btn.setEnabled(False)
        self.go_live_btn.clicked.connect(self.start_stream)
        self.go_live_btn.setFixedHeight(32)  # Consistent button height
        button_row.addWidget(self.go_live_btn)

        self.end_live_btn = QPushButton("End Live")
        self.end_live_btn.setEnabled(False)
        self.end_live_btn.clicked.connect(self.end_stream)
        self.end_live_btn.setFixedHeight(32)
        button_row.addWidget(self.end_live_btn)

        control_layout.addLayout(button_row)

        # URL Section
        url_label = QLabel("Stream URL:")
        url_label.setStyleSheet("font-weight: bold; margin-top: 5px;")
        control_layout.addWidget(url_label)

        self.stream_url = QLineEdit()
        self.stream_url.setReadOnly(True)
        self.stream_url.setFixedHeight(28)  # Compact height
        control_layout.addWidget(self.stream_url)

        self.copy_url_btn = QPushButton("Copy URL")
        self.copy_url_btn.setFixedHeight(28)
        self.copy_url_btn.clicked.connect(lambda: self.copy_to_clipboard(self.stream_url))
        control_layout.addWidget(self.copy_url_btn)

        # Key Section
        key_label = QLabel("Stream Key:")
        key_label.setStyleSheet("font-weight: bold; margin-top: 5px;")
        control_layout.addWidget(key_label)

        self.stream_key = QLineEdit()
        self.stream_key.setReadOnly(True)
        self.stream_key.setFixedHeight(28)
        control_layout.addWidget(self.stream_key)

        self.copy_key_btn = QPushButton("Copy Key")
        self.copy_key_btn.setFixedHeight(28)
        self.copy_key_btn.clicked.connect(lambda: self.copy_to_clipboard(self.stream_key))
        control_layout.addWidget(self.copy_key_btn)

        # Add stretch to prevent expansion of widgets
        control_layout.addStretch()
        # Bottom buttons
        bottom_buttons = QHBoxLayout()
        left_column.addLayout(bottom_buttons)

        self.save_btn = QPushButton("Save Config")
        self.save_btn.clicked.connect(lambda: self.save_config())
        bottom_buttons.addWidget(self.save_btn)

        self.help_btn = QPushButton("Help")
        self.help_btn.clicked.connect(self.show_help)
        bottom_buttons.addWidget(self.help_btn)

        self.donate_btn = QPushButton("☕ Donate")
        self.donate_btn.setToolTip("Support the developer")
        self.donate_btn.clicked.connect(lambda: QDesktopServices.openUrl("https://buymeacoffee.com/loukious"))
        bottom_buttons.addWidget(self.donate_btn)

        self.monitor_btn = QPushButton("Open Live Monitor")
        self.monitor_btn.clicked.connect(self.open_live_monitor)
        bottom_buttons.addWidget(self.monitor_btn)

    def toggle_token_visibility(self):
        """Toggle token visibility and update emoji"""
        if self.token_entry.echoMode() == QLineEdit.Normal:
            self.token_entry.setEchoMode(QLineEdit.Password)
            self.toggle_token_btn.setText("👁️")  # Closed eye
            self.toggle_token_btn.setToolTip("Show token")
        else:
            self.token_entry.setEchoMode(QLineEdit.Normal)
            self.toggle_token_btn.setText("👁️‍🗨️")  # Eye with speech bubble (visible)
            self.toggle_token_btn.setToolTip("Hide token")

    def handle_token_change(self):
        # Go Live stays disabled until the account info confirms can_be_live;
        # here we only (re)start the debounced auto-load of account info.
        if self.token_entry.text():
            self._token_timer.start()
        else:
            self._token_timer.stop()
            self.go_live_btn.setEnabled(False)

    def load_config(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError):
            # Missing or corrupted config: start with defaults instead of crashing.
            traceback.print_exc()
            data = {}
        self.token_entry.setText(data.get("token", ""))
        self.stream_title.setText(data.get("title", ""))
        self.game_category.setText(data.get("game", ""))
        self.mature_checkbox.setChecked(data.get("audience_type", "0") == "1")
        self.suppress_donation_reminder = bool(data.get("suppress_donation_reminder", False))

    def save_config(self, show_message=True):
        data = {
            "title": self.stream_title.text(),
            "game": self.game_category.text(),
            "audience_type": "1" if self.mature_checkbox.isChecked() else "0",
            "token": self.token_entry.text(),
            "suppress_donation_reminder": self.suppress_donation_reminder
        }
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as file:
                json.dump(data, file)
            try:
                os.chmod(CONFIG_PATH, 0o600)  # token is sensitive: restrict to current user
            except OSError:
                pass  # not critical (e.g. unsupported on some platforms)
        except OSError as e:
            traceback.print_exc()
            if show_message:
                QMessageBox.warning(self, "Error", f"Could not save configuration: {e}")
            return
        if show_message:
            QMessageBox.information(self, "Config Saved", "Configuration saved successfully!")

    # ------------------------------------------------------------------ #
    #  Account info (network access runs on worker threads)                #
    # ------------------------------------------------------------------ #

    def refresh_account_info(self):
        self._token_timer.stop()
        token = self.token_entry.text()
        if token:
            self.stream = Stream(token)
            self._account_gen += 1
            threading.Thread(
                target=self._load_account_worker,
                args=(self.stream, self._account_gen),
                daemon=True
            ).start()
        else:
            self.stream = None
        self.save_config(False)

    def _load_account_worker(self, stream, gen):
        try:
            info = stream.getInfo()
        except Exception as e:
            print(traceback.format_exc())
            if gen == self._account_gen:
                self._account_error.emit(f"Failed to load account info: {e}")
            return
        if gen == self._account_gen:
            self._account_ready.emit(info)

    def _apply_account_info(self, info):
        """Apply account info — always called on the GUI thread."""
        if not isinstance(info, dict):
            info = {}
        user = info.get("user") or {}
        self.tiktok_username.setText(user.get("username", "Unknown"))

        app_status = info.get("application_status") or {}
        self.app_status.setText(str(app_status.get("status", "Unknown")))

        can_go_live = bool(info.get("can_be_live", False))
        self.can_go_live.setText(str(can_go_live))

        for widget in (self.stream_title, self.game_category, self.mature_checkbox):
            widget.setEnabled(can_go_live)
        self.go_live_btn.setEnabled(can_go_live and bool(self.token_entry.text()))

        self.fetch_game_mask_id(self.game_category.text())

    def _on_account_error(self, msg):
        self.can_go_live.setText("Unknown")
        self.go_live_btn.setEnabled(False)
        QMessageBox.critical(self, "Error", msg)

    # ------------------------------------------------------------------ #
    #  Token loading                                                       #
    # ------------------------------------------------------------------ #

    def _do_restore_local_btn(self):
        self.load_local_btn.setEnabled(True)
        self.load_local_btn.setText("Load from PC")

    def _do_restore_online_btn(self):
        self.load_online_btn.setEnabled(True)
        self.load_online_btn.setText("Load from Web")

    def load_local_token(self):
        # Validate the OS on the GUI thread so exactly one clear error dialog
        # is shown (the old code used QTimer from the worker thread here).
        if platform.system() not in ("Windows", "Darwin"):
            QMessageBox.critical(self, "Error",
                                 "Unsupported operating system for local token retrieval.")
            return

        self.load_local_btn.setEnabled(False)
        self.load_local_btn.setText("Searching…")

        threading.Thread(target=self._local_token_worker, daemon=True).start()

    def _local_token_worker(self):
        try:
            token = self._find_local_token()
        except Exception as e:
            print(traceback.format_exc())
            self._token_error.emit(f"Unexpected error: {e}")
            return
        finally:
            self._restore_local_btn.emit()

        if token:
            self._token_ready.emit(token)
        else:
            self._token_error.emit(
                "No API Token found locally. Make sure Streamlabs is installed "
                "and you're logged in using TikTok."
            )

    def _find_local_token(self) -> str | None:
        """Blocking file scan — runs on a worker thread (Windows/macOS only)."""
        if platform.system() == 'Windows':
            path_pattern = os.path.expandvars(
                r'%appdata%\slobs-client\Local Storage\leveldb\*.log'
            )
        else:  # Darwin — Linux is rejected earlier on the GUI thread
            path_pattern = os.path.expanduser(
                '~/Library/Application Support/slobs-client/Local Storage/leveldb/*.log'
            )

        files = sorted(glob.glob(path_pattern), key=os.path.getmtime, reverse=True)
        token_pattern = re.compile(r'"apiToken":"([a-f0-9]+)"', re.IGNORECASE)

        for file in files:
            try:
                with open(file, 'rb') as f:
                    content = f.read().decode('utf-8', errors='ignore')
                content = re.sub(r'[\x00]', '', content)
                matches = token_pattern.findall(content)
                if matches:
                    return matches[-1]
            except Exception as e:
                print(f"Error reading {file}: {e}")

        return None

    def fetch_online_token(self):
        self.load_online_btn.setEnabled(False)
        self.load_online_btn.setText("Waiting for login…")

        retriever = TokenRetriever()

        def _run():
            try:
                token = retriever.retrieve_token()
            except Exception as e:
                print(traceback.format_exc())
                self._token_error.emit(f"Unexpected error: {e}")
                return
            finally:
                self._restore_online_btn.emit()

            if token:
                self._token_ready.emit(token)
            else:
                self._token_error.emit("Failed to obtain token online!")

        threading.Thread(target=_run, daemon=True).start()

    def _apply_token(self, token: str):
        """Apply a freshly retrieved token — always called on the GUI thread."""
        self.token_entry.setText(token)  # fires textChanged → starts the debounce timer
        self.refresh_account_info()      # load immediately; also cancels the pending timer

    # ------------------------------------------------------------------ #
    #  Game search                                                         #
    # ------------------------------------------------------------------ #

    def _set_game_mask_id(self, mask_id: str):
        self.game_mask_id = mask_id

    def fetch_game_mask_id(self, game_name):
        if not self.stream:
            return
        self._mask_gen += 1
        gen = self._mask_gen
        stream = self.stream
        threading.Thread(
            target=self._game_mask_worker,
            args=(stream, game_name, gen),
            daemon=True
        ).start()

    def _game_mask_worker(self, stream, game_name, gen):
        if not game_name:
            if gen == self._mask_gen:
                self._game_mask_ready.emit("")
            return
        try:
            categories = stream.search(game_name)
        except Exception as e:
            print(traceback.format_exc())
            if gen == self._mask_gen:
                self._search_error.emit(f"Failed to search games: {e}")
            return

        mask_id = ""
        for category in categories:
            if str(category.get("full_name", "")).strip().lower() == game_name.strip().lower():
                mask_id = category.get("game_mask_id", "")
                break
        if gen == self._mask_gen:
            self._game_mask_ready.emit(mask_id)

    def handle_game_search(self, text):
        if text and self.stream:
            self._search_timer.start()  # debounce: search once typing pauses
        else:
            self._search_timer.stop()
            self.suggestions_list.hide()

    def _start_game_search(self):
        text = self.game_category.text()
        if not text or not self.stream:
            return
        self._search_gen += 1
        gen = self._search_gen
        stream = self.stream
        threading.Thread(
            target=self._search_worker,
            args=(stream, text, gen),
            daemon=True
        ).start()

    def _search_worker(self, stream, text, gen):
        try:
            categories = stream.search(text)
        except Exception as e:
            print(traceback.format_exc())
            if gen == self._search_gen:
                self._search_error.emit(f"Game search failed: {e}")
            return
        if gen == self._search_gen:
            self.update_suggestions.emit(categories)

    def update_suggestions_list(self, categories):
        self.suggestions_list.clear()
        for category in categories:
            self.suggestions_list.addItem(QListWidgetItem(category['full_name']))
        self.suggestions_list.setVisible(bool(categories))

    def handle_suggestion_selected(self, item):
        self.game_category.setText(item.text())
        self.fetch_game_mask_id(item.text())
        self.suggestions_list.hide()

    # ------------------------------------------------------------------ #
    #  Stream control                                                      #
    # ------------------------------------------------------------------ #

    def start_stream(self):
        if not self.stream:
            QMessageBox.warning(
                self, "No Token",
                "Load a valid token first (press Enter in the token field "
                "or use the token loaders)."
            )
            return
        typed_game = self.game_category.text().strip()
        if not self.game_mask_id and typed_game and typed_game.lower() != "other":
            QMessageBox.warning(
                self, "Category Not Found",
                "The typed category could not be matched. Select a suggestion "
                "from the list or type the exact category name."
            )
            return
        try:
            audience_type = "1" if self.mature_checkbox.isChecked() else "0"
            stream_url, stream_key = self.stream.start(
                self.stream_title.text(),
                self.game_mask_id,
                audience_type
            )

            if stream_url and stream_key:
                self.stream_url.setText(stream_url)
                self.stream_key.setText(stream_key)
                self.end_live_btn.setEnabled(True)
                self.go_live_btn.setEnabled(False)
                QMessageBox.information(self, "Live Started", "Stream started successfully!")
            else:
                QMessageBox.critical(self, "Error", "Failed to start stream!")

        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to start stream: {str(e)}")

    def end_stream(self):
        if not self.stream:
            QMessageBox.warning(self, "No Stream", "There is no active stream session.")
            return
        try:
            if self.stream.end():
                self.stream_url.clear()
                self.stream_key.clear()
                self.end_live_btn.setEnabled(False)
                self.go_live_btn.setEnabled(True)
                QMessageBox.information(self, "Live Ended", "Stream ended successfully!")
            else:
                QMessageBox.critical(
                    self, "Error",
                    "Failed to end stream (no stream was started or the API refused)."
                )
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to end stream: {str(e)}")

    # ------------------------------------------------------------------ #
    #  Misc                                                                #
    # ------------------------------------------------------------------ #

    def copy_to_clipboard(self, widget):
        QApplication.clipboard().setText(widget.text())
        QMessageBox.information(self, "Copied", "Text copied to clipboard!")

    def show_help(self):
        help_text = """1. Apply for LIVE access on Streamlabs
2. Install Streamlabs and login to TikTok
3. Use this app to get Streamlabs token
4. Go live!"""

        msg = QMessageBox(self)
        msg.setWindowTitle("Help")
        msg.setText(help_text)
        msg.addButton(QMessageBox.Ok)
        msg.exec()


    def check_updates_on_startup(self):
        """Non-blocking update check — the network request runs on a worker."""
        threading.Thread(target=self._update_worker, daemon=True).start()

    def _update_worker(self):
        update_info = VersionChecker.check_update()
        if update_info:
            self._update_ready.emit(update_info)

    def _show_update_dialog(self, update_info):
        msg = QMessageBox(self)
        msg.setWindowTitle("Update Available")
        msg.setText(
            f"Version {update_info['latest']} is available!\n\n"
            f"Current version: {update_info['current']}\n\n"
            "Would you like to download it now?"
        )

        # Use standard buttons for better compatibility
        msg.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg.setDefaultButton(QMessageBox.Yes)

        if msg.exec() == QMessageBox.Yes:
            QDesktopServices.openUrl(update_info["url"])

    def show_donation_reminder(self):

        if self.suppress_donation_reminder:
            return

        msg = QMessageBox()
        msg.setIcon(QMessageBox.Information)
        msg.setWindowTitle("Support Development")
        msg.setText("Enjoying this app? Consider supporting its development!")

        dont_show_again = QCheckBox("Never show this message again")
        msg.setCheckBox(dont_show_again)

        # Add buttons
        donate_btn = msg.addButton("Donate Now", QMessageBox.AcceptRole)
        msg.addButton(QMessageBox.Ok)

        # Make the "Donate Now" button more prominent
        donate_btn.setStyleSheet("font-weight: bold;")

        # Execute the message box
        msg.exec()

        if dont_show_again.isChecked():
            self.suppress_donation_reminder = True
            self.save_config(False)
        # Handle button clicks
        if msg.clickedButton() == donate_btn:
            QDesktopServices.openUrl("https://buymeacoffee.com/loukious")

    def open_live_monitor(self):
        QDesktopServices.openUrl("https://livecenter.tiktok.com/live_monitor?lang=en-US")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = StreamApp()
    window.show()
    sys.exit(app.exec())
