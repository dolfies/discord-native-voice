from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from discord.flags import SpeakingFlags
from discord.player import AudioPlayer, AudioSource

from .media import AudioMediaSource, MediaSource

if TYPE_CHECKING:
    from .client import VoiceClient

__all__ = ('MediaPlayer', 'MediaPlayerStats')

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MediaPlayerStats:
    """Represents media player send timing statistics.

    Attributes
    ----------
    started_at: :class:`float`
        Local monotonic timestamp for when playback started or resumed.
    audio_frames_sent: :class:`int`
        Number of audio frames sent.
    video_frame_batches_sent: :class:`int`
        Number of video frame batches sent.
    video_frames_sent: :class:`int`
        Number of encoded video frames sent.
    video_packets_sent: :class:`int`
        Number of RTP video packets sent.
    late_video_frames: :class:`int`
        Number of video frames sent later than their scheduled time.
    max_video_late_ms: :class:`float`
        Maximum observed video lateness in milliseconds.
    audio_send_mean_ms: :class:`float`
        Mean time spent sending an audio frame in milliseconds.
    audio_send_max_ms: :class:`float`
        Maximum time spent sending an audio frame in milliseconds.
    video_send_mean_ms: :class:`float`
        Mean time spent sending a video frame batch in milliseconds.
    video_send_max_ms: :class:`float`
        Maximum time spent sending a video frame batch in milliseconds.
    video_send_interval_mean_ms: :class:`float`
        Mean interval between video frame batch sends in milliseconds.
    video_send_interval_p95_ms: :class:`float`
        Approximate p95 interval between video frame batch sends in milliseconds.
    video_send_interval_max_ms: :class:`float`
        Maximum interval between video frame batch sends in milliseconds.
    sleep_mean_ms: :class:`float`
        Mean time spent sleeping in the player loop in milliseconds.
    sleep_max_ms: :class:`float`
        Maximum time spent sleeping in the player loop in milliseconds.
    """

    started_at: float
    audio_frames_sent: int
    video_frame_batches_sent: int
    video_frames_sent: int
    video_packets_sent: int
    late_video_frames: int
    max_video_late_ms: float
    audio_send_mean_ms: float
    audio_send_max_ms: float
    video_send_mean_ms: float
    video_send_max_ms: float
    video_send_interval_mean_ms: float
    video_send_interval_p95_ms: float
    video_send_interval_max_ms: float
    sleep_mean_ms: float
    sleep_max_ms: float


class MediaPlayer(AudioPlayer):
    client: VoiceClient
    SILENCE_PACKET_COUNT: int = 10

    def __init__(
        self,
        source: AudioSource,
        media_source: MediaSource,
        client: VoiceClient,
        *,
        after: Callable[[Exception | None], Any] | None = None,
        video_transport_start: concurrent.futures.Future[None] | None = None,
    ) -> None:
        super().__init__(source, client, after=after)
        self.media_source = media_source
        self._video_transport_start = video_transport_start
        self._stop_video_on_end = video_transport_start is not None
        self._source_generation = 0
        self._started_at = time.perf_counter()
        self._audio_frames_sent = 0
        self._audio_send_total_ms = 0.0
        self._audio_send_max_ms = 0.0
        self._video_frame_batches_sent = 0
        self._video_frames_sent = 0
        self._video_packets_sent = 0
        self._video_send_total_ms = 0.0
        self._video_send_max_ms = 0.0
        self._last_video_send_started_at: float | None = None
        self._video_send_interval_samples: deque[float] = deque(maxlen=2048)
        self._late_video_frames = 0
        self._max_video_late_ms = 0.0
        self._sleep_count = 0
        self._sleep_total_ms = 0.0
        self._sleep_max_ms = 0.0

    @property
    def stats(self) -> MediaPlayerStats:
        video_send_intervals = self._video_send_interval_samples
        return MediaPlayerStats(
            started_at=self._started_at,
            audio_frames_sent=self._audio_frames_sent,
            video_frame_batches_sent=self._video_frame_batches_sent,
            video_frames_sent=self._video_frames_sent,
            video_packets_sent=self._video_packets_sent,
            late_video_frames=self._late_video_frames,
            max_video_late_ms=self._max_video_late_ms,
            audio_send_mean_ms=self._audio_send_total_ms / max(1, self._audio_frames_sent),
            audio_send_max_ms=self._audio_send_max_ms,
            video_send_mean_ms=self._video_send_total_ms / max(1, self._video_frame_batches_sent),
            video_send_max_ms=self._video_send_max_ms,
            video_send_interval_mean_ms=sum(video_send_intervals) / max(1, len(video_send_intervals)),
            video_send_interval_p95_ms=self._video_send_interval_p95_ms(),
            video_send_interval_max_ms=max(video_send_intervals, default=0.0),
            sleep_mean_ms=self._sleep_total_ms / max(1, self._sleep_count),
            sleep_max_ms=self._sleep_max_ms,
        )

    def _record_audio_send(self, elapsed_ms: float) -> None:
        self._audio_frames_sent += 1
        self._audio_send_total_ms += elapsed_ms
        self._audio_send_max_ms = max(self._audio_send_max_ms, elapsed_ms)

    def _video_send_interval_p95_ms(self) -> float:
        samples = self._video_send_interval_samples
        if not samples:
            return 0.0

        ordered = sorted(samples)
        index = min(len(ordered) - 1, int((len(ordered) - 1) * 0.95))
        return ordered[index]

    def _record_video_send(self, *, started_at: float, elapsed_ms: float, frames: int, packets: int) -> None:
        last_started_at = self._last_video_send_started_at
        if last_started_at is not None:
            interval_ms = (started_at - last_started_at) * 1000
            if interval_ms >= 0:
                self._video_send_interval_samples.append(interval_ms)
        self._last_video_send_started_at = started_at
        self._video_frame_batches_sent += 1
        self._video_frames_sent += frames
        self._video_packets_sent += packets
        self._video_send_total_ms += elapsed_ms
        self._video_send_max_ms = max(self._video_send_max_ms, elapsed_ms)

    def _record_video_late(self, elapsed_ms: float) -> None:
        if elapsed_ms < 1.0:
            return
        self._late_video_frames += 1
        self._max_video_late_ms = max(self._max_video_late_ms, elapsed_ms)

    def _sleep(self, delay: float) -> None:
        started = time.perf_counter()
        time.sleep(delay)
        elapsed_ms = (time.perf_counter() - started) * 1000
        self._sleep_count += 1
        self._sleep_total_ms += elapsed_ms
        self._sleep_max_ms = max(self._sleep_max_ms, elapsed_ms)

    def _video_transport_started(self) -> bool:
        if self._video_transport_start is None:
            return True

        if not self._video_transport_start.done():
            return False

        try:
            self._video_transport_start.result()
        except Exception as exc:
            self._current_error = exc
            self.stop()
            return False
        finally:
            self._video_transport_start = None
        return True

    @staticmethod
    def _next_video_time(source: MediaSource, next_video: float, frame_time: float) -> float:
        target = next_video + frame_time
        now = time.perf_counter()
        if source.video_realtime:
            if now <= target:
                return target
            if now - target > frame_time * 2:
                return now + frame_time
            return target + (int((now - target) // frame_time) + 1) * frame_time
        if now - target > frame_time:
            return now
        return target

    @staticmethod
    def _video_retry_delay(source: MediaSource) -> float:
        delay = source.video_retry_delay
        try:
            return max(0.001, min(float(delay), 0.02))
        except (TypeError, ValueError):
            return 0.02

    @staticmethod
    def _video_catchup_frames(source: MediaSource) -> int:
        if not source.video_realtime:
            return 1
        value = source.video_catchup_frames
        try:
            return max(1, min(int(value), 16))
        except (TypeError, ValueError):
            return 4

    @staticmethod
    def _video_pacing_time(source: MediaSource, frames: Mapping[str, Any]) -> float:
        if source.video_realtime:
            video_config = source.video_config
            if video_config is not None:
                return 1.0 / max(1, video_config.fps)

        frame_time_ms = max(frame.frame_time_ms for frame in frames.values())
        return max(0.001, frame_time_ms) / 1000.0

    @staticmethod
    def _audio_silence_speaking_flags(flags: SpeakingFlags) -> SpeakingFlags:
        if flags.soundshare:
            return SpeakingFlags(soundshare=True)
        return SpeakingFlags.none()

    def resume(self, *, update_speaking: bool = True) -> None:
        self.loops = 0
        self._start = time.perf_counter()
        self._started_at = self._start
        self._last_video_send_started_at = None
        self._resumed.set()
        if update_speaking:
            self._speak(self.client._speaking_flags_for_source(self.media_source))

    def run(self) -> None:
        try:
            self._do_run()
        except Exception as exc:
            self._current_error = exc
            self.stop()
        finally:
            self._call_after()
            self.media_source.cleanup()

    def _do_run(self) -> None:
        self.loops = 0
        self._start = time.perf_counter()
        self._started_at = self._start

        client = self.client
        send_audio_packet = client.send_audio_packet
        send_video_frames = client.send_video_frames
        source = self.media_source
        has_audio = False
        speaking_flags = SpeakingFlags.none()
        audio_done = True
        video_done = True
        source_generation = -1
        next_audio = self._start
        next_video = self._start
        audio_silence_sent = True

        try:
            while not self._end.is_set():
                if source_generation != self._source_generation:
                    source = self.media_source
                    has_audio = source.has_audio()
                    speaking_flags = client._speaking_flags_for_source(source)
                    audio_done = not has_audio
                    video_done = not source.has_video()
                    audio_silence_sent = not has_audio
                    source_generation = self._source_generation
                    next_audio = next_video = time.perf_counter()
                    self._last_video_send_started_at = None
                    self._speak(speaking_flags)

                if not self._resumed.is_set():
                    if has_audio and not audio_silence_sent:
                        self.send_silence(self.SILENCE_PACKET_COUNT)
                        audio_silence_sent = True
                    self._resumed.wait()
                    next_audio = next_video = time.perf_counter()
                    continue

                if not client.is_connected():
                    log.debug('Not connected, waiting for %ss...', client.timeout)
                    connected = client.wait_until_connected(client.timeout)
                    if self._end.is_set() or not connected:
                        log.debug('Aborting media playback.')
                        return
                    self._speak(speaking_flags)
                    next_audio = next_video = time.perf_counter()

                now = time.perf_counter()
                if not audio_done and now >= next_audio:
                    data = source.read()
                    if data:
                        send_started = time.perf_counter()
                        send_audio_packet(data, encode=not source.is_opus())
                        self._record_audio_send((time.perf_counter() - send_started) * 1000)
                        self.loops += 1
                        audio_silence_sent = False
                        next_audio += self.DELAY
                    else:
                        audio_done = True
                        if not video_done and not audio_silence_sent:
                            speaking_flags = self._audio_silence_speaking_flags(speaking_flags)
                            if client.is_connected():
                                self._speak(speaking_flags)
                                self.send_silence(self.SILENCE_PACKET_COUNT)
                            audio_silence_sent = True

                if not video_done and now >= next_video:
                    for index in range(self._video_catchup_frames(source)):
                        now = time.perf_counter()
                        if now < next_video:
                            break
                        if index > 0 and not audio_done and now >= next_audio:
                            break
                        if now > next_video:
                            self._record_video_late((now - next_video) * 1000)

                        # Video send needs the SSRC/packetizer created by start_video()
                        # DAVE encryption is not part of this startup check
                        if not self._video_transport_started():
                            next_video = time.perf_counter() + 0.02
                            break
                        if self._end.is_set():
                            return
                        streams = client.active_video_streams
                        if not streams:
                            next_video = time.perf_counter() + 0.02
                            break
                        if client._send_requested_video_keyframes():
                            video_config = source.video_config
                            frame_time = 1.0 / max(1, video_config.fps) if video_config is not None else 0.02
                            next_video = self._next_video_time(source, next_video, frame_time)
                            break
                        frames = source.read_video_streams(streams)
                        if frames is None:
                            video_done = True
                            break
                        if frames:
                            send_started = time.perf_counter()
                            sent_packets = send_video_frames(frames)
                            self._record_video_send(
                                started_at=send_started,
                                elapsed_ms=(time.perf_counter() - send_started) * 1000,
                                frames=len(frames),
                                packets=sent_packets,
                            )
                            frame_time = self._video_pacing_time(source, frames)
                            next_video = self._next_video_time(source, next_video, frame_time)
                        else:
                            next_video = time.perf_counter() + self._video_retry_delay(source)
                            break

                if (audio_done and video_done) or source.is_finished():
                    self.stop()
                    break

                wakeups = []
                if not audio_done:
                    wakeups.append(next_audio)
                if not video_done:
                    wakeups.append(next_video)
                delay = min(max((min(wakeups) - time.perf_counter()) if wakeups else self.DELAY, 0.0), 0.02)
                if delay:
                    self._sleep(delay)

            if has_audio and not audio_silence_sent and client.is_connected():
                self.send_silence(self.SILENCE_PACKET_COUNT)
        finally:
            if self._stop_video_on_end:
                future = asyncio.run_coroutine_threadsafe(client.stop_video(), client.loop)
                try:
                    future.result(timeout=client.timeout)
                except Exception:
                    log.debug('Failed to stop auto-started video after media playback.', exc_info=True)
            finished = getattr(client, '_media_player_finished', None)
            if callable(finished):
                client.loop.call_soon_threadsafe(finished, source)

    def set_source(self, source: AudioSource) -> None:
        media_source = source if isinstance(source, MediaSource) else AudioMediaSource(source)

        client = self.client
        video_transport_start = self._video_transport_start
        if media_source.has_video():
            should_start_video = client._video_codec is None or self._stop_video_on_end
            if should_start_video:
                if video_transport_start is not None and not video_transport_start.done():
                    video_transport_start.cancel()
                video_params = client._video_params_for_source(media_source)
                video_transport_start = asyncio.run_coroutine_threadsafe(
                    client.start_video(**video_params),
                    client.loop,
                )
                self._stop_video_on_end = True
        elif self._stop_video_on_end:
            if video_transport_start is not None and not video_transport_start.done():
                video_transport_start.cancel()
            asyncio.run_coroutine_threadsafe(client.stop_video(), client.loop)
            video_transport_start = None
            self._stop_video_on_end = False

        with self._lock:
            self.pause(update_speaking=False)
            self.source = source
            self.media_source = media_source
            self._video_transport_start = video_transport_start
            self._source_generation += 1
            client._media_player_source_changed(media_source)
            self.resume(update_speaking=False)
