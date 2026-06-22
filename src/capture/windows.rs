use std::collections::VecDeque;
use std::ptr;
use std::thread;
use std::time::{Duration, Instant};

use windows::Win32::Foundation::HMODULE;
use windows::Win32::Graphics::Direct3D::{
    D3D_DRIVER_TYPE_HARDWARE, D3D_FEATURE_LEVEL, D3D_FEATURE_LEVEL_10_0, D3D_FEATURE_LEVEL_10_1,
    D3D_FEATURE_LEVEL_11_0, D3D_FEATURE_LEVEL_11_1,
};
use windows::Win32::Graphics::Direct3D11::{
    D3D11_CPU_ACCESS_READ, D3D11_CREATE_DEVICE_BGRA_SUPPORT, D3D11_MAP_READ,
    D3D11_MAPPED_SUBRESOURCE, D3D11_SDK_VERSION, D3D11_TEXTURE2D_DESC, D3D11_USAGE_STAGING,
    D3D11CreateDevice, ID3D11Device, ID3D11DeviceContext, ID3D11Texture2D,
};
use windows::Win32::Graphics::Dxgi::Common::{DXGI_FORMAT_B8G8R8A8_UNORM, DXGI_SAMPLE_DESC};
use windows::Win32::Graphics::Dxgi::{
    DXGI_ERROR_ACCESS_LOST, DXGI_ERROR_WAIT_TIMEOUT, DXGI_OUTDUPL_FRAME_INFO,
    DXGI_OUTDUPL_POINTER_SHAPE_INFO, DXGI_OUTDUPL_POINTER_SHAPE_TYPE_COLOR,
    DXGI_OUTDUPL_POINTER_SHAPE_TYPE_MASKED_COLOR, DXGI_OUTDUPL_POINTER_SHAPE_TYPE_MONOCHROME,
    IDXGIAdapter, IDXGIDevice, IDXGIOutput, IDXGIOutput1, IDXGIOutputDuplication, IDXGIResource,
};
use windows::core::{Error as WindowsError, Interface};
use yuv::{
    BufferStoreMut, YuvBiPlanarImageMut, YuvConversionMode, YuvRange, YuvStandardMatrix,
    bgra_to_yuv_nv12,
};

use super::DesktopCaptureStats;

const RECENT_FRAME_WINDOW: Duration = Duration::from_millis(1001);

#[derive(Clone, Copy, Eq, PartialEq)]
enum DesktopPixelFormat {
    Bgra,
    Nv12,
}

#[derive(Default)]
struct PointerState {
    visible: bool,
    x: i32,
    y: i32,
    shape: Vec<u8>,
    shape_info: DXGI_OUTDUPL_POINTER_SHAPE_INFO,
}

struct PointerClip {
    src_x: usize,
    src_y: usize,
    dst_x: usize,
    dst_y: usize,
    width: usize,
    height: usize,
}

struct PointerRestore {
    dst_x: usize,
    dst_y: usize,
    width: usize,
    height: usize,
    pixels: Vec<u8>,
}

type DuplicationResources = (
    ID3D11Device,
    ID3D11DeviceContext,
    IDXGIOutputDuplication,
    u32,
    u32,
    i32,
    i32,
);

pub struct DesktopFrameSource {
    _device: ID3D11Device,
    context: ID3D11DeviceContext,
    duplication: IDXGIOutputDuplication,
    staging: ID3D11Texture2D,
    width: u32,
    height: u32,
    output_left: i32,
    output_top: i32,
    frame_size: usize,
    frame_interval: Duration,
    next_frame_at: Option<Instant>,
    last_frame: Vec<u8>,
    frame_generation: u64,
    pixel_format: DesktopPixelFormat,
    nv12_frame: Vec<u8>,
    nv12_generation: u64,
    pointer: PointerState,
    pointer_restore: Option<PointerRestore>,
    frames_read_total: u64,
    frames_captured_total: u64,
    frames_copied_total: u64,
    frames_reused_total: u64,
    pointer_updates_total: u64,
    capture_attempts_total: u64,
    capture_timeouts_total: u64,
    capture_elapsed_total_ms: f64,
    capture_elapsed_max_ms: f64,
    nv12_conversion_count: u64,
    nv12_conversion_elapsed_total_ms: f64,
    nv12_conversion_elapsed_max_ms: f64,
    recent_frame_times: VecDeque<Instant>,
    closed: bool,
}

impl DesktopFrameSource {
    pub fn new(output_index: u32, fps: u32) -> Result<Self, String> {
        let (device, context, duplication, width, height, output_left, output_top) =
            create_duplication(output_index)?;
        let staging = create_staging_texture(&device, width, height)?;
        let frame_size = width as usize * height as usize * 4;
        let frame_interval = Duration::from_secs_f64(1.0 / fps.max(1) as f64);

        Ok(Self {
            _device: device,
            context,
            duplication,
            staging,
            width,
            height,
            output_left,
            output_top,
            frame_size,
            frame_interval,
            next_frame_at: None,
            last_frame: Vec::new(),
            frame_generation: 0,
            pixel_format: DesktopPixelFormat::Bgra,
            nv12_frame: Vec::new(),
            nv12_generation: 0,
            pointer: PointerState::default(),
            pointer_restore: None,
            frames_read_total: 0,
            frames_captured_total: 0,
            frames_copied_total: 0,
            frames_reused_total: 0,
            pointer_updates_total: 0,
            capture_attempts_total: 0,
            capture_timeouts_total: 0,
            capture_elapsed_total_ms: 0.0,
            capture_elapsed_max_ms: 0.0,
            nv12_conversion_count: 0,
            nv12_conversion_elapsed_total_ms: 0.0,
            nv12_conversion_elapsed_max_ms: 0.0,
            recent_frame_times: VecDeque::new(),
            closed: false,
        })
    }

    pub fn width(&self) -> u32 {
        self.width
    }

    pub fn height(&self) -> u32 {
        self.height
    }

    pub fn frame_size(&self) -> usize {
        match self.pixel_format {
            DesktopPixelFormat::Bgra => self.frame_size,
            DesktopPixelFormat::Nv12 => self.nv12_frame_size(),
        }
    }

    pub fn pixel_format(&self) -> &'static str {
        match self.pixel_format {
            DesktopPixelFormat::Bgra => "bgra",
            DesktopPixelFormat::Nv12 => "nv12",
        }
    }

    pub fn set_pixel_format(&mut self, pixel_format: &str) -> Result<(), String> {
        self.pixel_format = match pixel_format.to_ascii_lowercase().as_str() {
            "bgra" => DesktopPixelFormat::Bgra,
            "nv12" => {
                if !self.width.is_multiple_of(2) || !self.height.is_multiple_of(2) {
                    return Err("NV12 desktop capture requires even output dimensions".to_owned());
                }
                DesktopPixelFormat::Nv12
            }
            _ => return Err("pixel_format must be 'bgra' or 'nv12'".to_owned()),
        };
        Ok(())
    }

    pub fn prepare_frame(&mut self) -> Result<(), String> {
        if self.closed {
            return Ok(());
        }

        self.wait_for_frame_slot();

        let timeout_ms = if self.last_frame.is_empty() { 1000 } else { 0 };
        let mut captured = false;
        match self.capture_frame(timeout_ms) {
            Ok(true) => {
                captured = true;
            }
            Ok(false) if self.last_frame.is_empty() => {
                while !self.closed && self.last_frame.is_empty() {
                    match self.capture_frame(100) {
                        Ok(true) => {
                            captured = true;
                            break;
                        }
                        Ok(false) => continue,
                        Err(err) => return Err(err),
                    }
                }
            }
            Ok(false) => {}
            Err(err) => return Err(err),
        }

        if !self.last_frame.is_empty() {
            if self.pixel_format == DesktopPixelFormat::Nv12 {
                self.ensure_nv12_frame()?;
            }
            if !captured {
                self.frames_reused_total = self.frames_reused_total.wrapping_add(1);
            }
            self.record_frame_read();
        }

        Ok(())
    }

    pub fn frame(&self) -> &[u8] {
        if self.closed {
            return &[];
        }

        match self.pixel_format {
            DesktopPixelFormat::Bgra => self.last_frame.as_slice(),
            DesktopPixelFormat::Nv12 => self.nv12_frame.as_slice(),
        }
    }

    pub fn close(&mut self) {
        self.closed = true;
    }

    pub fn stats(&self) -> DesktopCaptureStats {
        let mut deltas = Vec::new();
        let mut previous = None;
        for instant in self.recent_frame_times.iter().copied() {
            if let Some(previous_instant) = previous {
                deltas.push(
                    instant
                        .saturating_duration_since(previous_instant)
                        .as_secs_f64()
                        * 1000.0,
                );
            }
            previous = Some(instant);
        }

        let frame_intervals_count = deltas.len() as u32;
        let frame_interval_mean_ms = if deltas.is_empty() {
            0.0
        } else {
            deltas.iter().sum::<f64>() / deltas.len() as f64
        };
        let frame_interval_stdev_ms = if deltas.is_empty() {
            0.0
        } else {
            let variance = deltas
                .iter()
                .map(|delta| {
                    let diff = delta - frame_interval_mean_ms;
                    diff * diff
                })
                .sum::<f64>()
                / deltas.len() as f64;
            variance.max(0.0).sqrt()
        };

        DesktopCaptureStats {
            frames_read_total: self.frames_read_total,
            frames_captured_total: self.frames_captured_total,
            frames_copied_total: self.frames_copied_total,
            frames_reused_total: self.frames_reused_total,
            pointer_updates_total: self.pointer_updates_total,
            capture_attempts_total: self.capture_attempts_total,
            capture_timeouts_total: self.capture_timeouts_total,
            capture_call_mean_ms: self.capture_elapsed_total_ms
                / self.capture_attempts_total.max(1) as f64,
            capture_call_max_ms: self.capture_elapsed_max_ms,
            nv12_conversion_count: self.nv12_conversion_count,
            nv12_conversion_mean_ms: self.nv12_conversion_elapsed_total_ms
                / self.nv12_conversion_count.max(1) as f64,
            nv12_conversion_max_ms: self.nv12_conversion_elapsed_max_ms,
            frame_intervals_count,
            frame_interval_mean_ms,
            frame_interval_stdev_ms,
        }
    }

    fn record_frame_read(&mut self) {
        let now = Instant::now();
        self.frames_read_total = self.frames_read_total.wrapping_add(1);
        while self
            .recent_frame_times
            .front()
            .is_some_and(|front| now.saturating_duration_since(*front) >= RECENT_FRAME_WINDOW)
        {
            self.recent_frame_times.pop_front();
        }
        self.recent_frame_times.push_back(now);
    }

    fn wait_for_frame_slot(&mut self) {
        let now = Instant::now();
        let Some(next) = self.next_frame_at else {
            self.next_frame_at = Some(now + self.frame_interval);
            return;
        };

        if now < next {
            thread::sleep(next - now);
            self.next_frame_at = Some(next + self.frame_interval);
            return;
        }

        self.next_frame_at = Some(now + self.frame_interval);
    }

    fn record_capture_elapsed(&mut self, started: Instant) {
        let elapsed_ms = started.elapsed().as_secs_f64() * 1000.0;
        self.capture_elapsed_total_ms += elapsed_ms;
        self.capture_elapsed_max_ms = self.capture_elapsed_max_ms.max(elapsed_ms);
    }

    fn record_nv12_conversion_elapsed(&mut self, started: Instant) {
        let elapsed_ms = started.elapsed().as_secs_f64() * 1000.0;
        self.nv12_conversion_count = self.nv12_conversion_count.wrapping_add(1);
        self.nv12_conversion_elapsed_total_ms += elapsed_ms;
        self.nv12_conversion_elapsed_max_ms = self.nv12_conversion_elapsed_max_ms.max(elapsed_ms);
    }

    fn capture_frame(&mut self, timeout_ms: u32) -> Result<bool, String> {
        let started = Instant::now();
        self.capture_attempts_total = self.capture_attempts_total.wrapping_add(1);
        let mut frame_info = DXGI_OUTDUPL_FRAME_INFO::default();
        let mut resource: Option<IDXGIResource> = None;

        let acquired = unsafe {
            self.duplication
                .AcquireNextFrame(timeout_ms, &mut frame_info, &mut resource)
        };

        if let Err(err) = acquired {
            self.record_capture_elapsed(started);
            if err.code() == DXGI_ERROR_WAIT_TIMEOUT {
                self.capture_timeouts_total = self.capture_timeouts_total.wrapping_add(1);
                return Ok(false);
            }
            if err.code() == DXGI_ERROR_ACCESS_LOST {
                return Err("Desktop duplication access was lost".to_owned());
            }
            return Err(format_windows_error("AcquireNextFrame", err));
        }

        let mut captured = false;
        let result = (|| -> Result<(), String> {
            let copied_frame = self.copy_acquired_frame(resource)?;
            let updated_pointer = self.update_pointer(&frame_info)?;
            if copied_frame {
                self.frames_copied_total = self.frames_copied_total.wrapping_add(1);
            }
            if updated_pointer {
                self.pointer_updates_total = self.pointer_updates_total.wrapping_add(1);
            }
            if copied_frame {
                self.pointer_restore = None;
            } else if updated_pointer {
                self.restore_pointer();
            }

            if (copied_frame || updated_pointer) && !self.last_frame.is_empty() {
                self.draw_pointer();
                self.frame_generation = self.frame_generation.wrapping_add(1);
                self.frames_captured_total = self.frames_captured_total.wrapping_add(1);
                captured = true;
            }
            Ok(())
        })();
        let release = unsafe { self.duplication.ReleaseFrame() };
        self.record_capture_elapsed(started);

        if let Err(err) = release {
            return Err(format_windows_error("ReleaseFrame", err));
        }

        result?;
        Ok(captured)
    }

    fn update_pointer(&mut self, frame_info: &DXGI_OUTDUPL_FRAME_INFO) -> Result<bool, String> {
        let mut updated = false;
        if frame_info.LastMouseUpdateTime != 0 {
            self.pointer.visible = frame_info.PointerPosition.Visible.as_bool();
            self.pointer.x = frame_info.PointerPosition.Position.x - self.output_left;
            self.pointer.y = frame_info.PointerPosition.Position.y - self.output_top;
            updated = true;
        }

        if frame_info.PointerShapeBufferSize == 0 {
            return Ok(updated);
        }

        let mut required_size = 0;
        let mut shape_info = DXGI_OUTDUPL_POINTER_SHAPE_INFO::default();
        self.pointer
            .shape
            .resize(frame_info.PointerShapeBufferSize as usize, 0);

        unsafe {
            self.duplication
                .GetFramePointerShape(
                    frame_info.PointerShapeBufferSize,
                    self.pointer.shape.as_mut_ptr().cast(),
                    &mut required_size,
                    &mut shape_info,
                )
                .map_err(|err| {
                    format_windows_error("IDXGIOutputDuplication::GetFramePointerShape", err)
                })?;
        }

        self.pointer.shape.truncate(required_size as usize);
        self.pointer.shape_info = shape_info;
        Ok(true)
    }

    fn draw_pointer(&mut self) {
        if !self.pointer.visible || self.pointer.shape.is_empty() || self.last_frame.is_empty() {
            return;
        }

        let shape_type = self.pointer.shape_info.Type;
        if shape_type == DXGI_OUTDUPL_POINTER_SHAPE_TYPE_COLOR.0 as u32 {
            self.draw_color_pointer();
        } else if shape_type == DXGI_OUTDUPL_POINTER_SHAPE_TYPE_MASKED_COLOR.0 as u32 {
            self.draw_masked_color_pointer();
        } else if shape_type == DXGI_OUTDUPL_POINTER_SHAPE_TYPE_MONOCHROME.0 as u32 {
            self.draw_monochrome_pointer();
        }
    }

    fn save_pointer_restore(&mut self, clip: &PointerClip) {
        let frame_stride = self.width as usize * 4;
        let restore_stride = clip.width * 4;
        let mut pixels = vec![0; restore_stride * clip.height];
        for row in 0..clip.height {
            let frame_start = (clip.dst_y + row) * frame_stride + clip.dst_x * 4;
            let frame_end = frame_start + restore_stride;
            let restore_start = row * restore_stride;
            let restore_end = restore_start + restore_stride;
            if frame_end > self.last_frame.len() {
                continue;
            }
            pixels[restore_start..restore_end]
                .copy_from_slice(&self.last_frame[frame_start..frame_end]);
        }

        self.pointer_restore = Some(PointerRestore {
            dst_x: clip.dst_x,
            dst_y: clip.dst_y,
            width: clip.width,
            height: clip.height,
            pixels,
        });
    }

    fn restore_pointer(&mut self) {
        let Some(restore) = self.pointer_restore.take() else {
            return;
        };

        let frame_stride = self.width as usize * 4;
        let restore_stride = restore.width * 4;
        for row in 0..restore.height {
            let frame_start = (restore.dst_y + row) * frame_stride + restore.dst_x * 4;
            let frame_end = frame_start + restore_stride;
            let restore_start = row * restore_stride;
            let restore_end = restore_start + restore_stride;
            if frame_end > self.last_frame.len() || restore_end > restore.pixels.len() {
                continue;
            }
            self.last_frame[frame_start..frame_end]
                .copy_from_slice(&restore.pixels[restore_start..restore_end]);
        }
    }

    fn pointer_clip(&self, shape_width: usize, shape_height: usize) -> Option<PointerClip> {
        if shape_width == 0 || shape_height == 0 || self.width == 0 || self.height == 0 {
            return None;
        }

        let dst_x = self.pointer.x.max(0) as usize;
        let dst_y = self.pointer.y.max(0) as usize;
        let src_x = self.pointer.x.saturating_neg().max(0) as usize;
        let src_y = self.pointer.y.saturating_neg().max(0) as usize;
        let frame_width = self.width as usize;
        let frame_height = self.height as usize;

        if src_x >= shape_width
            || src_y >= shape_height
            || dst_x >= frame_width
            || dst_y >= frame_height
        {
            return None;
        }

        let width = (shape_width - src_x).min(frame_width - dst_x);
        let height = (shape_height - src_y).min(frame_height - dst_y);
        if width == 0 || height == 0 {
            return None;
        }

        Some(PointerClip {
            src_x,
            src_y,
            dst_x,
            dst_y,
            width,
            height,
        })
    }

    fn draw_color_pointer(&mut self) {
        let info = self.pointer.shape_info;
        let Some(clip) = self.pointer_clip(info.Width as usize, info.Height as usize) else {
            return;
        };
        self.save_pointer_restore(&clip);

        let shape_pitch = info.Pitch as usize;
        let frame_stride = self.width as usize * 4;
        for row in 0..clip.height {
            let shape_row = (clip.src_y + row) * shape_pitch;
            let frame_row = (clip.dst_y + row) * frame_stride;
            for col in 0..clip.width {
                let shape_index = shape_row + (clip.src_x + col) * 4;
                let frame_index = frame_row + (clip.dst_x + col) * 4;
                if shape_index + 4 > self.pointer.shape.len()
                    || frame_index + 4 > self.last_frame.len()
                {
                    continue;
                }

                let alpha = self.pointer.shape[shape_index + 3] as u32;
                if alpha == 0 {
                    continue;
                }

                if alpha == 255 {
                    self.last_frame[frame_index..frame_index + 4]
                        .copy_from_slice(&self.pointer.shape[shape_index..shape_index + 4]);
                    self.last_frame[frame_index + 3] = 255;
                    continue;
                }

                for channel in 0..3 {
                    let foreground = self.pointer.shape[shape_index + channel] as u32;
                    let background = self.last_frame[frame_index + channel] as u32;
                    self.last_frame[frame_index + channel] =
                        ((foreground * alpha + background * (255 - alpha) + 127) / 255) as u8;
                }
                self.last_frame[frame_index + 3] = 255;
            }
        }
    }

    fn draw_masked_color_pointer(&mut self) {
        let info = self.pointer.shape_info;
        let Some(clip) = self.pointer_clip(info.Width as usize, info.Height as usize) else {
            return;
        };
        self.save_pointer_restore(&clip);

        let shape_pitch = info.Pitch as usize;
        let frame_stride = self.width as usize * 4;
        for row in 0..clip.height {
            let shape_row = (clip.src_y + row) * shape_pitch;
            let frame_row = (clip.dst_y + row) * frame_stride;
            for col in 0..clip.width {
                let shape_index = shape_row + (clip.src_x + col) * 4;
                let frame_index = frame_row + (clip.dst_x + col) * 4;
                if shape_index + 4 > self.pointer.shape.len()
                    || frame_index + 4 > self.last_frame.len()
                {
                    continue;
                }

                let shape_pixel = read_u32_le(&self.pointer.shape, shape_index);
                let frame_pixel = read_u32_le(&self.last_frame, frame_index);
                let output = if shape_pixel & 0xFF00_0000 != 0 {
                    (frame_pixel ^ shape_pixel) | 0xFF00_0000
                } else {
                    shape_pixel | 0xFF00_0000
                };
                self.last_frame[frame_index..frame_index + 4]
                    .copy_from_slice(&output.to_le_bytes());
            }
        }
    }

    fn draw_monochrome_pointer(&mut self) {
        let info = self.pointer.shape_info;
        let shape_height = info.Height as usize / 2;
        let Some(clip) = self.pointer_clip(info.Width as usize, shape_height) else {
            return;
        };
        self.save_pointer_restore(&clip);

        let shape_pitch = info.Pitch as usize;
        let frame_stride = self.width as usize * 4;
        for row in 0..clip.height {
            let shape_y = clip.src_y + row;
            let frame_row = (clip.dst_y + row) * frame_stride;
            for col in 0..clip.width {
                let shape_x = clip.src_x + col;
                let byte_index = shape_x / 8;
                let bit = 0x80 >> (shape_x % 8);
                let and_index = shape_y * shape_pitch + byte_index;
                let xor_index = (shape_y + shape_height) * shape_pitch + byte_index;
                let frame_index = frame_row + (clip.dst_x + col) * 4;
                if xor_index >= self.pointer.shape.len() || frame_index + 4 > self.last_frame.len()
                {
                    continue;
                }

                let and_mask = if self.pointer.shape[and_index] & bit != 0 {
                    0xFFFF_FFFF
                } else {
                    0xFF00_0000
                };
                let xor_mask = if self.pointer.shape[xor_index] & bit != 0 {
                    0x00FF_FFFF
                } else {
                    0
                };
                let frame_pixel = read_u32_le(&self.last_frame, frame_index);
                let output = (frame_pixel & and_mask) ^ xor_mask;
                self.last_frame[frame_index..frame_index + 4]
                    .copy_from_slice(&output.to_le_bytes());
            }
        }
    }

    fn copy_acquired_frame(&mut self, resource: Option<IDXGIResource>) -> Result<bool, String> {
        let Some(resource) = resource else {
            return Ok(false);
        };

        let texture: ID3D11Texture2D = resource
            .cast()
            .map_err(|err| format_windows_error("IDXGIResource::cast<ID3D11Texture2D>", err))?;

        unsafe {
            self.context.CopyResource(&self.staging, &texture);
        }

        let mut mapped = D3D11_MAPPED_SUBRESOURCE::default();
        unsafe {
            self.context
                .Map(&self.staging, 0, D3D11_MAP_READ, 0, Some(&mut mapped))
                .map_err(|err| format_windows_error("ID3D11DeviceContext::Map", err))?;
        }

        let copy_result = copy_mapped_frame(
            &mut self.last_frame,
            mapped,
            self.width as usize,
            self.height as usize,
        );

        unsafe {
            self.context.Unmap(&self.staging, 0);
        }

        copy_result?;
        Ok(true)
    }

    fn nv12_frame_size(&self) -> usize {
        self.width as usize * self.height as usize * 3 / 2
    }

    fn ensure_nv12_frame(&mut self) -> Result<(), String> {
        if self.nv12_generation == self.frame_generation
            && self.nv12_frame.len() == self.nv12_frame_size()
        {
            return Ok(());
        }

        let started = Instant::now();
        let y_size = self.width as usize * self.height as usize;
        let frame_size = self.nv12_frame_size();
        if self.nv12_frame.len() != frame_size {
            self.nv12_frame.resize(frame_size, 0);
        }

        let (y_plane, uv_plane) = self.nv12_frame.split_at_mut(y_size);
        let mut image = YuvBiPlanarImageMut {
            y_plane: BufferStoreMut::Borrowed(y_plane),
            y_stride: self.width,
            uv_plane: BufferStoreMut::Borrowed(uv_plane),
            uv_stride: self.width,
            width: self.width,
            height: self.height,
        };

        bgra_to_yuv_nv12(
            &mut image,
            self.last_frame.as_slice(),
            self.width * 4,
            YuvRange::Limited,
            YuvStandardMatrix::Bt601,
            YuvConversionMode::Fast,
        )
        .map_err(|err| format!("BGRA to NV12 conversion failed: {err}"))?;

        self.nv12_generation = self.frame_generation;
        self.record_nv12_conversion_elapsed(started);
        Ok(())
    }
}

fn create_duplication(output_index: u32) -> Result<DuplicationResources, String> {
    let feature_levels: [D3D_FEATURE_LEVEL; 4] = [
        D3D_FEATURE_LEVEL_11_1,
        D3D_FEATURE_LEVEL_11_0,
        D3D_FEATURE_LEVEL_10_1,
        D3D_FEATURE_LEVEL_10_0,
    ];
    let mut device: Option<ID3D11Device> = None;
    let mut context: Option<ID3D11DeviceContext> = None;

    unsafe {
        D3D11CreateDevice(
            None,
            D3D_DRIVER_TYPE_HARDWARE,
            HMODULE(ptr::null_mut()),
            D3D11_CREATE_DEVICE_BGRA_SUPPORT,
            Some(&feature_levels),
            D3D11_SDK_VERSION,
            Some(&mut device),
            None,
            Some(&mut context),
        )
        .map_err(|err| format_windows_error("D3D11CreateDevice", err))?;
    }

    let device = device.ok_or_else(|| "D3D11CreateDevice returned no device".to_owned())?;
    let context =
        context.ok_or_else(|| "D3D11CreateDevice returned no device context".to_owned())?;

    let dxgi_device: IDXGIDevice = device
        .cast()
        .map_err(|err| format_windows_error("ID3D11Device::cast<IDXGIDevice>", err))?;
    let adapter: IDXGIAdapter = unsafe {
        dxgi_device
            .GetAdapter()
            .map_err(|err| format_windows_error("IDXGIDevice::GetAdapter", err))?
    };
    let output: IDXGIOutput = unsafe {
        adapter
            .EnumOutputs(output_index)
            .map_err(|err| format_windows_error("IDXGIAdapter::EnumOutputs", err))?
    };
    let output1: IDXGIOutput1 = output
        .cast()
        .map_err(|err| format_windows_error("IDXGIOutput::cast<IDXGIOutput1>", err))?;
    let duplication = unsafe {
        output1
            .DuplicateOutput(&device)
            .map_err(|err| format_windows_error("IDXGIOutput1::DuplicateOutput", err))?
    };

    let desc = unsafe {
        output
            .GetDesc()
            .map_err(|err| format_windows_error("IDXGIOutput::GetDesc", err))?
    };
    let width = (desc.DesktopCoordinates.right - desc.DesktopCoordinates.left).max(0) as u32;
    let height = (desc.DesktopCoordinates.bottom - desc.DesktopCoordinates.top).max(0) as u32;
    if width == 0 || height == 0 {
        return Err("Selected desktop output has no captureable dimensions".to_owned());
    }

    Ok((
        device,
        context,
        duplication,
        width,
        height,
        desc.DesktopCoordinates.left,
        desc.DesktopCoordinates.top,
    ))
}

fn create_staging_texture(
    device: &ID3D11Device,
    width: u32,
    height: u32,
) -> Result<ID3D11Texture2D, String> {
    let desc = D3D11_TEXTURE2D_DESC {
        Width: width,
        Height: height,
        MipLevels: 1,
        ArraySize: 1,
        Format: DXGI_FORMAT_B8G8R8A8_UNORM,
        SampleDesc: DXGI_SAMPLE_DESC {
            Count: 1,
            Quality: 0,
        },
        Usage: D3D11_USAGE_STAGING,
        BindFlags: 0,
        CPUAccessFlags: D3D11_CPU_ACCESS_READ.0 as u32,
        MiscFlags: 0,
    };

    let mut texture: Option<ID3D11Texture2D> = None;
    unsafe {
        device
            .CreateTexture2D(&desc, None, Some(&mut texture))
            .map_err(|err| format_windows_error("ID3D11Device::CreateTexture2D", err))?;
    };
    texture.ok_or_else(|| "ID3D11Device::CreateTexture2D returned no texture".to_owned())
}

fn copy_mapped_frame(
    destination: &mut Vec<u8>,
    mapped: D3D11_MAPPED_SUBRESOURCE,
    width: usize,
    height: usize,
) -> Result<(), String> {
    let stride = width
        .checked_mul(4)
        .ok_or_else(|| "Desktop frame stride overflowed".to_owned())?;
    let frame_size = stride
        .checked_mul(height)
        .ok_or_else(|| "Desktop frame size overflowed".to_owned())?;

    if destination.len() != frame_size {
        destination.resize(frame_size, 0);
    }

    let source = mapped.pData as *const u8;
    if source.is_null() {
        return Err("Mapped desktop frame had a null data pointer".to_owned());
    }

    let row_pitch = mapped.RowPitch as usize;
    if row_pitch < stride {
        return Err("Mapped desktop frame row pitch is smaller than the frame stride".to_owned());
    }

    for row in 0..height {
        unsafe {
            ptr::copy_nonoverlapping(
                source.add(row * row_pitch),
                destination.as_mut_ptr().add(row * stride),
                stride,
            );
        }
    }

    Ok(())
}

fn read_u32_le(bytes: &[u8], index: usize) -> u32 {
    u32::from_le_bytes([
        bytes[index],
        bytes[index + 1],
        bytes[index + 2],
        bytes[index + 3],
    ])
}

fn format_windows_error(operation: &str, err: WindowsError) -> String {
    format!("{operation} failed: {err}")
}
