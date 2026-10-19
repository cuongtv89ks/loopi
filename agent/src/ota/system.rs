//! The device side of an update: RAUC (CLI; the image builds it without the D-Bus service), the trial reboot, and the
//! bundle download. Traits so the OTA task is tested on the dev host.
use std::path::Path;
use std::process::Command;

use async_trait::async_trait;

use crate::transport::TransportError;

pub trait Rauc: Send + Sync {
    /// `rauc install <bundle>`: verify the signature, write the other slot, mark it primary (the custom bootloader
    /// backend writes tryboot.txt only for a slot that already has boot files) and run the post-install handler, which
    /// copies the boot files and writes tryboot.txt.
    fn install(&self, bundle: &Path) -> Result<(), String>;
    /// `rauc status mark-good`: the booted slot becomes the one config.txt boots.
    fn mark_good(&self) -> Result<(), String>;
    /// `rauc status mark-bad other`.
    fn mark_bad_other(&self) -> Result<(), String>;
}

pub trait Rebooter: Send + Sync {
    /// Reboot so the firmware reads tryboot.txt once (`systemctl reboot --reboot-argument="0 tryboot"`).
    fn reboot_into_trial(&self) -> Result<(), String>;
}

#[async_trait]
pub trait BundleFetcher: Send + Sync {
    /// Download `bundle` into `dest`, resuming a partial file; `Ok` once `dest` holds exactly `size` bytes.
    async fn fetch(&self, bundle: &str, dest: &Path, size: u64) -> Result<(), TransportError>;
}

/// Run a command; on failure the error names it and keeps the last lines it printed.
pub fn run(program: &str, args: &[&str]) -> Result<(), String> {
    let output = Command::new(program).args(args).output().map_err(|e| format!("{program}: {e}"))?;
    if output.status.success() {
        return Ok(());
    }
    let text = String::from_utf8_lossy(if output.stderr.is_empty() { &output.stdout } else { &output.stderr }).into_owned();
    let tail: Vec<&str> = text.lines().filter(|l| !l.trim().is_empty()).collect();
    let tail = tail[tail.len().saturating_sub(3)..].join(" / ");
    Err(format!("{program} {} failed ({}): {tail}", args.join(" "), output.status))
}

pub struct RaucCli;

impl Rauc for RaucCli {
    fn install(&self, bundle: &Path) -> Result<(), String> {
        run("rauc", &["install", &bundle.to_string_lossy()])
    }

    fn mark_good(&self) -> Result<(), String> {
        run("rauc", &["status", "mark-good"])
    }

    fn mark_bad_other(&self) -> Result<(), String> {
        run("rauc", &["status", "mark-bad", "other"])
    }
}

pub struct SystemdReboot;

impl Rebooter for SystemdReboot {
    fn reboot_into_trial(&self) -> Result<(), String> {
        // systemd 258 refuses the old positional argument; the kernel looks for " tryboot" after the partition number.
        run("systemctl", &["reboot", "--reboot-argument=0 tryboot"])
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn run_reports_the_command_and_its_last_words() {
        assert_eq!(run("sh", &["-c", "exit 0"]), Ok(()));
        let err = run("sh", &["-c", "echo one >&2; echo two >&2; echo three >&2; echo four >&2; exit 3"]).unwrap_err();
        assert!(err.starts_with("sh -c "), "{err}");
        // The message repeats the arguments (so "one" is in the script); only the printed tail is checked for it.
        assert_eq!(err.rsplit_once("): ").map(|(_, tail)| tail), Some("two / three / four"), "{err}");
        assert!(run("/nonexistent/program", &[]).unwrap_err().starts_with("/nonexistent/program: "));
    }
}
