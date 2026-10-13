//! An uploadable unit: one video/audio segment or a marker.
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ItemKind {
    Video,
    Audio,
    Marker,
}

impl ItemKind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Video => "video",
            Self::Audio => "audio",
            Self::Marker => "marker",
        }
    }

    pub fn content_type(self) -> &'static str {
        match self {
            Self::Video => "video/h264",
            Self::Audio => "audio/ogg",
            Self::Marker => "application/json",
        }
    }

    pub fn extension(self) -> &'static str {
        match self {
            Self::Video => "h264",
            Self::Audio => "opus",
            Self::Marker => "json",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ItemMeta {
    pub id: String,
    pub kind: ItemKind,
    pub started_at: DateTime<Utc>,
    pub ended_at: DateTime<Utc>,
    pub sha256: String,
    pub size: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub video_fps: Option<u32>,
}
