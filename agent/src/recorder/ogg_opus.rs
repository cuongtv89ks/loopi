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

    }

    struct Page {
        flags: u8,
        granule: u64,
        serial: u32,
        sequence: u32,
        packets: Vec<Vec<u8>>,
    }

    /// Walk the raw page structure (RFC 3533): header, segment table, lacing into packets.
    fn parse_pages(bytes: &[u8]) -> Vec<Page> {
        let mut pages = Vec::new();
        let mut pos = 0;
        while pos < bytes.len() {
            assert_eq!(&bytes[pos..pos + 4], b"OggS", "capture pattern at page start");
            assert_eq!(bytes[pos + 4], 0, "stream structure version");
            let flags = bytes[pos + 5];
            let granule = u64::from_le_bytes(bytes[pos + 6..pos + 14].try_into().unwrap());
            let serial = u32::from_le_bytes(bytes[pos + 14..pos + 18].try_into().unwrap());
            let sequence = u32::from_le_bytes(bytes[pos + 18..pos + 22].try_into().unwrap());
            let count = bytes[pos + 26] as usize;
            let table = &bytes[pos + 27..pos + 27 + count];
            let mut at = pos + 27 + count;
            let mut packets = Vec::new();
            let mut current = Vec::new();
            for &lace in table {
                current.extend_from_slice(&bytes[at..at + lace as usize]);
                at += lace as usize;
                if lace < 255 {
                    packets.push(std::mem::take(&mut current));
                }
            }
            assert!(current.is_empty(), "no packet continues across pages in these tests");
            pages.push(Page { flags, granule, serial, sequence, packets });
            pos = at;
        }
        pages
    }

    #[test]
    fn page_structure_follows_rfc_7845() {
        const BOS: u8 = 0x02;
        const EOS: u8 = 0x04;
        let mut writer = OggOpusWriter::new(Vec::new(), 0xA1B2_C3D4).unwrap();
        for i in 0..50 {
            writer.write_frame(&tone(i)).unwrap();
        }
        let bytes = writer.finish().unwrap();
        let pages = parse_pages(&bytes);
        assert!(pages.len() >= 3);

        let head = &pages[0];
        assert_eq!(head.flags, BOS, "OpusHead page is BOS only");
        assert_eq!(head.granule, 0);
        assert_eq!(head.packets.len(), 1, "OpusHead alone on its page");
        let h = &head.packets[0];
        assert_eq!(h.len(), 19);
        assert_eq!(&h[..8], b"OpusHead");
        assert_eq!(h[8], 1, "version");
        assert_eq!(h[9], 1, "channels");
        assert_eq!(u16::from_le_bytes([h[10], h[11]]), 312, "pre-skip");
        assert_eq!(u32::from_le_bytes(h[12..16].try_into().unwrap()), 16_000, "input rate");
        assert_eq!(i16::from_le_bytes([h[16], h[17]]), 0, "output gain");
        assert_eq!(h[18], 0, "channel mapping family");

        let tags = &pages[1];
        assert_eq!(tags.flags, 0);
        assert_eq!(tags.granule, 0);
        assert_eq!(tags.packets.len(), 1, "OpusTags alone on its page");
        let t = &tags.packets[0];
        assert_eq!(&t[..8], b"OpusTags");
        let vendor_len = u32::from_le_bytes(t[8..12].try_into().unwrap()) as usize;
        assert_eq!(&t[12..12 + vendor_len], b"loopi-agent");
        assert_eq!(u32::from_le_bytes(t[12 + vendor_len..16 + vendor_len].try_into().unwrap()), 0);

        let mut completed = 0u64;
        for (i, page) in pages.iter().enumerate() {
            assert_eq!(page.serial, 0xA1B2_C3D4, "one constant serial");
            assert_eq!(page.sequence as usize, i, "page sequence numbers are consecutive");
            if i < 2 {
                continue;
            }
            completed += page.packets.len() as u64;
            assert_eq!(page.granule, completed * GRANULE_PER_FRAME, "granule = 960 x packets completed at page end");
            let is_last = i == pages.len() - 1;
            assert_eq!(page.flags & EOS != 0, is_last, "EOS only on the last page");
            assert_eq!(page.flags & BOS, 0);
        }
        assert_eq!(completed, 50);
        assert_eq!(pages.last().unwrap().granule, 50 * GRANULE_PER_FRAME);
    }

    #[test]
    fn wrong_frame_size_is_rejected() {
        let mut writer = OggOpusWriter::new(Vec::new(), 1).unwrap();
        assert!(matches!(writer.write_frame(&[0; 10]), Err(OggOpusError::FrameSize(10))));
    }
}
