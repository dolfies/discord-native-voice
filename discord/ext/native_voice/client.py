from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import logging
import struct
import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING, TypeVar, TypedDict, overload

import discord
from discord import opus
from discord.flags import SpeakingFlags
from discord.gateway import DiscordVoiceWebSocket
from discord.player import AudioPlayer, AudioSource
from discord.utils import MISSING
from discord.voice_media import (
    RTP_AUDIO_LEVEL_SILENCE,
    _audio_level_from_pcm,
    _audio_rtp_extension_payload,
    _rtp_header_with_one_byte_extensions,
    VoiceCodec,
    VoiceStream,
    VoiceStreamResolution,
)
from discord.voice_state import ConnectionFlowState, VoiceConnectionState

from ._native_voice import (
    AV1Depacketizer,
    H264Depacketizer,
    H265Depacketizer,
    NativeUDPTransport,
    TransportCrypto,
    VP8Depacketizer,
    VP9Depacketizer,
    VideoSendPipeline,
    strip_h264_filler_nalus,
)
from .codecs import get_local_video_codec_capabilities
from .dave import (
    DAVE_PENDING_VIDEO_MAX_AGE,
    DAVE_PENDING_VIDEO_MAX_FRAMES,
    _PendingDaveVideoFrame,
    _dave_media_type,
    _is_retryable_dave_decrypt_error,
    _looks_like_dave_protocol_frame,
    davey,
)
from .media import (
    AudioMediaSource,
    MediaPacket,
    MediaSink,
    MediaSinkWants,
    MediaSource,
    VideoFrame,
    _H26X_KEYFRAME_TYPES,
    _H26X_PARAMETER_SET_TYPES,
    _native_video_bitrate,
)
from .player import MediaPlayer, MediaPlayerStats
from .rtp import (
    RTCP_GENERIC_NACK,
    RTCP_NACK_MAX_SEQUENCES,
    RTCP_PAYLOAD_FEEDBACK,
    RTCP_PICTURE_LOSS_INDICATION,
    RTCP_PROTECTED_HEADER_LEN,
    RTCP_RECEIVER_REPORT,
    RTCP_RTP_FEEDBACK,
    RTCP_SENDER_REPORT,
    AudioSendStats,
    RTCPReceiverReport,
    RTP_EXT_ABSOLUTE_SEND_TIME,
    RTP_EXT_AUDIO_LEVEL,
    RTP_EXT_DISCORD_SPEAKING,
    RTPExtension,
    RTPPacket,
    RTPSendStats,
    VIDEO_NACK_MAX_OUTSTANDING,
    VIDEO_NACK_MAX_RETRIES,
    VIDEO_NACK_RETRY_INTERVAL,
    _PendingVideoNack,
    _DecodedRTPPacket,
    _ReceivedVideoPacket,
    _VideoReceiveReorderBuffer,
    _rtp_timestamp_delta,
    _sequence_delta,
    _unwrap_sequence,
)

if TYPE_CHECKING:
    from discord.stream import Stream, StreamProtocol
    from discord.voice_client import VoiceProtocol

    from .stream_client import StreamClient

__all__ = ('VoiceClient',)

log = logging.getLogger(__name__)

CT = TypeVar('CT')
VC = TypeVar('VC', bound='VoiceClient')
ST = TypeVar('ST', bound='StreamProtocol')


@dataclass(slots=True)
class _RTCPReceiveReportState:
    base_extended_sequence: int
    max_extended_sequence: int
    packets_received: int = 0
    expected_prior: int = 0
    received_prior: int = 0
    jitter: float = 0.0
    transit: int | None = None
    last_report_at: float = 0.0


_VIDEO_DEPACKETIZER_FACTORIES: dict[str, Callable[[], Any]] = {
    'AV1': AV1Depacketizer,
    'H264': H264Depacketizer,
    'H265': H265Depacketizer,
    'VP8': VP8Depacketizer,
    'VP9': VP9Depacketizer,
}


def _configured_client_subclass(
    cls: type[CT],
    *,
    attrs: dict[str, Any],
    suffixes: Sequence[str],
) -> type[CT]:
    if all(getattr(cls, name) == value for name, value in attrs.items()):
        return cls

    class_attrs = {
        '__module__': cls.__module__,
        '__doc__': cls.__doc__,
        **attrs,
    }
    name = f'{cls.__name__}{"".join(dict.fromkeys(suffixes))}'
    return type(name, (cls,), class_attrs)  # type: ignore


class _VideoStartParams(TypedDict):
    width: int
    height: int
    fps: int
    bitrate: int


class _CodecLookups(TypedDict):
    payload_types: dict[int, tuple[str, str]]
    rtx_payload_types: dict[int, tuple[str, str, int]]
    video_payload_types: dict[str, int]
    video_rtx_payload_types: dict[str, int | None]


class _VoiceWebSocket(DiscordVoiceWebSocket):
    _connection: _ConnectionState

    async def _connect_udp_socket(self) -> None:
        state = self._connection
        log.debug('Connecting native UDP transport...')
        await asyncio.to_thread(state.connect_udp, state.endpoint_ip, state.voice_port)  # type: ignore

    async def discover_ip(self) -> tuple[str, int]:
        state = self._connection
        ssrc = state.ssrc

        log.debug('Sending native UDP IP discovery packet...')
        ip, port = await asyncio.to_thread(state.discover_ip, ssrc, state.timeout)
        log.debug('Detected IP: %s, port: %s.', ip, port)
        return ip, port


class _ConnectionState(VoiceConnectionState):
    voice_client: VoiceClient
    socket: NativeUDPTransport

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._udp_listener_ids: dict[Callable[..., Any], int] = {}

    def _create_socket_reader(self):
        return MISSING

    def _pause_socket_reader(self) -> None:
        return None

    def _stop_socket_reader(self) -> None:
        return None

    def _close_socket(self) -> None:
        if self.socket is MISSING:
            return

        try:
            self.socket.close()
        except OSError:
            pass

    def _recreate_socket(self, state: ConnectionFlowState) -> None:
        self._close_socket()
        self.endpoint_ip = MISSING
        self._create_socket()
        self.state = state

    def _create_socket(self) -> None:
        self.socket = NativeUDPTransport(4 * 1024 * 1024, (40 << 2) * self.voice_client.udp_qos_enabled)

    def connect_udp(self, address: str, port: int) -> None:
        self.socket.connect(address, port)

    def discover_ip(self, ssrc: int, timeout: float) -> tuple[str, int]:
        return self.socket.discover_ip(ssrc, timeout)

    def send_packets(self, packets: Sequence[bytes]) -> tuple[int, int]:
        sent, octets = self.socket.send_packets(packets)
        if sent != len(packets):
            raise OSError(f'RTP sender failed to send {len(packets) - sent} of {len(packets)} packets')
        return sent, octets

    def send_packet(self, packet: bytes) -> None:
        self.socket.send_packet(packet)

    def add_rtp_listener(self, callback: Callable[..., Any], *, batch: bool = False) -> None:
        log.debug('Registering native UDP RTP listener callback %s.', callback)
        if batch:
            self._udp_listener_ids[callback] = self.socket.add_batch_listener(callback, True, False)
        else:
            self._udp_listener_ids[callback] = self.socket.add_listener(callback, True, False)

    def add_rtcp_listener(self, callback: Callable[[bytes], Any]) -> None:
        log.debug('Registering native UDP RTCP listener callback %s.', callback)
        self._udp_listener_ids[callback] = self.socket.add_listener(callback, False, True)

    def add_socket_listener(self, callback: Callable[[bytes], Any]) -> None:
        log.debug('Registering native UDP listener callback %s.', callback)
        self._udp_listener_ids[callback] = self.socket.add_listener(callback, True, True)

    def remove_socket_listener(self, callback: Callable[[bytes], Any]) -> None:
        log.debug('Unregistering native UDP listener callback %s.', callback)
        listener_id = self._udp_listener_ids.pop(callback, None)
        if listener_id is not None:
            self.socket.remove_listener(listener_id)

    async def _connect_websocket(self, resume: bool) -> DiscordVoiceWebSocket:
        seq_ack = -1
        if self.ws is not MISSING:
            seq_ack = self.ws.seq_ack
        ws = await _VoiceWebSocket.from_connection_state(self, resume=resume, hook=self.hook, seq_ack=seq_ack)
        self.state = ConnectionFlowState.websocket_connected
        return ws

    async def soft_disconnect(self, *, with_state: ConnectionFlowState = ConnectionFlowState.got_both_voice_updates) -> None:
        self.voice_client._stop_udp_ping()
        await super().soft_disconnect(with_state=with_state)


class VoiceClient(discord.VoiceClient):
    """A native voice client with audio, video, and media receive support.

    This client extends :class:`discord.VoiceClient` with native RTP transport
    crypto, video send/receive, RTX/NACK handling, and media sinks.
    """

    _connection: _ConnectionState

    supported_modes = (
        'aead_aes256_gcm_rtpsize',  # Preferred
        'aead_xchacha20_poly1305_rtpsize',
    )
    media_stream_type = 'video'
    video_packet_burst_size: int = 12
    video_packet_burst_interval: float = 0.001
    video_keyframe_replay_max_frames: int = 30
    video_receive_reorder_max_packets: int = 8192
    video_receive_reorder_max_delay: float = 0.5
    rtcp_receiver_report_interval: float = 1.0
    rtcp_receiver_report_ttl: float = 3.0
    udp_qos_enabled: bool = False
    udp_ping_initial_delay: float = 0.1
    udp_ping_interval: float = 5.0
    udp_ping_timeout: float = 5.0
    packet_decode_error_log_interval: float = 0.501
    enable_debug_stats: bool = False

    video_simulcast_streams: tuple[VoiceStream, ...] = (VoiceStream.video(quality=100),)
    voice_codec_specs: tuple[VoiceCodec, ...] = (
        VoiceCodec.opus(payload_type=120),
        VoiceCodec.video('AV1', priority=1000, payload_type=101, rtx_payload_type=102, encode=False, decode=False),
        VoiceCodec.video('H265', priority=2000, payload_type=103, rtx_payload_type=104, encode=False, decode=False),
        VoiceCodec.video('H264', priority=3000, payload_type=105, rtx_payload_type=106, encode=False, decode=False),
        VoiceCodec.video('VP8', priority=4000, payload_type=107, rtx_payload_type=108, encode=False, decode=False),
        VoiceCodec.video('VP9', priority=5000, payload_type=109, rtx_payload_type=110, encode=False, decode=False),
    )

    auto_video_codec_capabilities: bool = True
    auto_video_codec_priority: bool = True
    ffmpeg_executable: str = 'ffmpeg'
    rtx_enabled: bool = True

    def supports_video(self) -> bool:
        return True

    def get_experiments(self, ready_experiments: Sequence[str]) -> Sequence[str]:
        # TODO: Do we need any H265 related exps here?
        return ('fixed_keyframe_interval',)

    def _speaking_flags_for_source(self, _source: MediaSource) -> SpeakingFlags:
        return SpeakingFlags(voice=True)

    def _media_player_source_changed(self, source: MediaSource) -> None:
        self._video_source_supports_simulcast = source.supports_simulcast()
        if self._video_start_params is None or self._video_codec is None or not self.is_connected():
            return
        asyncio.run_coroutine_threadsafe(
            self._apply_selected_video_stream(codec=self._video_codec, params=self._video_start_params),
            self.loop,
        )

    @staticmethod
    def _ensure_listenable_sink(sink: MediaSink) -> None:
        if sink.parent is not None:
            raise discord.ClientException('Cannot listen with a sink that is already registered as a child')
        closed = next((child for child in sink.walk_children(with_self=True) if child.closed), None)
        if closed is not None:
            raise discord.ClientException(f'Cannot listen with a closed sink: {closed.__class__.__name__}')

    @classmethod
    def get_voice_codecs(
        cls,
        *,
        refresh_capabilities: bool = False,
        rtx: bool | None = None,
        executable: str | None = None,
    ) -> list[VoiceCodec]:
        rtx = cls.rtx_enabled if rtx is None else rtx
        needs_capabilities = cls.auto_video_codec_capabilities or cls.auto_video_codec_priority
        executable = cls.ffmpeg_executable if executable is None else executable
        capabilities = (
            get_local_video_codec_capabilities(executable=executable, refresh=refresh_capabilities)
            if needs_capabilities
            else {}
        )
        audio: list[VoiceCodec] = []
        video: list[VoiceCodec] = []

        for spec in cls.voice_codec_specs:
            codec = spec.replace()
            if codec.type == 'video':
                codec_capabilities = capabilities.get(codec.name.upper(), {'encode': False, 'decode': False})
                if cls.auto_video_codec_capabilities:
                    codec = codec.replace(encode=codec_capabilities['encode'], decode=codec_capabilities['decode'])
                if not rtx and codec.rtx_payload_type is not None:
                    codec = codec.replace(rtx_payload_type=None)

            if not codec.encode and not codec.decode:
                continue
            if codec.type == 'video':
                video.append(codec)
            else:
                audio.append(codec)

        if cls.auto_video_codec_priority:
            video.sort(key=lambda codec: capabilities.get(codec.name.upper(), {}).get('score', 0), reverse=True)

        return [
            codec.replace(priority=index * 1000) for group in (audio, video) for index, codec in enumerate(group, start=1)
        ]

    @property
    def codecs(self) -> tuple[VoiceCodec, ...]:
        """Tuple[:class:`discord.VoiceCodec`, ...]: Codecs advertised by this client."""
        return tuple(self.get_voice_codecs(rtx=self.rtx_enabled))

    @classmethod
    def with_config(
        cls: type[VC],
        *,
        rtx: bool = MISSING,
        udp_qos: bool = MISSING,
        codecs: Sequence[VoiceCodec] = MISSING,
        video_streams: Sequence[VoiceStream] = MISSING,
        ffmpeg_executable: str = MISSING,
        enable_debug_stats: bool = MISSING,
    ) -> type[VC]:
        """Return a subclass with voice negotiation options preset.

        These options affect voice protocol identification,
        so they must be present on the class passed to ``channel.connect``.

        Parameters
        ----------
        rtx: :class:`bool`
            Whether to enable RTX support. This will cause the client to advertise RTX payload types for video codecs
            and to use RTX for video retransmissions if the voice server negotiates it.
            Enabling RTX may increase bandwidth usage, but can improve video quality on lossy connections.
            It is enabled by default.
        udp_qos: :class:`bool`
            Whether to request UDP QoS marking for the voice socket. This marks outgoing media packets with Discord's
            native DSCP value on platforms that allow it, which may improve prioritisation on supported networks.
            It is disabled by default.
        codecs: List[:class:`discord.VoiceCodec`]
            Codec objects to advertise. When omitted, codecs are generated from
            local FFmpeg capabilities and sorted by the local hardware/software
            capability score. When provided, the codec order is preserved and
            priorities are recomputed after unsupported entries are skipped.
        video_streams: List[:class:`discord.VoiceStream`]
            The simulcast streams advertised to the server.
            Defaults to a single max quality stream.
        ffmpeg_executable: :class:`str`
            FFmpeg executable used for automatic local codec capability probing.
        enable_debug_stats: :class:`bool`
            Whether to collect debug counters for RTP/RTCP receive diagnostics.
            This is disabled by default to avoid performance issues.

        Returns
        -------
        Type[:class:`VoiceClient`]
            A configured subclass of this voice client.

        Raises
        ------
        ValueError
            ``codecs`` does not include an Opus audio codec.
        """
        if (
            rtx is MISSING
            and udp_qos is MISSING
            and codecs is MISSING
            and video_streams is MISSING
            and ffmpeg_executable is MISSING
            and enable_debug_stats is MISSING
        ):
            return cls

        enabled = cls.rtx_enabled if rtx is MISSING else rtx
        qos_enabled = cls.udp_qos_enabled if udp_qos is MISSING else udp_qos
        executable = cls.ffmpeg_executable if ffmpeg_executable is MISSING else str(ffmpeg_executable)
        debug_stats_enabled = cls.enable_debug_stats if enable_debug_stats is MISSING else enable_debug_stats
        manual_codecs = codecs is not MISSING
        codec_specs = cls.voice_codec_specs if codecs is MISSING else cls._normalize_voice_codecs(codecs)
        streams = cls.video_simulcast_streams if video_streams is MISSING else cls._normalize_video_streams(video_streams)

        attrs: dict[str, Any] = {
            'rtx_enabled': enabled,
            'udp_qos_enabled': qos_enabled,
            'voice_codec_specs': codec_specs,
            'auto_video_codec_capabilities': cls.auto_video_codec_capabilities if not manual_codecs else False,
            'auto_video_codec_priority': cls.auto_video_codec_priority if not manual_codecs else False,
            'video_simulcast_streams': streams,
            'ffmpeg_executable': executable,
            'enable_debug_stats': debug_stats_enabled,
        }
        suffixes: list[str] = []
        if enabled != cls.rtx_enabled:
            suffixes.append('WithRTX' if enabled else 'WithoutRTX')
        if qos_enabled != cls.udp_qos_enabled:
            suffixes.append('WithQoS' if qos_enabled else 'WithoutQoS')
        if manual_codecs or codec_specs != cls.voice_codec_specs:
            suffixes.append('WithCodecs')
        if streams != cls.video_simulcast_streams:
            suffixes.append('WithVideoStreams')
        if executable != cls.ffmpeg_executable:
            suffixes.append('WithFFmpeg')
        if debug_stats_enabled != cls.enable_debug_stats:
            suffixes.append('WithDebugStats' if debug_stats_enabled else 'WithoutDebugStats')
        return _configured_client_subclass(cls, attrs=attrs, suffixes=suffixes)

    @staticmethod
    def _normalize_voice_codecs(codecs: Sequence[VoiceCodec]) -> tuple[VoiceCodec, ...]:
        normalized: list[VoiceCodec] = []
        for codec in codecs:
            if not isinstance(codec, VoiceCodec):
                raise TypeError(f'codecs must contain VoiceCodec, not {codec.__class__.__name__}')
            normalized.append(codec.replace())
        if not any(codec.type == 'audio' and codec.name.lower() == 'opus' for codec in normalized):
            raise ValueError('codecs must include an Opus audio codec')
        return tuple(normalized)

    @classmethod
    def _normalize_video_streams(cls, streams: Sequence[VoiceStream]) -> tuple[VoiceStream, ...]:
        normalized: list[VoiceStream] = []
        for stream in streams:
            if not isinstance(stream, VoiceStream):
                raise TypeError(f'video_streams must contain VoiceStream, not {stream.__class__.__name__}')
            normalized.append(stream.replace(type=cls.media_stream_type))
        return tuple(normalized)

    @property
    def video_streams(self) -> tuple[VoiceStream, ...]:
        """Tuple[:class:`discord.VoiceStream`, ...]: Simulcast streams advertised by this client."""
        return tuple(stream.replace() for stream in self.video_simulcast_streams)

    @property
    def active_video_streams(self) -> tuple[VoiceStream, ...]:
        return self._active_video_streams

    def _codec_cache_key(self) -> tuple[Any, ...]:
        return (
            self,
            self.voice_codec_specs,
            self.auto_video_codec_capabilities,
            self.auto_video_codec_priority,
            self.ffmpeg_executable,
            bool(self.rtx_enabled),
        )

    def _get_codec_lookups(self) -> _CodecLookups:
        key = self._codec_cache_key()
        if key == self._codec_lookup_key and self._codec_lookups is not None:
            return self._codec_lookups

        payload_types: dict[int, tuple[str, str]] = {}
        rtx_payload_types: dict[int, tuple[str, str, int]] = {}
        video_payload_types: dict[str, int] = {}
        video_rtx_payload_types: dict[str, int | None] = {}
        for codec in self.codecs:
            payload_types[codec.payload_type] = (codec.type, codec.name)
            if codec.type != 'video':
                continue

            normalized = codec.name.upper()
            video_payload_types[normalized] = codec.payload_type
            video_rtx_payload_types[normalized] = codec.rtx_payload_type
            if codec.rtx_payload_type is not None:
                rtx_payload_types[codec.rtx_payload_type] = (codec.type, codec.name, codec.payload_type)

        lookups: _CodecLookups = {
            'payload_types': payload_types,
            'rtx_payload_types': rtx_payload_types,
            'video_payload_types': video_payload_types,
            'video_rtx_payload_types': video_rtx_payload_types,
        }
        self._codec_lookup_key = key
        self._codec_lookups = lookups
        return lookups

    def __init__(self, client: discord.Client, channel: discord.abc.VocalChannel) -> None:
        discord.VoiceProtocol.__init__(self, client, channel)
        state = client._connection
        self.server_id: int = MISSING
        self.socket = MISSING
        self.loop: asyncio.AbstractEventLoop = state.loop
        self._state = state

        self.sequence: int = 0
        self.timestamp: int = 0
        self._player: AudioPlayer | None = None
        self.encoder = MISSING
        self._speaking_flags: SpeakingFlags = SpeakingFlags.none()
        self._audio_packet_count: int = 0
        self._audio_octet_count: int = 0
        self._audio_last_rtcp_media_time_ms: int | None = None

        self._codec_lookup_key: tuple[Any, ...] | None = None
        self._codec_lookups: _CodecLookups | None = None
        self._crypto: Any | None = None
        self._crypto_state: tuple[str, bytes] | None = None
        self._receive_crypto: Any | None = None
        self._receive_crypto_state: tuple[str, bytes] | None = None
        self._rtcp_sender_reports: dict[int, tuple[float, int, int]] = {}
        self._rtcp_sender_report_lsr: dict[int, tuple[int, float]] = {}
        self._rtcp_receiver_reports: dict[tuple[int, int], RTCPReceiverReport] = {}
        self._rtcp_receive_report_states: dict[int, _RTCPReceiveReportState] = {}
        self._rtp_send_stats: dict[int, RTPSendStats] = {}
        self._socket_listener: Callable[..., Any] | None = None
        self._rtcp_listener: Callable[[bytes], Any] | None = None
        self._udp_ping_rtt: float | None = None
        self._udp_ping_timeouts: int = 0
        self._last_packet_decode_error_log: float = 0.0
        self._video_nack_retry_stop: threading.Event | None = None
        self._video_nack_retry_thread: threading.Thread | None = None
        self._media_callback: Callable[[MediaPacket], Any] | None = None
        self._media_sink: MediaSink | None = None
        self._media_after: Callable[[Exception | None], Any] | None = None
        self._video_depacketizers: dict[tuple[int, int], Any] = {}
        self._video_receive_last_sequences: dict[int, int] = {}
        self._video_receive_pending_nacks: dict[int, dict[int, _PendingVideoNack]] = {}
        self._video_receive_stats: Counter[str] = Counter()
        if not self.enable_debug_stats:
            self._record_video_receive_stat = self._ignore_video_receive_stat
            self._record_video_receive_max_stat = self._ignore_video_receive_max_stat
            self._record_video_reorder_stats = self._ignore_video_reorder_stats
        self._video_receive_reorder_buffers: dict[tuple[int, int], _VideoReceiveReorderBuffer] = {}
        self._video_receive_startup_plis: set[int] = set()
        self._video_frame_rtp_packets: dict[tuple[int, int], list[RTPPacket]] = {}
        self._pending_dave_video_frames: deque[_PendingDaveVideoFrame] = deque()
        self._video_send_pipeline = VideoSendPipeline()
        self._video_codec: str | None = None
        self.rtx_enabled: bool = self.rtx_enabled
        self._video_ssrc: int = 0
        self._video_rtx_payload_type: int | None = None
        self._video_rtx_ssrcs: dict[int, int] = {}
        self._video_rids: dict[int, str] = {}
        self._video_start_params: _VideoStartParams | None = None
        self._video_state_streams: tuple[VoiceStream, ...] = ()
        self._last_video_state: tuple[int, int, tuple[VoiceStream, ...]] | None = None
        self._active_video_streams: tuple[VoiceStream, ...] = ()
        self._active_video_streams_by_rid: dict[str, VoiceStream] = {}
        self._video_source_supports_simulcast: bool = False
        self._video_packet_counts: dict[int, int] = {}
        self._video_octet_counts: dict[int, int] = {}
        self._video_media_times_ms: dict[int, float] = {}
        self._video_last_rtcp_media_times_ms: dict[int, float] = {}
        self._video_transport_sequence: int = 0
        self._video_keyframes: dict[int, list[tuple[bytes, float, VoiceStream]]] = {}
        self._video_keyframe_resend_requests: set[int] = set()
        self._video_keyframe_replay_active: bool = False
        self._remote_media_sink_wants: dict[int, int] = {}
        self._remote_media_sink_wants_any: int | None = None
        self._remote_media_sink_wants_received: bool = False
        self._local_media_sink_wants: dict[int, int] = {}
        self._local_media_sink_wants_any: int | None = 100
        self._local_media_sink_wants_pixel_counts: dict[int, int] = {}

        self._connection = self.create_connection_state()

    def create_connection_state(self) -> _ConnectionState:
        return _ConnectionState(self, hook=self._voice_websocket_hook)

    async def _voice_websocket_hook(self, ws: Any, msg: dict[str, Any]) -> None:
        if msg['op'] != ws.MEDIA_SINK_WANTS:
            return
        await self.on_media_sink_wants(MediaSinkWants.from_payload(msg['d']))

    async def connect(
        self,
        *,
        reconnect: bool,
        timeout: float,
        self_deaf: bool = False,
        self_mute: bool = False,
        self_video: bool = False,
    ) -> None:
        await super().connect(
            reconnect=reconnect,
            timeout=timeout,
            self_deaf=self_deaf,
            self_mute=self_mute,
            self_video=self_video,
        )
        if not self.is_connected() or self._connection.secret_key is MISSING or self._connection.mode is MISSING:
            raise discord.ClientException('Voice connection did not finish negotiating transport encryption.')
        self._send_rtcp_receiver_report(self.ssrc)
        self._start_udp_ping()
        await self.enable_video_receive()

    @overload
    async def create_stream(
        self,
        *,
        timeout: float = ...,
        reconnect: bool = ...,
    ) -> StreamClient: ...

    @overload
    async def create_stream(
        self,
        *,
        timeout: float = ...,
        reconnect: bool = ...,
        cls: Callable[[VoiceProtocol, Stream], ST],
    ) -> ST: ...

    async def create_stream(
        self,
        *,
        timeout: float = 30.0,
        reconnect: bool = True,
        cls: Callable[[VoiceProtocol, Stream], ST] = MISSING,
    ) -> StreamClient | ST:
        """Create a Go Live stream from the current voice channel.

        Parameters
        ----------
        timeout: :class:`float`
            The number of seconds to wait for stream RTC connection.
        reconnect: :class:`bool`
            Whether the stream protocol should attempt reconnects.
        cls: Type[:class:`~discord.StreamProtocol`]
            A type that subclasses :class:`~discord.StreamProtocol` to connect with.
            Defaults to :class:`StreamClient`.

        Returns
        -------
        :class:`~discord.StreamProtocol`
            The connected stream RTC client.

        Raises
        ------
        ClientException
            The voice client is not connected or the voice session is not ready.
        """
        if not self.is_connected():
            raise discord.ClientException('Must be connected to voice before starting a stream')
        if self.session_id is None:
            raise discord.ClientException('Voice session is not ready yet')

        if cls is MISSING:
            from .stream_client import StreamClient

            return await super().create_stream(
                timeout=timeout,
                reconnect=reconnect,
                cls=StreamClient,
            )

        return await super().create_stream(
            timeout=timeout,
            reconnect=reconnect,
            cls=cls,
        )

    async def disconnect(self, *, force: bool = False) -> None:
        """|coro|

        Disconnects this voice client from voice.
        """
        await self._disconnect_stream_clients()
        self._prepare_disconnect()
        await super().disconnect(force=force)

    async def _disconnect_stream_clients(self) -> None:
        streams = tuple(self.stream_clients)
        if not streams:
            return

        results = await asyncio.gather(
            *(stream.disconnect(force=True) for stream in streams),
            return_exceptions=True,
        )
        for stream, result in zip(streams, results, strict=True):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, Exception):
                log.error(
                    'Failed to disconnect stream %s.',
                    stream.stream_key,
                    exc_info=(type(result), result, result.__traceback__),
                )

    def _prepare_disconnect(self) -> None:
        self._stop_udp_ping()
        self._stop_rtcp_feedback_listener()

    def _cleanup_media_transport(self) -> None:
        self._stop_udp_ping()
        self.stop_listening()
        self._stop_rtcp_feedback_listener()
        self._rtcp_sender_reports.clear()
        self._rtcp_sender_report_lsr.clear()
        self._rtcp_receiver_reports.clear()
        self._rtcp_receive_report_states.clear()
        self._rtp_send_stats.clear()
        self._remote_media_sink_wants.clear()
        self._remote_media_sink_wants_any = None
        self._remote_media_sink_wants_received = False

    def cleanup(self) -> None:
        self._cleanup_media_transport()
        super().cleanup()

    def _get_crypto(self) -> Any:
        state = (self.mode, bytes(self.secret_key))
        if self._crypto is None or self._crypto_state != state:
            self._crypto = TransportCrypto(*state)
            self._crypto_state = state
        return self._crypto

    def _get_receive_crypto(self) -> Any:
        state = (self.mode, bytes(self.secret_key))
        if self._receive_crypto is None or self._receive_crypto_state != state:
            self._receive_crypto = TransportCrypto(*state)
            self._receive_crypto_state = state
        return self._receive_crypto

    def _send_udp_packet(self, packet: bytes, context: str) -> bool:
        try:
            self._connection.send_packet(packet)
        except OSError:
            log.debug('Dropping %s packet after UDP send failed.', context, exc_info=True)
            return False
        return True

    def _log_packet_decode_error(self, message: str, *args: Any) -> None:
        interval = self.packet_decode_error_log_interval
        now = time.perf_counter()
        if interval and now - self._last_packet_decode_error_log < interval:
            return
        self._last_packet_decode_error_log = now
        log.debug(message, *args, exc_info=True)

    def _ignore_video_receive_stat(self, _key: str, _amount: int = 1) -> None:
        pass

    def _ignore_video_receive_max_stat(self, _key: str, _value: int) -> None:
        pass

    def _ignore_video_reorder_stats(self) -> None:
        pass

    def _record_video_receive_stat(self, key: str, amount: int = 1) -> None:
        self._video_receive_stats[key] += amount

    def _record_video_receive_max_stat(self, key: str, value: int) -> None:
        self._video_receive_stats[key] = max(self._video_receive_stats.get(key, 0), value)

    def _record_video_reorder_stats(self) -> None:
        for key, value in self._video_reorder_stats().items():
            if key == 'reorder_max_buffered':
                self._record_video_receive_max_stat(key, value)
            else:
                self._record_video_receive_stat(key, value)

    @property
    def udp_ping_rtt(self) -> float | None:
        if self._connection.socket is not MISSING:
            return self._connection.socket.ping_rtt_ms
        return self._udp_ping_rtt

    @property
    def udp_ping_timeouts(self) -> int:
        if self._connection.socket is not MISSING:
            return self._connection.socket.ping_timeouts
        return self._udp_ping_timeouts

    def _record_rtp_send_stats(
        self,
        *,
        ssrc: int,
        sequence: int,
        transport_sequence: int | None = None,
    ) -> None:
        self._rtp_send_stats[ssrc] = RTPSendStats(
            ssrc=ssrc,
            sequence=sequence & 0xFFFF,
            transport_sequence=None if transport_sequence is None else transport_sequence & 0xFFFF,
            updated_at=time.perf_counter(),
        )

    @property
    def rtp_send_stats(self) -> tuple[RTPSendStats, ...]:
        return tuple(self._rtp_send_stats.values())

    @property
    def audio_send_stats(self) -> AudioSendStats:
        ssrc = self.ssrc
        if ssrc is MISSING:
            ssrc = 0
        stats = self._rtp_send_stats.get(ssrc)
        return AudioSendStats(
            ssrc=ssrc,
            packets_sent=self._audio_packet_count,
            octets_sent=self._audio_octet_count,
            last_sequence=None if stats is None else stats.sequence,
            updated_at=None if stats is None else stats.updated_at,
        )

    @property
    def media_player_stats(self) -> MediaPlayerStats | None:
        player = self._player
        if isinstance(player, MediaPlayer):
            return player.stats
        return None

    def _start_udp_ping(self) -> None:
        if self._connection.socket is MISSING:
            return

        self._connection.socket.start_ping(
            self.udp_ping_initial_delay,
            self.udp_ping_interval,
            self.udp_ping_timeout,
        )

    def _stop_udp_ping(self) -> None:
        if self._connection.socket is MISSING:
            return

        self._udp_ping_rtt = self._connection.socket.ping_rtt_ms
        self._udp_ping_timeouts = self._connection.socket.ping_timeouts
        self._connection.socket.stop_ping()

    def _ensure_rtcp_feedback_listener(self) -> None:
        if self._rtcp_listener is not None:
            return

        self._rtcp_listener = self._handle_rtcp_packet
        self._connection.add_rtcp_listener(self._rtcp_listener)

    def _stop_rtcp_feedback_listener(self) -> None:
        listener = self._rtcp_listener
        if listener is not None:
            self._connection.remove_socket_listener(listener)
        self._rtcp_listener = None

    def _stop_rtcp_listener_if_idle(self) -> None:
        if self.is_listening() or (self.rtx_enabled and bool(self._active_video_streams)):
            return
        self._stop_rtcp_feedback_listener()

    def _start_video_nack_retry_thread(self) -> None:
        if not self.rtx_enabled or self._video_nack_retry_thread is not None:
            return

        stop = threading.Event()

        def retry_pending_video_nacks() -> None:
            while not stop.wait(VIDEO_NACK_RETRY_INTERVAL):
                pending_ssrcs = list(self._video_receive_pending_nacks)
                if not pending_ssrcs:
                    continue
                now = time.perf_counter()
                for media_ssrc in pending_ssrcs:
                    self._retry_pending_video_nacks(media_ssrc=media_ssrc, now=now)

        thread = threading.Thread(target=retry_pending_video_nacks, daemon=True, name='native-voice-video-nack-retry')
        self._video_nack_retry_stop = stop
        self._video_nack_retry_thread = thread
        thread.start()

    def _stop_video_nack_retry_thread(self) -> None:
        stop = self._video_nack_retry_stop
        thread = self._video_nack_retry_thread
        self._video_nack_retry_stop = None
        self._video_nack_retry_thread = None
        if stop is not None:
            stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def _apply_dave(self, media_type: str, codec: str, payload: bytes) -> bytes:
        dave_session = self._connection.dave_session
        if not dave_session or not self._connection.can_encrypt:
            return payload

        if media_type == 'audio' and codec == 'opus':
            return dave_session.encrypt_opus(payload)
        if codec == 'VP8':
            dave_codec = davey.Codec.vp8
        elif codec == 'VP9':
            dave_codec = davey.Codec.vp9
        elif codec == 'H264':
            dave_codec = davey.Codec.h264
        elif codec == 'H265':
            dave_codec = davey.Codec.h265
        elif codec == 'AV1':
            dave_codec = davey.Codec.av1
        else:
            dave_codec = davey.Codec.unknown

        return dave_session.encrypt(_dave_media_type(media_type), dave_codec, payload)

    def _can_decrypt_dave(self, user_id: int) -> bool:
        if self._connection.can_encrypt:
            return True
        dave_session = self._connection.dave_session
        if not dave_session or not dave_session.ready:
            return False
        return bool(dave_session.can_passthrough(user_id))

    def _decrypt_video_frame(self, user_id: int | None, payload: bytes) -> bytes:
        dave_session = self._connection.dave_session
        if user_id is None or not dave_session or not self._can_decrypt_dave(user_id):
            return payload
        return dave_session.decrypt(user_id, _dave_media_type('video'), payload)

    def _can_attempt_video_dave_decrypt(self, user_id: int | None) -> bool:
        dave_session = self._connection.dave_session
        return bool(user_id is not None and dave_session and self._can_decrypt_dave(user_id))

    def _stash_dave_video_frame(
        self,
        *,
        decoded: _DecodedRTPPacket,
        codec: str,
        ssrc: int,
        user_id: int | None,
        raw: bytes,
        payload: bytes,
        rtp_packets: tuple[RTPPacket, ...],
        received_at: float,
    ) -> None:
        now = time.perf_counter()
        expired = 0
        while (
            self._pending_dave_video_frames
            and now - self._pending_dave_video_frames[0].queued_at > DAVE_PENDING_VIDEO_MAX_AGE
        ):
            self._pending_dave_video_frames.popleft()
            expired += 1
        if expired:
            log.debug('Expired %s pending DAVE video frame(s).', expired)

        if len(self._pending_dave_video_frames) >= DAVE_PENDING_VIDEO_MAX_FRAMES:
            self._pending_dave_video_frames.popleft()
            log.debug('Dropped oldest pending DAVE video frame after reaching queue limit.')

        self._pending_dave_video_frames.append(
            _PendingDaveVideoFrame(
                decoded=decoded,
                codec=codec,
                ssrc=ssrc,
                user_id=user_id,
                raw=raw,
                payload=payload,
                rtp_packets=rtp_packets,
                received_at=received_at,
                queued_at=now,
            )
        )

    def _retry_pending_dave_video_frames(self) -> list[MediaPacket]:
        if not self._pending_dave_video_frames:
            return []

        now = time.perf_counter()
        ready: list[MediaPacket] = []
        remaining: deque[_PendingDaveVideoFrame] = deque()
        while self._pending_dave_video_frames:
            frame = self._pending_dave_video_frames.popleft()
            if now - frame.queued_at > DAVE_PENDING_VIDEO_MAX_AGE:
                continue
            if not self._can_attempt_video_dave_decrypt(frame.user_id):
                remaining.append(frame)
                continue
            try:
                payload = self._decrypt_video_frame(frame.user_id, frame.payload)
            except Exception as exc:
                if _is_retryable_dave_decrypt_error(exc):
                    remaining.append(frame)
                    continue
                log.debug('Failed to decrypt pending DAVE video frame from SSRC %s.', frame.ssrc, exc_info=True)
                continue
            ready.append(
                self._make_video_media_packet(
                    frame.decoded,
                    codec=frame.codec,
                    ssrc=frame.ssrc,
                    user_id=frame.user_id,
                    raw=frame.raw,
                    payload=payload,
                    rtp_packets=frame.rtp_packets,
                    received_at=frame.received_at,
                )
            )

        self._pending_dave_video_frames = remaining
        if ready:
            log.debug('Retried %s pending DAVE video frame(s).', len(ready))
        return ready

    def _video_params_for_source(
        self,
        source: MediaSource,
        *,
        video_width: int | None = None,
        video_height: int | None = None,
        video_fps: int | None = None,
        video_bitrate: int | None = None,
    ) -> _VideoStartParams:
        video_config = source.video_config
        width = video_width if video_width is not None else (video_config.width if video_config is not None else None)
        height = video_height if video_height is not None else (video_config.height if video_config is not None else None)
        fps = video_fps if video_fps is not None else (video_config.fps if video_config is not None else 30)
        bitrate = (
            video_bitrate
            if video_bitrate is not None
            else (
                video_config.bitrate
                if video_config is not None and video_config.bitrate > 0
                else _native_video_bitrate(height, fps)
            )
        )
        codec = video_config.codec if video_config is not None else None
        negotiated_codec = self.negotiated_video_codec
        if codec is None:
            codec = negotiated_codec
        elif negotiated_codec is not None and codec.upper() != negotiated_codec:
            raise discord.ClientException(
                f'Video source is {codec.upper()}, but Discord negotiated {negotiated_codec}; '
                'transcode the source to the negotiated codec before playback'
            )
        if width is None or height is None or width <= 0 or height <= 0:
            raise discord.ClientException(
                'video_width and video_height are required to play a video MediaSource without video_config before video is started'
            )
        return {'width': width, 'height': height, 'fps': fps, 'bitrate': bitrate}

    @property
    def negotiated_video_codec(self) -> str | None:
        """Optional[:class:`str`]: The video codec selected by the voice server."""

        codec = self._connection.video_codec
        return codec.upper() if codec is not None else None

    def _get_voice_packet(self, data: bytes, *, audio_level: int = RTP_AUDIO_LEVEL_SILENCE) -> bytes:
        packet = self._apply_dave('audio', 'opus', data)
        header = bytearray(12)
        header[0] = 0x80
        header[1] = 0x78
        struct.pack_into('>H', header, 2, self.sequence)
        struct.pack_into('>I', header, 4, self.timestamp)
        struct.pack_into('>I', header, 8, self.ssrc)

        extension_payload = _audio_rtp_extension_payload(self._speaking_flags.value, audio_level=audio_level)
        if extension_payload:
            header = bytearray(_rtp_header_with_one_byte_extensions(header, extension_payload))
            packet = extension_payload + packet

        return self._get_crypto().encrypt_rtp(bytes(header), packet)

    def _maybe_send_audio_rtcp_sender_report(self, rtp_timestamp: int) -> None:
        media_time_ms = rtp_timestamp * 1000 // opus.Encoder.SAMPLING_RATE
        last_media_time_ms = self._audio_last_rtcp_media_time_ms
        if last_media_time_ms is not None and media_time_ms // 1000 <= last_media_time_ms // 1000:
            return

        self._send_rtcp_sender_report(
            ssrc=self.ssrc,
            rtp_timestamp=rtp_timestamp,
            packet_count=self._audio_packet_count,
            octet_count=self._audio_octet_count,
        )
        self._audio_last_rtcp_media_time_ms = media_time_ms

    def send_audio_packet(self, data: bytes, *, encode: bool = True) -> None:
        self.checked_add('sequence', 1, 65535)
        if encode:
            audio_level = _audio_level_from_pcm(data)
            encoded_data = self.encoder.encode(data, self.encoder.SAMPLES_PER_FRAME)
        else:
            audio_level = RTP_AUDIO_LEVEL_SILENCE if data == opus.OPUS_SILENCE else 0
            encoded_data = data

        rtp_timestamp = self.timestamp
        self._maybe_send_audio_rtcp_sender_report(rtp_timestamp)
        packet = self._get_voice_packet(encoded_data, audio_level=audio_level)
        try:
            self._connection.send_packet(packet)
        except OSError:
            log.debug('A packet has been dropped (seq: %s, timestamp: %s).', self.sequence, self.timestamp)
        else:
            self._audio_packet_count = (self._audio_packet_count + 1) & 0xFFFFFFFF
            self._audio_octet_count = (self._audio_octet_count + len(encoded_data)) & 0xFFFFFFFF
            self._record_rtp_send_stats(ssrc=self.ssrc, sequence=self.sequence)

        self.checked_add('timestamp', opus.Encoder.SAMPLES_PER_FRAME, 4294967295)

    def play(
        self,
        source: AudioSource,
        *,
        after: Callable[[Exception | None], Any] | None = None,
        application: Any = 'audio',
        bitrate: int = 64,
        fec: bool = True,
        expected_packet_loss: float = 0.0,
        bandwidth: Any = 'full',
        signal_type: Any = 'auto',
        video_width: int | None = None,
        video_height: int | None = None,
        video_fps: int | None = None,
        video_bitrate: int | None = None,
    ) -> None:
        """Play an audio or media source.

        This extends :meth:`discord.VoiceClient.play` with
        :class:`MediaSource` support. When the source has video, the client
        starts the negotiated video transport before sending frames.

        The finalizer, ``after`` is called after the source has been exhausted
        or an error occurred.

        If an error happens while the media player is running, the exception is
        caught and the player is then stopped. If no after callback is passed,
        any caught exception will be logged using the library logger.

        Extra parameters may be passed to the internal opus encoder if a PCM
        based audio source is used. Otherwise, they are ignored.

        Parameters
        ----------
        source: :class:`discord.AudioSource`
            The audio or media source to play.
        after: Callable[[Optional[:class:`Exception`]], Any]
            The finalizer that is called after the stream is exhausted.
            This function must have a single parameter, ``error``, that
            denotes an optional exception that was raised during playing.
        application: :class:`str`
            Configures the encoder's intended application. Can be one of:
            ``'audio'``, ``'voip'``, ``'lowdelay'``.
            Defaults to ``'audio'``.
        bitrate: :class:`int`
            Configures the bitrate in the audio encoder. Can be between ``16`` and
            ``512``. Defaults to ``64``.
        fec: :class:`bool`
            Configures the encoder's use of inband forward error correction.
            Defaults to ``True``.
        expected_packet_loss: :class:`float`
            Configures the encoder's expected packet loss percentage. Requires
            FEC. Defaults to ``0.0``.
        bandwidth: :class:`str`
            Configures the encoder's bandpass. Can be one of: ``'narrow'``,
            ``'medium'``, ``'wide'``, ``'superwide'``, ``'full'``. Defaults to
            ``'full'``.
        signal_type: :class:`str`
            Configures the type of signal being encoded. Can be one of:
            ``'auto'``, ``'voice'``, ``'music'``. Defaults to ``'auto'``.
        video_width: Optional[:class:`int`]
            Video width used when the source does not provide a
            :class:`VideoConfig`.
        video_height: Optional[:class:`int`]
            Video height used when the source does not provide a
            :class:`VideoConfig`.
        video_fps: Optional[:class:`int`]
            Video frame rate override.
        video_bitrate: Optional[:class:`int`]
            Video bitrate override in bits per second.

        Raises
        ------
        ClientException
            Already playing media or not connected.
        TypeError
            Source is not a :class:`AudioSource` or after is not callable.
        OpusNotLoaded
            Source is not opus encoded and opus is not loaded.
        ValueError
            An improper value was passed as an encoder parameter.
        """
        if not self.is_connected():
            raise discord.ClientException('Not connected to voice')
        if self.is_playing():
            raise discord.ClientException('Already playing media')
        if not isinstance(source, AudioSource):
            raise TypeError(f'source must be an AudioSource not {source.__class__.__name__}')
        if after is not None and not callable(after):
            raise TypeError('after must be callable')

        media_source = source if isinstance(source, MediaSource) else AudioMediaSource(source)
        self._media_player_source_changed(media_source)

        if media_source.has_audio() and not media_source.is_opus():
            self.encoder = opus.Encoder(
                application=application,
                bitrate=bitrate,
                fec=fec,
                expected_packet_loss=expected_packet_loss,
                bandwidth=bandwidth,
                signal_type=signal_type,
            )

        video_transport_start = None
        if media_source.has_video() and self._video_codec is None:
            video_params = self._video_params_for_source(
                media_source,
                video_width=video_width,
                video_height=video_height,
                video_fps=video_fps,
                video_bitrate=video_bitrate,
            )
            video_transport_start = asyncio.run_coroutine_threadsafe(
                self.start_video(**video_params),
                self.loop,
            )

        self._player = MediaPlayer(source, media_source, self, after=after, video_transport_start=video_transport_start)
        self._player.start()

    def _negotiated_video_streams(self) -> list[VoiceStream]:
        streams = [stream for stream in self._connection.video_streams.values() if stream.ssrc is not None]
        if not streams:
            raise discord.ClientException(
                f'Voice server did not negotiate a {self.media_stream_type} SSRC for this connection'
            )
        return streams

    def _video_stream_has_remote_receivers(self, stream: VoiceStream) -> bool:
        ssrc = stream.ssrc
        if ssrc is None:
            return False

        self._prune_rtcp_receiver_reports()
        if any(report.source_ssrc == ssrc for report in self._rtcp_receiver_reports.values()):
            return True

        if not self._remote_media_sink_wants_received:
            return False

        want = self._remote_media_sink_wants.get(ssrc)
        if want is not None:
            return want > 0

        return self._remote_media_sink_wants_any is not None and self._remote_media_sink_wants_any > 0

    def _video_stream_should_send(self, stream: VoiceStream) -> bool:
        if not stream.active:
            return False
        if stream.quality >= 100:
            return True
        return self._video_stream_has_remote_receivers(stream)

    def _select_active_video_streams(self, streams: Sequence[VoiceStream]) -> tuple[VoiceStream, ...]:
        if not self._video_source_supports_simulcast:
            return (max(streams, key=lambda stream: stream.quality),)

        active = tuple(stream for stream in streams if self._video_stream_should_send(stream))
        if active:
            return active
        return (max(streams, key=lambda stream: stream.quality),)

    def _video_state_for_streams(
        self,
        *,
        streams: Sequence[VoiceStream],
        width: int,
        height: int,
        fps: int,
        bitrate: int,
    ) -> tuple[VoiceStream, ...]:
        state_streams = []
        for stream in streams:
            self._require_video_stream_ssrc(stream)
            stream_bitrate = stream.max_bitrate
            if stream_bitrate is None:
                stream_bitrate = bitrate if stream.quality >= 100 else max(1, bitrate // 4)

            stream_fps = stream.max_framerate if stream.max_framerate is not None else fps
            stream_resolution = stream.max_resolution or VoiceStreamResolution.fixed(width=width, height=height)
            stream_rtx_ssrc = (
                self._rtx_ssrc_for_video_stream(stream)
                if self.rtx_enabled and self._video_rtx_payload_type is not None
                else 0
            )

            state_streams.append(
                stream.replace(
                    active=True,
                    rtx_ssrc=stream_rtx_ssrc or None,
                    max_bitrate=stream_bitrate,
                    max_framerate=stream_fps,
                    max_resolution=stream_resolution,
                )
            )
        return tuple(state_streams)

    def _reset_video_stream_counters(self, ssrc: int) -> None:
        self._video_packet_counts[ssrc] = 0
        self._video_octet_counts[ssrc] = 0
        self._video_media_times_ms[ssrc] = 0
        self._video_last_rtcp_media_times_ms[ssrc] = -1

    def _clear_video_send_caches(self) -> None:
        self._video_send_pipeline.clear()
        self._video_packet_counts.clear()
        self._video_octet_counts.clear()
        self._video_media_times_ms.clear()
        self._video_last_rtcp_media_times_ms.clear()
        self._video_transport_sequence = 0

    def _record_video_packet_send(self, *, ssrc: int, packets: int, octets: int) -> None:
        self._video_packet_counts[ssrc] = self._video_packet_counts.get(ssrc, 0) + packets
        self._video_octet_counts[ssrc] = (self._video_octet_counts.get(ssrc, 0) + octets) & 0xFFFFFFFF

    async def _announce_video_state(self, *, force: bool = False) -> None:
        video_ssrc = self._video_ssrc
        rtx_ssrc = self._video_rtx_ssrcs.get(video_ssrc, 0) if self._video_rtx_active_for_ssrc(video_ssrc) else 0
        video_state = (video_ssrc, rtx_ssrc, self._video_state_streams)
        if not force and video_state == self._last_video_state:
            return

        self._last_video_state = video_state
        await self.ws.video_state(
            video_ssrc=video_ssrc,
            rtx_ssrc=rtx_ssrc,
            streams=self._video_state_streams,
        )

    async def _apply_selected_video_stream(self, *, codec: str, params: _VideoStartParams, reset: bool = False) -> None:
        negotiated_streams = self._negotiated_video_streams()
        selected_streams = self._select_active_video_streams(negotiated_streams)
        selected = selected_streams[0]
        selected_ssrc = self._require_video_stream_ssrc(selected)

        self._video_codec = codec
        self._video_rtx_payload_type = (
            self._get_codec_lookups()['video_rtx_payload_types'].get(codec.upper()) if self.rtx_enabled else None
        )
        if self.rtx_enabled and self._video_rtx_payload_type is None:
            log.warning('RTX enabled, but codec %s was not advertised with an RTX payload type.', codec)

        self._video_ssrc = selected_ssrc
        active_ssrcs = {self._require_video_stream_ssrc(stream) for stream in selected_streams}
        if reset:
            self._clear_video_send_caches()

        self._video_send_pipeline.retain_streams(list(active_ssrcs))
        self._video_rids = {}
        self._video_rtx_ssrcs = {}
        for stream in negotiated_streams:
            ssrc = self._require_video_stream_ssrc(stream)
            self._video_rids[ssrc] = stream.rid
            announced_rtx_ssrc = self._rtx_ssrc_for_video_stream(stream)
            self._video_rtx_ssrcs[ssrc] = (
                announced_rtx_ssrc if self.rtx_enabled and self._video_rtx_payload_type is not None else 0
            )

        payload_type = self._payload_type_for_codec(codec)
        for stream in selected_streams:
            ssrc = self._require_video_stream_ssrc(stream)
            if ssrc not in self._video_packet_counts:
                self._reset_video_stream_counters(ssrc)
            self._video_send_pipeline.configure_stream(
                codec,
                ssrc,
                payload_type,
                self._video_rtx_payload_type,
                self._video_rtx_ssrcs.get(ssrc, 0),
                stream.rid,
            )

        self._active_video_streams = selected_streams
        self._active_video_streams_by_rid = {stream.rid: stream for stream in selected_streams}

        state_streams = negotiated_streams if self._video_source_supports_simulcast else selected_streams
        self._video_state_streams = self._video_state_for_streams(
            width=params['width'],
            height=params['height'],
            fps=params['fps'],
            bitrate=params['bitrate'],
            streams=state_streams,
        )
        await self._announce_video_state(force=reset)

        if self.rtx_enabled and not self._video_rtx_active_for_ssrc(self._video_ssrc):
            log.warning('RTX enabled, but the voice server did not negotiate an RTX SSRC.')

        if any(self._video_rtx_active_for_ssrc(self._require_video_stream_ssrc(stream)) for stream in selected_streams):
            self._ensure_rtcp_feedback_listener()
        else:
            self._stop_rtcp_listener_if_idle()

    def _make_video_depacketizer(self, codec: str) -> Any:
        normalized = codec.upper()
        try:
            return _VIDEO_DEPACKETIZER_FACTORIES[normalized]()
        except KeyError:
            raise discord.ClientException(f'Unsupported video codec: {codec}') from None

    def _payload_type_for_codec(self, codec: str) -> int:
        normalized = codec.upper()
        payload_type = self._get_codec_lookups()['video_payload_types'].get(normalized)
        if payload_type is not None:
            return payload_type
        raise discord.ClientException(f'Unsupported video codec: {codec}')

    def _rtx_ssrc_for_video_stream(self, stream: VoiceStream) -> int:
        if stream.rtx_ssrc is not None:
            return stream.rtx_ssrc
        return self._require_video_stream_ssrc(stream) + 1

    @staticmethod
    def _require_video_stream_ssrc(stream: VoiceStream) -> int:
        ssrc = stream.ssrc
        if ssrc is None:
            raise discord.ClientException(f'Voice server did not negotiate an SSRC for stream RID {stream.rid!r}')
        return ssrc

    def _video_rtx_active_for_ssrc(self, media_ssrc: int) -> bool:
        return (
            self.rtx_enabled and self._video_rtx_payload_type is not None and self._video_rtx_ssrcs.get(media_ssrc, 0) != 0
        )

    async def start_video(
        self,
        *,
        width: int,
        height: int,
        fps: int = 30,
        bitrate: int = 0,
    ) -> None:
        """Start outbound video using the negotiated video codec.

        Parameters
        ----------
        width: :class:`int`
            Encoded video width.
        height: :class:`int`
            Encoded video height.
        fps: :class:`int`
            Encoded frame rate.
        bitrate: :class:`int`
            Target bitrate in bits per second.

        Raises
        ------
        ClientException
            The voice client is not connected, or no video codec was negotiated.
        """
        if not self.is_connected():
            raise discord.ClientException('Not connected to voice')

        negotiated_codec = self.negotiated_video_codec
        if negotiated_codec is None:
            raise discord.ClientException('Voice server did not negotiate a video codec')
        params: _VideoStartParams = {
            'width': width,
            'height': height,
            'fps': fps,
            'bitrate': bitrate if bitrate > 0 else _native_video_bitrate(height, fps),
        }
        self._video_start_params = params
        await self._apply_selected_video_stream(codec=negotiated_codec, params=params, reset=True)

    async def enable_video_receive(self) -> None:
        await self.ws.video_state(video_ssrc=0, rtx_ssrc=0, streams=[])

    async def _send_media_sink_wants(self) -> None:
        await self.ws.media_sink_wants(
            self._local_media_sink_wants,
            any=self._local_media_sink_wants_any,
            pixel_counts=self._local_media_sink_wants_pixel_counts or None,
        )

    async def request_video(
        self,
        ssrc: int,
        *,
        quality: int = 100,
        any: int | None = MISSING,
        pixel_count: int | None = None,
    ) -> None:
        """Request that Discord forwards video for an SSRC.

        Parameters
        ----------
        ssrc: :class:`int`
            The video SSRC to request.
        quality: :class:`int`
            The requested stream quality.
        any: Optional[:class:`int`]
            The fallback quality request for otherwise unspecified streams.
        pixel_count: Optional[:class:`int`]
            Optional pixel-count hint sent with the media sink wants payload.

        Raises
        ------
        ClientException
            The voice client is not connected.
        """
        if not self.is_connected():
            raise discord.ClientException('Not connected to voice')

        self._local_media_sink_wants[ssrc] = quality
        if any is not MISSING:
            self._local_media_sink_wants_any = any
        if pixel_count is None:
            self._local_media_sink_wants_pixel_counts.pop(ssrc, None)
        else:
            self._local_media_sink_wants_pixel_counts[ssrc] = pixel_count
        await self._send_media_sink_wants()

    async def on_media_sink_wants(self, wants: MediaSinkWants, /) -> None:
        self._remote_media_sink_wants = dict(wants.wants)
        self._remote_media_sink_wants_any = wants.any
        self._remote_media_sink_wants_received = True

        player = self._player
        if isinstance(player, MediaPlayer):
            player.media_source.on_media_sink_wants(wants)

        if self._video_start_params is None or self._video_codec is None:
            return
        await self._apply_selected_video_stream(codec=self._video_codec, params=self._video_start_params)

    async def stop_video(self) -> None:
        """Stop outbound video and reset video transport state."""
        self._last_video_state = None
        if self.ws:
            await self.ws.video_state(video_ssrc=0, rtx_ssrc=0, streams=[])
        self._video_codec = None
        self._video_start_params = None
        self._video_state_streams = ()
        self._active_video_streams = ()
        self._active_video_streams_by_rid.clear()
        self._video_ssrc = 0
        self._video_rtx_payload_type = None
        self._video_rtx_ssrcs.clear()
        self._video_rids.clear()
        self._clear_video_send_caches()
        self._video_keyframes.clear()
        self._video_keyframe_resend_requests.clear()
        self._video_keyframe_replay_active = False
        self._stop_rtcp_listener_if_idle()

    def _send_video_rtx_packet(self, sequence: int, *, media_ssrc: int | None = None) -> bool:
        media_ssrc = self._video_ssrc if media_ssrc is None else media_ssrc
        rtx_payload_type = self._video_rtx_payload_type
        rtx_ssrc = self._video_rtx_ssrcs.get(media_ssrc, 0)
        if not self.rtx_enabled or rtx_payload_type is None or not rtx_ssrc:
            return False

        try:
            native_sent, native_rtx_ssrc, native_rtx_sequence, transport_sequence = (
                self._video_send_pipeline.send_video_rtx_packet(
                    self._get_crypto(),
                    self._connection.socket.fileno(),
                    media_ssrc,
                    sequence & 0xFFFF,
                    self.media_stream_type,
                )
            )
        except Exception:
            log.debug('Failed to resend video packet via native RTX pipeline.', exc_info=True)
            return False

        if not native_sent:
            return False

        self._video_transport_sequence = transport_sequence
        self._record_rtp_send_stats(
            ssrc=native_rtx_ssrc,
            sequence=native_rtx_sequence,
            transport_sequence=transport_sequence,
        )
        return True

    def _handle_rtcp_packet(self, data: bytes) -> bool:
        try:
            packets = self._get_receive_crypto().decrypt_rtcp_packets(data, RTCP_PROTECTED_HEADER_LEN)
        except Exception:
            self._log_packet_decode_error('Failed to decrypt RTCP packet.')
            return True

        for packet_type, fmt, sender_ssrc, body in packets:
            if packet_type == RTCP_SENDER_REPORT:
                self._handle_rtcp_sender_report(sender_ssrc, body)
            elif packet_type == RTCP_RECEIVER_REPORT:
                self._handle_rtcp_receiver_report(sender_ssrc, fmt, body)
            elif packet_type == RTCP_RTP_FEEDBACK and fmt == RTCP_GENERIC_NACK:
                self._handle_rtcp_nack(body)
            elif packet_type == RTCP_PAYLOAD_FEEDBACK and fmt == RTCP_PICTURE_LOSS_INDICATION:
                self._handle_rtcp_pli(body)
        return True

    def _handle_rtcp_sender_report(self, sender_ssrc: int, payload: bytes) -> None:
        if len(payload) < 20:
            return

        ntp_seconds, ntp_fraction, rtp_timestamp, _packet_count, _octet_count = struct.unpack_from('>IIIII', payload)
        ntp_time = ntp_seconds - 2_208_988_800 + (ntp_fraction / (1 << 32))
        self._rtcp_sender_reports[sender_ssrc] = (
            ntp_time,
            rtp_timestamp,
            self._rtp_clock_rate_for_ssrc(sender_ssrc),
        )
        self._rtcp_sender_report_lsr[sender_ssrc] = (
            ((ntp_seconds & 0xFFFF) << 16) | (ntp_fraction >> 16),
            time.perf_counter(),
        )

    def _handle_rtcp_receiver_report(self, sender_ssrc: int, report_count: int, payload: bytes) -> None:
        now = time.perf_counter()
        self._prune_rtcp_receiver_reports(now=now)
        for offset in range(0, min(len(payload), report_count * 24), 24):
            if offset + 24 > len(payload):
                return

            source_ssrc = struct.unpack_from('>I', payload, offset)[0]
            loss = int.from_bytes(payload[offset + 4 : offset + 8], 'big')
            fraction_lost = (loss >> 24) & 0xFF
            cumulative_lost = loss & 0xFFFFFF
            if cumulative_lost & 0x800000:
                cumulative_lost -= 0x1000000
            extended_high_sequence, jitter, last_sender_report, delay_since_last_sender_report = struct.unpack_from(
                '>IIII',
                payload,
                offset + 8,
            )
            self._rtcp_receiver_reports[sender_ssrc, source_ssrc] = RTCPReceiverReport(
                sender_ssrc=sender_ssrc,
                source_ssrc=source_ssrc,
                fraction_lost=fraction_lost,
                cumulative_lost=cumulative_lost,
                extended_high_sequence=extended_high_sequence,
                jitter=jitter,
                last_sender_report=last_sender_report,
                delay_since_last_sender_report=delay_since_last_sender_report,
                received_at=now,
            )

    def _prune_rtcp_receiver_reports(self, *, now: float | None = None) -> None:
        reports = self._rtcp_receiver_reports
        if not reports:
            return

        now = time.perf_counter() if now is None else now
        cutoff = now - self.rtcp_receiver_report_ttl
        expired = [key for key, report in reports.items() if report.received_at < cutoff]
        for key in expired:
            del reports[key]

    @property
    def rtcp_receiver_reports(self) -> tuple[RTCPReceiverReport, ...]:
        self._prune_rtcp_receiver_reports()
        return tuple(self._rtcp_receiver_reports.values())

    @property
    def video_receive_stats(self) -> dict[str, int]:
        if not self.enable_debug_stats:
            return {}

        stats = dict(self._video_receive_stats)
        for key, value in self._video_reorder_stats().items():
            if key == 'reorder_max_buffered':
                stats[key] = max(stats.get(key, 0), value)
            else:
                stats[key] = stats.get(key, 0) + value
        return stats

    def _video_reorder_stats(self) -> dict[str, int]:
        stats: dict[str, int] = {}
        for buffer in self._video_receive_reorder_buffers.values():
            stats['reorder_packets_pushed'] = stats.get('reorder_packets_pushed', 0) + buffer.packets_pushed
            stats['reorder_duplicate_packets'] = stats.get('reorder_duplicate_packets', 0) + buffer.duplicate_packets
            stats['reorder_old_packets'] = stats.get('reorder_old_packets', 0) + buffer.old_packets
            stats['reorder_waits'] = stats.get('reorder_waits', 0) + buffer.waits
            stats['reorder_flushes'] = stats.get('reorder_flushes', 0) + buffer.flushes
            stats['reorder_skipped_packets'] = stats.get('reorder_skipped_packets', 0) + buffer.skipped_packets
            stats['reorder_ready_packets'] = stats.get('reorder_ready_packets', 0) + buffer.ready_packets
            stats['reorder_max_buffered'] = max(stats.get('reorder_max_buffered', 0), buffer.max_buffered)
        return stats

    def _rtp_clock_rate_for_ssrc(self, ssrc: int) -> int:
        return 90_000 if ssrc in self._connection.video_ssrcs else opus.Encoder.SAMPLING_RATE

    def _media_ssrc_for_rtx_ssrc(self, ssrc: int) -> int | None:
        media_ssrc = self._connection.rtx_ssrc_media_ssrcs.get(ssrc)
        return media_ssrc if media_ssrc is not None else ssrc - 1 if ssrc > 0 else None

    def _decode_rtp_packets(self, packets: Sequence[bytes]) -> list[tuple[bytes, _DecodedRTPPacket]]:
        failed, decrypted_packets = self._get_receive_crypto().decrypt_rtp_packets(packets)
        if failed:
            log.debug('Failed to decrypt %s RTP packet(s) in receive batch.', failed)

        decoded_packets: list[tuple[bytes, _DecodedRTPPacket]] = []
        for packet in decrypted_packets:
            raw = packets[packet[0]]
            decoded_packets.append((raw, self._decoded_rtp_packet_from_payload(packet[1:])))
        return decoded_packets

    @staticmethod
    def _decoded_rtp_packet_from_payload(payload_data: Sequence[Any]) -> _DecodedRTPPacket:
        (
            payload_type,
            marker,
            padded,
            sequence,
            timestamp,
            ssrc,
            extended,
            extension_payload,
            rtp_extensions,
            payload,
        ) = payload_data
        extension_payload = bytes(extension_payload)
        if not rtp_extensions:
            rtp_extensions = ()
        elif not (isinstance(rtp_extensions, tuple) and all(isinstance(item, RTPExtension) for item in rtp_extensions)):
            rtp_extensions = tuple(RTPExtension(int(extension_id), bytes(data)) for extension_id, data in rtp_extensions)
        return _DecodedRTPPacket(
            payload_type=payload_type,
            marker=marker,
            padded=padded,
            sequence=sequence,
            timestamp=timestamp,
            ssrc=ssrc,
            extended=extended,
            extension_payload=extension_payload,
            rtp_extensions=rtp_extensions,
            payload=bytes(payload),
        )

    @staticmethod
    def _absolute_send_time_from_extensions(extensions: Sequence[RTPExtension]) -> float | None:
        for extension in extensions:
            if extension.id != RTP_EXT_ABSOLUTE_SEND_TIME or len(extension.data) != 3:
                continue

            value = int.from_bytes(extension.data, 'big')
            send_time_mod_64 = value / (1 << 18)
            now = time.time()
            send_time = now - (now % 64) + send_time_mod_64
            if send_time - now > 32:
                send_time -= 64
            elif now - send_time > 32:
                send_time += 64
            return send_time
        return None

    @staticmethod
    def _audio_speaking_flags_from_extensions(extensions: Sequence[RTPExtension]) -> SpeakingFlags:
        voice = SpeakingFlags.VALID_FLAGS['voice']
        soundshare = SpeakingFlags.VALID_FLAGS['soundshare']
        priority = SpeakingFlags.VALID_FLAGS['priority']
        for extension in extensions:
            if extension.id == RTP_EXT_DISCORD_SPEAKING and len(extension.data) == 1:
                value = extension.data[0]
                speaking = (value >> 1) & (voice | soundshare)
                if value == 0:
                    return SpeakingFlags._from_value(voice)
                if value & 1:
                    speaking |= voice | priority
                return SpeakingFlags._from_value(speaking)
        return SpeakingFlags._from_value(voice)

    @staticmethod
    def _audio_level_from_extensions(extensions: Sequence[RTPExtension]) -> tuple[int | None, bool | None]:
        for extension in extensions:
            if extension.id == RTP_EXT_AUDIO_LEVEL and len(extension.data) == 1:
                value = extension.data[0]
                return value & 0x7F, bool(value & 0x80)
        return None, None

    @staticmethod
    def _is_unencrypted_opus_silence_payload(payload: bytes) -> bool:
        if payload == opus.OPUS_SILENCE:
            return True
        if not payload.startswith(opus.OPUS_SILENCE):
            return False
        padding = payload[len(opus.OPUS_SILENCE) :]
        return bool(padding) and len(padding) <= 255 and all(byte == len(padding) for byte in padding)

    def _rtcp_time_for_packet(
        self,
        ssrc: int,
        timestamp: int,
        extensions: Sequence[RTPExtension] = (),
    ) -> float | None:
        report = self._rtcp_sender_reports.get(ssrc)
        if report is None:
            return self._absolute_send_time_from_extensions(extensions)

        ntp_time, report_timestamp, clock_rate = report
        delta = _rtp_timestamp_delta(timestamp, report_timestamp)
        if delta is None:
            return self._absolute_send_time_from_extensions(extensions)
        return ntp_time + (delta / clock_rate)

    def _handle_rtcp_nack(self, payload: bytes) -> None:
        if len(payload) < 8:
            return

        media_ssrc = struct.unpack_from('>I', payload, 0)[0]
        if not self._video_send_pipeline.has_stream(media_ssrc):
            return
        nack_sequences = 0
        for offset in range(4, len(payload) - 3, 4):
            _pid, blp = struct.unpack_from('>HH', payload, offset)
            nack_sequences += 1 + blp.bit_count()
        self._record_video_receive_stat('rtcp_nack_packets_received')
        self._record_video_receive_stat('rtcp_nack_sequences_received', nack_sequences)

        try:
            resent, media_ssrc, rtx_ssrc, rtx_sequence, transport_sequence = (
                self._video_send_pipeline.send_video_rtx_for_nack(
                    self._get_crypto(),
                    self._connection.socket.fileno(),
                    payload,
                    self.media_stream_type,
                    self.video_packet_burst_size,
                    self.video_packet_burst_interval,
                )
            )
        except Exception:
            log.debug('Failed to resend video packets via native RTX NACK responder.', exc_info=True)
            return

        if resent:
            self._record_video_receive_stat('rtx_packets_resent', resent)
            self._video_transport_sequence = transport_sequence
            self._record_rtp_send_stats(
                ssrc=rtx_ssrc,
                sequence=rtx_sequence,
                transport_sequence=transport_sequence,
            )
            log.debug('Resent %s video packet(s) via native RTX for NACK media_ssrc=%s.', resent, media_ssrc)

    def _handle_rtcp_pli(self, payload: bytes) -> None:
        if len(payload) < 4:
            return

        media_ssrc = struct.unpack_from('>I', payload, 0)[0]
        if not self._video_send_pipeline.has_stream(media_ssrc):
            return

        self._video_keyframe_resend_requests.add(media_ssrc)

    def send_video_pli(self, ssrc: int) -> None:
        header = struct.pack(
            '>BBHI',
            0x80 | RTCP_PICTURE_LOSS_INDICATION,
            RTCP_PAYLOAD_FEEDBACK,
            2,
            self._connection.ssrc,
        )
        body = struct.pack('>I', ssrc & 0xFFFFFFFF)
        self._send_udp_packet(self._get_crypto().encrypt_rtcp(header, body), 'RTCP PLI')

    def _maybe_send_startup_video_pli(self, *, codec: str, ssrc: int) -> None:
        if codec.upper() not in _H26X_PARAMETER_SET_TYPES or ssrc in self._video_receive_startup_plis:
            return

        self._video_receive_startup_plis.add(ssrc)
        try:
            self.send_video_pli(ssrc)
        except Exception:
            log.debug('Failed to send startup RTCP PLI for SSRC %s.', ssrc, exc_info=True)

    @staticmethod
    def _is_config_keyframe(codec: str, frame: bytes) -> bool:
        codec = codec.upper()
        parameter_sets = _H26X_PARAMETER_SET_TYPES.get(codec)
        keyframe_types = _H26X_KEYFRAME_TYPES.get(codec)
        if parameter_sets is None or keyframe_types is None:
            return False
        nal_types = set[int]()
        index = 0
        nal_count = 0
        while index + 3 <= len(frame) and nal_count < 16:
            start3 = frame.find(b'\x00\x00\x01', index)
            start4 = frame.find(b'\x00\x00\x00\x01', index)
            if start3 == -1 and start4 == -1:
                break
            if start4 != -1 and (start3 == -1 or start4 <= start3):
                start, start_code_len = start4, 4
            else:
                start, start_code_len = start3, 3

            nal_start = start + start_code_len
            if codec == 'H264':
                if nal_start >= len(frame):
                    break
                nal_type = frame[nal_start] & 0x1F
                nal_types.add(nal_type)
                index = nal_start + 1
                if 1 <= nal_type <= 5:
                    break
            else:
                if nal_start + 1 >= len(frame):
                    break
                nal_type = (frame[nal_start] >> 1) & 0x3F
                nal_types.add(nal_type)
                index = nal_start + 2
                if nal_type <= 31:
                    break
            nal_count += 1
        return parameter_sets.issubset(nal_types) and bool(keyframe_types.intersection(nal_types))

    def _cache_video_frame(
        self,
        *,
        media_ssrc: int,
        stream: VoiceStream,
        frame: bytes,
        frame_time_ms: float,
    ) -> None:
        if self._video_codec is None or self._video_keyframe_replay_active:
            return

        if self._is_config_keyframe(self._video_codec, frame):
            self._video_keyframes[media_ssrc] = [(frame, frame_time_ms, stream)]
            return

        frames = self._video_keyframes.get(media_ssrc)
        if frames is None:
            return

        max_frames = max(1, self.video_keyframe_replay_max_frames)
        if len(frames) >= max_frames:
            self._video_keyframes.pop(media_ssrc, None)
            return

        frames.append((frame, frame_time_ms, stream))

    def _send_requested_video_keyframes(self) -> bool:
        requests = tuple(self._video_keyframe_resend_requests)
        self._video_keyframe_resend_requests.clear()
        sent = False
        for media_ssrc in requests:
            frames = self._video_keyframes.get(media_ssrc)
            if not frames:
                continue
            replay_frames = tuple(frames)
            try:
                self._video_keyframe_replay_active = True
                for frame, frame_time_ms, stream in replay_frames:
                    self.send_video_frame(frame, frame_time_ms=frame_time_ms, stream=stream)
            except Exception:
                log.debug('Failed to resend cached video keyframe for SSRC %s.', media_ssrc, exc_info=True)
            else:
                sent = True
            finally:
                self._video_keyframe_replay_active = False
        return sent

    def _send_rtcp_sender_report(
        self,
        *,
        ssrc: int,
        rtp_timestamp: int,
        packet_count: int | None = None,
        octet_count: int | None = None,
    ) -> None:
        if packet_count is None:
            packet_count = self._video_packet_counts.get(ssrc, 0)
        if octet_count is None:
            octet_count = self._video_octet_counts.get(ssrc, 0)
        ntp = time.time() + 2_208_988_800
        ntp_seconds = int(ntp)
        ntp_fraction = int((ntp - ntp_seconds) * (1 << 32))
        header = struct.pack(
            '>BBHI',
            0x80,
            RTCP_SENDER_REPORT,
            6,
            ssrc,
        )
        body = struct.pack(
            '>IIIII',
            ntp_seconds,
            ntp_fraction,
            rtp_timestamp,
            packet_count & 0xFFFFFFFF,
            octet_count & 0xFFFFFFFF,
        )
        self._send_udp_packet(self._get_crypto().encrypt_rtcp(header, body), 'RTCP sender report')

    def _rtcp_receiver_report_block(self, ssrc: int, state: _RTCPReceiveReportState) -> bytes:
        expected = state.max_extended_sequence - state.base_extended_sequence + 1
        lost = expected - state.packets_received
        expected_interval = expected - state.expected_prior
        received_interval = state.packets_received - state.received_prior
        lost_interval = expected_interval - received_interval
        fraction_lost = 0
        if expected_interval > 0 and lost_interval > 0:
            fraction_lost = min(255, (lost_interval << 8) // expected_interval)

        state.expected_prior = expected
        state.received_prior = state.packets_received

        cumulative_lost = max(-(1 << 23), min((1 << 23) - 1, lost))
        if cumulative_lost < 0:
            cumulative_lost += 1 << 24
        loss = ((fraction_lost & 0xFF) << 24) | (cumulative_lost & 0xFFFFFF)

        lsr = 0
        delay_since_last_sender_report = 0
        sender_report = self._rtcp_sender_report_lsr.get(ssrc)
        if sender_report is not None:
            lsr, sender_report_received_at = sender_report
            delay_since_last_sender_report = max(
                0,
                min(0xFFFFFFFF, round((time.perf_counter() - sender_report_received_at) * 65536)),
            )

        return struct.pack(
            '>IIIIII',
            ssrc & 0xFFFFFFFF,
            loss,
            state.max_extended_sequence & 0xFFFFFFFF,
            round(state.jitter) & 0xFFFFFFFF,
            lsr & 0xFFFFFFFF,
            delay_since_last_sender_report,
        )

    def _send_rtcp_receiver_report(self, ssrc: int, reports: Sequence[bytes] = ()) -> None:
        reports = reports[:31]
        header = struct.pack(
            '>BBHI',
            0x80 | len(reports),
            RTCP_RECEIVER_REPORT,
            1 + (len(reports) * 6),
            ssrc & 0xFFFFFFFF,
        )
        self._send_udp_packet(self._get_crypto().encrypt_rtcp(header, b''.join(reports)), 'RTCP receiver report')

    def _maybe_send_video_rtcp_receiver_report(
        self,
        *,
        ssrc: int,
        sequence: int,
        timestamp: int,
        received_at: float,
    ) -> None:
        sequence &= 0xFFFF
        state = self._rtcp_receive_report_states.get(ssrc)
        if state is None:
            state = _RTCPReceiveReportState(
                base_extended_sequence=sequence,
                max_extended_sequence=sequence,
                packets_received=1,
            )
            self._rtcp_receive_report_states[ssrc] = state
        else:
            extended_sequence = _unwrap_sequence(sequence, state.max_extended_sequence)
            if extended_sequence > state.max_extended_sequence:
                state.max_extended_sequence = extended_sequence
            state.packets_received += 1

        arrival = round(received_at * 90_000)
        transit = arrival - timestamp
        previous_transit = state.transit
        state.transit = transit
        if previous_transit is not None:
            delta = abs(transit - previous_transit)
            state.jitter += (delta - state.jitter) / 16

        interval = self.rtcp_receiver_report_interval
        if interval <= 0.0 or received_at - state.last_report_at < interval:
            return

        state.last_report_at = received_at
        self._record_video_receive_stat('rtcp_receiver_reports_sent')
        self._send_rtcp_receiver_report(self.ssrc, (self._rtcp_receiver_report_block(ssrc, state),))

    def _send_rtcp_nack(self, *, media_ssrc: int, sequences: Sequence[int]) -> None:
        pending_sequences = list(dict.fromkeys(sequence & 0xFFFF for sequence in sequences))
        blocks: list[tuple[int, int]] = []
        while pending_sequences:
            pid = pending_sequences.pop(0)
            blp = 0
            remaining: list[int] = []
            for sequence in pending_sequences:
                distance = (sequence - pid) & 0xFFFF
                if 1 <= distance <= 16:
                    blp |= 1 << (distance - 1)
                else:
                    remaining.append(sequence)
            blocks.append((pid, blp))
            pending_sequences = remaining

        if not blocks:
            return

        sender_ssrc = self._connection.ssrc
        header = struct.pack(
            '>BBHI',
            0x80 | RTCP_GENERIC_NACK,
            RTCP_RTP_FEEDBACK,
            2 + len(blocks),
            sender_ssrc,
        )
        body = bytearray(struct.pack('>I', media_ssrc & 0xFFFFFFFF))
        for pid, blp in blocks:
            body.extend(struct.pack('>HH', pid, blp))
        self._send_udp_packet(self._get_crypto().encrypt_rtcp(header, bytes(body)), 'RTCP NACK')

    def _add_pending_video_nacks(self, *, media_ssrc: int, sequences: Iterable[int]) -> list[int]:
        unique_sequences = tuple(dict.fromkeys(sequence & 0xFFFF for sequence in sequences))
        if not unique_sequences:
            return []

        pending = self._video_receive_pending_nacks.get(media_ssrc)
        if pending is None:
            pending = {}
            self._video_receive_pending_nacks[media_ssrc] = pending

        added = [sequence for sequence in unique_sequences if sequence not in pending]
        if not added:
            return []

        if len(pending) + len(added) > VIDEO_NACK_MAX_OUTSTANDING:
            self._record_video_receive_stat('video_nack_overflows')
            pending.clear()
            self._video_receive_pending_nacks.pop(media_ssrc, None)
            try:
                self.send_video_pli(media_ssrc)
            except Exception:
                log.debug('Failed to send RTCP PLI after NACK list overflow for SSRC %s.', media_ssrc, exc_info=True)
            log.debug(
                'Cleared pending RTCP NACK list for SSRC %s after reaching %s entries and requested a keyframe.',
                media_ssrc,
                VIDEO_NACK_MAX_OUTSTANDING,
            )
            return []

        for sequence in added:
            pending[sequence] = _PendingVideoNack()
        return added

    def _send_pending_video_nacks(self, *, media_ssrc: int, sequences: Sequence[int], now: float) -> None:
        pending = self._video_receive_pending_nacks.get(media_ssrc)
        if pending is None:
            return

        seen: set[int] = set()
        sendable: list[int] = []
        for sequence in sequences:
            normalized = sequence & 0xFFFF
            if normalized in seen or normalized not in pending:
                continue
            seen.add(normalized)
            sendable.append(normalized)
        for offset in range(0, len(sendable), RTCP_NACK_MAX_SEQUENCES):
            chunk = sendable[offset : offset + RTCP_NACK_MAX_SEQUENCES]
            if not chunk:
                continue
            self._record_video_receive_stat('video_nack_packets_sent')
            self._record_video_receive_stat('video_nack_sequences_sent', len(chunk))
            self._send_rtcp_nack(media_ssrc=media_ssrc, sequences=chunk)
            for sequence in chunk:
                nack = pending.get(sequence)
                if nack is not None:
                    nack.sent_at = now
                    nack.retries += 1

    def _retry_pending_video_nacks(self, *, media_ssrc: int, now: float) -> None:
        pending = self._video_receive_pending_nacks.get(media_ssrc)
        if pending is None:
            return

        expired: list[int] = []
        due: list[int] = []
        for sequence, nack in list(pending.items()):
            if nack.retries >= VIDEO_NACK_MAX_RETRIES:
                expired.append(sequence)
            elif now - nack.sent_at >= VIDEO_NACK_RETRY_INTERVAL:
                due.append(sequence)

        for sequence in expired:
            pending.pop(sequence, None)
        if expired:
            self._record_video_receive_stat('video_nack_sequences_expired', len(expired))
            log.debug('Removed %s video packet(s) from RTCP NACK list after max retries.', len(expired))
        if due:
            self._send_pending_video_nacks(media_ssrc=media_ssrc, sequences=due, now=now)
        if not pending:
            self._video_receive_pending_nacks.pop(media_ssrc, None)

    def _track_video_receive_sequence(self, ssrc: int, sequence: int, *, repaired: bool = False) -> None:
        sequence &= 0xFFFF
        now = time.perf_counter()
        pending = self._video_receive_pending_nacks.get(ssrc)
        if pending is not None:
            pending.pop(sequence, None)
            if not pending:
                self._video_receive_pending_nacks.pop(ssrc, None)

        if repaired:
            self._record_video_receive_stat('video_sequence_repaired_packets')
            if self.rtx_enabled:
                self._retry_pending_video_nacks(media_ssrc=ssrc, now=now)
            return

        previous = self._video_receive_last_sequences.get(ssrc)
        if previous is None:
            self._video_receive_last_sequences[ssrc] = sequence
            if self.rtx_enabled:
                self._retry_pending_video_nacks(media_ssrc=ssrc, now=now)
            return

        delta = _sequence_delta(sequence, previous)
        if delta == 0 or delta > 0x7FFF:
            if delta == 0:
                self._record_video_receive_stat('video_sequence_duplicates')
            else:
                self._record_video_receive_stat('video_sequence_old_or_reordered')
            if self.rtx_enabled:
                self._retry_pending_video_nacks(media_ssrc=ssrc, now=now)
            return

        if sequence < previous:
            self._record_video_receive_stat('video_sequence_wraps')
        if delta > 1:
            self._record_video_receive_stat('video_sequence_gaps')
            self._record_video_receive_max_stat('video_sequence_gap_max', delta)
        self._video_receive_last_sequences[ssrc] = sequence
        if not self.rtx_enabled:
            return
        if delta == 1:
            self._retry_pending_video_nacks(media_ssrc=ssrc, now=now)
            return

        missing_count = delta - 1
        missing = [((previous + offset) & 0xFFFF) for offset in range(1, missing_count + 1)]
        missing = self._add_pending_video_nacks(media_ssrc=ssrc, sequences=missing)
        if missing:
            self._record_video_receive_stat('video_nack_sequences_added', len(missing))
            self._send_pending_video_nacks(media_ssrc=ssrc, sequences=missing, now=now)
            log.debug('sent RTCP NACK for %s missing video packet(s) on SSRC %s.', len(missing), ssrc)
        self._retry_pending_video_nacks(media_ssrc=ssrc, now=now)

    def _resolve_video_send_stream(self, stream: VoiceStream | None) -> VoiceStream:
        if stream is None:
            if not self._active_video_streams:
                raise discord.ClientException('No active video stream is selected')
            return self._active_video_streams[0]

        if not isinstance(stream, VoiceStream):
            raise TypeError(f'stream must be VoiceStream not {stream.__class__.__name__}')

        target_ssrc = stream.ssrc
        target_rid = stream.rid
        for active in self._active_video_streams:
            if active.ssrc == target_ssrc:
                return active
            if active.rid == target_rid:
                return active
        raise discord.ClientException('Video stream is not active')

    def _prepare_video_frame(
        self,
        *,
        codec: str,
        media_ssrc: int,
        stream: VoiceStream,
        frame: bytes,
        frame_time_ms: float,
    ) -> tuple[bytes, bytes, float]:
        codec_upper = codec.upper()
        original_frame = frame if isinstance(frame, bytes) else bytes(frame)
        if codec_upper == 'H264':
            original_frame = strip_h264_filler_nalus(original_frame)

        self._cache_video_frame(
            media_ssrc=media_ssrc,
            stream=stream,
            frame=original_frame,
            frame_time_ms=frame_time_ms,
        )
        encrypted_frame = self._apply_dave('video', codec, original_frame)
        return original_frame, encrypted_frame, max(0.001, frame_time_ms)

    def _maybe_send_video_rtcp_sender_report(self, *, media_ssrc: int, rtp_timestamp: int) -> None:
        media_time_ms = self._video_media_times_ms.get(media_ssrc, 0.0)
        last_rtcp_media_time_ms = self._video_last_rtcp_media_times_ms.get(media_ssrc, -1.0)
        if media_time_ms // 1000 <= last_rtcp_media_time_ms // 1000:
            return

        self._send_rtcp_sender_report(ssrc=media_ssrc, rtp_timestamp=rtp_timestamp)
        self._video_last_rtcp_media_times_ms[media_ssrc] = media_time_ms

    def send_video_frame(self, frame: bytes, *, frame_time_ms: float = 33.0, stream: VoiceStream | None = None) -> int:
        """Packetize, encrypt, and send one encoded video frame.

        Parameters
        ----------
        frame: :class:`bytes`
            The encoded frame in the negotiated codec.
        frame_time_ms: :class:`float`
            The frame duration in milliseconds.
        stream: Optional[:class:`discord.VoiceStream`]
            The active simulcast stream to send on. Defaults to the selected
            primary stream.

        Returns
        -------
        :class:`int`
            The number of RTP packets sent.

        Raises
        ------
        ClientException
            The voice client is not connected, video has not been started, no
            active stream is selected, the stream is inactive, or the stream has
            no negotiated SSRC.
        """
        if not self.is_connected():
            raise discord.ClientException('Not connected to voice')
        codec = self._video_codec
        if codec is None:
            raise discord.ClientException('Video has not been started')

        stream = self._resolve_video_send_stream(stream)
        media_ssrc = self._require_video_stream_ssrc(stream)
        original_frame, frame, frame_time_ms = self._prepare_video_frame(
            codec=codec,
            media_ssrc=media_ssrc,
            stream=stream,
            frame=frame,
            frame_time_ms=frame_time_ms,
        )

        media_time_ms = self._video_media_times_ms.get(media_ssrc, 0.0)
        sent = self._send_video_frame_raw(
            codec=codec,
            media_ssrc=media_ssrc,
            original_frame=original_frame,
            frame=frame,
            frame_time_ms=frame_time_ms,
        )
        self._video_media_times_ms[media_ssrc] = media_time_ms + frame_time_ms
        return sent

    def _send_video_frame_raw(
        self,
        *,
        codec: str,
        media_ssrc: int,
        original_frame: bytes,
        frame: bytes,
        frame_time_ms: float,
    ) -> int:
        metadata_frame = original_frame if codec.upper() == 'VP9' else None
        sent, sent_octets, transport_sequence, rtp_timestamp, last_sequence = self._video_send_pipeline.send_video_frame(
            self._get_crypto(),
            self._connection.socket.fileno(),
            media_ssrc,
            frame,
            metadata_frame,
            frame_time_ms,
            self.media_stream_type,
            self.video_packet_burst_size,
            self.video_packet_burst_interval,
        )
        self._video_transport_sequence = transport_sequence
        if sent:
            self._record_video_packet_send(ssrc=media_ssrc, packets=sent, octets=sent_octets)
            self._maybe_send_video_rtcp_sender_report(media_ssrc=media_ssrc, rtp_timestamp=rtp_timestamp)
            if last_sequence is not None:
                self._record_rtp_send_stats(
                    ssrc=media_ssrc,
                    sequence=last_sequence,
                    transport_sequence=(transport_sequence - 1) & 0xFFFF,
                )
        return sent

    def send_video_frames(self, frames: Mapping[str, VideoFrame], /) -> int:
        """Send RID-keyed encoded frames for active simulcast streams.

        Parameters
        ----------
        frames: Dict[:class:`str`, :class:`VideoFrame`]
            Mapping of stream RID to encoded frame.

        Returns
        -------
        :class:`int`
            The total number of RTP packets sent.

        Raises
        ------
        ClientException
            The voice client is not connected, video has not been started, an
            active stream has no negotiated SSRC, or no active stream is selected.
        """
        streams_by_rid = self._active_video_streams_by_rid
        sent = 0
        for rid, frame in frames.items():
            stream = streams_by_rid.get(str(rid))
            if stream is None:
                continue
            sent += self.send_video_frame(frame.data, frame_time_ms=max(0.001, frame.frame_time_ms), stream=stream)
        return sent

    def listen(
        self,
        sink: MediaSink | Callable[[MediaPacket], Any],
        *,
        after: Callable[[Exception | None], Any] | None = None,
    ) -> None:
        """Listen for inbound native media packets.

        Parameters
        ----------
        sink: Union[:class:`MediaSink`, Callable[[:class:`MediaPacket`], Any]]
            The sink or callback that receives decoded media packets.
        after: Optional[Callable[[Optional[:class:`Exception`]], Any]]
            A callback called after listening stops.

        Raises
        ------
        ClientException
            The voice client is not connected, is already listening, or ``sink``
            is already registered as a child or closed.
        TypeError
            ``sink`` is not a :class:`MediaSink` or callable, or ``after`` is not callable.
        """
        if not self.is_connected():
            raise discord.ClientException('Not connected to voice')
        if not isinstance(sink, MediaSink) and not callable(sink):
            raise TypeError('sink must be a MediaSink or callable')
        if after is not None and not callable(after):
            raise TypeError('after must be callable')
        if self.is_listening():
            raise discord.ClientException('Already listening to media')

        if isinstance(sink, MediaSink):
            self._ensure_listenable_sink(sink)

            sink.root._voice_client = self

            self._media_sink = sink
            self._media_callback = self._dispatch_media_sink_packet
        else:
            self._media_callback = sink

        self._media_after = after

        self._socket_listener = self._handle_socket_packets
        self._connection.add_rtp_listener(self._socket_listener, batch=True)
        self._ensure_rtcp_feedback_listener()
        self._start_video_nack_retry_thread()

    @property
    def sink(self) -> MediaSink | None:
        """:class:`MediaSink`: The current media receive sink, if one was provided to :meth:`listen`.

        This property can also be used to change the active sink while receiving.
        The old sink is detached but not cleaned up.
        """
        return self._media_sink

    @sink.setter
    def sink(self, sink: MediaSink) -> None:
        """Set the active receive sink."""
        self.set_sink(sink)

    def set_sink(self, sink: MediaSink, /) -> MediaSink | None:
        """Changes the active receive sink and returns the previous sink.

        The old sink is detached without running :meth:`MediaSink.cleanup`, so
        callers that keep it should clean it up explicitly when they are done.

        Parameters
        ----------
        sink: :class:`MediaSink`
            The sink to use.

        Returns
        -------
        Optional[:class:`MediaSink`]
            The previous active sink, if any.

        Raises
        ------
        ValueError
            The voice client is not currently listening.
        ClientException
            ``sink`` is already registered as a child or closed.
        """
        if not isinstance(sink, MediaSink):
            raise TypeError(f'expected MediaSink not {sink.__class__.__name__}.')
        if not self.is_listening() or self._media_callback is None:
            raise ValueError('Not listening to anything.')
        if sink is self._media_sink:
            return sink
        self._ensure_listenable_sink(sink)

        old_sink = self._media_sink
        if old_sink is not None:
            old_sink.root._voice_client = None
        sink.root._voice_client = self
        self._media_sink = sink
        self._media_callback = self._dispatch_media_sink_packet
        return old_sink

    def is_listening(self) -> bool:
        """:class:`bool`: Whether this client is currently receiving media packets."""
        return self._socket_listener is not None or self._media_callback is not None or self._media_sink is not None

    def _dispatch_media_sink_packet(self, packet: MediaPacket) -> Any:
        sink = self._media_sink
        if sink is not None and sink.wants_media(packet.media_type, packet.codec):
            return sink.write(packet)
        return None

    def stop_listening(self) -> None:
        """Stop receiving media packets and clean up the active sink."""
        self._stop_listening(None)

    def _reset_receive_transport_state(self) -> None:
        self._stop_video_nack_retry_thread()
        self._record_video_reorder_stats()
        self._video_depacketizers.clear()
        self._video_receive_last_sequences.clear()
        self._video_receive_pending_nacks.clear()
        self._video_receive_reorder_buffers.clear()
        self._video_receive_startup_plis.clear()
        self._video_frame_rtp_packets.clear()
        self._pending_dave_video_frames.clear()

    def _stop_listening(self, error: Exception | None) -> None:
        if not self.is_listening():
            return

        listener = self._socket_listener
        if listener is not None:
            self._connection.remove_socket_listener(listener)
        self._socket_listener = None
        self._media_callback = None
        after = self._media_after
        self._media_after = None
        sink = self._media_sink
        self._media_sink = None
        self._reset_receive_transport_state()
        self._stop_rtcp_listener_if_idle()

        cleanup_error: Exception | None = None
        if sink is not None:
            root = sink.root
            try:
                root.cleanup()
            except Exception as exc:
                cleanup_error = exc
                if error is not None:
                    cleanup_error.__context__ = error
                    log.exception('Failed to clean up media sink %s.', root, exc_info=cleanup_error)
            finally:
                root._voice_client = None

        reported_error = error if error is not None else cleanup_error
        if after is not None:
            try:
                after(reported_error)
            except Exception as exc:
                exc.__context__ = reported_error
                log.exception('Calling the media after function failed.', exc_info=exc)
        elif reported_error is not None:
            log.exception('Exception in media receive lifecycle.', exc_info=reported_error)

    def _handle_socket_packets(self, batch: Sequence[bytes]) -> None:
        received_at = time.perf_counter()

        callback = self._media_callback
        if callback is None:
            return

        try:
            packets = self._decode_media_packets(batch, received_at=received_at)
        except Exception:
            self._log_packet_decode_error('Failed to decode media packet batch.')
            return

        for packet in packets:
            if not self._dispatch_media_callback_packet(callback, packet):
                return

    def _decode_media_packets(self, batch: Sequence[bytes], *, received_at: float) -> list[MediaPacket]:
        pending_packets = self._retry_pending_dave_video_frames()
        packets: list[MediaPacket] = list(pending_packets)
        for raw, decoded in self._decode_rtp_packets(batch):
            decoded_packet = self._decode_decoded_media_packet(decoded, raw=raw, received_at=received_at)
            if decoded_packet is None:
                continue
            if isinstance(decoded_packet, MediaPacket):
                packets.append(decoded_packet)
            else:
                packets.extend(decoded_packet)
        return packets

    def _dispatch_media_callback_packet(self, callback: Callable[[MediaPacket], Any], packet: MediaPacket) -> bool:
        try:
            self._handle_media_callback_result(callback(packet))
        except Exception as exc:
            self._stop_listening(exc)
            return False
        return True

    def _handle_media_callback_result(self, result: Any) -> None:
        if result is None:
            return
        if inspect.isawaitable(result):

            async def await_result() -> Any:
                return await result

            future = asyncio.run_coroutine_threadsafe(await_result(), self.loop)

            def handle_media_callback_future(future: concurrent.futures.Future[Any]) -> None:
                try:
                    future.result()
                except Exception as exc:
                    self._stop_listening(exc)

            future.add_done_callback(handle_media_callback_future)
            return
        if isinstance(result, Iterable) and not isinstance(result, (str, bytes, bytearray, memoryview)):
            for item in result:
                self._handle_media_callback_result(item)

    def _decode_decoded_media_packet(
        self,
        decoded: _DecodedRTPPacket,
        *,
        raw: bytes,
        received_at: float,
    ) -> MediaPacket | list[MediaPacket] | None:
        payload_type = decoded.payload_type
        ssrc = decoded.ssrc
        rtx_ssrc: int | None = None
        rtx_payload_type: int | None = None
        lookups = self._get_codec_lookups()
        rtx_codec = lookups['rtx_payload_types'].get(payload_type) if self.rtx_enabled else None
        if rtx_codec is not None:
            self._record_video_receive_stat('rtx_packets_received')
            media_type, codec, associated_payload_type = rtx_codec
            rtx_payload = decoded.payload
            if len(rtx_payload) < 2:
                self._record_video_receive_stat('rtx_packets_too_short')
                return None
            rtx_ssrc = ssrc
            rtx_payload_type = payload_type
            media_ssrc = self._media_ssrc_for_rtx_ssrc(ssrc)
            if media_ssrc is not None:
                ssrc = media_ssrc
            decoded = decoded.replace(
                payload_type=associated_payload_type,
                sequence=int.from_bytes(rtx_payload[:2], 'big'),
                payload=rtx_payload[2:],
                ssrc=ssrc,
            )
        else:
            codec_info = lookups['payload_types'].get(payload_type)
            if codec_info is None:
                log.debug('Ignoring unknown RTP payload type %s from SSRC %s.', payload_type, ssrc)
                return None
            media_type, codec = codec_info
            if media_type == 'video':
                self._record_video_receive_stat('primary_packets_received')
        user_id = self._connection.ssrc_user_ids.get(ssrc)
        rtp_packet = self._rtp_packet_from_decoded(
            decoded,
            media_type=media_type,
            codec=codec,
            ssrc=ssrc,
            user_id=user_id,
            raw=raw,
            rtx=rtx_codec is not None,
            rtx_ssrc=rtx_ssrc,
            rtx_payload_type=rtx_payload_type,
        )
        if media_type == 'video':
            self._record_video_receive_stat('video_packets_received')
            self._maybe_send_startup_video_pli(codec=codec, ssrc=ssrc)
            if rtx_codec is None:
                self._maybe_send_video_rtcp_receiver_report(
                    ssrc=ssrc,
                    sequence=decoded.sequence,
                    timestamp=decoded.timestamp,
                    received_at=received_at,
                )
            self._track_video_receive_sequence(ssrc, decoded.sequence, repaired=rtx_codec is not None)
            video_packets = self._decode_video_packets(
                decoded,
                codec=codec,
                ssrc=ssrc,
                user_id=user_id,
                raw=raw,
                rtp_packet=rtp_packet,
                received_at=received_at,
            )
            return video_packets

        try:
            payload = decoded.payload
            audio_silence = media_type == 'audio' and self._is_unencrypted_opus_silence_payload(payload)
            dave_session = None if audio_silence else self._connection.dave_session

            if dave_session:
                looks_like_dave = media_type == 'audio' and _looks_like_dave_protocol_frame(payload)
                if user_id is None:
                    if looks_like_dave:
                        return None
                elif self._can_decrypt_dave(user_id):
                    payload = dave_session.decrypt(user_id, _dave_media_type(media_type), payload)
                elif looks_like_dave:
                    return None
        except Exception:
            self._log_packet_decode_error('Failed to decrypt %s packet from SSRC %s.', media_type, ssrc)
            return None
        extension_payload = decoded.extension_payload
        rtp_extensions = decoded.rtp_extensions
        speaking_flags = None
        audio_level = None
        audio_voice_activity = None
        if media_type == 'audio':
            speaking_flags = (
                SpeakingFlags.none() if audio_silence else self._audio_speaking_flags_from_extensions(rtp_extensions)
            )
            audio_level = rtp_packet.audio_level
            audio_voice_activity = rtp_packet.audio_voice_activity
        media_packet = MediaPacket(
            media_type=media_type,
            codec=codec,
            payload=payload,
            payload_type=payload_type,
            marker=decoded.marker,
            sequence=decoded.sequence,
            timestamp=decoded.timestamp,
            ssrc=ssrc,
            user_id=user_id,
            raw=raw,
            extension_payload=extension_payload,
            rtp_extended=decoded.extended,
            rtp_extensions=rtp_extensions,
            rtp_packets=(rtp_packet,),
            received_at=received_at,
            rtcp_time=self._rtcp_time_for_packet(ssrc, decoded.timestamp, rtp_extensions),
            speaking_flags=speaking_flags,
            audio_level=audio_level,
            audio_voice_activity=audio_voice_activity,
        )
        return media_packet

    def _rtp_packet_from_decoded(
        self,
        decoded: _DecodedRTPPacket,
        *,
        media_type: str,
        codec: str,
        ssrc: int,
        user_id: int | None,
        raw: bytes,
        rtx: bool = False,
        rtx_ssrc: int | None = None,
        rtx_payload_type: int | None = None,
    ) -> RTPPacket:
        extension_payload = decoded.extension_payload
        rtp_extensions = decoded.rtp_extensions
        audio_level, audio_voice_activity = (
            self._audio_level_from_extensions(rtp_extensions) if media_type == 'audio' else (None, None)
        )
        return RTPPacket(
            media_type=media_type,
            codec=codec,
            payload=decoded.payload,
            payload_type=decoded.payload_type,
            marker=decoded.marker,
            sequence=decoded.sequence,
            timestamp=decoded.timestamp,
            ssrc=ssrc,
            user_id=user_id,
            raw=raw,
            extension_payload=extension_payload,
            rtp_extended=decoded.extended,
            rtp_extensions=rtp_extensions,
            rtx=rtx,
            rtx_ssrc=rtx_ssrc,
            rtx_payload_type=rtx_payload_type,
            audio_level=audio_level,
            audio_voice_activity=audio_voice_activity,
        )

    def _decode_video_packets(
        self,
        decoded: _DecodedRTPPacket,
        *,
        codec: str,
        ssrc: int,
        user_id: int | None,
        raw: bytes,
        rtp_packet: RTPPacket,
        received_at: float,
    ) -> list[MediaPacket]:
        received = _ReceivedVideoPacket(decoded, codec, ssrc, user_id, raw, rtp_packet, received_at)
        if self.rtx_enabled:
            key = (ssrc, decoded.payload_type)
            reorder_buffer = self._video_receive_reorder_buffers.get(key)
            if reorder_buffer is None:
                reorder_buffer = _VideoReceiveReorderBuffer(
                    max_packets=max(1, self.video_receive_reorder_max_packets),
                    max_delay=self.video_receive_reorder_max_delay,
                    debug_stats=self.enable_debug_stats,
                )
                self._video_receive_reorder_buffers[key] = reorder_buffer
            ready = reorder_buffer.push(received)
        else:
            ready = [received]

        packets: list[MediaPacket] = []
        for packet in ready:
            self._record_video_receive_stat('video_packets_reorder_ready')
            decoded_packet = self._decode_video_frame(
                packet.decoded,
                codec=packet.codec,
                ssrc=packet.ssrc,
                user_id=packet.user_id,
                raw=packet.raw,
                rtp_packet=packet.rtp_packet,
                received_at=packet.received_at,
            )
            if decoded_packet is not None:
                self._record_video_receive_stat('video_frames_completed')
                packets.append(decoded_packet)
        return packets

    def _decode_video_frame(
        self,
        decoded: _DecodedRTPPacket,
        *,
        codec: str,
        ssrc: int,
        user_id: int | None,
        raw: bytes,
        rtp_packet: RTPPacket,
        received_at: float,
    ) -> MediaPacket | None:
        payload_type = decoded.payload_type
        key = (ssrc, payload_type)
        depacketizer = self._video_depacketizers.get(key)
        if depacketizer is None:
            depacketizer = self._make_video_depacketizer(codec)
            self._video_depacketizers[key] = depacketizer

        frame_rtp_packets = self._video_frame_rtp_packets.setdefault(key, [])
        frame_rtp_packets.append(rtp_packet)
        frame = depacketizer.push_packet(
            decoded.payload,
            decoded.marker,
            decoded.sequence,
            decoded.timestamp,
        )
        if frame is None:
            if decoded.marker:
                self._record_video_receive_stat('video_frames_dropped_at_marker')
                self._video_frame_rtp_packets[key] = []
            else:
                self._record_video_receive_stat('video_frame_packets_buffered')
            return None
        rtp_packets = tuple(frame_rtp_packets)
        self._video_frame_rtp_packets[key] = []

        frame_payload = bytes(frame)
        looks_like_dave = _looks_like_dave_protocol_frame(frame_payload)
        if looks_like_dave and not self._can_attempt_video_dave_decrypt(user_id):
            self._stash_dave_video_frame(
                decoded=decoded,
                codec=codec,
                ssrc=ssrc,
                user_id=user_id,
                raw=raw,
                payload=frame_payload,
                rtp_packets=rtp_packets,
                received_at=received_at,
            )
            return None

        try:
            payload = self._decrypt_video_frame(user_id, frame_payload)
        except Exception as exc:
            if looks_like_dave and _is_retryable_dave_decrypt_error(exc):
                self._stash_dave_video_frame(
                    decoded=decoded,
                    codec=codec,
                    ssrc=ssrc,
                    user_id=user_id,
                    raw=raw,
                    payload=frame_payload,
                    rtp_packets=rtp_packets,
                    received_at=received_at,
                )
                return None
            self._log_packet_decode_error('Failed to decrypt video frame from SSRC %s.', ssrc)
            self._record_video_receive_stat('video_frame_decrypt_failures')
            return None
        return self._make_video_media_packet(
            decoded,
            codec=codec,
            ssrc=ssrc,
            user_id=user_id,
            raw=raw,
            payload=payload,
            rtp_packets=rtp_packets,
            received_at=received_at,
        )

    def _make_video_media_packet(
        self,
        decoded: _DecodedRTPPacket,
        *,
        codec: str,
        ssrc: int,
        user_id: int | None,
        raw: bytes,
        payload: bytes,
        rtp_packets: tuple[RTPPacket, ...],
        received_at: float,
    ) -> MediaPacket:
        payload_type = decoded.payload_type
        extension_payload = decoded.extension_payload
        rtp_extensions = decoded.rtp_extensions
        return MediaPacket(
            media_type='video',
            codec=codec,
            payload=payload,
            payload_type=payload_type,
            marker=True,
            sequence=decoded.sequence,
            timestamp=decoded.timestamp,
            ssrc=ssrc,
            user_id=user_id,
            raw=raw,
            extension_payload=extension_payload,
            rtp_extended=decoded.extended,
            rtp_extensions=rtp_extensions,
            rtp_packets=rtp_packets,
            received_at=received_at,
            rtcp_time=self._rtcp_time_for_packet(ssrc, decoded.timestamp, rtp_extensions),
        )
