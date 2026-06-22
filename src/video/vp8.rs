use std::cell::Cell;

use crate::rtp::{MediaPacket, PacketError};

use super::common::{DepacketizerState, PacketizerState};
use super::{VideoDepacketizer, VideoPacketizer};

pub struct VP8Depacketizer {
    state: DepacketizerState,
}

impl VP8Depacketizer {
    pub fn new() -> Self {
        Self {
            state: DepacketizerState::default(),
        }
    }

    fn payload_offset(payload: &[u8]) -> Result<(usize, bool), PacketError> {
        if payload.is_empty() {
            return Err(PacketError::InvalidPayload);
        }

        let extended = payload[0] & 0x80 != 0;
        let start = payload[0] & 0x10 != 0;
        let mut offset = 1;

        if extended {
            if offset >= payload.len() {
                return Err(PacketError::InvalidPayload);
            }

            let ext = payload[offset];
            offset += 1;
            if ext & 0x80 != 0 {
                if offset >= payload.len() {
                    return Err(PacketError::InvalidPayload);
                }
                offset += if payload[offset] & 0x80 != 0 { 2 } else { 1 };
            }
            if ext & 0x40 != 0 {
                offset += 1;
            }
            if ext & 0x30 != 0 {
                offset += 1;
            }
        }

        if offset > payload.len() {
            return Err(PacketError::InvalidPayload);
        }

        Ok((offset, start))
    }
}

impl Default for VP8Depacketizer {
    fn default() -> Self {
        Self::new()
    }
}

impl VideoDepacketizer for VP8Depacketizer {
    fn push_packet(
        &mut self,
        payload: &[u8],
        marker: bool,
        sequence: u16,
        timestamp: u32,
    ) -> Result<Option<Vec<u8>>, PacketError> {
        let (offset, start) = Self::payload_offset(payload)?;
        self.state.begin_packet(sequence, timestamp);
        if self.state.is_discarding() {
            return Ok(if marker {
                self.state.finish_frame()
            } else {
                None
            });
        }
        if !start && self.state.frame.is_empty() {
            self.state.discard_frame();
            return Ok(if marker {
                self.state.finish_frame()
            } else {
                None
            });
        }

        self.state.frame.extend_from_slice(&payload[offset..]);
        Ok(if marker {
            self.state.finish_frame()
        } else {
            None
        })
    }
}

pub struct VP8Packetizer {
    state: PacketizerState,
    picture_id: Cell<u16>,
}

impl VP8Packetizer {
    pub fn new(ssrc: u32, payload_type: u8) -> Self {
        Self {
            state: PacketizerState::new(ssrc, payload_type),
            picture_id: Cell::new(0),
        }
    }

    pub fn timestamp(&self) -> u32 {
        self.state.timestamp()
    }

    fn make_payload(&self, chunk: &[u8], first: bool) -> Vec<u8> {
        let picture_id = self.picture_id.get() & 0x7FFF;
        let mut payload = Vec::with_capacity(4 + chunk.len());
        payload.push(0x80 | if first { 0x10 } else { 0 });
        payload.push(0x80);
        payload.push(((picture_id >> 8) as u8) | 0x80);
        payload.push((picture_id & 0xFF) as u8);
        payload.extend_from_slice(chunk);
        payload
    }
}

impl VideoPacketizer for VP8Packetizer {
    fn packetize(&self, frame: &[u8], frame_time_ms: f64) -> Result<Vec<MediaPacket>, PacketError> {
        if frame.is_empty() {
            return Err(PacketError::EmptyFrame);
        }

        let max_chunk = self.state.mtu().saturating_sub(4);
        if max_chunk == 0 {
            return Err(PacketError::PayloadTooLarge);
        }

        let chunk_count = frame.len().div_ceil(max_chunk);
        let mut packets = Vec::with_capacity(chunk_count);
        for (index, chunk) in frame.chunks(max_chunk).enumerate() {
            let first = index == 0;
            let marker = index + 1 == chunk_count;
            packets.push(
                self.state
                    .packet_for_payload(self.make_payload(chunk, first), marker),
            );
        }

        self.state.increment_timestamp(frame_time_ms);
        self.picture_id
            .set(self.picture_id.get().wrapping_add(1) & 0x7FFF);
        Ok(packets)
    }
}
