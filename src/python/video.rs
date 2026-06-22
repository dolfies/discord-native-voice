use pyo3::prelude::*;
use pyo3::types::PyBytes;
use pyo3::wrap_pyfunction;

use crate::video::VideoDepacketizer;

#[pyfunction]
fn strip_h264_filler_nalus<'py>(
    py: Python<'py>,
    frame: &Bound<'py, PyBytes>,
) -> Bound<'py, PyBytes> {
    let frame_bytes = frame.as_bytes();
    let mut starts = Vec::new();
    let mut index = 0;
    while index + 3 <= frame_bytes.len() {
        if index + 4 <= frame_bytes.len() && frame_bytes[index..index + 4] == [0, 0, 0, 1] {
            starts.push((index, 4));
            index += 4;
        } else if frame_bytes[index..index + 3] == [0, 0, 1] {
            starts.push((index, 3));
            index += 3;
        } else {
            index += 1;
        }
    }

    if starts.is_empty() {
        return frame.clone();
    }

    let mut output = Vec::with_capacity(frame_bytes.len());
    let mut removed = false;
    for (position, (start, prefix_len)) in starts.iter().copied().enumerate() {
        let payload_start = start + prefix_len;
        let payload_end = starts
            .get(position + 1)
            .map(|(next, _)| *next)
            .unwrap_or(frame_bytes.len());
        if payload_start >= payload_end {
            continue;
        }
        if frame_bytes[payload_start] & 0x1F == 12 {
            removed = true;
            continue;
        }
        output.extend_from_slice(&frame_bytes[start..payload_end]);
    }

    if !removed || output.is_empty() {
        return frame.clone();
    }
    PyBytes::new(py, &output)
}

fn push_depacketizer_packet<'py, D: VideoDepacketizer>(
    py: Python<'py>,
    depacketizer: &mut D,
    payload: &Bound<'_, PyBytes>,
    marker: bool,
    sequence: u16,
    timestamp: u32,
) -> PyResult<Option<Bound<'py, PyBytes>>> {
    let frame = depacketizer.push_packet(payload.as_bytes(), marker, sequence, timestamp)?;
    Ok(frame.map(|frame| PyBytes::new(py, frame.as_slice())))
}

macro_rules! py_depacketizer {
    ($py_name:ident, $inner_name:ident, $class_name:literal) => {
        #[pyclass(name = $class_name)]
        struct $py_name {
            inner: crate::video::$inner_name,
        }

        #[pymethods]
        impl $py_name {
            #[new]
            fn new() -> Self {
                Self {
                    inner: crate::video::$inner_name::new(),
                }
            }

            fn push_packet<'py>(
                &mut self,
                py: Python<'py>,
                payload: &Bound<'_, PyBytes>,
                marker: bool,
                sequence: u16,
                timestamp: u32,
            ) -> PyResult<Option<Bound<'py, PyBytes>>> {
                push_depacketizer_packet(py, &mut self.inner, payload, marker, sequence, timestamp)
            }
        }
    };
}

py_depacketizer!(PyH264Depacketizer, H264Depacketizer, "H264Depacketizer");
py_depacketizer!(PyH265Depacketizer, H265Depacketizer, "H265Depacketizer");
py_depacketizer!(PyVP8Depacketizer, VP8Depacketizer, "VP8Depacketizer");
py_depacketizer!(PyVP9Depacketizer, VP9Depacketizer, "VP9Depacketizer");
py_depacketizer!(PyAV1Depacketizer, AV1Depacketizer, "AV1Depacketizer");

pub(super) fn add_to_module(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(strip_h264_filler_nalus, m)?)?;
    m.add_class::<PyH264Depacketizer>()?;
    m.add_class::<PyH265Depacketizer>()?;
    m.add_class::<PyVP8Depacketizer>()?;
    m.add_class::<PyVP9Depacketizer>()?;
    m.add_class::<PyAV1Depacketizer>()?;
    Ok(())
}
