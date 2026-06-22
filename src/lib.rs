use pyo3::prelude::*;

mod audio;
mod capture;
mod rtp;
mod transport;
mod video;

mod python;

#[pymodule]
fn _native_voice(m: &Bound<'_, PyModule>) -> PyResult<()> {
    python::add_to_module(m)
}
