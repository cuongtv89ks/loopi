//! Minimal Ogg Opus writer (RFC 7845) for 16 kHz mono speech.
use std::io::Write;

use ogg::writing::{PacketWriteEndInfo, PacketWriter};
use opus::{Application, Bitrate, Channels, Encoder};

pub const SAMPLE_RATE: u32 = 16_000;
/// 20 ms at 16 kHz.
pub const FRAME_SAMPLES: usize = 320;
/// Ogg Opus granule positions always count 48 kHz samples.
pub const GRANULE_PER_FRAME: u64 = 960;
/// libopus look-ahead (2.5 ms + 4 ms delay compensation) expressed at 48 kHz.
pub const PRE_SKIP: u16 = 312;
pub const BITRATE_BPS: i32 = 24_000;
const MAX_PACKET: usize = 1500;

#[derive(Debug, thiserror::Error)]
pub enum OggOpusError {
    #[error("opus: {0}")]
    Opus(#[from] opus::Error),
    #[error("io: {0}")]
    Io(#[from] std::io::Error),
    #[error("a frame must have {FRAME_SAMPLES} samples, got {0}")]
    FrameSize(usize),
}

pub struct OggOpusWriter<W: Write> {
    writer: PacketWriter<'static, W>,
    encoder: Encoder,
    serial: u32,
    granule: u64,
    pending: Option<Vec<u8>>,
    frames: u64,
}

fn opus_head() -> Vec<u8> {
    let mut head = Vec::with_capacity(19);
    head.extend_from_slice(b"OpusHead");
    head.push(1); // version
    head.push(1); // channels
    head.extend_from_slice(&PRE_SKIP.to_le_bytes());
    head.extend_from_slice(&SAMPLE_RATE.to_le_bytes());
    head.extend_from_slice(&0i16.to_le_bytes()); // output gain
    head.push(0); // channel mapping family
    head
}

fn opus_tags() -> Vec<u8> {
    let vendor = b"loopi-agent";
    let mut tags = Vec::with_capacity(8 + 4 + vendor.len() + 4);
    tags.extend_from_slice(b"OpusTags");
    tags.extend_from_slice(&(vendor.len() as u32).to_le_bytes());
    tags.extend_from_slice(vendor);
    tags.extend_from_slice(&0u32.to_le_bytes()); // no user comments
    tags
}

impl<W: Write> OggOpusWriter<W> {
    pub fn new(out: W, serial: u32) -> Result<Self, OggOpusError> {
        let mut encoder = Encoder::new(SAMPLE_RATE, Channels::Mono, Application::Voip)?;
        encoder.set_bitrate(Bitrate::Bits(BITRATE_BPS))?;
        let mut writer = PacketWriter::new(out);
        writer.write_packet(opus_head(), serial, PacketWriteEndInfo::EndPage, 0)?;
        writer.write_packet(opus_tags(), serial, PacketWriteEndInfo::EndPage, 0)?;
        Ok(Self { writer, encoder, serial, granule: 0, pending: None, frames: 0 })
    }

    /// Encode one 20 ms frame. Packets are written one frame late so the final one can carry
    /// the end-of-stream flag.
    pub fn write_frame(&mut self, pcm: &[i16]) -> Result<(), OggOpusError> {
        if pcm.len() != FRAME_SAMPLES {
            return Err(OggOpusError::FrameSize(pcm.len()));
        }
        let mut buf = [0u8; MAX_PACKET];
        let len = self.encoder.encode(pcm, &mut buf)?;
        if let Some(previous) = self.pending.replace(buf[..len].to_vec()) {
            self.granule += GRANULE_PER_FRAME;
            self.writer.write_packet(previous, self.serial, PacketWriteEndInfo::NormalPacket, self.granule)?;
        }
        self.frames += 1;
        Ok(())
    }

    pub fn frames(&self) -> u64 {
        self.frames
    }

    pub fn finish(mut self) -> Result<W, OggOpusError> {
        let last = self.pending.take().unwrap_or_default();
        if !last.is_empty() {
            self.granule += GRANULE_PER_FRAME;
        }
        self.writer.write_packet(last, self.serial, PacketWriteEndInfo::EndStream, self.granule)?;
        Ok(self.writer.into_inner())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use ogg::reading::PacketReader;
    use std::io::Cursor;

    fn tone(frame_index: usize) -> Vec<i16> {
        (0..FRAME_SAMPLES)
            .map(|i| {
                let t = (frame_index * FRAME_SAMPLES + i) as f32 / SAMPLE_RATE as f32;
                ((t * 440.0 * std::f32::consts::TAU).sin() * 8000.0) as i16
            })
            .collect()
    }

    #[test]
    fn written_stream_is_valid_ogg_opus() {
        let mut writer = OggOpusWriter::new(Vec::new(), 7).unwrap();
        for i in 0..50 {
            writer.write_frame(&tone(i)).unwrap();
        }
        assert_eq!(writer.frames(), 50);
        let bytes = writer.finish().unwrap();

        let mut reader = PacketReader::new(Cursor::new(bytes));
        let head = reader.read_packet().unwrap().unwrap();
        assert_eq!(&head.data[..8], b"OpusHead");
        assert_eq!(u16::from_le_bytes([head.data[10], head.data[11]]), PRE_SKIP);
        assert_eq!(u32::from_le_bytes(head.data[12..16].try_into().unwrap()), SAMPLE_RATE);
        let tags = reader.read_packet().unwrap().unwrap();
        assert_eq!(&tags.data[..8], b"OpusTags");

        let mut decoder = opus::Decoder::new(SAMPLE_RATE, opus::Channels::Mono).unwrap();
        let mut pcm = [0i16; FRAME_SAMPLES];
        let mut packets = Vec::new();
        while let Some(packet) = reader.read_packet().unwrap() {
            assert_eq!(decoder.decode(&packet.data, &mut pcm, false).unwrap(), FRAME_SAMPLES);
            packets.push(packet);
        }
        assert_eq!(packets.len(), 50);
        let last = packets.last().unwrap();
        assert!(last.last_in_stream());
        assert_eq!(last.absgp_page(), 50 * GRANULE_PER_FRAME);

        // Structure: BOS on the head only, EOS on the last packet only, granules never go back.
        assert!(head.first_in_stream());
        assert!(!tags.first_in_stream());
        assert_eq!(head.stream_serial(), 7);
        assert_eq!(&head.data[8..10], &[1, 1], "version 1, mono");
        assert!(packets[..packets.len() - 1].iter().all(|p| !p.last_in_stream()));
        let granules: Vec<u64> = packets.iter().map(|p| p.absgp_page()).collect();
        assert!(granules.windows(2).all(|w| w[0] <= w[1]));
        assert!(granules.iter().all(|g| g % GRANULE_PER_FRAME == 0 && *g > 0));
    }

    #[test]
    fn stream_starts_with_capture_pattern_and_two_header_pages() {
        let mut writer = OggOpusWriter::new(Vec::new(), 3).unwrap();
        writer.write_frame(&tone(0)).unwrap();
        let bytes = writer.finish().unwrap();
        assert_eq!(&bytes[..4], b"OggS");
        let pages = bytes.windows(4).filter(|w| *w == b"OggS").count();
        assert!(pages >= 3, "OpusHead page, OpusTags page and at least one audio page");
        assert!(bytes.windows(8).any(|w| w == b"OpusTags"));
    }

    #[test]
    fn wrong_frame_size_is_rejected() {
        let mut writer = OggOpusWriter::new(Vec::new(), 1).unwrap();
        assert!(matches!(writer.write_frame(&[0; 10]), Err(OggOpusError::FrameSize(10))));
    }
}
