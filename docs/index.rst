Welcome to discord-native-voice
================================

.. image:: /images/snake.svg
.. image:: /images/snake_dark.svg

discord-native-voice is a native voice extension for :resource:`discord.py-self <discordpy-self>`.
It plugs into the normal voice connection flow and adds voice receive, video, and Go Live stream support.

**Features:**

- Native voice send and receive
- Video send and receive for Discord's supported video codecs
- Go Live stream creation, watching, sending, and receiving
- Media sources for FFmpeg files, desktop capture, raw audio, and raw video
- Media sinks for callbacks, queues, WAV output, FFmpeg output, and muxed recordings
- RTX/NACK packet recovery, video simulcast, and DAVE encryption support

Getting started
-----------------

Is this your first time using the extension? This is the place to get started.

.. toctree::
  :maxdepth: 1

  quickstart

Manuals
---------

These pages describe the public API exposed by the extension.

.. toctree::
  :maxdepth: 1

  api

Getting help
--------------

If you're having trouble with something, these resources might help.

- Report bugs in the :resource:`issue tracker <issues>`.
- Read the :resource:`discord.py-self repository <discordpy-self>` for the base library.
