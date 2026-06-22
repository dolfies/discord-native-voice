use crate::rtp::PacketError;

pub(super) const ANNEX_B_START_CODE: [u8; 4] = [0, 0, 0, 1];

pub(super) fn split_annex_b(frame: &[u8]) -> Result<Vec<Vec<u8>>, PacketError> {
    if frame.is_empty() {
        return Err(PacketError::EmptyFrame);
    }

    let mut starts = Vec::new();
    let mut i = 0;
    while i + 3 <= frame.len() {
        if i + 4 <= frame.len() && frame[i..i + 4] == [0, 0, 0, 1] {
            starts.push((i, 4));
            i += 4;
        } else if frame[i..i + 3] == [0, 0, 1] {
            starts.push((i, 3));
            i += 3;
        } else {
            i += 1;
        }
    }

    if starts.is_empty() {
        return Ok(vec![frame.to_vec()]);
    }

    let mut nalus = Vec::new();
    for (index, (start, prefix_len)) in starts.iter().copied().enumerate() {
        let payload_start = start + prefix_len;
        let payload_end = starts
            .get(index + 1)
            .map(|(next, _)| *next)
            .unwrap_or(frame.len());
        if payload_start < payload_end {
            nalus.push(frame[payload_start..payload_end].to_vec());
        }
    }

    if nalus.is_empty() {
        Err(PacketError::EmptyFrame)
    } else {
        Ok(nalus)
    }
}
