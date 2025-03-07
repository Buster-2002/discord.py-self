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
import copy
import datetime
import inspect
import logging
import os
from collections import OrderedDict, deque
from weakref import WeakValueDictionary

from . import utils
from .activity import BaseActivity
from .channel import *
from .emoji import Emoji
from .enums import (ChannelType, RequiredActionType, Status,
                    UnavailableGuildType, VoiceRegion, try_enum)
from .flags import GuildSubscriptionOptions, MemberCacheFlags
from .guild import Guild
from .invite import Invite
from .member import Member
from .mentions import AllowedMentions
from .message import Message
from .object import Object
from .partial_emoji import PartialEmoji
from .raw_models import *
from .relationship import Relationship
from .role import Role
from .settings import Settings
from .user import ClientUser, User


class ChunkRequest:
    def __init__(self, guild_id, loop, resolver, *, cache=True):
        self.guild_id = guild_id
        self.resolver = resolver
        self.loop = loop
        self.cache = cache
        self.nonce = os.urandom(16).hex()
        self.buffer = [] # List[Member]
        self.waiters = []

    def add_members(self, members):
        self.buffer.extend(members)
        if self.cache:
            guild = self.resolver(self.guild_id)
            if guild is None:
                return

            for member in members:
                existing = guild.get_member(member.id)
                if existing is None or existing.joined_at is None:
                    guild._add_member(member)

    async def wait(self):
        future = self.loop.create_future()
        self.waiters.append(future)
        try:
            return await future
        finally:
            self.waiters.remove(future)

    def get_future(self):
        future = self.loop.create_future()
        self.waiters.append(future)
        return future

    def done(self):
        for future in self.waiters:
            if not future.done():
                future.set_result(self.buffer)

log = logging.getLogger(__name__)

async def logging_coroutine(coroutine, *, info):
    try:
        await coroutine
    except Exception:
        log.exception('Exception occurred during %s', info)

class ConnectionState:
    def __init__(self, *, dispatch, handlers, hooks, http, loop, **options):
        self.loop = loop
        self.http = http
        self.max_messages = options.get('max_messages', 1000)
        if self.max_messages is not None and self.max_messages <= 0:
            self.max_messages = 1000

        self.dispatch = dispatch
        self.handlers = handlers
        self.hooks = hooks
        self._ready_task = None
        self.heartbeat_timeout = options.get('heartbeat_timeout', 60.0)

        allowed_mentions = options.get('allowed_mentions')

        if allowed_mentions is not None and not isinstance(allowed_mentions, AllowedMentions):
            raise TypeError('allowed_mentions parameter must be AllowedMentions')

        self.allowed_mentions = allowed_mentions
        self._chunk_requests = {} # Dict[Union[int, str], ChunkRequest]

        activity = options.get('activity', None)
        if activity:
            if not isinstance(activity, BaseActivity):
                raise TypeError('activity parameter must derive from BaseActivity.')

            activity = activity.to_dict()

        status = options.get('status', None)
        if status:
            if not isinstance(status, Status):
                raise TypeError('status must be Status not {0.__class__!r}'.format(status))
            if status is Status.offline:
                status = 'invisible'
            else:
                status = str(status)

        chunk_guilds = options.get('chunk_guilds_at_startup', True)
        self._chunk_guilds = chunk_guilds

        subscription_options = options.get('guild_subscription_options')
        if subscription_options is None:
            subscription_options = GuildSubscriptionOptions.default()
        else:
            if not isinstance(subscription_options, GuildSubscriptionOptions):
                raise TypeError('subscription_options parameter must be GuildSubscriptionOptions not %r' % type(subscription_options))
        self._subscription_options = subscription_options
        self._subscribe_guilds = subscription_options.auto_subscribe

        cache_flags = options.get('member_cache_flags')
        if cache_flags is None:
            cache_flags = MemberCacheFlags.all()
        else:
            if not isinstance(cache_flags, MemberCacheFlags):
                raise TypeError('member_cache_flags parameter must be MemberCacheFlags not %r' % type(cache_flags))

        self.member_cache_flags = cache_flags
        self._activity = activity
        self._status = status

        self.parsers = parsers = {}
        for attr, func in inspect.getmembers(self):
            if attr.startswith('parse_'):
                parsers[attr[6:].upper()] = func

        self.clear()

    def clear(self):
        self.user = None
        self._users = WeakValueDictionary()
        self._emojis = {}
        self._calls = {}
        self._guilds = {}
        self._unavailable_guilds = {}
        self._queued_guilds = {}
        self._voice_clients = {}

        # LRU of max size 128
        self._private_channels = OrderedDict()
        # extra dict to look up private channels by user id
        self._private_channels_by_user = {}
        self._messages = self.max_messages and deque(maxlen=self.max_messages)

    def process_chunk_requests(self, guild_id, nonce, members, complete):
        removed = []
        for key, request in self._chunk_requests.items():
            if request.guild_id == guild_id and request.nonce == nonce:
                request.add_members(members)
                if complete:
                    request.done()
                    removed.append(key)

        for key in removed:
            del self._chunk_requests[key]

    def call_handlers(self, key, *args, **kwargs):
        try:
            func = self.handlers[key]
        except KeyError:
            pass
        else:
            func(*args, **kwargs)

    async def call_hooks(self, key, *args, **kwargs):
        try:
            coro = self.hooks[key]
        except KeyError:
            pass
        else:
            await coro(*args, **kwargs)

    @property
    def self_id(self):
        u = self.user
        return u.id if u else None

    @property
    def voice_clients(self):
        return list(self._voice_clients.values())

    def _get_voice_client(self, guild_id):
        return self._voice_clients.get(guild_id)

    def _add_voice_client(self, guild_id, voice):
        self._voice_clients[guild_id] = voice

    def _remove_voice_client(self, guild_id):
        self._voice_clients.pop(guild_id, None)

    def _update_references(self, ws):
        for vc in self.voice_clients:
            vc.main_ws = ws

    def store_user(self, data):
        # This way is 300% faster than `dict.setdefault`.
        user_id = int(data['id'])
        try:
            user = self._users[user_id]
            # We use the data available to us since we
            # might not have events for that user.
            # However, the data may only have an ID.
            if user_id != self.self_id:
                try:
                    user._update(data)
                except KeyError:
                    pass
            return user
        except KeyError:
            user = User(state=self, data=data)
            if user.discriminator != '0000':
                self._users[user_id] = user
            return user

    def get_user(self, id):
        return self._users.get(id)

    def store_emoji(self, guild, data):
        emoji_id = int(data['id'])
        self._emojis[emoji_id] = emoji = Emoji(guild=guild, state=self, data=data)
        return emoji

    @property
    def guilds(self):
        return list(self._guilds.values())

    def _get_guild(self, guild_id):
        guild = self._guilds.get(guild_id)
        if guild is None:
            guild = self._queued_guilds.get(guild_id)
        return guild

    def _add_guild(self, guild):
        self._guilds[guild.id] = guild

    def _remove_guild(self, guild):
        self._guilds.pop(guild.id, None)

        for emoji in guild.emojis:
            self._emojis.pop(emoji.id, None)

        del guild

    @property
    def emojis(self):
        return list(self._emojis.values())

    def get_emoji(self, emoji_id):
        return self._emojis.get(emoji_id)

    @property
    def private_channels(self):
        return list(self._private_channels.values())

    def _get_private_channel(self, channel_id):
        try:
            value = self._private_channels[channel_id]
        except KeyError:
            return None
        else:
            self._private_channels.move_to_end(channel_id)
            return value

    def _get_private_channel_by_user(self, user_id):
        return self._private_channels_by_user.get(user_id)

    def _add_private_channel(self, channel):
        channel_id = channel.id
        self._private_channels[channel_id] = channel

        if isinstance(channel, DMChannel):
            self._private_channels_by_user[channel.recipient.id] = channel

    def add_dm_channel(self, data):
        channel = DMChannel(me=self.user, state=self, data=data)
        self._add_private_channel(channel)
        return channel

    def _remove_private_channel(self, channel):
        self._private_channels.pop(channel.id, None)
        if isinstance(channel, DMChannel):
            self._private_channels_by_user.pop(channel.recipient.id, None)

    def _get_message(self, msg_id):
        return utils.find(lambda m: m.id == msg_id, reversed(self._messages)) if self._messages else None

    def _add_guild_from_data(self, guild, *, from_ready=False):
        guild_id = int(guild['id'])
        unavailable = guild.get('unavailable', False)

        if not unavailable:
            guild = Guild(data=guild, state=self)
            self._add_guild(guild)
            return guild
        else:
            self._unavailable_guilds[guild_id] = UnavailableGuildType.existing if from_ready else UnavailableGuildType.joined
            log.debug('Forcing GUILD_CREATE for unavailable guild %s' % guild_id)
            asyncio.ensure_future(self.request_guild(guild_id), loop=self.loop)

    def _guild_needs_chunking(self, guild):
        try:
            return self._chunk_guilds and not guild.chunked and guild.me.guild_permissions.manage_guild
        except:
            return False

    def _guild_needs_subscribing(self, guild):
        return not guild.subscribed and self._subscribe_guilds

    def _get_guild_channel(self, data):
        channel_id = int(data['channel_id'])
        try:
            guild = self._get_guild(int(data['guild_id']))
        except KeyError:
            channel = self.get_channel(channel_id)
            guild = None
        else:
            channel = guild and guild.get_channel(channel_id)

        return channel or Object(id=channel_id), guild

    async def request_guild(self, guild_id):
        ws = self._get_websocket()
        await ws.request_lazy_guild(guild_id, typing=True, activities=True, threads=True)

    async def chunker(self, guild_id, query='', limit=0, presences=True, *, nonce=None):
        ws = self._get_websocket()
        await ws.request_chunks([guild_id], query=query, limit=limit, presences=presences, nonce=nonce)

    async def query_members(self, guild, query, limit, user_ids, cache, presences):
        guild_id = guild.id
        ws = self._get_websocket()

        request = ChunkRequest(guild.id, self.loop, self._get_guild, cache=cache)
        self._chunk_requests[request.nonce] = request

        try:
            await ws.request_chunks([guild_id], query=query, limit=limit, user_ids=user_ids, presences=presences, nonce=request.nonce)
            return await asyncio.wait_for(request.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            log.warning('Timed out waiting for chunks with query %r and limit %d for guild_id %d', query, limit, guild_id)
            raise

    async def _delay_ready(self):
        try:
            states = []
            subscribes = []
            for guild in self._guilds.values():
                await self.request_guild(guild.id)

                if self._guild_needs_chunking(guild):
                    future = await self.chunk_guild(guild, wait=False)
                    states.append((guild, future))

                if self._guild_needs_subscribing(guild):
                    subscribes.append(guild)

            for guild, future in states:
                try:
                    await asyncio.wait_for(future, timeout=5.0)
                except asyncio.TimeoutError:
                    log.warning('Bot timed out waiting for chunks for guild_id %s.', guild.id)

            options = self._subscription_options
            ticket = asyncio.Semaphore(options.concurrent_guilds)
            await asyncio.gather(*[guild.subscribe(ticket=ticket, max_online=options.max_online) for guild in subscribes])

        except asyncio.CancelledError:
            pass
        else:
            # Dispatch the event
            self.call_handlers('ready')
            self.dispatch('ready')
        finally:
            self._ready_task = None

    def parse_ready(self, data):
        # Before parsing, we wait for READY_SUPPLEMENTAL.
        # This has voice state objects, as well as an initial member cache.
        self._ready_data = data

    def parse_ready_supplemental(self, data):
        if self._ready_task is not None:
            self._ready_task.cancel()

        self.clear()

        # Merge with READY data
        extra_data = data
        data = self._ready_data

        # Discord bad
        for guild_data, guild_extra, merged_members, merged_me, merged_presences in zip(
            data.get('guilds', []),
            extra_data.get('guilds', []),
            extra_data.get('merged_members', []),
            data.get('merged_members', []),
            extra_data['merged_presences'].get('guilds', [])
        ):
            guild_data['voice_states'] = guild_extra.get('voice_states', [])
            guild_data['merged_members'] = merged_me
            guild_data['merged_members'].extend(merged_members)
            guild_data['merged_presences'] = merged_presences
            # There's also a friends key that has presence data for your friends.
            # Parsing that would require a redesign of the Relationship class ;-;.

        # Self parsing
        self.user = user = ClientUser(state=self, data=data['user'])
        self._users[user.id] = user

        # Temp user parsing
        temp_users = {user.id: user._to_minimal_user_json()}
        for u in data.get('users', []):
            u_id = int(u['id'])
            temp_users[u_id] = u

        # Guild parsing
        for guild_data in data.get('guilds', []):
            for member in guild_data['merged_members']:
                if 'user' not in member:
                    member['user'] = temp_users.get(int(member.pop('user_id')))
            self._add_guild_from_data(guild_data, from_ready=True)

        # Relationship parsing
        for relationship in data.get('relationships', []):
            try:
                r_id = int(relationship['id'])
            except KeyError:
                continue
            else:
                if 'user' not in relationship:
                    relationship['user'] = temp_users[int(relationship.pop('user_id'))]
                user._relationships[r_id] = Relationship(state=self, data=relationship)

        # Private channel parsing
        for pm in data.get('private_channels', []):
            factory, _ = _channel_factory(pm['type'])
            if 'recipients' not in pm:
                pm['recipients'] = [temp_users[int(u_id)] for u_id in pm.pop('recipient_ids')]
            self._add_private_channel(factory(me=user, data=pm, state=self))

        # Extras
        region = data.get('geo_ordered_rtc_regions', ['us-west'])[0]
        self.preferred_region = try_enum(VoiceRegion, region)
        user.settings = Settings(data=data.get('user_settings', {}), state=self)

        # We're done
        del self._ready_data
        self.call_handlers('connect')
        self.dispatch('connect')
        self._ready_task = asyncio.ensure_future(self._delay_ready(), loop=self.loop)

    def parse_resumed(self, data):
        self.dispatch('resumed')

    def parse_message_create(self, data):
        guild_id = data.get('guild_id')
        if guild_id is not None and int(guild_id) in self._unavailable_guilds:
            return
        channel, _ = self._get_guild_channel(data)
        message = Message(channel=channel, data=data, state=self)
        self.dispatch('message', message)
        if self._messages is not None:
            self._messages.append(message)
        if channel and channel.__class__ is TextChannel:
            channel.last_message_id = message.id

    def parse_message_delete(self, data):
        raw = RawMessageDeleteEvent(data)
        found = self._get_message(raw.message_id)
        raw.cached_message = found
        self.dispatch('raw_message_delete', raw)
        if self._messages is not None and found is not None:
            self.dispatch('message_delete', found)
            self._messages.remove(found)

    def parse_message_delete_bulk(self, data):
        raw = RawBulkMessageDeleteEvent(data)
        if self._messages:
            found_messages = [message for message in self._messages if message.id in raw.message_ids]
        else:
            found_messages = []
        raw.cached_messages = found_messages
        self.dispatch('raw_bulk_message_delete', raw)
        if found_messages:
            self.dispatch('bulk_message_delete', found_messages)
            for msg in found_messages:
                self._messages.remove(msg)

    def parse_message_update(self, data):
        raw = RawMessageUpdateEvent(data)
        message = self._get_message(raw.message_id)
        if message is not None:
            older_message = copy.copy(message)
            raw.cached_message = older_message
            self.dispatch('raw_message_edit', raw)
            message._update(data)
            # Coerce the `after` parameter to take the new updated Member
            # ref: #5999
            older_message.author = message.author
            self.dispatch('message_edit', older_message, message)
        else:
            self.dispatch('raw_message_edit', raw)

    def parse_message_reaction_add(self, data):
        emoji = data['emoji']
        emoji_id = utils._get_as_snowflake(emoji, 'id')
        emoji = PartialEmoji.with_state(self, id=emoji_id, animated=emoji.get('animated', False), name=emoji['name'])
        raw = RawReactionActionEvent(data, emoji, 'REACTION_ADD')

        member_data = data.get('member')
        if member_data:
            guild = self._get_guild(raw.guild_id)
            raw.member = Member(data=member_data, guild=guild, state=self)
        else:
            raw.member = None
        self.dispatch('raw_reaction_add', raw)

        # rich interface here
        message = self._get_message(raw.message_id)
        if message is not None:
            emoji = self._upgrade_partial_emoji(emoji)
            reaction = message._add_reaction(data, emoji, raw.user_id)
            user = raw.member or self._get_reaction_user(message.channel, raw.user_id)

            if user:
                self.dispatch('reaction_add', reaction, user)

    def parse_message_reaction_remove_all(self, data):
        raw = RawReactionClearEvent(data)
        self.dispatch('raw_reaction_clear', raw)

        message = self._get_message(raw.message_id)
        if message is not None:
            old_reactions = message.reactions.copy()
            message.reactions.clear()
            self.dispatch('reaction_clear', message, old_reactions)

    def parse_message_reaction_remove(self, data):
        emoji = data['emoji']
        emoji_id = utils._get_as_snowflake(emoji, 'id')
        emoji = PartialEmoji.with_state(self, id=emoji_id, name=emoji['name'])
        raw = RawReactionActionEvent(data, emoji, 'REACTION_REMOVE')
        self.dispatch('raw_reaction_remove', raw)

        message = self._get_message(raw.message_id)
        if message is not None:
            emoji = self._upgrade_partial_emoji(emoji)
            try:
                reaction = message._remove_reaction(data, emoji, raw.user_id)
            except (AttributeError, ValueError): # eventual consistency lol
                pass
            else:
                user = self._get_reaction_user(message.channel, raw.user_id)
                if user:
                    self.dispatch('reaction_remove', reaction, user)

    def parse_message_reaction_remove_emoji(self, data):
        emoji = data['emoji']
        emoji_id = utils._get_as_snowflake(emoji, 'id')
        emoji = PartialEmoji.with_state(self, id=emoji_id, name=emoji['name'])
        raw = RawReactionClearEmojiEvent(data, emoji)
        self.dispatch('raw_reaction_clear_emoji', raw)

        message = self._get_message(raw.message_id)
        if message is not None:
            try:
                reaction = message._clear_emoji(emoji)
            except (AttributeError, ValueError): # eventual consistency lol
                pass
            else:
                if reaction:
                    self.dispatch('reaction_clear_emoji', reaction)

    def parse_presence_update(self, data):
        # Testing shows that this is only sent for relationship presences,
        # which are not currently parsed.
        guild_id = utils._get_as_snowflake(data, 'guild_id')
        guild = self._get_guild(guild_id)
        if guild is None:
            log.debug('PRESENCE_UPDATE referencing an unknown guild ID: %s. Discarding.', guild_id)
            return

        user = data['user']
        member_id = int(user['id'])
        member = guild.get_member(member_id)
        flags = self.member_cache_flags
        if member is None:
            if 'username' not in user:
                return

            member, old_member = Member._from_presence_update(guild=guild, data=data, state=self)
            if flags.online or (flags._online_only and member.raw_status != 'offline'):
                guild._add_member(member)
        else:
            old_member = Member._copy(member)
            user_update = member._presence_update(data=data, user=user)
            if user_update:
                self.dispatch('user_update', user_update[0], user_update[1])

            if member.id != self.self_id and flags._online_only and member.raw_status == 'offline':
                guild._remove_member(member)

        self.dispatch('member_update', old_member, member)

    def parse_user_update(self, data):
        self.user._update(data)

    def parse_invite_create(self, data):
        invite = Invite.from_gateway(state=self, data=data)
        self.dispatch('invite_create', invite)

    def parse_invite_delete(self, data):
        invite = Invite.from_gateway(state=self, data=data)
        self.dispatch('invite_delete', invite)

    def parse_channel_delete(self, data):
        guild = self._get_guild(utils._get_as_snowflake(data, 'guild_id'))
        channel_id = int(data['id'])
        if guild is not None:
            channel = guild.get_channel(channel_id)
            if channel is not None:
                guild._remove_channel(channel)
                self.dispatch('guild_channel_delete', channel)
        else:
            # The reason we're doing this is so it's also removed from the
            # private channel by user cache as well.
            channel = self._get_private_channel(channel_id)
            if channel is not None:
                self._remove_private_channel(channel)
                self.dispatch('private_channel_delete', channel)

    def parse_channel_update(self, data):
        channel_type = try_enum(ChannelType, data.get('type'))
        channel_id = int(data['id'])
        if channel_type is ChannelType.group:
            channel = self._get_private_channel(channel_id)
            old_channel = copy.copy(channel)
            channel._update_group(data)
            self.dispatch('private_channel_update', old_channel, channel)
            return

        guild_id = utils._get_as_snowflake(data, 'guild_id')
        guild = self._get_guild(guild_id)
        if guild is not None:
            channel = guild.get_channel(channel_id)
            if channel is not None:
                old_channel = copy.copy(channel)
                channel._update(guild, data)
                self.dispatch('guild_channel_update', old_channel, channel)
            else:
                log.debug('CHANNEL_UPDATE referencing an unknown channel ID: %s. Discarding.', channel_id)
        else:
            log.debug('CHANNEL_UPDATE referencing an unknown guild ID: %s. Discarding.', guild_id)

    def parse_channel_create(self, data):
        factory, ch_type = _channel_factory(data['type'])
        if factory is None:
            log.debug('CHANNEL_CREATE referencing an unknown channel type %s. Discarding.', data['type'])
            return

        if ch_type in (ChannelType.group, ChannelType.private):
            channel_id = int(data['id'])
            if self._get_private_channel(channel_id) is None:
                channel = factory(me=self.user, data=data, state=self)
                self._add_private_channel(channel)
                self.dispatch('private_channel_create', channel)
        else:
            guild_id = utils._get_as_snowflake(data, 'guild_id')
            guild = self._get_guild(guild_id)
            if guild is not None:
                channel = factory(guild=guild, state=self, data=data)
                guild._add_channel(channel)
                self.dispatch('guild_channel_create', channel)
            else:
                log.debug('CHANNEL_CREATE referencing an unknown guild ID: %s. Discarding.', guild_id)
                return

    def parse_channel_pins_update(self, data):
        channel_id = int(data['channel_id'])
        channel = self.get_channel(channel_id)
        if channel is None:
            log.debug('CHANNEL_PINS_UPDATE referencing an unknown channel ID: %s. Discarding.', channel_id)
            return

        last_pin = utils.parse_time(data['last_pin_timestamp']) if data['last_pin_timestamp'] else None

        try:
            # I have not imported discord.abc in this file;
            # the isinstance check is also 2x slower than just checking this attribute,
            # so we're just gonna check it since it's easier and faster and lazier.
            channel.guild
        except AttributeError:
            self.dispatch('private_channel_pins_update', channel, last_pin)
        else:
            self.dispatch('guild_channel_pins_update', channel, last_pin)

    def parse_channel_recipient_add(self, data):
        channel = self._get_private_channel(int(data['channel_id']))
        user = self.store_user(data['user'])
        channel.recipients.append(user)
        self.dispatch('group_join', channel, user)

    def parse_channel_recipient_remove(self, data):
        channel = self._get_private_channel(int(data['channel_id']))
        user = self.store_user(data['user'])
        try:
            channel.recipients.remove(user)
        except ValueError:
            pass
        else:
            self.dispatch('group_remove', channel, user)

    def parse_guild_member_add(self, data):
        # IIRC this is sent when you join a guild...
        # This is currently useless since the guild is
        # probably in the middle of being subscribed.
        guild = self._get_guild(int(data['guild_id']))
        if guild is None:
            log.debug('GUILD_MEMBER_ADD referencing an unknown guild ID: %s. Discarding.', data['guild_id'])
            return

        member = Member(guild=guild, data=data, state=self)
        if self.member_cache_flags.joined:
            guild._add_member(member)

        try:
            guild._member_count += 1
        except AttributeError:
            pass

        #self.dispatch('member_join', member)

    def parse_guild_member_remove(self, data):
        # In rare events this is sent. Will probably be used more
        # in the future (when on_member_remove returns).
        guild = self._get_guild(int(data['guild_id']))
        if guild is not None:
            try:
                guild._member_count -= 1
            except AttributeError:
                pass

            user_id = int(data['user']['id'])
            member = guild.get_member(user_id)
            if member is not None:
                guild._remove_member(member)
                #self.dispatch('member_remove', member)
        else:
            log.debug('GUILD_MEMBER_REMOVE referencing an unknown guild ID: %s. Discarding.', data['guild_id'])

    def parse_guild_member_update(self, data):
        guild = self._get_guild(int(data['guild_id']))
        user = data['user']
        user_id = int(user['id'])
        if guild is None:
            log.debug('GUILD_MEMBER_UPDATE referencing an unknown guild ID: %s. Discarding.', data['guild_id'])
            return

        member = guild.get_member(user_id)
        if member is not None:
            old_member = Member._copy(member)
            member._update(data)
            user_update = member._update_inner_user(user)
            if user_update:
                self.dispatch('user_update', user_update[0], user_update[1])

            self.dispatch('member_update', old_member, member)
        else:
            if self.member_cache_flags.joined:
                member = Member(data=data, guild=guild, state=self)

                # Force an update on the inner user if necessary
                user_update = member._update_inner_user(user)
                if user_update:
                    self.dispatch('user_update', user_update[0], user_update[1])

                guild._add_member(member)
            log.debug('GUILD_MEMBER_UPDATE referencing an unknown member ID: %s. Discarding.', user_id)

    def parse_guild_sync(self, data):
        print('HOW THE FUCK DID YOU TRIGGER A `GUILD_SYNC`???\nIf you want to share your secrets, please feel free to email me.')

    def parse_guild_member_list_update(self, data):
        self.dispatch('raw_guild_member_list_update', data)
        guild = self._get_guild(int(data['guild_id']))
        if guild is None:
            log.debug('GUILD_MEMBER_LIST_UPDATE referencing an unknown guild ID: %s. Discarding.', data['guild_id'])
            return

        ops = data['ops']

        if data['member_count'] > 0:
            guild._member_count = data['member_count']

        online_count = 0
        for group in data['groups']:
            online_count += group['count'] if group['id'] != 'offline' else 0
        guild._online_count = online_count

        for opdata in ops:
            op = opdata['op']
            # There are two OPs I'm not parsing.
            # INVALIDATE: Usually invalid (hehe).
            # DELETE: Sends the index, not the user ID, so I can't do anything with
            # it unless I keep a seperate list of the member sidebar (maybe in future).

            if op == 'SYNC':
                members = [Member(guild=guild, data=member['member'], state=self) for member in [item for item in opdata.get('items', []) if 'member' in item]]

                member_dict = {str(member.id): member for member in members}
                for presence in [item for item in opdata.get('items', []) if 'member' in item]:
                    presence = presence['member']['presence']
                    user = presence['user']
                    member_id = user['id']
                    member = member_dict.get(member_id)
                    member._presence_update(presence, user)

                for member in members:
                    guild._add_member(member)

            if op == 'INSERT':
                if 'member' not in opdata['item']:
                    # Hoisted role INSERT
                    return

                mdata = opdata['item']['member']
                user = mdata['user']
                user_id = int(user['id'])

                member = guild.get_member(user_id)
                if member is not None: # INSERTs are also sent when a user changes range
                    old_member = Member._copy(member)
                    member._update(mdata)
                    user_update = member._update_inner_user(user)
                    if 'presence' in mdata:
                        presence = mdata['presence']
                        user = presence['user']
                        member_id = user['id']
                        member._presence_update(presence, user)
                    if user_update:
                        self.dispatch('user_update', user_update[0], user_update[1])

                    self.dispatch('member_update', old_member, member)
                else:
                    member = Member(data=mdata, guild=guild, state=self)
                    guild._add_member(member)

            if op == 'UPDATE':
                if 'member' not in opdata['item']:
                    # Hoisted role UPDATE
                    return

                mdata = opdata['item']['member']
                user = mdata['user']
                user_id = int(user['id'])

                member = guild.get_member(user_id)
                if member is not None:
                    old_member = Member._copy(member)
                    member._update(mdata)
                    user_update = member._update_inner_user(user)
                    if 'presence' in mdata:
                        presence = mdata['presence']
                        user = presence['user']
                        member_id = user['id']
                        member._presence_update(presence, user)
                    if user_update:
                        self.dispatch('user_update', user_update[0], user_update[1])

                    self.dispatch('member_update', old_member, member)
                else:
                    log.debug('GUILD_MEMBER_LIST_UPDATE type UPDATE referencing an unknown member ID: %s. Discarding.', user_id)

    def parse_guild_emojis_update(self, data):
        guild = self._get_guild(int(data['guild_id']))
        if guild is None:
            log.debug('GUILD_EMOJIS_UPDATE referencing an unknown guild ID: %s. Discarding.', data['guild_id'])
            return

        before_emojis = guild.emojis
        for emoji in before_emojis:
            self._emojis.pop(emoji.id, None)
        guild.emojis = tuple(map(lambda d: self.store_emoji(guild, d), data['emojis']))
        self.dispatch('guild_emojis_update', guild, before_emojis, guild.emojis)

    def _get_create_guild(self, data):
        guild = self._get_guild(int(data['id']))
        # Discord being Discord sends a GUILD_CREATE
        # after an OPCode 14 is sent (a la bots).
        # However, we want that if we forced a
        # GUILD_CREATE for an unavailable guild.
        if guild is not None:
            guild._from_data(data)
            return

        return self._add_guild_from_data(data)

    def is_guild_evicted(self, guild) -> bool:
        return guild.id not in self._guilds

    async def chunk_guild(self, guild, *, wait=True, cache=None):
        cache = cache or self.member_cache_flags.joined
        request = self._chunk_requests.get(guild.id)
        if request is None:
            self._chunk_requests[guild.id] = request = ChunkRequest(guild.id, self.loop, self._get_guild, cache=cache)
            await self.chunker(guild.id, nonce=request.nonce)

        if wait:
            return await request.wait()
        return request.get_future()

    async def _parse_and_dispatch(self, guild, *, chunk, subscribe):
        self._queued_guilds[guild.id] = guild

        if chunk:
            try:
                await asyncio.wait_for(self.chunk_guild(guild), timeout=60.0)
            except asyncio.TimeoutError:
                log.info('Somehow timed out waiting for chunks.')

        if subscribe:
            await guild.subscribe(max_online=self._subscription_options.max_online)

        self._queued_guilds.pop(guild.id)

        # Dispatch available/join depending on circumstances
        if guild.id in self._unavailable_guilds:
            type = self._unavailable_guilds.pop(guild.id)
            if type is UnavailableGuildType.existing:
                self.dispatch('guild_available', guild)
            else:
                self.dispatch('guild_join', guild)
        else:
            self.dispatch('guild_join', guild)

    def parse_guild_create(self, data):
        guild_id = int(data['id'])

        guild = self._get_create_guild(data)

        if guild is None:
            return

        # Chunk/subscribe if needed
        needs_chunking, needs_subscribing = self._guild_needs_chunking(guild), self._guild_needs_subscribing(guild)
        if needs_chunking or needs_subscribing:
            asyncio.ensure_future(self._parse_and_dispatch(guild, chunk=needs_chunking, subscribe=needs_subscribing), loop=self.loop)
            return

        # Dispatch available/join depending on circumstances
        if guild_id in self._unavailable_guilds:
            type = self._unavailable_guilds.pop(guild_id)
            if type is UnavailableGuildType.existing:
                self.dispatch('guild_available', guild)
            else:
                self.dispatch('guild_join', guild)
        else:
            self.dispatch('guild_join', guild)

    def parse_guild_update(self, data):
        guild = self._get_guild(int(data['id']))
        if guild is not None:
            old_guild = copy.copy(guild)
            guild._from_data(data)
            self.dispatch('guild_update', old_guild, guild)
        else:
            log.debug('GUILD_UPDATE referencing an unknown guild ID: %s. Discarding.', data['id'])

    def parse_guild_delete(self, data):
        guild = self._get_guild(int(data['id']))
        if guild is None:
            log.debug('GUILD_DELETE referencing an unknown guild ID: %s. Discarding.', data['id'])
            return

        if data.get('unavailable', False):
            # GUILD_DELETE with unavailable being True means that the
            # guild that was available is now currently unavailable
            guild.unavailable = True
            self.dispatch('guild_unavailable', guild)
            return

        # Cleanup the message cache
        if self._messages is not None:
            self._messages = deque((msg for msg in self._messages if msg.guild != guild), maxlen=self.max_messages)

        self._remove_guild(guild)
        self.dispatch('guild_remove', guild)

    def parse_guild_ban_add(self, data):
        guild = self._get_guild(int(data['guild_id']))
        if guild is not None:
            try:
                user = User(data=data['user'], state=self)
            except KeyError:
                pass
            else:
                member = guild.get_member(user.id) or user
                self.dispatch('member_ban', guild, member)

    def parse_guild_ban_remove(self, data):
        guild = self._get_guild(int(data['guild_id']))
        if guild is not None and 'user' in data:
            user = self.store_user(data['user'])
            self.dispatch('member_unban', guild, user)

    def parse_guild_role_create(self, data):
        guild = self._get_guild(int(data['guild_id']))
        if guild is None:
            log.debug('GUILD_ROLE_CREATE referencing an unknown guild ID: %s. Discarding.', data['guild_id'])
            return

        role_data = data['role']
        role = Role(guild=guild, data=role_data, state=self)
        guild._add_role(role)
        self.dispatch('guild_role_create', role)

    def parse_guild_role_delete(self, data):
        guild = self._get_guild(int(data['guild_id']))
        if guild is not None:
            role_id = int(data['role_id'])
            try:
                role = guild._remove_role(role_id)
            except KeyError:
                return
            else:
                self.dispatch('guild_role_delete', role)
        else:
            log.debug('GUILD_ROLE_DELETE referencing an unknown guild ID: %s. Discarding.', data['guild_id'])

    def parse_guild_role_update(self, data):
        guild = self._get_guild(int(data['guild_id']))
        if guild is not None:
            role_data = data['role']
            role_id = int(role_data['id'])
            role = guild.get_role(role_id)
            if role is not None:
                old_role = copy.copy(role)
                role._update(role_data)
                self.dispatch('guild_role_update', old_role, role)
        else:
            log.debug('GUILD_ROLE_UPDATE referencing an unknown guild ID: %s. Discarding.', data['guild_id'])

    def parse_guild_members_chunk(self, data):
        guild_id = int(data['guild_id'])
        guild = self._get_guild(guild_id)
        presences = data.get('presences', [])

        members = [Member(guild=guild, data=member, state=self) for member in data.get('members', [])]
        log.debug('Processed a chunk for %s members in guild ID %s.', len(members), guild_id)

        if presences:
            member_dict = {str(member.id): member for member in members}
            for presence in presences:
                user = presence['user']
                member_id = user['id']
                member = member_dict.get(member_id)
                member._presence_update(presence, user)

        complete = data.get('chunk_index', 0) + 1 == data.get('chunk_count')
        self.process_chunk_requests(guild_id, data.get('nonce'), members, complete)

    def parse_guild_integrations_update(self, data):
        guild = self._get_guild(int(data['guild_id']))
        if guild is not None:
            self.dispatch('guild_integrations_update', guild)
        else:
            log.debug('GUILD_INTEGRATIONS_UPDATE referencing an unknown guild ID: %s. Discarding.', data['guild_id'])

    def parse_webhooks_update(self, data):
        channel = self.get_channel(int(data['channel_id']))
        if channel is not None:
            self.dispatch('webhooks_update', channel)
        else:
            log.debug('WEBHOOKS_UPDATE referencing an unknown channel ID: %s. Discarding.', data['channel_id'])

    def parse_voice_state_update(self, data):
        guild = self._get_guild(utils._get_as_snowflake(data, 'guild_id'))
        channel_id = utils._get_as_snowflake(data, 'channel_id')
        flags = self.member_cache_flags
        self_id = self.user.id
        if guild is not None:
            if int(data['user_id']) == self_id:
                voice = self._get_voice_client(guild.id)
                if voice is not None:
                    coro = voice.on_voice_state_update(data)
                    asyncio.ensure_future(logging_coroutine(coro, info='Voice Protocol voice state update handler'))

            member, before, after = guild._update_voice_state(data, channel_id)
            if member is not None:
                if flags.voice:
                    if channel_id is None and flags._voice_only and member.id != self_id:
                        # Only remove from cache if we only have the voice flag enabled
                        guild._remove_member(member)
                    elif channel_id is not None:
                        guild._add_member(member)

                self.dispatch('voice_state_update', member, before, after)
            else:
                log.debug('VOICE_STATE_UPDATE referencing an unknown member ID: %s. Discarding.', data['user_id'])
        else:
            # We're at calls
            call = self._calls.get(channel_id)
            if call is not None:
                call._update_voice_state(data)

    def parse_voice_server_update(self, data):
        try:
            key_id = int(data['guild_id'])
        except KeyError:
            key_id = int(data['channel_id'])

        vc = self._get_voice_client(key_id)
        if vc is not None:
            coro = vc.on_voice_server_update(data)
            asyncio.ensure_future(logging_coroutine(coro, info='Voice Protocol voice server update handler'))

    def parse_typing_start(self, data):
        channel, guild = self._get_guild_channel(data)
        if channel is not None:
            member = None
            user_id = utils._get_as_snowflake(data, 'user_id')
            if isinstance(channel, DMChannel):
                member = channel.recipient
            elif isinstance(channel, TextChannel) and guild is not None:
                member = guild.get_member(user_id)
                if member is None:
                    member_data = data.get('member')
                    if member_data:
                        member = Member(data=member_data, state=self, guild=guild)

            elif isinstance(channel, GroupChannel):
                member = utils.find(lambda x: x.id == user_id, channel.recipients)

            if member is not None:
                timestamp = datetime.datetime.utcfromtimestamp(data.get('timestamp'))
                self.dispatch('typing', channel, member, timestamp)

    def parse_relationship_add(self, data):
        key = int(data['id'])
        old = self.user.get_relationship(key)
        new = Relationship(state=self, data=data)
        self.user._relationships[key] = new
        if old is not None:
            self.dispatch('relationship_update', old, new)
        else:
            self.dispatch('relationship_add', new)

    def parse_relationship_remove(self, data):
        key = int(data['id'])
        try:
            old = self.user._relationships.pop(key)
        except KeyError:
            pass
        else:
            self.dispatch('relationship_remove', old)

    def parse_user_required_action_update(self, data):
        required_action = try_enum(RequiredActionType, data['required_action'])
        self.dispatch('required_action_update', required_action)

    def _get_reaction_user(self, channel, user_id):
        if isinstance(channel, TextChannel):
            return channel.guild.get_member(user_id)
        return self.get_user(user_id)

    def get_reaction_emoji(self, data):
        emoji_id = utils._get_as_snowflake(data, 'id')

        if not emoji_id:
            return data['name']

        try:
            return self._emojis[emoji_id]
        except KeyError:
            return PartialEmoji.with_state(self, animated=data.get('animated', False), id=emoji_id, name=data['name'])

    def _upgrade_partial_emoji(self, emoji):
        emoji_id = emoji.id
        if not emoji_id:
            return emoji.name
        try:
            return self._emojis[emoji_id]
        except KeyError:
            return emoji

    def get_channel(self, id):
        if id is None:
            return None

        pm = self._get_private_channel(id)
        if pm is not None:
            return pm

        for guild in self.guilds:
            channel = guild.get_channel(id)
            if channel is not None:
                return channel

    def create_message(self, *, channel, data):
        return Message(state=self, channel=channel, data=data)
