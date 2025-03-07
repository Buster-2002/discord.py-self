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
import json
import logging
import re
import time
from urllib.parse import quote as _uriquote

import aiohttp

from . import utils
from .asset import Asset
from .enums import WebhookType, try_enum
from .errors import (DiscordServerError, Forbidden, HTTPException,
                     InvalidArgument, NotFound)
from .message import Message
from .mixins import Hashable
from .user import BaseUser, User

__all__ = (
    'WebhookAdapter',
    'AsyncWebhookAdapter',
    'RequestsWebhookAdapter',
    'Webhook',
    'WebhookMessage',
)

log = logging.getLogger(__name__)

class WebhookAdapter:
    """Base class for all webhook adapters.

    Attributes
    ------------
    webhook: :class:`Webhook`
        The webhook that owns this adapter.
    """

    BASE = 'https://discord.com/api/v7'

    def _prepare(self, webhook):
        self._webhook_id = webhook.id
        self._webhook_token = webhook.token
        self._request_url = '{0.BASE}/webhooks/{1}/{2}'.format(self, webhook.id, webhook.token)
        self.webhook = webhook

    def is_async(self):
        return False

    def request(self, verb, url, payload=None, multipart=None):
        """Actually does the request.

        Subclasses must implement this.

        Parameters
        -----------
        verb: :class:`str`
            The HTTP verb to use for the request.
        url: :class:`str`
            The URL to send the request to. This will have
            the query parameters already added to it, if any.
        multipart: Optional[:class:`dict`]
            A dict containing multipart form data to send with
            the request. If a filename is being uploaded, then it will
            be under a ``file`` key which will have a 3-element :class:`tuple`
            denoting ``(filename, file, content_type)``.
        payload: Optional[:class:`dict`]
            The JSON to send with the request, if any.
        """
        raise NotImplementedError()

    def delete_webhook(self, *, reason=None):
        return self.request('DELETE', self._request_url, reason=reason)

    def edit_webhook(self, *, reason=None, **payload):
        return self.request('PATCH', self._request_url, payload=payload, reason=reason)

    def edit_webhook_message(self, message_id, payload):
        return self.request('PATCH', '{}/messages/{}'.format(self._request_url, message_id), payload=payload)

    def delete_webhook_message(self, message_id):
        return self.request('DELETE', '{}/messages/{}'.format(self._request_url, message_id))

    def handle_execution_response(self, data, *, wait):
        """Transforms the webhook execution response into something
        more meaningful.

        This is mainly used to convert the data into a :class:`Message`
        if necessary.

        Subclasses must implement this.

        Parameters
        ------------
        data
            The data that was returned from the request.
        wait: :class:`bool`
            Whether the webhook execution was asked to wait or not.
        """
        raise NotImplementedError()

    async def _wrap_coroutine_and_cleanup(self, coro, cleanup):
        try:
            return await coro
        finally:
            cleanup()

    def execute_webhook(self, *, payload, wait=False, file=None, files=None):
        cleanup = None
        if file is not None:
            multipart = {
                'file': (file.filename, file.fp, 'application/octet-stream'),
                'payload_json': utils.to_json(payload)
            }
            data = None
            cleanup = file.close
            files_to_pass = [file]
        elif files is not None:
            multipart = {
                'payload_json': utils.to_json(payload)
            }
            for i, file in enumerate(files):
                multipart['file%i' % i] = (file.filename, file.fp, 'application/octet-stream')
            data = None

            def _anon():
                for f in files:
                    f.close()

            cleanup = _anon
            files_to_pass = files
        else:
            data = payload
            multipart = None
            files_to_pass = None

        url = '%s?wait=%d' % (self._request_url, wait)
        maybe_coro = None
        try:
            maybe_coro = self.request('POST', url, multipart=multipart, payload=data, files=files_to_pass)
        finally:
            if maybe_coro is not None and cleanup is not None:
                if not asyncio.iscoroutine(maybe_coro):
                    cleanup()
                else:
                    maybe_coro = self._wrap_coroutine_and_cleanup(maybe_coro, cleanup)

        # if request raises up there then this should never be `None`
        return self.handle_execution_response(maybe_coro, wait=wait)

class AsyncWebhookAdapter(WebhookAdapter):
    """A webhook adapter suited for use with aiohttp.

    .. note::

        You are responsible for cleaning up the client session.

    Parameters
    -----------
    session: :class:`aiohttp.ClientSession`
        The session to use to send requests.
    """

    def __init__(self, session):
        self.session = session
        self.loop = asyncio.get_event_loop()

    def is_async(self):
        return True

    async def request(self, verb, url, payload=None, multipart=None, *, files=None, reason=None):
        headers = {}
        data = None
        files = files or []
        if payload:
            headers['Content-Type'] = 'application/json'
            data = utils.to_json(payload)

        if reason:
            headers['X-Audit-Log-Reason'] = _uriquote(reason, safe='/ ')

        base_url = url.replace(self._request_url, '/') or '/'
        _id = self._webhook_id
        for tries in range(5):
            for file in files:
                file.reset(seek=tries)

            if multipart:
                data = aiohttp.FormData()
                for key, value in multipart.items():
                    if key.startswith('file'):
                        data.add_field(key, value[1], filename=value[0], content_type=value[2])
                    else:
                        data.add_field(key, value)

            async with self.session.request(verb, url, headers=headers, data=data) as r:
                log.debug('Webhook ID %s with %s %s has returned status code %s', _id, verb, base_url, r.status)
                # Coerce empty strings to return None for hygiene purposes
                response = (await r.text(encoding='utf-8')) or None
                if r.headers['Content-Type'] == 'application/json':
                    response = json.loads(response)

                # check if we have rate limit header information
                remaining = r.headers.get('X-Ratelimit-Remaining')
                if remaining == '0' and r.status != 429:
                    delta = utils._parse_ratelimit_header(r)
                    log.debug('Webhook ID %s has been pre-emptively rate limited, waiting %.2f seconds', _id, delta)
                    await asyncio.sleep(delta)

                if 300 > r.status >= 200:
                    return response

                # we are being rate limited
                if r.status == 429:
                    if not r.headers.get('Via'):
                        # Banned by Cloudflare more than likely.
                        raise HTTPException(r, data)

                    retry_after = response['retry_after'] / 1000.0
                    log.warning('Webhook ID %s is rate limited. Retrying in %.2f seconds', _id, retry_after)
                    await asyncio.sleep(retry_after)
                    continue

                if r.status in (500, 502):
                    await asyncio.sleep(1 + tries * 2)
                    continue

                if r.status == 403:
                    raise Forbidden(r, response)
                elif r.status == 404:
                    raise NotFound(r, response)
                else:
                    raise HTTPException(r, response)

        # no more retries
        if r.status >= 500:
            raise DiscordServerError(r, response)
        raise HTTPException(r, response)

    async def handle_execution_response(self, response, *, wait):
        data = await response
        if not wait:
            return data

        # transform into Message object
        # Make sure to coerce the state to the partial one to allow message edits/delete
        state = _PartialWebhookState(self, self.webhook, parent=self.webhook._state)
        return WebhookMessage(data=data, state=state, channel=self.webhook.channel)

class RequestsWebhookAdapter(WebhookAdapter):
    """A webhook adapter suited for use with ``requests``.

    Only versions of :doc:`req:index` higher than 2.13.0 are supported.

    Parameters
    -----------
    session: Optional[`requests.Session <http://docs.python-requests.org/en/latest/api/#requests.Session>`_]
        The requests session to use for sending requests. If not given then
        each request will create a new session. Note if a session is given,
        the webhook adapter **will not** clean it up for you. You must close
        the session yourself.
    sleep: :class:`bool`
        Whether to sleep the thread when encountering a 429 or pre-emptive
        rate limit or a 5xx status code. Defaults to ``True``. If set to
        ``False`` then this will raise an :exc:`HTTPException` instead.
    """

    def __init__(self, session=None, *, sleep=True):
        import requests
        self.session = session or requests
        self.sleep = sleep

    def request(self, verb, url, payload=None, multipart=None, *, files=None, reason=None):
        headers = {}
        data = None
        files = files or []
        if payload:
            headers['Content-Type'] = 'application/json'
            data = utils.to_json(payload)

        if reason:
            headers['X-Audit-Log-Reason'] = _uriquote(reason, safe='/ ')

        if multipart is not None:
            data = {'payload_json': multipart.pop('payload_json')}

        base_url = url.replace(self._request_url, '/') or '/'
        _id = self._webhook_id
        for tries in range(5):
            for file in files:
                file.reset(seek=tries)

            r = self.session.request(verb, url, headers=headers, data=data, files=multipart)
            r.encoding = 'utf-8'
            # Coerce empty responses to return None for hygiene purposes
            response = r.text or None

            # compatibility with aiohttp
            r.status = r.status_code

            log.debug('Webhook ID %s with %s %s has returned status code %s', _id, verb, base_url, r.status)
            if r.headers['Content-Type'] == 'application/json':
                response = json.loads(response)

            # check if we have rate limit header information
            remaining = r.headers.get('X-Ratelimit-Remaining')
            if remaining == '0' and r.status != 429 and self.sleep:
                delta = utils._parse_ratelimit_header(r)
                log.debug('Webhook ID %s has been pre-emptively rate limited, waiting %.2f seconds', _id, delta)
                time.sleep(delta)

            if 300 > r.status >= 200:
                return response

            # we are being rate limited
            if r.status == 429:
                if self.sleep:
                    if not r.headers.get('Via'):
                        # Banned by Cloudflare more than likely.
                        raise HTTPException(r, data)

                    retry_after = response['retry_after'] / 1000.0
                    log.warning('Webhook ID %s is rate limited. Retrying in %.2f seconds', _id, retry_after)
                    time.sleep(retry_after)
                    continue
                else:
                    raise HTTPException(r, response)

            if self.sleep and r.status in (500, 502):
                time.sleep(1 + tries * 2)
                continue

            if r.status == 403:
                raise Forbidden(r, response)
            elif r.status == 404:
                raise NotFound(r, response)
            else:
                raise HTTPException(r, response)

        # no more retries
        if r.status >= 500:
            raise DiscordServerError(r, response)
        raise HTTPException(r, response)

    def handle_execution_response(self, response, *, wait):
        if not wait:
            return response

        # transform into Message object
        # Make sure to coerce the state to the partial one to allow message edits/delete
        state = _PartialWebhookState(self, self.webhook, parent=self.webhook._state)
        return WebhookMessage(data=response, state=state, channel=self.webhook.channel)

class _FriendlyHttpAttributeErrorHelper:
    __slots__ = ()

    def __getattr__(self, attr):
        raise AttributeError('PartialWebhookState does not support http methods.')

class _PartialWebhookState:
    __slots__ = ('loop', 'parent', '_webhook')

    def __init__(self, adapter, webhook, parent):
        self._webhook = webhook

        if isinstance(parent, self.__class__):
            self.parent = None
        else:
            self.parent = parent

        # Fetch the loop from the adapter if it's there
        try:
            self.loop = adapter.loop
        except AttributeError:
            self.loop = None

    def _get_guild(self, guild_id):
        return None

    def store_user(self, data):
        return BaseUser(state=self, data=data)

    @property
    def http(self):
        if self.parent is not None:
            return self.parent.http

        # Some data classes assign state.http and that should be kosher
        # however, using it should result in a late-binding error.
        return _FriendlyHttpAttributeErrorHelper()

    def __getattr__(self, attr):
        if self.parent is not None:
            return getattr(self.parent, attr)

        raise AttributeError('PartialWebhookState does not support {0!r}.'.format(attr))

class WebhookMessage(Message):
    """Represents a message sent from your webhook.

    This allows you to edit or delete a message sent by your
    webhook.

    This inherits from :class:`discord.Message` with changes to
    :meth:`edit` and :meth:`delete` to work.

    .. versionadded:: 1.6
    """

    def edit(self, **fields):
        """|maybecoro|

        Edits the message.

        The content must be able to be transformed into a string via ``str(content)``.

        .. versionadded:: 1.6

        Parameters
        ------------
        content: Optional[:class:`str`]
            The content to edit the message with or ``None`` to clear it.
        embeds: List[:class:`Embed`]
            A list of embeds to edit the message with.
        embed: Optional[:class:`Embed`]
            The embed to edit the message with. ``None`` suppresses the embeds.
            This should not be mixed with the ``embeds`` parameter.
        allowed_mentions: :class:`AllowedMentions`
            Controls the mentions being processed in this message.
            See :meth:`.abc.Messageable.send` for more information.

        Raises
        -------
        HTTPException
            Editing the message failed.
        Forbidden
            Edited a message that is not yours.
        InvalidArgument
            You specified both ``embed`` and ``embeds`` or the length of
            ``embeds`` was invalid or there was no token associated with
            this webhook.
        """
        return self._state._webhook.edit_message(self.id, **fields)

    def _delete_delay_sync(self, delay):
        time.sleep(delay)
        return self._state._webhook.delete_message(self.id)

    async def _delete_delay_async(self, delay):
        async def inner_call():
            await asyncio.sleep(delay)
            try:
                await self._state._webhook.delete_message(self.id)
            except HTTPException:
                pass

        asyncio.ensure_future(inner_call(), loop=self._state.loop)
        return await asyncio.sleep(0)

    def delete(self, *, delay=None):
        """|coro|

        Deletes the message.

        Parameters
        -----------
        delay: Optional[:class:`float`]
            If provided, the number of seconds to wait before deleting the message.
            If this is a coroutine, the waiting is done in the background and deletion failures
            are ignored. If this is not a coroutine then the delay blocks the thread.

        Raises
        ------
        Forbidden
            You do not have proper permissions to delete the message.
        NotFound
            The message was deleted already.
        HTTPException
            Deleting the message failed.
        """

        if delay is not None:
            if self._state._webhook._adapter.is_async():
                return self._delete_delay_async(delay)
            else:
                return self._delete_delay_sync(delay)

        return self._state._webhook.delete_message(self.id)

    async def invites():
        raise NotImplementedError()

class Webhook(Hashable):
    """Represents a Discord webhook.

    Webhooks are a form to send messages to channels in Discord without a
    bot user or authentication.

    There are two main ways to use Webhooks. The first is through the ones
    received by the library such as :meth:`.Guild.webhooks` and
    :meth:`.TextChannel.webhooks`. The ones received by the library will
    automatically have an adapter bound using the library's HTTP session.
    Those webhooks will have :meth:`~.Webhook.send`, :meth:`~.Webhook.delete` and
    :meth:`~.Webhook.edit` as coroutines.

    The second form involves creating a webhook object manually without having
    it bound to a websocket connection using the :meth:`~.Webhook.from_url` or
    :meth:`~.Webhook.partial` classmethods. This form allows finer grained control
    over how requests are done, allowing you to mix async and sync code using either
    :doc:`aiohttp <aio:index>` or :doc:`req:index`.

    For example, creating a webhook from a URL and using :doc:`aiohttp <aio:index>`:

    .. code-block:: python3

        from discord import Webhook, AsyncWebhookAdapter
        import aiohttp

        async def foo():
            async with aiohttp.ClientSession() as session:
                webhook = Webhook.from_url('url-here', adapter=AsyncWebhookAdapter(session))
                await webhook.send('Hello World', username='Foo')

    Or creating a webhook from an ID and token and using :doc:`req:index`:

    .. code-block:: python3

        import requests
        from discord import Webhook, RequestsWebhookAdapter

        webhook = Webhook.partial(123456, 'abcdefg', adapter=RequestsWebhookAdapter())
        webhook.send('Hello World', username='Foo')

    .. container:: operations

        .. describe:: x == y

            Checks if two webhooks are equal.

        .. describe:: x != y

            Checks if two webhooks are not equal.

        .. describe:: hash(x)

            Returns the webhooks's hash.

    .. versionchanged:: 1.4
        Webhooks are now comparable and hashable.

    Attributes
    ------------
    id: :class:`int`
        The webhook's ID
    type: :class:`WebhookType`
        The type of the webhook.

        .. versionadded:: 1.3

    token: Optional[:class:`str`]
        The authentication token of the webhook. If this is ``None``
        then the webhook cannot be used to make requests.
    guild_id: Optional[:class:`int`]
        The guild ID this webhook is for.
    channel_id: Optional[:class:`int`]
        The channel ID this webhook is for.
    user: Optional[:class:`abc.User`]
        The user this webhook was created by. If the webhook was
        received without authentication then this will be ``None``.
    name: Optional[:class:`str`]
        The default name of the webhook.
    avatar: Optional[:class:`str`]
        The default avatar of the webhook.
    """

    __slots__ = ('id', 'type', 'guild_id', 'channel_id', 'user', 'name',
                 'avatar', 'token', '_state', '_adapter')

    def __init__(self, data, *, adapter, state=None):
        self.id = int(data['id'])
        self.type = try_enum(WebhookType, int(data['type']))
        self.channel_id = utils._get_as_snowflake(data, 'channel_id')
        self.guild_id = utils._get_as_snowflake(data, 'guild_id')
        self.name = data.get('name')
        self.avatar = data.get('avatar')
        self.token = data.get('token')
        self._state = state or _PartialWebhookState(adapter, self, parent=state)
        self._adapter = adapter
        self._adapter._prepare(self)

        user = data.get('user')
        if user is None:
            self.user = None
        elif state is None:
            self.user = BaseUser(state=None, data=user)
        else:
            self.user = User(state=state, data=user)

    def __repr__(self):
        return '<Webhook id=%r>' % self.id

    @property
    def url(self):
        """:class:`str` : Returns the webhook's url."""
        return 'https://discord.com/api/webhooks/{}/{}'.format(self.id, self.token)

    @classmethod
    def partial(cls, id, token, *, adapter):
        """Creates a partial :class:`Webhook`.

        Parameters
        -----------
        id: :class:`int`
            The ID of the webhook.
        token: :class:`str`
            The authentication token of the webhook.
        adapter: :class:`WebhookAdapter`
            The webhook adapter to use when sending requests. This is
            typically :class:`AsyncWebhookAdapter` for :doc:`aiohttp <aio:index>` or
            :class:`RequestsWebhookAdapter` for :doc:`req:index`.

        Returns
        --------
        :class:`Webhook`
            A partial :class:`Webhook`.
            A partial webhook is just a webhook object with an ID and a token.
        """

        if not isinstance(adapter, WebhookAdapter):
            raise TypeError('adapter must be a subclass of WebhookAdapter')

        data = {
            'id': id,
            'type': 1,
            'token': token
        }

        return cls(data, adapter=adapter)

    @classmethod
    def from_url(cls, url, *, adapter):
        """Creates a partial :class:`Webhook` from a webhook URL.

        Parameters
        ------------
        url: :class:`str`
            The URL of the webhook.
        adapter: :class:`WebhookAdapter`
            The webhook adapter to use when sending requests. This is
            typically :class:`AsyncWebhookAdapter` for :doc:`aiohttp <aio:index>` or
            :class:`RequestsWebhookAdapter` for :doc:`req:index`.

        Raises
        -------
        InvalidArgument
            The URL is invalid.

        Returns
        --------
        :class:`Webhook`
            A partial :class:`Webhook`.
            A partial webhook is just a webhook object with an ID and a token.
        """

        m = re.search(r'discord(?:app)?.com/api/webhooks/(?P<id>[0-9]{17,20})/(?P<token>[A-Za-z0-9\.\-\_]{60,68})', url)
        if m is None:
            raise InvalidArgument('Invalid webhook URL given.')
        data = m.groupdict()
        data['type'] = 1
        return cls(data, adapter=adapter)

    @classmethod
    def _as_follower(cls, data, *, channel, user):
        name = "{} #{}".format(channel.guild, channel)
        feed = {
            'id': data['webhook_id'],
            'type': 2,
            'name': name,
            'channel_id': channel.id,
            'guild_id': channel.guild.id,
            'user': {
                'username': user.name,
                'discriminator': user.discriminator,
                'id': user.id,
                'avatar': user.avatar
            }
        }

        session = channel._state.http._HTTPClient__session
        return cls(feed, adapter=AsyncWebhookAdapter(session=session))

    @classmethod
    def from_state(cls, data, state):
        session = state.http._HTTPClient__session
        return cls(data, adapter=AsyncWebhookAdapter(session=session), state=state)

    @property
    def guild(self):
        """Optional[:class:`Guild`]: The guild this webhook belongs to.

        If this is a partial webhook, then this will always return ``None``.
        """
        return self._state._get_guild(self.guild_id)

    @property
    def channel(self):
        """Optional[:class:`TextChannel`]: The text channel this webhook belongs to.

        If this is a partial webhook, then this will always return ``None``.
        """
        guild = self.guild
        return guild and guild.get_channel(self.channel_id)

    @property
    def created_at(self):
        """:class:`datetime.datetime`: Returns the webhook's creation time in UTC."""
        return utils.snowflake_time(self.id)

    @property
    def avatar_url(self):
        """:class:`Asset`: Returns an :class:`Asset` for the avatar the webhook has.

        If the webhook does not have a traditional avatar, an asset for
        the default avatar is returned instead.

        This is equivalent to calling :meth:`avatar_url_as` with the
        default parameters.
        """
        return self.avatar_url_as()

    def avatar_url_as(self, *, format=None, size=1024):
        """Returns an :class:`Asset` for the avatar the webhook has.

        If the webhook does not have a traditional avatar, an asset for
        the default avatar is returned instead.

        The format must be one of 'jpeg', 'jpg', or 'png'.
        The size must be a power of 2 between 16 and 1024.

        Parameters
        -----------
        format: Optional[:class:`str`]
            The format to attempt to convert the avatar to.
            If the format is ``None``, then it is equivalent to png.
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
        if self.avatar is None:
            # Default is always blurple apparently
            return Asset(self._state, '/embed/avatars/0.png')

        if not utils.valid_icon_size(size):
            raise InvalidArgument("size must be a power of 2 between 16 and 1024")

        format = format or 'png'

        if format not in ('png', 'jpg', 'jpeg'):
            raise InvalidArgument("format must be one of 'png', 'jpg', or 'jpeg'.")

        url = '/avatars/{0.id}/{0.avatar}.{1}?size={2}'.format(self, format, size)
        return Asset(self._state, url)

    def delete(self, *, reason=None):
        """|maybecoro|

        Deletes this Webhook.

        If the webhook is constructed with a :class:`RequestsWebhookAdapter` then this is
        not a coroutine.

        Parameters
        ------------
        reason: Optional[:class:`str`]
            The reason for deleting this webhook. Shows up on the audit log.

            .. versionadded:: 1.4

        Raises
        -------
        HTTPException
            Deleting the webhook failed.
        NotFound
            This webhook does not exist.
        Forbidden
            You do not have permissions to delete this webhook.
        InvalidArgument
            This webhook does not have a token associated with it.
        """
        if self.token is None:
            raise InvalidArgument('This webhook does not have a token associated with it')

        return self._adapter.delete_webhook(reason=reason)

    def edit(self, *, reason=None, **kwargs):
        """|maybecoro|

        Edits this Webhook.

        If the webhook is constructed with a :class:`RequestsWebhookAdapter` then this is
        not a coroutine.

        Parameters
        ------------
        name: Optional[:class:`str`]
            The webhook's new default name.
        avatar: Optional[:class:`bytes`]
            A :term:`py:bytes-like object` representing the webhook's new default avatar.
        reason: Optional[:class:`str`]
            The reason for editing this webhook. Shows up on the audit log.

            .. versionadded:: 1.4

        Raises
        -------
        HTTPException
            Editing the webhook failed.
        NotFound
            This webhook does not exist.
        InvalidArgument
            This webhook does not have a token associated with it.
        """
        if self.token is None:
            raise InvalidArgument('This webhook does not have a token associated with it')

        payload = {}

        try:
            name = kwargs['name']
        except KeyError:
            pass
        else:
            if name is not None:
                payload['name'] = str(name)
            else:
                payload['name'] = None

        try:
            avatar = kwargs['avatar']
        except KeyError:
            pass
        else:
            if avatar is not None:
                payload['avatar'] = utils._bytes_to_base64_data(avatar)
            else:
                payload['avatar'] = None

        return self._adapter.edit_webhook(reason=reason, **payload)

    def send(self, content=None, *, wait=False, username=None, avatar_url=None, tts=False,
                                    file=None, files=None, embed=None, embeds=None, allowed_mentions=None):
        """|maybecoro|

        Sends a message using the webhook.

        If the webhook is constructed with a :class:`RequestsWebhookAdapter` then this is
        not a coroutine.

        The content must be a type that can convert to a string through ``str(content)``.

        To upload a single file, the ``file`` parameter should be used with a
        single :class:`File` object.

        If the ``embed`` parameter is provided, it must be of type :class:`Embed` and
        it must be a rich embed type. You cannot mix the ``embed`` parameter with the
        ``embeds`` parameter, which must be a :class:`list` of :class:`Embed` objects to send.

        Parameters
        ------------
        content: :class:`str`
            The content of the message to send.
        wait: :class:`bool`
            Whether the server should wait before sending a response. This essentially
            means that the return type of this function changes from ``None`` to
            a :class:`WebhookMessage` if set to ``True``.
        username: :class:`str`
            The username to send with this message. If no username is provided
            then the default username for the webhook is used.
        avatar_url: Union[:class:`str`, :class:`Asset`]
            The avatar URL to send with this message. If no avatar URL is provided
            then the default avatar for the webhook is used.
        tts: :class:`bool`
            Indicates if the message should be sent using text-to-speech.
        file: :class:`File`
            The file to upload. This cannot be mixed with ``files`` parameter.
        files: List[:class:`File`]
            A list of files to send with the content. This cannot be mixed with the
            ``file`` parameter.
        embed: :class:`Embed`
            The rich embed for the content to send. This cannot be mixed with
            ``embeds`` parameter.
        embeds: List[:class:`Embed`]
            A list of embeds to send with the content. Maximum of 10. This cannot
            be mixed with the ``embed`` parameter.
        allowed_mentions: :class:`AllowedMentions`
            Controls the mentions being processed in this message.

            .. versionadded:: 1.4

        Raises
        --------
        HTTPException
            Sending the message failed.
        NotFound
            This webhook was not found.
        Forbidden
            The authorization token for the webhook is incorrect.
        InvalidArgument
            You specified both ``embed`` and ``embeds`` or the length of
            ``embeds`` was invalid or there was no token associated with
            this webhook.

        Returns
        ---------
        Optional[:class:`WebhookMessage`]
            The message that was sent.
        """

        payload = {}
        if self.token is None:
            raise InvalidArgument('This webhook does not have a token associated with it')
        if files is not None and file is not None:
            raise InvalidArgument('Cannot mix file and files keyword arguments.')
        if embeds is not None and embed is not None:
            raise InvalidArgument('Cannot mix embed and embeds keyword arguments.')

        if embeds is not None:
            if len(embeds) > 10:
                raise InvalidArgument('embeds has a maximum of 10 elements.')
            payload['embeds'] = [e.to_dict() for e in embeds]

        if embed is not None:
            payload['embeds'] = [embed.to_dict()]

        if content is not None:
            payload['content'] = str(content)

        payload['tts'] = tts
        if avatar_url:
            payload['avatar_url'] = str(avatar_url)
        if username:
            payload['username'] = username

        previous_mentions = getattr(self._state, 'allowed_mentions', None)

        if allowed_mentions:
            if previous_mentions is not None:
                payload['allowed_mentions'] = previous_mentions.merge(allowed_mentions).to_dict()
            else:
                payload['allowed_mentions'] = allowed_mentions.to_dict()
        elif previous_mentions is not None:
            payload['allowed_mentions'] = previous_mentions.to_dict()

        return self._adapter.execute_webhook(wait=wait, file=file, files=files, payload=payload)

    def execute(self, *args, **kwargs):
        """An alias for :meth:`~.Webhook.send`."""
        return self.send(*args, **kwargs)

    def edit_message(self, message_id, **fields):
        """|maybecoro|

        Edits a message owned by this webhook.

        This is a lower level interface to :meth:`WebhookMessage.edit` in case
        you only have an ID.

        .. versionadded:: 1.6

        Parameters
        ------------
        message_id: :class:`int`
            The message ID to edit.
        content: Optional[:class:`str`]
            The content to edit the message with or ``None`` to clear it.
        embeds: List[:class:`Embed`]
            A list of embeds to edit the message with.
        embed: Optional[:class:`Embed`]
            The embed to edit the message with. ``None`` suppresses the embeds.
            This should not be mixed with the ``embeds`` parameter.
        allowed_mentions: :class:`AllowedMentions`
            Controls the mentions being processed in this message.
            See :meth:`.abc.Messageable.send` for more information.

        Raises
        -------
        HTTPException
            Editing the message failed.
        Forbidden
            Edited a message that is not yours.
        InvalidArgument
            You specified both ``embed`` and ``embeds`` or the length of
            ``embeds`` was invalid or there was no token associated with
            this webhook.
        """

        payload = {}

        if self.token is None:
            raise InvalidArgument('This webhook does not have a token associated with it')

        try:
            content = fields['content']
        except KeyError:
            pass
        else:
            if content is not None:
                content = str(content)
            payload['content'] = content

        # Check if the embeds interface is being used
        try:
            embeds = fields['embeds']
        except KeyError:
            # Nope
            pass
        else:
            if embeds is None or len(embeds) > 10:
                raise InvalidArgument('embeds has a maximum of 10 elements')
            payload['embeds'] = [e.to_dict() for e in embeds]

        try:
            embed = fields['embed']
        except KeyError:
            pass
        else:
            if 'embeds' in payload:
                raise InvalidArgument('Cannot mix embed and embeds keyword arguments')

            if embed is None:
                payload['embeds'] = []
            else:
                payload['embeds'] = [embed.to_dict()]

        allowed_mentions = fields.pop('allowed_mentions', None)
        previous_mentions = getattr(self._state, 'allowed_mentions', None)

        if allowed_mentions:
            if previous_mentions is not None:
                payload['allowed_mentions'] = previous_mentions.merge(allowed_mentions).to_dict()
            else:
                payload['allowed_mentions'] = allowed_mentions.to_dict()
        elif previous_mentions is not None:
            payload['allowed_mentions'] = previous_mentions.to_dict()

        return self._adapter.edit_webhook_message(message_id, payload=payload)

    def delete_message(self, message_id):
        """|maybecoro|

        Deletes a message owned by this webhook.

        This is a lower level interface to :meth:`WebhookMessage.delete` in case
        you only have an ID.

        .. versionadded:: 1.6

        Parameters
        ------------
        message_id: :class:`int`
            The message ID to delete.

        Raises
        -------
        HTTPException
            Deleting the message failed.
        Forbidden
            Deleted a message that is not yours.
        """
        return self._adapter.delete_webhook_message(message_id)
