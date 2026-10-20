//! Capture of video and audio. The Pi: rpicam-vid for video, ALSA -> gain -> Ogg Opus for sound. A network camera
//! (`stream`, `stream_audio`): ffmpeg re-encodes the camera's stream into rpicam-like H.264 segments (`stream`) and
//! decodes its sound to PCM on a pipe (`stream_audio`), which then follows the same gain -> Ogg Opus path.
pub mod audio;
pub mod gain;
pub mod ogg_opus;
pub mod stream;
pub mod stream_audio;
pub mod video;
