# -*- coding: utf-8 -*-
# Copyright 2014-2016 OpenMarket Ltd
# Copyright 2019 Matrix.org Foundation C.I.C.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging

from twisted.internet import defer

from synapse.api.constants import Membership
from synapse.api.errors import Codes, MissingClientTokenError, SynapseError
from synapse.storage.data_stores.main.admin import TokenState
from synapse.types import RoomStreamToken
from synapse.visibility import filter_events_for_client

from ._base import BaseHandler

logger = logging.getLogger(__name__)


class AdminHandler(BaseHandler):
    def __init__(self, hs):
        super(AdminHandler, self).__init__(hs)
        self.storage = hs.get_storage()
        self.state_store = self.storage.state

    async def validate_admin_token(
        self, servlet, request, raise_if_missing: bool = True
    ) -> bool:
        """
        Validate that there is an admin token on the request, and that it can
        access this servlet.
        """
        # This servlet can't be validated by an admin token. Error out.
        if servlet.PERMISSION_CODE is None:
            raise SynapseError(403, "Forbidden", errcode=Codes.FORBIDDEN)

        auth_headers = request.requestHeaders.getRawHeaders(b"Authorization")

        if not auth_headers:
            raise MissingClientTokenError("Missing Authorization header.")

        if len(auth_headers) > 1:
            raise MissingClientTokenError("Too many Authorization headers.")

        parts = auth_headers[0].split(b" ")
        if parts[0] == b"Bearer" and len(parts) == 2:
            token = parts[1].decode("ascii")
        else:
            raise MissingClientTokenError("Invalid Authorization header.")

        token_rules = await self.store.get_permissions_for_token(token)

        if not raise_if_missing and token_rules.token_state is TokenState.NON_EXISTANT:
            return False

        action = request.method.decode("ascii")

        if token_rules.permissions[servlet.PERMISSION_CODE][action] is True:
            return True
        else:
            raise SynapseError(403, "Forbidden", errcode=Codes.FORBIDDEN)

    async def get_permissions_for_token(self, token):

        token_rules = await self.store.get_permissions_for_token(token)

        return token_rules

    async def set_permission_for_token(
        self, admin_token: str, endpoint: str, action: str, allowed: bool
    ) -> bool:

        if action not in ["GET", "PUT", "POST", "DELETE"]:
            raise ValueError("%r is an invalid action" % (action,))

        return await self.store.set_permission_for_token(
            admin_token=admin_token, endpoint=endpoint, action=action, allowed=allowed
        )

    async def create_admin_token(self, valid_until, creator, description):

        token = await self.store.create_admin_token(
            valid_until=valid_until, creator=creator, description=description
        )
        return token

    @defer.inlineCallbacks
    def get_whois(self, user):
        connections = []

        sessions = yield self.store.get_user_ip_and_agents(user)
        for session in sessions:
            connections.append(
                {
                    "ip": session["ip"],
                    "last_seen": session["last_seen"],
                    "user_agent": session["user_agent"],
                }
            )

        ret = {
            "user_id": user.to_string(),
            "devices": {"": {"sessions": [{"connections": connections}]}},
        }

        return ret

    @defer.inlineCallbacks
    def get_users(self):
        """Function to reterive a list of users in users table.

        Args:
        Returns:
            defer.Deferred: resolves to list[dict[str, Any]]
        """
        ret = yield self.store.get_users()

        return ret

    @defer.inlineCallbacks
    def get_users_paginate(self, order, start, limit):
        """Function to reterive a paginated list of users from
        users list. This will return a json object, which contains
        list of users and the total number of users in users table.

        Args:
            order (str): column name to order the select by this column
            start (int): start number to begin the query from
            limit (int): number of rows to reterive
        Returns:
            defer.Deferred: resolves to json object {list[dict[str, Any]], count}
        """
        ret = yield self.store.get_users_paginate(order, start, limit)

        return ret

    @defer.inlineCallbacks
    def search_users(self, term):
        """Function to search users list for one or more users with
        the matched term.

        Args:
            term (str): search term
        Returns:
            defer.Deferred: resolves to list[dict[str, Any]]
        """
        ret = yield self.store.search_users(term)

        return ret

    def get_user_server_admin(self, user):
        """
        Get the admin bit on a user.

        Args:
            user_id (UserID): the (necessarily local) user to manipulate
        """
        return self.store.is_server_admin(user)

    def set_user_server_admin(self, user, admin):
        """
        Set the admin bit on a user.

        Args:
            user_id (UserID): the (necessarily local) user to manipulate
            admin (bool): whether or not the user should be an admin of this server
        """
        return self.store.set_server_admin(user, admin)

    @defer.inlineCallbacks
    def export_user_data(self, user_id, writer):
        """Write all data we have on the user to the given writer.

        Args:
            user_id (str)
            writer (ExfiltrationWriter)

        Returns:
            defer.Deferred: Resolves when all data for a user has been written.
            The returned value is that returned by `writer.finished()`.
        """
        # Get all rooms the user is in or has been in
        rooms = yield self.store.get_rooms_for_user_where_membership_is(
            user_id,
            membership_list=(
                Membership.JOIN,
                Membership.LEAVE,
                Membership.BAN,
                Membership.INVITE,
            ),
        )

        # We only try and fetch events for rooms the user has been in. If
        # they've been e.g. invited to a room without joining then we handle
        # those seperately.
        rooms_user_has_been_in = yield self.store.get_rooms_user_has_been_in(user_id)

        for index, room in enumerate(rooms):
            room_id = room.room_id

            logger.info(
                "[%s] Handling room %s, %d/%d", user_id, room_id, index + 1, len(rooms)
            )

            forgotten = yield self.store.did_forget(user_id, room_id)
            if forgotten:
                logger.info("[%s] User forgot room %d, ignoring", user_id, room_id)
                continue

            if room_id not in rooms_user_has_been_in:
                # If we haven't been in the rooms then the filtering code below
                # won't return anything, so we need to handle these cases
                # explicitly.

                if room.membership == Membership.INVITE:
                    event_id = room.event_id
                    invite = yield self.store.get_event(event_id, allow_none=True)
                    if invite:
                        invited_state = invite.unsigned["invite_room_state"]
                        writer.write_invite(room_id, invite, invited_state)

                continue

            # We only want to bother fetching events up to the last time they
            # were joined. We estimate that point by looking at the
            # stream_ordering of the last membership if it wasn't a join.
            if room.membership == Membership.JOIN:
                stream_ordering = yield self.store.get_room_max_stream_ordering()
            else:
                stream_ordering = room.stream_ordering

            from_key = str(RoomStreamToken(0, 0))
            to_key = str(RoomStreamToken(None, stream_ordering))

            written_events = set()  # Events that we've processed in this room

            # We need to track gaps in the events stream so that we can then
            # write out the state at those events. We do this by keeping track
            # of events whose prev events we haven't seen.

            # Map from event ID to prev events that haven't been processed,
            # dict[str, set[str]].
            event_to_unseen_prevs = {}

            # The reverse mapping to above, i.e. map from unseen event to events
            # that have the unseen event in their prev_events, i.e. the unseen
            # events "children". dict[str, set[str]]
            unseen_to_child_events = {}

            # We fetch events in the room the user could see by fetching *all*
            # events that we have and then filtering, this isn't the most
            # efficient method perhaps but it does guarantee we get everything.
            while True:
                events, _ = yield self.store.paginate_room_events(
                    room_id, from_key, to_key, limit=100, direction="f"
                )
                if not events:
                    break

                from_key = events[-1].internal_metadata.after

                events = yield filter_events_for_client(self.storage, user_id, events)

                writer.write_events(room_id, events)

                # Update the extremity tracking dicts
                for event in events:
                    # Check if we have any prev events that haven't been
                    # processed yet, and add those to the appropriate dicts.
                    unseen_events = set(event.prev_event_ids()) - written_events
                    if unseen_events:
                        event_to_unseen_prevs[event.event_id] = unseen_events
                        for unseen in unseen_events:
                            unseen_to_child_events.setdefault(unseen, set()).add(
                                event.event_id
                            )

                    # Now check if this event is an unseen prev event, if so
                    # then we remove this event from the appropriate dicts.
                    for child_id in unseen_to_child_events.pop(event.event_id, []):
                        event_to_unseen_prevs[child_id].discard(event.event_id)

                    written_events.add(event.event_id)

                logger.info(
                    "Written %d events in room %s", len(written_events), room_id
                )

            # Extremities are the events who have at least one unseen prev event.
            extremities = (
                event_id
                for event_id, unseen_prevs in event_to_unseen_prevs.items()
                if unseen_prevs
            )
            for event_id in extremities:
                if not event_to_unseen_prevs[event_id]:
                    continue
                state = yield self.state_store.get_state_for_event(event_id)
                writer.write_state(room_id, event_id, state)

        return writer.finished()


class ExfiltrationWriter(object):
    """Interface used to specify how to write exported data.
    """

    def write_events(self, room_id, events):
        """Write a batch of events for a room.

        Args:
            room_id (str)
            events (list[FrozenEvent])
        """
        pass

    def write_state(self, room_id, event_id, state):
        """Write the state at the given event in the room.

        This only gets called for backward extremities rather than for each
        event.

        Args:
            room_id (str)
            event_id (str)
            state (dict[tuple[str, str], FrozenEvent])
        """
        pass

    def write_invite(self, room_id, event, state):
        """Write an invite for the room, with associated invite state.

        Args:
            room_id (str)
            event (FrozenEvent)
            state (dict[tuple[str, str], dict]): A subset of the state at the
                invite, with a subset of the event keys (type, state_key
                content and sender)
        """

    def finished(self):
        """Called when all data has succesfully been exported and written.

        This functions return value is passed to the caller of
        `export_user_data`.
        """
        pass
