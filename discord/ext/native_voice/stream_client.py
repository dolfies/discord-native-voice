from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, TYPE_CHECKING, TypeVar

import discord
from discord.flags import SpeakingFlags
from discord.player import AudioSource
from discord.stream import Stream, StreamProtocol
from discord.utils import MISSING
from discord.voice_media import VoiceCodec, VoiceStream
from discord.voice_state import ConnectionFlowState

from .client import VoiceClient, _ConnectionState, _configured_client_subclass
from .media import MediaSource

if TYPE_CHECKING:
    from discord.types import gateway as gw

__all__ = ('StreamClient',)

log = logging.getLogger(__name__)

SC = TypeVar('SC', bound='StreamClient')

StreamPreviewData = bytes | bytearray | memoryview | None
StreamPreviewProvider = Callable[[], StreamPreviewData | Awaitable[StreamPreviewData]]


class StreamConnectionState(_ConnectionState):
    voice_client: StreamClient

    def __init__(
        self,
        voice_client: StreamClient,
        *,
        rtc_channel_id: int,
        rtc_server_id: int,
        session_id: str,
    ) -> None:
        super().__init__(voice_client, hook=voice_client._voice_websocket_hook)
        self.channel_id: int = rtc_channel_id
        self.server_id: int = rtc_server_id
        self.session_id: str = session_id

    @property
    def dave_group_id(self) -> int:
        # Go Live stream MLS groups use the media-session snowflake, which is
        # currently one less than the stream voice rtc_server_id
        return self.server_id - 1

    async def reinit_dave_session(self) -> None:
        log.debug(
            'Stream DAVE init protocol=%s group_id=%s media_session_id=%s stream_key=%s.',
            self.dave_protocol_version,
            self.dave_group_id,
            self.media_session_id,
            self.voice_client.stream.key,
        )
        await super().reinit_dave_session()

    async def _voice_connect(self, *, self_deaf: bool = False, self_mute: bool = False, self_video: bool = False) -> None:
        if self.token is None or self.endpoint is None:
            self.state = ConnectionFlowState.set_guild_voice_state
            return

        self._recreate_socket(ConnectionFlowState.got_both_voice_updates)

    async def stream_server_update(self, data: gw.StreamServerUpdateEvent) -> None:
        previous_token = self.token
        previous_endpoint = self.endpoint

        self.token = data['token']
        endpoint = data['endpoint']
        if self.token is None or endpoint is None:
            log.warning('Awaiting stream endpoint for %s.', self.voice_client.stream.key)
            return

        self.endpoint = endpoint.removeprefix('wss://')
        if self.state in (ConnectionFlowState.disconnected, ConnectionFlowState.set_guild_voice_state):
            self._recreate_socket(ConnectionFlowState.got_both_voice_updates)

        elif self.state is ConnectionFlowState.connected:
            log.debug('Got STREAM_SERVER_UPDATE, closing old stream socket.')
            await self.ws.close(4014)
            self.state = ConnectionFlowState.got_voice_server_update

        elif self.state is not ConnectionFlowState.disconnected:
            if previous_token == self.token and previous_endpoint == self.endpoint:
                return
            log.debug('Unexpected STREAM_SERVER_UPDATE, attempting to handle...')
            await self.soft_disconnect(with_state=ConnectionFlowState.got_voice_server_update)
            self._recreate_socket(ConnectionFlowState.got_both_voice_updates)

    async def stream_unavailable(self) -> None:
        self.token = None
        self.endpoint = None
        self.endpoint_ip = MISSING
        await self.soft_disconnect(with_state=ConnectionFlowState.disconnected)

    async def _voice_disconnect(self) -> None:
        self.state = ConnectionFlowState.disconnected
        self._expecting_disconnect = True
        self._disconnected.clear()
        if self.voice_client._skip_stream_delete:
            return
        try:
            await self.voice_client.stream.delete()
        except Exception:
            log.debug('Ignoring exception while deleting stream %s.', self.voice_client.stream.key, exc_info=True)


class StreamClient(VoiceClient, StreamProtocol):
    """A native RTC client for a Discord Go Live stream.

    Stream clients are created from :meth:`VoiceClient.create_stream` or
    :meth:`discord.Stream.watch`. By default, stream clients inherit codec and
    RTX policy from their parent :class:`VoiceClient`, but stream protocol
    subclasses can override their own negotiation config.
    """

    _connection: StreamConnectionState
    media_stream_type = 'screen'
    video_simulcast_streams: tuple[VoiceStream, ...] = (VoiceStream.screen(quality=100),)
    inherit_parent_codecs: bool = True
    inherit_parent_rtx: bool = True
    inherit_parent_udp_qos: bool = True
    inherit_parent_debug_stats: bool = True

    @property
    def codecs(self) -> tuple[VoiceCodec, ...]:
        """Tuple[:class:`discord.VoiceCodec`, ...]: Codecs advertised by this stream RTC client."""
        codec_client = self.parent_voice_client if self.inherit_parent_codecs else self
        rtx_client = self.parent_voice_client if self.inherit_parent_rtx else self
        return tuple(codec_client.get_voice_codecs(rtx=rtx_client.rtx_enabled))

    @classmethod
    def with_config(
        cls: type[SC],
        *,
        rtx: bool = MISSING,
        udp_qos: bool = MISSING,
        codecs: Sequence[VoiceCodec] = MISSING,
        video_streams: Sequence[VoiceStream] = MISSING,
        ffmpeg_executable: str = MISSING,
        enable_debug_stats: bool = MISSING,
    ) -> type[SC]:
        """Return a subclass with stream RTC negotiation options preset.

        Omitted codec and RTX options inherit from the parent native voice
        client. Options provided here apply only to the stream RTC transport.

        These options affect voice protocol identification,
        so they must be present on the class passed to ``create_stream`` or similar.

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
            FFmpeg executable used for automatic local codec capability probing
            when this stream client does not inherit parent codecs.
        enable_debug_stats: :class:`bool`
            Whether to collect debug RTP/RTCP receive counters. When omitted,
            the stream RTC client inherits the parent voice client's setting.

        Returns
        -------
        Type[:class:`StreamClient`]
            A configured subclass of this stream client.

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

        attrs: dict[str, Any] = {
            'inherit_parent_rtx': cls.inherit_parent_rtx if rtx is MISSING else False,
            'inherit_parent_udp_qos': cls.inherit_parent_udp_qos if udp_qos is MISSING else False,
            'inherit_parent_debug_stats': cls.inherit_parent_debug_stats if enable_debug_stats is MISSING else False,
            'inherit_parent_codecs': cls.inherit_parent_codecs
            if codecs is MISSING and ffmpeg_executable is MISSING
            else False,
        }
        suffixes: list[str] = []

        if rtx is not MISSING:
            enabled = bool(rtx)
            attrs['rtx_enabled'] = enabled
            suffixes.append('WithRTX' if enabled else 'WithoutRTX')
        if udp_qos is not MISSING:
            enabled = bool(udp_qos)
            attrs['udp_qos_enabled'] = enabled
            suffixes.append('WithQoS' if enabled else 'WithoutQoS')
        if codecs is not MISSING:
            attrs.update(
                voice_codec_specs=cls._normalize_voice_codecs(codecs),
                auto_video_codec_capabilities=False,
                auto_video_codec_priority=False,
            )
            suffixes.append('WithCodecs')
        if video_streams is not MISSING:
            streams = cls._normalize_video_streams(video_streams)
            attrs['video_simulcast_streams'] = streams
            if streams != cls.video_simulcast_streams:
                suffixes.append('WithVideoStreams')
        if ffmpeg_executable is not MISSING:
            executable = str(ffmpeg_executable)
            attrs['ffmpeg_executable'] = executable
            if executable != cls.ffmpeg_executable:
                suffixes.append('WithFFmpeg')
        if enable_debug_stats is not MISSING:
            enabled = bool(enable_debug_stats)
            attrs['enable_debug_stats'] = enabled
            suffixes.append('WithDebugStats' if enabled else 'WithoutDebugStats')

        return _configured_client_subclass(cls, attrs=attrs, suffixes=suffixes)

    def _speaking_flags_for_source(self, _source: MediaSource) -> SpeakingFlags:
        return SpeakingFlags(soundshare=True)

    def _codec_cache_key(self) -> tuple[Any, ...]:
        codec_client = self.parent_voice_client if self.inherit_parent_codecs else self
        rtx_client = self.parent_voice_client if self.inherit_parent_rtx else self
        codec_cls = type(codec_client)
        return (
            codec_cls,
            codec_cls.voice_codec_specs,
            codec_cls.auto_video_codec_capabilities,
            codec_cls.auto_video_codec_priority,
            codec_cls.ffmpeg_executable,
            bool(rtx_client.rtx_enabled),
        )

    def __init__(
        self,
        parent_voice_client: discord.VoiceProtocol,
        stream: Stream,
    ) -> None:
        if not isinstance(parent_voice_client, VoiceClient):
            raise TypeError('StreamClient requires a native VoiceClient parent')

        self.parent_voice_client = parent_voice_client
        if self.inherit_parent_debug_stats:
            self.enable_debug_stats = parent_voice_client.enable_debug_stats
        self._skip_stream_delete: bool = False
        self._stream_connect_timeout: float = 30.0
        self._stream_reconnect: bool = True
        self._reconnect_on_available: bool = False
        self.stream_preview_enabled: bool = False
        self._stream_preview_provider: StreamPreviewProvider | None = None
        self._stream_preview_provider_source: MediaSource | None = None
        self._stream_preview_task: asyncio.Task[None] | None = None
        self._stream_preview_interval: float = 60.0 * 5.0
        self._stream_preview_retry_interval: float = 60.0
        self._stream_preview_start_delay: float = 0.5
        StreamProtocol.__init__(self, parent_voice_client, stream)
        super().__init__(parent_voice_client.client, parent_voice_client.channel)
        if self.inherit_parent_rtx:
            self.rtx_enabled = parent_voice_client.rtx_enabled
        if self.inherit_parent_udp_qos:
            self.udp_qos_enabled = parent_voice_client.udp_qos_enabled

    def create_connection_state(self) -> StreamConnectionState:
        session_id = self.parent_voice_client.session_id
        if session_id is None:
            raise discord.ClientException('Voice session is not ready yet')

        rtc_channel_id = self.stream.rtc_channel_id
        rtc_server_id = self.stream.rtc_server_id
        if rtc_channel_id is None or rtc_server_id is None:
            raise discord.ClientException(f'Stream {self.stream.key} is missing RTC connection metadata')

        return StreamConnectionState(
            self,
            rtc_channel_id=rtc_channel_id,
            rtc_server_id=rtc_server_id,
            session_id=session_id,
        )

    def play(
        self,
        source: AudioSource,
        *,
        preview_provider: StreamPreviewProvider | None = MISSING,
        **kwargs: Any,
    ) -> None:
        """Play media on the stream RTC transport.

        This extends :meth:`~VoiceClient.play` with stream preview provider support.

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
        preview_provider: Optional[Callable[[], Optional[:class:`bytes`]]]
            A callable returning image preview bytes. By default,
            the media source's preview reader is used, if available.

        Raises
        ------
        discord.ClientException
            Already playing media or not connected.
            You do not own this stream.
            A preview was requested without a media source or preview provider.
        TypeError
            Source is not a :class:`AudioSource` or after is not callable.
        discord.opus.OpusNotLoaded
            Source is not opus encoded and opus is not loaded.
        ValueError
            An improper value was passed as an encoder parameter.
        """
        if not self.stream.is_owner():
            raise discord.ClientException('Cannot send media to a stream you do not own')

        preview_source: MediaSource | None = None
        if preview_provider is MISSING:
            if self.stream.is_owner() and isinstance(source, MediaSource):
                preview_source = source
        elif preview_provider is not None:
            if not self.stream.is_owner():
                raise discord.ClientException('Cannot create previews for a stream you do not own')
            if not callable(preview_provider):
                raise TypeError(f'preview_provider must be callable or None, not {preview_provider.__class__.__name__}')

        super().play(
            source,
            **kwargs,
        )

        if preview_source is not None or preview_provider is not None:
            if preview_source is not None:
                self._set_stream_preview_provider_from_source(preview_source)
            self.start_preview_loop(preview_provider)

    async def connect(
        self,
        *,
        reconnect: bool,
        timeout: float,
        self_deaf: bool = False,
        self_mute: bool = False,
        self_video: bool = True,
    ) -> None:
        self._stream_connect_timeout = timeout
        self._stream_reconnect = reconnect
        await self._connect_rtc_transport(
            reconnect=reconnect,
            timeout=timeout,
            self_deaf=self_deaf,
            self_mute=self_mute,
            self_video=self_video,
        )
        self._ensure_stream_preview_task()

    async def disconnect(self, *, force: bool = False) -> None:
        """|coro|

        Disconnect this stream RTC client and clean up stream playback.
        """
        self._prepare_disconnect()
        was_connected = self.is_connected()
        await self._connection.disconnect(force=force, wait=False)
        if not was_connected and not force:
            self.cleanup()

    def _prepare_disconnect(self) -> None:
        self.stop_preview_loop()
        self.stop()
        super()._prepare_disconnect()

    def set_preview_provider(self, provider: StreamPreviewProvider | None, /) -> None:
        """Set the callable used for stream preview uploads.

        Parameters
        ----------
        provider: Optional[Callable[[], Optional[:class:`bytes`]]]
            The preview provider to use, or ``None`` to clear it.
        """

        if provider is not None and not callable(provider):
            raise TypeError(f'provider must be callable or None, not {provider.__class__.__name__}')
        if provider is None:
            self.stop_preview_loop()
        self._stream_preview_provider = provider
        self._stream_preview_provider_source = None

    def _set_stream_preview_provider_from_source(self, source: MediaSource) -> None:
        self._stream_preview_provider = source.read_preview
        self._stream_preview_provider_source = source

    def start_preview_loop(
        self,
        provider: StreamPreviewProvider | None = None,
        /,
        *,
        interval: float = 60.0 * 5.0,
        retry_interval: float = 60.0,
        start_delay: float = 0.5,
    ) -> None:
        """Start periodic stream preview uploads.

        All interval parameters default to Discord client behavior.

        Parameters
        ----------
        provider: Optional[Callable[[], Optional[:class:`bytes`]]]
            The preview provider to use. When omitted, the current provider is reused.
        interval: :class:`float`
            Number of seconds between successful preview uploads.
        retry_interval: :class:`float`
            Number of seconds to wait after a skipped or failed preview upload.
        start_delay: :class:`float`
            Number of seconds to wait before the first preview upload attempt.

        Raises
        ------
        discord.ClientException
            This client does not own the stream or no preview provider is set.
        """
        if not self.stream.is_owner():
            raise discord.ClientException('Cannot create previews for a stream you do not own')
        if provider:
            self.set_preview_provider(provider)
        if self._stream_preview_provider is None:
            raise discord.ClientException('Stream preview provider is not set')

        self._stream_preview_interval = interval
        self._stream_preview_retry_interval = retry_interval
        self._stream_preview_start_delay = start_delay
        self.stream_preview_enabled = True
        self._ensure_stream_preview_task()

    def stop_preview_loop(self) -> None:
        """Stop periodic stream preview uploads."""
        self.stream_preview_enabled = False
        self._cancel_stream_preview_task()

    def _cancel_stream_preview_task(self) -> None:
        task = self._stream_preview_task
        self._stream_preview_task = None
        if task is not None and not task.done():
            task.cancel()

    def _ensure_stream_preview_task(self) -> None:
        if not self.stream_preview_enabled or self._stream_preview_provider is None or not self.is_connected():
            return
        task = self._stream_preview_task
        if task is None or task.done():
            self._stream_preview_task = self.loop.create_task(self._run_stream_preview_loop())

    def _set_default_stream_preview_provider(self, source: MediaSource) -> None:
        if self._stream_preview_provider is None or self._stream_preview_provider_source is not None:
            self._set_stream_preview_provider_from_source(source)
        if self.stream_preview_enabled:
            self._ensure_stream_preview_task()

    def _media_player_source_changed(self, source: MediaSource) -> None:
        super()._media_player_source_changed(source)
        self._set_default_stream_preview_provider(source)

    def _media_player_finished(self, source: MediaSource) -> None:
        if self._stream_preview_provider_source is not source:
            return
        self.stop_preview_loop()
        self._stream_preview_provider = None
        self._stream_preview_provider_source = None

    async def _read_stream_preview(self) -> bytes | None:
        provider = self._stream_preview_provider
        if provider is None:
            return None

        if inspect.iscoroutinefunction(provider):
            maybe_result = provider()
        else:
            maybe_result = await asyncio.to_thread(provider)

        if inspect.isawaitable(maybe_result):
            result = await maybe_result
        else:
            result = maybe_result
        if result is None:
            return None
        if not isinstance(result, (bytes, bytearray, memoryview)):
            raise TypeError(
                f'Stream preview provider returned {result.__class__.__name__}, expected bytes-like object or None'
            )
        return bytes(result)

    async def _run_stream_preview_loop(self) -> None:
        try:
            await asyncio.sleep(self._stream_preview_start_delay)
            while self.stream_preview_enabled and self.is_connected():
                delay = self._stream_preview_retry_interval
                try:
                    if self.stream.paused:
                        await asyncio.sleep(delay)
                        continue
                    image = await self._read_stream_preview()
                    if image:
                        await self.stream.create_preview(image)
                        delay = self._stream_preview_interval
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception('Failed to upload stream preview for %s.', self.stream.key)
                await asyncio.sleep(delay)
        finally:
            if self._stream_preview_task is asyncio.current_task():
                self._stream_preview_task = None

    def _update_stream_state(self, stream: Stream) -> None:
        self.stream = stream
        if stream.rtc_channel_id is not None:
            self._connection.channel_id = stream.rtc_channel_id
        if stream.rtc_server_id is not None:
            self._connection.server_id = stream.rtc_server_id

    async def on_stream_create(self, stream: Stream) -> None:
        self._update_stream_state(stream)

    async def on_stream_available(self, stream: Stream) -> None:
        self._update_stream_state(stream)
        server_update = self.client._connection._stream_server_updates.get(stream.key)
        if server_update is not None:
            await self.on_stream_server_update(server_update)

        if self._reconnect_on_available and not self.is_connected():
            if not self._stream_reconnect:
                self._reconnect_on_available = False
                self.cleanup()
                return
            await self.connect(timeout=self._stream_connect_timeout, reconnect=self._stream_reconnect)
            self._reconnect_on_available = False

    async def on_stream_server_update(self, data: gw.StreamServerUpdateEvent) -> None:
        if self.stream.unavailable:
            return
        await self._connection.stream_server_update(data)

    async def on_stream_update(self, _before: Stream, after: Stream) -> None:
        self._update_stream_state(after)

    async def on_stream_unavailable(self, stream: Stream) -> None:
        self._update_stream_state(stream)
        if not self._stream_reconnect:
            await self._disconnect_from_gateway()
            return

        self.stop()
        self._cancel_stream_preview_task()
        self._stop_rtcp_feedback_listener()
        # Keep receive sinks registered across reconnect, but drop packet state
        # tied to the old stream RTC transport
        self._reset_receive_transport_state()
        self._reconnect_on_available = True
        await self._connection.stream_unavailable()

    async def on_stream_delete(self, _stream: Stream, _reason: discord.StreamDeleteReason) -> None:
        await self._disconnect_from_gateway()

    async def _disconnect_from_gateway(self) -> None:
        if not self.is_connected():
            self.cleanup()
            return

        self._skip_stream_delete = True
        try:
            await self.disconnect(force=True)
        finally:
            self._skip_stream_delete = False

    def cleanup(self) -> None:
        """Clean up preview, playback, receive, feedback, and state registration."""
        self.stop_preview_loop()
        self.stop()
        self._cleanup_media_transport()
        StreamProtocol.cleanup(self)
