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
import time

from pynicotine import slskmessages
from pynicotine.config import config
from pynicotine.core import core
from pynicotine.events import events
from pynicotine.logfacility import log
from pynicotine.shares import PermissionLevel
from pynicotine.slskmessages import TransferRejectReason
from pynicotine.transfers import Transfer
from pynicotine.transfers import Transfers
from pynicotine.transfers import TransferStatus
from pynicotine.utils import encode_path
from pynicotine.utils import human_speed


class Uploads(Transfers):

    def __init__(self):

        super().__init__(transfers_file_path=os.path.join(config.data_folder_path, "uploads.json"))

        self.pending_shutdown = False
        self.privileged_users = set()
        self.upload_speed = 0
        self.token = 0

        self._pending_network_msgs = []
        self._user_update_counter = 0
        self._user_update_counters = {}

        self._upload_queue_timer_id = None
        self._retry_failed_uploads_timer_id = None

        for event_name, callback in (
            ("add-privileged-user", self._add_to_privileged),
            ("file-connection-closed", self._file_connection_closed),
            ("file-transfer-init", self._file_transfer_init),
            ("file-upload-progress", self._file_upload_progress),
            ("peer-connection-closed", self._peer_connection_error),
            ("peer-connection-error", self._peer_connection_error),
            ("place-in-queue-request", self._place_in_queue_request),
            ("queue-upload", self._queue_upload),
            ("remove-privileged-user", self._remove_from_privileged),
            ("schedule-quit", self._schedule_quit),
            ("set-connection-stats", self._set_connection_stats),
            ("shares-ready", self._shares_ready),
            ("transfer-request", self._transfer_request),
            ("transfer-response", self._transfer_response),
            ("upload-file-error", self._upload_file_error),
            ("user-stats", self._user_stats),
            ("user-status", self._user_status)
        ):
            events.connect(event_name, callback)

    def _schedule_quit(self, should_finish_uploads):

        if not should_finish_uploads:
            return

        self.pending_shutdown = True
        self._check_upload_queue()

    def _quit(self):

        super()._quit()

        self.upload_speed = 0
        self.token = 0

    def _server_login(self, msg):

        if not msg.success:
            return

        super()._server_login(msg)

        # Check if queued uploads can be started every 10 seconds
        self._upload_queue_timer_id = events.schedule(delay=10, callback=self._check_upload_queue, repeat=True)

        # Re-queue timed out uploads every 3 minutes
        self._retry_failed_uploads_timer_id = events.schedule(
            delay=180, callback=self._retry_failed_uploads, repeat=True)

    def _server_disconnect(self, msg):

        super()._server_disconnect(msg)

        for timer_id in (self._upload_queue_timer_id, self._retry_failed_uploads_timer_id):
            events.cancel_scheduled(timer_id)

        events.emit("update-uploads")

        self.privileged_users.clear()
        self._pending_network_msgs.clear()
        self._user_update_counters.clear()
        self._user_update_counter = 0

        # Quit in case we were waiting for uploads to finish
        self._check_upload_queue()

    # Load Transfers #

    def _load_transfers(self):

        for transfer in self._get_stored_transfers(
                self.transfers_file_path, self._load_transfers_file, load_only_finished=True):
            self._append_transfer(transfer)

    # Privileges #

    def _add_to_privileged(self, username):
        self.privileged_users.add(username)

    def _remove_from_privileged(self, username):
        if username in self.privileged_users:
            self.privileged_users.remove(username)

    def is_privileged(self, username):

        if not username:
            return False

        if username in self.privileged_users:
            return True

        return self.is_buddy_prioritized(username)

    def is_buddy_prioritized(self, username):

        if not username:
            return False

        if username not in core.userlist.buddies:
            return False

        user_data = core.userlist.buddies[username]

        # All users
        if config.sections["transfers"]["preferfriends"]:
            return True

        # Only explicitly prioritized users
        return bool(user_data.is_prioritized)

    # Stats/Limits #

    @staticmethod
    def _get_file_size(file_path):

        try:
            size = os.path.getsize(encode_path(file_path))
        except Exception:
            # file doesn't exist (remote files are always this)
            size = 0

        return size

    def get_downloading_users(self):
        return set(self.active_users).union(self.queued_users)

    def get_total_uploads_allowed(self):

        if config.sections["transfers"]["useupslots"]:
            upload_slots = config.sections["transfers"]["uploadslots"]
        else:
            upload_slots = len(self.active_users)

            if self.is_new_upload_accepted():
                return upload_slots + 1

        if upload_slots <= 0:
            upload_slots = 1

        return upload_slots

    def get_upload_queue_size(self, username):

        if self.is_privileged(username):
            return sum(
                len(queued_uploads)
                for username, queued_uploads in self.queued_users.items() if self.is_privileged(username)
            )

        return len(self.queued_transfers)

    def has_active_uploads(self):
        return bool(self.active_users or self.queued_users)

    def is_queue_limit_reached(self, username):

        file_limit = config.sections["transfers"]["filelimit"]
        queue_size_limit = config.sections["transfers"]["queuelimit"] * 1024 * 1024

        if len(self.queued_users.get(username, {})) >= file_limit >= 1:
            return True, TransferRejectReason.TOO_MANY_FILES

        if self._user_queue_sizes.get(username, 0) >= queue_size_limit >= 1:
            return True, TransferRejectReason.TOO_MANY_MEGABYTES

        return False, None

    def is_slot_limit_reached(self):

        upload_slot_limit = config.sections["transfers"]["uploadslots"]

        if upload_slot_limit <= 0:
            upload_slot_limit = 1

        return len(self.active_users) >= upload_slot_limit

    def is_bandwidth_limit_reached(self):

        bandwidth_limit = config.sections["transfers"]["uploadbandwidth"] * 1024

        if not bandwidth_limit:
            return False

        return self.total_bandwidth >= bandwidth_limit

    def is_new_upload_accepted(self):

        if core.shares is None or core.shares.rescanning:
            return False

        if config.sections["transfers"]["useupslots"]:
            # Limit by upload slots
            if self.is_slot_limit_reached():
                return False

        elif self.is_bandwidth_limit_reached():
            # Limit by maximum bandwidth
            return False

        # No limits
        return True

    @staticmethod
    def is_file_readable(virtual_path, real_path):

        try:
            if os.access(encode_path(real_path), os.R_OK):
                return True

            log.add_transfer("Cannot access file, not sharing: %(virtual_name)s with real path %(path)s", {
                "virtual_name": virtual_path,
                "path": real_path
            })

        except Exception:
            log.add_transfer(("Requested file path contains invalid characters or other errors, not sharing: "
                              "%(virtual_name)s with real path %(path)s"), {
                "virtual_name": virtual_path,
                "path": real_path
            })

        return False

    def is_upload_queued(self, username, virtual_path):

        if virtual_path in self.queued_users.get(username, {}):
            return True

        return any(upload.virtual_path == virtual_path for upload in self.active_users.get(username, {}).values())

    def update_transfer_limits(self):

        events.emit("update-upload-limits")

        if core.user_status == slskmessages.UserStatus.OFFLINE:
            return

        use_speed_limit = config.sections["transfers"]["use_upload_speed_limit"]
        limit_by = config.sections["transfers"]["limitby"]

        if use_speed_limit == "primary":
            speed_limit = config.sections["transfers"]["uploadlimit"]

        elif use_speed_limit == "alternative":
            speed_limit = config.sections["transfers"]["uploadlimitalt"]

        else:
            speed_limit = 0

        core.send_message_to_network_thread(slskmessages.SetUploadLimit(speed_limit, limit_by))
        self._check_upload_queue()

    # Transfer Actions #

    def _append_transfer(self, transfer):

        username = transfer.username
        virtual_path = transfer.virtual_path
        old_upload = self.transfers.get(username + virtual_path)

        if self.is_privileged(username):
            transfer.modifier = "privileged" if username in self.privileged_users else "prioritized"

        if old_upload is not None:
            if virtual_path in self.queued_users.get(username, {}):
                old_size = old_upload.size
                new_size = transfer.size

                if new_size != old_size:
                    self._user_queue_sizes[username] -= old_size
                    self._user_queue_sizes[username] += new_size

                    old_upload.size = new_size

                old_upload.folder_path = transfer.folder_path
                self._update_transfer(old_upload)
                return

            if old_upload.status != TransferStatus.FINISHED:
                transfer.current_byte_offset = old_upload.current_byte_offset
                transfer.time_elapsed = old_upload.time_elapsed
                transfer.time_left = old_upload.time_left
                transfer.speed = old_upload.speed

            self._clear_transfer(old_upload)

        self.transfers[username + virtual_path] = transfer

    def _dequeue_transfer(self, transfer):

        username = transfer.username

        super()._dequeue_transfer(transfer)

        if username not in self.queued_users:
            self._user_update_counters.pop(username, None)

    def _activate_transfer(self, transfer, token):
        super()._activate_transfer(transfer, token)
        self._user_update_counters.pop(transfer.username, None)

    def _update_transfer(self, transfer, update_parent=True):

        username = transfer.username

        # Don't update existing user counter for queued uploads
        # We don't want to push the user back in the queue if they enqueued new files
        if (username not in self._user_update_counters
                or transfer.virtual_path not in self.queued_users.get(username, {})):
            self._update_user_counter(username)

        events.emit("update-upload", transfer, update_parent)

    def _enqueue_limited_transfers(self, username):
        # Not used for uploads
        pass

    def _finish_transfer(self, transfer):

        username = transfer.username
        virtual_path = transfer.virtual_path

        self._deactivate_transfer(transfer)
        self._close_file(transfer)

        transfer.status = TransferStatus.FINISHED
        transfer.current_byte_offset = transfer.size
        transfer.sock = None

        log.add_upload(
            _("Upload finished: user %(user)s, IP address %(ip)s, file %(file)s"), {
                "user": username,
                "ip": core.user_addresses.get(username),
                "file": virtual_path
            }
        )

        core.statistics.append_stat_value("completed_uploads", 1)

        # Autoclear this upload
        if not self._auto_clear_transfer(transfer):
            self._update_transfer(transfer)

        real_path = core.shares.virtual2real(virtual_path)
        core.pluginhandler.upload_finished_notification(username, virtual_path, real_path)

        self._check_upload_queue()

    def _abort_transfer(self, transfer, denied_message=None, status=None, update_parent=True):

        username = transfer.username
        virtual_path = transfer.virtual_path

        if transfer.sock is not None:
            core.send_message_to_network_thread(slskmessages.CloseConnection(transfer.sock))
            transfer.sock = None

        if transfer.file_handle is not None:
            self._close_file(transfer)

            log.add_upload(
                _("Upload aborted, user %(user)s file %(file)s"), {
                    "user": username,
                    "file": virtual_path
                }
            )

        elif denied_message and virtual_path in self.queued_users.get(username, {}):
            core.send_message_to_peer(
                username, slskmessages.UploadDenied(file=virtual_path, reason=denied_message))

        self._deactivate_transfer(transfer)
        self._dequeue_transfer(transfer)
        self._unfail_transfer(transfer)
        self._update_user_counter(username)

        if not status:
            return

        transfer.status = status

        if status not in {TransferStatus.FINISHED, TransferStatus.CANCELLED}:
            self._fail_transfer(transfer)

        events.emit("abort-upload", transfer, status, update_parent)

    def _auto_clear_transfer(self, transfer):

        if config.sections["transfers"]["autoclear_uploads"]:
            self._clear_transfer(transfer)
            return True

        return False

    def _clear_transfer(self, transfer, denied_message=None, update_parent=True):

        self._abort_transfer(transfer, denied_message=denied_message)
        del self.transfers[transfer.username + transfer.virtual_path]

        events.emit("clear-upload", transfer, update_parent)

    def _retry_failed_uploads(self):

        for failed_uploads in self.failed_users.copy().values():
            for upload in failed_uploads.copy().values():
                if upload.status != TransferStatus.CONNECTION_TIMEOUT:
                    continue

                self._unfail_transfer(upload)
                self._enqueue_transfer(upload)
                self._update_transfer(upload)

    def _check_queue_upload_allowed(self, username, addr, virtual_path, real_path, msg):

        # Is user allowed to download?
        ip_address, _port = addr
        permission_level, reject_reason = core.shares.check_user_permission(username, ip_address)

        if permission_level == PermissionLevel.BANNED:
            reject_message = TransferRejectReason.BANNED

            if reject_reason:
                reject_message += f" ({reject_reason})"

            return False, reject_message

        if core.shares.rescanning:
            self._pending_network_msgs.append(msg)
            return False, None

        # Is that file already in the queue?
        if self.is_upload_queued(username, virtual_path):
            return False, TransferRejectReason.QUEUED

        # Are we waiting for existing uploads to finish?
        if self.pending_shutdown:
            return False, TransferRejectReason.PENDING_SHUTDOWN

        # Has user hit queue limit?
        enable_limits = True

        if config.sections["transfers"]["friendsnolimits"]:
            if username in core.userlist.buddies:
                enable_limits = False

        if enable_limits:
            limit_reached, reason = self.is_queue_limit_reached(username)

            if limit_reached:
                return False, reason

        # Do we actually share that file with the world?
        if not core.shares.file_is_shared(username, virtual_path, real_path):
            return False, TransferRejectReason.FILE_NOT_SHARED

        if not self.is_file_readable(virtual_path, real_path):
            return False, TransferRejectReason.FILE_READ_ERROR

        return True, None

    def _get_upload_candidate(self):
        """Retrieve a suitable queued transfer for uploading.

        Round Robin: Get the first queued item from the oldest user
        FIFO: Get the first queued item in the list
        """

        is_fifo_queue = config.sections["transfers"]["fifoqueue"]
        has_active_uploads = bool(self.active_users)
        oldest_time = None
        target_username = None
        upload_candidate = None
        privileged_users = set()

        for username in self._user_update_counters:
            if self.is_privileged(username):
                privileged_users.add(username)

        if is_fifo_queue:
            for upload in self.queued_transfers:
                username = upload.username

                if privileged_users and username not in privileged_users:
                    continue

                if username not in self._user_update_counters:
                    continue

                target_username = username
                break
        else:
            for username, update_time in self._user_update_counters.items():
                if privileged_users and username not in privileged_users:
                    continue

                if not oldest_time:
                    oldest_time = update_time + 1

                if update_time < oldest_time:
                    target_username = username
                    oldest_time = update_time

        if target_username is not None:
            upload_candidate = next(iter(self.queued_users[target_username].values()), None)

        return upload_candidate, has_active_uploads

    def _update_user_counter(self, username):
        """Called when an upload associated with a user has changed.

        The user update counter is used by the Round Robin queue system
        to determine which user has waited the longest since their last
        download.
        """

        if username in self.queued_users and username not in self.active_users:
            self._user_update_counter += 1
            self._user_update_counters[username] = self._user_update_counter

    def _check_upload_queue(self):
        """Find next file to upload."""

        if not self.is_new_upload_accepted():
            return

        upload_candidate, has_active_uploads = self._get_upload_candidate()

        if upload_candidate is None:
            if not has_active_uploads and self.pending_shutdown:
                self.pending_shutdown = False
                core.quit()
            return

        username = upload_candidate.username

        if slskmessages.UserStatus.OFFLINE in (core.user_status, core.user_statuses.get(username)):
            # Either we are offline or the user we want to upload to is
            if self._auto_clear_transfer(upload_candidate):
                return

            self._abort_transfer(upload_candidate, status=TransferStatus.USER_LOGGED_OFF)
            return

        self.token = slskmessages.increment_token(self.token)
        virtual_path = upload_candidate.virtual_path

        log.add_transfer(
            "Checked upload queue, requesting to upload file %(file)s with token %(token)s to user %(user)s", {
                "file": virtual_path,
                "token": self.token,
                "user": username
            }
        )

        self._dequeue_transfer(upload_candidate)
        self._unfail_transfer(upload_candidate)
        self._activate_transfer(upload_candidate, self.token)

        core.send_message_to_peer(
            username, slskmessages.TransferRequest(
                direction=slskmessages.TransferDirection.UPLOAD, token=self.token, file=virtual_path,
                filesize=upload_candidate.size))

        self._update_transfer(upload_candidate)

    def ban_users(self, users, ban_message=None):
        """Ban a user, cancel all the user's uploads, send a 'Banned' message
        via the transfers, and clear the transfers from the uploads list."""

        if not ban_message and config.sections["transfers"]["usecustomban"]:
            ban_message = config.sections["transfers"]["customban"]

        if ban_message:
            status = f"{TransferRejectReason.BANNED} ({ban_message})"
        else:
            status = TransferRejectReason.BANNED

        for upload in self.transfers.copy().values():
            if upload.username not in users:
                continue

            self._clear_transfer(upload, denied_message=status)

        for username in users:
            core.network_filter.ban_user(username)

        self._check_upload_queue()

    def enqueue_upload(self, username, virtual_path, size, folder_path=None):

        transfer = self.transfers.get(username + virtual_path)
        real_path = core.shares.virtual2real(virtual_path)
        new_size = self._get_file_size(real_path)

        if new_size > 0:
            size = new_size

        if transfer is None:
            if not folder_path:
                folder_path = os.path.dirname(real_path)
            else:
                folder_path = os.path.normpath(folder_path)

            transfer = Transfer(
                username=username, virtual_path=virtual_path, folder_path=folder_path, size=size
            )
            self._append_transfer(transfer)
        else:
            if transfer in self.active_users.get(username, {}).values():
                # Upload already in progress
                return

            if virtual_path in self.queued_users.get(username, {}):
                # Upload already queued
                return

            self._unfail_transfer(transfer)
            transfer.size = size

        if slskmessages.UserStatus.OFFLINE in (core.user_status, core.user_statuses.get(username)):
            # Either we are offline or the user we want to upload to is
            if self._auto_clear_transfer(transfer):
                return

            self._abort_transfer(transfer, status=TransferStatus.USER_LOGGED_OFF)
            return

        self._enqueue_transfer(transfer)
        self._update_transfer(transfer)
        self._check_upload_queue()

    def retry_upload(self, transfer):

        username = transfer.username
        active_uploads = self.active_users.get(username, {}).values()

        if transfer in active_uploads or transfer.status == TransferStatus.FINISHED:
            # Don't retry active or finished uploads
            return

        if transfer not in self.queued_users.get(username, {}).values():
            # User already has an active upload, queue the retry attempt
            self._unfail_transfer(transfer)
            self._enqueue_transfer(transfer)
            self._update_transfer(transfer)

        if not active_uploads:
            self._check_upload_queue()

    def retry_uploads(self, uploads):
        for upload in uploads:
            self.retry_upload(upload)

    def abort_uploads(self, uploads, denied_message=None, status=TransferStatus.CANCELLED):

        ignored_statuses = {status, TransferStatus.FINISHED}

        for upload in uploads:
            if upload.status not in ignored_statuses:
                self._abort_transfer(
                    upload, denied_message=denied_message, status=status, update_parent=False)

        events.emit("abort-uploads", uploads, status)

    def clear_uploads(self, uploads=None, statuses=None):

        if uploads is None:
            # Clear all uploads
            uploads = self.transfers.copy().values()
        else:
            uploads = uploads.copy()

        for upload in uploads:
            if statuses and upload.status not in statuses:
                continue

            self._clear_transfer(upload, update_parent=False)

        events.emit("clear-uploads", uploads, statuses)

    # Events #

    def _shares_ready(self, _successful):
        """Process any file transfer queue requests that arrived while
        scanning shares.
        """

        if self._pending_network_msgs:
            core.send_message_to_network_thread(slskmessages.EmitNetworkMessageEvents(self._pending_network_msgs[:]))
            self._pending_network_msgs.clear()

    def _user_status(self, msg):
        """Server code 7."""

        update = False
        username = msg.user
        privileged = msg.privileged
        is_user_offline = (msg.status == slskmessages.UserStatus.OFFLINE)

        if privileged is not None:
            if privileged:
                events.emit("add-privileged-user", username)
            else:
                events.emit("remove-privileged-user", username)

        if is_user_offline:
            for upload in self.active_users.get(username, {}).copy().values():
                if upload.status == TransferStatus.TRANSFERRING:
                    continue

                if not self._auto_clear_transfer(upload):
                    self._abort_transfer(upload, status=TransferStatus.USER_LOGGED_OFF)

                update = True

        for upload in self.failed_users.get(username, {}).copy().values():
            if not self._auto_clear_transfer(upload):
                self._abort_transfer(
                    upload, status=TransferStatus.USER_LOGGED_OFF if is_user_offline else TransferStatus.CANCELLED)

            update = True

        if update:
            events.emit("update-uploads")

    def _connect_to_peer(self, msg):
        """Server code 18."""

        if msg.privileged is None:
            return

        if msg.privileged:
            events.emit("add-privileged-user", msg.user)
        else:
            events.emit("remove-privileged-user", msg.user)

    def _user_stats(self, msg):
        """Server code 36."""

        if msg.user == core.login_username:
            self.upload_speed = msg.avgspeed

    def _set_connection_stats(self, upload_bandwidth=0, **_unused):
        self.total_bandwidth = upload_bandwidth

    def _peer_connection_error(self, username, msgs=None, is_offline=False, is_timeout=True):

        if msgs is None:
            return

        for msg in msgs:
            if msg.__class__ in (slskmessages.TransferRequest, slskmessages.FileTransferInit):
                self._cant_connect_upload(username, msg.token, is_offline, is_timeout)

    def _peer_connection_closed(self, username, msgs=None):
        self._peer_connection_error(username, msgs, is_timeout=False)

    def _cant_connect_upload(self, username, token, is_offline, is_timeout):
        """We can't connect to the user, either way (TransferRequest,
        FileTransferInit)."""

        upload = self.active_users.get(username, {}).get(token)

        if upload is None:
            return

        if is_offline:
            status = TransferStatus.USER_LOGGED_OFF

        elif is_timeout:
            status = TransferStatus.CONNECTION_TIMEOUT

        else:
            status = TransferStatus.CONNECTION_CLOSED

        log.add_transfer(("Upload attempt for file %(filename)s with token %(token)s to user %(user)s failed "
                          "with status %(status)s"), {
            "filename": upload.virtual_path,
            "token": token,
            "user": username,
            "status": status
        })

        upload_cleared = is_offline and self._auto_clear_transfer(upload)

        if not upload_cleared:
            self._abort_transfer(upload, status=status)

        self._check_upload_queue()

    def _queue_upload(self, msg):
        """Peer code 43.

        Peer remotely queued a download (upload here). This is the
        modern replacement to a TransferRequest with direction 0
        (download request). We will initiate the upload of the queued
        file later.
        """

        username = msg.username
        virtual_path = msg.file
        real_path = core.shares.virtual2real(virtual_path)
        allowed, reason = self._check_queue_upload_allowed(username, msg.addr, virtual_path, real_path, msg)

        log.add_transfer(("Upload request for file %(filename)s from user: %(user)s, "
                          "allowed: %(allowed)s, reason: %(reason)s"), {
            "filename": virtual_path,
            "user": username,
            "allowed": allowed,
            "reason": reason
        })

        if not allowed:
            if reason and reason != TransferRejectReason.QUEUED:
                core.send_message_to_peer(username, slskmessages.UploadDenied(file=virtual_path, reason=reason))

            return

        transfer = Transfer(username=username, virtual_path=virtual_path, folder_path=os.path.dirname(real_path),
                            size=self._get_file_size(real_path))

        self._append_transfer(transfer)
        self._enqueue_transfer(transfer)
        self._update_transfer(transfer)

        core.pluginhandler.upload_queued_notification(username, virtual_path, real_path)
        self._check_upload_queue()

    def _transfer_request(self, msg):
        """Peer code 40."""

        username = msg.username

        if msg.direction != slskmessages.TransferDirection.DOWNLOAD:
            return

        response = self._transfer_request_uploads(msg)

        if response is None:
            return

        log.add_transfer(("Responding to legacy upload request %(token)s for file %(filename)s "
                          "from user %(user)s, allowed: %(allowed)s, reason: %(reason)s"), {
            "token": response.token, "filename": msg.file, "user": username,
            "allowed": response.allowed, "reason": response.reason
        })

        core.send_message_to_peer(username, response)

    def _transfer_request_uploads(self, msg):
        """Remote peer is requesting to download a file through your upload
        queue.

        Note that the QueueUpload peer message has replaced this method
        of requesting a download in most clients.
        """

        username = msg.username
        virtual_path = msg.file
        token = msg.token

        log.add_transfer("Received legacy upload request %(token)s for file %(filename)s from user %(user)s", {
            "token": token,
            "filename": virtual_path,
            "user": username
        })

        # Is user allowed to download?
        real_path = core.shares.virtual2real(virtual_path)
        allowed, reason = self._check_queue_upload_allowed(username, msg.addr, virtual_path, real_path, msg)

        if not allowed:
            if reason:
                return slskmessages.TransferResponse(allowed=False, reason=reason, token=token)

            return None

        # All checks passed, user can queue file!
        core.pluginhandler.upload_queued_notification(username, virtual_path, real_path)

        if not self.is_new_upload_accepted() or username in self.active_users:
            transfer = Transfer(
                username=username, virtual_path=virtual_path, folder_path=os.path.dirname(real_path),
                size=self._get_file_size(real_path))

            self._append_transfer(transfer)
            self._enqueue_transfer(transfer)
            self._update_transfer(transfer)

            return slskmessages.TransferResponse(allowed=False, reason=TransferRejectReason.QUEUED, token=token)

        # All checks passed, starting a new upload.
        size = self._get_file_size(real_path)
        transfer = Transfer(
            username=username, virtual_path=virtual_path, folder_path=os.path.dirname(real_path), size=size)

        self._append_transfer(transfer)
        self._activate_transfer(transfer, token)
        self._update_transfer(transfer)

        return slskmessages.TransferResponse(allowed=True, token=token, filesize=size)

    def _transfer_response(self, msg):
        """Peer code 41.

        Received a response to the file request from the peer
        """

        username = msg.username
        token = msg.token
        reason = msg.reason

        log.add_transfer(("Received response for upload with token: %(token)s, allowed: %(allowed)s, "
                          "reason: %(reason)s, file size: %(size)s"), {
            "token": token,
            "allowed": msg.allowed,
            "reason": reason,
            "size": msg.filesize
        })

        upload = self.active_users.get(username, {}).get(token)

        if upload is None:
            log.add_transfer("Received unknown upload response: %s", msg)
            return

        if upload.sock is not None:
            log.add_transfer("Upload with token %s already has an existing file connection", token)
            return

        if reason is not None:
            if reason in TransferStatus.__dict__.values() or reason == TransferRejectReason.DISALLOWED_EXTENSION:
                # Don't allow internal statuses as reason
                reason = TransferRejectReason.CANCELLED

            self._abort_transfer(upload, status=reason)

            if reason == TransferRejectReason.COMPLETE:
                # A complete download of this file already exists on the user's end
                self._finish_transfer(upload)

            elif reason == TransferRejectReason.CANCELLED:
                self._auto_clear_transfer(upload)

            self._check_upload_queue()
            return

        core.send_message_to_peer(upload.username, slskmessages.FileTransferInit(token=token, is_outgoing=True))
        self._check_upload_queue()

    def _transfer_timeout(self, transfer):

        if transfer.request_timer_id is None:
            return

        log.add_transfer("Upload %(filename)s with token %(token)s for user %(user)s timed out", {
            "filename": transfer.virtual_path,
            "token": transfer.token,
            "user": transfer.username
        })

        self._abort_transfer(transfer, status=TransferStatus.CONNECTION_TIMEOUT)
        self._check_upload_queue()

    def _upload_file_error(self, username, token, error):
        """Networking thread encountered a local file error for upload."""

        upload = self.active_users.get(username, {}).get(token)

        if upload is None:
            return

        self._abort_transfer(upload, status=TransferStatus.LOCAL_FILE_ERROR)

        log.add(_("Upload I/O error: %s"), error)
        self._check_upload_queue()

    def _file_transfer_init(self, msg):
        """We are requesting to start uploading a file to a peer."""

        username = msg.username
        token = msg.token
        upload = self.active_users.get(username, {}).get(token)

        if upload is None or upload.sock is not None:
            return

        virtual_path = upload.virtual_path
        sock = upload.sock = msg.sock
        need_update = True

        log.add_transfer("Initializing upload with token %(token)s for file %(filename)s to user %(user)s", {
            "token": token,
            "filename": virtual_path,
            "user": username
        })

        real_path = core.shares.virtual2real(virtual_path)

        if not core.shares.file_is_shared(username, virtual_path, real_path):
            self._abort_transfer(upload, status=TransferRejectReason.FILE_NOT_SHARED)
            self._check_upload_queue()
            return

        try:
            # Open File
            file_handle = open(encode_path(real_path), "rb")  # pylint: disable=consider-using-with

        except OSError as error:
            log.add(_("Upload I/O error: %s"), error)
            self._abort_transfer(upload, status=TransferStatus.LOCAL_FILE_ERROR)
            self._check_upload_queue()

        else:
            upload.file_handle = file_handle
            upload.last_update = time.monotonic()
            upload.start_time = upload.last_update - upload.time_elapsed

            core.statistics.append_stat_value("started_uploads", 1)
            core.pluginhandler.upload_started_notification(username, virtual_path, real_path)

            log.add_upload(
                _("Upload started: user %(user)s, IP address %(ip)s, file %(file)s"), {
                    "user": username,
                    "ip": core.user_addresses.get(username),
                    "file": virtual_path
                }
            )

            if upload.size > 0:
                upload.status = TransferStatus.TRANSFERRING
                core.send_message_to_network_thread(slskmessages.UploadFile(
                    sock=sock, token=token, file=file_handle, size=upload.size
                ))

            else:
                self._finish_transfer(upload)
                need_update = False

        events.emit("upload-notification")

        if need_update:
            self._update_transfer(upload)

    def _file_upload_progress(self, username, token, offset, bytes_sent):
        """A file upload is in progress."""

        upload = self.active_users.get(username, {}).get(token)

        if upload is None:
            return

        if upload.request_timer_id is not None:
            events.cancel_scheduled(upload.request_timer_id)
            upload.request_timer_id = None

        current_time = time.monotonic()
        size = upload.size

        if not upload.last_byte_offset:
            upload.last_byte_offset = offset

        upload.status = TransferStatus.TRANSFERRING
        upload.time_elapsed = current_time - upload.start_time
        upload.current_byte_offset = current_byte_offset = (offset + bytes_sent)
        byte_difference = current_byte_offset - upload.last_byte_offset

        if byte_difference:
            core.statistics.append_stat_value("uploaded_size", byte_difference)

            if size > current_byte_offset or upload.speed is None:
                upload.speed = int(max(0, byte_difference // max(0.1, current_time - upload.last_update)))
                upload.time_left = (size - current_byte_offset) // upload.speed if upload.speed else 0
            else:
                upload.time_left = 0

        upload.last_byte_offset = current_byte_offset
        upload.last_update = current_time

        self._update_transfer(upload)

    def _file_connection_closed(self, username, token, sock, timed_out):
        """A file upload connection has closed for any reason."""

        upload = self.active_users.get(username, {}).get(token)

        if upload is None:
            return

        if upload.sock != sock:
            return

        if not timed_out and upload.current_byte_offset is not None and upload.current_byte_offset >= upload.size:
            # We finish the upload here in case the downloading peer has a slow/limited download
            # speed and finishes later than us

            if upload.speed is not None:
                # Inform the server about the last upload speed for this transfer
                log.add_transfer("Sending upload speed %s to the server", human_speed(upload.speed))
                core.send_message_to_server(slskmessages.SendUploadSpeed(upload.speed))

            self._finish_transfer(upload)
            return

        if core.user_statuses.get(upload.username) == slskmessages.UserStatus.OFFLINE:
            status = TransferStatus.USER_LOGGED_OFF
        else:
            status = TransferStatus.CANCELLED

            # Transfer ended abruptly. Tell the peer to re-queue the file. If the transfer was
            # intentionally cancelled, the peer should ignore this message.
            core.send_message_to_peer(upload.username, slskmessages.UploadFailed(file=upload.virtual_path))

        if not self._auto_clear_transfer(upload):
            self._abort_transfer(upload, status=status)

        self._check_upload_queue()

    def _place_in_queue_request(self, msg):
        """Peer code 51."""

        username = msg.username
        virtual_path = msg.file
        upload = self.queued_users.get(username, {}).get(virtual_path)

        if upload is None:
            return

        is_fifo_queue = config.sections["transfers"]["fifoqueue"]
        is_privileged_queue = self.is_privileged(username)
        privileged_queued_users = {k: v for k, v in self.queued_users.items() if self.is_privileged(k)}
        queue_position = 0

        if is_fifo_queue:
            num_non_privileged = 0

            for position, i_upload in enumerate(self.queued_transfers, start=1):
                if is_privileged_queue and i_upload.username not in privileged_queued_users:
                    num_non_privileged += 1

                if i_upload == upload:
                    queue_position += position - num_non_privileged
                    break
        else:
            for position, i_upload in enumerate(self.queued_users.get(username, {}).values(), start=1):
                if i_upload == upload:
                    if is_privileged_queue:
                        num_queued_users = len(privileged_queued_users)
                    else:
                        # Cycling through privileged users first
                        queue_position += sum(
                            len(queued_uploads) for queued_uploads in privileged_queued_users.values())
                        num_queued_users = len(self.queued_users)

                    queue_position += position * num_queued_users
                    break

        if queue_position > 0:
            core.send_message_to_peer(
                username, slskmessages.PlaceInQueueResponse(filename=virtual_path, place=queue_position))

        # Update queue position in our list of uploads
        upload.queue_position = queue_position
        self._update_transfer(upload, update_parent=False)
