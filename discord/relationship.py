# -*- coding: utf-8 -*-

"""
The MIT License (MIT)

Copyright (c) 2015-present Rapptz

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
"""

from .enums import RelationshipAction, RelationshipType, try_enum


class Relationship:
    """Represents a relationship in Discord.

    A relationship is like a friendship, a person who is blocked, etc.

    Attributes
    -----------
    nickname: :class:`str`
        The user's friend nickname (if applicable).
    user: :class:`User`
        The user you have the relationship with.
    type: :class:`RelationshipType`
        The type of relationship you have.
    """

    __slots__ = ('nickname', 'type', 'user', '_state')

    def __init__(self, *, state, data):
        self._state = state
        self.type = try_enum(RelationshipType, data['type'])
        self.user = state.store_user(data['user'])
        self.nickname = data.get('nickname', None)

    def __repr__(self):
        return '<Relationship user={0.user!r} type={0.type!r}>'.format(self)

    async def delete(self):
        """|coro|

        Deletes the relationship.

        Raises
        ------
        HTTPException
            Deleting the relationship failed.
        """

        if self.type is RelationshipType.friend:
            await self._state.http.remove_relationship(self.user.id, action=RelationshipAction.unfriend)
        elif self.type is RelationshipType.blocked:
            await self._state.http.remove_relationship(self.user.id, action=RelationshipAction.unblock)
        elif self.type is RelationshipType.incoming_request:
            await self._state.http.remove_relationship(self.user.id, action=RelationshipAction.deny_request)
        elif self.type is RelationshipType.outgoing_request:
            await self._state.http.remove_relationship(self.user.id, action=RelationshipAction.remove_pending_request)

    async def accept(self):
        """|coro|

        Accepts the relationship request. Only applicable for
        type :class:`RelationshipType.incoming_request`.

        Raises
        -------
        HTTPException
            Accepting the relationship failed.
        """

        await self._state.http.add_relationship(self.user.id, action=RelationshipAction.accept_request)

    def change_nickname(self, nick):
        """|coro|

        Changes a relationship's nickname. Only applicable for
        type :class:`RelationshipType.friend`.

        Parameters
        ----------
        nick: :class:`str`
            The nickname to change to.

        Raises
        -------
        HTTPException
            Changing the nickname failed.

        .. versionadded:: 1.9
        """

        return self._state.http.change_friend_nickname(self.user.id, nick)
        self.nickname = nick
