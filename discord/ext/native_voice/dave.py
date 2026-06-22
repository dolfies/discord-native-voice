from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .rtp import RTPPacket, _DecodedRTPPacket

try:
    import davey
except ImportError:
    davey = None

DAVE_PENDING_VIDEO_MAX_FRAMES = 128
DAVE_PENDING_VIDEO_MAX_AGE = 10.0
DAVE_TAG_SIZE = 8
DAVE_SIZE_SIZE = 1
DAVE_MARKER = b'\xfa\xfa'
DAVE_MARKER_SIZE = len(DAVE_MARKER)
DAVE_MIN_SIZE = DAVE_TAG_SIZE + DAVE_SIZE_SIZE + DAVE_MARKER_SIZE


def _dave_media_type(media_type: str) -> Any:
    if davey is None:
        return media_type
    return getattr(davey.MediaType, media_type)


def _read_leb128_uint64(buffer: bytes, offset: int, end: int) -> tuple[int, int] | None:
    value = 0
    fill_bits = 0
    while offset != end and fill_bits < 64 - 7:
        byte = buffer[offset]
        value |= (byte & 0x7F) << fill_bits
        offset += 1
        fill_bits += 7
        if byte & 0x80 == 0:
            return value, offset

    if offset != end and buffer[offset] <= 1:
        value |= buffer[offset] << fill_bits
        return value, offset + 1
    return None


def _valid_unencrypted_ranges(buffer: bytes, offset: int, end: int, frame_size: int) -> bool:
    previous_end = 0
    while offset != end:
        offset_result = _read_leb128_uint64(buffer, offset, end)
        if offset_result is None:
            return False
        range_offset, offset = offset_result

        size_result = _read_leb128_uint64(buffer, offset, end)
        if size_result is None:
            return False
        range_size, offset = size_result

        range_end = range_offset + range_size
        if range_offset < previous_end or range_end > frame_size:
            return False
        previous_end = range_end
    return True


def _looks_like_dave_protocol_frame(payload: bytes) -> bool:
    if len(payload) < DAVE_MIN_SIZE:
        return False
    if not payload.endswith(DAVE_MARKER):
        return False

    supplemental_size = payload[-(DAVE_SIZE_SIZE + DAVE_MARKER_SIZE)]
    if supplemental_size < DAVE_MIN_SIZE or supplemental_size > len(payload):
        return False

    supplemental = payload[-supplemental_size:]
    ranges_end = supplemental_size - DAVE_SIZE_SIZE - DAVE_MARKER_SIZE
    if supplemental[ranges_end] != supplemental_size or supplemental[ranges_end + DAVE_SIZE_SIZE :] != DAVE_MARKER:
        return False

    nonce = _read_leb128_uint64(supplemental, DAVE_TAG_SIZE, ranges_end)
    if nonce is None:
        return False
    return _valid_unencrypted_ranges(supplemental, nonce[1], ranges_end, len(payload))


def _is_retryable_dave_decrypt_error(exc: Exception) -> bool:
    message = str(exc)
    return 'NoValidCryptorFound' in message or 'NoDecryptorForUser' in message


@dataclass(frozen=True, slots=True)
class _PendingDaveVideoFrame:
    decoded: _DecodedRTPPacket
    codec: str
    ssrc: int
    user_id: int | None
    raw: bytes
    payload: bytes
    rtp_packets: tuple[RTPPacket, ...]
    received_at: float
    queued_at: float
