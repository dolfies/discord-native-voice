API Reference
=============

.. currentmodule:: discord.ext.native_voice

Clients
---------

.. attributetable:: discord.ext.native_voice.VoiceClient

.. autoclass:: VoiceClient()
    :members:
    :inherited-members:
    :exclude-members: supports_video, get_experiments, connect, cleanup, on_voice_state_update, on_voice_server_update

.. attributetable:: discord.ext.native_voice.StreamClient

.. autoclass:: StreamClient()
    :members:
    :inherited-members:
    :exclude-members: supports_video, get_experiments, connect, cleanup, on_voice_state_update, on_voice_server_update

Media Sources
---------------

.. autoclass:: MediaSource()
    :members:
    :inherited-members:

.. autoclass:: AudioMediaSource
    :members:

.. autoclass:: PCMMediaSource
    :members:

.. autoclass:: AudioFrameSource
    :members:

.. autoclass:: PCMAudio
    :members:

.. autoclass:: FFmpegAudio
    :members:

.. autoclass:: FFmpegPCMAudio
    :members:

.. autoclass:: FFmpegOpusAudio
    :members:

.. autoclass:: VideoFrameSource
    :members:

.. autoclass:: EncodedVideoSource
    :members:

.. autoclass:: SimulcastVideoSource
    :members:
    :exclude-members: supports_simulcast, on_media_sink_wants

.. autoclass:: FFmpegVideoSource
    :members:

.. autoclass:: FFmpegMediaSource
    :members:

.. autoclass:: FFmpegSimulcastVideoSource
    :members:

.. autoclass:: MultiMediaSource
    :members:
    :exclude-members: supports_simulcast, on_media_sink_wants

.. autoclass:: CompositeMediaSource
    :members:
    :exclude-members: on_media_sink_wants

.. autoclass:: MediaVolumeTransformer
    :members:

Media Sinks
-------------

.. autoclass:: MediaSink()
    :members:

.. autoclass:: BasicSink
    :members:

.. autoclass:: QueueSink
    :members:

.. autoclass:: AsyncQueueSink
    :members:

.. autoclass:: MultiSink
    :members:

.. autoclass:: PerUserSink
    :members:

.. autoclass:: WaveSink
    :members:

.. autoclass:: MixedWaveSink
    :members:

.. autoclass:: FFmpegSink
    :members:

.. autoclass:: FFmpegMuxSink
    :members:

.. autoclass:: EncodedVideoSink
    :members:

.. autoclass:: PCMDecodeSink
    :members:

.. autoclass:: SilenceFillSink
    :members:

.. autoclass:: MediaSinkVolumeTransformer
    :members:

.. autoclass:: ConditionalFilter
    :members:

.. autoclass:: TimedFilter
    :members:

.. autoclass:: UserFilter
    :members:

.. autoclass:: MediaFilter
    :members:

Data Objects
--------------

.. autoclass:: MediaPacket()
    :members:

.. autoclass:: MediaSinkWants()
    :members:

.. autoclass:: VideoConfig
    :members:

.. autoclass:: VideoFrame
    :members:

.. autoclass:: VideoProbeInfo
    :members:

.. autoclass:: VideoTranscoderConfig
    :members:

.. autoclass:: RTPExtension()
    :members:

.. autoclass:: RTPPacket()
    :members:

.. autoclass:: RTPSendStats()
    :members:

.. autoclass:: AudioSendStats()
    :members:

.. autoclass:: RTCPReceiverReport()
    :members:

.. autoclass:: MediaPlayerStats()
    :members:
