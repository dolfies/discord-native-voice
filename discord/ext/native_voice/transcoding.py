from __future__ import annotations

import shlex
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TypedDict

import discord
from discord.player import CREATE_NO_WINDOW


class _VideoCodecNames(TypedDict):
    encoders: tuple[str, ...]
    decoders: tuple[str, ...]


VIDEO_CODEC_CAPABILITY_NAMES: dict[str, _VideoCodecNames] = {
    'AV1': {
        'encoders': (
            'av1_nvenc',
            'av1_amf',
            'av1_qsv',
            'av1_vaapi',
            'av1_mf',
            'libaom-av1',
            'libsvtav1',
            'librav1e',
        ),
        'decoders': ('av1', 'av1_cuvid', 'av1_qsv', 'libaom-av1', 'libdav1d'),
    },
    'H265': {
        'encoders': (
            'hevc_nvenc',
            'hevc_amf',
            'hevc_qsv',
            'hevc_vaapi',
            'hevc_videotoolbox',
            'hevc_mf',
            'hevc_v4l2m2m',
            'libx265',
        ),
        'decoders': ('hevc', 'hevc_cuvid', 'hevc_qsv', 'hevc_v4l2m2m'),
    },
    'H264': {
        'encoders': (
            'h264_nvenc',
            'h264_amf',
            'h264_qsv',
            'h264_vaapi',
            'h264_videotoolbox',
            'h264_mf',
            'h264_v4l2m2m',
            'libx264',
            'libopenh264',
        ),
        'decoders': ('h264', 'h264_cuvid', 'h264_qsv', 'h264_v4l2m2m'),
    },
    'VP8': {
        'encoders': ('vp8_vaapi', 'vp8_v4l2m2m', 'libvpx'),
        'decoders': ('vp8', 'libvpx', 'vp8_cuvid', 'vp8_qsv', 'vp8_v4l2m2m'),
    },
    'VP9': {
        'encoders': ('vp9_qsv', 'vp9_vaapi', 'vp9_v4l2m2m', 'libvpx-vp9'),
        'decoders': ('vp9', 'libvpx-vp9', 'vp9_cuvid', 'vp9_qsv', 'vp9_v4l2m2m'),
    },
}

VIDEO_CODEC_SOFTWARE_PERFORMANCE: dict[str, int] = {
    'H264': 500,
    'VP8': 400,
    'H265': 350,
    'VP9': 300,
    'AV1': 250,
}

HARDWARE_ENCODERS = frozenset(
    {
        'h264_amf',
        'h264_mf',
        'h264_nvenc',
        'h264_qsv',
        'h264_v4l2m2m',
        'h264_vaapi',
        'h264_videotoolbox',
        'hevc_amf',
        'hevc_mf',
        'hevc_nvenc',
        'hevc_qsv',
        'hevc_v4l2m2m',
        'hevc_vaapi',
        'hevc_videotoolbox',
        'vp8_v4l2m2m',
        'vp8_vaapi',
        'vp9_qsv',
        'vp9_v4l2m2m',
        'vp9_vaapi',
        'av1_amf',
        'av1_mf',
        'av1_nvenc',
        'av1_qsv',
        'av1_vaapi',
    }
)
HARDWARE_DECODERS = frozenset(
    {
        'h264_cuvid',
        'h264_qsv',
        'h264_v4l2m2m',
        'hevc_cuvid',
        'hevc_qsv',
        'hevc_v4l2m2m',
        'vp8_cuvid',
        'vp8_qsv',
        'vp8_v4l2m2m',
        'vp9_cuvid',
        'vp9_qsv',
        'vp9_v4l2m2m',
        'av1_cuvid',
        'av1_qsv',
    }
)

SUPPORTED_VIDEO_CODECS = frozenset(VIDEO_CODEC_CAPABILITY_NAMES)
VIDEO_CODEC_ALIASES = {
    'h264': 'H264',
    'avc1': 'H264',
    'h265': 'H265',
    'hevc': 'H265',
    'hev1': 'H265',
    'hvc1': 'H265',
    'vp8': 'VP8',
    'vp9': 'VP9',
    'av1': 'AV1',
    'av01': 'AV1',
}
_VIDEO_ENCODER_PRIORITY: dict[str, tuple[str, ...]] = {
    codec: names['encoders'] for codec, names in VIDEO_CODEC_CAPABILITY_NAMES.items()
}
_VIDEO_SOFTWARE_ENCODER_PRIORITY: dict[str, tuple[str, ...]] = {
    codec: tuple(encoder for encoder in encoders if encoder not in HARDWARE_ENCODERS)
    for codec, encoders in _VIDEO_ENCODER_PRIORITY.items()
}
_VIDEO_DEFAULT_ENCODERS: dict[str, str] = {
    codec: encoders[0] for codec, encoders in _VIDEO_SOFTWARE_ENCODER_PRIORITY.items()
}
_VIDEO_OUTPUT_FORMAT: dict[str, str] = {
    'H264': 'h264',
    'H265': 'hevc',
    'VP8': 'ivf',
    'VP9': 'ivf',
    'AV1': 'ivf',
}
_NVENC_ENCODERS = frozenset({'h264_nvenc', 'hevc_nvenc', 'av1_nvenc'})
_NVENC_H26X_ENCODERS = frozenset({'h264_nvenc', 'hevc_nvenc'})
_AMF_ENCODERS = frozenset({'h264_amf', 'hevc_amf', 'av1_amf'})
_QSV_ENCODERS = frozenset({'h264_qsv', 'hevc_qsv', 'vp9_qsv', 'av1_qsv'})
_VAAPI_ENCODERS = frozenset({'h264_vaapi', 'hevc_vaapi', 'vp8_vaapi', 'vp9_vaapi', 'av1_vaapi'})
_VIDEOTOOLBOX_ENCODERS = frozenset({'h264_videotoolbox', 'hevc_videotoolbox'})
_V4L2M2M_ENCODERS = frozenset({'h264_v4l2m2m', 'hevc_v4l2m2m', 'vp8_v4l2m2m', 'vp9_v4l2m2m'})
_MEDIA_FOUNDATION_ENCODERS = frozenset({'h264_mf', 'hevc_mf', 'av1_mf'})
_FFmpegEncoderUsableCacheKey = tuple[
    str,
    str,
    str,
    int,
    int,
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...] | None,
]
_ffmpeg_encoder_usable_cache: dict[_FFmpegEncoderUsableCacheKey, bool] = {}
_ffmpeg_encoder_cache: dict[str, frozenset[str]] = {}
_ffmpeg_decoder_cache: dict[str, frozenset[str]] = {}


def _ffmpeg_video_codec_names(executable: str, kind: str) -> frozenset[str]:
    cache = _ffmpeg_encoder_cache if kind == 'encoders' else _ffmpeg_decoder_cache
    cached = cache.get(executable)
    if cached is not None:
        return cached

    try:
        process = subprocess.run(
            [executable, '-hide_banner', f'-{kind}'],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
    except OSError:
        names = frozenset[str]()
    else:
        parsed_names = set[str]()
        if process.returncode == 0:
            for line in process.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0].startswith('V') and '.' in parts[0]:
                    parsed_names.add(parts[1])
        names = frozenset(parsed_names)

    cache[executable] = names
    return names


def _native_video_bitrate(height: int | None, fps: int | None) -> int:
    if (height or 0) >= 721 or (fps or 0) >= 31:
        return 8_000_000
    return 4_000_000


@dataclass(frozen=True)
class VideoTranscoderConfig:
    """FFmpeg codec selection options for video sources.

    Attributes
    ----------
    encoder: Optional[Union[:class:`str`, Dict[:class:`str`, :class:`str`]]]
        Exact FFmpeg video encoder to use, or a mapping of Discord codec name to
        FFmpeg encoder name. If omitted, an available encoder is selected for the
        target codec.
    decoder: Optional[Union[:class:`str`, Dict[:class:`str`, :class:`str`]]]
        Exact FFmpeg video decoder to use for the input, or a mapping of
        Discord codec name to FFmpeg decoder name. This is emitted as an input
        option before ``-i``.
    prefer_hardware: :class:`bool`
        Prefer low-latency hardware encoders when FFmpeg advertises them.
    validate_encoder: :class:`bool`
        Validate explicit encoders against ``ffmpeg -encoders`` before starting.
    validate_decoder: :class:`bool`
        Validate explicit decoders against ``ffmpeg -decoders`` before starting.
    encoder_options: List[:class:`str`]
        Extra arguments appended immediately after the selected encoder options.
    input_options: List[:class:`str`]
        Extra arguments inserted before the input arguments.
    output_options: List[:class:`str`]
        Extra arguments appended after ``options`` and before the output format.
    video_filters: Optional[List[:class:`str`]]
        Full FFmpeg video filtergraph fragments. If omitted, sources use the
        default low-latency software scale and ``yuv420p`` conversion.
    """

    encoder: str | Mapping[str, str] | None = None
    decoder: str | Mapping[str, str] | None = None
    prefer_hardware: bool = True
    validate_encoder: bool = True
    validate_decoder: bool = True
    encoder_options: Sequence[str] = ()
    input_options: Sequence[str] = ()
    output_options: Sequence[str] = ()
    video_filters: Sequence[str] | None = None

    @classmethod
    def software(
        cls,
        *,
        validate_encoder: bool = True,
        encoder_options: Sequence[str] = (),
        input_options: Sequence[str] = (),
        output_options: Sequence[str] = (),
        video_filters: Sequence[str] | None = None,
    ) -> VideoTranscoderConfig:
        """Prefer software encoders and skip hardware encoder probing.

        Parameters
        ----------
        validate_encoder: :class:`bool`
            Whether to validate the selected encoder before starting FFmpeg.
        encoder_options: List[:class:`str`]
            Extra arguments appended immediately after selected encoder options.
        input_options: List[:class:`str`]
            Extra arguments inserted before input arguments.
        output_options: List[:class:`str`]
            Extra arguments appended after source options and before output format.
        video_filters: Optional[List[:class:`str`]]
            Full FFmpeg video filtergraph fragments.

        Returns
        -------
        :class:`VideoTranscoderConfig`
            The configured transcoder options.
        """
        return cls(
            prefer_hardware=False,
            validate_encoder=validate_encoder,
            encoder_options=encoder_options,
            input_options=input_options,
            output_options=output_options,
            video_filters=video_filters,
        )

    @classmethod
    def _encoder_config(
        cls,
        encoder: Mapping[str, str],
        *,
        validate_encoder: bool,
        encoder_options: Sequence[str],
        input_options: Sequence[str],
        output_options: Sequence[str],
        video_filters: Sequence[str] | None,
    ) -> VideoTranscoderConfig:
        return cls(
            encoder=encoder,
            validate_encoder=validate_encoder,
            encoder_options=encoder_options,
            input_options=input_options,
            output_options=output_options,
            video_filters=video_filters,
        )

    @classmethod
    def nvenc(
        cls,
        *,
        preset: str | None = None,
        tune: str | None = None,
        gpu: int | None = None,
        spatial_aq: bool | None = None,
        temporal_aq: bool | None = None,
        validate_encoder: bool = True,
        encoder_options: Sequence[str] = (),
        input_options: Sequence[str] = (),
        output_options: Sequence[str] = (),
        video_filters: Sequence[str] | None = None,
    ) -> VideoTranscoderConfig:
        """Use NVIDIA NVENC encoders for H264, H265, and AV1.

        Parameters
        ----------
        preset: Optional[:class:`str`]
            NVENC preset option.
        tune: Optional[:class:`str`]
            NVENC tuning option.
        gpu: Optional[:class:`int`]
            GPU index passed to NVENC.
        spatial_aq: Optional[:class:`bool`]
            Whether to enable NVENC spatial adaptive quantization.
        temporal_aq: Optional[:class:`bool`]
            Whether to enable NVENC temporal adaptive quantization.
        validate_encoder: :class:`bool`
            Whether to validate the selected encoder before starting FFmpeg.
        encoder_options: List[:class:`str`]
            Extra arguments appended immediately after selected encoder options.
        input_options: List[:class:`str`]
            Extra arguments inserted before input arguments.
        output_options: List[:class:`str`]
            Extra arguments appended after source options and before output format.
        video_filters: Optional[List[:class:`str`]]
            Full FFmpeg video filtergraph fragments.

        Returns
        -------
        :class:`VideoTranscoderConfig`
            The configured transcoder options.
        """
        options: list[str] = []
        if preset is not None:
            options.extend(('-preset', preset))
        if tune is not None:
            options.extend(('-tune', tune))
        if gpu is not None:
            options.extend(('-gpu', str(gpu)))
        if spatial_aq is not None:
            options.extend(('-spatial-aq', '1' if spatial_aq else '0'))
        if temporal_aq is not None:
            options.extend(('-temporal-aq', '1' if temporal_aq else '0'))

        return cls._encoder_config(
            {'H264': 'h264_nvenc', 'H265': 'hevc_nvenc', 'AV1': 'av1_nvenc'},
            validate_encoder=validate_encoder,
            encoder_options=(*options, *encoder_options),
            input_options=input_options,
            output_options=output_options,
            video_filters=video_filters,
        )

    @classmethod
    def amf(
        cls,
        *,
        validate_encoder: bool = True,
        encoder_options: Sequence[str] = (),
        input_options: Sequence[str] = (),
        output_options: Sequence[str] = (),
        video_filters: Sequence[str] | None = None,
    ) -> VideoTranscoderConfig:
        """Use AMD AMF encoders for H264, H265, and AV1.

        Parameters
        ----------
        validate_encoder: :class:`bool`
            Whether to validate the selected encoder before starting FFmpeg.
        encoder_options: List[:class:`str`]
            Extra arguments appended immediately after selected encoder options.
        input_options: List[:class:`str`]
            Extra arguments inserted before input arguments.
        output_options: List[:class:`str`]
            Extra arguments appended after source options and before output format.
        video_filters: Optional[List[:class:`str`]]
            Full FFmpeg video filtergraph fragments.

        Returns
        -------
        :class:`VideoTranscoderConfig`
            The configured transcoder options.
        """
        return cls._encoder_config(
            {'H264': 'h264_amf', 'H265': 'hevc_amf', 'AV1': 'av1_amf'},
            validate_encoder=validate_encoder,
            encoder_options=encoder_options,
            input_options=input_options,
            output_options=output_options,
            video_filters=video_filters,
        )

    @classmethod
    def qsv(
        cls,
        *,
        validate_encoder: bool = True,
        encoder_options: Sequence[str] = (),
        input_options: Sequence[str] = (),
        output_options: Sequence[str] = (),
        video_filters: Sequence[str] | None = None,
    ) -> VideoTranscoderConfig:
        """Use Intel Quick Sync Video encoders for H264, H265, VP9, and AV1.

        Parameters
        ----------
        validate_encoder: :class:`bool`
            Whether to validate the selected encoder before starting FFmpeg.
        encoder_options: List[:class:`str`]
            Extra arguments appended immediately after selected encoder options.
        input_options: List[:class:`str`]
            Extra arguments inserted before input arguments.
        output_options: List[:class:`str`]
            Extra arguments appended after source options and before output format.
        video_filters: Optional[List[:class:`str`]]
            Full FFmpeg video filtergraph fragments.

        Returns
        -------
        :class:`VideoTranscoderConfig`
            The configured transcoder options.
        """
        return cls._encoder_config(
            {'H264': 'h264_qsv', 'H265': 'hevc_qsv', 'VP9': 'vp9_qsv', 'AV1': 'av1_qsv'},
            validate_encoder=validate_encoder,
            encoder_options=encoder_options,
            input_options=input_options,
            output_options=output_options,
            video_filters=video_filters,
        )

    @classmethod
    def vaapi(
        cls,
        *,
        device: str = '/dev/dri/renderD128',
        validate_encoder: bool = True,
        encoder_options: Sequence[str] = (),
        input_options: Sequence[str] = (),
        output_options: Sequence[str] = (),
    ) -> VideoTranscoderConfig:
        """Use VAAPI encoders.

        Parameters
        ----------
        device: :class:`str`
            VAAPI render device path.
        validate_encoder: :class:`bool`
            Whether to validate the selected encoder before starting FFmpeg.
        encoder_options: List[:class:`str`]
            Extra arguments appended immediately after selected encoder options.
        input_options: List[:class:`str`]
            Extra arguments inserted before input arguments.
        output_options: List[:class:`str`]
            Extra arguments appended after source options and before output format.

        Returns
        -------
        :class:`VideoTranscoderConfig`
            The configured transcoder options.
        """
        return cls(
            encoder={
                'H264': 'h264_vaapi',
                'H265': 'hevc_vaapi',
                'VP8': 'vp8_vaapi',
                'VP9': 'vp9_vaapi',
                'AV1': 'av1_vaapi',
            },
            validate_encoder=validate_encoder,
            encoder_options=encoder_options,
            input_options=('-vaapi_device', device, *input_options),
            output_options=output_options,
            video_filters=('scale={width}:{height}:flags=fast_bilinear', 'format=nv12|vaapi', 'hwupload'),
        )

    @classmethod
    def video_toolbox(
        cls,
        *,
        validate_encoder: bool = True,
        encoder_options: Sequence[str] = (),
        input_options: Sequence[str] = (),
        output_options: Sequence[str] = (),
        video_filters: Sequence[str] | None = None,
    ) -> VideoTranscoderConfig:
        """Use macOS VideoToolbox encoders for H264 and H265.

        Parameters
        ----------
        validate_encoder: :class:`bool`
            Whether to validate the selected encoder before starting FFmpeg.
        encoder_options: List[:class:`str`]
            Extra arguments appended immediately after selected encoder options.
        input_options: List[:class:`str`]
            Extra arguments inserted before input arguments.
        output_options: List[:class:`str`]
            Extra arguments appended after source options and before output format.
        video_filters: Optional[List[:class:`str`]]
            Full FFmpeg video filtergraph fragments.

        Returns
        -------
        :class:`VideoTranscoderConfig`
            The configured transcoder options.
        """
        return cls._encoder_config(
            {'H264': 'h264_videotoolbox', 'H265': 'hevc_videotoolbox'},
            validate_encoder=validate_encoder,
            encoder_options=encoder_options,
            input_options=input_options,
            output_options=output_options,
            video_filters=video_filters,
        )

    @classmethod
    def media_foundation(
        cls,
        *,
        validate_encoder: bool = True,
        encoder_options: Sequence[str] = (),
        input_options: Sequence[str] = (),
        output_options: Sequence[str] = (),
        video_filters: Sequence[str] | None = None,
    ) -> VideoTranscoderConfig:
        """Use Windows Media Foundation encoders for H264, H265, and AV1.

        Parameters
        ----------
        validate_encoder: :class:`bool`
            Whether to validate the selected encoder before starting FFmpeg.
        encoder_options: List[:class:`str`]
            Extra arguments appended immediately after selected encoder options.
        input_options: List[:class:`str`]
            Extra arguments inserted before input arguments.
        output_options: List[:class:`str`]
            Extra arguments appended after source options and before output format.
        video_filters: Optional[List[:class:`str`]]
            Full FFmpeg video filtergraph fragments.

        Returns
        -------
        :class:`VideoTranscoderConfig`
            The configured transcoder options.
        """
        return cls._encoder_config(
            {'H264': 'h264_mf', 'H265': 'hevc_mf', 'AV1': 'av1_mf'},
            validate_encoder=validate_encoder,
            encoder_options=encoder_options,
            input_options=input_options,
            output_options=output_options,
            video_filters=video_filters,
        )


def _video_codec_or_none(value: object) -> str | None:
    if value is None:
        return None

    normalized = str(value).strip().lower()
    if not normalized:
        return None
    return VIDEO_CODEC_ALIASES.get(normalized)


def _coerce_video_codec(value: str) -> str:
    normalized = _video_codec_or_none(value)
    if normalized is None:
        normalized = value.upper()
    if normalized not in SUPPORTED_VIDEO_CODECS:
        raise ValueError(f'Unsupported video source codec {value!r}')
    return normalized


def _argv_options(value: Sequence[str] | str | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return shlex.split(value)
    return list(value)


def _resolve_transcoder_codec(codec: str, selection: str | Mapping[str, str], *, kind: str) -> str:
    if isinstance(selection, str):
        return selection

    normalized = _coerce_video_codec(codec)
    selected = selection.get(normalized)
    if selected is None:
        selected = selection.get(normalized.lower())
    if selected is None:
        supported = ', '.join(sorted(selection))
        raise discord.ClientException(
            f'Transcoder {kind} mapping does not include {normalized}. Supported codecs: {supported or "none"}.'
        )
    return selected


def _video_encoder_args(codec: str, encoder: str, *, fps: int) -> list[str]:
    codec = _coerce_video_codec(codec)
    args = ['-c:v', encoder]

    if encoder == 'libx264':
        return [
            *args,
            '-preset',
            'ultrafast',
            '-tune',
            'zerolatency',
            '-profile:v',
            'baseline',
            '-g',
            str(fps),
            '-keyint_min',
            str(fps),
            '-x264-params',
            f'repeat-headers=1:scenecut=0:aud=1:keyint={fps}:min-keyint={fps}',
        ]
    if encoder == 'libx265':
        return [
            *args,
            '-preset',
            'ultrafast',
            '-tune',
            'zerolatency',
            '-g',
            str(fps),
            '-keyint_min',
            str(fps),
            '-x265-params',
            f'repeat-headers=1:aud=1:scenecut=0:keyint={fps}:min-keyint={fps}',
        ]
    if encoder in _NVENC_ENCODERS:
        nvenc_args = [
            *args,
            '-preset',
            'p1',
            '-tune',
            'ull',
            '-g',
            str(fps),
            '-bf',
            '0',
            '-forced-idr',
            '1',
            '-zerolatency',
            '1',
            '-delay',
            '0',
        ]
        if encoder in _NVENC_H26X_ENCODERS:
            nvenc_args.extend(('-aud', '1'))
        return nvenc_args
    if encoder in _AMF_ENCODERS:
        return [*args, '-usage', 'ultralowlatency', '-quality', 'speed', '-g', str(fps), '-bf', '0']
    if encoder in _QSV_ENCODERS:
        return [*args, '-preset', 'veryfast', '-g', str(fps), '-bf', '0']
    if encoder in _VAAPI_ENCODERS:
        return [*args, '-g', str(fps), '-bf', '0']
    if encoder in _VIDEOTOOLBOX_ENCODERS:
        return [*args, '-g', str(fps), '-bf', '0', '-realtime', '1']
    if encoder in _V4L2M2M_ENCODERS:
        return [*args, '-g', str(fps), '-bf', '0']
    if encoder in _MEDIA_FOUNDATION_ENCODERS:
        return [*args, '-g', str(fps)]
    if encoder == 'libvpx':
        return [
            *args,
            '-deadline',
            'realtime',
            '-cpu-used',
            '8',
            '-lag-in-frames',
            '0',
            '-error-resilient',
            '1',
            '-auto-alt-ref',
            '0',
            '-g',
            str(fps),
        ]
    if encoder == 'libvpx-vp9':
        return [
            *args,
            '-deadline',
            'realtime',
            '-cpu-used',
            '8',
            '-lag-in-frames',
            '0',
            '-error-resilient',
            '1',
            '-auto-alt-ref',
            '0',
            '-row-mt',
            '1',
            '-g',
            str(fps),
        ]
    if encoder == 'libsvtav1':
        return [*args, '-preset', '12', '-rc', '2', '-la_depth', '0', '-sc_detection', '0', '-g', str(fps)]
    if encoder == 'libaom-av1':
        return [*args, '-cpu-used', '8', '-usage', 'realtime', '-lag-in-frames', '0', '-g', str(fps)]
    if encoder == 'librav1e':
        return [*args, '-speed', '10', '-g', str(fps)]

    return [*args, '-g', str(fps)]


def _video_bitstream_filter_args(codec: str, encoder: str | None = None) -> list[str]:
    codec = _coerce_video_codec(codec)
    if (codec, encoder) in {
        ('H264', 'h264_nvenc'),
        ('H265', 'hevc_nvenc'),
    }:
        return ['-bsf:v', 'dump_extra=freq=keyframe']
    if (codec, encoder) in {
        ('H264', 'libx264'),
        ('H265', 'libx265'),
    }:
        return []
    if codec == 'H264':
        return ['-bsf:v', 'h264_metadata=aud=insert']
    if codec == 'H265':
        return ['-bsf:v', 'hevc_metadata=aud=insert']
    return []


def _video_filtergraph(
    config: VideoTranscoderConfig,
    *,
    width: int,
    height: int,
    fps: int,
    codec: str,
) -> str:
    filters = config.video_filters
    if filters is None:
        filters = ('scale={width}:{height}:flags=fast_bilinear', 'format=yuv420p')

    return ','.join(fragment.format(width=width, height=height, fps=fps, codec=codec) for fragment in filters)


def _ffmpeg_encoder_is_usable(
    executable: str,
    codec: str,
    encoder: str,
    *,
    width: int,
    height: int,
    transcoder: VideoTranscoderConfig | None = None,
) -> bool:
    codec = _coerce_video_codec(codec)
    config = transcoder if transcoder is not None else VideoTranscoderConfig()
    probe_width = max(16, width if width > 0 else 64)
    probe_height = max(16, height if height > 0 else 64)
    if probe_width % 2:
        probe_width += 1
    if probe_height % 2:
        probe_height += 1
    key = (
        executable,
        codec,
        encoder,
        probe_width,
        probe_height,
        tuple(_argv_options(config.input_options)),
        tuple(_argv_options(config.encoder_options)),
        tuple(_argv_options(config.output_options)),
        None if config.video_filters is None else tuple(config.video_filters),
    )
    cached = _ffmpeg_encoder_usable_cache.get(key)
    if cached is not None:
        return cached

    command = [
        executable,
        '-hide_banner',
        '-loglevel',
        'error',
        '-nostdin',
        *_argv_options(config.input_options),
        '-f',
        'lavfi',
        '-i',
        f'testsrc2=size={probe_width}x{probe_height}:rate=1',
        '-frames:v',
        '1',
        '-an',
    ]
    filtergraph = _video_filtergraph(config, width=probe_width, height=probe_height, fps=1, codec=codec)
    if filtergraph:
        command.extend(('-vf', filtergraph))
    command.extend(
        [
            '-b:v',
            '200k',
            *_video_encoder_args(codec, encoder, fps=1),
            *_argv_options(config.encoder_options),
            *_argv_options(config.output_options),
            *_video_bitstream_filter_args(codec, encoder),
            '-f',
            'null',
            '-',
        ]
    )
    try:
        process = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        usable = False
    else:
        usable = process.returncode == 0

    _ffmpeg_encoder_usable_cache[key] = usable
    return usable
