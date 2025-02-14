# COPYRIGHT (C) 2020-2023 Nicotine+ Contributors
#
# GNU GENERAL PUBLIC LICENSE
#    Version 3, 29 June 2007
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import os
import os.path
import re
import shutil
import time

from collections import defaultdict
from locale import strxfrm

from pynicotine import slskmessages
from pynicotine.config import config
from pynicotine.core import core
from pynicotine.events import events
from pynicotine.logfacility import log
from pynicotine.slskmessages import TransferRejectReason
from pynicotine.transfers import Transfer
from pynicotine.transfers import Transfers
from pynicotine.transfers import TransferStatus
from pynicotine.utils import execute_command
from pynicotine.utils import clean_file
from pynicotine.utils import clean_path
from pynicotine.utils import encode_path
from pynicotine.utils import truncate_string_byte


class Downloads(Transfers):

    def __init__(self):

        super().__init__(transfers_file_path=os.path.join(config.data_folder_path, "downloads.json"))

        self.requested_folders = defaultdict(dict)
        self.requested_folder_token = 0

        self._folder_basename_byte_limits = {}
        self._pending_queue_messages = {}

        self._download_queue_timer_id = None
        self._retry_connection_downloads_timer_id = None
        self._retry_io_downloads_timer_id = None

        for event_name, callback in (
            ("download-file-error", self._download_file_error),
            ("file-connection-closed", self._file_connection_closed),
            ("file-transfer-init", self._file_transfer_init),
            ("file-download-progress", self._file_download_progress),
            ("folder-contents-response", self._folder_contents_response),
            ("peer-connection-closed", self._peer_connection_closed),
            ("peer-connection-error", self._peer_connection_error),
            ("place-in-queue-response", self._place_in_queue_response),
            ("set-connection-stats", self._set_connection_stats),
            ("shares-ready", self._shares_ready),
            ("transfer-request", self._transfer_request),
            ("upload-denied", self._upload_denied),
            ("upload-failed", self._upload_failed),
            ("user-status", self._user_status)
        ):
            events.connect(event_name, callback)

    def _start(self):
        super()._start()
        self.update_download_filters()

    def _quit(self):

        super()._quit()

        self._folder_basename_byte_limits.clear()
        self.requested_folder_token = 0

    def _server_login(self, msg):

        if not msg.success:
            return

        super()._server_login(msg)

        self.requested_folders.clear()

        # Request queue position of queued downloads and retry failed downloads every 3 minutes
        self._download_queue_timer_id = events.schedule(delay=180, callback=self._check_download_queue, repeat=True)

        # Retry downloads failed due to connection issues every 3 minutes
        self._retry_connection_downloads_timer_id = events.schedule(
            delay=180, callback=self._retry_failed_connection_downloads, repeat=True)

        # Retry downloads failed due to file I/O errors every 15 minutes
        self._retry_io_downloads_timer_id = events.schedule(
            delay=900, callback=self._retry_failed_io_downloads, repeat=True)

    def _server_disconnect(self, msg):

        super()._server_disconnect(msg)

        for timer_id in (
            self._download_queue_timer_id,
            self._retry_connection_downloads_timer_id,
            self._retry_io_downloads_timer_id
        ):
            events.cancel_scheduled(timer_id)

        events.emit("update-downloads")
        self.requested_folders.clear()

    # Load Transfers #

    def _get_transfer_list_file_path(self):

        downloads_file_1_4_2 = os.path.join(config.data_folder_path, "config.transfers.pickle")
        downloads_file_1_4_1 = os.path.join(config.data_folder_path, "transfers.pickle")

        if os.path.exists(encode_path(self.transfers_file_path)):
            # New file format
            return self.transfers_file_path

        if os.path.exists(encode_path(downloads_file_1_4_2)):
            # Nicotine+ 1.4.2+
            return downloads_file_1_4_2

        if os.path.exists(encode_path(downloads_file_1_4_1)):
            # Nicotine <=1.4.1
            return downloads_file_1_4_1

        # Fall back to new file format
        return self.transfers_file_path

    def _load_transfers(self):

        load_func = self._load_transfers_file
        transfers_file_path = self._get_transfer_list_file_path()

        if transfers_file_path != self.transfers_file_path:
            load_func = self._load_legacy_transfers_file

        for transfer in self._get_stored_transfers(transfers_file_path, load_func):
            self._append_transfer(transfer)

            if transfer.status == TransferStatus.USER_LOGGED_OFF:
                # Mark transfer as failed in order to resume it when connected
                self._fail_transfer(transfer)

    # Filters/Limits #

    def update_download_filters(self):

        failed = {}
        outfilter = "(\\\\("
        download_filters = sorted(config.sections["transfers"]["downloadfilters"])
        # Get Filters from config file and check their escaped status
        # Test if they are valid regular expressions and save error messages

        for item in download_filters:
            dfilter, escaped = item
            if escaped:
                dfilter = re.escape(dfilter)
                dfilter = dfilter.replace("\\*", ".*")

            try:
                re.compile(f"({dfilter})")
                outfilter += dfilter

                if item is not download_filters[-1]:
                    outfilter += "|"

            except re.error as error:
                failed[dfilter] = error

        outfilter += ")$)"

        try:
            re.compile(outfilter)

        except re.error as error:
            # Strange that individual filters _and_ the composite filter both fail
            log.add(_("Error: Download Filter failed! Verify your filters. Reason: %s"), error)
            config.sections["transfers"]["downloadregexp"] = ""
            return

        config.sections["transfers"]["downloadregexp"] = outfilter

        # Send error messages for each failed filter to log window
        if not failed:
            return

        errors = ""

        for dfilter, error in failed.items():
            errors += f"Filter: {dfilter} Error: {error} "

        log.add(_("Error: %(num)d Download filters failed! %(error)s "), {"num": len(failed), "error": errors})

    def update_transfer_limits(self):

        events.emit("update-download-limits")

        if core.user_status == slskmessages.UserStatus.OFFLINE:
            return

        use_speed_limit = config.sections["transfers"]["use_download_speed_limit"]

        if use_speed_limit == "primary":
            speed_limit = config.sections["transfers"]["downloadlimit"]

        elif use_speed_limit == "alternative":
            speed_limit = config.sections["transfers"]["downloadlimitalt"]

        else:
            speed_limit = 0

        core.send_message_to_network_thread(slskmessages.SetDownloadLimit(speed_limit))

    # Transfer Actions #

    def _append_transfer(self, transfer):
        self.transfers[transfer.username + transfer.virtual_path] = transfer

    def _update_transfer(self, transfer, update_parent=True):
        events.emit("update-download", transfer, update_parent)

    def _enqueue_transfer(self, transfer, bypass_filter=False):

        username = transfer.username
        virtual_path = transfer.virtual_path
        size = transfer.size

        if not bypass_filter and config.sections["transfers"]["enablefilters"]:
            try:
                downloadregexp = re.compile(config.sections["transfers"]["downloadregexp"], flags=re.IGNORECASE)

                if downloadregexp.search(virtual_path) is not None:
                    log.add_transfer("Filtering: %s", virtual_path)

                    if self._auto_clear_transfer(transfer):
                        return

                    self._abort_transfer(transfer, status=TransferStatus.FILTERED)
                    return

            except re.error:
                pass

        if slskmessages.UserStatus.OFFLINE in (core.user_status, core.user_statuses.get(username)):
            # Either we are offline or the user we want to download from is
            self._abort_transfer(transfer, status=TransferStatus.USER_LOGGED_OFF)
            return

        download_path = self.get_complete_download_file_path(username, virtual_path, size, transfer.folder_path)

        if download_path:
            transfer.status = TransferStatus.FINISHED
            transfer.size = transfer.current_byte_offset = size

            log.add_transfer("File %s is already downloaded", download_path)
            return

        log.add_transfer("Adding file %(filename)s from user %(user)s to download queue", {
            "filename": virtual_path,
            "user": username
        })
        super()._enqueue_transfer(transfer)

        msg = slskmessages.QueueUpload(file=virtual_path, legacy_client=transfer.legacy_attempt)

        if not core.shares.initialized:
            # Remain queued locally until our shares have initialized, to prevent invalid
            # messages about not sharing any files
            self._pending_queue_messages[transfer] = msg
        else:
            core.send_message_to_peer(username, msg)

    def _enqueue_limited_transfers(self, username):

        num_limited_transfers = 0
        queue_size_limit = self._user_queue_limits.get(username)

        if queue_size_limit is None:
            return

        for download in self.failed_users.get(username, {}).copy().values():
            if download.status != TransferRejectReason.QUEUED:
                continue

            if num_limited_transfers >= queue_size_limit:
                # Only enqueue a small number of downloads at a time
                return

            self._unfail_transfer(download)
            self._enqueue_transfer(download)
            self._update_transfer(download)

            num_limited_transfers += 1

        # No more limited downloads
        del self._user_queue_limits[username]

    def _dequeue_transfer(self, transfer):

        super()._dequeue_transfer(transfer)

        if transfer in self._pending_queue_messages:
            del self._pending_queue_messages[transfer]

    def _file_downloaded_actions(self, username, file_path):

        if config.sections["notifications"]["notification_popup_file"]:
            core.notifications.show_download_notification(
                _("%(file)s downloaded from %(user)s") % {
                    "user": username,
                    "file": os.path.basename(file_path)
                },
                title=_("File Downloaded")
            )

        if config.sections["transfers"]["afterfinish"]:
            try:
                execute_command(config.sections["transfers"]["afterfinish"], file_path)
                log.add(_("Executed: %s"), config.sections["transfers"]["afterfinish"])

            except Exception:
                log.add(_("Trouble executing '%s'"), config.sections["transfers"]["afterfinish"])

    def _folder_downloaded_actions(self, username, folder_path):

        if not folder_path:
            return

        for downloads in (
            self.queued_users.get(username, {}),
            self.active_users.get(username, {}),
            self.failed_users.get(username, {})
        ):
            for download in downloads.values():
                if download.folder_path == folder_path:
                    return

        if config.sections["notifications"]["notification_popup_folder"]:
            core.notifications.show_download_notification(
                _("%(folder)s downloaded from %(user)s") % {
                    "user": username,
                    "folder": folder_path
                },
                title=_("Folder Downloaded")
            )

        if config.sections["transfers"]["afterfolder"]:
            try:
                execute_command(config.sections["transfers"]["afterfolder"], folder_path)
                log.add(_("Executed on folder: %s"), config.sections["transfers"]["afterfolder"])

            except Exception:
                log.add(_("Trouble executing on folder: %s"), config.sections["transfers"]["afterfolder"])

    def _finish_transfer(self, transfer):

        download_folder_path = transfer.folder_path or self.get_default_download_folder(transfer.username)
        download_folder_path_encoded = encode_path(download_folder_path)

        download_basename = self.get_download_basename(transfer.virtual_path, download_folder_path, avoid_conflict=True)
        download_file_path = os.path.join(download_folder_path, download_basename)
        incomplete_file_path_encoded = transfer.file_handle.name

        self._deactivate_transfer(transfer)
        self._close_file(transfer)

        try:
            if not os.path.isdir(download_folder_path_encoded):
                os.makedirs(download_folder_path_encoded)

            shutil.move(incomplete_file_path_encoded, encode_path(download_file_path))

        except OSError as error:
            log.add(
                _("Couldn't move '%(tempfile)s' to '%(file)s': %(error)s"), {
                    "tempfile": incomplete_file_path_encoded.decode("utf-8", "replace"),
                    "file": download_file_path,
                    "error": error
                }
            )
            self._abort_transfer(transfer, status=TransferStatus.DOWNLOAD_FOLDER_ERROR)
            core.notifications.show_download_notification(
                str(error), title=_("Download Folder Error"), high_priority=True
            )
            return

        transfer.status = TransferStatus.FINISHED
        transfer.current_byte_offset = transfer.size
        transfer.sock = None

        core.statistics.append_stat_value("completed_downloads", 1)

        # Attempt to show notification and execute commands
        self._file_downloaded_actions(transfer.username, download_file_path)
        self._folder_downloaded_actions(transfer.username, transfer.folder_path)

        finished = True
        events.emit("download-notification", finished)

        # Attempt to autoclear this download, if configured
        if not self._auto_clear_transfer(transfer):
            self._update_transfer(transfer)

        core.pluginhandler.download_finished_notification(transfer.username, transfer.virtual_path, download_file_path)

        log.add_download(
            _("Download finished: user %(user)s, file %(file)s"), {
                "user": transfer.username,
                "file": transfer.virtual_path
            }
        )

    def _abort_transfer(self, transfer, denied_message=None, status=None, update_parent=True):

        transfer.legacy_attempt = False
        transfer.size_changed = False

        if transfer.sock is not None:
            core.send_message_to_network_thread(slskmessages.CloseConnection(transfer.sock))
            transfer.sock = None

        if transfer.file_handle is not None:
            self._close_file(transfer)

            log.add_download(
                _("Download aborted, user %(user)s file %(file)s"), {
                    "user": transfer.username,
                    "file": transfer.virtual_path
                }
            )

        self._deactivate_transfer(transfer)
        self._dequeue_transfer(transfer)
        self._unfail_transfer(transfer)

        if not status:
            return

        transfer.status = status

        if status not in {TransferStatus.FINISHED, TransferStatus.FILTERED, TransferStatus.PAUSED}:
            self._fail_transfer(transfer)

        events.emit("abort-download", transfer, status, update_parent)

    def _auto_clear_transfer(self, transfer):

        if config.sections["transfers"]["autoclear_downloads"]:
            self._clear_transfer(transfer)
            return True

        return False

    def _clear_transfer(self, transfer, update_parent=True):

        self._abort_transfer(transfer)
        self._remove_transfer(transfer)

        events.emit("clear-download", transfer, update_parent)

    def _check_download_queue(self):

        for download in self.queued_transfers:
            core.send_message_to_peer(
                download.username,
                slskmessages.PlaceInQueueRequest(file=download.virtual_path, legacy_client=download.legacy_attempt)
            )

    def _retry_failed_connection_downloads(self):

        statuses = {
            TransferStatus.CONNECTION_CLOSED, TransferStatus.CONNECTION_TIMEOUT, TransferRejectReason.PENDING_SHUTDOWN}

        for failed_downloads in self.failed_users.copy().values():
            for download in failed_downloads.copy().values():
                if download.status not in statuses:
                    continue

                self._unfail_transfer(download)
                self._enqueue_transfer(download)
                self._update_transfer(download)

    def _retry_failed_io_downloads(self):

        statuses = {
            TransferStatus.DOWNLOAD_FOLDER_ERROR, TransferStatus.LOCAL_FILE_ERROR, TransferRejectReason.FILE_READ_ERROR}

        for failed_downloads in self.failed_users.copy().values():
            for download in failed_downloads.copy().values():
                if download.status not in statuses:
                    continue

                self._unfail_transfer(download)
                self._enqueue_transfer(download)
                self._update_transfer(download)

    def can_upload(self, username):

        transfers = config.sections["transfers"]

        if not transfers["remotedownloads"]:
            return False

        if transfers["uploadallowed"] == 1:
            # Everyone
            return True

        if transfers["uploadallowed"] == 2 and username in core.userlist.buddies:
            # Buddies
            return True

        if transfers["uploadallowed"] == 3:
            # Trusted buddies
            user_data = core.userlist.buddies.get(username)

            if user_data and user_data.is_trusted:
                return True

        return False

    def get_folder_destination(self, username, folder_path, root_folder_path=None, download_folder_path=None):

        # Remove parent folders of the requested folder from path
        parent_folder_path = root_folder_path if root_folder_path else folder_path
        removed_parent_folders = parent_folder_path.rsplit("\\", 1)[0] if "\\" in parent_folder_path else ""
        target_folders = folder_path.replace(removed_parent_folders, "").lstrip("\\").replace("\\", os.sep)

        # Check if a custom download location was specified
        if not download_folder_path:
            if (username in self.requested_folders and folder_path in self.requested_folders[username]
                    and self.requested_folders[username][folder_path]):
                download_folder_path = self.requested_folders[username][folder_path]
            else:
                download_folder_path = self.get_default_download_folder(username)

        # Merge download path with target folder name
        return os.path.join(download_folder_path, target_folders)

    def get_default_download_folder(self, username=None):

        download_folder_path = os.path.normpath(config.sections["transfers"]["downloaddir"])

        # Check if username subfolders should be created for downloads
        if username and config.sections["transfers"]["usernamesubfolders"]:
            download_folder_path = os.path.join(download_folder_path, clean_file(username))

        return download_folder_path

    def get_basename_byte_limit(self, folder_path):

        max_bytes = self._folder_basename_byte_limits.get(folder_path)

        if max_bytes is None:
            try:
                max_bytes = os.statvfs(encode_path(folder_path)).f_namemax

            except (AttributeError, OSError):
                max_bytes = 255

            self._folder_basename_byte_limits[folder_path] = max_bytes

        return max_bytes

    def get_download_basename(self, virtual_path, download_folder_path, avoid_conflict=False):
        """Returns the download basename for a virtual file path."""

        max_bytes = self.get_basename_byte_limit(download_folder_path)

        basename = clean_file(virtual_path.replace("/", "\\").split("\\")[-1])
        basename_no_extension, extension = os.path.splitext(basename)
        basename_limit = max_bytes - len(extension.encode("utf-8"))
        basename_no_extension = truncate_string_byte(basename_no_extension, max(0, basename_limit))

        if basename_limit < 0:
            extension = truncate_string_byte(extension, max_bytes)

        corrected_basename = basename_no_extension + extension

        if not avoid_conflict:
            return corrected_basename

        counter = 1

        while os.path.exists(encode_path(os.path.join(download_folder_path, corrected_basename))):
            corrected_basename = f"{basename_no_extension} ({counter}){extension}"
            counter += 1

        return corrected_basename

    def get_complete_download_file_path(self, username, virtual_path, size, download_folder_path=None):
        """Returns the download path of a complete download, if available."""

        if not download_folder_path:
            download_folder_path = self.get_default_download_folder(username)

        basename = self.get_download_basename(virtual_path, download_folder_path)
        basename_no_extension, extension = os.path.splitext(basename)
        download_file_path = os.path.join(download_folder_path, basename)
        counter = 1

        while os.path.isfile(encode_path(download_file_path)):
            if os.stat(encode_path(download_file_path)).st_size == size:
                # Found a previous download with a matching file size
                return download_file_path

            basename = f"{basename_no_extension} ({counter}){extension}"
            download_file_path = os.path.join(download_folder_path, basename)
            counter += 1

        return None

    def get_incomplete_download_file_path(self, username, virtual_path):
        """Returns the path to store a download while it's still
        transferring."""

        from hashlib import md5
        md5sum = md5()
        md5sum.update((virtual_path + username).encode("utf-8"))
        prefix = f"INCOMPLETE{md5sum.hexdigest()}"

        # Ensure file name length doesn't exceed file system limit
        incomplete_folder_path = os.path.normpath(config.sections["transfers"]["incompletedir"])
        max_bytes = self.get_basename_byte_limit(incomplete_folder_path)

        basename = clean_file(virtual_path.replace("/", "\\").split("\\")[-1])
        basename_no_extension, extension = os.path.splitext(basename)
        basename_limit = max_bytes - len(prefix) - len(extension.encode("utf-8"))
        basename_no_extension = truncate_string_byte(basename_no_extension, max(0, basename_limit))

        if basename_limit < 0:
            extension = truncate_string_byte(extension, max_bytes - len(prefix))

        return os.path.join(incomplete_folder_path, prefix + basename_no_extension + extension)

    def get_current_download_file_path(self, username, virtual_path, download_folder_path, size):
        """Returns the current file path of a download."""

        return (self.get_complete_download_file_path(username, virtual_path, size, download_folder_path)
                or self.get_incomplete_download_file_path(username, virtual_path))

    def enqueue_folder(self, username, folder_path, download_folder_path=None):

        self.requested_folders[username][folder_path] = download_folder_path
        self.requested_folder_token = slskmessages.increment_token(self.requested_folder_token)

        core.send_message_to_peer(
            username, slskmessages.FolderContentsRequest(directory=folder_path, token=self.requested_folder_token))

    def enqueue_download(self, username, virtual_path, folder_path=None, size=0, file_attributes=None,
                         bypass_filter=False):

        transfer = self.transfers.get(username + virtual_path)

        if folder_path:
            folder_path = clean_path(folder_path)
        else:
            folder_path = self.get_default_download_folder(username)

        if transfer is not None and transfer.folder_path != folder_path and transfer.status == TransferStatus.FINISHED:
            # Only one user + virtual path transfer possible at a time, remove the old one
            self._clear_transfer(transfer, update_parent=False)
            transfer = None

        if transfer is not None:
            # Duplicate download found, stop here
            return

        transfer = Transfer(
            username=username, virtual_path=virtual_path, folder_path=folder_path,
            size=size, file_attributes=file_attributes
        )

        self._append_transfer(transfer)
        self._enqueue_transfer(transfer, bypass_filter=bypass_filter)
        self._update_transfer(transfer)

    def retry_download(self, transfer, bypass_filter=False):

        username = transfer.username
        active_downloads = self.active_users.get(username, {}).values()

        if transfer in active_downloads or transfer.status == TransferStatus.FINISHED:
            # Don't retry active or finished downloads
            return

        self._dequeue_transfer(transfer)
        self._unfail_transfer(transfer)
        self._enqueue_transfer(transfer, bypass_filter=bypass_filter)
        self._update_transfer(transfer)

    def retry_downloads(self, downloads):

        num_downloads = len(downloads)

        for download in downloads:
            # Provide a way to bypass download filters in case the user actually wants a file.
            # To avoid accidentally bypassing filters, ensure that only a single file is selected,
            # and it has the "Filtered" status.

            bypass_filter = (num_downloads == 1 and download.status == TransferStatus.FILTERED)
            self.retry_download(download, bypass_filter)

    def abort_downloads(self, downloads, status=TransferStatus.PAUSED):

        ignored_statuses = {status, TransferStatus.FINISHED}

        for download in downloads:
            if download.status not in ignored_statuses:
                self._abort_transfer(download, status=status, update_parent=False)

        events.emit("abort-downloads", downloads, status)

    def clear_downloads(self, downloads=None, statuses=None, clear_deleted=False):

        if downloads is None:
            # Clear all downloads
            downloads = self.transfers.copy().values()
        else:
            downloads = downloads.copy()

        for download in downloads:
            if statuses and download.status not in statuses:
                continue

            if clear_deleted:
                if download.status != TransferStatus.FINISHED:
                    continue

                if self.get_complete_download_file_path(
                        download.username, download.virtual_path, download.size, download.folder_path):
                    continue

            self._clear_transfer(download, update_parent=False)

        events.emit("clear-downloads", downloads, statuses, clear_deleted)

    # Events #

    def _shares_ready(self, _successful):
        """Send any QueueUpload messages we delayed while our shares were
        initializing.
        """

        for transfer, msg in self._pending_queue_messages.items():
            core.send_message_to_peer(transfer.username, msg)

        self._pending_queue_messages.clear()

    def _user_status(self, msg):
        """Server code 7."""

        update = False
        username = msg.user

        if msg.status == slskmessages.UserStatus.OFFLINE:
            for users in (self.queued_users, self.failed_users):
                for download in users.get(username, {}).copy().values():
                    self._abort_transfer(download, status=TransferStatus.USER_LOGGED_OFF)
                    update = True

            for download in self.active_users.get(username, {}).copy().values():
                if download.status != TransferStatus.TRANSFERRING:
                    self._abort_transfer(download, status=TransferStatus.USER_LOGGED_OFF)
                    update = True
        else:
            for download in self.failed_users.get(username, {}).copy().values():
                self._unfail_transfer(download)
                self._enqueue_transfer(download)
                update = True

        if update:
            events.emit("update-downloads")

    def _set_connection_stats(self, download_bandwidth=0, **_unused):
        self.total_bandwidth = download_bandwidth

    def _peer_connection_error(self, username, msgs=None, is_offline=False, is_timeout=True):

        if msgs is None:
            return

        for msg in msgs:
            if msg.__class__ is slskmessages.QueueUpload:
                self._cant_connect_queue_file(username, msg.file, is_offline, is_timeout)

    def _peer_connection_closed(self, username, msgs=None):
        self._peer_connection_error(username, msgs, is_timeout=False)

    def _cant_connect_queue_file(self, username, virtual_path, is_offline, is_timeout):
        """We can't connect to the user, either way (QueueUpload)."""

        download = self.queued_users.get(username, {}).get(virtual_path)

        if download is None:
            return

        if is_offline:
            status = TransferStatus.USER_LOGGED_OFF

        elif is_timeout:
            status = TransferStatus.CONNECTION_TIMEOUT

        else:
            status = TransferStatus.CONNECTION_CLOSED

        log.add_transfer(("Download attempt for file %(filename)s from user %(user)s failed "
                          "with status %(status)s"), {
            "filename": virtual_path,
            "user": username,
            "status": status
        })
        self._abort_transfer(download, status=status)

    def _folder_contents_response(self, msg, check_num_files=True):
        """Peer code 37."""

        username = msg.username

        if username not in self.requested_folders:
            return

        for folder_path, files in msg.list.items():
            if folder_path not in self.requested_folders[username]:
                continue

            log.add_transfer("Received response for folder content request from user %s", username)

            num_files = len(files)

            if check_num_files and num_files > 100:
                events.emit("download-large-folder", username, folder_path, num_files, msg)
                return

            destination_folder_path = self.get_folder_destination(username, folder_path)
            del self.requested_folders[username][folder_path]

            if num_files > 1:
                files.sort(key=lambda x: strxfrm(x[1]))

            log.add_transfer(("Attempting to download files in folder %(folder)s for user %(user)s. "
                              "Destination path: %(destination)s"), {
                "folder": folder_path,
                "user": username,
                "destination": destination_folder_path
            })

            for _code, basename, file_size, _ext, file_attributes, *_unused in files:
                virtual_path = folder_path.rstrip("\\") + "\\" + basename

                self.enqueue_download(
                    username, virtual_path, folder_path=destination_folder_path, size=file_size,
                    file_attributes=file_attributes)

    def _transfer_request(self, msg):
        """Peer code 40."""

        if msg.direction != slskmessages.TransferDirection.UPLOAD:
            return

        username = msg.username
        response = self._transfer_request_downloads(msg)

        log.add_transfer(("Responding to download request with token %(token)s for file %(filename)s "
                          "from user: %(user)s, allowed: %(allowed)s, reason: %(reason)s"), {
            "token": response.token, "filename": msg.file, "user": username,
            "allowed": response.allowed, "reason": response.reason
        })

        core.send_message_to_peer(username, response)

    def _transfer_request_downloads(self, msg):

        username = msg.username
        virtual_path = msg.file
        size = msg.filesize
        token = msg.token

        log.add_transfer("Received download request with token %(token)s for file %(filename)s from user %(user)s", {
            "token": token,
            "filename": virtual_path,
            "user": username
        })

        download = (self.queued_users.get(username, {}).get(virtual_path)
                    or self.failed_users.get(username, {}).get(virtual_path))

        if download is not None:
            # Remote peer is signaling a transfer is ready, attempting to download it

            # If the file is larger than 2GB, the SoulseekQt client seems to
            # send a malformed file size (0 bytes) in the TransferRequest response.
            # In that case, we rely on the cached, correct file size we received when
            # we initially added the download.

            self._unfail_transfer(download)
            self._dequeue_transfer(download)

            if size > 0:
                if download.size != size:
                    # The remote user's file contents have changed since we queued the download
                    download.size_changed = True

                download.size = size

            self._activate_transfer(download, token)
            self._update_transfer(download)

            return slskmessages.TransferResponse(allowed=True, token=token)

        download = self.transfers.get(username + virtual_path)
        cancel_reason = TransferRejectReason.CANCELLED

        if download is not None:
            if download.status == TransferStatus.FINISHED:
                # SoulseekQt sends "Complete" as the reason for rejecting the download if it exists
                cancel_reason = TransferRejectReason.COMPLETE

        elif self.can_upload(username):
            if self.get_complete_download_file_path(username, virtual_path, size):
                # Check if download exists in our default download folder
                cancel_reason = TransferRejectReason.COMPLETE
            else:
                # If this file is not in your download queue, then it must be
                # a remotely initiated download and someone is manually uploading to you
                parent_folder_path = virtual_path.replace("/", "\\").split("\\")[-2]
                folder_path = os.path.join(
                    os.path.normpath(config.sections["transfers"]["uploaddir"]), username, parent_folder_path)

                transfer = Transfer(
                    username=username, virtual_path=virtual_path, folder_path=folder_path, size=size)

                self._append_transfer(transfer)
                self._activate_transfer(transfer, token)
                self._update_transfer(transfer)

                return slskmessages.TransferResponse(allowed=True, token=token)

        log.add_transfer("Denied file request: User %(user)s, %(msg)s", {
            "user": username,
            "msg": msg
        })

        return slskmessages.TransferResponse(allowed=False, reason=cancel_reason, token=token)

    def _transfer_timeout(self, transfer):

        if transfer.request_timer_id is None:
            return

        log.add_transfer("Download %(filename)s with token %(token)s for user %(user)s timed out", {
            "filename": transfer.virtual_path,
            "token": transfer.token,
            "user": transfer.username
        })

        self._abort_transfer(transfer, status=TransferStatus.CONNECTION_TIMEOUT)

    def _download_file_error(self, username, token, error):
        """Networking thread encountered a local file error for download."""

        download = self.active_users.get(username, {}).get(token)

        if download is None:
            return

        self._abort_transfer(download, status=TransferStatus.LOCAL_FILE_ERROR)
        log.add(_("Download I/O error: %s"), error)

    def _file_transfer_init(self, msg):
        """A peer is requesting to start uploading a file to us."""

        if msg.is_outgoing:
            # Upload init message sent to ourselves, ignore
            return

        username = msg.username
        token = msg.token
        download = self.active_users.get(username, {}).get(token)

        if download is None or download.sock is not None:
            return

        virtual_path = download.virtual_path
        incomplete_folder_path = os.path.normpath(config.sections["transfers"]["incompletedir"])
        sock = download.sock = msg.sock
        need_update = True

        log.add_transfer(("Received file download init with token %(token)s for file %(filename)s "
                          "from user %(user)s"), {
            "token": token,
            "filename": virtual_path,
            "user": username
        })

        try:
            incomplete_folder_path_encoded = encode_path(incomplete_folder_path)

            if not os.path.isdir(incomplete_folder_path_encoded):
                os.makedirs(incomplete_folder_path_encoded)

            incomplete_file_path = self.get_incomplete_download_file_path(username, virtual_path)
            file_handle = open(encode_path(incomplete_file_path), "ab+")  # pylint: disable=consider-using-with

            try:
                import fcntl
                try:
                    fcntl.lockf(file_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as error:
                    log.add(_("Can't get an exclusive lock on file - I/O error: %s"), error)
            except ImportError:
                pass

            if download.size_changed:
                # Remote user sent a different file size than we originally requested,
                # wipe any existing data in the incomplete file to avoid corruption
                file_handle.truncate(0)

            # Seek to the end of the file for resuming the download
            offset = file_handle.seek(0, os.SEEK_END)

        except OSError as error:
            log.add(_("Cannot save file in %(folder_path)s: %(error)s"), {
                "folder_path": incomplete_folder_path,
                "error": error
            })
            self._abort_transfer(download, status=TransferStatus.DOWNLOAD_FOLDER_ERROR)
            core.notifications.show_download_notification(
                str(error), title=_("Download Folder Error"), high_priority=True)
            need_update = False

        else:
            download.file_handle = file_handle
            download.last_byte_offset = offset
            download.last_update = time.monotonic()
            download.start_time = download.last_update - download.time_elapsed

            core.statistics.append_stat_value("started_downloads", 1)
            core.pluginhandler.download_started_notification(username, virtual_path, incomplete_file_path)

            log.add_download(
                _("Download started: user %(user)s, file %(file)s"), {
                    "user": username,
                    "file": file_handle.name.decode("utf-8", "replace")
                }
            )

            if download.size > offset:
                download.status = TransferStatus.TRANSFERRING
                core.send_message_to_network_thread(slskmessages.DownloadFile(
                    sock=sock, token=token, file=file_handle, leftbytes=(download.size - offset)
                ))
                core.send_message_to_peer(username, slskmessages.FileOffset(sock, offset))

            else:
                self._finish_transfer(download)
                need_update = False

        events.emit("download-notification")

        if need_update:
            self._update_transfer(download)

    def _upload_denied(self, msg):
        """Peer code 50."""

        username = msg.username
        virtual_path = msg.file
        reason = msg.reason
        queued_downloads = self.queued_users.get(username, {})
        download = queued_downloads.get(virtual_path)

        if download is None:
            return

        if reason in TransferStatus.__dict__.values():
            # Don't allow internal statuses as reason
            reason = TransferRejectReason.CANCELLED

        if reason == TransferRejectReason.FILE_NOT_SHARED and not download.legacy_attempt:
            # The peer is possibly using an old client that doesn't support Unicode
            # (Soulseek NS). Attempt to request file name encoded as latin-1 once.

            log.add_transfer("User %(user)s responded with reason '%(reason)s' for download request %(filename)s. "
                             "Attempting to request file as latin-1.", {
                                 "user": username,
                                 "reason": reason,
                                 "filename": virtual_path
                             })

            self._dequeue_transfer(download)
            download.legacy_attempt = True
            self._enqueue_transfer(download)
            self._update_transfer(download)
            return

        if (reason in {TransferRejectReason.TOO_MANY_FILES, TransferRejectReason.TOO_MANY_MEGABYTES}
                or reason.startswith("User limit of")):
            # Make limited downloads appear as queued, and automatically resume them later
            reason = TransferRejectReason.QUEUED
            self._user_queue_limits[username] = max(5, len(queued_downloads) - 1)

        self._abort_transfer(download, status=reason)
        self._update_transfer(download)

        log.add_transfer("Download request denied by user %(user)s for file %(filename)s. Reason: %(reason)s", {
            "user": username,
            "filename": virtual_path,
            "reason": msg.reason
        })

    def _upload_failed(self, msg):
        """Peer code 46."""

        username = msg.username
        virtual_path = msg.file
        download = self.transfers.get(username + virtual_path)

        if download is None:
            return

        if download.token not in self.active_users.get(username, {}):
            return

        should_retry = not download.legacy_attempt

        if should_retry:
            # Attempt to request file name encoded as latin-1 once

            self._dequeue_transfer(download)
            download.legacy_attempt = True
            self._enqueue_transfer(download)
            self._update_transfer(download)
            return

        # Already failed once previously, give up
        self._abort_transfer(download, status=TransferStatus.CONNECTION_CLOSED)

        log.add_transfer("Upload attempt by user %(user)s for file %(filename)s failed. Reason: %(reason)s", {
            "filename": virtual_path,
            "user": username,
            "reason": download.status
        })

    def _file_download_progress(self, username, token, bytes_left):
        """A file download is in progress."""

        download = self.active_users.get(username, {}).get(token)

        if download is None:
            return

        if download.request_timer_id is not None:
            events.cancel_scheduled(download.request_timer_id)
            download.request_timer_id = None

        current_time = time.monotonic()
        size = download.size

        download.status = TransferStatus.TRANSFERRING
        download.time_elapsed = current_time - download.start_time
        download.current_byte_offset = current_byte_offset = (size - bytes_left)
        byte_difference = current_byte_offset - download.last_byte_offset

        if byte_difference:
            core.statistics.append_stat_value("downloaded_size", byte_difference)

            if size > current_byte_offset or download.speed is None:
                download.speed = int(max(0, byte_difference // max(0.1, current_time - download.last_update)))
                download.time_left = (size - current_byte_offset) // download.speed if download.speed else 0
            else:
                download.time_left = 0

        download.last_byte_offset = current_byte_offset
        download.last_update = current_time

        self._update_transfer(download)

    def _file_connection_closed(self, username, token, sock, **_unused):
        """A file download connection has closed for any reason."""

        download = self.active_users.get(username, {}).get(token)

        if download is None:
            return

        if download.sock != sock:
            return

        if download.current_byte_offset is not None and download.current_byte_offset >= download.size:
            self._finish_transfer(download)
            return

        if core.user_statuses.get(download.username) == slskmessages.UserStatus.OFFLINE:
            status = TransferStatus.USER_LOGGED_OFF
        else:
            status = TransferStatus.CANCELLED

        self._abort_transfer(download, status=status)

    def _place_in_queue_response(self, msg):
        """Peer code 44.

        The peer tells us our place in queue for a particular transfer
        """

        username = msg.username
        virtual_path = msg.filename
        download = self.queued_users.get(username, {}).get(virtual_path)

        if download is None:
            return

        download.queue_position = msg.place
        self._update_transfer(download, update_parent=False)
