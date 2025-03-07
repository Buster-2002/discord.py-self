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

import asyncio

import discord.abc

from . import utils
from .asset import Asset
from .enums import ChannelType, VoiceRegion, try_enum
from .errors import ClientException, InvalidArgument, NoMoreItems, NotFound
from .mixins import Hashable
from .permissions import Permissions

__all__ = (
    'TextChannel',
    'VoiceChannel',
    'StageChannel',
    'DMChannel',
    'CategoryChannel',
    'StoreChannel',
    'GroupChannel',
    '_channel_factory',
)

async def _delete_messages(state, channel_id, messages):
    delete_message = state.http.delete_message
    for msg in messages:
        try:
            await delete_message(channel_id, msg.id)
        except NotFound:
            pass

class TextChannel(discord.abc.Messageable, discord.abc.GuildChannel, Hashable):
    """Represents a Discord guild text channel.

    .. container:: operations

        .. describe:: x == y

            Checks if two channels are equal.

        .. describe:: x != y

            Checks if two channels are not equal.

        .. describe:: hash(x)

            Returns the channel's hash.

        .. describe:: str(x)

            Returns the channel's name.

    Attributes
    -----------
    name: :class:`str`
        The channel name.
    guild: :class:`Guild`
        The guild the channel belongs to.
    id: :class:`int`
        The channel ID.
    category_id: Optional[:class:`int`]
        The category channel ID this channel belongs to, if applicable.
    topic: Optional[:class:`str`]
        The channel's topic. ``None`` if it doesn't exist.
    position: :class:`int`
        The position in the channel list. This is a number that starts at 0. e.g. the
        top channel is position 0.
    last_message_id: Optional[:class:`int`]
        The last message ID of the message sent to this channel. It may
        *not* point to an existing or valid message.
    slowmode_delay: :class:`int`
        The number of seconds a member must wait between sending messages
        in this channel. A value of `0` denotes that it is disabled.
        Bots and users with :attr:`~Permissions.manage_channels` or
        :attr:`~Permissions.manage_messages` bypass slowmode.
    """

    __slots__ = ('name', 'id', 'guild', 'topic', '_state', 'nsfw',
                 'category_id', 'position', 'slowmode_delay', '_overwrites',
                 '_type', 'last_message_id')

    def __init__(self, *, state, guild, data):
        self._state = state
        self.id = int(data['id'])
        self._type = data['type']
        self._update(guild, data)

    def __repr__(self):
        attrs = [
            ('id', self.id),
            ('name', self.name),
            ('position', self.position),
            ('nsfw', self.nsfw),
            ('news', self.is_news()),
            ('category_id', self.category_id)
        ]
        return '<%s %s>' % (self.__class__.__name__, ' '.join('%s=%r' % t for t in attrs))

    def _update(self, guild, data):
        self.guild = guild
        self.name = data['name']
        self.category_id = utils._get_as_snowflake(data, 'parent_id')
        self.topic = data.get('topic')
        self.position = data['position']
        self.nsfw = data.get('nsfw', False)
        # Does this need coercion into `int`? No idea yet.
        self.slowmode_delay = data.get('rate_limit_per_user', 0)
        self._type = data.get('type', self._type)
        self.last_message_id = utils._get_as_snowflake(data, 'last_message_id')
        self._fill_overwrites(data)

    async def _get_channel(self):
        return self

    @property
    def type(self):
        """:class:`ChannelType`: The channel's Discord type."""
        return try_enum(ChannelType, self._type)

    @property
    def _sorting_bucket(self):
        return ChannelType.text.value

    @utils.copy_doc(discord.abc.GuildChannel.permissions_for)
    def permissions_for(self, member):
        base = super().permissions_for(member)

        # text channels do not have voice related permissions
        denied = Permissions.voice()
        base.value &= ~denied.value
        return base

    @property
    def members(self):
        """List[:class:`Member`]: Returns all members that can see this channel."""
        return [m for m in self.guild.members if self.permissions_for(m).read_messages]

    def is_nsfw(self):
        """:class:`bool`: Checks if the channel is NSFW."""
        return self.nsfw

    def is_news(self):
        """:class:`bool`: Checks if the channel is a news channel."""
        return self._type == ChannelType.news.value

    @property
    def last_message(self):
        """Fetches the last message from this channel in cache.

        The message might not be valid or point to an existing message.

        .. admonition:: Reliable Fetching
            :class: helpful

            For a slightly more reliable method of fetching the
            last message, consider using either :meth:`history`
            or :meth:`fetch_message` with the :attr:`last_message_id`
            attribute.

        Returns
        ---------
        Optional[:class:`Message`]
            The last message in this channel or ``None`` if not found.
        """
        return self._state._get_message(self.last_message_id) if self.last_message_id else None

    async def edit(self, **options):
        """|coro|

        Edits the channel.

        You must have the :attr:`~Permissions.manage_channels` permission to
        use this.

        .. versionchanged:: 1.3
            The ``overwrites`` keyword-only parameter was added.

        .. versionchanged:: 1.4
            The ``type`` keyword-only parameter was added.

        Parameters
        ----------
        name: :class:`str`
            The new channel name.
        topic: :class:`str`
            The new channel's topic.
        position: :class:`int`
            The new channel's position.
        nsfw: :class:`bool`
            To mark the channel as NSFW or not.
        sync_permissions: :class:`bool`
            Whether to sync permissions with the channel's new or pre-existing
            category. Defaults to ``False``.
        category: Optional[:class:`CategoryChannel`]
            The new category for this channel. Can be ``None`` to remove the
            category.
        slowmode_delay: :class:`int`
            Specifies the slowmode rate limit for user in this channel, in seconds.
            A value of `0` disables slowmode. The maximum value possible is `21600`.
        type: :class:`ChannelType`
            Change the type of this text channel. Currently, only conversion between
            :attr:`ChannelType.text` and :attr:`ChannelType.news` is supported. This
            is only available to guilds that contain ``NEWS`` in :attr:`Guild.features`.
        overwrites: :class:`dict`
            A :class:`dict` of target (either a role or a member) to
            :class:`PermissionOverwrite` to apply to the channel.

        Raises
        ------
        InvalidArgument
            If position is less than 0 or greater than the number of channels, or if
            the permission overwrite information is not in proper form.
        Forbidden
            You do not have permissions to edit the channel.
        HTTPException
            Editing the channel failed.
        """
        await self._edit(options)

    @utils.copy_doc(discord.abc.GuildChannel.clone)
    async def clone(self, *, name=None):
        return await self._clone_impl({
            'topic': self.topic,
            'nsfw': self.nsfw,
            'rate_limit_per_user': self.slowmode_delay
        }, name=name)

    async def delete_messages(self, messages):
        """|coro|

        Deletes a list of messages. This is similar to :meth:`Message.delete`
        except it bulk deletes multiple messages.

        You must have the :attr:`~Permissions.manage_messages` permission to
        use this (unless they're your own).

        .. note::
            Users do not have access to the message bulk-delete endpoint.
            Since messages are just iterated over and deleted one-by-one,
            it's easy to get ratelimited using this method.

        Parameters
        -----------
        messages: Iterable[:class:`abc.Snowflake`]
            An iterable of messages denoting which ones to bulk delete.

        Raises
        ------
        Forbidden
            You do not have proper permissions to delete the messages.
        HTTPException
            Deleting the messages failed.
        """
        if not isinstance(messages, (list, tuple)):
            messages = list(messages)

        if len(messages) == 0:
            return # do nothing

        await _delete_messages(self._state, self.id, messages)

    async def purge(self, *, limit=100, check=None, before=None, after=None, around=None, oldest_first=False):
        """|coro|

        Purges a list of messages that meet the criteria given by the predicate
        ``check``. If a ``check`` is not provided then all messages are deleted
        without discrimination.

        The :attr:`~Permissions.read_message_history` permission is needed to
        retrieve message history.

        Examples
        ---------

        Deleting bot's messages ::

            def is_me(m):
                return m.author == client.user

            deleted = await channel.purge(limit=100, check=is_me)
            await channel.send('Deleted {} message(s)'.format(len(deleted)))

        Parameters
        -----------
        limit: Optional[:class:`int`]
            The number of messages to search through. This is not the number
            of messages that will be deleted, though it can be.
        check: Callable[[:class:`Message`], :class:`bool`]
            The function used to check if a message should be deleted.
            It must take a :class:`Message` as its sole parameter.
        before: Optional[Union[:class:`abc.Snowflake`, :class:`datetime.datetime`]]
            Same as ``before`` in :meth:`history`.
        after: Optional[Union[:class:`abc.Snowflake`, :class:`datetime.datetime`]]
            Same as ``after`` in :meth:`history`.
        around: Optional[Union[:class:`abc.Snowflake`, :class:`datetime.datetime`]]
            Same as ``around`` in :meth:`history`.
        oldest_first: Optional[:class:`bool`]
            Same as ``oldest_first`` in :meth:`history`.

        Raises
        -------
        Forbidden
            You do not have proper permissions to do the actions required.
        HTTPException
            Purging the messages failed.

        Returns
        --------
        List[:class:`.Message`]
            The list of messages that were deleted.
        """

        if check is None:
            check = lambda m: True

        state = self._state
        channel_id = self.id
        iterator = self.history(limit=limit, before=before, after=after, oldest_first=oldest_first, around=around)
        ret = []
        count = 0

        while True:
            try:
                msg = await iterator.next()
            except NoMoreItems:
                to_delete = ret[-count:]
                await _delete_messages(state, channel_id, to_delete)
                return ret
            else:
                if count == 100:
                    # we've reached a full 'queue'
                    to_delete = ret[-100:]
                    await _delete_messages(state, to_delete)
                    count = 0
                    await asyncio.sleep(1)

                if check(msg):
                    count += 1
                    ret.append(msg)

    async def webhooks(self):
        """|coro|

        Gets the list of webhooks from this channel.

        Requires :attr:`~.Permissions.manage_webhooks` permissions.

        Raises
        -------
        Forbidden
            You don't have permissions to get the webhooks.

        Returns
        --------
        List[:class:`Webhook`]
            The webhooks for this channel.
        """

        from .webhook import Webhook
        data = await self._state.http.channel_webhooks(self.id)
        return [Webhook.from_state(d, state=self._state) for d in data]

    async def create_webhook(self, *, name, avatar=None):
        """|coro|

        Creates a webhook for this channel.

        Requires :attr:`~.Permissions.manage_webhooks` permissions.

        Parameters
        -------------
        name: :class:`str`
            The webhook's name.
        avatar: Optional[:class:`bytes`]
            A :term:`py:bytes-like object` representing the webhook's default avatar.
            This operates similarly to :meth:`~ClientUser.edit`.

        Raises
        -------
        HTTPException
            Creating the webhook failed.
        Forbidden
            You do not have permissions to create a webhook.

        Returns
        --------
        :class:`Webhook`
            The created webhook.
        """

        from .webhook import Webhook
        if avatar is not None:
            avatar = utils._bytes_to_base64_data(avatar)

        data = await self._state.http.create_webhook(self.id, name=str(name), avatar=avatar)
        return Webhook.from_state(data, state=self._state)

    async def follow(self, *, destination):
        """
        Follows a channel using a webhook.

        Only news channels can be followed.

        .. note::

            The webhook returned will not provide a token to do webhook
            actions, as Discord does not provide it.

        .. versionadded:: 1.3

        Parameters
        -----------
        destination: :class:`TextChannel`
            The channel you would like to follow from.

        Raises
        -------
        HTTPException
            Following the channel failed.
        Forbidden
            You do not have the permissions to create a webhook.

        Returns
        --------
        :class:`Webhook`
            The created webhook.
        """

        if not self.is_news():
            raise ClientException('The channel must be a news channel.')

        if not isinstance(destination, TextChannel):
            raise InvalidArgument('Expected TextChannel received {0.__name__}'.format(type(destination)))

        from .webhook import Webhook
        data = await self._state.http.follow_webhook(self.id, webhook_channel_id=destination.id)
        return Webhook._as_follower(data, channel=destination, user=self._state.user)

    def get_partial_message(self, message_id):
        """Creates a :class:`PartialMessage` from the message ID.

        This is useful if you want to work with a message and only have its ID without
        doing an unnecessary API call.

        .. versionadded:: 1.6

        Parameters
        ------------
        message_id: :class:`int`
            The message ID to create a partial message for.

        Returns
        ---------
        :class:`PartialMessage`
            The partial message.
        """

        from .message import PartialMessage
        return PartialMessage(channel=self, id=message_id)

class VocalGuildChannel(discord.abc.Connectable, discord.abc.GuildChannel, Hashable):
    __slots__ = ('name', 'id', 'guild', 'bitrate', 'user_limit',
                 '_state', 'position', '_overwrites', 'category_id',
                 'rtc_region')

    def __init__(self, *, state, guild, data):
        self._state = state
        self.id = int(data['id'])
        self._update(guild, data)

    def _get_voice_client_key(self):
        return self.guild.id, 'guild_id'

    def _get_voice_state_pair(self):
        return self.guild.id, self.id

    def _update(self, guild, data):
        self.guild = guild
        self.name = data['name']
        self.rtc_region = data.get('rtc_region')
        if self.rtc_region:
            self.rtc_region = try_enum(VoiceRegion, self.rtc_region)
        self.category_id = utils._get_as_snowflake(data, 'parent_id')
        self.position = data['position']
        self.bitrate = data.get('bitrate')
        self.user_limit = data.get('user_limit')
        self._fill_overwrites(data)

    @property
    def _sorting_bucket(self):
        return ChannelType.voice.value

    @property
    def members(self):
        """List[:class:`Member`]: Returns all members that are currently inside this voice channel."""
        ret = []
        for user_id, state in self.guild._voice_states.items():
            if state.channel and state.channel.id == self.id:
                member = self.guild.get_member(user_id)
                if member is not None:
                    ret.append(member)
        return ret

    @property
    def voice_states(self):
        """Returns a mapping of member IDs who have voice states in this channel.

        .. versionadded:: 1.3

        .. note::

            This function is intentionally low level to replace :attr:`members`
            when the member cache is unavailable.

        Returns
        --------
        Mapping[:class:`int`, :class:`VoiceState`]
            The mapping of member ID to a voice state.
        """
        return {key: value for key, value in self.guild._voice_states.items() if value.channel.id == self.id}

    @utils.copy_doc(discord.abc.GuildChannel.permissions_for)
    def permissions_for(self, member):
        base = super().permissions_for(member)

        # voice channels cannot be edited by people who can't connect to them
        # It also implicitly denies all other voice perms
        if not base.connect:
            denied = Permissions.voice()
            denied.update(manage_channels=True, manage_roles=True)
            base.value &= ~denied.value
        return base

class VoiceChannel(VocalGuildChannel):
    """Represents a Discord guild voice channel.

    .. container:: operations

        .. describe:: x == y

            Checks if two channels are equal.

        .. describe:: x != y

            Checks if two channels are not equal.

        .. describe:: hash(x)

            Returns the channel's hash.

        .. describe:: str(x)

            Returns the channel's name.

    Attributes
    -----------
    name: :class:`str`
        The channel name.
    guild: :class:`Guild`
        The guild the channel belongs to.
    id: :class:`int`
        The channel ID.
    category_id: Optional[:class:`int`]
        The category channel ID this channel belongs to, if applicable.
    position: :class:`int`
        The position in the channel list. This is a number that starts at 0. e.g. the
        top channel is position 0.
    bitrate: :class:`int`
        The channel's preferred audio bitrate in bits per second.
    user_limit: :class:`int`
        The channel's limit for number of members that can be in a voice channel.
    rtc_region: Optional[:class:`VoiceRegion`]
        The region for the voice channel's voice communication.
        A value of ``None`` indicates automatic voice region detection.

        .. versionadded:: 1.7
    """

    __slots__ = ()

    def __repr__(self):
        attrs = [
            ('id', self.id),
            ('name', self.name),
            ('rtc_region', self.rtc_region),
            ('position', self.position),
            ('bitrate', self.bitrate),
            ('user_limit', self.user_limit),
            ('category_id', self.category_id)
        ]
        return '<%s %s>' % (self.__class__.__name__, ' '.join('%s=%r' % t for t in attrs))

    @property
    def type(self):
        """:class:`ChannelType`: The channel's Discord type."""
        return ChannelType.voice

    @utils.copy_doc(discord.abc.GuildChannel.clone)
    async def clone(self, *, name=None):
        return await self._clone_impl({
            'bitrate': self.bitrate,
            'user_limit': self.user_limit
        }, name=name)

    async def edit(self, **options):
        """|coro|

        Edits the channel.

        You must have the :attr:`~Permissions.manage_channels` permission to
        use this.

        .. versionchanged:: 1.3
            The ``overwrites`` keyword-only parameter was added.

        Parameters
        ----------
        name: :class:`str`
            The new channel's name.
        bitrate: :class:`int`
            The new channel's bitrate.
        user_limit: :class:`int`
            The new channel's user limit.
        position: :class:`int`
            The new channel's position.
        sync_permissions: :class:`bool`
            Whether to sync permissions with the channel's new or pre-existing
            category. Defaults to ``False``.
        category: Optional[:class:`CategoryChannel`]
            The new category for this channel. Can be ``None`` to remove the
            category.
        overwrites: :class:`dict`
            A :class:`dict` of target (either a role or a member) to
            :class:`PermissionOverwrite` to apply to the channel.
        rtc_region: Optional[:class:`VoiceRegion`]
            The new region for the voice channel's voice communication.
            A value of ``None`` indicates automatic voice region detection.

            .. versionadded:: 1.7

        Raises
        ------
        InvalidArgument
            If the permission overwrite information is not in proper form.
        Forbidden
            You do not have permissions to edit the channel.
        HTTPException
            Editing the channel failed.
        """

        await self._edit(options)

class StageChannel(VocalGuildChannel):
    """Represents a Discord guild stage channel.

    .. versionadded:: 1.7

    .. container:: operations

        .. describe:: x == y

            Checks if two channels are equal.

        .. describe:: x != y

            Checks if two channels are not equal.

        .. describe:: hash(x)

            Returns the channel's hash.

        .. describe:: str(x)

            Returns the channel's name.

    Attributes
    -----------
    name: :class:`str`
        The channel name.
    guild: :class:`Guild`
        The guild the channel belongs to.
    id: :class:`int`
        The channel ID.
    topic: Optional[:class:`str`]
        The channel's topic. ``None`` if it isn't set.
    category_id: Optional[:class:`int`]
        The category channel ID this channel belongs to, if applicable.
    position: :class:`int`
        The position in the channel list. This is a number that starts at 0. e.g. the
        top channel is position 0.
    bitrate: :class:`int`
        The channel's preferred audio bitrate in bits per second.
    user_limit: :class:`int`
        The channel's limit for number of members that can be in a stage channel.
    rtc_region: Optional[:class:`VoiceRegion`]
        The region for the stage channel's voice communication.
        A value of ``None`` indicates automatic voice region detection.
    """
    __slots__ = ('topic',)

    def __repr__(self):
        attrs = [
            ('id', self.id),
            ('name', self.name),
            ('topic', self.topic),
            ('rtc_region', self.rtc_region),
            ('position', self.position),
            ('bitrate', self.bitrate),
            ('user_limit', self.user_limit),
            ('category_id', self.category_id)
        ]
        return '<%s %s>' % (self.__class__.__name__, ' '.join('%s=%r' % t for t in attrs))

    def _update(self, guild, data):
        super()._update(guild, data)
        self.topic = data.get('topic')

    @property
    def requesting_to_speak(self):
        """List[:class:`Member`]: A list of members who are requesting to speak in the stage channel."""
        return [member for member in self.members if member.voice.requested_to_speak_at is not None]

    @property
    def type(self):
        """:class:`ChannelType`: The channel's Discord type."""
        return ChannelType.stage_voice

    @utils.copy_doc(discord.abc.GuildChannel.clone)
    async def clone(self, *, name=None):
        return await self._clone_impl({
            'topic': self.topic,
        }, name=name)

    async def edit(self, **options):
        """|coro|

        Edits the channel.

        You must have the :attr:`~Permissions.manage_channels` permission to
        use this.

        Parameters
        ----------
        name: :class:`str`
            The new channel's name.
        topic: :class:`str`
            The new channel's topic.
        position: :class:`int`
            The new channel's position.
        sync_permissions: :class:`bool`
            Whether to sync permissions with the channel's new or pre-existing
            category. Defaults to ``False``.
        category: Optional[:class:`CategoryChannel`]
            The new category for this channel. Can be ``None`` to remove the
            category.
        overwrites: :class:`dict`
            A :class:`dict` of target (either a role or a member) to
            :class:`PermissionOverwrite` to apply to the channel.
        rtc_region: Optional[:class:`VoiceRegion`]
            The new region for the stage channel's voice communication.
            A value of ``None`` indicates automatic voice region detection.

        Raises
        ------
        InvalidArgument
            If the permission overwrite information is not in proper form.
        Forbidden
            You do not have permissions to edit the channel.
        HTTPException
            Editing the channel failed.
        """

        await self._edit(options)

class CategoryChannel(discord.abc.GuildChannel, Hashable):
    """Represents a Discord channel category.

    These are useful to group channels to logical compartments.

    .. container:: operations

        .. describe:: x == y

            Checks if two channels are equal.

        .. describe:: x != y

            Checks if two channels are not equal.

        .. describe:: hash(x)

            Returns the category's hash.

        .. describe:: str(x)

            Returns the category's name.

    Attributes
    -----------
    name: :class:`str`
        The category name.
    guild: :class:`Guild`
        The guild the category belongs to.
    id: :class:`int`
        The category channel ID.
    position: :class:`int`
        The position in the category list. This is a number that starts at 0. e.g. the
        top category is position 0.
    """

    __slots__ = ('name', 'id', 'guild', 'nsfw', '_state', 'position', '_overwrites', 'category_id')

    def __init__(self, *, state, guild, data):
        self._state = state
        self.id = int(data['id'])
        self._update(guild, data)

    def __repr__(self):
        return '<CategoryChannel id={0.id} name={0.name!r} position={0.position} nsfw={0.nsfw}>'.format(self)

    def _update(self, guild, data):
        self.guild = guild
        self.name = data['name']
        self.category_id = utils._get_as_snowflake(data, 'parent_id')
        self.nsfw = data.get('nsfw', False)
        self.position = data['position']
        self._fill_overwrites(data)

    @property
    def _sorting_bucket(self):
        return ChannelType.category.value

    @property
    def type(self):
        """:class:`ChannelType`: The channel's Discord type."""
        return ChannelType.category

    def is_nsfw(self):
        """:class:`bool`: Checks if the category is NSFW."""
        return self.nsfw

    @utils.copy_doc(discord.abc.GuildChannel.clone)
    async def clone(self, *, name=None):
        return await self._clone_impl({
            'nsfw': self.nsfw
        }, name=name)

    async def edit(self, **options):
        """|coro|

        Edits the channel.

        You must have the :attr:`~Permissions.manage_channels` permission to
        use this.

        .. versionchanged:: 1.3
            The ``overwrites`` keyword-only parameter was added.

        Parameters
        ----------
        name: :class:`str`
            The new category's name.
        position: :class:`int`
            The new category's position.
        nsfw: :class:`bool`
            To mark the category as NSFW or not.
        overwrites: :class:`dict`
            A :class:`dict` of target (either a role or a member) to
            :class:`PermissionOverwrite` to apply to the channel.

        Raises
        ------
        InvalidArgument
            If position is less than 0 or greater than the number of categories.
        Forbidden
            You do not have permissions to edit the category.
        HTTPException
            Editing the category failed.
        """

        await self._edit(options=options)

    @utils.copy_doc(discord.abc.GuildChannel.move)
    async def move(self, **kwargs):
        kwargs.pop('category', None)
        await super().move(**kwargs)

    @property
    def channels(self):
        """List[:class:`abc.GuildChannel`]: Returns the channels that are under this category.

        These are sorted by the official Discord UI, which places voice channels below the text channels.
        """
        def comparator(channel):
            return (not isinstance(channel, TextChannel), channel.position)

        ret = [c for c in self.guild.channels if c.category_id == self.id]
        ret.sort(key=comparator)
        return ret

    @property
    def text_channels(self):
        """List[:class:`TextChannel`]: Returns the text channels that are under this category."""
        ret = [c for c in self.guild.channels
            if c.category_id == self.id
            and isinstance(c, TextChannel)]
        ret.sort(key=lambda c: (c.position, c.id))
        return ret

    @property
    def voice_channels(self):
        """List[:class:`VoiceChannel`]: Returns the voice channels that are under this category."""
        ret = [c for c in self.guild.channels
            if c.category_id == self.id
            and isinstance(c, VoiceChannel)]
        ret.sort(key=lambda c: (c.position, c.id))
        return ret

    @property
    def stage_channels(self):
        """List[:class:`StageChannel`]: Returns the voice channels that are under this category.

        .. versionadded:: 1.7
        """
        ret = [c for c in self.guild.channels
            if c.category_id == self.id
            and isinstance(c, StageChannel)]
        ret.sort(key=lambda c: (c.position, c.id))
        return ret

    async def create_text_channel(self, name, *, overwrites=None, **options):
        """|coro|

        A shortcut method to :meth:`Guild.create_text_channel` to create a :class:`TextChannel` in the category.

        Returns
        -------
        :class:`TextChannel`
            The channel that was just created.
        """
        return await self.guild.create_text_channel(name, overwrites=overwrites, category=self, **options)

    async def create_voice_channel(self, name, *, overwrites=None, **options):
        """|coro|

        A shortcut method to :meth:`Guild.create_voice_channel` to create a :class:`VoiceChannel` in the category.

        Returns
        -------
        :class:`VoiceChannel`
            The channel that was just created.
        """
        return await self.guild.create_voice_channel(name, overwrites=overwrites, category=self, **options)

    async def create_stage_channel(self, name, *, overwrites=None, **options):
        """|coro|

        A shortcut method to :meth:`Guild.create_stage_channel` to create a :class:`StageChannel` in the category.

        .. versionadded:: 1.7

        Returns
        -------
        :class:`StageChannel`
            The channel that was just created.
        """
        return await self.guild.create_stage_channel(name, overwrites=overwrites, category=self, **options)

class StoreChannel(discord.abc.GuildChannel, Hashable):
    """Represents a Discord guild store channel.

    .. container:: operations

        .. describe:: x == y

            Checks if two channels are equal.

        .. describe:: x != y

            Checks if two channels are not equal.

        .. describe:: hash(x)

            Returns the channel's hash.

        .. describe:: str(x)

            Returns the channel's name.

    Attributes
    -----------
    name: :class:`str`
        The channel name.
    guild: :class:`Guild`
        The guild the channel belongs to.
    id: :class:`int`
        The channel ID.
    category_id: :class:`int`
        The category channel ID this channel belongs to.
    position: :class:`int`
        The position in the channel list. This is a number that starts at 0. e.g. the
        top channel is position 0.
    """
    __slots__ = ('name', 'id', 'guild', '_state', 'nsfw',
                 'category_id', 'position', '_overwrites',)

    def __init__(self, *, state, guild, data):
        self._state = state
        self.id = int(data['id'])
        self._update(guild, data)

    def __repr__(self):
        return '<StoreChannel id={0.id} name={0.name!r} position={0.position} nsfw={0.nsfw}>'.format(self)

    def _update(self, guild, data):
        self.guild = guild
        self.name = data['name']
        self.category_id = utils._get_as_snowflake(data, 'parent_id')
        self.position = data['position']
        self.nsfw = data.get('nsfw', False)
        self._fill_overwrites(data)

    @property
    def _sorting_bucket(self):
        return ChannelType.text.value

    @property
    def type(self):
        """:class:`ChannelType`: The channel's Discord type."""
        return ChannelType.store

    @utils.copy_doc(discord.abc.GuildChannel.permissions_for)
    def permissions_for(self, member):
        base = super().permissions_for(member)

        # store channels do not have voice related permissions
        denied = Permissions.voice()
        base.value &= ~denied.value
        return base

    def is_nsfw(self):
        """:class:`bool`: Checks if the channel is NSFW."""
        return self.nsfw

    @utils.copy_doc(discord.abc.GuildChannel.clone)
    async def clone(self, *, name=None):
        return await self._clone_impl({
            'nsfw': self.nsfw
        }, name=name)

    async def edit(self, **options):
        """|coro|

        Edits the channel.

        You must have the :attr:`~Permissions.manage_channels` permission to
        use this.

        Parameters
        ----------
        name: :class:`str`
            The new channel name.
        position: :class:`int`
            The new channel's position.
        nsfw: :class:`bool`
            To mark the channel as NSFW or not.
        sync_permissions: :class:`bool`
            Whether to sync permissions with the channel's new or pre-existing
            category. Defaults to ``False``.
        category: Optional[:class:`CategoryChannel`]
            The new category for this channel. Can be ``None`` to remove the
            category.
        overwrites: :class:`dict`
            A :class:`dict` of target (either a role or a member) to
            :class:`PermissionOverwrite` to apply to the channel.

            .. versionadded:: 1.3

        Raises
        ------
        InvalidArgument
            If position is less than 0 or greater than the number of channels, or if
            the permission overwrite information is not in proper form.
        Forbidden
            You do not have permissions to edit the channel.
        HTTPException
            Editing the channel failed.
        """
        await self._edit(options)

class DMChannel(discord.abc.Messageable, Hashable):
    """Represents a Discord direct message channel.

    .. container:: operations

        .. describe:: x == y

            Checks if two channels are equal.

        .. describe:: x != y

            Checks if two channels are not equal.

        .. describe:: hash(x)

            Returns the channel's hash.

        .. describe:: str(x)

            Returns a string representation of the channel

    Attributes
    ----------
    recipient: :class:`User`
        The user you are participating with in the direct message channel.
    me: :class:`ClientUser`
        The user presenting yourself.
    id: :class:`int`
        The direct message channel ID.
    """

    __slots__ = ('id', 'recipient', 'me', '_state')

    def __init__(self, *, me, state, data):
        self._state = state
        self.recipient = state.store_user(data['recipients'][0])
        self.me = me
        self.id = int(data['id'])

    async def _get_channel(self):
        return self

    def __str__(self):
        return 'Direct Message with %s' % self.recipient

    def __repr__(self):
        return '<DMChannel id={0.id} recipient={0.recipient!r}>'.format(self)

    @property
    def type(self):
        """:class:`ChannelType`: The channel's Discord type."""
        return ChannelType.private

    @property
    def created_at(self):
        """:class:`datetime.datetime`: Returns the direct message channel's creation time in UTC."""
        return utils.snowflake_time(self.id)

    def permissions_for(self, user=None):
        """Handles permission resolution for a :class:`User`.

        This function is there for compatibility with other channel types.

        Actual direct messages do not really have the concept of permissions.

        This returns all the Text related permissions set to ``True`` except:

        - :attr:`~Permissions.send_tts_messages`: You cannot send TTS messages in a DM.
        - :attr:`~Permissions.manage_messages`: You cannot delete others messages in a DM.

        Parameters
        -----------
        user: :class:`User`
            The user to check permissions for. This parameter is ignored
            but kept for compatibility.

        Returns
        --------
        :class:`Permissions`
            The resolved permissions.
        """

        base = Permissions.text()
        base.read_messages = True
        base.send_tts_messages = False
        base.manage_messages = False
        return base

    def get_partial_message(self, message_id):
        """Creates a :class:`PartialMessage` from the message ID.

        This is useful if you want to work with a message and only have its ID without
        doing an unnecessary API call.

        .. versionadded:: 1.6

        Parameters
        ------------
        message_id: :class:`int`
            The message ID to create a partial message for.

        Returns
        ----------
        :class:`PartialMessage`
            The partial message.
        """

        from .message import PartialMessage
        return PartialMessage(channel=self, id=message_id)

    async def change_region(self, region):
        """|coro|

        Changes the channel's voice region.
        
        Parameters
        -----------
        region: :class:`VoiceRegion`
            A :class:`VoiceRegion` to change the voice region to.

        Raises
        -------
        HTTPException
            Failed to change the channel's voice region.
        """
        return await self._state.http.change_voice_region_in_private_channel(self.id, region.value)

class GroupChannel(discord.abc.Messageable, Hashable):
    """Represents a Discord group channel.

    .. container:: operations

        .. describe:: x == y

            Checks if two channels are equal.

        .. describe:: x != y

            Checks if two channels are not equal.

        .. describe:: hash(x)

            Returns the channel's hash.

        .. describe:: str(x)

            Returns a string representation of the channel

    Attributes
    ----------
    recipients: List[:class:`User`]
        The users you are participating with in the group channel.
    me: :class:`ClientUser`
        The user presenting yourself.
    id: :class:`int`
        The group channel ID.
    owner: :class:`User`
        The user that owns the group channel.
    icon: Optional[:class:`str`]
        The group channel's icon hash if provided.
    name: Optional[:class:`str`]
        The group channel's name if provided.
    """

    __slots__ = ('id', 'recipients', 'owner', 'icon', 'name', 'me', '_state')

    def __init__(self, *, me, state, data):
        self._state = state
        self.id = int(data['id'])
        self.me = me
        self._update_group(data)

    def _update_group(self, data):
        owner_id = utils._get_as_snowflake(data, 'owner_id')
        self.icon = data.get('icon')
        self.name = data.get('name')

        try:
            self.recipients = [self._state.store_user(u) for u in data['recipients']]
        except KeyError:
            pass

        if owner_id == self.me.id:
            self.owner = self.me
        else:
            self.owner = utils.find(lambda u: u.id == owner_id, self.recipients)

    async def _get_channel(self):
        return self

    def __str__(self):
        if self.name:
            return self.name

        if len(self.recipients) == 0:
            return 'Unnamed'

        return ', '.join(map(lambda x: x.name, self.recipients))

    def __repr__(self):
        return '<GroupChannel id={0.id} name={0.name!r}>'.format(self)

    @property
    def type(self):
        """:class:`ChannelType`: The channel's Discord type."""
        return ChannelType.group

    @property
    def icon_url(self):
        """:class:`Asset`: Returns the channel's icon asset if available.

        This is equivalent to calling :meth:`icon_url_as` with
        the default parameters ('webp' format and a size of 1024).
        """
        return self.icon_url_as()

    def icon_url_as(self, *, format='webp', size=1024):
        """Returns an :class:`Asset` for the icon the channel has.

        The format must be one of 'webp', 'jpeg', 'jpg' or 'png'.
        The size must be a power of 2 between 16 and 4096.

        .. versionadded:: 2.0

        Parameters
        -----------
        format: :class:`str`
            The format to attempt to convert the icon to. Defaults to 'webp'.
        size: :class:`int`
            The size of the image to display.

        Raises
        ------
        InvalidArgument
            Bad image format passed to ``format`` or invalid ``size``.

        Returns
        --------
        :class:`Asset`
            The resulting CDN asset.
        """
        return Asset._from_icon(self._state, self, 'channel', format=format, size=size)

    @property
    def created_at(self):
        """:class:`datetime.datetime`: Returns the channel's creation time in UTC."""
        return utils.snowflake_time(self.id)

    def permissions_for(self, user):
        """Handles permission resolution for a :class:`User`.

        This function is there for compatibility with other channel types.

        Actual direct messages do not really have the concept of permissions.

        This returns all the Text related permissions set to ``True`` except:

        - :attr:`~Permissions.send_tts_messages`: You cannot send TTS messages in a DM.
        - :attr:`~Permissions.manage_messages`: You cannot delete others messages in a DM.

        This also checks the kick_members permission if the user is the owner.

        Parameters
        -----------
        user: :class:`User`
            The user to check permissions for.

        Returns
        --------
        :class:`Permissions`
            The resolved permissions for the user.
        """

        base = Permissions.text()
        base.read_messages = True
        base.send_tts_messages = False
        base.manage_messages = False
        base.mention_everyone = True

        if user.id == self.owner.id:
            base.kick_members = True

        return base

    async def add_recipients(self, *recipients):
        r"""|coro|

        Adds recipients to this group.

        A group can only have a maximum of 10 members.
        Attempting to add more ends up in an exception. To
        add a recipient to the group, you must have a relationship
        with the user of type :attr:`RelationshipType.friend`.

        Parameters
        -----------
        \*recipients: :class:`User`
            An argument list of users to add to this group.

        Raises
        -------
        HTTPException
            Adding a recipient to this group failed.
        """

        # TODO: wait for the corresponding WS event

        req = self._state.http.add_group_recipient
        for recipient in recipients:
            await req(self.id, recipient.id)

    async def remove_recipients(self, *recipients):
        r"""|coro|

        Removes recipients from this group.

        Parameters
        -----------
        \*recipients: :class:`User`
            An argument list of users to remove from this group.

        Raises
        -------
        HTTPException
            Removing a recipient from this group failed.
        """

        # TODO: wait for the corresponding WS event

        req = self._state.http.remove_group_recipient
        for recipient in recipients:
            await req(self.id, recipient.id)

    async def edit(self, **fields):
        """|coro|

        Edits the group.

        Parameters
        -----------
        name: Optional[:class:`str`]
            The new name to change the group to.
            Could be ``None`` to remove the name.
        icon: Optional[:class:`bytes`]
            A :term:`py:bytes-like object` representing the new icon.
            Could be ``None`` to remove the icon.

        Raises
        -------
        HTTPException
            Editing the group failed.
        """

        try:
            icon_bytes = fields['icon']
        except KeyError:
            pass
        else:
            if icon_bytes is not None:
                fields['icon'] = utils._bytes_to_base64_data(icon_bytes)

        data = await self._state.http.edit_group(self.id, **fields)
        self._update_group(data)

    async def leave(self):
        """|coro|

        Leave the group.

        If you are the only one in the group, this deletes it as well.

        Raises
        -------
        HTTPException
            Leaving the group failed.
        """

        await self._state.http.leave_group(self.id)

    async def change_region(self, region):
        """|coro|

        Changes the channel's voice region.
        
        Parameters
        -----------
        region: :class:`VoiceRegion`
            A :class:`VoiceRegion` to change the voice region to.

        Raises
        -------
        HTTPException
            Failed to change the channel's voice region.
        """
        return await self._state.http.change_voice_region_in_private_channel(self.id, region.value)


def _channel_factory(channel_type):
    value = try_enum(ChannelType, channel_type)
    if value is ChannelType.text:
        return TextChannel, value
    elif value is ChannelType.voice:
        return VoiceChannel, value
    elif value is ChannelType.private:
        return DMChannel, value
    elif value is ChannelType.category:
        return CategoryChannel, value
    elif value is ChannelType.group:
        return GroupChannel, value
    elif value is ChannelType.news:
        return TextChannel, value
    elif value is ChannelType.store:
        return StoreChannel, value
    elif value is ChannelType.stage_voice:
        return StageChannel, value
    else:
        return None, value
