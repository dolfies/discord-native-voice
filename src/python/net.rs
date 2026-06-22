use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyTuple};
use std::io;
use std::net::UdpSocket;
use std::sync::atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering};
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

const SEND_WOULD_BLOCK_TIMEOUT: Duration = Duration::from_millis(50);
const SEND_WOULD_BLOCK_SLEEP: Duration = Duration::from_millis(1);
const UDP_IDLE_SLEEP: Duration = Duration::from_millis(1);
const UDP_PACKET_BUFFER_SIZE: usize = 2048;
const UDP_RECV_BATCH_SIZE: usize = 128;
const UDP_DEFAULT_BUFFER_SIZE: usize = 4 * 1024 * 1024;
const UDP_LISTENER_RTP: u8 = 1;
const UDP_LISTENER_RTCP: u8 = 1 << 1;

#[cfg(windows)]
fn socket_fileno(socket: &UdpSocket) -> usize {
    use std::os::windows::io::AsRawSocket;

    socket.as_raw_socket() as usize
}

#[cfg(unix)]
fn socket_fileno(socket: &UdpSocket) -> usize {
    use std::os::unix::io::AsRawFd;

    socket.as_raw_fd() as usize
}

#[cfg(not(any(unix, windows)))]
fn socket_fileno(_socket: &UdpSocket) -> usize {
    0
}

fn update_atomic_max(value: &AtomicUsize, candidate: usize) {
    let mut current = value.load(Ordering::Relaxed);
    while candidate > current {
        match value.compare_exchange_weak(current, candidate, Ordering::Relaxed, Ordering::Relaxed)
        {
            Ok(_) => break,
            Err(next) => current = next,
        }
    }
}

fn retry_would_block(mut send: impl FnMut() -> io::Result<()>) -> io::Result<()> {
    let started = Instant::now();
    loop {
        match send() {
            Ok(()) => return Ok(()),
            Err(err)
                if err.kind() == io::ErrorKind::WouldBlock
                    || err.kind() == io::ErrorKind::Interrupted =>
            {
                if started.elapsed() >= SEND_WOULD_BLOCK_TIMEOUT {
                    return Err(err);
                }
                std::thread::sleep(SEND_WOULD_BLOCK_SLEEP);
            }
            Err(err) => return Err(err),
        }
    }
}

#[cfg(windows)]
fn send_one(fd: usize, packet: &[u8]) -> io::Result<()> {
    use windows::Win32::Networking::WinSock::{SEND_RECV_FLAGS, SOCKET, SOCKET_ERROR, send};

    retry_would_block(|| {
        let sent = unsafe { send(SOCKET(fd), packet, SEND_RECV_FLAGS(0)) };
        if sent == SOCKET_ERROR {
            return Err(io::Error::last_os_error());
        }
        if sent as usize != packet.len() {
            return Err(io::Error::new(
                io::ErrorKind::WriteZero,
                "Socket sent a partial datagram",
            ));
        }
        Ok(())
    })
}

#[cfg(unix)]
fn send_one(fd: usize, packet: &[u8]) -> io::Result<()> {
    retry_would_block(|| {
        let sent =
            unsafe { libc::send(fd as libc::c_int, packet.as_ptr().cast(), packet.len(), 0) };
        if sent < 0 {
            return Err(io::Error::last_os_error());
        }
        if sent as usize != packet.len() {
            return Err(io::Error::new(
                io::ErrorKind::WriteZero,
                "Socket sent a partial datagram",
            ));
        }
        Ok(())
    })
}

#[cfg(not(any(unix, windows)))]
fn send_one(_fd: usize, _packet: &[u8]) -> io::Result<()> {
    Err(io::Error::new(
        io::ErrorKind::Unsupported,
        "Native socket sending is not supported on this platform",
    ))
}

pub(crate) fn send_packet_fd(fd: usize, packet: &[u8]) -> io::Result<()> {
    send_one(fd, packet)
}

#[cfg(windows)]
fn set_socket_option_i32(fd: usize, level: i32, optname: i32, value: i32) -> io::Result<()> {
    use windows::Win32::Networking::WinSock::{SOCKET, SOCKET_ERROR, setsockopt};

    let bytes = value.to_ne_bytes();
    let result = unsafe { setsockopt(SOCKET(fd), level, optname, Some(&bytes)) };
    if result == SOCKET_ERROR {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

#[cfg(unix)]
fn set_socket_option_i32(fd: usize, level: i32, optname: i32, value: i32) -> io::Result<()> {
    let result = unsafe {
        libc::setsockopt(
            fd as libc::c_int,
            level,
            optname,
            (&value as *const i32).cast(),
            std::mem::size_of::<i32>() as libc::socklen_t,
        )
    };
    if result < 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

#[cfg(not(any(unix, windows)))]
fn set_socket_option_i32(_fd: usize, _level: i32, _optname: i32, _value: i32) -> io::Result<()> {
    Ok(())
}

fn set_udp_socket_options(socket: &UdpSocket, buffer_size: usize, tos: i32) {
    let fd = socket_fileno(socket);
    let buffer_size = buffer_size.min(i32::MAX as usize) as i32;

    #[cfg(windows)]
    {
        use windows::Win32::Networking::WinSock::{
            IP_TOS, IPPROTO_IP, SO_RCVBUF, SO_SNDBUF, SOL_SOCKET,
        };

        let _ = set_socket_option_i32(fd, SOL_SOCKET, SO_RCVBUF, buffer_size);
        let _ = set_socket_option_i32(fd, SOL_SOCKET, SO_SNDBUF, buffer_size);
        let _ = set_socket_option_i32(fd, IPPROTO_IP.0, IP_TOS, tos);
    }

    #[cfg(unix)]
    {
        let _ = set_socket_option_i32(fd, libc::SOL_SOCKET, libc::SO_RCVBUF, buffer_size);
        let _ = set_socket_option_i32(fd, libc::SOL_SOCKET, libc::SO_SNDBUF, buffer_size);
        let _ = set_socket_option_i32(fd, libc::IPPROTO_IP, libc::IP_TOS, tos);
    }
}

#[derive(Default)]
struct UDPTransportStats {
    packets_received: AtomicUsize,
    rtp_packets_received: AtomicUsize,
    rtcp_packets_received: AtomicUsize,
    control_packets_received: AtomicUsize,
    invalid_packets_received: AtomicUsize,
    packets_sent: AtomicUsize,
    octets_sent: AtomicUsize,
    callbacks_called: AtomicUsize,
    callback_errors: AtomicUsize,
    dispatch_batches_queued: AtomicUsize,
    dispatch_packets_queued: AtomicUsize,
    dispatch_batches_dispatched: AtomicUsize,
    dispatch_packets_dispatched: AtomicUsize,
    dispatch_queue_pending: AtomicUsize,
    dispatch_queue_max_pending: AtomicUsize,
}

struct UDPListener {
    id: u64,
    flags: u8,
    batch: bool,
    callback: Py<PyAny>,
}

struct UDPTransportInner {
    socket: UdpSocket,
    closed: AtomicBool,
    reader_running: AtomicBool,
    next_listener_id: AtomicU64,
    listeners: Mutex<Vec<UDPListener>>,
    reader: Mutex<Option<JoinHandle<()>>>,
    dispatcher: Mutex<Option<JoinHandle<()>>>,
    dispatch_tx: Mutex<Option<Sender<UDPDispatchBatch>>>,
    ping: Mutex<Option<crate::transport::udp::UDPPing>>,
    stats: UDPTransportStats,
}

struct UDPDispatchBatch {
    rtp_packets: Vec<Vec<u8>>,
    rtcp_packets: Vec<Vec<u8>>,
}

impl UDPTransportInner {
    fn ensure_open(&self) -> io::Result<()> {
        if self.closed.load(Ordering::SeqCst) {
            return Err(io::Error::new(
                io::ErrorKind::NotConnected,
                "UDP transport is closed",
            ));
        }
        Ok(())
    }

    fn send_packet(&self, packet: &[u8]) -> io::Result<()> {
        self.ensure_open()?;
        retry_would_block(|| {
            let sent = self.socket.send(packet)?;
            if sent != packet.len() {
                return Err(io::Error::new(
                    io::ErrorKind::WriteZero,
                    "Socket sent a partial datagram",
                ));
            }
            Ok(())
        })?;
        self.stats.packets_sent.fetch_add(1, Ordering::Relaxed);
        self.stats
            .octets_sent
            .fetch_add(packet.len(), Ordering::Relaxed);
        Ok(())
    }

    fn add_listener(&self, callback: Py<PyAny>, flags: u8, batch: bool) -> u64 {
        let id = self.next_listener_id.fetch_add(1, Ordering::Relaxed);
        self.listeners
            .lock()
            .expect("UDP transport listeners poisoned")
            .push(UDPListener {
                id,
                flags,
                batch,
                callback,
            });
        id
    }

    fn remove_listener(&self, id: u64) {
        self.listeners
            .lock()
            .expect("UDP transport listeners poisoned")
            .retain(|listener| listener.id != id);
    }

    fn start_reader(self: &Arc<Self>) {
        if self.reader_running.swap(true, Ordering::SeqCst) {
            return;
        }

        let (dispatch_tx, dispatch_rx) = mpsc::channel();
        *self
            .dispatch_tx
            .lock()
            .expect("UDP dispatcher sender poisoned") = Some(dispatch_tx);

        let dispatcher_inner = Arc::clone(self);
        let dispatcher_handle = thread::Builder::new()
            .name("native-voice-udp-dispatch".to_owned())
            .spawn(move || run_udp_dispatcher(dispatcher_inner, dispatch_rx));
        match dispatcher_handle {
            Ok(handle) => {
                *self
                    .dispatcher
                    .lock()
                    .expect("UDP dispatcher handle poisoned") = Some(handle);
            }
            Err(_) => {
                self.reader_running.store(false, Ordering::SeqCst);
                self.dispatch_tx
                    .lock()
                    .expect("UDP dispatcher sender poisoned")
                    .take();
                return;
            }
        }

        let inner = Arc::clone(self);
        let handle = thread::Builder::new()
            .name("native-voice-udp-transport".to_owned())
            .spawn(move || run_udp_reader(inner));
        match handle {
            Ok(handle) => {
                *self.reader.lock().expect("UDP reader handle poisoned") = Some(handle);
            }
            Err(_) => {
                self.reader_running.store(false, Ordering::SeqCst);
                self.dispatch_tx
                    .lock()
                    .expect("UDP dispatcher sender poisoned")
                    .take();
                if let Some(handle) = self
                    .dispatcher
                    .lock()
                    .expect("UDP dispatcher handle poisoned")
                    .take()
                    && handle.thread().id() != thread::current().id()
                {
                    let _ = handle.join();
                }
            }
        }
    }

    fn stop_reader_threads(&self) {
        self.reader_running.store(false, Ordering::SeqCst);
        self.dispatch_tx
            .lock()
            .expect("UDP dispatcher sender poisoned")
            .take();

        let handle = self
            .reader
            .lock()
            .expect("UDP reader handle poisoned")
            .take();
        if let Some(handle) = handle
            && handle.thread().id() != thread::current().id()
        {
            let _ = handle.join();
        }

        let handle = self
            .dispatcher
            .lock()
            .expect("UDP dispatcher handle poisoned")
            .take();
        if let Some(handle) = handle
            && handle.thread().id() != thread::current().id()
        {
            let _ = handle.join();
        }
    }

    fn close(&self) {
        self.closed.store(true, Ordering::SeqCst);
        if let Some(mut ping) = self.ping.lock().expect("UDP ping poisoned").take() {
            ping.stop();
        }
        self.stop_reader_threads();
    }

    fn start_ping(
        self: &Arc<Self>,
        initial_delay: Duration,
        interval: Duration,
        timeout: Duration,
    ) {
        self.start_reader();
        let mut ping = self.ping.lock().expect("UDP ping poisoned");
        if ping.is_some() {
            return;
        }
        *ping = Some(crate::transport::udp::UDPPing::start(
            socket_fileno(&self.socket),
            initial_delay,
            interval,
            timeout,
            send_packet_fd,
        ));
    }

    fn stop_ping(&self) {
        if let Some(mut ping) = self.ping.lock().expect("UDP ping poisoned").take() {
            ping.stop();
        }
    }
}

impl Drop for UDPTransportInner {
    fn drop(&mut self) {
        self.closed.store(true, Ordering::SeqCst);
        if let Some(mut ping) = self.ping.lock().expect("UDP ping poisoned").take() {
            ping.stop();
        }
        self.stop_reader_threads();
    }
}

#[derive(Clone, Copy)]
enum UDPPacketKind {
    Rtp,
    Rtcp,
}

fn classify_udp_packet(data: &[u8]) -> Option<UDPPacketKind> {
    if crate::transport::udp::is_rtcp_packet(data) {
        return Some(UDPPacketKind::Rtcp);
    }
    if crate::transport::udp::is_rtp_packet(data) {
        return Some(UDPPacketKind::Rtp);
    }
    None
}

fn run_udp_reader(inner: Arc<UDPTransportInner>) {
    let mut buffer = [0u8; UDP_PACKET_BUFFER_SIZE];
    let mut packets = Vec::with_capacity(UDP_RECV_BATCH_SIZE);
    while inner.reader_running.load(Ordering::SeqCst) {
        packets.clear();
        match inner.socket.recv(&mut buffer) {
            Ok(length) => packets.push(buffer[..length].to_vec()),
            Err(err)
                if err.kind() == io::ErrorKind::WouldBlock
                    || err.kind() == io::ErrorKind::Interrupted
                    || err.kind() == io::ErrorKind::TimedOut =>
            {
                thread::sleep(UDP_IDLE_SLEEP);
                continue;
            }
            Err(_) => {
                if !inner.closed.load(Ordering::SeqCst) {
                    inner
                        .stats
                        .invalid_packets_received
                        .fetch_add(1, Ordering::Relaxed);
                }
                continue;
            }
        }

        while packets.len() < UDP_RECV_BATCH_SIZE {
            match inner.socket.recv(&mut buffer) {
                Ok(length) => packets.push(buffer[..length].to_vec()),
                Err(err)
                    if err.kind() == io::ErrorKind::WouldBlock
                        || err.kind() == io::ErrorKind::Interrupted
                        || err.kind() == io::ErrorKind::TimedOut =>
                {
                    break;
                }
                Err(_) => {
                    if !inner.closed.load(Ordering::SeqCst) {
                        inner
                            .stats
                            .invalid_packets_received
                            .fetch_add(1, Ordering::Relaxed);
                    }
                    break;
                }
            }
        }

        let batch = std::mem::replace(&mut packets, Vec::with_capacity(UDP_RECV_BATCH_SIZE));
        handle_udp_packets(&inner, batch);
    }
}

fn handle_udp_packets(inner: &Arc<UDPTransportInner>, packets: Vec<Vec<u8>>) {
    if inner.closed.load(Ordering::SeqCst) || !inner.reader_running.load(Ordering::SeqCst) {
        return;
    }

    let mut rtp_packets: Vec<Vec<u8>> = Vec::new();
    let mut rtcp_packets: Vec<Vec<u8>> = Vec::new();
    for packet in packets {
        inner.stats.packets_received.fetch_add(1, Ordering::Relaxed);

        if let Some(ping) = inner.ping.lock().expect("UDP ping poisoned").as_ref()
            && ping.handle_packet(packet.as_slice())
        {
            inner
                .stats
                .control_packets_received
                .fetch_add(1, Ordering::Relaxed);
            continue;
        }

        if crate::transport::udp::is_native_control_packet(packet.as_slice()) {
            inner
                .stats
                .control_packets_received
                .fetch_add(1, Ordering::Relaxed);
            continue;
        }

        match classify_udp_packet(packet.as_slice()) {
            Some(UDPPacketKind::Rtp) => {
                inner
                    .stats
                    .rtp_packets_received
                    .fetch_add(1, Ordering::Relaxed);
                rtp_packets.push(packet);
            }
            Some(UDPPacketKind::Rtcp) => {
                inner
                    .stats
                    .rtcp_packets_received
                    .fetch_add(1, Ordering::Relaxed);
                rtcp_packets.push(packet);
            }
            None => {
                inner
                    .stats
                    .invalid_packets_received
                    .fetch_add(1, Ordering::Relaxed);
            }
        }
    }

    queue_udp_dispatch(
        inner,
        UDPDispatchBatch {
            rtp_packets,
            rtcp_packets,
        },
    );
}

fn queue_udp_dispatch(inner: &Arc<UDPTransportInner>, batch: UDPDispatchBatch) {
    let packet_count = batch.rtp_packets.len() + batch.rtcp_packets.len();
    if packet_count == 0 {
        return;
    }

    let sender = inner
        .dispatch_tx
        .lock()
        .expect("UDP dispatcher sender poisoned")
        .as_ref()
        .cloned();
    let Some(sender) = sender else {
        return;
    };

    let pending = inner
        .stats
        .dispatch_queue_pending
        .fetch_add(1, Ordering::Relaxed)
        + 1;
    update_atomic_max(&inner.stats.dispatch_queue_max_pending, pending);

    match sender.send(batch) {
        Ok(()) => {
            inner
                .stats
                .dispatch_batches_queued
                .fetch_add(1, Ordering::Relaxed);
            inner
                .stats
                .dispatch_packets_queued
                .fetch_add(packet_count, Ordering::Relaxed);
        }
        Err(_) => {
            inner
                .stats
                .dispatch_queue_pending
                .fetch_sub(1, Ordering::Relaxed);
        }
    }
}

fn run_udp_dispatcher(inner: Arc<UDPTransportInner>, receiver: Receiver<UDPDispatchBatch>) {
    while let Ok(batch) = receiver.recv() {
        inner
            .stats
            .dispatch_queue_pending
            .fetch_sub(1, Ordering::Relaxed);
        if inner.closed.load(Ordering::SeqCst) {
            break;
        }

        let packet_count = batch.rtp_packets.len() + batch.rtcp_packets.len();
        dispatch_udp_packets(&inner, UDP_LISTENER_RTP, batch.rtp_packets);
        dispatch_udp_packets(&inner, UDP_LISTENER_RTCP, batch.rtcp_packets);
        inner
            .stats
            .dispatch_batches_dispatched
            .fetch_add(1, Ordering::Relaxed);
        inner
            .stats
            .dispatch_packets_dispatched
            .fetch_add(packet_count, Ordering::Relaxed);
    }
}

fn dispatch_udp_packets(inner: &Arc<UDPTransportInner>, listener_flag: u8, packets: Vec<Vec<u8>>) {
    if packets.is_empty() {
        return;
    }

    let callbacks = Python::attach(|py| {
        inner
            .listeners
            .lock()
            .expect("UDP transport listeners poisoned")
            .iter()
            .filter(|listener| listener.flags & listener_flag != 0)
            .map(|listener| (listener.callback.clone_ref(py), listener.batch))
            .collect::<Vec<_>>()
    });
    if callbacks.is_empty() {
        return;
    }

    Python::attach(|py| {
        let batch_packets = if callbacks.iter().any(|(_callback, batch)| *batch) {
            match PyTuple::new(
                py,
                packets
                    .iter()
                    .map(|packet| PyBytes::new(py, packet.as_slice())),
            ) {
                Ok(tuple) => Some(tuple),
                Err(err) => {
                    inner.stats.callback_errors.fetch_add(1, Ordering::Relaxed);
                    err.print(py);
                    None
                }
            }
        } else {
            None
        };

        for (callback, batch) in callbacks.iter() {
            if *batch {
                if let Some(batch_packets) = batch_packets.as_ref() {
                    match callback.call1(py, (batch_packets,)) {
                        Ok(_) => {
                            inner.stats.callbacks_called.fetch_add(1, Ordering::Relaxed);
                        }
                        Err(err) => {
                            inner.stats.callback_errors.fetch_add(1, Ordering::Relaxed);
                            err.print(py);
                        }
                    }
                }
                continue;
            }

            for packet in packets.iter() {
                let data = PyBytes::new(py, packet.as_slice());
                match callback.call1(py, (data,)) {
                    Ok(_) => {
                        inner.stats.callbacks_called.fetch_add(1, Ordering::Relaxed);
                    }
                    Err(err) => {
                        inner.stats.callback_errors.fetch_add(1, Ordering::Relaxed);
                        err.print(py);
                    }
                }
            }
        }
    });
}

#[pyclass(name = "NativeUDPTransport")]
struct PyNativeUDPTransport {
    inner: Arc<UDPTransportInner>,
}

#[pymethods]
impl PyNativeUDPTransport {
    #[new]
    fn new(buffer_size: Option<usize>, tos: Option<i32>) -> PyResult<Self> {
        let socket = UdpSocket::bind("0.0.0.0:0")
            .map_err(|err| pyo3::exceptions::PyOSError::new_err(err.to_string()))?;
        socket
            .set_nonblocking(true)
            .map_err(|err| pyo3::exceptions::PyOSError::new_err(err.to_string()))?;
        set_udp_socket_options(
            &socket,
            buffer_size.unwrap_or(UDP_DEFAULT_BUFFER_SIZE),
            tos.unwrap_or(0),
        );
        Ok(Self {
            inner: Arc::new(UDPTransportInner {
                socket,
                closed: AtomicBool::new(false),
                reader_running: AtomicBool::new(false),
                next_listener_id: AtomicU64::new(1),
                listeners: Mutex::new(Vec::new()),
                reader: Mutex::new(None),
                dispatcher: Mutex::new(None),
                dispatch_tx: Mutex::new(None),
                ping: Mutex::new(None),
                stats: UDPTransportStats::default(),
            }),
        })
    }

    fn fileno(&self) -> usize {
        socket_fileno(&self.inner.socket)
    }

    fn connect(&self, py: Python<'_>, address: String, port: u16) -> PyResult<()> {
        self.inner
            .ensure_open()
            .map_err(|err| pyo3::exceptions::PyOSError::new_err(err.to_string()))?;
        let inner = Arc::clone(&self.inner);
        py.detach(move || inner.socket.connect((address.as_str(), port)))
            .map_err(|err| pyo3::exceptions::PyOSError::new_err(err.to_string()))
    }

    fn discover_ip(&self, py: Python<'_>, ssrc: u32, timeout: f64) -> PyResult<(String, u16)> {
        self.inner
            .ensure_open()
            .map_err(|err| pyo3::exceptions::PyOSError::new_err(err.to_string()))?;
        let inner = Arc::clone(&self.inner);
        py.detach(move || {
            crate::transport::udp::discover_ip_socket(
                &inner.socket,
                ssrc,
                crate::transport::udp::seconds_to_duration(timeout),
            )
        })
        .map_err(|err| pyo3::exceptions::PyOSError::new_err(err.to_string()))
    }

    fn add_listener(&self, callback: Py<PyAny>, rtp: bool, rtcp: bool) -> u64 {
        let mut flags = 0;
        if rtp {
            flags |= UDP_LISTENER_RTP;
        }
        if rtcp {
            flags |= UDP_LISTENER_RTCP;
        }
        self.inner.start_reader();
        self.inner.add_listener(callback, flags, false)
    }

    fn add_batch_listener(&self, callback: Py<PyAny>, rtp: bool, rtcp: bool) -> u64 {
        let mut flags = 0;
        if rtp {
            flags |= UDP_LISTENER_RTP;
        }
        if rtcp {
            flags |= UDP_LISTENER_RTCP;
        }
        self.inner.start_reader();
        self.inner.add_listener(callback, flags, true)
    }

    fn remove_listener(&self, id: u64) {
        self.inner.remove_listener(id);
    }

    fn send_packet(&self, py: Python<'_>, packet: &Bound<'_, PyBytes>) -> PyResult<()> {
        let data = packet.as_bytes().to_vec();
        let inner = Arc::clone(&self.inner);
        py.detach(move || inner.send_packet(&data))
            .map_err(|err| pyo3::exceptions::PyOSError::new_err(err.to_string()))
    }

    fn send_packets(&self, py: Python<'_>, packets: &Bound<'_, PyAny>) -> PyResult<(usize, usize)> {
        let mut packet_bytes: Vec<Vec<u8>> = Vec::new();
        for item in packets.try_iter()? {
            let item = item?;
            let packet = item.cast::<PyBytes>()?;
            packet_bytes.push(packet.as_bytes().to_vec());
        }

        let inner = Arc::clone(&self.inner);
        let (sent, octets) = py
            .detach(move || {
                let mut sent = 0usize;
                let mut octets = 0usize;
                for packet in packet_bytes {
                    inner.send_packet(&packet)?;
                    sent += 1;
                    octets += packet.len();
                }
                Ok::<_, io::Error>((sent, octets))
            })
            .map_err(|err| pyo3::exceptions::PyOSError::new_err(err.to_string()))?;

        Ok((sent, octets))
    }

    fn start_ping(&self, initial_delay: f64, interval: f64, timeout: f64) {
        self.inner.start_ping(
            crate::transport::udp::seconds_to_duration(initial_delay),
            crate::transport::udp::seconds_to_duration(interval),
            crate::transport::udp::seconds_to_duration(timeout),
        );
    }

    fn stop_ping(&self) {
        self.inner.stop_ping();
    }

    #[getter]
    fn ping_rtt_ms(&self) -> Option<f64> {
        self.inner
            .ping
            .lock()
            .expect("UDP ping poisoned")
            .as_ref()
            .and_then(|ping| ping.rtt_ms())
    }

    #[getter]
    fn ping_timeouts(&self) -> usize {
        self.inner
            .ping
            .lock()
            .expect("UDP ping poisoned")
            .as_ref()
            .map_or(0, |ping| ping.timeouts())
    }

    #[getter]
    fn ping_send_errors(&self) -> usize {
        self.inner
            .ping
            .lock()
            .expect("UDP ping poisoned")
            .as_ref()
            .map_or(0, |ping| ping.send_errors())
    }

    fn stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let stats = &self.inner.stats;
        let data = PyDict::new(py);
        data.set_item(
            "packets_received",
            stats.packets_received.load(Ordering::Relaxed),
        )?;
        data.set_item(
            "rtp_packets_received",
            stats.rtp_packets_received.load(Ordering::Relaxed),
        )?;
        data.set_item(
            "rtcp_packets_received",
            stats.rtcp_packets_received.load(Ordering::Relaxed),
        )?;
        data.set_item(
            "control_packets_received",
            stats.control_packets_received.load(Ordering::Relaxed),
        )?;
        data.set_item(
            "invalid_packets_received",
            stats.invalid_packets_received.load(Ordering::Relaxed),
        )?;
        data.set_item("packets_sent", stats.packets_sent.load(Ordering::Relaxed))?;
        data.set_item("octets_sent", stats.octets_sent.load(Ordering::Relaxed))?;
        data.set_item(
            "callbacks_called",
            stats.callbacks_called.load(Ordering::Relaxed),
        )?;
        data.set_item(
            "callback_errors",
            stats.callback_errors.load(Ordering::Relaxed),
        )?;
        data.set_item(
            "dispatch_batches_queued",
            stats.dispatch_batches_queued.load(Ordering::Relaxed),
        )?;
        data.set_item(
            "dispatch_packets_queued",
            stats.dispatch_packets_queued.load(Ordering::Relaxed),
        )?;
        data.set_item(
            "dispatch_batches_dispatched",
            stats.dispatch_batches_dispatched.load(Ordering::Relaxed),
        )?;
        data.set_item(
            "dispatch_packets_dispatched",
            stats.dispatch_packets_dispatched.load(Ordering::Relaxed),
        )?;
        data.set_item(
            "dispatch_queue_pending",
            stats.dispatch_queue_pending.load(Ordering::Relaxed),
        )?;
        data.set_item(
            "dispatch_queue_max_pending",
            stats.dispatch_queue_max_pending.load(Ordering::Relaxed),
        )?;
        Ok(data)
    }

    fn close(&self, py: Python<'_>) {
        let inner = Arc::clone(&self.inner);
        py.detach(move || inner.close());
    }
}

pub(super) fn add_to_module(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyNativeUDPTransport>()?;
    Ok(())
}
