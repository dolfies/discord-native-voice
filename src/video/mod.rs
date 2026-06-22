mod av1;
mod common;
mod h264;
mod h265;
mod h26x;
mod vp8;
mod vp9;

pub use av1::{AV1Depacketizer, AV1Packetizer};
pub(crate) use common::random_u16;
pub use h264::{H264Depacketizer, H264Packetizer};
pub use h265::{H265Depacketizer, H265Packetizer};
pub use vp8::{VP8Depacketizer, VP8Packetizer};
pub use vp9::{VP9Depacketizer, VP9Packetizer};

use crate::rtp::{MediaPacket, PacketError};

pub trait VideoPacketizer {
    fn packetize(&self, frame: &[u8], frame_time_ms: f64) -> Result<Vec<MediaPacket>, PacketError>;
}

pub trait VideoDepacketizer {
    fn push_packet(
        &mut self,
        payload: &[u8],
        marker: bool,
        sequence: u16,
        timestamp: u32,
    ) -> Result<Option<Vec<u8>>, PacketError>;
}
