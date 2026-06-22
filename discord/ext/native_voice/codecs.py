from __future__ import annotations

import logging
import shutil
from typing import TypedDict

from .transcoding import (
    HARDWARE_DECODERS,
    HARDWARE_ENCODERS,
    VIDEO_CODEC_CAPABILITY_NAMES,
    VIDEO_CODEC_SOFTWARE_PERFORMANCE,
    _ffmpeg_decoder_cache,
    _ffmpeg_encoder_cache,
    _ffmpeg_encoder_is_usable,
    _ffmpeg_encoder_usable_cache,
    _ffmpeg_video_codec_names,
)

__all__ = (
    'VideoCodecCapability',
    'get_local_video_codec_capabilities',
)


log = logging.getLogger(__name__)


class VideoCodecCapability(TypedDict):
    encode: bool
    decode: bool
    encoder: str | None
    decoder: str | None
    encoders: tuple[str, ...]
    decoders: tuple[str, ...]
    hardware_encode: bool
    hardware_decode: bool
    score: int


_local_video_codec_capabilities: dict[str, dict[str, VideoCodecCapability]] = {}


def get_local_video_codec_capabilities(
    *,
    executable: str = 'ffmpeg',
    refresh: bool = False,
) -> dict[str, VideoCodecCapability]:
    """Return local video encode/decode support discovered from FFmpeg.

    Parameters
    ----------
    executable: :class:`str`
        FFmpeg executable used to probe local codec support.
    refresh: :class:`bool`
        Whether to ignore the cached probe result and query FFmpeg again.

    Returns
    -------
    Dict[:class:`str`, :class:`VideoCodecCapability`]
        A mapping of Discord video codec name to local encode/decode support,
        selected FFmpeg encoder/decoder names, hardware flags, and an ordering
        score.
    """

    global _local_video_codec_capabilities
    if refresh:
        _ffmpeg_encoder_cache.clear()
        _ffmpeg_decoder_cache.clear()
        _ffmpeg_encoder_usable_cache.clear()
        _local_video_codec_capabilities.pop(executable, None)

    cached = _local_video_codec_capabilities.get(executable)
    if cached is not None:
        return {codec: capability.copy() for codec, capability in cached.items()}

    ffmpeg = shutil.which(executable) if executable == 'ffmpeg' else executable
    encoders = set(_ffmpeg_video_codec_names(ffmpeg, 'encoders')) if ffmpeg is not None else set()
    decoders = set(_ffmpeg_video_codec_names(ffmpeg, 'decoders')) if ffmpeg is not None else set()
    encoder_probe = _ffmpeg_encoder_is_usable if ffmpeg is not None else None

    capabilities: dict[str, VideoCodecCapability] = {}
    for codec, names in VIDEO_CODEC_CAPABILITY_NAMES.items():
        available_encoders = tuple(name for name in names['encoders'] if name in encoders)
        if encoder_probe is None or ffmpeg is None:
            usable_encoders = ()
        else:
            usable_encoders = tuple(
                name for name in available_encoders if encoder_probe(ffmpeg, codec, name, width=640, height=360)
            )
        available_decoders = tuple(name for name in names['decoders'] if name in decoders)
        hardware_encoder = next((name for name in usable_encoders if name in HARDWARE_ENCODERS), None)
        hardware_decoder = next((name for name in available_decoders if name in HARDWARE_DECODERS), None)
        encoder = hardware_encoder or (usable_encoders[0] if usable_encoders else None)
        decoder = hardware_decoder or (available_decoders[0] if available_decoders else None)
        encode = encoder is not None
        decode = decoder is not None
        score = VIDEO_CODEC_SOFTWARE_PERFORMANCE[codec]
        if encode:
            score += 100_000
        if decode:
            score += 20_000
        if hardware_encoder is not None:
            score += 10_000
        elif encode:
            score += 1_000
        if hardware_decoder is not None:
            score += 5_000
        elif decode:
            score += 500

        capabilities[codec] = {
            'encode': encode,
            'decode': decode,
            'encoder': encoder,
            'decoder': decoder,
            'encoders': usable_encoders,
            'decoders': available_decoders,
            'hardware_encode': hardware_encoder is not None,
            'hardware_decode': hardware_decoder is not None,
            'score': score,
        }

    _local_video_codec_capabilities[executable] = capabilities
    log.debug('Local video codec capabilities: %s.', capabilities)
    return {codec: capability.copy() for codec, capability in capabilities.items()}
