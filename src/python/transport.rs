use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyTuple};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use std::{io, thread};

const RTP_ONE_BYTE_EXTENSION_PROFILE: [u8; 2] = [0xBE, 0xDE];
const RTP_EXT_TRANSMISSION_OFFSET: u8 = 2;
const RTP_EXT_ABS_SEND_TIME: u8 = 3;
const RTP_EXT_VIDEO_ORIENTATION: u8 = 4;
const RTP_EXT_TRANSPORT_SEQUENCE_NUMBER: u8 = 5;
const RTP_EXT_PLAYOUT_DELAY: u8 = 6;
const RTP_EXT_VIDEO_CONTENT_TYPE: u8 = 7;
const RTP_EXT_VIDEO_TIMING: u8 = 8;
const RTP_EXT_MEDIA_STREAM_TYPE: u8 = 10;
const RTP_EXT_RID: u8 = 11;
const RTP_EXT_REPAIRED_RID: u8 = 12;
const PRECISE_SLEEP_MARGIN: Duration = Duration::from_millis(1);
const PRECISE_SPIN_THRESHOLD: Duration = Duration::from_micros(250);

pub(super) type SharedTransportCrypto = Arc<Mutex<crate::transport::TransportCrypto>>;

struct DecryptedRTPPacket {
    index: usize,
    packet: crate::rtp::RtpPacket,
    extension_payload: Vec<u8>,
    media_payload: Vec<u8>,
}

pub(super) enum SendError {
    Crypto(crate::transport::CryptoError),
    Io(io::Error),
    Poisoned,
    Value(String),
}

impl From<crate::transport::CryptoError> for SendError {
    fn from(error: crate::transport::CryptoError) -> Self {
        Self::Crypto(error)
    }
}

impl From<io::Error> for SendError {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}

impl SendError {
    pub(super) fn into_pyerr(self) -> PyErr {
        match self {
            Self::Crypto(error) => error.into(),
            Self::Io(error) => pyo3::exceptions::PyOSError::new_err(error.to_string()),
            Self::Poisoned => {
                pyo3::exceptions::PyRuntimeError::new_err("Transport crypto lock poisoned")
            }
            Self::Value(message) => pyo3::exceptions::PyValueError::new_err(message),
        }
    }
}

pub(super) fn lock_crypto(
    inner: &SharedTransportCrypto,
) -> Result<std::sync::MutexGuard<'_, crate::transport::TransportCrypto>, SendError> {
    inner.lock().map_err(|_| SendError::Poisoned)
}

fn parse_transport_encryption_mode(
    mode: &str,
) -> PyResult<crate::transport::TransportEncryptionMode> {
    match mode {
        "aead_xchacha20_poly1305_rtpsize" => {
            Ok(crate::transport::TransportEncryptionMode::AeadXChaCha20Poly1305RtpSize)
        }
        "aead_aes256_gcm_rtpsize" => {
            Ok(crate::transport::TransportEncryptionMode::AeadAes256GcmRtpSize)
        }
        _ => Err(pyo3::exceptions::PyValueError::new_err(format!(
            "Unsupported transport encryption mode: {mode}"
        ))),
    }
}

fn strip_rtp_padding_native(payload: &mut Vec<u8>, padded: bool) -> Result<(), SendError> {
    if !padded {
        return Ok(());
    }

    let padding_len = payload
        .last()
        .copied()
        .ok_or_else(|| SendError::Value("RTP padding is missing a padding length".to_owned()))?
        as usize;
    if padding_len == 0 || padding_len > payload.len() {
        return Err(SendError::Value(
            "RTP padding length exceeds decrypted payload length".to_owned(),
        ));
    }

    payload.truncate(payload.len() - padding_len);
    Ok(())
}

fn rtp_extension_string(value: &str, fallback: &str) -> Vec<u8> {
    let encoded: Vec<u8> = value.bytes().filter(u8::is_ascii).take(16).collect();
    if !encoded.is_empty() {
        return encoded;
    }
    fallback.as_bytes()[..fallback.len().min(16)].to_vec()
}

fn abs_send_time() -> [u8; 3] {
    let seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs_f64())
        .unwrap_or(0.0);
    let value = (((seconds % 64.0) * 262_144.0) as u32) & 0xFF_FFFF;
    [
        ((value >> 16) & 0xFF) as u8,
        ((value >> 8) & 0xFF) as u8,
        (value & 0xFF) as u8,
    ]
}

fn push_one_byte_extension(payload: &mut Vec<u8>, id: u8, data: &[u8]) -> PyResult<()> {
    if !(1..=14).contains(&id) {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "RTP extension ID must be in [1, 14], got {id}"
        )));
    }
    if data.is_empty() || data.len() > 16 {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "RTP extension data length must be in [1, 16], got {}",
            data.len()
        )));
    }
    payload.push((id << 4) | ((data.len() - 1) as u8));
    payload.extend_from_slice(data);
    Ok(())
}

pub(super) fn build_video_extension_payload(
    transport_sequence: u16,
    rid: &str,
    media_stream_type: &str,
    repaired: bool,
) -> PyResult<Vec<u8>> {
    let mut payload = Vec::with_capacity(48);
    push_one_byte_extension(&mut payload, RTP_EXT_TRANSMISSION_OFFSET, &[0, 0, 0])?;
    push_one_byte_extension(&mut payload, RTP_EXT_ABS_SEND_TIME, &abs_send_time())?;
    push_one_byte_extension(&mut payload, RTP_EXT_VIDEO_ORIENTATION, &[0])?;
    push_one_byte_extension(
        &mut payload,
        RTP_EXT_TRANSPORT_SEQUENCE_NUMBER,
        &transport_sequence.to_be_bytes(),
    )?;
    push_one_byte_extension(&mut payload, RTP_EXT_PLAYOUT_DELAY, &[0, 0, 0])?;
    push_one_byte_extension(
        &mut payload,
        RTP_EXT_VIDEO_CONTENT_TYPE,
        &[if media_stream_type == "screen" { 1 } else { 0 }],
    )?;
    push_one_byte_extension(&mut payload, RTP_EXT_VIDEO_TIMING, &[0; 13])?;
    push_one_byte_extension(
        &mut payload,
        RTP_EXT_MEDIA_STREAM_TYPE,
        &rtp_extension_string(media_stream_type, "video"),
    )?;
    if repaired {
        push_one_byte_extension(
            &mut payload,
            RTP_EXT_REPAIRED_RID,
            &rtp_extension_string(rid, "100"),
        )?;
    } else {
        push_one_byte_extension(&mut payload, RTP_EXT_RID, &rtp_extension_string(rid, "100"))?;
    }

    let padding = (4 - (payload.len() % 4)) % 4;
    payload.extend(std::iter::repeat_n(0, padding));
    Ok(payload)
}

pub(super) fn wait_until(deadline: Instant) {
    loop {
        let now = Instant::now();
        if now >= deadline {
            return;
        }

        let remaining = deadline - now;
        if remaining > PRECISE_SLEEP_MARGIN {
            thread::sleep(remaining - PRECISE_SLEEP_MARGIN);
        } else if remaining > PRECISE_SPIN_THRESHOLD {
            thread::yield_now();
        } else {
            std::hint::spin_loop();
        }
    }
}

pub(super) fn header_with_one_byte_extensions(
    header: &[u8],
    extension_payload: &[u8],
) -> PyResult<Vec<u8>> {
    if header.len() < 12 {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "RTP header must be at least 12 bytes",
        ));
    }
    let mut header_buffer = header[..12].to_vec();
    header_buffer[0] |= 0x10;
    header_buffer.extend_from_slice(&RTP_ONE_BYTE_EXTENSION_PROFILE);
    header_buffer.extend_from_slice(&((extension_payload.len() / 4) as u16).to_be_bytes());
    Ok(header_buffer)
}

fn parse_rtp_extensions<'py>(
    py: Python<'py>,
    profile: u16,
    payload: &[u8],
) -> PyResult<Bound<'py, PyTuple>> {
    let mut extensions = Vec::new();
    if profile == 0xBEDE {
        parse_one_byte_extensions(py, payload, &mut extensions)?;
    } else if profile & 0xFFF0 == 0x1000 {
        parse_two_byte_extensions(py, payload, &mut extensions)?;
    }
    PyTuple::new(py, extensions)
}

fn parse_one_byte_extensions<'py>(
    py: Python<'py>,
    payload: &[u8],
    extensions: &mut Vec<(u8, Bound<'py, PyBytes>)>,
) -> PyResult<()> {
    let mut index = 0usize;

    while index < payload.len() {
        let header = payload[index];
        index += 1;
        if header == 0 {
            continue;
        }

        let extension_id = header >> 4;
        if extension_id == 15 {
            break;
        }

        let length = ((header & 0x0F) as usize) + 1;
        let end = index + length;
        if end > payload.len() {
            break;
        }

        extensions.push((extension_id, PyBytes::new(py, &payload[index..end])));
        index = end;
    }

    Ok(())
}

fn parse_two_byte_extensions<'py>(
    py: Python<'py>,
    payload: &[u8],
    extensions: &mut Vec<(u8, Bound<'py, PyBytes>)>,
) -> PyResult<()> {
    let mut index = 0usize;

    while index < payload.len() {
        let extension_id = payload[index];
        index += 1;
        if extension_id == 0 {
            continue;
        }
        if index >= payload.len() {
            break;
        }

        let length = payload[index] as usize;
        index += 1;
        let end = index + length;
        if end > payload.len() {
            break;
        }

        extensions.push((extension_id, PyBytes::new(py, &payload[index..end])));
        index = end;
    }

    Ok(())
}

fn decrypt_rtcp_payload(
    inner: SharedTransportCrypto,
    packet: &[u8],
    header_len: usize,
) -> Result<Vec<u8>, SendError> {
    if header_len == 0 || packet.len() < header_len + 4 {
        return Err(SendError::Value(
            "RTCP packet is too short for the requested header length".to_owned(),
        ));
    }

    let nonce_offset = packet.len() - 4;
    let mut nonce = [0u8; 4];
    nonce.copy_from_slice(&packet[nonce_offset..]);
    let header = packet[..header_len].to_vec();
    let encrypted_payload = packet[header_len..nonce_offset].to_vec();
    Ok(lock_crypto(&inner)?.decrypt_packet(&header, &encrypted_payload, &nonce)?)
}

fn decrypt_rtp_packet_inner(
    crypto: &mut crate::transport::TransportCrypto,
    index: usize,
    packet: crate::rtp::RtpPacket,
) -> Result<DecryptedRTPPacket, SendError> {
    let mut payload = crypto.decrypt_rtp(&packet)?;
    strip_rtp_padding_native(&mut payload, packet.padded)?;
    if packet.extension_payload_len > payload.len() {
        return Err(SendError::Value(
            "RTP extension length exceeds decrypted payload length".to_owned(),
        ));
    }

    let extension_payload_len = packet.extension_payload_len;
    let extension_payload = payload[..extension_payload_len].to_vec();
    let media_payload = payload[extension_payload_len..].to_vec();
    Ok(DecryptedRTPPacket {
        index,
        packet,
        extension_payload,
        media_payload,
    })
}

fn decrypt_rtp_packet_batch(
    inner: SharedTransportCrypto,
    packets: Vec<(usize, crate::rtp::RtpPacket)>,
) -> Result<(usize, Vec<DecryptedRTPPacket>), SendError> {
    let mut crypto = lock_crypto(&inner)?;
    let mut failed = 0usize;
    let mut decoded = Vec::with_capacity(packets.len());
    for (index, packet) in packets {
        match decrypt_rtp_packet_inner(&mut crypto, index, packet) {
            Ok(packet) => decoded.push(packet),
            Err(_) => failed += 1,
        }
    }
    Ok((failed, decoded))
}

fn parse_decrypted_rtcp_packets<'py>(
    py: Python<'py>,
    packet: &[u8],
    payload: &[u8],
    header_len: usize,
) -> PyResult<Bound<'py, PyTuple>> {
    if packet.len() < header_len || header_len < 8 {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "RTCP packet is too short for the requested header length",
        ));
    }

    let first_packet_length = (u16::from_be_bytes([packet[2], packet[3]]) as usize + 1) * 4;
    if first_packet_length < header_len {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "RTCP packet length is shorter than the protected header",
        ));
    }

    let first_payload_length = first_packet_length - header_len;
    if first_payload_length > payload.len() {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "RTCP payload is shorter than the compound packet length",
        ));
    }

    let mut packets = Vec::new();
    let sender_ssrc = u32::from_be_bytes([packet[4], packet[5], packet[6], packet[7]]);
    packets.push((
        packet[1],
        packet[0] & 0x1F,
        sender_ssrc,
        PyBytes::new(py, &payload[..first_payload_length]),
    ));

    let mut offset = first_payload_length;
    while offset + header_len <= payload.len() {
        let compound = &payload[offset..];
        if !crate::transport::udp::is_rtcp_packet(compound) {
            break;
        }

        let packet_length = (u16::from_be_bytes([compound[2], compound[3]]) as usize + 1) * 4;
        if packet_length < header_len || packet_length > compound.len() {
            break;
        }

        let sender_ssrc = u32::from_be_bytes([compound[4], compound[5], compound[6], compound[7]]);
        packets.push((
            compound[1],
            compound[0] & 0x1F,
            sender_ssrc,
            PyBytes::new(py, &compound[header_len..packet_length]),
        ));
        offset += packet_length;
    }

    PyTuple::new(py, packets)
}

#[pyclass(name = "TransportCrypto")]
pub(super) struct PyTransportCrypto {
    pub(super) inner: SharedTransportCrypto,
}

#[pymethods]
impl PyTransportCrypto {
    #[new]
    fn new(mode: &str, key: &Bound<'_, PyBytes>) -> PyResult<Self> {
        let mode = parse_transport_encryption_mode(mode)?;
        let inner = crate::transport::TransportCrypto::new(mode, key.as_bytes())?;
        Ok(Self {
            inner: Arc::new(Mutex::new(inner)),
        })
    }

    fn encrypt_rtp<'py>(
        &self,
        py: Python<'py>,
        header: &Bound<'_, PyBytes>,
        payload: &Bound<'_, PyBytes>,
    ) -> PyResult<Bound<'py, PyBytes>> {
        let inner = self.inner.clone();
        let header = header.as_bytes().to_vec();
        let payload = payload.as_bytes().to_vec();
        let packet = py
            .detach(move || Ok(lock_crypto(&inner)?.encrypt_rtp(&header, &payload)?))
            .map_err(SendError::into_pyerr)?;
        Ok(PyBytes::new(py, packet.as_slice()))
    }

    fn encrypt_rtcp<'py>(
        &self,
        py: Python<'py>,
        header: &Bound<'_, PyBytes>,
        payload: &Bound<'_, PyBytes>,
    ) -> PyResult<Bound<'py, PyBytes>> {
        let inner = self.inner.clone();
        let header = header.as_bytes().to_vec();
        let payload = payload.as_bytes().to_vec();
        let packet = py
            .detach(move || Ok(lock_crypto(&inner)?.encrypt_rtp(&header, &payload)?))
            .map_err(SendError::into_pyerr)?;
        Ok(PyBytes::new(py, packet.as_slice()))
    }

    fn decrypt_rtcp_packets<'py>(
        &self,
        py: Python<'py>,
        packet: &Bound<'_, PyBytes>,
        header_len: usize,
    ) -> PyResult<Bound<'py, PyTuple>> {
        let packet = packet.as_bytes().to_vec();
        let packet_for_decrypt = packet.clone();
        let inner = self.inner.clone();
        let payload = py
            .detach(move || decrypt_rtcp_payload(inner, &packet_for_decrypt, header_len))
            .map_err(SendError::into_pyerr)?;
        parse_decrypted_rtcp_packets(py, packet.as_slice(), payload.as_slice(), header_len)
    }

    fn decrypt_rtp_packets<'py>(
        &self,
        py: Python<'py>,
        packets: &Bound<'_, PyAny>,
    ) -> PyResult<(usize, Bound<'py, PyTuple>)> {
        let mut parsed_packets = Vec::new();
        let mut parse_failed = 0usize;
        for (index, item) in packets.try_iter()?.enumerate() {
            let item = item?;
            let packet = item.cast::<PyBytes>()?;
            match crate::rtp::RtpPacket::parse(packet.as_bytes()) {
                Ok(packet) => parsed_packets.push((index, packet)),
                Err(_) => parse_failed += 1,
            }
        }

        let inner = self.inner.clone();
        let (decrypt_failed, decoded) = py
            .detach(move || decrypt_rtp_packet_batch(inner, parsed_packets))
            .map_err(SendError::into_pyerr)?;
        let failed = parse_failed + decrypt_failed;

        let mut output = Vec::with_capacity(decoded.len());
        for decoded in decoded {
            let packet = decoded.packet;
            output.push((
                decoded.index,
                packet.payload_type,
                packet.marker,
                packet.padded,
                packet.sequence,
                packet.timestamp,
                packet.ssrc,
                packet.extended,
                PyBytes::new(py, decoded.extension_payload.as_slice()),
                parse_rtp_extensions(
                    py,
                    packet.extension_profile,
                    decoded.extension_payload.as_slice(),
                )?,
                PyBytes::new(py, decoded.media_payload.as_slice()),
            ));
        }

        Ok((failed, PyTuple::new(py, output)?))
    }
}

pub(super) fn add_to_module(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyTransportCrypto>()?;
    Ok(())
}
