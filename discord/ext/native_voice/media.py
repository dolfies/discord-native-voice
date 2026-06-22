from __future__ import annotations

import abc
import asyncio
import concurrent.futures
import io
import json
import logging
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, BinaryIO, IO, TYPE_CHECKING, TypedDict, TypeVar

import discord
from discord.flags import SpeakingFlags
from discord.opus import Decoder as OpusDecoder, Encoder as OpusEncoder
from discord.player import CREATE_NO_WINDOW, AudioSource, FFmpegAudio, FFmpegOpusAudio, FFmpegPCMAudio, PCMAudio
from discord.utils import MISSING, SequenceProxy
from discord.voice_media import RTP_AUDIO_LEVEL_SILENCE, VoiceStream

from ._native_voice import DesktopFrameSource, pcm16_add, pcm16_mix, pcm16_mul
from .rtp import RTPExtension, RTPPacket, _rtp_timestamp_delta, _sequence_delta
from .transcoding import (
    _NVENC_ENCODERS,
    _NVENC_H26X_ENCODERS,
    _VIDEO_DEFAULT_ENCODERS,
    _VIDEO_ENCODER_PRIORITY,
    _VIDEO_OUTPUT_FORMAT,
    _VIDEO_SOFTWARE_ENCODER_PRIORITY,
    VideoTranscoderConfig,
    _argv_options,
    _coerce_video_codec,
    _ffmpeg_encoder_is_usable,
    _ffmpeg_video_codec_names,
    _native_video_bitrate,
    _resolve_transcoder_codec,
    _video_bitstream_filter_args,
    _video_codec_or_none,
    _video_encoder_args,
    _video_filtergraph,
)

if sys.platform == 'win32':
    import msvcrt

if TYPE_CHECKING:
    from discord.abc import Snowflake
    from discord.types.voice import MediaSinkWants as MediaSinkWantsPayload


FFmpegStderr = IO[bytes] | int | None
log = logging.getLogger(__name__)
T = TypeVar('T', bound=AudioSource)


@dataclass(frozen=True, slots=True)
class VideoProbeInfo:
    """Metadata discovered for a video input.

    Attributes
    ----------
    width: Optional[:class:`int`]
        The video width in pixels.
    height: Optional[:class:`int`]
        The video height in pixels.
    fps: Optional[:class:`int`]
        The frame rate, rounded to an integer.
    bitrate: Optional[:class:`int`]
        The video bitrate in bits per second.
    codec: Optional[:class:`str`]
        The Discord video codec name, if it could be mapped.
    """

    width: int | None = None
    height: int | None = None
    fps: int | None = None
    bitrate: int | None = None
    codec: str | None = None


class VideoProbePayload(TypedDict, total=False):
    width: int | str | None
    height: int | str | None
    fps: int | float | str | None
    bitrate: int | str | None
    codec: str | None


VideoProbeResult = (
    VideoProbeInfo
    | tuple[int | None, int | None, int | None, int | None]
    | tuple[int | None, int | None, int | None, int | None, str | None]
    | VideoProbePayload
)
VideoProbeMethod = str | Callable[[str, str], VideoProbeResult] | None


__all__ = (
    'AsyncQueueSink',
    'AudioFrameSource',
    'AudioMediaSource',
    'AudioSource',
    'BasicSink',
    'CompositeMediaSource',
    'ConditionalFilter',
    'EncodedVideoSink',
    'EncodedVideoSource',
    'FFmpegAudio',
    'FFmpegMediaSource',
    'FFmpegMuxSink',
    'FFmpegOpusAudio',
    'FFmpegPCMAudio',
    'FFmpegSimulcastVideoSource',
    'FFmpegSink',
    'FFmpegVideoSource',
    'MediaFilter',
    'MediaPacket',
    'MediaSink',
    'MediaSinkVolumeTransformer',
    'MediaSinkWants',
    'MediaSource',
    'MediaVolumeTransformer',
    'MixedWaveSink',
    'MultiMediaSource',
    'MultiSink',
    'PCMAudio',
    'PCMDecodeSink',
    'PCMMediaSource',
    'PCMVolumeTransformer',
    'PerUserSink',
    'QueueSink',
    'RTPExtension',
    'RTPPacket',
    'SilenceFillSink',
    'SimulcastVideoSource',
    'TimedFilter',
    'UserFilter',
    'VideoConfig',
    'VideoFrame',
    'VideoFrameSource',
    'VideoProbeInfo',
    'VideoProbePayload',
    'VideoTranscoderConfig',
    'WaveSink',
)


@dataclass(frozen=True, slots=True)
class MediaPacket:
    """Represents one decoded receive-side media packet.

    For video, ``payload`` is a full depacketized encoded frame. The RTP fields,
    ``raw``, and extension fields correspond to the RTP packet that completed
    that frame.

    Attributes
    ----------
    media_type: :class:`str`
        The media type, currently ``"audio"`` or ``"video"``.
    codec: :class:`str`
        The decoded codec name.
    payload: :class:`bytes`
        The Opus packet, PCM packet, or full encoded video frame.
    payload_type: :class:`int`
        The media RTP payload type.
    marker: :class:`bool`
        Whether the RTP marker bit was set.
    sequence: :class:`int`
        The RTP sequence number.
    timestamp: :class:`int`
        The RTP timestamp.
    ssrc: :class:`int`
        The normalized media SSRC.
    user_id: Optional[:class:`int`]
        The mapped user ID, if Discord has identified the SSRC.
    raw: :class:`bytes`
        The raw encrypted RTP packet received from the socket.
    extension_payload: :class:`bytes`
        The decrypted one-byte RTP extension payload bytes.
    rtp_extended: :class:`bool`
        Whether the RTP extension bit was set.
    rtp_extensions: Tuple[:class:`RTPExtension`, ...]
        Parsed one-byte RTP extension elements.
    rtp_packets: Tuple[:class:`RTPPacket`, ...]
        Parsed RTP packets that produced this media packet.
    received_at: Optional[:class:`float`]
        Local monotonic timestamp for when this packet/frame was decoded.
    rtcp_time: Optional[:class:`float`]
        Unix timestamp mapped from RTCP sender reports or RTP absolute send
        time, if either was available.
    speaking_flags: Optional[:class:`SpeakingFlags`]
        The decoded Discord speaking flags, if this is an audio packet.
    audio_level: Optional[:class:`int`]
        Decoded RTP audio-level extension value, where ``0`` is loudest and
        ``127`` is silence.
    audio_voice_activity: Optional[:class:`bool`]
        RTP audio-level voice activity bit, if present.
    """

    media_type: str
    codec: str
    payload: bytes
    payload_type: int
    marker: bool
    sequence: int
    timestamp: int
    ssrc: int
    user_id: int | None
    raw: bytes
    extension_payload: bytes = b''
    rtp_extended: bool = False
    rtp_extensions: tuple[RTPExtension, ...] = ()
    rtp_packets: tuple[RTPPacket, ...] = ()
    received_at: float | None = None
    rtcp_time: float | None = None
    speaking_flags: SpeakingFlags | None = None
    audio_level: int | None = None
    audio_voice_activity: bool | None = None

    def replace(self, **changes: Any) -> MediaPacket:
        return replace(self, **changes)


@dataclass(slots=True)
class _SilenceTrack:
    packet: MediaPacket
    next_due: float
    sequence: int
    timestamp: int
    emitted_for: float = 0.0


@dataclass(frozen=True, slots=True)
class VideoFrame:
    """Represents one encoded video frame yielded by a :class:`MediaSource`.

    Attributes
    ----------
    data: :class:`bytes`
        The encoded frame bytes for the selected video codec.
    frame_time_ms: :class:`float`
        The duration of the frame in milliseconds.
    """

    data: bytes
    frame_time_ms: float = 33.0


@dataclass(slots=True)
class _PipeStats:
    read_count: int = 0
    read_empty_count: int = 0
    read_total_ms: float = 0.0
    read_max_ms: float = 0.0
    write_count: int = 0
    write_total_ms: float = 0.0
    write_max_ms: float = 0.0
    bytes_written: int = 0

    def record_read(self, elapsed_ms: float, *, empty: bool) -> None:
        self.read_count += 1
        self.read_total_ms += elapsed_ms
        self.read_max_ms = max(self.read_max_ms, elapsed_ms)
        if empty:
            self.read_empty_count += 1

    def record_write(self, elapsed_ms: float, size: int) -> None:
        self.write_count += 1
        self.write_total_ms += elapsed_ms
        self.write_max_ms = max(self.write_max_ms, elapsed_ms)
        self.bytes_written += size

    def snapshot(self) -> dict[str, int | float]:
        return {
            'pipeReadCount': self.read_count,
            'pipeReadEmptyCount': self.read_empty_count,
            'pipeReadMeanMs': self.read_total_ms / max(1, self.read_count),
            'pipeReadMaxMs': self.read_max_ms,
            'pipeWriteCount': self.write_count,
            'pipeWriteMeanMs': self.write_total_ms / max(1, self.write_count),
            'pipeWriteMaxMs': self.write_max_ms,
            'pipeBytesWritten': self.bytes_written,
        }


@dataclass(slots=True)
class _EncodedFrameStats:
    read_count: int = 0
    read_empty_count: int = 0
    read_total_ms: float = 0.0
    read_max_ms: float = 0.0
    bytes_read: int = 0

    def record_read(self, elapsed_ms: float, frame_size: int) -> None:
        self.read_count += 1
        self.read_total_ms += elapsed_ms
        self.read_max_ms = max(self.read_max_ms, elapsed_ms)
        if frame_size:
            self.bytes_read += frame_size
        else:
            self.read_empty_count += 1

    def snapshot(self) -> dict[str, int | float]:
        return {
            'encodedFrameReadCount': self.read_count,
            'encodedFrameReadEmptyCount': self.read_empty_count,
            'encodedFrameReadMeanMs': self.read_total_ms / max(1, self.read_count),
            'encodedFrameReadMaxMs': self.read_max_ms,
            'encodedFrameBytesRead': self.bytes_read,
        }


@dataclass(frozen=True, slots=True)
class MediaSinkWants:
    """Represents a Discord media sink wants payload.

    Attributes
    ----------
    wants: Dict[:class:`int`, :class:`int`]
        Per-SSRC quality requests. Positive values select the requested send
        quality; ``0`` means the receiver does not want that SSRC forwarded.
    any: Optional[:class:`int`]
        The fallback quality request for otherwise unspecified streams.
    pixel_counts: Dict[:class:`int`, :class:`float`]
        Per-SSRC preferred pixel counts.
    """

    wants: dict[int, int] = field(default_factory=dict)
    any: int | None = None
    pixel_counts: dict[int, float] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: MediaSinkWantsPayload) -> MediaSinkWants:
        any_want = payload.get('any')

        wants: dict[int, int] = {}
        for key, value in payload.items():
            if key in ('any', 'pixelCounts'):
                continue
            if isinstance(value, Mapping):
                raise TypeError(f'media sink wants value for {key!r} must be numeric')
            if not isinstance(value, (int, float, str)):
                raise TypeError(f'media sink wants value for {key!r} must be numeric')
            wants[int(key)] = int(value)

        pixel_counts_payload = payload.get('pixelCounts')
        pixel_counts = (
            {int(key): float(value) for key, value in pixel_counts_payload.items()}
            if isinstance(pixel_counts_payload, Mapping)
            else {}
        )
        return cls(wants=wants, any=int(any_want) if any_want is not None else None, pixel_counts=pixel_counts)


VideoFrameInput = VideoFrame | bytes | bytearray | memoryview
AudioFrameInput = bytes | bytearray | memoryview
PreviewFrameInput = bytes | bytearray | memoryview
_H26X_PARAMETER_SET_TYPES = {
    'H264': frozenset({7, 8}),
    'H265': frozenset({32, 33, 34}),
}
_H26X_KEYFRAME_TYPES = {
    'H264': frozenset({5}),
    'H265': frozenset(range(16, 22)),
}


@dataclass(frozen=True, slots=True)
class VideoConfig:
    """Playback parameters for a video-capable :class:`MediaSource`.

    This lets :meth:`discord.ext.native_voice.VoiceClient.play` start video
    automatically when the source knows its own dimensions and codec.

    Attributes
    ----------
    codec: :class:`str`
        The encoded video codec name, such as ``H264``.
    width: :class:`int`
        The encoded video width in pixels.
    height: :class:`int`
        The encoded video height in pixels.
    fps: :class:`int`
        The target frame rate.
    bitrate: :class:`int`
        The target video bitrate in bits per second.
    """

    codec: str
    width: int
    height: int
    fps: int = 30
    bitrate: int = 0


def _unique_sources(sources: Iterable[T | None]) -> Iterator[T]:
    seen: set[int] = set()
    for source in sources:
        if source is None:
            continue
        source_id = id(source)
        if source_id in seen:
            continue
        seen.add(source_id)
        yield source


class MediaSource(AudioSource):
    """An audio source that can also yield encoded video frames.

    Attributes
    ----------
    video_realtime: :class:`bool`
        Whether video frame pacing should track wall-clock capture timing.
    video_retry_delay: :class:`float`
        Delay used before retrying video reads that temporarily return no frames.
    video_catchup_frames: :class:`int`
        Maximum number of video frames to send in one player tick while catching up.
    """

    video_realtime = False
    video_retry_delay = 0.02
    video_catchup_frames = 4

    def has_audio(self) -> bool:
        """:class:`bool`: Whether this source currently has audio to read."""
        return True

    def read_video(self) -> VideoFrame | None:
        """Read one encoded video frame for the primary stream.

        Returns
        -------
        Optional[:class:`VideoFrame`]
            The next encoded video frame, if one is available.
        """
        return None

    def read_video_streams(self, streams: Sequence[VoiceStream]) -> Mapping[str, VideoFrame] | None:
        """Read encoded video frames for the active outbound simulcast streams.

        The default implementation preserves the single-stream
        :meth:`read_video` behaviour and returns a frame for the first active
        stream only. Sources that can encode multiple simulcast outputs should
        override this and return RID-keyed frames for each stream they are able
        to produce on this tick.

        Parameters
        ----------
        streams: List[:class:`discord.VoiceStream`]
            The active outbound video streams selected by the voice client.

        Returns
        -------
        Optional[Mapping[:class:`str`, :class:`VideoFrame`]]
            A mapping of RTP stream ID to encoded frame, ``None`` when the video
            lane is finished, or an empty mapping when no frame is ready yet.
        """
        if not streams:
            return {}

        frame = self.read_video()
        if frame is None:
            return None
        return {streams[0].rid: frame}

    def read_preview(self) -> PreviewFrameInput | None:
        """Read image preview bytes for a Go Live stream preview.

        Returns
        -------
        Optional[Union[:class:`bytes`, :class:`bytearray`, :class:`memoryview`]]
            Encoded image bytes for a stream preview, if available.
        """
        return None

    def has_video(self) -> bool:
        """:class:`bool`: Whether this source currently has video to read."""
        return False

    def supports_simulcast(self) -> bool:
        """:class:`bool`: Whether :meth:`read_video_streams` can emit multiple video outputs."""
        return False

    @property
    def video_config(self) -> VideoConfig | None:
        """Optional[:class:`VideoConfig`]: Video parameters known by this source."""
        return None

    def on_media_sink_wants(self, wants: MediaSinkWants) -> None:
        """Handle a remote media sink wants update for this source.

        The default implementation does nothing. Adaptive sources can override
        this to adjust their encoder, bitrate, resolution, or selected output
        stream when Discord asks for a different quality.

        Parameters
        ----------
        wants: :class:`MediaSinkWants`
            The remote quality requests sent by Discord.
        """

    def is_finished(self) -> bool:
        """:class:`bool`: Whether this source has no more media to produce."""
        return False


class _VideoSourceDelegate:
    def _active_video_source(self) -> MediaSource | None:
        return None

    def has_video(self) -> bool:
        source = self._active_video_source()
        return source is not None and source.has_video()

    def read_video(self) -> VideoFrame | None:
        source = self._active_video_source()
        return None if source is None else source.read_video()

    def read_video_streams(self, streams: Sequence[VoiceStream]) -> Mapping[str, VideoFrame] | None:
        source = self._active_video_source()
        return None if source is None else source.read_video_streams(streams)

    def read_preview(self) -> PreviewFrameInput | None:
        source = self._active_video_source()
        return None if source is None else source.read_preview()

    def supports_simulcast(self) -> bool:
        source = self._active_video_source()
        return source is not None and source.supports_simulcast()

    @property
    def video_config(self) -> VideoConfig | None:
        source = self._active_video_source()
        return None if source is None else source.video_config

    def on_media_sink_wants(self, wants: MediaSinkWants) -> None:
        source = self._active_video_source()
        if source is not None:
            source.on_media_sink_wants(wants)


class AudioMediaSource(MediaSource):
    """Wraps an existing :class:`discord.AudioSource` as a media source.

    This keeps first-party d.py audio sources, such as
    :class:`discord.PCMAudio`, :class:`discord.FFmpegPCMAudio`, and
    :class:`discord.FFmpegOpusAudio`, usable in unified media pipelines.

    Parameters
    ----------
    original: :class:`discord.AudioSource`
        The audio source to wrap.

    Attributes
    ----------
    original: :class:`discord.AudioSource`
        The wrapped audio source.
    """

    def __init__(self, original: AudioSource, /) -> None:
        if not isinstance(original, AudioSource):
            raise TypeError(f'expected AudioSource not {original.__class__.__name__}')

        self.original = original
        self._finished = False
        self._cleaned = False

    def has_audio(self) -> bool:
        return not self._cleaned and not self._finished

    def read(self) -> bytes:
        if self._cleaned or self._finished:
            return b''

        data = self.original.read()
        if not data:
            self._finished = True
            return b''
        return bytes(data)

    def is_opus(self) -> bool:
        return not self._cleaned and self.original.is_opus()

    def is_finished(self) -> bool:
        return self._cleaned or self._finished

    def cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        self._finished = True
        self.original.cleanup()


class CompositeMediaSource(_VideoSourceDelegate, MediaSource):
    """Combines separate audio and video sources into one media source.

    Parameters
    ----------
    audio: Optional[:class:`discord.AudioSource`]
        The source used for audio frames.
    video: Optional[:class:`MediaSource`]
        The source used for video frames and stream previews.

    Attributes
    ----------
    audio: Optional[:class:`discord.AudioSource`]
        The source used for audio frames.
    video: Optional[:class:`MediaSource`]
        The source used for video frames and stream previews.
    """

    def __init__(self, *, audio: AudioSource | None = None, video: MediaSource | None = None) -> None:
        if audio is None and video is None:
            raise TypeError('audio or video must be provided')

        self.audio = audio
        self.video = video
        self._cleaned = False
        self._audio_finished = False

    def _audio_done(self) -> bool:
        if self.audio is None or self._audio_finished:
            return True
        return isinstance(self.audio, MediaSource) and self.audio.is_finished()

    def _active_video_source(self) -> MediaSource | None:
        video = self.video
        if self._cleaned or video is None or video.is_finished():
            return None
        return video

    def _children(self) -> Iterator[AudioSource]:
        yield from _unique_sources((self.audio, self.video))

    def has_audio(self) -> bool:
        return not self._cleaned and not self._audio_done()

    def read(self) -> bytes:
        if self._cleaned or self.audio is None or self._audio_done():
            return b''

        data = self.audio.read()
        if not data:
            self._audio_finished = True
            return b''
        return data

    def is_opus(self) -> bool:
        return self.audio.is_opus() if not self._cleaned and self.audio is not None and not self._audio_done() else False

    def on_media_sink_wants(self, wants: MediaSinkWants) -> None:
        for source in self._children():
            if isinstance(source, MediaSource):
                source.on_media_sink_wants(wants)

    def is_finished(self) -> bool:
        if self._cleaned:
            return True
        audio_done = self._audio_done()
        video_done = self._active_video_source() is None
        return audio_done and video_done

    def cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        self._audio_finished = True
        for source in self._children():
            source.cleanup()


class MultiMediaSource(MediaSource):
    """Combines multiple sources into one playable media source.

    The source mixes multiple PCM audio inputs into one audio lane. Opus audio is
    supported only when it is the sole audio input, since encoded Opus cannot be
    mixed without decoding first. Video uses the first video-capable source that
    yields a frame.

    Parameters
    ----------
    sources: List[:class:`discord.AudioSource`]
        The sources to combine.

    Attributes
    ----------
    sources: List[:class:`discord.AudioSource`]
        The sources being combined.
    """

    def __init__(self, sources: Sequence[AudioSource], /) -> None:
        if not sources:
            raise TypeError('sources must not be empty')

        self._sources = list(sources)
        self._audio_sources = [
            source for source in self._sources if not isinstance(source, MediaSource) or source.has_audio()
        ]
        self._video_sources = [source for source in self._sources if isinstance(source, MediaSource) and source.has_video()]
        self._opus_audio_sources = [source for source in self._audio_sources if source.is_opus()]
        if self._opus_audio_sources and len(self._audio_sources) != 1:
            raise discord.ClientException('Opus audio cannot be mixed with other media sources')
        self._finished_audio_sources: set[int] = set()
        self._finished_video_sources: set[int] = set()
        self._cleaned = False

    def _audio_source_done(self, source: AudioSource) -> bool:
        if id(source) in self._finished_audio_sources:
            return True
        return isinstance(source, MediaSource) and source.is_finished()

    def _video_source_done(self, source: MediaSource) -> bool:
        return id(source) in self._finished_video_sources or source.is_finished()

    def _active_audio_sources(self) -> Iterator[AudioSource]:
        return (source for source in self._audio_sources if not self._audio_source_done(source))

    def _active_video_sources(self) -> Iterator[MediaSource]:
        return (source for source in self._video_sources if not self._video_source_done(source))

    @property
    def sources(self) -> Sequence[AudioSource]:
        """Sequence[:class:`discord.AudioSource`]: The sources being combined."""
        return SequenceProxy(self._sources)

    def has_audio(self) -> bool:
        return next(self._active_audio_sources(), None) is not None

    def has_video(self) -> bool:
        return next(self._active_video_sources(), None) is not None

    def read(self) -> bytes:
        audio_sources = tuple(self._active_audio_sources())
        if not audio_sources:
            return b''

        if self.is_opus():
            source = self._opus_audio_sources[0]
            data = source.read()
            if not data:
                self._finished_audio_sources.add(id(source))
            return data

        chunks = []
        for source in audio_sources:
            chunk = source.read()
            if chunk:
                chunks.append(chunk)
            else:
                self._finished_audio_sources.add(id(source))
        if not chunks:
            return b''

        return pcm16_mix(chunks)

    def is_opus(self) -> bool:
        return any(not self._audio_source_done(source) for source in self._opus_audio_sources)

    def read_video(self) -> VideoFrame | None:
        for source in self._active_video_sources():
            frame = source.read_video()
            if frame is not None:
                return frame
            if source.is_finished():
                self._finished_video_sources.add(id(source))
        return None

    def read_video_streams(self, streams: Sequence[VoiceStream]) -> Mapping[str, VideoFrame] | None:
        for source in self._active_video_sources():
            frames = source.read_video_streams(streams)
            if frames is not None:
                return frames
            if source.is_finished():
                self._finished_video_sources.add(id(source))
        return None

    def read_preview(self) -> PreviewFrameInput | None:
        for source in self._active_video_sources():
            preview = source.read_preview()
            if preview:
                return preview
        return None

    @property
    def video_config(self) -> VideoConfig | None:
        """Optional[:class:`VideoConfig`]: Video configuration from the active video source."""
        for source in self._active_video_sources():
            config = source.video_config
            if config is not None:
                return config
        return None

    def supports_simulcast(self) -> bool:
        for source in self._active_video_sources():
            return source.supports_simulcast()
        return False

    def on_media_sink_wants(self, wants: MediaSinkWants) -> None:
        for source in _unique_sources(self._sources):
            if not isinstance(source, MediaSource):
                continue
            source.on_media_sink_wants(wants)

    def is_finished(self) -> bool:
        audio_done = all(self._audio_source_done(source) for source in self._audio_sources)
        video_done = all(self._video_source_done(source) for source in self._video_sources)
        return audio_done and video_done

    def cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        self._finished_audio_sources.update(id(source) for source in self._audio_sources)
        self._finished_video_sources.update(id(source) for source in self._video_sources)
        for source in _unique_sources(self._sources):
            source.cleanup()


class MediaVolumeTransformer(_VideoSourceDelegate, MediaSource):
    """Adjusts PCM audio volume while preserving video from another source.

    Parameters
    ----------
    original: :class:`discord.AudioSource`
        The source to wrap.
    volume: :class:`float`
        The initial volume multiplier.

    Attributes
    ----------
    original: :class:`discord.AudioSource`
        The wrapped source.
    volume: :class:`float`
        The audio volume multiplier.
    """

    def __init__(self, original: AudioSource, volume: float = 1.0) -> None:
        if not isinstance(original, AudioSource):
            raise TypeError(f'expected AudioSource not {original.__class__.__name__}')

        source = original if isinstance(original, MediaSource) else AudioMediaSource(original)
        if source.has_audio() and source.is_opus():
            raise discord.ClientException('MediaSource audio must not be Opus encoded')

        self.original = original
        self._media_source = source
        self.volume = volume
        self._cleaned = False

    def _active_source(self) -> MediaSource | None:
        return None if self._cleaned else self._media_source

    def _active_video_source(self) -> MediaSource | None:
        return self._active_source()

    @property
    def volume(self) -> float:
        """:class:`float`: The audio volume multiplier."""
        return self._volume

    @volume.setter
    def volume(self, value: float) -> None:
        """:class:`float`: Set the audio volume multiplier."""
        self._volume = max(value, 0.0)

    def has_audio(self) -> bool:
        source = self._active_source()
        return source is not None and source.has_audio()

    def read(self) -> bytes:
        source = self._active_source()
        if source is None:
            return b''
        data = source.read()
        return pcm16_mul(data, min(self._volume, 2.0)) if data else data

    def is_opus(self) -> bool:
        return False

    def is_finished(self) -> bool:
        source = self._active_source()
        return source is None or source.is_finished()

    def cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        self._media_source.cleanup()


PCMVolumeTransformer = MediaVolumeTransformer


class PCMMediaSource(MediaSource):
    """A media source backed by raw 16-bit 48 kHz stereo PCM bytes.

    This mirrors :class:`discord.PCMAudio` for file-like raw PCM inputs while
    keeping the source composable with video-capable media sources.

    Parameters
    ----------
    stream: :class:`bytes`
        A :term:`py:bytes-like object` that yields 20 ms PCM frames.
    close: :class:`bool`
        Whether to close the stream when the source is exhausted or cleaned up.

    Attributes
    ----------
    stream: :class:`bytes`
        The wrapped binary stream.
    """

    def __init__(self, stream: BinaryIO, /, *, close: bool = False) -> None:
        if not callable(getattr(stream, 'read', None)):
            raise TypeError(f'stream must be a binary file-like object not {stream.__class__.__name__}')

        self.stream = stream
        self._close = close
        self._closed = False

    def _close_stream(self) -> None:
        if not self._close:
            return
        self._close = False
        close = getattr(self.stream, 'close', None)
        if callable(close):
            close()

    def has_audio(self) -> bool:
        return not self._closed

    def is_opus(self) -> bool:
        return False

    def read(self) -> bytes:
        if self._closed:
            return b''

        data = self.stream.read(OpusEncoder.FRAME_SIZE)
        data = bytes(data) if data else b''
        if not data:
            self._closed = True
            self._close_stream()
            return b''
        if len(data) < OpusEncoder.FRAME_SIZE:
            self._closed = True
            self._close_stream()
            return data + b'\x00' * (OpusEncoder.FRAME_SIZE - len(data))
        return data

    def is_finished(self) -> bool:
        return self._closed

    def cleanup(self) -> None:
        self._closed = True
        self._close_stream()


class AudioFrameSource(MediaSource):
    """An audio source backed by an iterable of audio frames.

    This is the in-memory/custom-producer counterpart to d.py's file-like
    :class:`discord.PCMAudio`. PCM frames should be 20 ms of 48 kHz stereo
    signed 16-bit audio; Opus frames may be variable length.

    Parameters
    ----------
    frames: Iterable[Union[:class:`bytes`, :class:`bytearray`, :class:`memoryview`]]
        The audio frames to read from.
    opus: :class:`bool`
        Whether the frames are already Opus encoded.
    """

    def __init__(self, frames: Iterable[AudioFrameInput], /, *, opus: bool = False) -> None:
        self._frames: Iterator[AudioFrameInput] = iter(frames)
        self._opus = opus
        self._closed = False

    def has_audio(self) -> bool:
        return not self._closed

    def is_opus(self) -> bool:
        return self._opus

    def read(self) -> bytes:
        if self._closed:
            return b''

        try:
            frame = next(self._frames)
        except StopIteration:
            self._closed = True
            return b''

        data = bytes(frame)
        if not data:
            self._closed = True
        elif not self._opus and len(data) < OpusEncoder.FRAME_SIZE:
            self._closed = True
            return data + b'\x00' * (OpusEncoder.FRAME_SIZE - len(data))
        return data

    def is_finished(self) -> bool:
        return self._closed

    def cleanup(self) -> None:
        self._closed = True


class _VideoOnlySource(MediaSource):
    def has_audio(self) -> bool:
        return False

    def read(self) -> bytes:
        return b''


class VideoFrameSource(_VideoOnlySource):
    """A video source backed by an iterable of already-encoded frames.

    Parameters
    ----------
    frames: Iterable[Union[:class:`VideoFrame`, :class:`bytes`, :class:`bytearray`, :class:`memoryview`]]
        Encoded video frames to read from.
    codec: :class:`str`
        The Discord video codec name for the frames.
    fps: :class:`int`
        The frame rate used to derive frame durations.
    width: :class:`int`
        The encoded frame width in pixels.
    height: :class:`int`
        The encoded frame height in pixels.
    bitrate: :class:`int`
        The target video bitrate in bits per second.

    Attributes
    ----------
    codec: :class:`str`
        The normalized Discord video codec name.
    frame_time_ms: :class:`float`
        The default frame duration in milliseconds.
    """

    def __init__(
        self,
        frames: Iterable[VideoFrameInput],
        *,
        codec: str,
        fps: int,
        width: int = 0,
        height: int = 0,
        bitrate: int = 0,
    ) -> None:
        self.codec = _coerce_video_codec(codec)

        self.frame_time_ms = 1000.0 / max(1, fps)
        self._video_config = VideoConfig(
            codec=self.codec,
            width=max(0, width),
            height=max(0, height),
            fps=max(1, fps),
            bitrate=max(0, bitrate),
        )
        self._frames: Iterator[VideoFrameInput] = iter(frames)
        self._closed = False

    def has_video(self) -> bool:
        return not self._closed

    @property
    def video_config(self) -> VideoConfig:
        """:class:`VideoConfig`: Video configuration for frames from this source."""
        return self._video_config

    def read_video(self) -> VideoFrame | None:
        if self._closed:
            return None

        try:
            frame = next(self._frames)
        except StopIteration:
            self._closed = True
            return None

        if isinstance(frame, VideoFrame):
            return frame

        return VideoFrame(bytes(frame), frame_time_ms=self.frame_time_ms)

    def is_finished(self) -> bool:
        return self._closed

    def cleanup(self) -> None:
        self._closed = True


def _read_exact(stream: IO[bytes], size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            break
        chunks.extend(chunk)
    return bytes(chunks)


def _copy_binary_stream(source: IO[bytes], destination: IO[bytes]) -> None:
    while True:
        try:
            data = source.read(io.DEFAULT_BUFFER_SIZE)
        except (OSError, ValueError):
            return
        if not data:
            return
        try:
            destination.write(data)
        except (OSError, ValueError):
            return


def _normalize_ffmpeg_stderr(stderr: FFmpegStderr) -> FFmpegStderr:
    return None if stderr == subprocess.PIPE else stderr


def _resolve_ffmpeg_source_input(source: str | os.PathLike[str] | BinaryIO, *, pipe: bool) -> tuple[BinaryIO | None, str]:
    if pipe:
        if isinstance(source, (str, os.PathLike)):
            raise TypeError('source parameter cannot be a path when piping to stdin')
        return source, '-'

    if not isinstance(source, (str, os.PathLike)):
        raise TypeError('source parameter must be a path unless piping to stdin')
    return None, os.fspath(source)


def _close_quietly(resource: Any) -> None:
    try:
        resource.close()
    except Exception:
        pass


def _join_thread(thread: threading.Thread | None, timeout: float = 1.0) -> None:
    if thread is not None and thread is not threading.current_thread():
        thread.join(timeout=timeout)


def _wait_or_kill_process(process: subprocess.Popen[Any], timeout: float = 5.0) -> None:
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)


def _ffprobe_executable(executable: str) -> str:
    path, name = os.path.split(executable)
    stem, suffix = os.path.splitext(name)
    if name in ('ffmpeg', 'avconv'):
        return name[:2] + 'probe'
    lower_stem = stem.lower()
    if lower_stem in ('ffmpeg', 'avconv'):
        probe_name = lower_stem[:2] + 'probe' + suffix
        if not path:
            return probe_name
        probe_path = os.path.join(path, probe_name)
        if os.path.exists(probe_path):
            return probe_path
    return executable


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bitrate_bps_or_none(value: Any) -> int | None:
    bitrate = _int_or_none(value)
    return bitrate if bitrate and bitrate > 0 else None


def _fps_or_none(value: Any) -> int | None:
    if not value:
        return None
    try:
        if isinstance(value, str) and '/' in value:
            numerator, denominator = value.split('/', 1)
            denominator_value = float(denominator)
            if denominator_value == 0:
                return None
            fps = float(numerator) / denominator_value
        else:
            fps = float(value)
    except (TypeError, ValueError):
        return None
    return max(1, round(fps)) if fps > 0 else None


def _annex_b_nal_types(payload: bytes, codec: str) -> frozenset[int]:
    nal_types = set()
    index = 0
    while index + 3 <= len(payload):
        if index + 4 <= len(payload) and payload[index : index + 4] == b'\x00\x00\x00\x01':
            start_code_len = 4
        elif payload[index : index + 3] == b'\x00\x00\x01':
            start_code_len = 3
        else:
            index += 1
            continue

        nal_start = index + start_code_len
        if codec == 'H264':
            if nal_start < len(payload):
                nal_types.add(payload[nal_start] & 0x1F)
        elif nal_start + 1 < len(payload):
            nal_types.add((payload[nal_start] >> 1) & 0x3F)
        index = nal_start + 1
    return frozenset(nal_types)


def _coerce_video_probe_info(value: Any) -> VideoProbeInfo:
    if value is None:
        return VideoProbeInfo()
    if isinstance(value, VideoProbeInfo):
        return value
    if isinstance(value, Mapping):
        return VideoProbeInfo(
            width=_int_or_none(value.get('width')),
            height=_int_or_none(value.get('height')),
            fps=_fps_or_none(value.get('fps')),
            bitrate=_bitrate_bps_or_none(value.get('bitrate')),
            codec=_video_codec_or_none(value.get('codec')),
        )
    values = tuple(value)
    if len(values) == 4:
        width, height, fps, bitrate = values
        codec = None
    elif len(values) == 5:
        width, height, fps, bitrate, codec = values
    else:
        raise TypeError('video probe results must be VideoProbeInfo, a mapping, or a 4/5-item tuple')
    return VideoProbeInfo(
        width=_int_or_none(width),
        height=_int_or_none(height),
        fps=_fps_or_none(fps),
        bitrate=_bitrate_bps_or_none(bitrate),
        codec=_video_codec_or_none(codec),
    )


def _resolve_video_probe_config(
    video_probe: VideoProbeInfo,
    *,
    codec: str | None,
    width: int | None,
    height: int | None,
    fps: int | None,
    bitrate: int | None,
) -> VideoConfig:
    video_codec = _coerce_video_codec(codec) if codec is not None else video_probe.codec
    if video_codec is None:
        raise discord.ClientException('Could not probe video codec; pass codec explicitly')

    video_width = width if width is not None else video_probe.width
    video_height = height if height is not None else video_probe.height
    if video_width is None or video_width <= 0 or video_height is None or video_height <= 0:
        raise discord.ClientException('Could not probe video dimensions; pass width and height explicitly')

    video_fps = fps if fps is not None else video_probe.fps or 30
    video_bitrate = bitrate if bitrate is not None else video_probe.bitrate or _native_video_bitrate(video_height, video_fps)
    return VideoConfig(video_codec, video_width, video_height, video_fps, video_bitrate)


async def _probe_video_config(
    source: str,
    *,
    codec: str | None,
    width: int | None,
    height: int | None,
    fps: int | None,
    bitrate: int | None,
    method: VideoProbeMethod,
    executable: str,
) -> tuple[VideoProbeInfo, VideoConfig]:
    video_probe = VideoProbeInfo()
    if codec is None or width is None or height is None or fps is None or bitrate is None:
        video_probe = await FFmpegMediaSource.probe_video(source, method=method, executable=executable)

    return video_probe, _resolve_video_probe_config(
        video_probe,
        codec=codec,
        width=width,
        height=height,
        fps=fps,
        bitrate=bitrate,
    )


class _IVFFrameReader:
    def __init__(self, stream: IO[bytes]) -> None:
        header = _read_exact(stream, 32)
        if len(header) != 32 or header[:4] != b'DKIF':
            raise RuntimeError('FFmpeg did not produce an IVF stream')
        self.stream = stream

    def read_frame(self) -> bytes:
        frame_header = _read_exact(self.stream, 12)
        if not frame_header:
            return b''
        if len(frame_header) != 12:
            raise RuntimeError('truncated IVF frame header')
        frame_size = int.from_bytes(frame_header[:4], 'little')
        frame = _read_exact(self.stream, frame_size)
        if len(frame) != frame_size:
            raise RuntimeError('truncated IVF frame payload')
        return frame


class _AnnexBFrameReader:
    def __init__(self, stream: IO[bytes], *, codec: str) -> None:
        self.stream = stream
        self.codec = _coerce_video_codec(codec)
        self.buffer = bytearray()
        self._search_pos = 0
        self._aud_positions: list[int] = []

    def _nal_type(self, start: int, start_code_len: int) -> int | None:
        offset = start + start_code_len
        if offset >= len(self.buffer):
            return None
        if self.codec == 'H264':
            return self.buffer[offset] & 0x1F
        if offset + 1 >= len(self.buffer):
            return None
        return (self.buffer[offset] >> 1) & 0x3F

    def _find_start_code(self, cursor: int) -> tuple[int, int] | None:
        if cursor >= len(self.buffer):
            return None

        three = self.buffer.find(b'\x00\x00\x01', cursor)
        four = self.buffer.find(b'\x00\x00\x00\x01', cursor)
        if three == -1 and four == -1:
            return None
        if four != -1 and (three == -1 or four <= three):
            return four, 4
        return three, 3

    def _scan_for_auds(self) -> None:
        aud_type = 9 if self.codec == 'H264' else 35
        while len(self._aud_positions) < 2:
            found = self._find_start_code(self._search_pos)
            if found is None:
                self._search_pos = max(0, len(self.buffer) - 4)
                return

            start, length = found
            self._search_pos = start + length
            if self._nal_type(start, length) == aud_type and (not self._aud_positions or self._aud_positions[-1] != start):
                self._aud_positions.append(start)

    def _extract_frame(self) -> bytes | None:
        self._scan_for_auds()
        if not self._aud_positions:
            if len(self.buffer) > 2 * 1024 * 1024:
                trim = len(self.buffer) - 1024
                del self.buffer[:trim]
                self._search_pos = max(0, self._search_pos - trim)
            return None

        if self._aud_positions[0] > 0:
            trim = self._aud_positions[0]
            del self.buffer[:trim]
            self._aud_positions = [position - trim for position in self._aud_positions]
            self._search_pos = max(0, self._search_pos - trim)
            return None

        if len(self._aud_positions) < 2:
            return None

        frame_end = self._aud_positions[1]
        frame = bytes(self.buffer[:frame_end])
        del self.buffer[:frame_end]
        self._aud_positions = [position - frame_end for position in self._aud_positions[1:]]
        self._search_pos = max(0, self._search_pos - frame_end)
        return frame

    def read_frame(self) -> bytes:
        while True:
            frame = self._extract_frame()
            if frame:
                return frame

            chunk = self.stream.read(65536)
            if not chunk:
                if self.buffer:
                    frame = bytes(self.buffer)
                    self.buffer.clear()
                    return frame
                return b''
            self.buffer.extend(chunk)


class EncodedVideoSource(_VideoOnlySource):
    """A video source backed by already-encoded video frames.

    VP8, VP9, and AV1 inputs are read as IVF streams. H264 and H265 inputs are
    read as Annex B streams with access unit delimiters.

    Parameters
    ----------
    source: Union[:class:`str`, :class:`os.PathLike`, BinaryIO]
        A path or :term:`py:bytes-like object` containing encoded video frames.
    codec: :class:`str`
        The video codec name for the input.
    fps: :class:`int`
        The frame rate used to derive frame durations.
    width: :class:`int`
        The encoded frame width in pixels.
    height: :class:`int`
        The encoded frame height in pixels.
    bitrate: :class:`int`
        The target video bitrate in bits per second.

    Attributes
    ----------
    codec: :class:`str`
        The normalized Discord video codec name.
    frame_time_ms: :class:`float`
        The default frame duration in milliseconds.
    """

    _IVF_CODECS = frozenset({'VP8', 'VP9', 'AV1'})

    def __init__(
        self,
        source: str | os.PathLike[str] | BinaryIO,
        *,
        codec: str,
        fps: int,
        width: int = 0,
        height: int = 0,
        bitrate: int = 0,
    ) -> None:
        self.codec = _coerce_video_codec(codec)

        if isinstance(source, (str, os.PathLike)):
            self._file = open(source, 'rb')
            self._close_file = True
        else:
            self._file = source
            self._close_file = False
        self.frame_time_ms = 1000.0 / max(1, fps)
        self._video_config = VideoConfig(
            codec=self.codec,
            width=max(0, width),
            height=max(0, height),
            fps=max(1, fps),
            bitrate=max(0, bitrate),
        )
        self._closed = False
        try:
            if self.codec in self._IVF_CODECS:
                self._reader = _IVFFrameReader(self._file)
            else:
                self._reader = _AnnexBFrameReader(self._file, codec=self.codec)
        except Exception:
            self.cleanup()
            raise

    def has_video(self) -> bool:
        return not self._closed

    @property
    def video_config(self) -> VideoConfig:
        return self._video_config

    def read_video(self) -> VideoFrame | None:
        if self._closed:
            return None
        frame = self._reader.read_frame()
        if not frame:
            self._closed = True
            return None
        return VideoFrame(frame, frame_time_ms=self.frame_time_ms)

    def is_finished(self) -> bool:
        return self._closed

    def cleanup(self) -> None:
        file = getattr(self, '_file', MISSING)
        if file is MISSING:
            return
        self._file = MISSING
        self._closed = True
        if self._close_file:
            file.close()


class SimulcastVideoSource(_VideoSourceDelegate, _VideoOnlySource):
    """A video source composed of RID-keyed video sources.

    Each child source should produce encoded frames for the same codec, with
    keys matching the negotiated :class:`discord.VoiceStream` RIDs.

    Parameters
    ----------
    sources: Dict[:class:`str`, :class:`MediaSource`]
        The child video sources, keyed by RTP stream ID.

    Attributes
    ----------
    sources: Dict[:class:`str`, :class:`MediaSource`]
        The child sources, keyed by RTP stream ID.
    """

    def __init__(self, sources: Mapping[str, MediaSource], /) -> None:
        if not sources:
            raise TypeError('sources must not be empty')

        normalized: dict[str, MediaSource] = {}
        for rid, source in sources.items():
            if not isinstance(source, MediaSource):
                raise TypeError(f'sources values must be MediaSource, not {source.__class__.__name__}')
            if not source.has_video():
                raise discord.ClientException(f'Simulcast source {rid!r} does not have video')
            normalized[str(rid)] = source

        codec: str | None = None
        for rid, source in normalized.items():
            config = source.video_config
            if config is None:
                continue
            if codec is None:
                codec = config.codec
            elif config.codec != codec:
                raise discord.ClientException(f'Simulcast source {rid!r} uses {config.codec}, expected {codec}')

        self._sources = dict(normalized)
        self.sources: Mapping[str, MediaSource] = MappingProxyType(self._sources)
        self._primary_rid = next(iter(normalized))
        self._closed = False

    def _primary_source(self) -> MediaSource | None:
        return None if self._closed else self._sources.get(self._primary_rid)

    def _active_video_source(self) -> MediaSource | None:
        return self._primary_source()

    def has_video(self) -> bool:
        return not self._closed and any(source.has_video() and not source.is_finished() for source in self._sources.values())

    def supports_simulcast(self) -> bool:
        return not self._closed and len(self._sources) > 1

    def read_video_streams(self, streams: Sequence[VoiceStream]) -> Mapping[str, VideoFrame] | None:
        if self._closed:
            return None
        if not streams:
            return {}

        frames: dict[str, VideoFrame] = {}
        for stream in streams:
            rid = stream.rid
            source = self._sources.get(rid)
            if source is None:
                raise discord.ClientException(f'Simulcast source does not include RID {rid!r}')
            if source.is_finished():
                continue
            frame = source.read_video()
            if frame is not None:
                frames[rid] = frame

        if frames:
            return frames
        return None if self.is_finished() else {}

    def on_media_sink_wants(self, wants: MediaSinkWants) -> None:
        for source in _unique_sources(self._sources.values()):
            source.on_media_sink_wants(wants)

    def is_finished(self) -> bool:
        return self._closed or all(source.is_finished() for source in self._sources.values())

    def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        for source in _unique_sources(self._sources.values()):
            source.cleanup()


class FFmpegVideoSource(_VideoOnlySource):
    """An encoded video source backed by an FFmpeg subprocess.

    The subprocess writes codec-ready H264/H265 Annex B or VP8/VP9/AV1 IVF
    frames to stdout for the native RTP packetizers.

    Parameters
    ----------
    command: List[:class:`str`]
        The FFmpeg command to run.
    codec: :class:`str`
        The Discord video codec name produced by FFmpeg.
    fps: :class:`int`
        The target frame rate.
    width: :class:`int`
        The encoded frame width in pixels.
    height: :class:`int`
        The encoded frame height in pixels.
    bitrate: :class:`int`
        The target video bitrate in bits per second.
    preview_command: Optional[List[:class:`str`]]
        FFmpeg command used to produce a stream preview image frame.
    pipe_source: Any
        Optional file-like object or native desktop capture source piped into FFmpeg stdin.
    stderr: Optional[Union[IO[:class:`bytes`], :class:`int`]]
        Where FFmpeg stderr is redirected.
    live_timestamps: :class:`bool`
        Whether frame durations should track wall-clock capture timing.

    Attributes
    ----------
    command: List[:class:`str`]
        The FFmpeg command being run.
    preview_command: Optional[List[:class:`str`]]
        The FFmpeg preview command, if configured.
    codec: :class:`str`
        The normalized Discord video codec name.
    frame_time_ms: :class:`float`
        The default frame duration in milliseconds.
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        codec: str,
        fps: int,
        width: int = 0,
        height: int = 0,
        bitrate: int = 0,
        preview_command: Sequence[str] | None = None,
        pipe_source: Any = None,
        stderr: FFmpegStderr = None,
        live_timestamps: bool = False,
    ) -> None:
        self.command = list(command)
        self.preview_command = list(preview_command) if preview_command is not None else None
        self.codec = _coerce_video_codec(codec)
        self.frame_time_ms = 1000.0 / max(1, fps)
        self.video_realtime = live_timestamps
        self._live_last_frame_at: float | None = None
        self._live_initial_frame_time_ms = max(self.frame_time_ms, 1000.0 / min(max(1, fps), 60))
        self._live_max_frame_time_ms = 250
        self._video_config = VideoConfig(
            codec=self.codec,
            width=max(0, width),
            height=max(0, height),
            fps=max(1, fps),
            bitrate=max(0, bitrate),
        )
        self._closed = False
        self._process = MISSING
        self._stdin: IO[bytes] | None = None
        self._stdout: IO[bytes] | None = None
        self._stderr: IO[bytes] | None = None
        self._pipe_source = pipe_source
        self._pipe_error: BaseException | None = None
        self._pipe_writer_thread: threading.Thread | None = None
        self._pipe_reader_thread: threading.Thread | None = None
        self._pipe_stats = _PipeStats()
        self._encoded_frame_stats = _EncodedFrameStats()

        subprocess_kwargs: dict[str, Any] = {
            'stdout': subprocess.PIPE,
            'stdin': subprocess.PIPE if pipe_source is not None else subprocess.DEVNULL,
        }
        stderr = _normalize_ffmpeg_stderr(stderr)
        stderr_destination: IO[bytes] | None = None
        piping_stderr = False
        if stderr is not None:
            if isinstance(stderr, int):
                subprocess_kwargs['stderr'] = stderr
            else:
                try:
                    stderr.fileno()
                except Exception:
                    piping_stderr = True
                    stderr_destination = stderr
                    subprocess_kwargs['stderr'] = subprocess.PIPE
                else:
                    subprocess_kwargs['stderr'] = stderr

        try:
            process: Any = subprocess.Popen(
                self.command,
                creationflags=CREATE_NO_WINDOW,
                **subprocess_kwargs,
            )
            self._process = process
        except FileNotFoundError:
            executable = self.command[0] if self.command else 'ffmpeg'
            raise discord.ClientException(executable + ' was not found') from None
        except subprocess.SubprocessError as exc:
            raise discord.ClientException(f'Popen failed: {exc.__class__.__name__}: {exc}') from exc

        try:
            if self._process.stdout is None:
                raise RuntimeError('FFmpeg stdout pipe was not created')
            stdout = self._process.stdout
            self._stdout = stdout
            if pipe_source is not None and self._process.stdin is not None:
                stdin = self._stdin = self._process.stdin
                target: Callable[..., None] = self._pipe_writer
                args: tuple[Any, ...] = (pipe_source,)
                native_handle = self._native_pipe_handle(stdin) if isinstance(pipe_source, DesktopFrameSource) else None
                if native_handle is not None:
                    target = self._native_pipe_writer
                    args = (pipe_source, native_handle)

                self._pipe_writer_thread = threading.Thread(
                    target=target,
                    args=args,
                    daemon=True,
                    name=f'native-voice-video-stdin:{self._process.pid}',
                )
                self._pipe_writer_thread.start()
            if piping_stderr and self._process.stderr is not None and stderr_destination is not None:
                self._stderr = self._process.stderr
                self._pipe_reader_thread = threading.Thread(
                    target=_copy_binary_stream,
                    args=(self._stderr, stderr_destination),
                    daemon=True,
                    name=f'native-voice-video-stderr:{self._process.pid}',
                )
                self._pipe_reader_thread.start()
            if self.codec in {'VP8', 'VP9', 'AV1'}:
                self._reader = _IVFFrameReader(stdout)
            else:
                self._reader = _AnnexBFrameReader(stdout, codec=self.codec)
        except Exception:
            self.cleanup()
            raise

    @staticmethod
    def _desktop_input_args(width: int, height: int, fps: int, *, display: str = MISSING) -> list[str]:
        if sys.platform == 'win32':
            return ['-f', 'gdigrab', '-framerate', str(fps), '-i', 'desktop']
        if sys.platform == 'linux':
            display = os.environ.get('DISPLAY', ':0.0') if display is MISSING else display
            return ['-f', 'x11grab', '-framerate', str(fps), '-video_size', f'{width}x{height}', '-i', display]
        raise RuntimeError('desktop capture input_args are required for this platform')

    @staticmethod
    def _native_desktop_capture_args(fps: int, *, output_index: int = 0) -> tuple[list[str], DesktopFrameSource]:
        if sys.platform != 'win32':
            raise discord.ClientException('Desktop capture is not available on this platform')

        try:
            source = DesktopFrameSource(output_index, fps)
        except Exception as exc:
            raise discord.ClientException(f'Desktop capture failed: {exc}') from exc

        pixel_format = 'bgra'
        if source.width % 2 == 0 and source.height % 2 == 0:
            try:
                source.set_pixel_format('nv12')
            except Exception:
                log.debug('Desktop capture could not switch to NV12; using BGRA.', exc_info=True)
            else:
                pixel_format = 'nv12'

        return (
            [
                '-f',
                'rawvideo',
                '-pix_fmt',
                pixel_format,
                '-video_size',
                f'{source.width}x{source.height}',
                '-framerate',
                str(fps),
                '-i',
                '-',
            ],
            source,
        )

    @staticmethod
    def _select_video_decoder(
        source_codec: str,
        *,
        executable: str,
        transcoder: VideoTranscoderConfig,
    ) -> str | None:
        if transcoder.decoder is None:
            return None

        codec = _coerce_video_codec(source_codec)
        decoder = _resolve_transcoder_codec(codec, transcoder.decoder, kind='decoder')
        available = _ffmpeg_video_codec_names(executable, 'decoders')
        if transcoder.validate_decoder and available and decoder not in available:
            raise discord.ClientException(f'FFmpeg decoder {decoder!r} is not available for {codec}')
        return decoder

    @staticmethod
    def _select_video_encoder(
        codec: str,
        *,
        executable: str,
        width: int,
        height: int,
        transcoder: VideoTranscoderConfig,
    ) -> str:
        codec = _coerce_video_codec(codec)
        available = _ffmpeg_video_codec_names(executable, 'encoders')

        if transcoder.encoder is not None:
            encoder = _resolve_transcoder_codec(codec, transcoder.encoder, kind='encoder')
            if transcoder.validate_encoder and available and encoder not in available:
                raise discord.ClientException(f'FFmpeg encoder {encoder!r} is not available for {codec}')
            if transcoder.validate_encoder and not _ffmpeg_encoder_is_usable(
                executable,
                codec,
                encoder,
                width=width,
                height=height,
                transcoder=transcoder,
            ):
                raise discord.ClientException(f'FFmpeg encoder {encoder!r} could not encode a test {codec} frame')
            return encoder

        candidates = (
            _VIDEO_ENCODER_PRIORITY[codec] if transcoder.prefer_hardware else _VIDEO_SOFTWARE_ENCODER_PRIORITY[codec]
        )
        if available:
            for encoder in candidates:
                if encoder not in available:
                    continue
                if transcoder.validate_encoder and not _ffmpeg_encoder_is_usable(
                    executable,
                    codec,
                    encoder,
                    width=width,
                    height=height,
                    transcoder=transcoder,
                ):
                    continue
                return encoder

            raise discord.ClientException(f'FFmpeg does not advertise a supported {codec} encoder')

        return _VIDEO_DEFAULT_ENCODERS[codec]

    @classmethod
    def _ffmpeg_command(
        cls,
        codec: str,
        *,
        width: int,
        height: int,
        fps: int,
        bitrate: int,
        executable: str,
        input_args: Sequence[str],
        before_options: str | None = None,
        options: str | None = None,
        source_codec: str | None = None,
        pipe: bool = False,
        transcoder: VideoTranscoderConfig | None = None,
        use_decoder: bool = True,
    ) -> list[str]:
        codec = _coerce_video_codec(codec)
        config = transcoder if transcoder is not None else VideoTranscoderConfig()
        encoder = cls._select_video_encoder(
            codec,
            executable=executable,
            width=width,
            height=height,
            transcoder=config,
        )
        decoder = (
            cls._select_video_decoder(source_codec or codec, executable=executable, transcoder=config)
            if use_decoder
            else None
        )
        base = [
            executable,
            '-hide_banner',
            '-loglevel',
            'warning',
        ]
        if not pipe:
            base.append('-nostdin')
        base.extend(_argv_options(before_options))
        base.extend(_argv_options(config.input_options))
        if decoder is not None:
            base.extend(('-c:v', decoder))
        base.extend([*input_args, '-an'])
        filtergraph = _video_filtergraph(config, width=width, height=height, fps=fps, codec=codec)
        if filtergraph:
            base.extend(('-vf', filtergraph))
        base.extend(('-b:v', f'{max(1, bitrate // 1000)}k'))
        output_args = _argv_options(options)
        output_options = _argv_options(config.output_options)
        has_sync_option = any(option in output_args or option in output_options for option in ('-fps_mode', '-vsync'))
        sync_args = [] if has_sync_option or fps <= 60 else ['-vsync', 'passthrough']
        rate_control_args = []
        if encoder in _NVENC_ENCODERS:
            bitrate_k = max(1, bitrate // 1000)
            frame_budget = bitrate / 8 / max(1, fps)
            buffer_k = max(64, round((frame_budget * 4) / 1000))
            rate_control_args = [
                '-rc',
                'cbr',
                '-maxrate',
                f'{bitrate_k}k',
                '-bufsize',
                f'{buffer_k}k',
                '-multipass',
                'disabled',
            ]
            if encoder in _NVENC_H26X_ENCODERS:
                rate_control_args.extend(('-strict_gop', '1'))
        return [
            *base,
            *_video_encoder_args(codec, encoder, fps=fps),
            *rate_control_args,
            *_argv_options(config.encoder_options),
            *sync_args,
            *output_args,
            *output_options,
            *_video_bitstream_filter_args(codec, encoder),
            '-f',
            _VIDEO_OUTPUT_FORMAT[codec],
            'pipe:1',
        ]

    @staticmethod
    def _preview_ffmpeg_command(
        *,
        width: int,
        height: int,
        executable: str,
        input_args: Sequence[str],
        before_options: str | None = None,
    ) -> list[str]:
        ratio = min(512 / width, 288 / height) if width > 0 and height > 0 else 1
        preview_width = max(1, round(width * ratio))
        preview_height = max(1, round(height * ratio))
        command = [
            executable,
            '-hide_banner',
            '-loglevel',
            'warning',
            '-nostdin',
        ]
        command.extend(_argv_options(before_options))
        return [
            *command,
            *input_args,
            '-frames:v',
            '1',
            '-vf',
            f'scale={preview_width}:{preview_height}:flags=fast_bilinear,format=yuvj420p',
            '-q:v',
            '5',
            '-f',
            'image2pipe',
            '-vcodec',
            'mjpeg',
            'pipe:1',
        ]

    @classmethod
    def _spawn_from_input(
        cls,
        codec: str,
        *,
        width: int,
        height: int,
        fps: int,
        bitrate: int,
        executable: str,
        input_args: Sequence[str],
        stderr: FFmpegStderr = None,
        before_options: str | None = None,
        options: str | None = None,
        source_codec: str | None = None,
        preview_input_args: Sequence[str] | None = None,
        pipe_source: Any = None,
        transcoder: VideoTranscoderConfig | None = None,
        use_decoder: bool = True,
        live_timestamps: bool = False,
    ) -> FFmpegVideoSource:
        preview_command = None
        if preview_input_args is not None:
            preview_command = cls._preview_ffmpeg_command(
                width=width,
                height=height,
                executable=executable,
                input_args=preview_input_args,
                before_options=before_options,
            )

        return cls(
            cls._ffmpeg_command(
                codec,
                width=width,
                height=height,
                fps=fps,
                bitrate=bitrate,
                executable=executable,
                input_args=input_args,
                before_options=before_options,
                options=options,
                source_codec=source_codec,
                pipe=pipe_source is not None,
                transcoder=transcoder,
                use_decoder=use_decoder,
            ),
            codec=codec,
            fps=fps,
            width=width,
            height=height,
            bitrate=bitrate,
            preview_command=preview_command,
            pipe_source=pipe_source,
            stderr=stderr,
            live_timestamps=live_timestamps,
        )

    @classmethod
    def preflight_desktop(
        cls,
        *,
        width: int,
        height: int,
        fps: int = 1,
        codec: str = 'H264',
        bitrate: int = 4_000_000,
        executable: str = 'ffmpeg',
        input_args: Sequence[str] | None = None,
        before_options: str | None = None,
        transcoder: VideoTranscoderConfig | None = None,
        native_capture: bool = False,
        output_index: int = 0,
        timeout: float = 15.0,
    ) -> None:
        """Check whether the configured desktop source can produce an encoded frame.

        This is useful before joining voice, since desktop capture and encoder
        failures are often caused by the local session rather than Discord
        transport.

        Parameters
        ----------
        width: :class:`int`
            The capture width in pixels.
        height: :class:`int`
            The capture height in pixels.
        fps: :class:`int`
            The capture frame rate.
        codec: :class:`str`
            The Discord video codec to encode.
        bitrate: :class:`int`
            The target video bitrate in bits per second.
        executable: :class:`str`
            The FFmpeg executable to run.
        input_args: Optional[List[:class:`str`]]
            FFmpeg input arguments. When omitted, platform desktop capture defaults are used.
        before_options: Optional[:class:`str`]
            Extra FFmpeg options placed before input options.
        transcoder: Optional[:class:`VideoTranscoderConfig`]
            Encoder and filter selection options.
        native_capture: :class:`bool`
            Whether to use the native desktop capture bridge on supported platforms (currently Windows only).
        output_index: :class:`int`
            The native desktop output index to capture.
        timeout: :class:`float`
            Maximum seconds to wait for the preflight encode.

        Raises
        ------
        ClientException
            Desktop capture, FFmpeg startup, encoder validation, or the preflight encode failed.
        RuntimeError
            Platform desktop capture defaults are not available.
        """
        if native_capture:
            source = cls.from_desktop(
                codec,
                width=width,
                height=height,
                fps=fps,
                bitrate=bitrate,
                executable=executable,
                stderr=subprocess.DEVNULL,
                before_options=before_options,
                transcoder=transcoder,
                native_capture=True,
                output_index=output_index,
            )
            executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix='native-voice-preflight')
            future = executor.submit(source.read_video)
            try:
                frame = future.result(timeout=timeout)
            except concurrent.futures.TimeoutError as exc:
                raise discord.ClientException(f'Desktop capture preflight timed out after {timeout:g}s') from exc
            except Exception as exc:
                raise discord.ClientException(f'Desktop capture preflight failed: {exc}') from exc
            finally:
                source.cleanup()
                executor.shutdown(wait=False, cancel_futures=True)
            if frame is None:
                raise discord.ClientException('Desktop capture preflight returned no encoded frame')
            return

        capture_args = list(input_args) if input_args is not None else cls._desktop_input_args(width, height, fps)
        command = cls._ffmpeg_command(
            codec,
            width=width,
            height=height,
            fps=fps,
            bitrate=bitrate,
            executable=executable,
            input_args=capture_args,
            before_options=before_options,
            options='-frames:v 1',
            transcoder=transcoder,
            use_decoder=False,
        )
        try:
            process = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError:
            raise discord.ClientException(executable + ' was not found') from None
        except subprocess.TimeoutExpired as exc:
            raise discord.ClientException(f'FFmpeg desktop capture preflight timed out after {timeout:g}s') from exc
        except subprocess.SubprocessError as exc:
            raise discord.ClientException(f'FFmpeg desktop capture preflight failed: {exc}') from exc

        if process.returncode != 0:
            output = process.stderr.decode('utf8', 'replace').strip()
            detail = output[-1000:] if output else f'exit code {process.returncode}'
            raise discord.ClientException(f'FFmpeg desktop capture preflight failed: {detail}')

    @classmethod
    def from_desktop(
        cls,
        codec: str,
        *,
        width: int,
        height: int,
        fps: int,
        bitrate: int,
        executable: str = 'ffmpeg',
        input_args: Sequence[str] | None = None,
        stderr: FFmpegStderr = None,
        before_options: str | None = None,
        options: str | None = None,
        transcoder: VideoTranscoderConfig | None = None,
        native_capture: bool = False,
        output_index: int = 0,
        display: str = MISSING,
    ) -> FFmpegVideoSource:
        """Create an FFmpeg video source from the current desktop capture input.

        Parameters
        ----------
        codec: :class:`str`
            The Discord video codec to encode.
        width: :class:`int`
            The capture width in pixels.
        height: :class:`int`
            The capture height in pixels.
        fps: :class:`int`
            The capture frame rate.
        bitrate: :class:`int`
            The target video bitrate in bits per second.
        executable: :class:`str`
            The FFmpeg executable to run.
        input_args: Optional[List[:class:`str`]]
            FFmpeg input arguments. When omitted, platform desktop capture defaults are used.
        stderr: Optional[Union[IO[:class:`bytes`], :class:`int`]]
            Where FFmpeg stderr is redirected.
        before_options: Optional[:class:`str`]
            Extra FFmpeg options placed before input options.
        options: Optional[:class:`str`]
            Extra FFmpeg output options.
        transcoder: Optional[:class:`VideoTranscoderConfig`]
            Encoder and filter selection options.
        native_capture: :class:`bool`
            Whether to use the native desktop capture bridge on supported platforms (currently Windows only).
        output_index: :class:`int`
            The native desktop output index to capture.
        display: Optional[:class:`str`]
            The X11 display name used by the default Linux desktop input.

        Returns
        -------
        :class:`FFmpegVideoSource`
            The created video source.

        Raises
        ------
        ClientException
            Desktop capture, FFmpeg startup, or encoder selection failed.
        RuntimeError
            Platform desktop capture defaults are not available.
        ValueError
            ``codec`` is not a supported Discord video codec.
        """
        pipe_source: DesktopFrameSource | None = None
        if native_capture:
            capture_args, pipe_source = cls._native_desktop_capture_args(fps, output_index=output_index)
        else:
            capture_args = (
                list(input_args) if input_args is not None else cls._desktop_input_args(width, height, fps, display=display)
            )

        effective_transcoder = transcoder
        if (
            pipe_source is not None
            and transcoder is None
            and pipe_source.pixel_format == 'nv12'
            and pipe_source.width == width
            and pipe_source.height == height
        ):
            effective_transcoder = VideoTranscoderConfig(video_filters=())

        return cls._spawn_from_input(
            codec,
            width=width,
            height=height,
            fps=fps,
            bitrate=bitrate,
            executable=executable,
            input_args=capture_args,
            before_options=before_options,
            options=options,
            transcoder=effective_transcoder,
            use_decoder=False,
            preview_input_args=None if pipe_source is not None else capture_args,
            pipe_source=pipe_source,
            stderr=stderr,
            live_timestamps=True,
        )

    @classmethod
    def from_file(
        cls,
        source: str | os.PathLike[str] | BinaryIO,
        codec: str,
        *,
        width: int,
        height: int,
        fps: int,
        bitrate: int,
        executable: str = 'ffmpeg',
        pipe: bool = False,
        stderr: FFmpegStderr = None,
        before_options: str | None = None,
        options: str | None = None,
        source_codec: str | None = None,
        input_args: Sequence[str] | None = None,
        preview_input_args: Sequence[str] | None = None,
        transcoder: VideoTranscoderConfig | None = None,
    ) -> FFmpegVideoSource:
        """Create an FFmpeg video source from a file or stdin pipe.

        Parameters
        ----------
        source: Union[:class:`str`, :class:`os.PathLike`, BinaryIO]
            A video path or binary stream.
        codec: :class:`str`
            The Discord video codec to encode.
        width: :class:`int`
            The encoded video width in pixels.
        height: :class:`int`
            The encoded video height in pixels.
        fps: :class:`int`
            The target frame rate.
        bitrate: :class:`int`
            The target video bitrate in bits per second.
        executable: :class:`str`
            The FFmpeg executable to run.
        pipe: :class:`bool`
            Whether to pipe ``source`` into FFmpeg stdin instead of treating it as a path.
        stderr: Optional[Union[IO[:class:`bytes`], :class:`int`]]
            Where FFmpeg stderr is redirected.
        before_options: Optional[:class:`str`]
            Extra FFmpeg options placed before input options.
        options: Optional[:class:`str`]
            Extra FFmpeg output options.
        source_codec: Optional[:class:`str`]
            The input video codec used for decoder selection.
        input_args: Optional[List[:class:`str`]]
            Explicit FFmpeg input arguments.
        preview_input_args: Optional[List[:class:`str`]]
            FFmpeg input arguments used to produce stream previews.
        transcoder: Optional[:class:`VideoTranscoderConfig`]
            Encoder and filter selection options.

        Returns
        -------
        :class:`FFmpegVideoSource`
            The created video source.

        Raises
        ------
        TypeError
            ``source`` is incompatible with the selected ``pipe`` mode.
        ClientException
            FFmpeg startup or encoder selection failed.
        ValueError
            ``codec`` is not a supported Discord video codec.
        """
        pipe_source, source_input = _resolve_ffmpeg_source_input(source, pipe=pipe)
        capture_args = list(input_args) if input_args is not None else ['-i', source_input]
        preview_args = list(preview_input_args) if preview_input_args is not None else (None if pipe else capture_args)

        return cls._spawn_from_input(
            codec,
            width=width,
            height=height,
            fps=fps,
            bitrate=bitrate,
            executable=executable,
            input_args=capture_args,
            stderr=stderr,
            before_options=before_options,
            options=options,
            source_codec=source_codec,
            preview_input_args=preview_args,
            pipe_source=pipe_source,
            transcoder=transcoder,
        )

    @classmethod
    async def from_probe(
        cls,
        source: str | os.PathLike[str],
        codec: str | None = None,
        *,
        width: int | None = None,
        height: int | None = None,
        fps: int | None = None,
        bitrate: int | None = None,
        method: VideoProbeMethod = None,
        executable: str = 'ffmpeg',
        stderr: FFmpegStderr = None,
        before_options: str | None = None,
        options: str | None = None,
        input_args: Sequence[str] | None = None,
        preview_input_args: Sequence[str] | None = None,
        transcoder: VideoTranscoderConfig | None = None,
    ) -> FFmpegVideoSource:
        """Create a video source while probing missing video metadata first.

        Parameters
        ----------
        source: Union[:class:`str`, :class:`os.PathLike`]
            The video file path to probe and encode.
        codec: Optional[:class:`str`]
            The Discord video codec to encode. If omitted, the first video stream is probed.
        width: Optional[:class:`int`]
            The encoded video width in pixels. If omitted, the first video stream is probed.
        height: Optional[:class:`int`]
            The encoded video height in pixels. If omitted, the first video stream is probed.
        fps: Optional[:class:`int`]
            The target frame rate. If omitted, the first video stream is probed.
        bitrate: Optional[:class:`int`]
            The target video bitrate in bits per second. If omitted, the first video stream is probed.
        method: Optional[Union[:class:`str`, Callable[[str, str], Any]]]
            The video probing method.
        executable: :class:`str`
            The FFmpeg executable to run.
        stderr: Optional[Union[IO[:class:`bytes`], :class:`int`]]
            Where FFmpeg stderr is redirected.
        before_options: Optional[:class:`str`]
            Extra FFmpeg options placed before input options.
        options: Optional[:class:`str`]
            Extra FFmpeg output options.
        input_args: Optional[List[:class:`str`]]
            Explicit FFmpeg input arguments.
        preview_input_args: Optional[List[:class:`str`]]
            FFmpeg input arguments used to produce stream previews.
        transcoder: Optional[:class:`VideoTranscoderConfig`]
            Encoder and filter selection options.

        Returns
        -------
        :class:`FFmpegVideoSource`
            The created video source.

        Raises
        ------
        AttributeError
            ``method`` names an invalid video probe method.
        TypeError
            ``method`` is not a string, callable, or ``None``.
        ClientException
            Required video metadata could not be probed or FFmpeg setup failed.
        ValueError
            ``codec`` is not a supported Discord video codec.
        """
        source_path = os.fspath(source)
        video_probe, video_config = await _probe_video_config(
            source_path,
            codec=codec,
            width=width,
            height=height,
            fps=fps,
            bitrate=bitrate,
            method=method,
            executable=executable,
        )
        return cls.from_file(
            source_path,
            video_config.codec,
            width=video_config.width,
            height=video_config.height,
            fps=video_config.fps,
            bitrate=video_config.bitrate,
            executable=executable,
            stderr=stderr,
            before_options=before_options,
            options=options,
            source_codec=video_probe.codec,
            input_args=input_args,
            preview_input_args=preview_input_args,
            transcoder=transcoder,
        )

    @classmethod
    async def probe(
        cls,
        source: str | os.PathLike[str],
        *,
        method: VideoProbeMethod = None,
        executable: str = 'ffmpeg',
    ) -> VideoProbeInfo:
        """Probe the first video stream for codec, width, height, FPS, and bitrate.

        Parameters
        ----------
        source: Union[:class:`str`, :class:`os.PathLike`]
            The video file path to probe.
        method: Optional[Union[:class:`str`, Callable[[str, str], Any]]]
            The video probing method.
        executable: :class:`str`
            The FFmpeg executable used to locate ffprobe or run fallback probing.

        Returns
        -------
        :class:`VideoProbeInfo`
            The discovered video stream metadata.
        """
        return await FFmpegMediaSource.probe_video(source, method=method, executable=executable)

    def has_video(self) -> bool:
        return not self._closed

    @property
    def video_config(self) -> VideoConfig:
        return self._video_config

    def read_video(self) -> VideoFrame | None:
        if self._closed:
            return None
        if self._pipe_error is not None:
            self._raise_pipe_error()

        read_started = time.perf_counter()
        frame = self._reader.read_frame()
        self._encoded_frame_stats.record_read((time.perf_counter() - read_started) * 1000, len(frame))
        if self._pipe_error is not None:
            self._raise_pipe_error()
        if not frame:
            self._closed = True
            return None

        frame_time_ms = self.frame_time_ms
        if self.video_realtime:
            frame_time_ms = self._pace_live_frame()

        return VideoFrame(frame, frame_time_ms=frame_time_ms)

    def _raise_pipe_error(self) -> None:
        self._closed = True
        error = self._pipe_error
        if error is None:
            raise RuntimeError('FFmpeg video source pipe failed')
        raise RuntimeError('FFmpeg video source pipe failed') from error

    def _pace_live_frame(self) -> float:
        now = time.perf_counter()
        last_frame_at = self._live_last_frame_at
        self._live_last_frame_at = now
        if last_frame_at is None:
            return self._live_initial_frame_time_ms

        elapsed_ms = max(0.0, (now - last_frame_at) * 1000)
        target_ms = min(self._live_max_frame_time_ms, max(self.frame_time_ms, elapsed_ms))
        return max(0.001, target_ms)

    def read_preview(self) -> bytes | None:
        if self.preview_command is None:
            return None
        try:
            process = subprocess.run(
                self.preview_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return process.stdout if process.returncode == 0 and process.stdout else None

    def capture_stats(self) -> dict[str, int | float]:
        stats = getattr(self._pipe_source, 'stats', None)
        pipe_stats = getattr(self._pipe_source, 'pipe_stats', None)
        payload: dict[str, int | float] = {}
        if callable(stats):
            payload.update(stats())
        if callable(pipe_stats):
            native_pipe_stats = pipe_stats()
            payload.update(
                {
                    'pipeReadCount': int(native_pipe_stats.get('readCount', 0) or 0),
                    'pipeReadEmptyCount': int(native_pipe_stats.get('readEmptyCount', 0) or 0),
                    'pipeReadMeanMs': float(native_pipe_stats.get('readTotalMs', 0.0) or 0.0)
                    / max(1, int(native_pipe_stats.get('readCount', 0) or 0)),
                    'pipeReadMaxMs': float(native_pipe_stats.get('readMaxMs', 0.0) or 0.0),
                    'pipeWriteCount': int(native_pipe_stats.get('writeCount', 0) or 0),
                    'pipeWriteMeanMs': float(native_pipe_stats.get('writeTotalMs', 0.0) or 0.0)
                    / max(1, int(native_pipe_stats.get('writeCount', 0) or 0)),
                    'pipeWriteMaxMs': float(native_pipe_stats.get('writeMaxMs', 0.0) or 0.0),
                    'pipeBytesWritten': int(native_pipe_stats.get('bytesWritten', 0) or 0),
                }
            )
        elif self._pipe_writer_thread is not None or self._pipe_stats.read_count or self._pipe_stats.write_count:
            payload.update(self._pipe_stats.snapshot())
        if self._encoded_frame_stats.read_count:
            payload.update(self._encoded_frame_stats.snapshot())
        return payload

    def is_finished(self) -> bool:
        return self._closed

    def cleanup(self) -> None:
        process = self._process
        if process is MISSING:
            return
        self._process = MISSING
        self._closed = True
        pipe_source = self._pipe_source
        native_pipe_source = isinstance(pipe_source, DesktopFrameSource)
        if pipe_source is not None and not native_pipe_source:
            _close_quietly(pipe_source)
        if native_pipe_source:
            _close_quietly(self._stdin)
        if process.poll() is None:
            process.terminate()
        _wait_or_kill_process(process)
        for pipe in (self._stdin, self._stdout, self._stderr):
            if pipe is not None:
                _close_quietly(pipe)
        _join_thread(self._pipe_writer_thread)
        _join_thread(self._pipe_reader_thread)
        if pipe_source is not None and native_pipe_source:
            _close_quietly(pipe_source)
        self._stdin = None
        self._stdout = None
        self._stderr = None
        self._pipe_writer_thread = None
        self._pipe_reader_thread = None

    @staticmethod
    def _native_pipe_handle(stdin: IO[bytes]) -> int | None:
        if sys.platform != 'win32':
            return None

        try:
            fileno = stdin.fileno()
            handle = msvcrt.get_osfhandle(fileno)
        except Exception:
            log.debug('Could not resolve native desktop capture pipe handle.', exc_info=True)
            return None
        return handle if handle != -1 else None

    def _native_pipe_writer(self, source: DesktopFrameSource, handle: int) -> None:
        try:
            source.write_to_handle(handle)
        except Exception as exc:
            self._pipe_error = exc
            _close_quietly(self._stdin)
            return
        _close_quietly(self._stdin)

    def _pipe_writer(self, source: BinaryIO) -> None:
        read_size = getattr(source, 'preferred_read_size', io.DEFAULT_BUFFER_SIZE)
        if not isinstance(read_size, int) or read_size <= 0:
            read_size = io.DEFAULT_BUFFER_SIZE
        while True:
            process = self._process
            stdin = self._stdin
            if process is MISSING or stdin is None or stdin.closed or process.poll() is not None:
                return
            try:
                read_started = time.perf_counter()
                data = source.read(read_size)
                read_ms = (time.perf_counter() - read_started) * 1000
            except Exception as exc:
                self._pipe_error = exc
                _close_quietly(stdin)
                return
            self._pipe_stats.record_read(read_ms, empty=not data)
            if not data:
                _close_quietly(stdin)
                return
            try:
                write_started = time.perf_counter()
                stdin.write(data)
                write_ms = (time.perf_counter() - write_started) * 1000
                self._pipe_stats.record_write(write_ms, len(data))
                if len(data) < read_size:
                    stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                return


class FFmpegSimulcastVideoSource(SimulcastVideoSource):
    """An FFmpeg-backed simulcast source with one encoder per RID.

    This source is intended for camera/self-video style simulcast. Each child
    encoder produces an encoded frame stream for one advertised
    :class:`discord.VoiceStream` RID, and :class:`VoiceClient` sends only active
    negotiated RIDs.
    """

    @staticmethod
    def _stream_config(
        codec: str,
        stream: VoiceStream,
        *,
        width: int,
        height: int,
        fps: int,
        bitrate: int,
    ) -> VideoConfig:
        resolution = stream.max_resolution
        if resolution is not None and resolution.type == 'fixed' and resolution.width > 0 and resolution.height > 0:
            stream_width = resolution.width
            stream_height = resolution.height
        else:
            stream_width = width
            stream_height = height

        return VideoConfig(
            codec=codec,
            width=stream_width,
            height=stream_height,
            fps=stream.max_framerate if stream.max_framerate is not None else fps,
            bitrate=(
                stream.max_bitrate
                if stream.max_bitrate is not None
                else (bitrate if stream.quality >= 100 else max(1, bitrate // 4))
            ),
        )

    @staticmethod
    def _source_streams(streams: Sequence[VoiceStream]) -> tuple[VoiceStream, ...]:
        return tuple(stream.replace() for stream in streams)

    def _drain_inactive_streams(self, active_rids: set[str]) -> None:
        for rid, source in self._sources.items():
            if rid in active_rids or source.is_finished():
                continue
            source.read_video()

    def read_video_streams(self, streams: Sequence[VoiceStream]) -> Mapping[str, VideoFrame] | None:
        frames = super().read_video_streams(streams)
        if not self._closed:
            self._drain_inactive_streams({stream.rid for stream in streams})
        return frames

    @classmethod
    def _build_sources(
        cls,
        *,
        codec: str,
        width: int,
        height: int,
        fps: int,
        bitrate: int,
        streams: Sequence[VoiceStream],
        factory: Callable[[VideoConfig], FFmpegVideoSource],
    ) -> dict[str, FFmpegVideoSource]:
        children: dict[str, FFmpegVideoSource] = {}
        try:
            for stream in cls._source_streams(streams):
                rid = stream.rid
                if rid in children:
                    raise discord.ClientException(f'Duplicate simulcast RID {rid!r}')
                config = cls._stream_config(codec, stream, width=width, height=height, fps=fps, bitrate=bitrate)
                children[rid] = factory(config)
        except Exception:
            for child in children.values():
                child.cleanup()
            raise
        return children

    @classmethod
    def from_desktop(
        cls,
        codec: str,
        *,
        streams: Sequence[VoiceStream],
        width: int,
        height: int,
        fps: int,
        bitrate: int,
        executable: str = 'ffmpeg',
        input_args: Sequence[str] | None = None,
        stderr: FFmpegStderr = None,
        before_options: str | None = None,
        options: str | None = None,
        transcoder: VideoTranscoderConfig | None = None,
        native_capture: bool = False,
        output_index: int = 0,
    ) -> FFmpegSimulcastVideoSource:
        """Create a simulcast source from the current desktop capture input.

        Parameters
        ----------
        codec: :class:`str`
            The Discord video codec to encode.
        width: :class:`int`
            The source capture width in pixels.
        height: :class:`int`
            The source capture height in pixels.
        fps: :class:`int`
            The source frame rate.
        bitrate: :class:`int`
            The source video bitrate in bits per second.
        streams: List[:class:`discord.VoiceStream`]
            The simulcast stream descriptors to encode.
        executable: :class:`str`
            The FFmpeg executable to run.
        input_args: Optional[List[:class:`str`]]
            FFmpeg input arguments. When omitted, platform desktop capture defaults are used.
        stderr: Optional[Union[IO[:class:`bytes`], :class:`int`]]
            Where FFmpeg stderr is redirected.
        before_options: Optional[:class:`str`]
            Extra FFmpeg options placed before input options.
        options: Optional[:class:`str`]
            Extra FFmpeg output options.
        transcoder: Optional[:class:`VideoTranscoderConfig`]
            Encoder and filter selection options.
        native_capture: :class:`bool`
            Whether to use the native desktop capture bridge on supported platforms (currently Windows only).
        output_index: :class:`int`
            The native desktop output index to capture.

        Returns
        -------
        :class:`FFmpegSimulcastVideoSource`
            The created simulcast video source.

        Raises
        ------
        ClientException
            Duplicate stream RIDs, desktop capture, FFmpeg startup, or encoder selection failed.
        RuntimeError
            Platform desktop capture defaults are not available.
        ValueError
            ``codec`` is not a supported Discord video codec.
        """
        capture_args = list(input_args) if input_args is not None else None
        return cls(
            cls._build_sources(
                codec=codec,
                width=width,
                height=height,
                fps=fps,
                bitrate=bitrate,
                streams=streams,
                factory=lambda config: FFmpegVideoSource.from_desktop(
                    codec,
                    width=config.width,
                    height=config.height,
                    fps=config.fps,
                    bitrate=config.bitrate,
                    executable=executable,
                    input_args=capture_args,
                    stderr=stderr,
                    before_options=before_options,
                    options=options,
                    transcoder=transcoder,
                    native_capture=native_capture,
                    output_index=output_index,
                ),
            )
        )

    @classmethod
    def from_file(
        cls,
        source: str | os.PathLike[str],
        codec: str,
        *,
        streams: Sequence[VoiceStream],
        width: int,
        height: int,
        fps: int,
        bitrate: int,
        executable: str = 'ffmpeg',
        stderr: FFmpegStderr = None,
        before_options: str | None = None,
        options: str | None = None,
        source_codec: str | None = None,
        input_args: Sequence[str] | None = None,
        preview_input_args: Sequence[str] | None = None,
        transcoder: VideoTranscoderConfig | None = None,
    ) -> FFmpegSimulcastVideoSource:
        """Create a simulcast source from a video file.

        Parameters
        ----------
        source: Union[:class:`str`, :class:`os.PathLike`]
            The video file path to encode.
        codec: :class:`str`
            The Discord video codec to encode.
        width: :class:`int`
            The source video width in pixels.
        height: :class:`int`
            The source video height in pixels.
        fps: :class:`int`
            The source frame rate.
        bitrate: :class:`int`
            The source video bitrate in bits per second.
        streams: List[:class:`discord.VoiceStream`]
            The simulcast stream descriptors to encode.
        executable: :class:`str`
            The FFmpeg executable to run.
        stderr: Optional[Union[IO[:class:`bytes`], :class:`int`]]
            Where FFmpeg stderr is redirected.
        before_options: Optional[:class:`str`]
            Extra FFmpeg options placed before input options.
        options: Optional[:class:`str`]
            Extra FFmpeg output options.
        source_codec: Optional[:class:`str`]
            The input video codec used for decoder selection.
        input_args: Optional[List[:class:`str`]]
            Explicit FFmpeg input arguments.
        preview_input_args: Optional[List[:class:`str`]]
            FFmpeg input arguments used to produce stream previews.
        transcoder: Optional[:class:`VideoTranscoderConfig`]
            Encoder and filter selection options.

        Returns
        -------
        :class:`FFmpegSimulcastVideoSource`
            The created simulcast video source.

        Raises
        ------
        ClientException
            Duplicate stream RIDs, FFmpeg startup, or encoder selection failed.
        ValueError
            ``codec`` is not a supported Discord video codec.
        """
        source_path = os.fspath(source)
        return cls(
            cls._build_sources(
                codec=codec,
                width=width,
                height=height,
                fps=fps,
                bitrate=bitrate,
                streams=streams,
                factory=lambda config: FFmpegVideoSource.from_file(
                    source_path,
                    codec,
                    width=config.width,
                    height=config.height,
                    fps=config.fps,
                    bitrate=config.bitrate,
                    executable=executable,
                    stderr=stderr,
                    before_options=before_options,
                    options=options,
                    source_codec=source_codec,
                    input_args=input_args,
                    preview_input_args=preview_input_args,
                    transcoder=transcoder,
                ),
            )
        )

    @classmethod
    async def from_probe(
        cls,
        source: str | os.PathLike[str],
        codec: str | None = None,
        *,
        streams: Sequence[VoiceStream],
        width: int | None = None,
        height: int | None = None,
        fps: int | None = None,
        bitrate: int | None = None,
        method: VideoProbeMethod = None,
        executable: str = 'ffmpeg',
        stderr: FFmpegStderr = None,
        before_options: str | None = None,
        options: str | None = None,
        input_args: Sequence[str] | None = None,
        preview_input_args: Sequence[str] | None = None,
        transcoder: VideoTranscoderConfig | None = None,
    ) -> FFmpegSimulcastVideoSource:
        """Create a simulcast source while probing missing video metadata first.

        Parameters
        ----------
        source: Union[:class:`str`, :class:`os.PathLike`]
            The video file path to probe and encode.
        codec: Optional[:class:`str`]
            The Discord video codec to encode. If omitted, the first video stream is probed.
        width: Optional[:class:`int`]
            The source video width in pixels. If omitted, the first video stream is probed.
        height: Optional[:class:`int`]
            The source video height in pixels. If omitted, the first video stream is probed.
        fps: Optional[:class:`int`]
            The source frame rate. If omitted, the first video stream is probed.
        bitrate: Optional[:class:`int`]
            The source video bitrate in bits per second. If omitted, the first video stream is probed.
        streams: List[:class:`discord.VoiceStream`]
            The simulcast stream descriptors to encode.
        method: Optional[Union[:class:`str`, Callable[[str, str], Any]]]
            The video probing method.
        executable: :class:`str`
            The FFmpeg executable to run.
        stderr: Optional[Union[IO[:class:`bytes`], :class:`int`]]
            Where FFmpeg stderr is redirected.
        before_options: Optional[:class:`str`]
            Extra FFmpeg options placed before input options.
        options: Optional[:class:`str`]
            Extra FFmpeg output options.
        input_args: Optional[List[:class:`str`]]
            Explicit FFmpeg input arguments.
        preview_input_args: Optional[List[:class:`str`]]
            FFmpeg input arguments used to produce stream previews.
        transcoder: Optional[:class:`VideoTranscoderConfig`]
            Encoder and filter selection options.

        Returns
        -------
        :class:`FFmpegSimulcastVideoSource`
            The created simulcast video source.

        Raises
        ------
        ClientException
            Required video metadata could not be probed, duplicate stream RIDs were found, or FFmpeg setup failed.
        ValueError
            ``codec`` is not a supported Discord video codec.
        """
        source_path = os.fspath(source)
        video_probe, video_config = await _probe_video_config(
            source_path,
            codec=codec,
            width=width,
            height=height,
            fps=fps,
            bitrate=bitrate,
            method=method,
            executable=executable,
        )
        return cls.from_file(
            source_path,
            video_config.codec,
            width=video_config.width,
            height=video_config.height,
            fps=video_config.fps,
            bitrate=video_config.bitrate,
            streams=streams,
            executable=executable,
            stderr=stderr,
            before_options=before_options,
            options=options,
            source_codec=video_probe.codec,
            input_args=input_args,
            preview_input_args=preview_input_args,
            transcoder=transcoder,
        )


class FFmpegMediaSource(CompositeMediaSource):
    """A composite FFmpeg source that can provide audio and video together.

    Parameters
    ----------
    audio: Optional[:class:`discord.AudioSource`]
        The FFmpeg-backed audio source.
    video: Optional[:class:`MediaSource`]
        The FFmpeg-backed video source.
    """

    @classmethod
    def from_file(
        cls,
        source: str | os.PathLike[str] | BinaryIO,
        codec: str,
        *,
        width: int,
        height: int,
        fps: int,
        bitrate: int,
        executable: str = 'ffmpeg',
        pipe: bool = False,
        audio: bool = True,
        opus_audio: bool = False,
        audio_bitrate: int = 128,
        audio_stderr: BinaryIO | None = None,
        audio_before_options: str | None = None,
        audio_options: str | None = None,
        video_stderr: FFmpegStderr = None,
        video_before_options: str | None = None,
        video_options: str | None = None,
        video_source_codec: str | None = None,
        video_input_args: Sequence[str] | None = None,
        preview_input_args: Sequence[str] | None = None,
        video_transcoder: VideoTranscoderConfig | None = None,
    ) -> FFmpegMediaSource:
        """Create an FFmpeg media source from a file or video stdin pipe.

        Parameters
        ----------
        source: Union[:class:`str`, :class:`os.PathLike`, BinaryIO]
            A media path or binary stream.
        codec: :class:`str`
            The Discord video codec to encode.
        width: :class:`int`
            The encoded video width in pixels.
        height: :class:`int`
            The encoded video height in pixels.
        fps: :class:`int`
            The target video frame rate.
        bitrate: :class:`int`
            The target video bitrate in bits per second.
        executable: :class:`str`
            The FFmpeg executable to run.
        pipe: :class:`bool`
            Whether to pipe ``source`` into FFmpeg stdin for video.
        audio: :class:`bool`
            Whether to include audio from the input.
        opus_audio: :class:`bool`
            Whether to copy/probe Opus audio instead of decoding to PCM.
        audio_bitrate: :class:`int`
            The audio bitrate in kbps when using Opus audio.
        audio_stderr: Optional[BinaryIO]
            Where audio FFmpeg stderr is redirected.
        audio_before_options: Optional[:class:`str`]
            Extra audio FFmpeg options placed before input options.
        audio_options: Optional[:class:`str`]
            Extra audio FFmpeg output options.
        video_stderr: Optional[Union[IO[:class:`bytes`], :class:`int`]]
            Where video FFmpeg stderr is redirected.
        video_before_options: Optional[:class:`str`]
            Extra video FFmpeg options placed before input options.
        video_options: Optional[:class:`str`]
            Extra video FFmpeg output options.
        video_source_codec: Optional[:class:`str`]
            The input video codec used for decoder selection.
        video_input_args: Optional[List[:class:`str`]]
            Explicit video FFmpeg input arguments.
        preview_input_args: Optional[List[:class:`str`]]
            FFmpeg input arguments used to produce stream previews.
        video_transcoder: Optional[:class:`VideoTranscoderConfig`]
            Video encoder and filter selection options.

        Returns
        -------
        :class:`FFmpegMediaSource`
            The created media source.

        Raises
        ------
        ClientException
            ``pipe=True`` was used with ``audio=True`` or FFmpeg setup failed.
        TypeError
            ``source`` is incompatible with the selected ``pipe`` mode.
        ValueError
            ``codec`` is not a supported Discord video codec.
        """
        if pipe and audio:
            raise discord.ClientException(
                'pipe=True cannot feed both audio and video from one stream; pass audio=False or provide audio separately.'
            )

        pipe_source, source_input = _resolve_ffmpeg_source_input(source, pipe=pipe)
        video_source_input = pipe_source if pipe_source is not None else source_input
        audio_source: AudioSource | None = None
        if audio:
            if opus_audio:
                audio_source = FFmpegOpusAudio(
                    source_input,
                    bitrate=audio_bitrate,
                    executable=executable,
                    stderr=audio_stderr,
                    before_options=audio_before_options,
                    options=audio_options,
                )
            else:
                audio_source = FFmpegPCMAudio(
                    source_input,
                    executable=executable,
                    stderr=audio_stderr,
                    before_options=audio_before_options,
                    options=audio_options,
                )

        try:
            video_source = FFmpegVideoSource.from_file(
                video_source_input,
                codec,
                width=width,
                height=height,
                fps=fps,
                bitrate=bitrate,
                executable=executable,
                pipe=pipe,
                stderr=video_stderr,
                before_options=video_before_options,
                options=video_options,
                source_codec=video_source_codec,
                input_args=video_input_args,
                preview_input_args=preview_input_args,
                transcoder=video_transcoder,
            )
        except Exception:
            if audio_source is not None:
                audio_source.cleanup()
            raise

        return cls(audio=audio_source, video=video_source)

    @classmethod
    async def from_probe(
        cls,
        source: str | os.PathLike[str],
        codec: str | None = None,
        *,
        width: int | None = None,
        height: int | None = None,
        fps: int | None = None,
        bitrate: int | None = None,
        method: str | Callable[[str, str], tuple[str | None, int | None]] | None = None,
        video_method: VideoProbeMethod = None,
        executable: str = 'ffmpeg',
        audio: bool = True,
        audio_stderr: BinaryIO | None = None,
        audio_before_options: str | None = None,
        audio_options: str | None = None,
        video_stderr: FFmpegStderr = None,
        video_before_options: str | None = None,
        video_options: str | None = None,
        video_input_args: Sequence[str] | None = None,
        preview_input_args: Sequence[str] | None = None,
        video_transcoder: VideoTranscoderConfig | None = None,
    ) -> FFmpegMediaSource:
        """Create a media source while probing media metadata first.

        This mirrors :meth:`discord.FFmpegOpusAudio.from_probe` for unified
        audio/video playback, letting FFmpeg copy Opus audio when possible and
        using the first video stream for missing codec, width, height, FPS, and
        bitrate values.

        Parameters
        ----------
        source: Union[:class:`str`, :class:`os.PathLike`]
            The media file path to probe and encode.
        codec: Optional[:class:`str`]
            The Discord video codec to encode. If omitted, the first video stream is probed.
        width: Optional[:class:`int`]
            The encoded video width in pixels. If omitted, the first video stream is probed.
        height: Optional[:class:`int`]
            The encoded video height in pixels. If omitted, the first video stream is probed.
        fps: Optional[:class:`int`]
            The target video frame rate. If omitted, the first video stream is probed.
        bitrate: Optional[:class:`int`]
            The target video bitrate in bits per second. If omitted, the first video stream is probed.
        method: Optional[Union[:class:`str`, Callable[[str, str], Any]]]
            The audio probing method passed to :meth:`discord.FFmpegOpusAudio.from_probe`.
        video_method: Optional[Union[:class:`str`, Callable[[str, str], Any]]]
            The video probing method.
        executable: :class:`str`
            The FFmpeg executable to run.
        audio: :class:`bool`
            Whether to include audio from the input.
        audio_stderr: Optional[BinaryIO]
            Where audio FFmpeg stderr is redirected.
        audio_before_options: Optional[:class:`str`]
            Extra audio FFmpeg options placed before input options.
        audio_options: Optional[:class:`str`]
            Extra audio FFmpeg output options.
        video_stderr: Optional[Union[IO[:class:`bytes`], :class:`int`]]
            Where video FFmpeg stderr is redirected.
        video_before_options: Optional[:class:`str`]
            Extra video FFmpeg options placed before input options.
        video_options: Optional[:class:`str`]
            Extra video FFmpeg output options.
        video_input_args: Optional[List[:class:`str`]]
            Explicit video FFmpeg input arguments.
        preview_input_args: Optional[List[:class:`str`]]
            FFmpeg input arguments used to produce stream previews.
        video_transcoder: Optional[:class:`VideoTranscoderConfig`]
            Video encoder and filter selection options.

        Returns
        -------
        :class:`FFmpegMediaSource`
            The created media source.

        Raises
        ------
        ClientException
            Required media metadata could not be probed or FFmpeg setup failed.
        ValueError
            ``codec`` is not a supported Discord video codec.
        """
        source_path = os.fspath(source)
        audio_source: AudioSource | None = None
        if audio:
            audio_source = await FFmpegOpusAudio.from_probe(
                source_path,
                method=method,
                executable=executable,
                stderr=audio_stderr,
                before_options=audio_before_options,
                options=audio_options,
            )

        try:
            video_source = await FFmpegVideoSource.from_probe(
                source_path,
                codec,
                width=width,
                height=height,
                fps=fps,
                bitrate=bitrate,
                method=video_method,
                executable=executable,
                stderr=video_stderr,
                before_options=video_before_options,
                options=video_options,
                input_args=video_input_args,
                preview_input_args=preview_input_args,
                transcoder=video_transcoder,
            )
        except Exception:
            if audio_source is not None:
                audio_source.cleanup()
            raise

        return cls(audio=audio_source, video=video_source)

    @classmethod
    async def probe_video(
        cls,
        source: str | os.PathLike[str],
        *,
        method: VideoProbeMethod = None,
        executable: str = 'ffmpeg',
    ) -> VideoProbeInfo:
        """Probe the first video stream for codec, width, height, FPS, and bitrate.

        Parameters
        ----------
        source: Union[:class:`str`, :class:`os.PathLike`]
            The media file path to probe.
        method: Optional[Union[:class:`str`, Callable[[str, str], Any]]]
            The video probing method.
        executable: :class:`str`
            The FFmpeg executable used to locate ffprobe or run fallback probing.

        Returns
        -------
        :class:`VideoProbeInfo`
            The discovered video stream metadata.
        """
        source_path = os.fspath(source)
        method = method or 'native'
        fallback = None

        if isinstance(method, str):
            probefunc = getattr(cls, '_probe_video_' + method, None)
            if probefunc is None:
                raise AttributeError(f'Invalid video probe method {method!r}')
            if probefunc is cls._probe_video_native:
                fallback = cls._probe_video_fallback
        elif callable(method):
            probefunc = method
            fallback = cls._probe_video_fallback
        else:
            raise TypeError(f"Expected str or callable for parameter 'method', not '{method.__class__.__name__}'")

        loop = asyncio.get_running_loop()
        try:
            return _coerce_video_probe_info(await loop.run_in_executor(None, lambda: probefunc(source_path, executable)))
        except Exception:
            if fallback is None:
                return VideoProbeInfo()

        try:
            return _coerce_video_probe_info(await loop.run_in_executor(None, lambda: fallback(source_path, executable)))
        except Exception:
            return VideoProbeInfo()

    @staticmethod
    def _probe_video_native(source: str, executable: str = 'ffmpeg') -> VideoProbeInfo:
        probe_executable = _ffprobe_executable(executable)
        output = subprocess.check_output(
            [
                probe_executable,
                '-v',
                'quiet',
                '-print_format',
                'json',
                '-show_streams',
                '-select_streams',
                'v:0',
                source,
            ],
            timeout=20,
        )
        if not output:
            return VideoProbeInfo()

        data = json.loads(output)
        streams = data.get('streams') or []
        if not streams:
            return VideoProbeInfo()

        stream = streams[0]
        return VideoProbeInfo(
            width=_int_or_none(stream.get('width')),
            height=_int_or_none(stream.get('height')),
            fps=_fps_or_none(stream.get('avg_frame_rate') or stream.get('r_frame_rate')),
            bitrate=_bitrate_bps_or_none(stream.get('bit_rate')),
            codec=_video_codec_or_none(stream.get('codec_name') or stream.get('codec_tag_string')),
        )

    @staticmethod
    def _probe_video_fallback(source: str, executable: str = 'ffmpeg') -> VideoProbeInfo:
        process = subprocess.Popen(
            [executable, '-hide_banner', '-i', source],
            creationflags=CREATE_NO_WINDOW,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            output, _ = process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        text = output.decode('utf8', 'replace')
        video_line = next((line for line in text.splitlines() if ' Video: ' in line), '')
        codec_match = re.search(r'Video:\s*([^,\s]+)', video_line)
        resolution = re.search(r'(?<![A-Z])(\d{2,5})x(\d{2,5})(?:[,\s]|$)', video_line)
        fps_match = re.search(r'(\d+(?:\.\d+)?) fps', video_line)
        bitrate_match = re.search(r'(\d+) kb/s', video_line)
        return VideoProbeInfo(
            width=int(resolution.group(1)) if resolution else None,
            height=int(resolution.group(2)) if resolution else None,
            fps=_fps_or_none(fps_match.group(1)) if fps_match else None,
            bitrate=int(bitrate_match.group(1)) * 1000 if bitrate_match else None,
            codec=_video_codec_or_none(codec_match.group(1) if codec_match else None),
        )

    @classmethod
    def preflight_desktop(
        cls,
        *,
        width: int,
        height: int,
        fps: int = 1,
        codec: str = 'H264',
        bitrate: int = 4_000_000,
        executable: str = 'ffmpeg',
        input_args: Sequence[str] | None = None,
        before_options: str | None = None,
        video_transcoder: VideoTranscoderConfig | None = None,
        native_capture: bool = False,
        output_index: int = 0,
        timeout: float = 15.0,
    ) -> None:
        """Check whether the configured FFmpeg desktop input can capture a frame.

        Parameters
        ----------
        width: :class:`int`
            The capture width in pixels.
        height: :class:`int`
            The capture height in pixels.
        fps: :class:`int`
            The capture frame rate.
        codec: :class:`str`
            The Discord video codec to encode.
        bitrate: :class:`int`
            The target video bitrate in bits per second.
        executable: :class:`str`
            The FFmpeg executable to run.
        input_args: Optional[List[:class:`str`]]
            FFmpeg input arguments. When omitted, platform desktop capture defaults are used.
        before_options: Optional[:class:`str`]
            Extra FFmpeg options placed before input options.
        video_transcoder: Optional[:class:`VideoTranscoderConfig`]
            Video encoder and filter selection options.
        native_capture: :class:`bool`
            Whether to use the native desktop capture bridge on supported platforms.
        output_index: :class:`int`
            The native desktop output index to capture.
        timeout: :class:`float`
            Maximum seconds to wait for the preflight encode.

        Raises
        ------
        ClientException
            Desktop capture, FFmpeg startup, encoder validation, or the preflight encode failed.
        RuntimeError
            Platform desktop capture defaults are not available.
        ValueError
            ``codec`` is not a supported Discord video codec.
        """
        FFmpegVideoSource.preflight_desktop(
            width=width,
            height=height,
            fps=fps,
            codec=codec,
            bitrate=bitrate,
            executable=executable,
            input_args=input_args,
            before_options=before_options,
            transcoder=video_transcoder,
            native_capture=native_capture,
            output_index=output_index,
            timeout=timeout,
        )

    @classmethod
    def from_desktop(
        cls,
        codec: str,
        *,
        width: int,
        height: int,
        fps: int,
        bitrate: int,
        executable: str = 'ffmpeg',
        input_args: Sequence[str] | None = None,
        stderr: FFmpegStderr = None,
        before_options: str | None = None,
        options: str | None = None,
        audio: AudioSource | None = None,
        video_transcoder: VideoTranscoderConfig | None = None,
        native_capture: bool = False,
        output_index: int = 0,
    ) -> FFmpegMediaSource:
        """Create an FFmpeg media source from desktop capture video.

        Parameters
        ----------
        codec: :class:`str`
            The Discord video codec to encode.
        width: :class:`int`
            The capture width in pixels.
        height: :class:`int`
            The capture height in pixels.
        fps: :class:`int`
            The capture frame rate.
        bitrate: :class:`int`
            The target video bitrate in bits per second.
        executable: :class:`str`
            The FFmpeg executable to run.
        input_args: Optional[List[:class:`str`]]
            FFmpeg input arguments. When omitted, platform desktop capture defaults are used.
        stderr: Optional[Union[IO[:class:`bytes`], :class:`int`]]
            Where video FFmpeg stderr is redirected.
        before_options: Optional[:class:`str`]
            Extra FFmpeg options placed before input options.
        options: Optional[:class:`str`]
            Extra FFmpeg output options.
        audio: Optional[:class:`discord.AudioSource`]
            Existing audio source to combine with the desktop video source.
        video_transcoder: Optional[:class:`VideoTranscoderConfig`]
            Video encoder and filter selection options.
        native_capture: :class:`bool`
            Whether to use the native desktop capture bridge on supported platforms.
        output_index: :class:`int`
            The native desktop output index to capture.

        Returns
        -------
        :class:`FFmpegMediaSource`
            The created media source.

        Raises
        ------
        ClientException
            Desktop capture, FFmpeg startup, or encoder selection failed.
        RuntimeError
            Platform desktop capture defaults are not available.
        ValueError
            ``codec`` is not a supported Discord video codec.
        """
        try:
            video_source = FFmpegVideoSource.from_desktop(
                codec,
                width=width,
                height=height,
                fps=fps,
                bitrate=bitrate,
                executable=executable,
                input_args=input_args,
                stderr=stderr,
                before_options=before_options,
                options=options,
                transcoder=video_transcoder,
                native_capture=native_capture,
                output_index=output_index,
            )
        except Exception:
            if audio is not None:
                audio.cleanup()
            raise

        return cls(audio=audio, video=video_source)


class MediaSink(abc.ABC):
    """Base class for receive-side media sinks.

    Sinks can be chained by passing a destination sink to another sink. The root
    sink is owned by :meth:`VoiceClient.listen` and is cleaned up when listening
    stops.

    Parameters
    ----------
    destination: Optional[:class:`MediaSink`]
        A child sink to register under this sink.
    """

    _voice_client: discord.VoiceProtocol | None
    _parent: MediaSink | None = None
    _child: MediaSink | None = None

    def __init__(self, destination: MediaSink | None = None, /) -> None:
        self._voice_client = None
        self._parent = None
        self._child = None
        self._closed = False
        if destination is not None:
            self._register_child(destination)

    def __del__(self) -> None:
        try:
            self.cleanup()
        except Exception:
            logger = globals().get('log')
            if logger is not None:
                logger.debug('Ignoring exception during %s destructor cleanup.', self.__class__.__name__, exc_info=True)

    def _check_child(self, child: MediaSink) -> None:
        if not isinstance(child, MediaSink):
            raise TypeError(f'expected MediaSink not {child.__class__.__name__}')
        if child is self or child.parent is not None or child in self.root.walk_children():
            raise RuntimeError('Sink is already registered')
        if child.closed:
            raise RuntimeError('Sink is already closed')

    def _register_child(self, child: MediaSink) -> None:
        self._check_child(child)
        self._child = child
        child._parent = self

    @property
    def root(self) -> MediaSink:
        """:class:`MediaSink`: The root sink in this sink chain."""
        if self.parent is None:
            return self
        return self.parent.root

    @property
    def parent(self) -> MediaSink | None:
        """Optional[:class:`MediaSink`]: The parent sink in this chain."""
        return self._parent

    @property
    def child(self) -> MediaSink | None:
        """Optional[:class:`MediaSink`]: The first child sink, if any."""
        return self._child

    @property
    def children(self) -> Sequence[MediaSink]:
        """Sequence[:class:`MediaSink`]: Child sinks registered under this sink."""
        return (self._child,) if self._child is not None else ()

    @property
    def voice_client(self) -> discord.VoiceProtocol | None:
        """Optional[:class:`discord.VoiceProtocol`]: The voice client owning this sink."""
        if self.parent is not None:
            return self.parent.voice_client
        return self._voice_client

    @property
    def client(self) -> discord.Client | None:
        """Optional[:class:`discord.Client`]: The Discord client owning this sink."""
        voice_client = self.voice_client
        return voice_client.client if voice_client is not None else None

    @property
    def closed(self) -> bool:
        """:class:`bool`: Whether this sink has been cleaned up."""
        return self._closed

    def walk_children(self, *, with_self: bool = False) -> Generator[MediaSink, None, None]:
        """Yield child sinks depth-first.

        Parameters
        ----------
        with_self: :class:`bool`
            Whether to yield this sink before its children.

        Yields
        ------
        :class:`MediaSink`
            Child sinks in depth-first order.
        """
        if with_self:
            yield self
        for child in self.children:
            yield child
            yield from child.walk_children()

    def wants_media(self, media_type: str, codec: str) -> bool:
        """Return whether this sink wants a media type/codec pair.

        Parameters
        ----------
        media_type: :class:`str`
            The decoded media type, such as ``audio`` or ``video``.
        codec: :class:`str`
            The decoded media codec name.

        Returns
        -------
        :class:`bool`
            Whether this sink wants packets with the provided media type and codec.
        """
        return not self._closed

    @abc.abstractmethod
    def write(self, packet: MediaPacket) -> Any:
        raise NotImplementedError

    def cleanup(self) -> None:
        """Close this sink and all child sinks."""
        if self._closed:
            return
        self._closed = True
        for child in self.children:
            child.cleanup()


class _DestinationSink(MediaSink):
    @property
    def destination(self) -> MediaSink:
        """:class:`MediaSink`: The child sink this wrapper forwards to."""

        child = self.child
        if child is None:
            raise RuntimeError('Sink does not have a destination')
        return child


class MultiSink(MediaSink):
    """Fan out each received packet to multiple child sinks.

    Parameters
    ----------
    destinations: List[:class:`MediaSink`]
        The child sinks to fan out to.
    """

    def __init__(self, destinations: Sequence[MediaSink], /) -> None:
        super().__init__()
        self._children: list[MediaSink] = []
        for child in destinations:
            self._register_child(child)

    def _register_child(self, child: MediaSink) -> None:
        self._check_child(child)
        child._parent = self
        self._children.append(child)

    @property
    def child(self) -> MediaSink | None:
        """Optional[:class:`MediaSink`]: The first child sink, if any."""
        return self._children[0] if self._children else None

    @property
    def children(self) -> Sequence[MediaSink]:
        """Sequence[:class:`MediaSink`]: Child sinks registered under this fan-out."""
        return SequenceProxy(self._children)

    def write(self, packet: MediaPacket) -> list[Any]:
        if self._closed:
            return []

        return [child.write(packet) for child in self._children if child.wants_media(packet.media_type, packet.codec)]


class PerUserSink(MediaSink):
    """Lazily creates one child sink per received user.

    If a packet arrives before Discord has mapped the SSRC to a user ID, the
    packet is routed by SSRC. When a later packet for that SSRC has a user ID,
    the existing child is promoted to the user key so recordings stay together.

    Parameters
    ----------
    factory: Callable[[:class:`int`], :class:`MediaSink`]
        Callable used to create a sink for each user ID or fallback SSRC.
    fallback_to_ssrc: :class:`bool`
        Whether packets without a user ID should be routed by SSRC.

    Attributes
    ----------
    factory: Callable[[:class:`int`], :class:`MediaSink`]
        Callable used to create child sinks.
    fallback_to_ssrc: :class:`bool`
        Whether packets without a user ID are routed by SSRC.
    """

    def __init__(self, factory: Callable[[int], MediaSink], /, *, fallback_to_ssrc: bool = True) -> None:
        if not callable(factory):
            raise TypeError('factory must be callable')

        super().__init__()
        self.factory = factory
        self.fallback_to_ssrc = fallback_to_ssrc
        self._children_by_key: dict[tuple[str, int], MediaSink] = {}
        self._user_ids_by_ssrc: dict[int, int] = {}

    def _register_child(self, child: MediaSink) -> None:
        self._check_child(child)
        child._parent = self
        if self._child is None:
            self._child = child

    @property
    def children(self) -> Sequence[MediaSink]:
        """Sequence[:class:`MediaSink`]: All currently-created per-user sinks."""
        return tuple(self._children_by_key.values())

    def wants_media(self, media_type: str, codec: str) -> bool:
        return not self._closed

    def _packet_key(self, packet: MediaPacket) -> tuple[str, int] | None:
        if packet.user_id is not None:
            self._user_ids_by_ssrc[packet.ssrc] = packet.user_id
            user_key = ('user', packet.user_id)
            fallback_key = ('ssrc', packet.ssrc)
            if fallback_key in self._children_by_key and user_key not in self._children_by_key:
                self._children_by_key[user_key] = self._children_by_key.pop(fallback_key)
            return user_key
        user_id = self._user_ids_by_ssrc.get(packet.ssrc)
        if user_id is not None:
            return ('user', user_id)
        if self.fallback_to_ssrc:
            return ('ssrc', packet.ssrc)
        return None

    def _get_child(self, key: tuple[str, int]) -> MediaSink:
        child = self._children_by_key.get(key)
        if child is not None:
            return child

        child = self.factory(key[1])
        if not isinstance(child, MediaSink):
            raise TypeError(f'factory must return MediaSink not {child.__class__.__name__}')
        self._register_child(child)
        self._children_by_key[key] = child
        return child

    def write(self, packet: MediaPacket) -> Any:
        if self._closed:
            return None

        key = self._packet_key(packet)
        if key is None:
            return None
        child = self._get_child(key)
        if child.wants_media(packet.media_type, packet.codec):
            return child.write(packet)
        return None

    def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        for child in self.children:
            child.cleanup()
        self._children_by_key.clear()
        self._user_ids_by_ssrc.clear()
        self._child = None


class _FilteredMediaSink(MediaSink):
    def __init__(
        self,
        destination: MediaSink | None = None,
        /,
        *,
        media_types: Sequence[str] | None = None,
        codecs: Sequence[str] | None = None,
    ) -> None:
        super().__init__(destination)
        self.media_types = None if media_types is None else frozenset(media_types)
        self.codecs = None if codecs is None else frozenset(codec.lower() for codec in codecs)

    @staticmethod
    def _matches(
        media_type: str,
        codec: str,
        *,
        media_types: frozenset[str] | None,
        codecs: frozenset[str] | None,
    ) -> bool:
        if media_types is not None and media_type not in media_types:
            return False
        return codecs is None or codec.lower() in codecs

    def wants_media(self, media_type: str, codec: str) -> bool:
        if self._closed:
            return False
        return self._matches(media_type, codec, media_types=self.media_types, codecs=self.codecs)


class BasicSink(_FilteredMediaSink):
    """A sink that forwards each accepted packet to a callback.

    Parameters
    ----------
    callback: Callable[[:class:`MediaPacket`], Any]
        The callback invoked for each accepted packet.
    media_types: Optional[List[:class:`str`]]
        Media types to accept.
    codecs: Optional[List[:class:`str`]]
        Codec names to accept.

    Attributes
    ----------
    callback: Callable[[:class:`MediaPacket`], Any]
        The callback invoked for each accepted packet.
    """

    def __init__(
        self,
        callback: Callable[[MediaPacket], Any],
        *,
        media_types: Sequence[str] | None = None,
        codecs: Sequence[str] | None = None,
    ) -> None:
        if not callable(callback):
            raise TypeError('callback must be callable')

        super().__init__(media_types=media_types, codecs=codecs)
        self.callback = callback

    def write(self, packet: MediaPacket) -> Any:
        if self._closed:
            return None
        return self.callback(packet)


class ConditionalFilter(_DestinationSink):
    """A sink filter that forwards packets when a predicate returns true.

    Parameters
    ----------
    destination: :class:`MediaSink`
        The child sink to forward accepted packets to.
    predicate: Callable[[:class:`MediaPacket`], :class:`bool`]
        The predicate used to accept packets.

    Attributes
    ----------
    predicate: Callable[[:class:`MediaPacket`], :class:`bool`]
        The predicate used to accept packets.
    """

    def __init__(self, destination: MediaSink, predicate: Callable[[MediaPacket], bool], /) -> None:
        if not callable(predicate):
            raise TypeError('predicate must be callable')

        super().__init__(destination)
        self.predicate = predicate

    def wants_media(self, media_type: str, codec: str) -> bool:
        if self._closed:
            return False
        return self.destination.wants_media(media_type, codec)

    def write(self, packet: MediaPacket) -> Any:
        if self._closed:
            return None
        if self.predicate(packet):
            return self.destination.write(packet)
        return None


class TimedFilter(ConditionalFilter):
    """Forward packets for a bounded duration.

    Parameters
    ----------
    destination: :class:`MediaSink`
        The child sink to forward accepted packets to.
    duration: :class:`float`
        The number of seconds to accept packets for.
    start_on_init: :class:`bool`
        Whether the duration timer starts when the filter is created.

    Attributes
    ----------
    duration: :class:`float`
        The number of seconds to accept packets for.
    start_time: Optional[:class:`float`]
        The monotonic time when the filter started accepting packets.
    """

    def __init__(self, destination: MediaSink, duration: float, *, start_on_init: bool = False) -> None:
        self.duration = max(0.0, duration)
        self.start_time = time.perf_counter() if start_on_init else None
        super().__init__(destination, self._predicate)

    def _predicate(self, _packet: MediaPacket) -> bool:
        if self.start_time is None:
            self.start_time = time.perf_counter()
        return time.perf_counter() - self.start_time < self.duration


class UserFilter(ConditionalFilter):
    """Forward only packets from a specific user.

    Parameters
    ----------
    destination: :class:`MediaSink`
        The child sink to forward accepted packets to.
    user: :class:`discord.abc.Snowflake`
        The user whose media packets should be accepted.

    Attributes
    ----------
    user_id: :class:`int`
        The ID of the accepted user.
    """

    def __init__(self, destination: MediaSink, user: Snowflake, /) -> None:
        self.user_id = user.id
        super().__init__(destination, self._predicate)

    def _predicate(self, packet: MediaPacket) -> bool:
        return packet.user_id == self.user_id


class MediaFilter(ConditionalFilter):
    """Forward packets matching media type, codec, and user filters.

    Parameters
    ----------
    destination: :class:`MediaSink`
        The child sink to forward accepted packets to.
    media_types: Optional[List[:class:`str`]]
        Media types to accept.
    codecs: Optional[List[:class:`str`]]
        Codec names to accept.
    users: Optional[List[:class:`discord.abc.Snowflake`]]
        Users whose media packets should be accepted.

    Attributes
    ----------
    media_types: Optional[Set[:class:`str`]]
        Media types accepted by this filter.
    codecs: Optional[Set[:class:`str`]]
        Codec names accepted by this filter.
    user_ids: Optional[Set[:class:`int`]]
        User IDs accepted by this filter.
    """

    def __init__(
        self,
        destination: MediaSink,
        *,
        media_types: Sequence[str] | None = None,
        codecs: Sequence[str] | None = None,
        users: Sequence[Snowflake] | None = None,
    ) -> None:
        self.media_types = None if media_types is None else frozenset(media_types)
        self.codecs = None if codecs is None else frozenset(codec.lower() for codec in codecs)
        self.user_ids = frozenset(user.id for user in users) if users is not None else None
        super().__init__(destination, self._predicate)

    def wants_media(self, media_type: str, codec: str) -> bool:
        if not super().wants_media(media_type, codec):
            return False
        return _FilteredMediaSink._matches(
            media_type,
            codec,
            media_types=self.media_types,
            codecs=self.codecs,
        )

    def _predicate(self, packet: MediaPacket) -> bool:
        if not _FilteredMediaSink._matches(
            packet.media_type,
            packet.codec,
            media_types=self.media_types,
            codecs=self.codecs,
        ):
            return False
        return self.user_ids is None or packet.user_id in self.user_ids


class MediaSinkVolumeTransformer(_DestinationSink):
    """Adjusts PCM audio volume before forwarding to another sink.

    Parameters
    ----------
    destination: :class:`MediaSink`
        The child sink to forward transformed packets to.
    volume: :class:`float`
        The initial audio volume multiplier.

    Attributes
    ----------
    volume: :class:`float`
        The audio volume multiplier.
    """

    def __init__(self, destination: MediaSink, volume: float = 1.0, /) -> None:
        super().__init__(destination)
        self._decoder = _OpusDecoderCache()
        self.volume = volume

    @property
    def volume(self) -> float:
        """:class:`float`: The audio volume multiplier."""
        return self._volume

    @volume.setter
    def volume(self, value: float) -> None:
        """:class:`float`: Set the audio volume multiplier."""
        self._volume = max(value, 0.0)

    def wants_media(self, media_type: str, codec: str) -> bool:
        if self._closed:
            return False
        if media_type == 'audio' and codec == 'opus':
            return self.destination.wants_media('audio', 'pcm')
        return self.destination.wants_media(media_type, codec)

    def write(self, packet: MediaPacket) -> Any:
        if self._closed:
            return None
        if packet.media_type != 'audio' or packet.codec not in {'opus', 'pcm'}:
            return self.destination.write(packet)

        pcm = self._decoder.decode(packet) if packet.codec == 'opus' else packet.payload
        payload = pcm16_mul(pcm, min(self._volume, 2.0)) if pcm else pcm
        transformed = packet.replace(codec='pcm', payload=payload)
        return self.destination.write(transformed)

    def cleanup(self) -> None:
        self._decoder.clear()
        super().cleanup()


@dataclass(slots=True)
class _OpusDecodeState:
    decoder: OpusDecoder
    sequence: int | None = None
    timestamp: int | None = None


class _OpusDecoderCache:
    def __init__(self) -> None:
        self._decoders: dict[int, _OpusDecodeState] = {}

    def _state_for(self, packet: MediaPacket) -> _OpusDecodeState:
        state = self._decoders.get(packet.ssrc)
        if state is None:
            state = _OpusDecodeState(OpusDecoder())
            self._decoders[packet.ssrc] = state
        return state

    @staticmethod
    def _packet_samples(packet: MediaPacket) -> int:
        frames = OpusDecoder.packet_get_nb_frames(packet.payload)
        samples_per_frame = OpusDecoder.packet_get_samples_per_frame(packet.payload)
        return max(1, frames * samples_per_frame)

    def decode(self, packet: MediaPacket) -> bytes:
        if packet.codec == 'pcm':
            return packet.payload
        if packet.media_type != 'audio' or packet.codec != 'opus':
            raise TypeError(f'cannot decode {packet.media_type}/{packet.codec} as Opus audio')

        state = self._state_for(packet)
        payload = state.decoder.decode(packet.payload, fec=False)
        state.sequence = packet.sequence
        state.timestamp = packet.timestamp
        return payload

    def decode_packets(self, packet: MediaPacket, *, fec: bool = False) -> list[MediaPacket]:
        if packet.codec == 'pcm':
            return [packet]
        if packet.media_type != 'audio' or packet.codec != 'opus':
            raise TypeError(f'cannot decode {packet.media_type}/{packet.codec} as Opus audio')

        state = self._state_for(packet)
        samples = self._packet_samples(packet)
        packets: list[MediaPacket] = []

        if fec and state.sequence is not None and state.timestamp is not None:
            sequence_delta = _sequence_delta(packet.sequence, state.sequence)
            timestamp_delta = _rtp_timestamp_delta(packet.timestamp, state.timestamp)
            if sequence_delta == 2 and timestamp_delta == samples * 2:
                try:
                    packets.append(
                        packet.replace(
                            codec='pcm',
                            payload=state.decoder.decode(packet.payload, fec=True),
                            sequence=(packet.sequence - 1) & 0xFFFF,
                            timestamp=(packet.timestamp - samples) & 0xFFFFFFFF,
                            raw=b'',
                            extension_payload=b'',
                            rtp_extended=False,
                            rtp_extensions=(),
                            rtp_packets=(),
                            speaking_flags=None,
                            audio_level=None,
                            audio_voice_activity=None,
                        )
                    )
                except Exception:
                    log.debug('Failed to recover missing Opus packet with in-band FEC.', exc_info=True)

        packets.append(packet.replace(codec='pcm', payload=state.decoder.decode(packet.payload, fec=False)))
        state.sequence = packet.sequence
        state.timestamp = packet.timestamp
        return packets

    def clear(self) -> None:
        self._decoders.clear()


class PCMDecodeSink(_DestinationSink):
    """Decodes Opus audio packets to PCM before forwarding them to another sink.

    Parameters
    ----------
    destination: :class:`MediaSink`
        The child sink to forward decoded packets to.
    fec: :class:`bool`
        Whether to attempt Opus in-band FEC recovery for one missing packet.

    Attributes
    ----------
    fec: :class:`bool`
        Whether Opus in-band FEC recovery is enabled.
    """

    def __init__(self, destination: MediaSink, /, *, fec: bool = False) -> None:
        super().__init__(destination)
        self.fec = fec
        self._decoder = _OpusDecoderCache()

    def wants_media(self, media_type: str, codec: str) -> bool:
        if self._closed:
            return False
        if media_type == 'audio' and codec == 'opus':
            return self.destination.wants_media('audio', 'pcm')
        return self.destination.wants_media(media_type, codec)

    def write(self, packet: MediaPacket) -> Any:
        if self._closed:
            return None
        if packet.media_type == 'audio' and packet.codec == 'opus':
            if not self.fec:
                packet = packet.replace(codec='pcm', payload=self._decoder.decode(packet))
                return self.destination.write(packet)
            result = None
            for decoded in self._decoder.decode_packets(packet, fec=self.fec):
                result = self.destination.write(decoded)
            return result
        return self.destination.write(packet)

    def cleanup(self) -> None:
        self._decoder.clear()
        super().cleanup()


class SilenceFillSink(_DestinationSink):
    """Pads short receive-audio gaps with synthetic PCM silence packets.

    The sink forwards real packets to its destination, then emits ``audio/pcm``
    silence for active audio SSRCs after a short gap. This is useful for sinks
    that consume a continuous PCM timeline, such as FFmpeg, callback, and queue
    consumers. The default silence duration is bounded so a speaker that stops
    talking does not produce endless output.

    Parameters
    ----------
    destination: :class:`MediaSink`
        The child sink to forward real and synthetic packets to.
    silence_after: :class:`float`
        Seconds to wait after the last audio packet before emitting silence.
    frame_duration: :class:`float`
        Duration of each synthetic PCM silence packet in seconds.
    max_silence: Optional[:class:`float`]
        Maximum seconds of silence to emit for each active audio track.

    Attributes
    ----------
    silence_after: :class:`float`
        Seconds to wait after the last audio packet before emitting silence.
    frame_duration: :class:`float`
        Duration of each synthetic PCM silence packet in seconds.
    max_silence: Optional[:class:`float`]
        Maximum seconds of silence to emit for each active audio track.
    """

    def __init__(
        self,
        destination: MediaSink,
        /,
        *,
        silence_after: float = 0.06,
        frame_duration: float = 0.02,
        max_silence: float | None = 1.0,
    ) -> None:
        self.silence_after = max(0.0, silence_after)
        self.frame_duration = max(0.001, frame_duration)
        self.max_silence = max_silence if max_silence is None else max(0.0, max_silence)
        self._samples_per_frame = max(1, round(OpusDecoder.SAMPLING_RATE * self.frame_duration))
        self._timestamp_step = self._samples_per_frame
        self._silence = b'\x00' * (self._samples_per_frame * OpusDecoder.SAMPLE_SIZE)
        self._decoder = _OpusDecoderCache()
        self._tracks: dict[tuple[str, int], _SilenceTrack] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

        super().__init__(destination)
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name='native-voice-silence-fill',
        )
        self._thread.start()

    def wants_media(self, media_type: str, codec: str) -> bool:
        if self._closed:
            return False
        if media_type == 'audio':
            return self.destination.wants_media(media_type, codec) or self.destination.wants_media('audio', 'pcm')
        return self.destination.wants_media(media_type, codec)

    def _remember_audio_packet(self, packet: MediaPacket) -> None:
        if packet.media_type != 'audio':
            return
        if not self.destination.wants_media('audio', 'pcm'):
            return

        now = time.perf_counter()
        track_key = ('user', packet.user_id) if packet.user_id is not None else ('ssrc', packet.ssrc)
        with self._lock:
            self._tracks[track_key] = _SilenceTrack(
                packet=packet,
                next_due=now + self.silence_after,
                sequence=(packet.sequence + 1) & 0xFFFF,
                timestamp=(packet.timestamp + self._timestamp_step) & 0xFFFFFFFF,
            )

    def write(self, packet: MediaPacket) -> Any:
        if self._closed:
            return None

        result = None
        if self.destination.wants_media(packet.media_type, packet.codec):
            result = self.destination.write(packet)
        elif packet.media_type == 'audio' and packet.codec == 'opus' and self.destination.wants_media('audio', 'pcm'):
            pcm_packet = packet.replace(codec='pcm', payload=self._decoder.decode(packet))
            result = self.destination.write(pcm_packet)
        self._remember_audio_packet(packet)
        return result

    def _make_silence_packet(self, track: _SilenceTrack) -> MediaPacket:
        packet = track.packet.replace(
            codec='pcm',
            payload=self._silence,
            marker=False,
            sequence=track.sequence,
            timestamp=track.timestamp,
            raw=b'',
            extension_payload=b'',
            rtp_extended=False,
            rtp_extensions=(),
            speaking_flags=SpeakingFlags.none(),
            audio_level=RTP_AUDIO_LEVEL_SILENCE,
            audio_voice_activity=False,
        )
        track.sequence = (track.sequence + 1) & 0xFFFF
        track.timestamp = (track.timestamp + self._timestamp_step) & 0xFFFFFFFF
        track.next_due += self.frame_duration
        track.emitted_for += self.frame_duration
        return packet

    def _collect_silence_packets(self) -> list[MediaPacket]:
        packets: list[MediaPacket] = []
        now = time.perf_counter()
        with self._lock:
            for key, track in list(self._tracks.items()):
                if self.max_silence is not None and track.emitted_for >= self.max_silence:
                    del self._tracks[key]
                    continue
                if track.next_due > now:
                    continue
                packets.append(self._make_silence_packet(track))
        return packets

    def _run(self) -> None:
        interval = min(self.frame_duration, 0.02)
        while not self._stop_event.wait(interval):
            if self._closed:
                return
            if not self.destination.wants_media('audio', 'pcm'):
                continue
            for packet in self._collect_silence_packets():
                if self._closed:
                    return
                try:
                    self.destination.write(packet)
                except Exception:
                    log.warning('SilenceFillSink destination raised while writing silence.', exc_info=True)

    def cleanup(self) -> None:
        if self._closed:
            return

        self._stop_event.set()
        _join_thread(self._thread)
        with self._lock:
            self._tracks.clear()
        self._decoder.clear()
        super().cleanup()


class _BinaryWriterSink(_FilteredMediaSink):
    def __init__(
        self,
        destination: str | os.PathLike[str] | BinaryIO,
        *,
        media_types: Sequence[str] | None = None,
        codecs: Sequence[str] | None = None,
    ) -> None:
        super().__init__(media_types=media_types, codecs=codecs)
        if isinstance(destination, (str, os.PathLike)):
            self._close_file = True
            self._file = open(destination, 'wb')
        else:
            self._close_file = False
            self._file = destination

    def _close_writer(self) -> None:
        file = getattr(self, '_file', MISSING)
        if file is not MISSING:
            self._file = MISSING
            if self._close_file:
                file.close()
            else:
                file.flush()

    def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._close_writer()


class QueueSink(_FilteredMediaSink):
    """Stores decoded receive packets in a :class:`queue.Queue`.

    This is useful when application code wants to consume multiplexed audio and
    video packets from its own worker instead of doing all work inside the
    receive callback.

    Parameters
    ----------
    destination: :class:`queue.Queue`
        Queue to write packets into. If omitted, a queue is created.
    media_types: Optional[List[:class:`str`]]
        Media types to accept.
    codecs: Optional[List[:class:`str`]]
        Codec names to accept.
    maxsize: :class:`int`
        Maximum size for a created queue.
    drop_oldest: :class:`bool`
        Whether to drop the oldest packet when the queue is full.

    Attributes
    ----------
    queue: :class:`queue.Queue`
        The queue receiving packets.
    drop_oldest: :class:`bool`
        Whether the oldest packet is dropped when the queue is full.
    dropped: :class:`int`
        Number of packets dropped by this sink.
    """

    def __init__(
        self,
        destination: queue.Queue[MediaPacket] = MISSING,
        *,
        media_types: Sequence[str] | None = None,
        codecs: Sequence[str] | None = None,
        maxsize: int = 0,
        drop_oldest: bool = False,
    ) -> None:
        super().__init__(media_types=media_types, codecs=codecs)
        if destination is not None and not isinstance(destination, queue.Queue):
            raise TypeError(f'destination must be queue.Queue not {destination.__class__.__name__}')

        self.queue: queue.Queue[MediaPacket] = destination if destination is not MISSING else queue.Queue(maxsize=maxsize)
        self.drop_oldest = drop_oldest
        self.dropped = 0

    def write(self, packet: MediaPacket) -> bool:
        """Queue one packet.

        Parameters
        ----------
        packet: :class:`MediaPacket`
            The packet to queue.

        Returns
        -------
        :class:`bool`
            Whether the packet was accepted by the queue.
        """

        if self._closed:
            return False

        try:
            self.queue.put_nowait(packet)
            return True
        except queue.Full:
            if not self.drop_oldest:
                self.dropped += 1
                return False

        try:
            self.queue.get_nowait()
            self.queue.task_done()
        except (queue.Empty, ValueError):
            pass

        try:
            self.queue.put_nowait(packet)
            self.dropped += 1
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def get(self, block: bool = True, timeout: float | None = None) -> MediaPacket:
        """Remove and return one packet from the queue.

        Parameters
        ----------
        block: :class:`bool`
            Whether to block until a packet is available.
        timeout: Optional[:class:`float`]
            Maximum seconds to block.

        Returns
        -------
        :class:`MediaPacket`
            The next queued packet.

        Raises
        ------
        queue.Empty
            The queue is empty and ``block`` is ``False`` or the timeout elapses.
        """
        return self.queue.get(block=block, timeout=timeout)

    def get_nowait(self) -> MediaPacket:
        """:class:`MediaPacket`: Remove and return one packet without blocking.

        Raises
        ------
        queue.Empty
            The queue is empty.
        """
        return self.queue.get_nowait()

    def qsize(self) -> int:
        """:class:`int`: The approximate queue size."""
        return self.queue.qsize()

    def empty(self) -> bool:
        """:class:`bool`: Whether the queue is empty."""
        return self.queue.empty()

    def full(self) -> bool:
        """:class:`bool`: Whether the queue is full."""
        return self.queue.full()

    def task_done(self) -> None:
        """Indicate that a queued packet has been processed.

        Raises
        ------
        ValueError
            Called more times than there were queued packets.
        """
        self.queue.task_done()

    def join(self) -> None:
        """Block until all queued packets are marked done."""
        self.queue.join()


class AsyncQueueSink(_FilteredMediaSink):
    """Stores decoded receive packets in an :class:`asyncio.Queue`.

    Async equivalent to :class:`QueueSink`.

    Parameters
    ----------
    destination: :class:`asyncio.Queue`
        Queue to write packets into. If omitted, a queue is created.
    loop: Optional[:class:`asyncio.AbstractEventLoop`]
        The event loop used to schedule queue writes from the receive thread.
    media_types: Optional[List[:class:`str`]]
        Media types to accept.
    codecs: Optional[List[:class:`str`]]
        Codec names to accept.
    maxsize: :class:`int`
        Maximum size for a created queue.
    drop_oldest: :class:`bool`
        Whether to drop the oldest packet when the queue is full.

    Attributes
    ----------
    queue: :class:`asyncio.Queue`
        The queue receiving packets.
    loop: Optional[:class:`asyncio.AbstractEventLoop`]
        The event loop used to schedule queue writes.
    drop_oldest: :class:`bool`
        Whether the oldest packet is dropped when the queue is full.
    dropped: :class:`int`
        Number of packets dropped by this sink.
    """

    def __init__(
        self,
        destination: asyncio.Queue[MediaPacket] = MISSING,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
        media_types: Sequence[str] | None = None,
        codecs: Sequence[str] | None = None,
        maxsize: int = 0,
        drop_oldest: bool = False,
    ) -> None:
        super().__init__(media_types=media_types, codecs=codecs)
        if destination is not None and not isinstance(destination, asyncio.Queue):
            raise TypeError(f'destination must be asyncio.Queue not {destination.__class__.__name__}')

        self.queue: asyncio.Queue[MediaPacket] = destination if destination is not None else asyncio.Queue(maxsize=maxsize)
        self.loop = loop
        self.drop_oldest = drop_oldest
        self.dropped = 0

    def _resolve_loop(self) -> asyncio.AbstractEventLoop:
        if self.loop is not None:
            return self.loop

        voice_client = self.voice_client
        loop = getattr(voice_client, 'loop', None)
        if isinstance(loop, asyncio.AbstractEventLoop):
            return loop

        client = self.client
        loop = getattr(client, 'loop', None)
        if isinstance(loop, asyncio.AbstractEventLoop):
            return loop

        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            raise discord.ClientException('AsyncQueueSink requires an explicit event loop before listening') from None

    def write(self, packet: MediaPacket) -> bool:
        if self._closed:
            return False

        loop = self._resolve_loop()
        try:
            loop.call_soon_threadsafe(self._put_packet, packet)
        except RuntimeError:
            self.dropped += 1
            return False
        return True

    def _put_packet(self, packet: MediaPacket) -> None:
        if self._closed:
            return

        try:
            self.queue.put_nowait(packet)
            return
        except asyncio.QueueFull:
            if not self.drop_oldest:
                self.dropped += 1
                return

        try:
            self.queue.get_nowait()
            self.queue.task_done()
        except asyncio.QueueEmpty:
            pass

        try:
            self.queue.put_nowait(packet)
            self.dropped += 1
        except asyncio.QueueFull:
            self.dropped += 1

    async def get(self) -> MediaPacket:
        """:class:`MediaPacket`: Remove and return one packet from the async queue."""
        return await self.queue.get()

    def get_nowait(self) -> MediaPacket:
        """:class:`MediaPacket`: Remove and return one packet without blocking.

        Raises
        ------
        asyncio.QueueEmpty
            The queue is empty.
        """
        return self.queue.get_nowait()

    def qsize(self) -> int:
        """:class:`int`: The approximate queue size."""
        return self.queue.qsize()

    def empty(self) -> bool:
        """:class:`bool`: Whether the queue is empty."""
        return self.queue.empty()

    def full(self) -> bool:
        """:class:`bool`: Whether the queue is full."""
        return self.queue.full()

    def task_done(self) -> None:
        """Indicate that a queued packet has been processed.

        Raises
        ------
        ValueError
            Called more times than there were queued packets.
        """
        self.queue.task_done()

    async def join(self) -> None:
        """Wait until all queued packets are marked done."""
        await self.queue.join()


class FFmpegSink(MediaSink):
    """Writes decoded audio packets into an FFmpeg subprocess.

    Parameters
    ----------
    destination: Union[:class:`str`, :class:`os.PathLike`, :class:`bytes`]
        Output path or :term:`py:bytes-like object`. File-like destinations receive FFmpeg stdout.
    executable: :class:`str`
        The FFmpeg executable to run.
    before_options: Optional[:class:`str`]
        Extra FFmpeg options placed before input options.
    options: Optional[:class:`str`]
        Extra FFmpeg output options.
    stderr: Optional[Union[IO[:class:`bytes`], :class:`int`]]
        Where FFmpeg stderr is redirected.

    Attributes
    ----------
    returncode: Optional[:class:`int`]
        The FFmpeg process return code after cleanup.
    """

    def __init__(
        self,
        destination: str | os.PathLike[str] | BinaryIO,
        *,
        executable: str = 'ffmpeg',
        before_options: str | None = None,
        options: str | None = None,
        stderr: FFmpegStderr = None,
    ) -> None:
        super().__init__()
        self._decoder = _OpusDecoderCache()
        if isinstance(destination, (str, os.PathLike)):
            self._buffer: BinaryIO | None = None
            output = os.fspath(destination)
        else:
            self._buffer = destination
            output = 'pipe:1'

        args = [executable, '-hide_banner']
        args.extend(_argv_options(before_options))
        args.extend(
            [
                '-f',
                's16le',
                '-ar',
                str(WaveSink.SAMPLING_RATE),
                '-ac',
                str(WaveSink.CHANNELS),
                '-i',
                'pipe:0',
                '-loglevel',
                'warning',
                '-blocksize',
                str(io.DEFAULT_BUFFER_SIZE),
            ]
        )
        args.extend(_argv_options(options))
        args.append(output)

        stderr = _normalize_ffmpeg_stderr(stderr)

        try:
            self._process: subprocess.Popen[bytes] = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE if self._buffer is not None else None,
                stderr=stderr,
                creationflags=CREATE_NO_WINDOW,
                text=False,
            )
        except FileNotFoundError:
            raise discord.ClientException(executable + ' was not found') from None
        except subprocess.SubprocessError as exc:
            raise discord.ClientException(f'Popen failed: {exc.__class__.__name__}: {exc}') from exc

        self.returncode: int | None = None
        self._stdin = self._process.stdin
        self._stdout_thread: threading.Thread | None = None
        if self._buffer is not None and self._process.stdout is not None:
            self._stdout_thread = threading.Thread(
                target=_copy_binary_stream,
                args=(self._process.stdout, self._buffer),
                daemon=True,
                name=f'native-voice-ffmpeg-sink:{self._process.pid}',
            )
            self._stdout_thread.start()

    def wants_media(self, media_type: str, codec: str) -> bool:
        return not self._closed and media_type == 'audio' and codec in {'opus', 'pcm'}

    def write(self, packet: MediaPacket) -> None:
        if self._closed:
            return
        stdin = self._stdin
        process = self._process
        if stdin is None or process is MISSING or stdin.closed or process.poll() is not None:
            return
        try:
            data = self._decoder.decode(packet)
            stdin.write(data)
            stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            self.cleanup()

    def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        process = self._process
        if process is MISSING:
            return

        self._process = MISSING
        stdin = self._stdin
        if stdin is not None:
            _close_quietly(stdin)

        _wait_or_kill_process(process)
        self.returncode = process.returncode

        _join_thread(self._stdout_thread)
        buffer = self._buffer
        if buffer is not None:
            buffer.flush()
        self._stdin = None
        self._stdout_thread = None
        self._decoder.clear()

        if process.returncode != 0:
            raise discord.ClientException(f'FFmpeg sink failed with exit code {process.returncode}')


class FFmpegMuxSink(MediaSink):
    """Records multiplexed receive packets into one FFmpeg output.

    Audio packets are decoded to timestamp-aligned PCM and video packets
    are written as their decoded frame payloads.

    Parameters
    ----------
    destination: Union[:class:`str`, :class:`os.PathLike`, :class:`bytes`]
        Output path or :term:`py:bytes-like object`. File-like destinations receive FFmpeg stdout.
    video_codec: Optional[:class:`str`]
        Restrict recording to a single Discord video codec.
    width: :class:`int`
        Video width used for codecs that require container dimensions.
    height: :class:`int`
        Video height used for codecs that require container dimensions.
    fps: :class:`int`
        Fallback video frame rate for muxing.
    audio: :class:`bool`
        Whether to record audio packets.
    video: :class:`bool`
        Whether to record video packets.
    executable: :class:`str`
        The FFmpeg executable to run.
    before_options: Optional[:class:`str`]
        Extra FFmpeg options placed before input options.
    options: Optional[:class:`str`]
        Extra FFmpeg output options.
    output_format: Optional[:class:`str`]
        Explicit FFmpeg output format.
    audio_codec: Optional[:class:`str`]
        Audio codec to encode with during muxing.
    shortest: :class:`bool`
        Whether to stop muxed output at the shortest audio/video input.
    stderr: Optional[Union[IO[:class:`bytes`], :class:`int`]]
        Where FFmpeg stderr is redirected.
    keep_temp: :class:`bool`
        Whether to keep temporary elementary stream files after cleanup.
    timeout: Optional[:class:`float`]
        Maximum seconds to wait for FFmpeg muxing during cleanup.

    Attributes
    ----------
    destination: Union[:class:`str`, :class:`os.PathLike`, BinaryIO]
        The configured output destination.
    video_codec: Optional[:class:`str`]
        The selected or detected Discord video codec.
    width: :class:`int`
        Video width used for muxing.
    height: :class:`int`
        Video height used for muxing.
    fps: :class:`int`
        Fallback video frame rate for muxing.
    audio_enabled: :class:`bool`
        Whether audio recording is enabled.
    video_enabled: :class:`bool`
        Whether video recording is enabled.
    returncode: Optional[:class:`int`]
        The FFmpeg process return code after cleanup.
    """

    _VIDEO_SUFFIXES = MappingProxyType(
        {
            'H264': '.h264',
            'H265': '.h265',
            'VP8': '.ivf',
            'VP9': '.ivf',
            'AV1': '.ivf',
        }
    )
    _AUDIO_TIMELINE_RESET_THRESHOLD = 0.151

    def __init__(
        self,
        destination: str | os.PathLike[str] | BinaryIO,
        *,
        video_codec: str | None = None,
        width: int = 0,
        height: int = 0,
        fps: int = 30,
        audio: bool = True,
        video: bool = True,
        executable: str = 'ffmpeg',
        before_options: str | None = None,
        options: str | None = None,
        output_format: str | None = None,
        audio_codec: str | None = None,
        shortest: bool = True,
        stderr: FFmpegStderr = None,
        keep_temp: bool = False,
        timeout: float | None = 120.0,
    ) -> None:
        super().__init__()
        self.destination = destination
        if isinstance(destination, (str, os.PathLike)):
            self._destination_path: str | None = os.fspath(destination)
            self._buffer: BinaryIO | None = None
        else:
            self._destination_path = None
            self._buffer = destination
        self.video_codec: str | None = None
        self.width = width
        self.height = height
        self.fps = max(1, fps)
        self.audio_enabled = audio
        self.video_enabled = video
        self.executable = executable
        self.before_options = before_options
        self.options = options
        self.output_format = output_format
        self.audio_codec = audio_codec
        self.shortest = shortest
        self.stderr = _normalize_ffmpeg_stderr(stderr)
        self.keep_temp = keep_temp
        self.timeout = timeout

        self._decoder = _OpusDecoderCache()
        self._audio_path: str | None = None
        self._audio_file: BinaryIO | None = None
        self._audio_base_timestamp: int | None = None
        self._audio_start_offset_samples: int | None = None
        self._audio_packets = 0
        self._video_path: str | None = None
        self._video_sink: EncodedVideoSink | None = None
        self._video_first_offset: float | None = None
        self._video_last_offset: float | None = None
        self._video_packets = 0
        self._recording_start_received_at: float | None = None
        self._recording_rtcp_time_offset: float | None = None
        self.returncode: int | None = None

        if not audio and not video:
            raise ValueError('audio or video must be enabled')
        if shutil.which(executable) is None and not os.path.exists(executable):
            raise discord.ClientException(executable + ' was not found')

        self.video_codec = _coerce_video_codec(video_codec) if video_codec is not None else None
        if video and self.video_codec in EncodedVideoSink._IVF_FOURCC and (width <= 0 or height <= 0):
            raise ValueError('width and height are required to record VP8, VP9, or AV1 with FFmpegMuxSink')

    @staticmethod
    def _temp_path(suffix: str) -> str:
        fd, path = tempfile.mkstemp(prefix='native-voice-', suffix=suffix)
        os.close(fd)
        return path

    def wants_media(self, media_type: str, codec: str) -> bool:
        if self._closed:
            return False
        if media_type == 'audio':
            return self.audio_enabled and codec in {'opus', 'pcm'}
        if media_type == 'video':
            normalized = codec.upper()
            return (
                self.video_enabled
                and normalized in _VIDEO_OUTPUT_FORMAT
                and (self.video_codec is None or normalized == self.video_codec)
            )
        return False

    def _ensure_video_sink(self, codec: str) -> EncodedVideoSink:
        normalized = codec.upper()
        if self.video_codec is None:
            self.video_codec = normalized
        elif normalized != self.video_codec:
            raise discord.ClientException(f'FFmpegMuxSink already recording {self.video_codec}, not {normalized}')

        if normalized in EncodedVideoSink._IVF_FOURCC and (self.width <= 0 or self.height <= 0):
            raise discord.ClientException('width and height are required to record VP8, VP9, or AV1 with FFmpegMuxSink')

        if self._video_sink is None:
            self._video_path = self._temp_path(self._VIDEO_SUFFIXES[normalized])
            self._video_sink = EncodedVideoSink(
                self._video_path,
                codec=normalized,
                width=self.width,
                height=self.height,
                fps=self.fps,
                rtp_timestamps=True,
            )
        return self._video_sink

    def _audio_timestamp_delta(self, timestamp: int) -> int | None:
        base = self._audio_base_timestamp
        if base is None:
            self._audio_base_timestamp = timestamp
            return 0

        return _rtp_timestamp_delta(timestamp, base)

    def _packet_time_offset(self, packet: MediaPacket, received_at: float) -> float:
        base = self._recording_start_received_at
        if base is None:
            self._recording_start_received_at = received_at
            return 0.0

        received_offset = max(0.0, received_at - base)
        rtcp_time = packet.rtcp_time
        if rtcp_time is None:
            return received_offset

        rtcp_offset = self._recording_rtcp_time_offset
        if rtcp_offset is None:
            rtcp_offset = received_offset - rtcp_time
            self._recording_rtcp_time_offset = rtcp_offset
        return max(0.0, rtcp_time + rtcp_offset)

    def _write_audio_packet(self, packet: MediaPacket, received_at: float) -> None:
        if self._audio_file is None:
            self._audio_path = self._temp_path('.pcm')
            self._audio_file = open(self._audio_path, 'w+b')

        pcm = packet.payload if packet.codec == 'pcm' else self._decoder.decode(packet)
        if not pcm:
            return

        timestamp_delta = self._audio_timestamp_delta(packet.timestamp)
        if timestamp_delta is None:
            return

        packet_offset: float | None = None
        packet_offset = self._packet_time_offset(packet, received_at)

        if self._audio_start_offset_samples is None:
            self._audio_start_offset_samples = round((packet_offset or 0.0) * WaveSink.SAMPLING_RATE)
        elif packet_offset is not None:
            current_offset = (self._audio_start_offset_samples + timestamp_delta) / WaveSink.SAMPLING_RATE
            if abs(packet_offset - current_offset) > self._AUDIO_TIMELINE_RESET_THRESHOLD:
                self._audio_start_offset_samples = round(packet_offset * WaveSink.SAMPLING_RATE) - timestamp_delta

        audio_file = self._audio_file
        sample_offset = self._audio_start_offset_samples or 0
        start = (sample_offset + timestamp_delta) * WaveSink.CHANNELS * WaveSink.SAMPLE_WIDTH
        audio_file.seek(0, os.SEEK_END)
        current_end = audio_file.tell()
        if start < current_end:
            audio_file.seek(start)
            existing = audio_file.read(len(pcm))
            if len(existing) < len(pcm):
                existing += b'\x00' * (len(pcm) - len(existing))
            pcm = pcm16_add(existing, pcm)

        audio_file.seek(start)
        audio_file.write(pcm)
        self._audio_packets += 1

    def _write_video_packet(self, packet: MediaPacket, received_at: float) -> None:
        video_sink = self._ensure_video_sink(packet.codec)
        if not video_sink.write(packet):
            return
        offset = self._packet_time_offset(packet, received_at)
        if self._video_first_offset is None:
            self._video_first_offset = offset

        self._video_last_offset = offset
        self._video_packets += 1

    def write(self, packet: MediaPacket) -> None:
        if self._closed:
            return

        received_at = packet.received_at if packet.received_at is not None else time.perf_counter()
        if packet.media_type == 'audio':
            self._write_audio_packet(packet, received_at)
            return

        if packet.media_type == 'video':
            self._write_video_packet(packet, received_at)

    def _video_effective_fps(self) -> float:
        if self._video_packets >= 2 and self._video_first_offset is not None and self._video_last_offset is not None:
            delta = self._video_last_offset - self._video_first_offset
            if delta > 0:
                return max(1.0, (self._video_packets - 1) / delta)

        return float(self.fps)

    def _video_effective_fps_arg(self) -> str:
        value = self._video_effective_fps()
        return f'{value:.6f}'.rstrip('0').rstrip('.')

    @staticmethod
    def _time_offset_arg(value: float) -> str:
        return f'{value:.6f}'.rstrip('0').rstrip('.') or '0'

    def _reserve_destination_temp_path(self) -> str | None:
        destination = self._destination_path
        if destination is None:
            return None

        root, ext = os.path.splitext(destination)
        for attempt in range(1000):
            candidate = f'{root}.{attempt}.tmp{ext}' if ext else f'{destination}.{attempt}.tmp'
            try:
                fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                continue
            except OSError as exc:
                raise discord.ClientException(f'Could not create FFmpeg mux temp file: {exc}') from exc
            else:
                os.close(fd)
                return candidate

        raise discord.ClientException('Could not reserve an FFmpeg mux temp file')

    def _close_inputs(self) -> None:
        audio_file = self._audio_file
        if audio_file is not None:
            self._audio_file = None
            audio_file.close()

        video_sink = self._video_sink
        if video_sink is not None:
            self._video_sink = None
            video_sink.cleanup()

    def _build_ffmpeg_args(self, *, output_path: str | None = None) -> list[str] | None:
        audio_input = self._audio_path if self._audio_packets > 0 else None
        video_input = (
            (self._video_path, self.video_codec)
            if self._video_packets > 0 and self._video_path is not None and self.video_codec is not None
            else None
        )
        if audio_input is None and video_input is None:
            return None

        args = [self.executable, '-hide_banner', '-y']
        args.extend(_argv_options(self.before_options))

        sync_start = (self._video_first_offset or 0.0) if audio_input is not None and video_input is not None else 0.0
        if audio_input is not None:
            if sync_start > 0:
                args.extend(['-ss', self._time_offset_arg(sync_start)])
            args.extend(
                [
                    '-f',
                    's16le',
                    '-ar',
                    str(WaveSink.SAMPLING_RATE),
                    '-ac',
                    str(WaveSink.CHANNELS),
                    '-i',
                    audio_input,
                ]
            )

        if video_input is not None:
            video_path, video_codec = video_input
            demuxer = _VIDEO_OUTPUT_FORMAT[video_codec]
            if demuxer != 'ivf':
                args.extend(['-r', self._video_effective_fps_arg()])
            args.extend(['-f', demuxer, '-i', video_path, '-c:v', 'copy'])

        if self.audio_codec is not None and audio_input is not None:
            args.extend(['-c:a', self.audio_codec])
        if self.shortest and audio_input is not None and video_input is not None:
            args.append('-shortest')
        if self.output_format is not None:
            args.extend(['-f', self.output_format])
        elif self._buffer is not None:
            args.extend(['-f', 'matroska'])
        args.extend(_argv_options(self.options))
        args.append(output_path or self._destination_path or 'pipe:1')
        return args

    def _run_ffmpeg(self, args: list[str]) -> None:
        try:
            process: subprocess.Popen[bytes] = subprocess.Popen(
                args,
                stdout=subprocess.PIPE if self._buffer is not None else None,
                stderr=subprocess.PIPE if self.stderr is None else self.stderr,
                creationflags=CREATE_NO_WINDOW,
                text=False,
            )
        except FileNotFoundError:
            raise discord.ClientException(self.executable + ' was not found') from None
        except subprocess.SubprocessError as exc:
            raise discord.ClientException(f'Popen failed: {exc.__class__.__name__}: {exc}') from exc

        try:
            stdout, stderr = process.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            stdout, stderr = process.communicate()
            self.returncode = process.returncode
            timeout = self.timeout
            suffix = f' after {timeout:g}s' if timeout is not None else ''
            raise discord.ClientException(f'FFmpeg mux timed out{suffix}') from exc

        self.returncode = process.returncode
        if process.returncode != 0:
            detail = ''
            if isinstance(stderr, bytes) and stderr:
                detail = stderr.decode('utf-8', 'replace').strip()
            suffix = f': {detail[-1000:]}' if detail else ''
            raise discord.ClientException(f'FFmpeg mux failed with exit code {process.returncode}{suffix}')
        buffer = self._buffer
        if buffer is not None and stdout:
            buffer.write(stdout)

    def _unlink_temp_files(self) -> None:
        if self.keep_temp:
            return
        for path in (self._audio_path, self._video_path):
            if path is None:
                continue
            try:
                os.unlink(path)
            except OSError:
                pass
        self._audio_path = None
        self._video_path = None
        self._video_first_offset = None
        self._video_last_offset = None

    def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._audio_path is None and self._video_path is None:
            self._decoder.clear()
            return

        try:
            self._close_inputs()
            output_path = self._reserve_destination_temp_path()
            args = self._build_ffmpeg_args(output_path=output_path)
            if args is not None:
                try:
                    self._run_ffmpeg(args)
                    if output_path is not None and self._destination_path is not None:
                        os.replace(output_path, self._destination_path)
                        output_path = None
                finally:
                    if output_path is not None:
                        try:
                            os.unlink(output_path)
                        except OSError:
                            pass
        finally:
            self._decoder.clear()
            self._unlink_temp_files()


class EncodedVideoSink(_BinaryWriterSink):
    """Writes received encoded video frames to IVF or Annex B output.

    Parameters
    ----------
    destination: Union[:class:`str`, :class:`os.PathLike`, :class:`bytes`]
        Output path or :term:`py:bytes-like object`.
    codec: :class:`str`
        The Discord video codec to write.
    width: :class:`int`
        Video width for IVF headers.
    height: :class:`int`
        Video height for IVF headers.
    fps: :class:`int`
        Video frame rate for IVF headers when RTP timestamps are not used.
    rtp_timestamps: :class:`bool`
        Whether IVF frame timestamps should be derived from RTP timestamps.

    Attributes
    ----------
    codec: :class:`str`
        The normalized Discord video codec name.
    width: :class:`int`
        Video width for output metadata.
    height: :class:`int`
        Video height for output metadata.
    fps: :class:`int`
        Video frame rate for output metadata.
    rtp_timestamps: :class:`bool`
        Whether output timestamps are derived from RTP timestamps.
    """

    _IVF_FOURCC = MappingProxyType(
        {
            'VP8': b'VP80',
            'VP9': b'VP90',
            'AV1': b'AV01',
        }
    )
    _RTP_CLOCK_RATE = 90_000

    def __init__(
        self,
        destination: str | os.PathLike[str] | BinaryIO,
        *,
        codec: str,
        width: int = 0,
        height: int = 0,
        fps: int = 30,
        rtp_timestamps: bool = False,
    ) -> None:
        normalized = _coerce_video_codec(codec)
        if normalized in self._IVF_FOURCC and (width <= 0 or height <= 0):
            raise ValueError('width and height are required for IVF video sinks')

        super().__init__(destination, media_types=('video',), codecs=(normalized,))
        self.codec = normalized
        self.width = width
        self.height = height
        self.fps = max(1, fps)
        self.rtp_timestamps = rtp_timestamps
        self._base_timestamp: int | None = None
        self._frames = 0
        self._ivf = self.codec in self._IVF_FOURCC
        self._h26x_started = self.codec not in _H26X_PARAMETER_SET_TYPES
        if self._ivf:
            timebase_denominator = self._RTP_CLOCK_RATE if self.rtp_timestamps else self.fps
            self._file.write(
                struct.pack(
                    '<4sHH4sHHIIII',
                    b'DKIF',
                    0,
                    32,
                    self._IVF_FOURCC[self.codec],
                    self.width,
                    self.height,
                    timebase_denominator,
                    1,
                    0,
                    0,
                )
            )

    def _ivf_timestamp(self, packet: MediaPacket) -> int:
        if not self.rtp_timestamps:
            return self._frames

        base = self._base_timestamp
        if base is None:
            self._base_timestamp = packet.timestamp
            return 0

        delta = _rtp_timestamp_delta(packet.timestamp, base)
        if delta is None:
            return self._frames
        return delta

    def _should_write_h26x(self, packet: MediaPacket) -> bool:
        if self._h26x_started:
            return True

        nal_types = _annex_b_nal_types(packet.payload, self.codec)
        has_config = _H26X_PARAMETER_SET_TYPES[self.codec].issubset(nal_types)
        has_keyframe = bool(_H26X_KEYFRAME_TYPES[self.codec].intersection(nal_types))
        if has_config and has_keyframe:
            self._h26x_started = True
            return True

        log.debug(
            'Dropping %s frame before keyframe with codec parameter sets was received for SSRC %s.',
            packet.codec,
            packet.ssrc,
        )
        return False

    def write(self, packet: MediaPacket) -> bool:
        if self._closed:
            return False
        payload = packet.payload
        if self.codec in _H26X_PARAMETER_SET_TYPES and not self._should_write_h26x(packet):
            return False
        if self._ivf:
            self._file.write(struct.pack('<IQ', len(payload), self._ivf_timestamp(packet)))
        self._file.write(payload)
        self._frames += 1
        return True

    def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        file = getattr(self, '_file', MISSING)
        if file is MISSING:
            return
        if self._ivf:
            try:
                position = file.tell()
                file.seek(24)
                file.write(struct.pack('<I', self._frames))
                file.seek(position)
            except (OSError, ValueError):
                pass
        self._close_writer()


class WaveSink(MediaSink):
    """Writes decoded audio packets to a WAV file.

    Parameters
    ----------
    destination: Union[:class:`str`, :class:`os.PathLike`, :class:`bytes`]
        Output path or :term:`py:bytes-like object`.
    """

    CHANNELS = OpusDecoder.CHANNELS
    SAMPLE_WIDTH = OpusDecoder.SAMPLE_SIZE // OpusDecoder.CHANNELS
    SAMPLING_RATE = OpusDecoder.SAMPLING_RATE

    def __init__(self, destination: str | os.PathLike[str] | BinaryIO) -> None:
        super().__init__()
        wave_destination = os.fspath(destination) if isinstance(destination, (str, os.PathLike)) else destination
        self._file = wave.open(wave_destination, 'wb')
        self._file.setnchannels(self.CHANNELS)
        self._file.setsampwidth(self.SAMPLE_WIDTH)
        self._file.setframerate(self.SAMPLING_RATE)
        self._decoder = _OpusDecoderCache()

    def wants_media(self, media_type: str, codec: str) -> bool:
        return not self._closed and media_type == 'audio' and codec in {'opus', 'pcm'}

    def write(self, packet: MediaPacket) -> None:
        if self._closed:
            return
        if packet.codec == 'pcm':
            self._file.writeframes(packet.payload)
            return
        self._file.writeframes(self._decoder.decode(packet))

    def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        file = getattr(self, '_file', MISSING)
        if file is not MISSING:
            self._file = MISSING
            file.close()
            self._decoder.clear()


class MixedWaveSink(MediaSink):
    """Records decoded audio packets into one timeline-aligned WAV file.

    Unlike :class:`WaveSink`, this sink uses each packet's RTP timestamp to
    place audio on the output timeline.

    Parameters
    ----------
    destination: Union[:class:`str`, :class:`os.PathLike`, :class:`bytes`]
        Output path or :term:`py:bytes-like object`.
    users: Optional[List[:class:`int`]]
        User IDs to include. When omitted, all users are mixed.
    """

    CHANNELS = WaveSink.CHANNELS
    SAMPLE_WIDTH = WaveSink.SAMPLE_WIDTH
    SAMPLING_RATE = WaveSink.SAMPLING_RATE

    def __init__(self, destination: str | os.PathLike[str] | BinaryIO, *, users: Sequence[int] | None = None) -> None:
        super().__init__()
        self._destination = destination
        self._users = None if users is None else set(users)
        self._base_timestamp: int | None = None
        self._mix = bytearray()
        self._decoder = _OpusDecoderCache()

    def wants_media(self, media_type: str, codec: str) -> bool:
        return not self._closed and media_type == 'audio' and codec in {'opus', 'pcm'}

    def _timestamp_delta(self, timestamp: int) -> int | None:
        base = self._base_timestamp
        if base is None:
            self._base_timestamp = timestamp
            return 0

        return _rtp_timestamp_delta(timestamp, base)

    def write(self, packet: MediaPacket) -> None:
        if self._closed:
            return
        if self._users is not None and packet.user_id not in self._users:
            return

        timestamp_delta = self._timestamp_delta(packet.timestamp)
        if timestamp_delta is None:
            return

        pcm = packet.payload if packet.codec == 'pcm' else self._decoder.decode(packet)
        if not pcm:
            return

        start = timestamp_delta * self.CHANNELS * self.SAMPLE_WIDTH
        end = start + len(pcm)
        if end > len(self._mix):
            self._mix.extend(b'\x00' * (end - len(self._mix)))

        mixed = pcm16_add(bytes(self._mix[start:end]), pcm)
        self._mix[start:end] = mixed

    def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        destination = getattr(self, '_destination', MISSING)
        if destination is MISSING:
            return

        self._destination = MISSING
        try:
            wave_destination = os.fspath(destination) if isinstance(destination, (str, os.PathLike)) else destination
            with wave.open(wave_destination, 'wb') as file:
                file.setnchannels(self.CHANNELS)
                file.setsampwidth(self.SAMPLE_WIDTH)
                file.setframerate(self.SAMPLING_RATE)
                file.writeframes(self._mix)
        finally:
            self._mix.clear()
            self._decoder.clear()
