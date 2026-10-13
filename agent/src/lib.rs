//! Loopi capture agent. All logic lives in this library so it can be tested on the dev host;
//! `main.rs` only wires the real hardware implementations.
pub mod config;
pub mod fsutil;
pub mod schedule;
