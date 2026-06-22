use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyList};
use pyo3::wrap_pyfunction;

#[pyfunction]
fn pcm16_add<'py>(
    py: Python<'py>,
    left: &Bound<'_, PyBytes>,
    right: &Bound<'_, PyBytes>,
) -> PyResult<Bound<'py, PyBytes>> {
    let mixed = crate::audio::pcm16_add(left.as_bytes(), right.as_bytes())?;
    Ok(PyBytes::new(py, mixed.as_slice()))
}

#[pyfunction]
fn pcm16_mul<'py>(
    py: Python<'py>,
    data: &Bound<'_, PyBytes>,
    factor: f64,
) -> PyResult<Bound<'py, PyBytes>> {
    let scaled = crate::audio::pcm16_mul(data.as_bytes(), factor)?;
    Ok(PyBytes::new(py, scaled.as_slice()))
}

#[pyfunction]
fn pcm16_mix<'py>(py: Python<'py>, chunks: &Bound<'_, PyList>) -> PyResult<Bound<'py, PyBytes>> {
    let mut owned = Vec::with_capacity(chunks.len());
    for item in chunks.iter() {
        owned.push(item.extract::<Vec<u8>>()?);
    }
    let buffers: Vec<&[u8]> = owned.iter().map(Vec::as_slice).collect();

    let mixed = crate::audio::pcm16_mix(buffers.as_slice())?;
    Ok(PyBytes::new(py, mixed.as_slice()))
}

pub(super) fn add_to_module(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(pcm16_add, m)?)?;
    m.add_function(wrap_pyfunction!(pcm16_mul, m)?)?;
    m.add_function(wrap_pyfunction!(pcm16_mix, m)?)?;
    Ok(())
}
