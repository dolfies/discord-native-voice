from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Any

__all__ = (
    'RTCP_GENERIC_NACK',
    'RTCP_NACK_MAX_SEQUENCES',
    'RTCP_PAYLOAD_FEEDBACK',
    'RTCP_PICTURE_LOSS_INDICATION',
    'RTCP_PROTECTED_HEADER_LEN',
    'RTCP_RECEIVER_REPORT',
    'RTCP_RTP_FEEDBACK',
    'RTCP_SENDER_REPORT',
    'RTP_EXT_ABSOLUTE_SEND_TIME',
    'RTP_EXT_AUDIO_LEVEL',
    'RTP_EXT_DISCORD_SPEAKING',
    'RTP_EXT_MID',
    'RTP_EXT_PLAYOUT_DELAY',
    'RTP_EXT_REPAIRED_RID',
    'RTP_EXT_RID',
    'RTP_EXT_TIMESTAMP_OFFSET',
    'RTP_EXT_TRANSPORT_SEQUENCE',
    'RTP_EXT_VIDEO_CONTENT_TYPE',
    'RTP_EXT_VIDEO_ROTATION',
    'RTP_EXT_VIDEO_TIMING',
    'VIDEO_NACK_MAX_OUTSTANDING',
    'VIDEO_NACK_MAX_RETRIES',
    'VIDEO_NACK_RETRY_INTERVAL',
    'AudioSendStats',
    'RTCPReceiverReport',
    'RTPExtension',
    'RTPPacket',
    'RTPSendStats',
)


@dataclass(frozen=True, slots=True)
class RTPExtension:
    """Represents a parsed one-byte RTP header extension.

    Attributes
    ----------
    id: :class:`int`
        The RTP extension ID.
    data: :class:`bytes`
        The extension payload bytes.
    """

    id: int
    data: bytes


@dataclass(frozen=True, slots=True)
class RTCPReceiverReport:
    """Represents one RTCP receiver report block.

    Attributes
    ----------
    sender_ssrc: :class:`int`
        The SSRC that sent the receiver report.
    source_ssrc: :class:`int`
        The SSRC that the report describes.
    fraction_lost: :class:`int`
        The packet loss fraction reported by the receiver.
    cumulative_lost: :class:`int`
        The cumulative packet loss count reported by the receiver.
    extended_high_sequence: :class:`int`
        The extended highest sequence number received.
    jitter: :class:`int`
        The interarrival jitter value reported by the receiver.
    last_sender_report: :class:`int`
        Compact NTP timestamp from the last sender report.
    delay_since_last_sender_report: :class:`int`
        Delay since the last sender report in RTCP timestamp units.
    received_at: :class:`float`
        Local monotonic timestamp for when this report was decoded.
    """

    sender_ssrc: int
    source_ssrc: int
    fraction_lost: int
    cumulative_lost: int
    extended_high_sequence: int
    jitter: int
    last_sender_report: int
    delay_since_last_sender_report: int
    received_at: float


@dataclass(frozen=True, slots=True)
class RTPSendStats:
    """Represents the latest RTP send state for an SSRC.

    Attributes
    ----------
    ssrc: :class:`int`
        The RTP SSRC.
    sequence: :class:`int`
        The latest RTP sequence number sent.
    transport_sequence: Optional[:class:`int`]
        The latest RTP transport-wide sequence number sent, if available.
    updated_at: :class:`float`
        Local monotonic timestamp for the latest update.
    """

    ssrc: int
    sequence: int
    transport_sequence: int | None
    updated_at: float


@dataclass(frozen=True, slots=True)
class AudioSendStats:
    """Represents audio RTP send counters.

    Attributes
    ----------
    ssrc: :class:`int`
        The audio RTP SSRC.
    packets_sent: :class:`int`
        Number of audio RTP packets sent.
    octets_sent: :class:`int`
        Number of audio payload octets sent.
    last_sequence: Optional[:class:`int`]
        The latest audio RTP sequence number sent, if available.
    updated_at: Optional[:class:`float`]
        Local monotonic timestamp for the latest send update, if available.
    """

    ssrc: int
    packets_sent: int
    octets_sent: int
    last_sequence: int | None
    updated_at: float | None


@dataclass(frozen=True, slots=True)
class _DecodedRTPPacket:
    payload_type: int
    marker: bool
    padded: bool
    sequence: int
    timestamp: int
    ssrc: int
    extended: bool
    extension_payload: bytes
    rtp_extensions: tuple[RTPExtension, ...]
    payload: bytes

    def replace(self, **changes: Any) -> _DecodedRTPPacket:
        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class RTPPacket:
    """Represents one parsed receive-side RTP packet.

    For RTX packets, ``payload`` is the recovered associated media payload and
    ``sequence`` is the original media sequence number. The transport RTX SSRC
    and payload type are preserved in ``rtx_ssrc`` and ``rtx_payload_type``.

    Attributes
    ----------
    media_type: :class:`str`
        The media type, currently ``audio`` or ``video``.
    codec: :class:`str`
        The decoded codec name.
    payload: :class:`bytes`
        The RTP media payload.
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
    rtx: :class:`bool`
        Whether this packet was received through RTX retransmission.
    rtx_ssrc: Optional[:class:`int`]
        The RTX transport SSRC, if this packet was repaired.
    rtx_payload_type: Optional[:class:`int`]
        The RTX RTP payload type, if this packet was repaired.
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
    rtx: bool = False
    rtx_ssrc: int | None = None
    rtx_payload_type: int | None = None
    audio_level: int | None = None
    audio_voice_activity: bool | None = None

    def replace(self, **changes: Any) -> RTPPacket:
        return replace(self, **changes)


RTCP_RTP_FEEDBACK = 205
RTCP_PAYLOAD_FEEDBACK = 206
RTCP_SENDER_REPORT = 200
RTCP_RECEIVER_REPORT = 201
RTCP_GENERIC_NACK = 1
RTCP_PICTURE_LOSS_INDICATION = 1
RTCP_PROTECTED_HEADER_LEN = 8
RTCP_NACK_MAX_SEQUENCES = 0xFD
RTP_TIMESTAMP_MASK = (1 << 32) - 1
RTP_TIMESTAMP_HALF_RANGE = 1 << 31
VIDEO_NACK_MAX_OUTSTANDING = 1000
VIDEO_NACK_MAX_RETRIES = 100
VIDEO_NACK_RETRY_INTERVAL = 0.1
RTP_EXT_AUDIO_LEVEL = 1
RTP_EXT_TIMESTAMP_OFFSET = 2
RTP_EXT_ABSOLUTE_SEND_TIME = 3
RTP_EXT_VIDEO_ROTATION = 4
RTP_EXT_TRANSPORT_SEQUENCE = 5
RTP_EXT_PLAYOUT_DELAY = 6
RTP_EXT_VIDEO_CONTENT_TYPE = 7
RTP_EXT_VIDEO_TIMING = 8
RTP_EXT_DISCORD_SPEAKING = 9
RTP_EXT_MID = 10
RTP_EXT_RID = 11
RTP_EXT_REPAIRED_RID = 12


def _sequence_delta(sequence: int, previous: int) -> int:
    return (sequence - previous) & 0xFFFF


def _unwrap_sequence(sequence: int, reference: int) -> int:
    delta = (sequence - (reference & 0xFFFF)) & 0xFFFF
    if delta > 0x7FFF:
        delta -= 0x10000
    return reference + delta


def _rtp_timestamp_delta(timestamp: int, base: int) -> int | None:
    delta = (timestamp - base) & RTP_TIMESTAMP_MASK
    return None if delta >= RTP_TIMESTAMP_HALF_RANGE else delta


@dataclass(slots=True)
class _PendingVideoNack:
    sent_at: float = 0.0
    retries: int = 0


@dataclass(frozen=True, slots=True)
class _ReceivedVideoPacket:
    decoded: _DecodedRTPPacket
    codec: str
    ssrc: int
    user_id: int | None
    raw: bytes
    rtp_packet: RTPPacket
    received_at: float

    @property
    def sequence(self) -> int:
        return self.decoded.sequence & 0xFFFF


class _VideoReceiveReorderBuffer:
    def __init__(
        self,
        *,
        max_packets: int = 2048,
        max_delay: float = VIDEO_NACK_RETRY_INTERVAL,
        debug_stats: bool = False,
    ) -> None:
        self.max_packets = max_packets
        self.max_delay = max_delay
        self.debug_stats = debug_stats
        self.expected_sequence: int | None = None
        self._blocked_at: float | None = None
        self._packets: dict[int, _ReceivedVideoPacket] = {}
        self.packets_pushed = 0
        self.duplicate_packets = 0
        self.old_packets = 0
        self.waits = 0
        self.flushes = 0
        self.skipped_packets = 0
        self.ready_packets = 0
        self.max_buffered = 0

    def push(self, packet: _ReceivedVideoPacket) -> list[_ReceivedVideoPacket]:
        now = time.perf_counter()
        sequence = packet.sequence
        if self.debug_stats:
            self.packets_pushed += 1
        expected = self.expected_sequence
        if expected is None:
            expected = sequence
            self.expected_sequence = expected
            unwrapped_sequence = sequence
        else:
            unwrapped_sequence = _unwrap_sequence(sequence, expected)

        if unwrapped_sequence < expected:
            if self.debug_stats:
                self.old_packets += 1
            return []

        if unwrapped_sequence in self._packets:
            if self.debug_stats:
                self.duplicate_packets += 1
        self._packets.setdefault(unwrapped_sequence, packet)
        if self.debug_stats:
            self.max_buffered = max(self.max_buffered, len(self._packets))
        ready = self._pop_ready()
        if ready:
            self._blocked_at = None
            if self.debug_stats:
                self.ready_packets += len(ready)
            return ready

        if self._blocked_at is None:
            self._blocked_at = now

        if len(self._packets) <= self.max_packets and now - self._blocked_at < self.max_delay:
            if self.debug_stats:
                self.waits += 1
            return []

        previous_expected = expected
        self.expected_sequence = min(self._packets)
        if self.debug_stats:
            self.skipped_packets += self.expected_sequence - previous_expected
        self._blocked_at = None
        if self.debug_stats:
            self.flushes += 1
        ready = self._pop_ready()
        if self.debug_stats:
            self.ready_packets += len(ready)
        return ready

    def _pop_ready(self) -> list[_ReceivedVideoPacket]:
        expected = self.expected_sequence
        if expected is None:
            return []

        ready: list[_ReceivedVideoPacket] = []
        while expected in self._packets:
            ready.append(self._packets.pop(expected))
            expected += 1
        self.expected_sequence = expected
        return ready
