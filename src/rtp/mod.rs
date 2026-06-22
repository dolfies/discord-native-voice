use std::fmt;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PacketError {
    TooShort,
    InvalidVersion(u8),
    TruncatedExtension,
    PayloadTooLarge,
    EmptyFrame,
    UnsupportedCodec,
    InvalidPayload,
}

impl fmt::Display for PacketError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::TooShort => write!(f, "Packet is too short"),
            Self::InvalidVersion(version) => write!(f, "Invalid RTP version {version}"),
            Self::TruncatedExtension => write!(f, "RTP extension header is truncated"),
            Self::PayloadTooLarge => write!(f, "Payload is larger than the configured MTU"),
            Self::EmptyFrame => write!(f, "Media frame is empty"),
            Self::UnsupportedCodec => write!(f, "Unsupported codec"),
            Self::InvalidPayload => write!(f, "Invalid media payload"),
        }
    }
}

impl std::error::Error for PacketError {}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RtpPacket {
    pub header: Vec<u8>,
    pub encrypted_payload: Vec<u8>,
    pub nonce_suffix: [u8; 4],
    pub extension_payload_len: usize,
    pub payload_type: u8,
    pub marker: bool,
    pub padded: bool,
    pub sequence: u16,
    pub timestamp: u32,
    pub ssrc: u32,
    pub extended: bool,
    pub extension_profile: u16,
}

impl RtpPacket {
    pub fn new_header(
        payload_type: u8,
        sequence: u16,
        timestamp: u32,
        ssrc: u32,
        marker: bool,
        extended: bool,
    ) -> Vec<u8> {
        let mut header = Vec::with_capacity(if extended { 16 } else { 12 });
        header.push(0x80 | if extended { 0x10 } else { 0 });
        header.push(payload_type | if marker { 0x80 } else { 0 });
        header.extend_from_slice(&sequence.to_be_bytes());
        header.extend_from_slice(&timestamp.to_be_bytes());
        header.extend_from_slice(&ssrc.to_be_bytes());

        if extended {
            header.extend_from_slice(&[0xBE, 0xDE, 0x00, 0x01]);
        }

        header
    }

    pub fn parse(data: &[u8]) -> Result<Self, PacketError> {
        if data.len() < 12 + 4 {
            return Err(PacketError::TooShort);
        }

        let version = data[0] >> 6;
        if version != 2 {
            return Err(PacketError::InvalidVersion(version));
        }

        let csrc_count = (data[0] & 0x0F) as usize;
        let extended = data[0] & 0x10 != 0;
        let mut header_len = 12 + csrc_count * 4;
        let mut extension_payload_len = 0;
        let mut extension_profile = 0;
        if data.len() < header_len + 4 {
            return Err(PacketError::TooShort);
        }

        if extended {
            if data.len() < header_len + 4 {
                return Err(PacketError::TruncatedExtension);
            }
            extension_profile = u16::from_be_bytes([data[header_len], data[header_len + 1]]);
            extension_payload_len =
                u16::from_be_bytes([data[header_len + 2], data[header_len + 3]]) as usize * 4;
            header_len += 4;
            if data.len() < header_len + extension_payload_len + 4 {
                return Err(PacketError::TruncatedExtension);
            }
        }

        if data.len() < header_len + 4 {
            return Err(PacketError::TooShort);
        }

        let nonce_offset = data.len() - 4;
        let mut nonce_suffix = [0u8; 4];
        nonce_suffix.copy_from_slice(&data[nonce_offset..]);

        Ok(Self {
            header: data[..header_len].to_vec(),
            encrypted_payload: data[header_len..nonce_offset].to_vec(),
            nonce_suffix,
            extension_payload_len,
            payload_type: data[1] & 0x7F,
            marker: data[1] & 0x80 != 0,
            padded: data[0] & 0x20 != 0,
            sequence: u16::from_be_bytes([data[2], data[3]]),
            timestamp: u32::from_be_bytes([data[4], data[5], data[6], data[7]]),
            ssrc: u32::from_be_bytes([data[8], data[9], data[10], data[11]]),
            extended,
            extension_profile,
        })
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MediaPacket {
    pub header: Vec<u8>,
    pub payload: Vec<u8>,
    pub marker: bool,
    pub sequence: u16,
    pub timestamp: u32,
    pub ssrc: u32,
    pub payload_type: u8,
}
