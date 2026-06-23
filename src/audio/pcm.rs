use std::fmt;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AudioError {
    NotWholeFrame,
    LengthMismatch,
}

impl fmt::Display for AudioError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotWholeFrame => write!(f, "Not a whole number of 16-bit PCM frames"),
            Self::LengthMismatch => write!(f, "PCM fragments must have the same length"),
        }
    }
}

impl std::error::Error for AudioError {}

fn validate_pcm16(data: &[u8]) -> Result<(), AudioError> {
    if !data.len().is_multiple_of(2) {
        return Err(AudioError::NotWholeFrame);
    }
    Ok(())
}

fn clamp_i16(value: i32) -> i16 {
    value.clamp(i16::MIN as i32, i16::MAX as i32) as i16
}

fn clamp_sample_f64(value: f64) -> i16 {
    if value.is_nan() {
        return 0;
    }
    let clamped = value.clamp(i16::MIN as f64, i16::MAX as f64);
    let rounded = clamped + (0.5f64).copysign(clamped);
    rounded.clamp(i16::MIN as f64, i16::MAX as f64) as i16
}

pub fn pcm16_add(left: &[u8], right: &[u8]) -> Result<Vec<u8>, AudioError> {
    validate_pcm16(left)?;
    validate_pcm16(right)?;
    if left.len() != right.len() {
        return Err(AudioError::LengthMismatch);
    }

    let mut output = vec![0u8; left.len()];
    for ((left_sample, right_sample), output_sample) in left
        .chunks_exact(2)
        .zip(right.chunks_exact(2))
        .zip(output.chunks_exact_mut(2))
    {
        let left_value = i16::from_le_bytes([left_sample[0], left_sample[1]]);
        let right_value = i16::from_le_bytes([right_sample[0], right_sample[1]]);
        output_sample.copy_from_slice(&left_value.saturating_add(right_value).to_le_bytes());
    }
    Ok(output)
}

pub fn pcm16_mul(data: &[u8], factor: f64) -> Result<Vec<u8>, AudioError> {
    validate_pcm16(data)?;

    let mut output = vec![0u8; data.len()];
    for (sample, output_sample) in data.chunks_exact(2).zip(output.chunks_exact_mut(2)) {
        let value = i16::from_le_bytes([sample[0], sample[1]]) as f64;
        output_sample.copy_from_slice(&clamp_sample_f64(value * factor).to_le_bytes());
    }
    Ok(output)
}

pub fn pcm16_mix(chunks: &[&[u8]]) -> Result<Vec<u8>, AudioError> {
    let Some(max_len) = chunks.iter().map(|chunk| chunk.len()).max() else {
        return Ok(Vec::new());
    };

    if !max_len.is_multiple_of(2) {
        return Err(AudioError::NotWholeFrame);
    }
    for chunk in chunks {
        validate_pcm16(chunk)?;
    }

    let sample_count = max_len / 2;
    let mut mixed = vec![0i32; sample_count];

    for chunk in chunks.iter() {
        for (index, sample) in chunk.chunks_exact(2).enumerate() {
            let value = i16::from_le_bytes([sample[0], sample[1]]) as i32;
            mixed[index] = mixed[index].saturating_add(value);
        }
    }

    let mut output = vec![0u8; max_len];
    for (sample, output_sample) in mixed.into_iter().zip(output.chunks_exact_mut(2)) {
        output_sample.copy_from_slice(&clamp_i16(sample).to_le_bytes());
    }
    Ok(output)
}
