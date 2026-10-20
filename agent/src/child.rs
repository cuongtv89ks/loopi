//! Child processes that must not outlive the process that started them (privacy: no recorder without its agent,
//! no agent without loopi-cameras). PR_SET_PDEATHSIG fires when the thread that forked exits: the binaries use a
//! current-thread runtime, so that is the main thread.
#[cfg(target_os = "linux")]
use std::io;

/// SIGTERM to the child when its parent dies (Linux; nothing elsewhere).
pub fn die_with_parent_tokio(command: &mut tokio::process::Command) {
    #[cfg(target_os = "linux")]
    // SAFETY: prctl(2) is async-signal-safe; this runs in the child between fork and exec.
    unsafe {
        command.pre_exec(set_pdeathsig);
    }
    #[cfg(not(target_os = "linux"))]
    let _ = command;
}

/// The same for `std::process::Command` (the sound thread spawns its ffmpeg with it).
pub fn die_with_parent_std(command: &mut std::process::Command) {
    #[cfg(target_os = "linux")]
    {
        use std::os::unix::process::CommandExt;
        // SAFETY: as above.
        unsafe {
            command.pre_exec(set_pdeathsig);
        }
    }
    #[cfg(not(target_os = "linux"))]
    let _ = command;
}

#[cfg(target_os = "linux")]
fn set_pdeathsig() -> io::Result<()> {
    // SAFETY: prctl(2) with PR_SET_PDEATHSIG only reads its integer arguments.
    if unsafe { libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM) } != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

#[cfg(all(test, target_os = "linux"))]
mod tests {
    use super::*;
    use std::os::unix::process::ExitStatusExt;
    use std::process::Stdio;
    use std::time::{Duration, Instant};

    #[test]
    fn a_child_gets_sigterm_when_the_thread_that_started_it_ends() {
        let mut child = std::thread::spawn(|| {
            let mut command = std::process::Command::new("sleep");
            command.arg("30").stdin(Stdio::null());
            die_with_parent_std(&mut command);
            command.spawn().unwrap()
        })
        .join()
        .unwrap();
        let deadline = Instant::now() + Duration::from_secs(5);
        let status = loop {
            if let Some(status) = child.try_wait().unwrap() {
                break Some(status);
            }
            if Instant::now() > deadline {
                break None;
            }
            std::thread::sleep(Duration::from_millis(20));
        };
        if status.is_none() {
            let _ = child.kill();
            let _ = child.wait();
        }
        assert_eq!(status.and_then(|s| s.signal()), Some(libc::SIGTERM), "the sleeping child outlived its parent thread");
    }

    #[tokio::test]
    async fn a_tokio_child_starts_with_the_death_signal_set() {
        let mut command = tokio::process::Command::new("true");
        die_with_parent_tokio(&mut command);
        assert!(command.status().await.unwrap().success());
    }
}
