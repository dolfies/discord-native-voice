#[cfg(windows)]
mod windows;

#[derive(Clone, Copy, Default)]
pub struct DesktopCaptureStats {
    pub frames_read_total: u64,
    pub frames_captured_total: u64,
    pub frames_copied_total: u64,
    pub frames_reused_total: u64,
    pub pointer_updates_total: u64,
    pub capture_attempts_total: u64,
    pub capture_timeouts_total: u64,
    pub capture_call_mean_ms: f64,
    pub capture_call_max_ms: f64,
    pub nv12_conversion_count: u64,
    pub nv12_conversion_mean_ms: f64,
    pub nv12_conversion_max_ms: f64,
    pub frame_intervals_count: u32,
    pub frame_interval_mean_ms: f64,
    pub frame_interval_stdev_ms: f64,
}

#[cfg(windows)]
pub use self::windows::DesktopFrameSource;

#[cfg(not(windows))]
pub struct DesktopFrameSource;

#[cfg(not(windows))]
impl DesktopFrameSource {
    pub fn new(_output_index: u32, _fps: u32) -> Result<Self, String> {
        Err("Desktop capture is only supported on Windows".to_owned())
    }

    pub fn width(&self) -> u32 {
        0
    }

    pub fn height(&self) -> u32 {
        0
    }

    pub fn frame_size(&self) -> usize {
        0
    }

    pub fn pixel_format(&self) -> &'static str {
        "bgra"
    }

    pub fn set_pixel_format(&mut self, _pixel_format: &str) -> Result<(), String> {
        Err("Desktop capture is only supported on Windows".to_owned())
    }

    pub fn prepare_frame(&mut self) -> Result<(), String> {
        Err("Desktop capture is only supported on Windows".to_owned())
    }

    pub fn frame(&self) -> &[u8] {
        &[]
    }

    pub fn stats(&self) -> DesktopCaptureStats {
        DesktopCaptureStats::default()
    }

    pub fn close(&mut self) {}
}
