use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict};
use std::sync::{Mutex, MutexGuard, TryLockError};
use std::time::Instant;

#[derive(Clone, Copy, Default)]
struct DesktopPipeStats {
    read_count: u64,
    read_empty_count: u64,
    read_total_ms: f64,
    read_max_ms: f64,
    write_count: u64,
    write_total_ms: f64,
    write_max_ms: f64,
    bytes_written: u64,
}

impl DesktopPipeStats {
    fn record_read(&mut self, elapsed_ms: f64, empty: bool) {
        self.read_count = self.read_count.wrapping_add(1);
        self.read_total_ms += elapsed_ms;
        self.read_max_ms = self.read_max_ms.max(elapsed_ms);
        if empty {
            self.read_empty_count = self.read_empty_count.wrapping_add(1);
        }
    }

    fn record_write(&mut self, elapsed_ms: f64, size: usize) {
        self.write_count = self.write_count.wrapping_add(1);
        self.write_total_ms += elapsed_ms;
        self.write_max_ms = self.write_max_ms.max(elapsed_ms);
        self.bytes_written = self.bytes_written.wrapping_add(size as u64);
    }
}

#[pyclass(name = "DesktopFrameSource")]
struct PyDesktopFrameSource {
    inner: Mutex<crate::capture::DesktopFrameSource>,
    capture_stats: Mutex<crate::capture::DesktopCaptureStats>,
    pipe_stats: Mutex<DesktopPipeStats>,
}

fn lock_source(
    source: &Mutex<crate::capture::DesktopFrameSource>,
) -> Result<MutexGuard<'_, crate::capture::DesktopFrameSource>, String> {
    source
        .lock()
        .map_err(|_| "desktop frame source lock poisoned".to_owned())
}

fn update_capture_stats(
    source: &crate::capture::DesktopFrameSource,
    shared_stats: &Mutex<crate::capture::DesktopCaptureStats>,
) {
    if let Ok(mut shared_stats) = shared_stats.lock() {
        *shared_stats = source.stats();
    }
}

#[cfg(windows)]
fn write_all_to_handle(handle: usize, mut data: &[u8]) -> Result<(), String> {
    use windows::Win32::Foundation::HANDLE;
    use windows::Win32::Storage::FileSystem::WriteFile;

    let handle = HANDLE(handle as *mut core::ffi::c_void);
    while !data.is_empty() {
        let mut written = 0u32;
        unsafe {
            WriteFile(handle, Some(data), Some(&mut written), None)
                .map_err(|err| err.to_string())?;
        }
        if written == 0 {
            return Err("WriteFile wrote zero bytes".to_owned());
        }
        data = &data[written as usize..];
    }
    Ok(())
}

#[cfg(not(windows))]
fn write_all_to_handle(_handle: usize, _data: &[u8]) -> Result<(), String> {
    Err("Native desktop pipe writing is only supported on Windows".to_owned())
}

fn pipe_desktop_frames_to_handle(
    source: &Mutex<crate::capture::DesktopFrameSource>,
    shared_capture_stats: &Mutex<crate::capture::DesktopCaptureStats>,
    shared_stats: &Mutex<DesktopPipeStats>,
    handle: usize,
) -> Result<DesktopPipeStats, String> {
    let mut stats = DesktopPipeStats::default();
    loop {
        let read_started = Instant::now();
        let mut source = lock_source(source)?;
        source.prepare_frame()?;
        update_capture_stats(&source, shared_capture_stats);
        let frame = source.frame();
        let read_ms = read_started.elapsed().as_secs_f64() * 1000.0;
        stats.record_read(read_ms, frame.is_empty());
        if let Ok(mut shared_stats) = shared_stats.lock() {
            shared_stats.record_read(read_ms, frame.is_empty());
        }
        if frame.is_empty() {
            return Ok(stats);
        }

        let frame_len = frame.len();
        let write_started = Instant::now();
        let write_result = write_all_to_handle(handle, frame);
        let write_ms = write_started.elapsed().as_secs_f64() * 1000.0;
        drop(source);

        if write_result.is_err() {
            return Ok(stats);
        }
        stats.record_write(write_ms, frame_len);
        if let Ok(mut shared_stats) = shared_stats.lock() {
            shared_stats.record_write(write_ms, frame_len);
        }
    }
}

fn pipe_stats_dict<'py>(py: Python<'py>, stats: DesktopPipeStats) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    dict.set_item("readCount", stats.read_count)?;
    dict.set_item("readEmptyCount", stats.read_empty_count)?;
    dict.set_item("readTotalMs", stats.read_total_ms)?;
    dict.set_item("readMaxMs", stats.read_max_ms)?;
    dict.set_item("writeCount", stats.write_count)?;
    dict.set_item("writeTotalMs", stats.write_total_ms)?;
    dict.set_item("writeMaxMs", stats.write_max_ms)?;
    dict.set_item("bytesWritten", stats.bytes_written)?;
    Ok(dict)
}

fn capture_stats_dict<'py>(
    py: Python<'py>,
    stats: crate::capture::DesktopCaptureStats,
) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    dict.set_item("framesReadTotal", stats.frames_read_total)?;
    dict.set_item("framesCapturedTotal", stats.frames_captured_total)?;
    dict.set_item("framesCopiedTotal", stats.frames_copied_total)?;
    dict.set_item("framesReusedTotal", stats.frames_reused_total)?;
    dict.set_item("pointerUpdatesTotal", stats.pointer_updates_total)?;
    dict.set_item("captureAttemptsTotal", stats.capture_attempts_total)?;
    dict.set_item("captureTimeoutsTotal", stats.capture_timeouts_total)?;
    dict.set_item("captureCallMeanMs", stats.capture_call_mean_ms)?;
    dict.set_item("captureCallMaxMs", stats.capture_call_max_ms)?;
    dict.set_item("nv12ConversionCount", stats.nv12_conversion_count)?;
    dict.set_item("nv12ConversionMeanMs", stats.nv12_conversion_mean_ms)?;
    dict.set_item("nv12ConversionMaxMs", stats.nv12_conversion_max_ms)?;
    dict.set_item("frameIntervalsCount", stats.frame_intervals_count)?;
    dict.set_item("frameIntervalMeanMs", stats.frame_interval_mean_ms)?;
    dict.set_item("frameIntervalStdevMs", stats.frame_interval_stdev_ms)?;
    Ok(dict)
}

#[pymethods]
impl PyDesktopFrameSource {
    #[new]
    fn new(output_index: u32, fps: u32) -> PyResult<Self> {
        let inner = crate::capture::DesktopFrameSource::new(output_index, fps)
            .map_err(pyo3::exceptions::PyRuntimeError::new_err)?;
        Ok(Self {
            inner: Mutex::new(inner),
            capture_stats: Mutex::new(crate::capture::DesktopCaptureStats::default()),
            pipe_stats: Mutex::new(DesktopPipeStats::default()),
        })
    }

    #[getter]
    fn width(&self) -> PyResult<u32> {
        Ok(lock_source(&self.inner)
            .map_err(pyo3::exceptions::PyRuntimeError::new_err)?
            .width())
    }

    #[getter]
    fn height(&self) -> PyResult<u32> {
        Ok(lock_source(&self.inner)
            .map_err(pyo3::exceptions::PyRuntimeError::new_err)?
            .height())
    }

    #[getter]
    fn frame_size(&self) -> PyResult<usize> {
        Ok(lock_source(&self.inner)
            .map_err(pyo3::exceptions::PyRuntimeError::new_err)?
            .frame_size())
    }

    #[getter]
    fn pixel_format(&self) -> PyResult<&'static str> {
        Ok(lock_source(&self.inner)
            .map_err(pyo3::exceptions::PyRuntimeError::new_err)?
            .pixel_format())
    }

    #[getter]
    fn preferred_read_size(&self) -> PyResult<usize> {
        Ok(lock_source(&self.inner)
            .map_err(pyo3::exceptions::PyRuntimeError::new_err)?
            .frame_size())
    }

    fn set_pixel_format(&self, pixel_format: &str) -> PyResult<()> {
        lock_source(&self.inner)
            .map_err(pyo3::exceptions::PyRuntimeError::new_err)?
            .set_pixel_format(pixel_format)
            .map_err(pyo3::exceptions::PyValueError::new_err)
    }

    fn read<'py>(&self, py: Python<'py>, _size: usize) -> PyResult<Bound<'py, PyBytes>> {
        py.detach(|| -> Result<(), String> {
            let mut source = lock_source(&self.inner)?;
            source.prepare_frame()?;
            update_capture_stats(&source, &self.capture_stats);
            Ok(())
        })
        .map_err(pyo3::exceptions::PyRuntimeError::new_err)?;
        let source = lock_source(&self.inner).map_err(pyo3::exceptions::PyRuntimeError::new_err)?;
        Ok(PyBytes::new(py, source.frame()))
    }

    fn write_to_handle<'py>(&self, py: Python<'py>, handle: usize) -> PyResult<Bound<'py, PyDict>> {
        let stats = py
            .detach(|| {
                pipe_desktop_frames_to_handle(
                    &self.inner,
                    &self.capture_stats,
                    &self.pipe_stats,
                    handle,
                )
            })
            .map_err(pyo3::exceptions::PyRuntimeError::new_err)?;
        pipe_stats_dict(py, stats)
    }

    fn pipe_stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let stats = *self.pipe_stats.lock().map_err(|_| {
            pyo3::exceptions::PyRuntimeError::new_err("Desktop pipe stats lock poisoned")
        })?;
        pipe_stats_dict(py, stats)
    }

    fn stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let stats = match self.inner.try_lock() {
            Ok(source) => {
                let stats = source.stats();
                update_capture_stats(&source, &self.capture_stats);
                stats
            }
            Err(TryLockError::WouldBlock) => *self.capture_stats.lock().map_err(|_| {
                pyo3::exceptions::PyRuntimeError::new_err("Desktop capture stats lock poisoned")
            })?,
            Err(TryLockError::Poisoned(_)) => {
                return Err(pyo3::exceptions::PyRuntimeError::new_err(
                    "Desktop frame source lock poisoned",
                ));
            }
        };
        capture_stats_dict(py, stats)
    }

    fn close(&self) -> PyResult<()> {
        lock_source(&self.inner)
            .map_err(pyo3::exceptions::PyRuntimeError::new_err)?
            .close();
        Ok(())
    }
}

pub(super) fn add_to_module(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyDesktopFrameSource>()?;
    Ok(())
}
