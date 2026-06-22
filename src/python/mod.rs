use pyo3::prelude::*;

mod audio;
mod capture;
mod net;
mod transport;
mod video;
mod video_send;

impl From<crate::transport::CryptoError> for PyErr {
    fn from(err: crate::transport::CryptoError) -> Self {
        pyo3::exceptions::PyRuntimeError::new_err(err.to_string())
    }
}

impl From<crate::rtp::PacketError> for PyErr {
    fn from(err: crate::rtp::PacketError) -> Self {
        pyo3::exceptions::PyValueError::new_err(err.to_string())
    }
}

impl From<crate::audio::AudioError> for PyErr {
    fn from(err: crate::audio::AudioError) -> Self {
        pyo3::exceptions::PyValueError::new_err(err.to_string())
    }
}

pub(crate) fn add_to_module(m: &Bound<'_, PyModule>) -> PyResult<()> {
    transport::add_to_module(m)?;
    video::add_to_module(m)?;
    video_send::add_to_module(m)?;
    audio::add_to_module(m)?;
    capture::add_to_module(m)?;
    net::add_to_module(m)?;
    Ok(())
}
