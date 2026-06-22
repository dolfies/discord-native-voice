use std::io;
use std::net::UdpSocket;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

const IP_DISCOVERY_PACKET_SIZE: usize = 74;
const IP_DISCOVERY_PAYLOAD_LEN: u16 = 70;
const IP_DISCOVERY_SEND: u16 = 1;
const IP_DISCOVERY_RESPONSE: u16 = 2;
const NATIVE_PING_RESPONSE: u32 = 0xF00D_1337;
const NATIVE_PING_REQUEST: u32 = 0xCAFE_1337;
const NATIVE_PING_SIZE: usize = 8;

pub fn native_ping_request(sequence: u32) -> [u8; NATIVE_PING_SIZE] {
    let mut packet = [0u8; NATIVE_PING_SIZE];
    packet[..4].copy_from_slice(&NATIVE_PING_REQUEST.to_le_bytes());
    packet[4..].copy_from_slice(&sequence.to_le_bytes());
    packet
}

pub fn native_ping_response_sequence(packet: &[u8]) -> Option<u32> {
    if packet.len() < NATIVE_PING_SIZE {
        return None;
    }

    let magic = u32::from_le_bytes(packet[..4].try_into().ok()?);
    if magic != NATIVE_PING_RESPONSE {
        return None;
    }

    Some(u32::from_le_bytes(packet[4..8].try_into().ok()?))
}

fn ip_discovery_request(ssrc: u32) -> [u8; IP_DISCOVERY_PACKET_SIZE] {
    let mut packet = [0u8; IP_DISCOVERY_PACKET_SIZE];
    packet[..2].copy_from_slice(&IP_DISCOVERY_SEND.to_be_bytes());
    packet[2..4].copy_from_slice(&IP_DISCOVERY_PAYLOAD_LEN.to_be_bytes());
    packet[4..8].copy_from_slice(&ssrc.to_be_bytes());
    packet
}

pub fn discover_ip_socket(
    socket: &UdpSocket,
    ssrc: u32,
    timeout: Duration,
) -> io::Result<(String, u16)> {
    let packet = ip_discovery_request(ssrc);
    socket.send(&packet)?;

    let started = Instant::now();
    let mut recv = [0u8; 2048];
    loop {
        match socket.recv(&mut recv) {
            Ok(length) => {
                if let Some(result) = parse_ip_discovery_response(&recv[..length], ssrc)? {
                    return Ok(result);
                }
            }
            Err(err)
                if err.kind() == io::ErrorKind::WouldBlock
                    || err.kind() == io::ErrorKind::Interrupted
                    || err.kind() == io::ErrorKind::TimedOut => {}
            Err(err) => return Err(err),
        }

        if started.elapsed() >= timeout {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "Timed out waiting for UDP IP discovery response",
            ));
        }
        thread::sleep(Duration::from_millis(1));
    }
}

pub fn parse_ip_discovery_response(packet: &[u8], ssrc: u32) -> io::Result<Option<(String, u16)>> {
    if packet.len() != IP_DISCOVERY_PACKET_SIZE {
        return Ok(None);
    }
    let packet_type = u16::from_be_bytes(packet[..2].try_into().expect("slice length checked"));
    let payload_len = u16::from_be_bytes(packet[2..4].try_into().expect("slice length checked"));
    let packet_ssrc = u32::from_be_bytes(packet[4..8].try_into().expect("slice length checked"));
    if packet_type != IP_DISCOVERY_RESPONSE
        || payload_len != IP_DISCOVERY_PAYLOAD_LEN
        || packet_ssrc != ssrc
    {
        return Ok(None);
    }

    let ip_bytes = &packet[8..72];
    let ip_len = ip_bytes
        .iter()
        .position(|byte| *byte == 0)
        .unwrap_or(ip_bytes.len());
    let ip = std::str::from_utf8(&ip_bytes[..ip_len])
        .map_err(|_| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "UDP IP discovery returned non-ASCII IP",
            )
        })?
        .to_owned();
    let port = u16::from_be_bytes(
        packet[IP_DISCOVERY_PACKET_SIZE - 2..]
            .try_into()
            .expect("slice length checked"),
    );
    Ok(Some((ip, port)))
}

pub fn is_rtcp_packet(data: &[u8]) -> bool {
    data.len() >= 8 && (data[0] & 0xC0) == 0x80 && data[1].wrapping_add(62) < 30
}

pub fn is_rtp_packet(data: &[u8]) -> bool {
    data.len() >= 8 && (data[0] & 0xC0) == 0x80 && data[1].wrapping_add(32) < 0xE2
}

pub fn is_native_echo_packet(data: &[u8]) -> bool {
    if data.len() < IP_DISCOVERY_PACKET_SIZE || data[0] > 1 {
        return false;
    }
    let Ok(payload_len) = data[2..4].try_into().map(u16::from_be_bytes) else {
        return false;
    };
    if data.len() - 4 != payload_len as usize {
        return false;
    }
    let packet_type = u16::from_be_bytes([data[0], data[1]]);
    (IP_DISCOVERY_SEND..=IP_DISCOVERY_RESPONSE).contains(&packet_type)
}

pub fn is_native_control_packet(data: &[u8]) -> bool {
    native_ping_response_sequence(data).is_some() || is_native_echo_packet(data)
}

#[derive(Default)]
struct PingState {
    sequence: u32,
    sent: Option<(u32, Instant)>,
    rtt_ms: Option<f64>,
    timeouts: usize,
    send_errors: usize,
}

struct PingShared {
    state: Mutex<PingState>,
    stop: AtomicBool,
    wake: Condvar,
}

pub struct UDPPing {
    shared: Arc<PingShared>,
    thread: Option<JoinHandle<()>>,
}

impl UDPPing {
    pub fn start(
        fd: usize,
        initial_delay: Duration,
        interval: Duration,
        timeout: Duration,
        send_packet: fn(usize, &[u8]) -> io::Result<()>,
    ) -> Self {
        let shared = Arc::new(PingShared {
            state: Mutex::new(PingState::default()),
            stop: AtomicBool::new(false),
            wake: Condvar::new(),
        });
        let thread_shared = Arc::clone(&shared);
        let thread = thread::Builder::new()
            .name("native-voice-udp-ping".to_owned())
            .spawn(move || {
                run_ping_thread(
                    thread_shared,
                    fd,
                    initial_delay,
                    interval,
                    timeout,
                    send_packet,
                )
            })
            .ok();

        Self { shared, thread }
    }

    pub fn stop(&mut self) {
        self.shared.stop.store(true, Ordering::SeqCst);
        self.shared.wake.notify_all();
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }

    pub fn handle_packet(&self, packet: &[u8]) -> bool {
        let Some(sequence) = native_ping_response_sequence(packet) else {
            return false;
        };

        let mut state = self.shared.state.lock().expect("UDP ping state poisoned");
        let Some((sent_sequence, sent_at)) = state.sent else {
            return true;
        };
        if sequence == sent_sequence {
            state.sent = None;
            state.rtt_ms = Some(sent_at.elapsed().as_secs_f64() * 1000.0);
        }
        true
    }

    pub fn rtt_ms(&self) -> Option<f64> {
        self.shared
            .state
            .lock()
            .expect("UDP ping state poisoned")
            .rtt_ms
    }

    pub fn timeouts(&self) -> usize {
        self.shared
            .state
            .lock()
            .expect("UDP ping state poisoned")
            .timeouts
    }

    pub fn send_errors(&self) -> usize {
        self.shared
            .state
            .lock()
            .expect("UDP ping state poisoned")
            .send_errors
    }
}

impl Drop for UDPPing {
    fn drop(&mut self) {
        self.stop();
    }
}

fn run_ping_thread(
    shared: Arc<PingShared>,
    fd: usize,
    initial_delay: Duration,
    interval: Duration,
    timeout: Duration,
    send_packet: fn(usize, &[u8]) -> io::Result<()>,
) {
    if wait_for_stop(&shared, initial_delay) {
        return;
    }

    loop {
        let sequence = {
            let mut state = shared.state.lock().expect("UDP ping state poisoned");
            if let Some((_, sent_at)) = state.sent
                && sent_at.elapsed() >= timeout
            {
                state.sent = None;
                state.timeouts += 1;
            }
            state.sequence = state.sequence.wrapping_add(1);
            state.sequence
        };

        let packet = native_ping_request(sequence);
        let sent = send_packet(fd, &packet).is_ok();
        let mut state = shared.state.lock().expect("UDP ping state poisoned");
        if sent {
            state.sent = Some((sequence, Instant::now()));
        } else {
            state.sent = None;
            state.send_errors += 1;
        }
        drop(state);

        if wait_for_stop(&shared, interval) {
            return;
        }
    }
}

fn wait_for_stop(shared: &PingShared, duration: Duration) -> bool {
    if shared.stop.load(Ordering::SeqCst) {
        return true;
    }

    let state = shared.state.lock().expect("UDP ping state poisoned");
    drop(
        shared
            .wake
            .wait_timeout_while(state, duration, |_| !shared.stop.load(Ordering::SeqCst))
            .expect("UDP ping state poisoned"),
    );
    shared.stop.load(Ordering::SeqCst)
}

pub fn seconds_to_duration(seconds: f64) -> Duration {
    Duration::from_secs_f64(seconds.max(0.001))
}
