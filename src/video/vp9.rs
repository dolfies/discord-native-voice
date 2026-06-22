use crate::rtp::{MediaPacket, PacketError};

use super::common::{DepacketizerState, PacketizerState};
use super::{VideoDepacketizer, VideoPacketizer};

pub struct VP9Depacketizer {
    state: DepacketizerState,
}

impl VP9Depacketizer {
    pub fn new() -> Self {
        Self {
            state: DepacketizerState::default(),
        }
    }

    fn payload_offset(payload: &[u8]) -> Result<(usize, bool, bool), PacketError> {
        if payload.is_empty() {
            return Err(PacketError::InvalidPayload);
        }

        let descriptor = payload[0];
        let has_picture_id = descriptor & 0x80 != 0;
        let predicted = descriptor & 0x40 != 0;
        let has_layer_indices = descriptor & 0x20 != 0;
        let flexible = descriptor & 0x10 != 0;
        let start = descriptor & 0x08 != 0;
        let end = descriptor & 0x04 != 0;
        let has_scalability_structure = descriptor & 0x02 != 0;
        let mut offset = 1;

        if has_picture_id {
            if offset >= payload.len() {
                return Err(PacketError::InvalidPayload);
            }
            let two_byte_picture_id = payload[offset] & 0x80 != 0;
            offset += if two_byte_picture_id { 2 } else { 1 };
        }

        if has_layer_indices {
            offset += 1;
            if !flexible {
                offset += 1;
            }
        }

        if flexible && predicted {
            loop {
                if offset >= payload.len() {
                    return Err(PacketError::InvalidPayload);
                }
                let pdiff = payload[offset];
                offset += 1;
                if pdiff & 0x01 == 0 {
                    break;
                }
            }
        }

        if has_scalability_structure {
            offset = skip_vp9_scalability_structure(payload, offset)?;
        }

        if offset > payload.len() {
            return Err(PacketError::InvalidPayload);
        }

        Ok((offset, start, end))
    }
}

impl Default for VP9Depacketizer {
    fn default() -> Self {
        Self::new()
    }
}

impl VideoDepacketizer for VP9Depacketizer {
    fn push_packet(
        &mut self,
        payload: &[u8],
        marker: bool,
        sequence: u16,
        timestamp: u32,
    ) -> Result<Option<Vec<u8>>, PacketError> {
        let (offset, start, end) = Self::payload_offset(payload)?;
        self.state.begin_packet(sequence, timestamp);
        if self.state.is_discarding() {
            return Ok(if marker || end {
                self.state.finish_frame()
            } else {
                None
            });
        }
        if !start && self.state.frame.is_empty() {
            self.state.discard_frame();
            return Ok(if marker || end {
                self.state.finish_frame()
            } else {
                None
            });
        }

        self.state.frame.extend_from_slice(&payload[offset..]);
        Ok(if marker || end {
            self.state.finish_frame()
        } else {
            None
        })
    }
}

pub struct VP9Packetizer {
    state: PacketizerState,
}

#[derive(Clone, Copy)]
struct VP9FrameInfo {
    predicted: bool,
    resolution: Option<(u16, u16)>,
}

impl VP9Packetizer {
    pub fn new(ssrc: u32, payload_type: u8) -> Self {
        Self {
            state: PacketizerState::new(ssrc, payload_type),
        }
    }

    pub fn timestamp(&self) -> u32 {
        self.state.timestamp()
    }

    fn frame_info(frame: &[u8]) -> VP9FrameInfo {
        let mut reader = BitReader {
            data: frame,
            offset: 0,
        };
        let _frame_marker = match reader.read_bits(2) {
            Some(value) => value,
            None => {
                return VP9FrameInfo {
                    predicted: false,
                    resolution: None,
                };
            }
        };
        let profile_low = match reader.read_bit() {
            Some(value) => value,
            None => {
                return VP9FrameInfo {
                    predicted: false,
                    resolution: None,
                };
            }
        };
        let profile_high = match reader.read_bit() {
            Some(value) => value,
            None => {
                return VP9FrameInfo {
                    predicted: false,
                    resolution: None,
                };
            }
        };
        let profile = profile_low | (profile_high << 1);
        if profile == 3 && reader.read_bit().is_none() {
            return VP9FrameInfo {
                predicted: false,
                resolution: None,
            };
        }
        let show_existing_frame = match reader.read_bit() {
            Some(value) => value != 0,
            None => {
                return VP9FrameInfo {
                    predicted: false,
                    resolution: None,
                };
            }
        };
        if show_existing_frame {
            return VP9FrameInfo {
                predicted: true,
                resolution: None,
            };
        }
        let predicted = matches!(reader.read_bit(), Some(1));
        if predicted {
            return VP9FrameInfo {
                predicted,
                resolution: None,
            };
        }

        let _show_frame = reader.read_bit();
        let _error_resilient = reader.read_bit();
        if reader.read_bits(24) != Some(0x49_83_42) {
            return VP9FrameInfo {
                predicted,
                resolution: None,
            };
        }

        if !skip_vp9_color_config(&mut reader, profile) {
            return VP9FrameInfo {
                predicted,
                resolution: None,
            };
        }

        let width = reader
            .read_bits(16)
            .and_then(|value| u16::try_from(value.checked_add(1)?).ok());
        let height = reader
            .read_bits(16)
            .and_then(|value| u16::try_from(value.checked_add(1)?).ok());
        VP9FrameInfo {
            predicted,
            resolution: width.zip(height),
        }
    }

    fn make_payload(
        &self,
        chunk: &[u8],
        first: bool,
        last: bool,
        info: VP9FrameInfo,
        include_ss: bool,
    ) -> Vec<u8> {
        let mut payload = Vec::with_capacity(1 + if include_ss { 8 } else { 0 } + chunk.len());
        payload.push(
            if info.predicted { 0x40 } else { 0 }
                | if first { 0x08 } else { 0 }
                | if last { 0x04 } else { 0 }
                | if include_ss { 0x02 } else { 0 },
        );
        if let Some((width, height)) = info.resolution.filter(|_| include_ss) {
            payload.push(0x18);
            payload.extend_from_slice(&width.to_be_bytes());
            payload.extend_from_slice(&height.to_be_bytes());
            payload.push(1);
            payload.push(0x14);
            payload.push(1);
        }
        payload.extend_from_slice(chunk);
        payload
    }

    pub fn packetize_with_metadata_frame(
        &self,
        frame: &[u8],
        metadata_frame: &[u8],
        frame_time_ms: f64,
    ) -> Result<Vec<MediaPacket>, PacketError> {
        self.packetize_with_info(frame, Self::frame_info(metadata_frame), frame_time_ms)
    }

    fn packetize_with_info(
        &self,
        frame: &[u8],
        info: VP9FrameInfo,
        frame_time_ms: f64,
    ) -> Result<Vec<MediaPacket>, PacketError> {
        if frame.is_empty() {
            return Err(PacketError::EmptyFrame);
        }

        let ss_len = if info.resolution.is_some() { 8 } else { 0 };
        let max_chunk = self.state.mtu().saturating_sub(1 + ss_len);
        if max_chunk == 0 {
            return Err(PacketError::PayloadTooLarge);
        }

        let chunk_count = frame.len().div_ceil(max_chunk);
        let mut packets = Vec::with_capacity(chunk_count);
        for (index, chunk) in frame.chunks(max_chunk).enumerate() {
            let first = index == 0;
            let last = index + 1 == chunk_count;
            packets.push(self.state.packet_for_payload(
                self.make_payload(chunk, first, last, info, first && info.resolution.is_some()),
                last,
            ));
        }

        self.state.increment_timestamp(frame_time_ms);
        Ok(packets)
    }
}

struct BitReader<'a> {
    data: &'a [u8],
    offset: usize,
}

impl BitReader<'_> {
    fn read_bit(&mut self) -> Option<u8> {
        let byte = *self.data.get(self.offset / 8)?;
        let shift = 7 - (self.offset % 8);
        self.offset += 1;
        Some((byte >> shift) & 1)
    }

    fn read_bits(&mut self, count: usize) -> Option<u32> {
        let mut value = 0u32;
        for _ in 0..count {
            value = (value << 1) | u32::from(self.read_bit()?);
        }
        Some(value)
    }
}

fn skip_vp9_color_config(reader: &mut BitReader<'_>, profile: u8) -> bool {
    if profile >= 2 && reader.read_bit().is_none() {
        return false;
    }

    let color_space = match reader.read_bits(3) {
        Some(value) => value,
        None => return false,
    };
    if color_space == 7 {
        if profile == 1 || profile == 3 {
            reader.read_bit().is_some() && reader.read_bit().is_some()
        } else {
            true
        }
    } else if reader.read_bit().is_none() {
        false
    } else if profile == 1 || profile == 3 {
        reader.read_bit().is_some() && reader.read_bit().is_some() && reader.read_bit().is_some()
    } else {
        true
    }
}

fn skip_vp9_scalability_structure(payload: &[u8], offset: usize) -> Result<usize, PacketError> {
    if offset >= payload.len() {
        return Err(PacketError::InvalidPayload);
    }

    let descriptor = payload[offset];
    let spatial_layers = (descriptor >> 5) + 1;
    let has_resolution = descriptor & 0x10 != 0;
    let has_gof = descriptor & 0x08 != 0;
    let mut offset = offset + 1;

    if has_resolution {
        let bytes = spatial_layers as usize * 4;
        if offset + bytes > payload.len() {
            return Err(PacketError::InvalidPayload);
        }
        offset += bytes;
    }

    if has_gof {
        if offset >= payload.len() {
            return Err(PacketError::InvalidPayload);
        }
        let frames = payload[offset] as usize;
        offset += 1;
        for _ in 0..frames {
            if offset >= payload.len() {
                return Err(PacketError::InvalidPayload);
            }
            let frame_descriptor = payload[offset];
            let references = ((frame_descriptor >> 2) & 0x03) as usize;
            offset += 1 + references;
            if offset > payload.len() {
                return Err(PacketError::InvalidPayload);
            }
        }
    }

    Ok(offset)
}

impl VideoPacketizer for VP9Packetizer {
    fn packetize(&self, frame: &[u8], frame_time_ms: f64) -> Result<Vec<MediaPacket>, PacketError> {
        self.packetize_with_info(frame, Self::frame_info(frame), frame_time_ms)
    }
}
