use crate::rtp::{MediaPacket, PacketError};

use super::common::{DepacketizerState, PacketizerState};
use super::{VideoDepacketizer, VideoPacketizer};

const AGGREGATION_HEADER_SIZE: usize = 1;
const MAX_NUM_OBUS_TO_OMIT_SIZE: usize = 3;
const OBU_SIZE_PRESENT_BIT: u8 = 0x02;
const OBU_TYPE_SEQUENCE_HEADER: u8 = 1;
const OBU_TYPE_TEMPORAL_DELIMITER: u8 = 2;
const OBU_TYPE_TILE_LIST: u8 = 8;
const OBU_TYPE_PADDING: u8 = 15;
const BYTES_OVERHEAD_EVEN_DISTRIBUTION: usize = 1;
const MIN_BYTES_SAVED_PER_PACKET_WITH_EVEN_DISTRIBUTION: usize = 10;

#[derive(Debug, Clone)]
struct AV1Obu {
    header: u8,
    extension_header: Option<u8>,
    payload: Vec<u8>,
}

impl AV1Obu {
    fn size(&self) -> usize {
        1 + usize::from(self.extension_header.is_some()) + self.payload.len()
    }

    fn obu_type(&self) -> u8 {
        obu_type(self.header)
    }

    fn write_rtp_fragment(
        &self,
        offset: usize,
        fragment_size: usize,
        output: &mut Vec<u8>,
    ) -> Result<(), PacketError> {
        let end = offset
            .checked_add(fragment_size)
            .ok_or(PacketError::InvalidPayload)?;
        if end > self.size() {
            return Err(PacketError::InvalidPayload);
        }

        let mut cursor = 0;
        if offset <= cursor && end > cursor {
            output.push(self.header & !OBU_SIZE_PRESENT_BIT);
        }
        cursor += 1;

        if let Some(extension_header) = self.extension_header {
            if offset <= cursor && end > cursor {
                output.push(extension_header);
            }
            cursor += 1;
        }

        let payload_start = offset.saturating_sub(cursor);
        let payload_end = end.saturating_sub(cursor).min(self.payload.len());
        if payload_start < payload_end {
            output.extend_from_slice(&self.payload[payload_start..payload_end]);
        }

        Ok(())
    }
}

#[derive(Debug, Clone)]
struct AV1PayloadSizeLimits {
    max_payload_len: usize,
    first_packet_reduction_len: usize,
    last_packet_reduction_len: usize,
    single_packet_reduction_len: usize,
}

#[derive(Debug, Clone)]
struct AV1Packet {
    first_obu: usize,
    first_obu_offset: usize,
    num_obu_elements: usize,
    last_obu_size: usize,
    packet_size: usize,
}

impl AV1Packet {
    fn new(first_obu: usize) -> Self {
        Self {
            first_obu,
            first_obu_offset: 0,
            num_obu_elements: 0,
            last_obu_size: 0,
            packet_size: 0,
        }
    }
}

pub struct AV1Depacketizer {
    state: DepacketizerState,
    obu: Vec<u8>,
}

impl AV1Depacketizer {
    pub fn new() -> Self {
        Self {
            state: DepacketizerState::default(),
            obu: Vec::new(),
        }
    }

    fn push_payload(
        &mut self,
        payload: &[u8],
        marker: bool,
    ) -> Result<Option<Vec<u8>>, PacketError> {
        if payload.is_empty() {
            return Err(PacketError::InvalidPayload);
        }

        let aggregation_header = payload[0];
        let starts_with_fragment = aggregation_header & 0x80 != 0;
        let ends_with_fragment = aggregation_header & 0x40 != 0;
        let w = (aggregation_header >> 4) & 0x03;
        let mut offset = 1;

        if aggregation_header & 0x08 != 0 && starts_with_fragment {
            return Err(PacketError::InvalidPayload);
        }

        let mut element_index = 0;
        while offset < payload.len() {
            element_index += 1;
            let has_size = w == 0 || element_index != w;
            let element_len = if has_size {
                let (obu_len, leb_len) = read_leb128(&payload[offset..])?;
                offset += leb_len;
                if offset + obu_len > payload.len() {
                    return Err(PacketError::InvalidPayload);
                }
                obu_len
            } else {
                payload.len() - offset
            };

            let element = &payload[offset..offset + element_len];
            offset += element_len;
            let is_first_element = element_index == 1;
            let is_last_element = offset == payload.len();
            let continues_previous = is_first_element && starts_with_fragment;
            let continues_next = is_last_element && ends_with_fragment;

            if continues_previous && self.obu.is_empty() {
                self.state.discard_frame();
                return Ok(if marker { self.finish_frame() } else { None });
            }

            if !continues_previous && !self.obu.is_empty() {
                return Err(PacketError::InvalidPayload);
            }

            self.obu.extend_from_slice(element);
            if !continues_next {
                self.finish_obu(marker && is_last_element)?;
            }
        }

        if w != 0 && element_index != w {
            return Err(PacketError::InvalidPayload);
        }

        if marker && ends_with_fragment {
            return Err(PacketError::InvalidPayload);
        }

        Ok(if marker { self.finish_frame() } else { None })
    }

    fn finish_obu(&mut self, omit_size: bool) -> Result<(), PacketError> {
        if self.obu.is_empty() {
            return Ok(());
        }

        let rebuilt = rebuild_obu(&self.obu, omit_size)?;
        self.state.frame.extend_from_slice(&rebuilt);
        self.obu.clear();
        Ok(())
    }

    fn finish_frame(&mut self) -> Option<Vec<u8>> {
        self.obu.clear();
        self.state.finish_frame()
    }
}

impl Default for AV1Depacketizer {
    fn default() -> Self {
        Self::new()
    }
}

impl VideoDepacketizer for AV1Depacketizer {
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
            return Ok(if marker { self.finish_frame() } else { None });
        }
        self.push_payload(payload, marker)
    }
}

pub struct AV1Packetizer {
    state: PacketizerState,
}

impl AV1Packetizer {
    pub fn new(ssrc: u32, payload_type: u8) -> Self {
        Self {
            state: PacketizerState::new(ssrc, payload_type),
        }
    }

    pub fn timestamp(&self) -> u32 {
        self.state.timestamp()
    }

    fn payload_limits(&self) -> Result<AV1PayloadSizeLimits, PacketError> {
        let max_payload_len = self.state.mtu();
        if max_payload_len.saturating_sub(AGGREGATION_HEADER_SIZE) < 3 {
            return Err(PacketError::PayloadTooLarge);
        }

        Ok(AV1PayloadSizeLimits {
            max_payload_len,
            first_packet_reduction_len: 0,
            last_packet_reduction_len: 0,
            single_packet_reduction_len: 0,
        })
    }

    fn packetize_internal(
        &self,
        obus: &[AV1Obu],
        mut limits: AV1PayloadSizeLimits,
    ) -> Result<Vec<AV1Packet>, PacketError> {
        let mut packets = Vec::new();
        if obus.is_empty() {
            return Ok(packets);
        }

        if limits
            .max_payload_len
            .saturating_sub(limits.last_packet_reduction_len)
            < 3
            || limits
                .max_payload_len
                .saturating_sub(limits.first_packet_reduction_len)
                < 3
        {
            return Err(PacketError::PayloadTooLarge);
        }

        limits.max_payload_len -= AGGREGATION_HEADER_SIZE;

        packets.push(AV1Packet::new(0));
        let mut packet_remaining_bytes = limits
            .max_payload_len
            .saturating_sub(limits.first_packet_reduction_len);

        for (obu_index, obu) in obus.iter().enumerate() {
            let is_last_obu = obu_index == obus.len() - 1;
            let packet = packets.last().ok_or(PacketError::PayloadTooLarge)?;
            let mut previous_obu_extra_size = additional_bytes_for_previous_obu_element(packet);
            let min_required_size = if packet.num_obu_elements >= MAX_NUM_OBUS_TO_OMIT_SIZE {
                2
            } else {
                1
            };

            if packet_remaining_bytes < previous_obu_extra_size + min_required_size {
                packets.push(AV1Packet::new(obu_index));
                packet_remaining_bytes = limits.max_payload_len;
                previous_obu_extra_size = 0;
            }

            let packet = packets.last_mut().ok_or(PacketError::PayloadTooLarge)?;
            packet.packet_size += previous_obu_extra_size;
            packet_remaining_bytes -= previous_obu_extra_size;
            packet.num_obu_elements += 1;

            let must_write_obu_element_size = packet.num_obu_elements > MAX_NUM_OBUS_TO_OMIT_SIZE;
            let mut required_bytes = obu.size();
            if must_write_obu_element_size {
                required_bytes += leb128_size(obu.size());
            }

            let mut available_bytes = packet_remaining_bytes;
            if is_last_obu {
                if packets.len() == 1 {
                    available_bytes += limits.first_packet_reduction_len;
                    available_bytes =
                        available_bytes.saturating_sub(limits.single_packet_reduction_len);
                } else {
                    available_bytes =
                        available_bytes.saturating_sub(limits.last_packet_reduction_len);
                }
            }

            if required_bytes <= available_bytes {
                let packet = packets.last_mut().ok_or(PacketError::PayloadTooLarge)?;
                packet.last_obu_size = obu.size();
                packet.packet_size += required_bytes;
                packet_remaining_bytes -= required_bytes;
                continue;
            }

            let max_first_fragment_size = if must_write_obu_element_size {
                max_fragment_size(packet_remaining_bytes)
            } else {
                packet_remaining_bytes
            };
            let first_fragment_size = (obu.size() - 1).min(max_first_fragment_size);

            let packet = packets.last_mut().ok_or(PacketError::PayloadTooLarge)?;
            if first_fragment_size == 0 {
                packet.num_obu_elements -= 1;
                packet.packet_size -= previous_obu_extra_size;
            } else {
                packet.packet_size += first_fragment_size;
                if must_write_obu_element_size {
                    packet.packet_size += leb128_size(first_fragment_size);
                }
                packet.last_obu_size = first_fragment_size;
            }

            let mut obu_offset = first_fragment_size;
            while obu_offset + limits.max_payload_len < obu.size() {
                let mut middle_packet = AV1Packet::new(obu_index);
                middle_packet.num_obu_elements = 1;
                middle_packet.first_obu_offset = obu_offset;
                middle_packet.last_obu_size = limits.max_payload_len;
                middle_packet.packet_size = limits.max_payload_len;
                packets.push(middle_packet);
                obu_offset += limits.max_payload_len;
            }

            let mut last_fragment_size = obu.size() - obu_offset;
            if is_last_obu
                && last_fragment_size
                    > limits
                        .max_payload_len
                        .saturating_sub(limits.last_packet_reduction_len)
            {
                if last_fragment_size < 2 {
                    return Err(PacketError::PayloadTooLarge);
                }

                let mut semi_last_fragment_size =
                    (last_fragment_size + limits.last_packet_reduction_len) / 2;
                if semi_last_fragment_size >= last_fragment_size {
                    semi_last_fragment_size = last_fragment_size - 1;
                }
                last_fragment_size -= semi_last_fragment_size;

                let mut second_last_packet = AV1Packet::new(obu_index);
                second_last_packet.num_obu_elements = 1;
                second_last_packet.first_obu_offset = obu_offset;
                second_last_packet.last_obu_size = semi_last_fragment_size;
                second_last_packet.packet_size = semi_last_fragment_size;
                packets.push(second_last_packet);
                obu_offset += semi_last_fragment_size;
            }

            let mut last_packet = AV1Packet::new(obu_index);
            last_packet.num_obu_elements = 1;
            last_packet.first_obu_offset = obu_offset;
            last_packet.last_obu_size = last_fragment_size;
            last_packet.packet_size = last_fragment_size;
            packets.push(last_packet);
            packet_remaining_bytes = limits.max_payload_len - last_fragment_size;
        }

        Ok(packets)
    }

    fn packetize_obus(&self, obus: &[AV1Obu]) -> Result<Vec<AV1Packet>, PacketError> {
        let limits = self.payload_limits()?;
        let packets = self.packetize_internal(obus, limits.clone())?;
        if packets.len() <= 1 {
            return Ok(packets);
        }

        let mut unused_packet_capacity = 0;
        for (packet_index, packet) in packets.iter().enumerate() {
            let mut available_bytes = limits.max_payload_len - AGGREGATION_HEADER_SIZE;
            if packet_index == 0 {
                available_bytes = available_bytes.saturating_sub(limits.first_packet_reduction_len);
            } else if packet_index == packets.len() - 1 {
                available_bytes = available_bytes.saturating_sub(limits.last_packet_reduction_len);
            }
            if available_bytes >= packet.packet_size {
                unused_packet_capacity += available_bytes - packet.packet_size;
            }
        }

        if unused_packet_capacity
            > packets.len() * MIN_BYTES_SAVED_PER_PACKET_WITH_EVEN_DISTRIBUTION
        {
            let size_reduction = unused_packet_capacity / packets.len();
            if limits.max_payload_len > size_reduction
                && size_reduction > BYTES_OVERHEAD_EVEN_DISTRIBUTION
            {
                let mut reduced_limits = limits.clone();
                reduced_limits.max_payload_len -= size_reduction - BYTES_OVERHEAD_EVEN_DISTRIBUTION;
                if reduced_limits
                    .max_payload_len
                    .saturating_sub(reduced_limits.last_packet_reduction_len)
                    >= 3
                    && reduced_limits
                        .max_payload_len
                        .saturating_sub(reduced_limits.first_packet_reduction_len)
                        >= 3
                {
                    let even_packets = self.packetize_internal(obus, reduced_limits)?;
                    if even_packets.len() == packets.len() {
                        return Ok(even_packets);
                    }
                }
            }
        }

        Ok(packets)
    }

    fn aggregation_header(&self, obus: &[AV1Obu], packet: &AV1Packet, packet_index: usize) -> u8 {
        let mut aggregation_header = 0;

        if packet.first_obu_offset > 0 {
            aggregation_header |= 0x80;
        }

        let last_obu_index = packet.first_obu + packet.num_obu_elements - 1;
        let last_obu_offset = if packet.num_obu_elements == 1 {
            packet.first_obu_offset
        } else {
            0
        };
        if last_obu_offset + packet.last_obu_size < obus[last_obu_index].size() {
            aggregation_header |= 0x40;
        }

        if packet.num_obu_elements <= MAX_NUM_OBUS_TO_OMIT_SIZE {
            aggregation_header |= (packet.num_obu_elements as u8) << 4;
        }

        if packet_index == 0 && obus[0].obu_type() == OBU_TYPE_SEQUENCE_HEADER {
            aggregation_header |= 0x08;
        }

        aggregation_header
    }

    fn write_packet_payload(
        &self,
        obus: &[AV1Obu],
        packet: &AV1Packet,
        packet_index: usize,
    ) -> Result<Vec<u8>, PacketError> {
        if packet.num_obu_elements == 0 {
            return Err(PacketError::InvalidPayload);
        }

        let mut payload = Vec::with_capacity(AGGREGATION_HEADER_SIZE + packet.packet_size);
        payload.push(self.aggregation_header(obus, packet, packet_index));

        let mut obu_offset = packet.first_obu_offset;
        for index in 0..packet.num_obu_elements - 1 {
            let obu = &obus[packet.first_obu + index];
            let fragment_size = obu.size() - obu_offset;
            write_leb128(fragment_size, &mut payload);
            obu.write_rtp_fragment(obu_offset, fragment_size, &mut payload)?;
            obu_offset = 0;
        }

        let last_obu = &obus[packet.first_obu + packet.num_obu_elements - 1];
        let fragment_size = packet.last_obu_size;
        if packet.num_obu_elements > MAX_NUM_OBUS_TO_OMIT_SIZE {
            write_leb128(fragment_size, &mut payload);
        }
        last_obu.write_rtp_fragment(obu_offset, fragment_size, &mut payload)?;

        if payload.len() != AGGREGATION_HEADER_SIZE + packet.packet_size {
            return Err(PacketError::InvalidPayload);
        }
        Ok(payload)
    }
}

fn additional_bytes_for_previous_obu_element(packet: &AV1Packet) -> usize {
    if packet.packet_size == 0 || packet.num_obu_elements > MAX_NUM_OBUS_TO_OMIT_SIZE {
        return 0;
    }
    leb128_size(packet.last_obu_size)
}

fn max_fragment_size(remaining_bytes: usize) -> usize {
    if remaining_bytes <= 1 {
        return 0;
    }

    let mut bytes = 1;
    loop {
        let threshold = (1usize << (7 * bytes)) + bytes;
        if remaining_bytes < threshold {
            return remaining_bytes - bytes;
        }
        bytes += 1;
    }
}

fn leb128_size(mut value: usize) -> usize {
    let mut size = 1;
    while value >= 0x80 {
        value >>= 7;
        size += 1;
    }
    size
}

impl VideoPacketizer for AV1Packetizer {
    fn packetize(&self, frame: &[u8], frame_time_ms: f64) -> Result<Vec<MediaPacket>, PacketError> {
        if frame.is_empty() {
            return Err(PacketError::EmptyFrame);
        }

        let obus = parse_av1_obus(frame)?;
        if obus.is_empty() {
            return Err(PacketError::EmptyFrame);
        }

        let mut packets = Vec::new();
        let av1_packets = self.packetize_obus(&obus)?;
        let av1_packet_count = av1_packets.len();
        for (index, packet) in av1_packets.iter().enumerate() {
            let payload = self.write_packet_payload(&obus, packet, index)?;
            packets.push(
                self.state
                    .packet_for_payload(payload, index + 1 == av1_packet_count),
            );
        }

        self.state.increment_timestamp(frame_time_ms);
        Ok(packets)
    }
}

fn obu_type(header: u8) -> u8 {
    (header >> 3) & 0x0F
}

fn obu_has_extension(header: u8) -> bool {
    header & 0x04 != 0
}

fn obu_has_size(header: u8) -> bool {
    header & OBU_SIZE_PRESENT_BIT != 0
}

fn should_transmit_obu(header: u8) -> bool {
    !matches!(
        obu_type(header),
        OBU_TYPE_TEMPORAL_DELIMITER | OBU_TYPE_TILE_LIST | OBU_TYPE_PADDING
    )
}

fn parse_av1_obus(frame: &[u8]) -> Result<Vec<AV1Obu>, PacketError> {
    if frame.is_empty() {
        return Err(PacketError::EmptyFrame);
    }

    let mut obus = Vec::new();
    let mut offset = 0;

    while offset < frame.len() {
        let header = frame[offset];
        offset += 1;

        let extension_header = if obu_has_extension(header) {
            if offset >= frame.len() {
                return Err(PacketError::InvalidPayload);
            }
            let extension_header = frame[offset];
            offset += 1;
            Some(extension_header)
        } else {
            None
        };

        let payload_len = if obu_has_size(header) {
            let (payload_len, leb_len) = read_leb128(&frame[offset..])?;
            offset += leb_len;
            if offset + payload_len > frame.len() {
                return Err(PacketError::InvalidPayload);
            }
            payload_len
        } else {
            frame.len() - offset
        };

        let payload = frame[offset..offset + payload_len].to_vec();
        offset += payload_len;

        if should_transmit_obu(header) {
            obus.push(AV1Obu {
                header,
                extension_header,
                payload,
            });
        }
    }

    Ok(obus)
}

fn read_leb128(data: &[u8]) -> Result<(usize, usize), PacketError> {
    let mut value = 0usize;
    let mut shift = 0usize;

    for (index, byte) in data.iter().copied().enumerate() {
        let low = (byte & 0x7F) as usize;
        if shift >= usize::BITS as usize || (low << shift) >> shift != low {
            return Err(PacketError::InvalidPayload);
        }
        value |= low << shift;
        if byte & 0x80 == 0 {
            return Ok((value, index + 1));
        }
        shift += 7;
    }

    Err(PacketError::InvalidPayload)
}

fn write_leb128(mut value: usize, output: &mut Vec<u8>) {
    while value >= 0x80 {
        output.push(0x80 | (value as u8 & 0x7F));
        value >>= 7;
    }
    output.push(value as u8);
}

fn rebuild_obu(obu: &[u8], omit_size: bool) -> Result<Vec<u8>, PacketError> {
    let (&header, rest) = obu.split_first().ok_or(PacketError::InvalidPayload)?;
    let mut offset = 0;
    let extension_header = if obu_has_extension(header) {
        let value = rest.first().copied().ok_or(PacketError::InvalidPayload)?;
        offset = 1;
        Some(value)
    } else {
        None
    };

    let payload = if obu_has_size(header) {
        let (payload_len, leb_len) = read_leb128(&rest[offset..])?;
        offset += leb_len;
        if offset + payload_len != rest.len() {
            return Err(PacketError::InvalidPayload);
        }
        &rest[offset..]
    } else {
        &rest[offset..]
    };

    let mut rebuilt =
        Vec::with_capacity(1 + usize::from(extension_header.is_some()) + 5 + payload.len());
    rebuilt.push(if omit_size {
        header & !OBU_SIZE_PRESENT_BIT
    } else {
        header | OBU_SIZE_PRESENT_BIT
    });
    if let Some(extension_header) = extension_header {
        rebuilt.push(extension_header);
    }
    if !omit_size {
        write_leb128(payload.len(), &mut rebuilt);
    }
    rebuilt.extend_from_slice(payload);
    Ok(rebuilt)
}
