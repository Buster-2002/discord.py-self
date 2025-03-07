discord.py-self
===============

.. image:: https://img.shields.io/pypi/v/discord.py-self.svg
   :target: https://pypi.python.org/pypi/discord.py-self
   :alt: PyPI version info
.. image:: https://img.shields.io/pypi/pyversions/discord.py-self.svg
   :target: https://pypi.python.org/pypi/discord.py-self
   :alt: PyPI supported Python versions

A modern, easy to use, feature-rich, and async ready API wrapper for Discord's user API written in Python.

Fork Changes
------------

This has been moved to the `website <https://dolf.ml/discord.py-self>`_.

| **Credits:**
| - `arandomnewaccount <https://www.reddit.com/user/obviouslymymain123/>`_ for Discord API help.
| - `NoahCardoza <https://github.com/NoahCardoza/>`_ for the library `CaptchaHarvester <https://github.com/NoahCardoza/CaptchaHarvester/>`_, part of which is used here.
|

| **Note:**
| Automating user accounts is against the Discord ToS. This library is a proof of concept and I do not recommend using it. Do so at your own risk.

Key Features
-------------

- Modern Pythonic API using ``async`` and ``await``.
- Proper rate limit handling.
- Optimised in both speed and memory.
- Mostly compatible with the official ``discord.py``.

Installing
----------

**Python 3.8 or higher is required**

To install the library without full voice support, you can just run the following command:

.. code:: sh

    # Linux/macOS
    python3 -m pip install -U discord.py-self

    # Windows
    py -3 -m pip install -U discord.py-self

Otherwise to get voice support you should run the following command:

.. code:: sh

    # Linux/macOS
    python3 -m pip install -U "discord.py-self[voice]"

    # Windows
    py -3 -m pip install -U discord.py-self[voice]


To install the development version, do the following (not recommended):

.. code:: sh

    $ git clone --single-branch --branch development https://github.com/dolfies/discord.py-self
    $ cd discord.py-self
    $ python3 -m pip install -U .[voice]


The master branch (version 2.0) is not ready for use in *any* way. Do not use.


Optional Packages
~~~~~~~~~~~~~~~~~~

* PyNaCl (for voice support)

Please note that on Linux installing voice you must install the following packages via your favourite package manager (e.g. ``apt``, ``dnf``, etc) before running the above commands:

* libffi-dev (or ``libffi-devel`` on some systems)
* python-dev (e.g. ``python3.6-dev`` for Python 3.6)

Quick Example
--------------

.. code:: py

    import discord

    class MyClient(discord.Client):
        async def on_ready(self):
            print('Logged on as', self.user)

        async def on_message(self, message):
            # Only respond to ourselves
            if message.author != self.user:
                return

            if message.content == 'ping':
                await message.channel.send('pong')

    client = MyClient()
    client.run('token')

Bot Example
~~~~~~~~~~~~~

.. code:: py

    import discord
    from discord.ext import commands

    TOKEN = ''
    bot = commands.Bot(command_prefix='!', self_bot=True)

    @bot.command()
    async def ping(ctx):
        await ctx.send('pong')

    bot.run(TOKEN)

You can find more examples in the examples directory.

Links
------

- `Official Documentation <https://discordpy.readthedocs.io/en/latest/index.html>`_
- `Fork Documentation <https://dolf.ml/discord.py-self>`_
