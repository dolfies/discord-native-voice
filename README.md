# discord-native-voice

A native voice extension for
[discord.py-self](https://github.com/dolfies/discord.py-self).

This package plugs into discord.py-self's normal voice connection flow and adds
voice receive, video, and Go Live stream support. It is meant to feel like a
drop-in replacement for the existing `VoiceClient`.

> [!IMPORTANT]
> This project is still under active development and may have issues.
>
> Currently, it is only compatible with discord.py-self.
> discord.py is not supported, but contributions towards changing this are welcome.

## Key Features

- Native voice send and receive
- Video send and receive for all Discord video codecs: AV1, H265, H264, VP8, and VP9
- Go Live stream creation, watching, sending, and receiving
- Media sources for FFmpeg files, desktop capture, raw audio, and raw video
- Media sinks for callbacks, queues, WAV output, FFmpeg output, and muxed audio/video recordings
- Codec advertisement based on local FFmpeg support
- RTX/NACK support for video packet recovery
- Video simulcast support
- End-to-end encryption support with DAVE
- Compatible with nearly all existing voice sinks

## Installing

**Python 3.10 or higher is required.**

Install discord.py-self first, then install this extension:

```sh
# Linux/macOS
python3 -m pip install -U discord.py-self <todo>

# Windows
py -3 -m pip install -U discord.py-self <todo>
```

Note that [PyNaCl](https://pypi.org/project/PyNaCl/) is not required when using this extension.

A Rust toolchain is required when building from source. FFmpeg is also
recommended, since the built-in file, desktop, and recording helpers use it.

For development tools:

```sh
python3 -m pip install -U .[dev]
```

## Quick Example

> [!NOTE]
> In the future, discord.py-self will automatically prioritize this extension's voice implementation when installed.

Connect with the native voice client:

```python
from discord.ext.native_voice import VoiceClient

voice = await channel.connect(cls=VoiceClient, self_video=True)
```

Play a media file with audio and video:

```python
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
```

Receive media with a callback:

```python
from discord.ext.native_voice import MediaPacket, VoiceClient

voice = await channel.connect(cls=VoiceClient)

def on_packet(packet: MediaPacket) -> None:
    print(packet.media_type, packet.codec, packet.user_id)

voice.listen(on_packet)
```

### Go Live Streams

Streams use a separate `StreamClient`, created from an existing native voice
connection.

```python
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
```

To watch another user's stream:

```python
stream = voice.streams[0]
stream_client = await stream.watch(cls=StreamClient)
```

### Configuration

Options that affect Discord's voice negotiation should be set before connecting.
Use `with_config()` to create a configured client class:

```python
from discord.ext.native_voice import VoiceClient

ConfiguredVoiceClient = VoiceClient.with_config(
    rtx=False,
    udp_qos=True,
    enable_debug_stats=True,
)

voice = await channel.connect(cls=ConfiguredVoiceClient, self_video=True)
```

## Media Helpers

The extension includes source and sink helpers for common media tasks:

- `FFmpegMediaSource` and `FFmpegVideoSource` for files, pipes, and desktop capture.
- `PCMMediaSource`, `AudioFrameSource`, and `VideoFrameSource` for already produced audio or video.
- `QueueSink` and `AsyncQueueSink` for application-managed receive loops.
- `WaveSink`, `MixedWaveSink`, `FFmpegSink`, and `FFmpegMuxSink` for recording.
- `MultiSink`, `PerUserSink`, and filter sinks for routing packets.

For advanced integrations, received media is exposed as `MediaPacket` objects
with RTP metadata and parsed RTP extensions.

## Docs

Docs are available at https://discord-native-voice.readthedocs.io/en/latest/index.html. Credits to [discord.py](https://github.com/Rapptz/discord.py) for the theme.

## Issues

For now, issues should be opened under this repository. Eventually, they will be migrated to the discord.py-self repository.

## Links

- [discord.py-self](https://github.com/dolfies/discord.py-self)
