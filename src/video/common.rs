use std::cell::Cell;
use std::time::{SystemTime, UNIX_EPOCH};

use crate::rtp::{MediaPacket, RtpPacket};

pub(super) const DEFAULT_MTU: usize = 1200;

fn fallback_random_bytes<const N: usize>() -> [u8; N] {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or_default();
    let mut state = nanos as u64 ^ (std::process::id() as u64).rotate_left(17);
    let mut bytes = [0u8; N];
    for byte in &mut bytes {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        *byte = state as u8;
    }
    bytes
}

fn random_bytes<const N: usize>() -> [u8; N] {
    let mut bytes = [0u8; N];
    if getrandom::fill(&mut bytes).is_err() {
        return fallback_random_bytes();
    }
    bytes
}

pub(crate) fn random_u16() -> u16 {
    u16::from_ne_bytes(random_bytes())
}

fn random_u32() -> u32 {
    u32::from_ne_bytes(random_bytes())
}

#[derive(Debug, Default)]
pub(super) struct DepacketizerState {
    timestamp: Option<u32>,
    last_sequence: Option<u16>,
    discarding: bool,
    pub(super) frame: Vec<u8>,
}

impl DepacketizerState {
    pub(super) fn begin_packet(&mut self, sequence: u16, timestamp: u32) {
        if self.timestamp != Some(timestamp) {
            self.reset(timestamp);
        } else if let Some(last_sequence) = self.last_sequence
            && last_sequence.wrapping_add(1) != sequence
        {
            self.discard_frame();
        }
        self.last_sequence = Some(sequence);
    }

    fn reset(&mut self, timestamp: u32) {
        self.timestamp = Some(timestamp);
        self.last_sequence = None;
        self.discarding = false;
        self.frame.clear();
    }

    pub(super) fn discard_frame(&mut self) {
        self.discarding = true;
        self.frame.clear();
    }

    pub(super) fn is_discarding(&self) -> bool {
        self.discarding
    }

    pub(super) fn finish_frame(&mut self) -> Option<Vec<u8>> {
        let frame = std::mem::take(&mut self.frame);
        self.timestamp = None;
        self.last_sequence = None;
        self.discarding = false;

        if frame.is_empty() { None } else { Some(frame) }
    }
}

pub(super) struct PacketizerState {
    payload_type: u8,
    ssrc: u32,
    mtu: usize,
    sequence: Cell<u16>,
    timestamp: Cell<u32>,
    timestamp_remainder: Cell<f64>,
}

impl PacketizerState {
    pub(super) fn new(ssrc: u32, payload_type: u8) -> Self {
        Self {
            payload_type,
            ssrc,
            mtu: DEFAULT_MTU,
            sequence: Cell::new(random_u16()),
            timestamp: Cell::new(random_u32()),
            timestamp_remainder: Cell::new(0.0),
        }
    }

    pub(super) fn mtu(&self) -> usize {
        self.mtu
    }

    pub(super) fn packet_for_payload(&self, payload: Vec<u8>, marker: bool) -> MediaPacket {
        let sequence = self.next_sequence();
        let timestamp = self.timestamp.get();
        let header = RtpPacket::new_header(
            self.payload_type,
            sequence,
            timestamp,
            self.ssrc,
            marker,
            false,
        );
        MediaPacket {
            header,
            payload,
            marker,
            sequence,
            timestamp,
            ssrc: self.ssrc,
            payload_type: self.payload_type,
        }
    }

    pub(super) fn increment_timestamp(&self, frame_time_ms: f64) {
        let duration_ms = if frame_time_ms.is_finite() {
            frame_time_ms.max(0.0)
        } else {
            0.0
        };
        let ticks = duration_ms * 90.0 + self.timestamp_remainder.get();
        let whole_ticks = (ticks + 1e-9).floor();
        let step = whole_ticks.clamp(0.0, u32::MAX as f64) as u32;
        self.timestamp_remainder.set(ticks - whole_ticks);
        self.timestamp.set(self.timestamp.get().wrapping_add(step));
    }

    pub(super) fn timestamp(&self) -> u32 {
        self.timestamp.get()
    }

    fn next_sequence(&self) -> u16 {
        let next = self.sequence.get().wrapping_add(1);
        self.sequence.set(next);
        next
    }
}
