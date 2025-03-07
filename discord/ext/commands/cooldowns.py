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
import time
from collections import deque

from discord.enums import Enum

from ...abc import PrivateChannel
from .errors import MaxConcurrencyReached

__all__ = (
    'BucketType',
    'Cooldown',
    'CooldownMapping',
    'MaxConcurrency',
)

class BucketType(Enum):
    default  = 0
    user     = 1
    guild    = 2
    channel  = 3
    member   = 4
    category = 5
    role     = 6

    def get_key(self, msg):
        if self is BucketType.user:
            return msg.author.id
        elif self is BucketType.guild:
            return (msg.guild or msg.author).id
        elif self is BucketType.channel:
            return msg.channel.id
        elif self is BucketType.member:
            return ((msg.guild and msg.guild.id), msg.author.id)
        elif self is BucketType.category:
            return (msg.channel.category or msg.channel).id
        elif self is BucketType.role:
            # we return the channel id of a private-channel as there are only roles in guilds
            # and that yields the same result as for a guild with only the @everyone role
            # NOTE: PrivateChannel doesn't actually have an id attribute but we assume we are
            # recieving a DMChannel or GroupChannel which inherit from PrivateChannel and do
            return (msg.channel if isinstance(msg.channel, PrivateChannel) else msg.author.top_role).id

    def __call__(self, msg):
        return self.get_key(msg)


class Cooldown:
    __slots__ = ('rate', 'per', 'type', '_window', '_tokens', '_last')

    def __init__(self, rate, per, type):
        self.rate = int(rate)
        self.per = float(per)
        self.type = type
        self._window = 0.0
        self._tokens = self.rate
        self._last = 0.0

        if not callable(self.type):
            raise TypeError('Cooldown type must be a BucketType or callable')

    def get_tokens(self, current=None):
        if not current:
            current = time.time()

        tokens = self._tokens

        if current > self._window + self.per:
            tokens = self.rate
        return tokens

    def get_retry_after(self, current=None):
        current = current or time.time()
        tokens = self.get_tokens(current)

        if tokens == 0:
            return self.per - (current - self._window)

        return 0.0

    def update_rate_limit(self, current=None):
        current = current or time.time()
        self._last = current

        self._tokens = self.get_tokens(current)

        # first token used means that we start a new rate limit window
        if self._tokens == self.rate:
            self._window = current

        # check if we are rate limited
        if self._tokens == 0:
            return self.per - (current - self._window)

        # we're not so decrement our tokens
        self._tokens -= 1

        # see if we got rate limited due to this token change, and if
        # so update the window to point to our current time frame
        if self._tokens == 0:
            self._window = current

    def reset(self):
        self._tokens = self.rate
        self._last = 0.0

    def copy(self):
        return Cooldown(self.rate, self.per, self.type)

    def __repr__(self):
        return '<Cooldown rate: {0.rate} per: {0.per} window: {0._window} tokens: {0._tokens}>'.format(self)

class CooldownMapping:
    def __init__(self, original):
        self._cache = {}
        self._cooldown = original

    def copy(self):
        ret = CooldownMapping(self._cooldown)
        ret._cache = self._cache.copy()
        return ret

    @property
    def valid(self):
        return self._cooldown is not None

    @classmethod
    def from_cooldown(cls, rate, per, type):
        return cls(Cooldown(rate, per, type))

    def _bucket_key(self, msg):
        return self._cooldown.type(msg)

    def _verify_cache_integrity(self, current=None):
        # we want to delete all cache objects that haven't been used
        # in a cooldown window. e.g. if we have a  command that has a
        # cooldown of 60s and it has not been used in 60s then that key should be deleted
        current = current or time.time()
        dead_keys = [k for k, v in self._cache.items() if current > v._last + v.per]
        for k in dead_keys:
            del self._cache[k]

    def get_bucket(self, message, current=None):
        if self._cooldown.type is BucketType.default:
            return self._cooldown

        self._verify_cache_integrity(current)
        key = self._bucket_key(message)
        if key not in self._cache:
            bucket = self._cooldown.copy()
            self._cache[key] = bucket
        else:
            bucket = self._cache[key]

        return bucket

    def update_rate_limit(self, message, current=None):
        bucket = self.get_bucket(message, current)
        return bucket.update_rate_limit(current)

class _Semaphore:
    """This class is a version of a semaphore.

    If you're wondering why asyncio.Semaphore isn't being used,
    it's because it doesn't expose the internal value. This internal
    value is necessary because I need to support both `wait=True` and
    `wait=False`.

    An asyncio.Queue could have been used to do this as well -- but it is
    not as inefficient since internally that uses two queues and is a bit
    overkill for what is basically a counter.
    """

    __slots__ = ('value', 'loop', '_waiters')

    def __init__(self, number):
        self.value = number
        self.loop = asyncio.get_event_loop()
        self._waiters = deque()

    def __repr__(self):
        return '<_Semaphore value={0.value} waiters={1}>'.format(self, len(self._waiters))

    def locked(self):
        return self.value == 0

    def is_active(self):
        return len(self._waiters) > 0

    def wake_up(self):
        while self._waiters:
            future = self._waiters.popleft()
            if not future.done():
                future.set_result(None)
                return

    async def acquire(self, *, wait=False):
        if not wait and self.value <= 0:
            # signal that we're not acquiring
            return False

        while self.value <= 0:
            future = self.loop.create_future()
            self._waiters.append(future)
            try:
                await future
            except:
                future.cancel()
                if self.value > 0 and not future.cancelled():
                    self.wake_up()
                raise

        self.value -= 1
        return True

    def release(self):
        self.value += 1
        self.wake_up()

class MaxConcurrency:
    __slots__ = ('number', 'per', 'wait', '_mapping')

    def __init__(self, number, *, per, wait):
        self._mapping = {}
        self.per = per
        self.number = number
        self.wait = wait

        if number <= 0:
            raise ValueError('max_concurrency \'number\' cannot be less than 1')

        if not isinstance(per, BucketType):
            raise TypeError('max_concurrency \'per\' must be of type BucketType not %r' % type(per))

    def copy(self):
        return self.__class__(self.number, per=self.per, wait=self.wait)

    def __repr__(self):
        return '<MaxConcurrency per={0.per!r} number={0.number} wait={0.wait}>'.format(self)

    def get_key(self, message):
        return self.per.get_key(message)

    async def acquire(self, message):
        key = self.get_key(message)

        try:
            sem = self._mapping[key]
        except KeyError:
            self._mapping[key] = sem = _Semaphore(self.number)

        acquired = await sem.acquire(wait=self.wait)
        if not acquired:
            raise MaxConcurrencyReached(self.number, self.per)

    async def release(self, message):
        # Technically there's no reason for this function to be async
        # But it might be more useful in the future
        key = self.get_key(message)

        try:
            sem = self._mapping[key]
        except KeyError:
            # ...? peculiar
            return
        else:
            sem.release()

        if sem.value >= self.number and not sem.is_active():
            del self._mapping[key]
