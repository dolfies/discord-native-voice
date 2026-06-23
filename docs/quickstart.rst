Quickstart
===========

Installing
------------

Python 3.10 or higher is required.

Install :resource:`discord.py-self <discordpy-self>` first, then install this extension:

.. code-block:: sh

    python -m pip install -U discord.py-self discord-native-voice

PyNaCl is not required when using this extension. FFmpeg is recommended because the included file, desktop, and recording helpers use it.

Connecting
------------

Pass :class:`~discord.ext.native_voice.VoiceClient` to ``connect``:

.. code-block:: python3

    from discord.ext.native_voice import VoiceClient

    voice = await channel.connect(cls=VoiceClient)

To send camera video, connect with ``self_video=True``:

.. code-block:: python3

    voice = await channel.connect(cls=VoiceClient, self_video=True)

Playing Media
---------------

Use :class:`~discord.ext.native_voice.FFmpegMediaSource` for files containing audio and video:

.. code-block:: python3

    from discord.ext.native_voice import FFmpegMediaSource, VoiceClient

    voice = await channel.connect(cls=VoiceClient, self_video=True)
    source = FFmpegMediaSource.from_file(
        'clip.mp4',
        'H264',
        width=1280,
        height=720,
        fps=30,
        bitrate=4_000_000,
    )

    voice.play(source)

Receiving Media
-----------------

Use ``listen`` with a sink or callback to receive media packets:

.. code-block:: python3

    from discord.ext.native_voice import MediaPacket, VoiceClient

    voice = await channel.connect(cls=VoiceClient)

    def on_packet(packet: MediaPacket) -> None:
        print(packet.media_type, packet.codec, packet.user_id)

    voice.listen(on_packet)

Go Live Streams
-----------------

Streams use :class:`~discord.ext.native_voice.StreamClient`, created from an existing native voice connection:

.. code-block:: python3

    from discord.ext.native_voice import FFmpegMediaSource, StreamClient, VoiceClient

    voice = await channel.connect(cls=VoiceClient)
    stream = await voice.create_stream(cls=StreamClient)

    # Simplified example, reality is slightly more complicated
    source = FFmpegMediaSource.from_desktop(
        'H264',
        width=1920,
        height=1080,
        fps=30,
        bitrate=6_000_000,
    )

    stream.play(source)

To watch another user's stream, use the stream object exposed by discord.py-self:

.. code-block:: python3

    stream = voice.streams[0]
    stream_client = await stream.watch(cls=StreamClient)
