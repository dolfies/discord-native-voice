use crate::rtp::{MediaPacket, PacketError};

use super::common::{DepacketizerState, PacketizerState};
use super::h26x::{ANNEX_B_START_CODE, split_annex_b};
use super::{VideoDepacketizer, VideoPacketizer};

pub struct H264Depacketizer {
    state: DepacketizerState,
}

impl H264Depacketizer {
    pub fn new() -> Self {
        Self {
            state: DepacketizerState::default(),
        }
    }

    fn push_nalu(&mut self, nalu: &[u8]) {
        self.state.frame.extend_from_slice(&ANNEX_B_START_CODE);
        self.state.frame.extend_from_slice(nalu);
    }

    fn push_stap_a(&mut self, payload: &[u8]) -> Result<(), PacketError> {
        let mut offset = 1;
        while offset + 2 <= payload.len() {
            let nalu_len = u16::from_be_bytes([payload[offset], payload[offset + 1]]) as usize;
            offset += 2;
            if nalu_len == 0 || offset + nalu_len > payload.len() {
                return Err(PacketError::InvalidPayload);
            }
            self.push_nalu(&payload[offset..offset + nalu_len]);
            offset += nalu_len;
        }

        if offset != payload.len() {
            return Err(PacketError::InvalidPayload);
        }

        Ok(())
    }

    fn push_fu_a(&mut self, payload: &[u8]) -> Result<(), PacketError> {
        if payload.len() < 2 {
            return Err(PacketError::InvalidPayload);
        }

        let fu_indicator = payload[0];
        let fu_header = payload[1];
        let start = fu_header & 0x80 != 0;
        let nal_type = fu_header & 0x1F;
        if nal_type == 0 || nal_type > 23 {
            return Err(PacketError::InvalidPayload);
        }

        if start {
            self.state.frame.extend_from_slice(&ANNEX_B_START_CODE);
            self.state.frame.push((fu_indicator & 0xE0) | nal_type);
        } else if self.state.frame.is_empty() {
            self.state.discard_frame();
            return Ok(());
        }

        self.state.frame.extend_from_slice(&payload[2..]);
        Ok(())
    }
}

impl Default for H264Depacketizer {
    fn default() -> Self {
        Self::new()
    }
}

impl VideoDepacketizer for H264Depacketizer {
    fn push_packet(
        &mut self,
        payload: &[u8],
        marker: bool,
        sequence: u16,
        timestamp: u32,
    ) -> Result<Option<Vec<u8>>, PacketError> {
        if payload.is_empty() {
            return Err(PacketError::InvalidPayload);
        }

        self.state.begin_packet(sequence, timestamp);
        if self.state.is_discarding() {
            return Ok(if marker {
                self.state.finish_frame()
            } else {
                None
            });
        }
        match payload[0] & 0x1F {
            1..=23 => self.push_nalu(payload),
            24 => self.push_stap_a(payload)?,
            28 => self.push_fu_a(payload)?,
            _ => return Err(PacketError::UnsupportedCodec),
        }

        Ok(if marker {
            self.state.finish_frame()
        } else {
            None
        })
    }
}

pub struct H264Packetizer {
    state: PacketizerState,
}

impl H264Packetizer {
    pub fn new(ssrc: u32, payload_type: u8) -> Self {
        Self {
            state: PacketizerState::new(ssrc, payload_type),
        }
    }

    pub fn timestamp(&self) -> u32 {
        self.state.timestamp()
    }
}

impl VideoPacketizer for H264Packetizer {
    fn packetize(&self, frame: &[u8], frame_time_ms: f64) -> Result<Vec<MediaPacket>, PacketError> {
        let nalus = split_annex_b(frame)?;
        let mut packets = Vec::new();

        for (nalu_index, nalu) in nalus.iter().enumerate() {
            let is_last_nalu = nalu_index == nalus.len() - 1;
            if nalu.len() <= self.state.mtu() {
                packets.push(self.state.packet_for_payload(nalu.to_vec(), is_last_nalu));
                continue;
            }

            let (&nalu_header, nalu_data) = nalu.split_first().ok_or(PacketError::EmptyFrame)?;
            let nal_type = nalu_header & 0x1F;
            let nri = nalu_header & 0x60;
            let max_chunk = self.state.mtu().saturating_sub(2);
            if max_chunk == 0 {
                return Err(PacketError::PayloadTooLarge);
            }

            let chunk_count = nalu_data.len().div_ceil(max_chunk);
            for (chunk_index, chunk) in nalu_data.chunks(max_chunk).enumerate() {
                let first = chunk_index == 0;
                let last = chunk_index + 1 == chunk_count;
                let marker = is_last_nalu && last;

                let mut payload = Vec::with_capacity(2 + chunk.len());
                payload.push(0x1C | nri);
                payload.push(
                    (if first { 0x80 } else { 0 }) | (if last { 0x40 } else { 0 }) | nal_type,
                );
                payload.extend_from_slice(chunk);
                packets.push(self.state.packet_for_payload(payload, marker));
            }
        }

        self.state.increment_timestamp(frame_time_ms);
        Ok(packets)
    }
}
