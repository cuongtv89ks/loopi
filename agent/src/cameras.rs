//! `loopi-cameras` (P3c design §2, D30): one `loopi-agent run --config <dir>/loopi.toml` per network camera directory
//! under the root (`/srv/loopi/cameras` in the container). The brain writes the directories; this supervisor starts an
//! agent for every `<ULID>/loopi.toml` without a `disabled` file, restarts an agent that exits (with backoff) or whose
//! config changed (at once), stops the agent of a disabled or removed camera, and on SIGTERM stops them all. Agents die
//! with it (PR_SET_PDEATHSIG). Their output lines are passed on prefixed with the camera id.
use std::collections::HashMap;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::time::{Duration, SystemTime};

use tokio::io::{AsyncBufReadExt, AsyncRead, BufReader};
use tokio::process::{Child, Command};
use tokio::sync::watch;
use tokio::time::Instant;

use crate::child::die_with_parent_tokio;

pub const CONFIG_FILE: &str = "loopi.toml";
pub const DISABLED_FILE: &str = "disabled";
pub const DEFAULT_ROOT: &str = "/srv/loopi/cameras";

#[derive(Debug, Clone)]
pub struct SupervisorOptions {
    pub root: PathBuf,
    pub agent_cmd: PathBuf,
    pub scan: Duration,
    pub stop_grace: Duration,
    pub min_backoff: Duration,
    pub max_backoff: Duration,
    pub healthy_run: Duration,
}

/// `loopi-cameras [--root DIR] [--agent PATH]`; the agent defaults to `loopi-agent` next to this binary.
pub fn options_from_args(args: &[String]) -> Result<SupervisorOptions, String> {
    let next_to_us = std::env::current_exe().ok().and_then(|p| p.parent().map(|d| d.join("loopi-agent")));
    let mut opts = SupervisorOptions {
        root: DEFAULT_ROOT.into(),
        agent_cmd: next_to_us.filter(|p| p.exists()).unwrap_or_else(|| "loopi-agent".into()),
        scan: Duration::from_secs(5),
        stop_grace: Duration::from_secs(15),
        min_backoff: Duration::from_secs(2),
        max_backoff: Duration::from_secs(60),
        healthy_run: Duration::from_secs(60),
    };
    let mut it = args.iter();
    while let Some(flag) = it.next() {
        let value = it.next().ok_or_else(|| format!("{flag} needs a value"))?;
        match flag.as_str() {
            "--root" => opts.root = value.into(),
            "--agent" => opts.agent_cmd = value.into(),
            other => return Err(format!("unknown flag {other:?}; use loopi-cameras [--root DIR] [--agent PATH]")),
        }
    }
    Ok(opts)
}

/// A camera id is the brain's ULID: 26 characters of 0-9 and A-Z.
pub fn is_camera_id(name: &str) -> bool {
    name.len() == 26 && name.bytes().all(|b| b.is_ascii_digit() || b.is_ascii_uppercase())
}

/// What tells a changed config apart: its modification time and length.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Stamp {
    modified: Option<SystemTime>,
    len: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Wanted {
    pub id: String,
    pub config: PathBuf,
    pub stamp: Stamp,
}

/// The cameras to run now, in id order.
pub fn scan(root: &Path) -> io::Result<Vec<Wanted>> {
    let mut wanted = Vec::new();
    for entry in fs::read_dir(root)? {
        let entry = entry?;
        let Some(id) = entry.file_name().to_str().map(str::to_string) else { continue };
        let dir = entry.path();
        if !is_camera_id(&id) || !dir.is_dir() || dir.join(DISABLED_FILE).exists() {
            continue;
        }
        let config = dir.join(CONFIG_FILE);
        let Ok(meta) = fs::metadata(&config) else { continue };
        if meta.is_file() {
            wanted.push(Wanted { id, config, stamp: Stamp { modified: meta.modified().ok(), len: meta.len() } });
        }
    }
    wanted.sort_by(|a, b| a.id.cmp(&b.id));
    Ok(wanted)
}

pub fn prefixed(id: &str, line: &str) -> String {
    format!("[{id}] {line}")
}

struct Running {
    child: Child,
    stamp: Stamp,
    started: Instant,
}

fn backoff(failures: u32, min: Duration, max: Duration) -> Duration {
    min.saturating_mul(1u32 << failures.saturating_sub(1).min(6)).min(max)
}

fn forward_lines<R: AsyncRead + Unpin + Send + 'static>(id: String, stream: R) {
    tokio::spawn(async move {
        let mut lines = BufReader::new(stream).lines();
        while let Ok(Some(line)) = lines.next_line().await {
            eprintln!("{}", prefixed(&id, &line));
        }
    });
}

fn spawn_agent(agent: &Path, wanted: &Wanted) -> io::Result<Child> {
    let mut command = Command::new(agent);
    command
        .arg("run")
        .arg("--config")
        .arg(&wanted.config)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true);
    die_with_parent_tokio(&mut command);
    let mut child = command.spawn()?;
    if let Some(out) = child.stdout.take() {
        forward_lines(wanted.id.clone(), out);
    }
    if let Some(err) = child.stderr.take() {
        forward_lines(wanted.id.clone(), err);
    }
    Ok(child)
}

fn terminate(child: &Child) {
    if let Some(pid) = child.id() {
        // SAFETY: kill(2) on our own child.
        unsafe {
            libc::kill(pid as libc::pid_t, libc::SIGTERM);
        }
    }
}

/// SIGTERM, then SIGKILL after `grace` (the agent drains its recorders within 15 s).
async fn stop(child: &mut Child, grace: Duration) {
    terminate(child);
    if tokio::time::timeout(grace, child.wait()).await.is_err() {
        let _ = child.kill().await;
    }
}

pub async fn supervise(opts: SupervisorOptions, mut shutdown: watch::Receiver<bool>) -> io::Result<()> {
    let mut running: HashMap<String, Running> = HashMap::new();
    let mut retry: HashMap<String, (u32, Instant)> = HashMap::new(); // failures, not before
    let mut ticker = tokio::time::interval(opts.scan);
    ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    loop {
        tokio::select! {
            _ = ticker.tick() => {}
            changed = shutdown.changed() => if changed.is_err() || *shutdown.borrow() { break; },
        }
        let exited: Vec<(String, Duration)> = running
            .iter_mut()
            .filter_map(|(id, r)| match r.child.try_wait() {
                Ok(Some(_)) | Err(_) => Some((id.clone(), r.started.elapsed())),
                Ok(None) => None,
            })
            .collect();
        for (id, ran_for) in exited {
            running.remove(&id);
            let failures = if ran_for >= opts.healthy_run { 1 } else { retry.get(&id).map_or(1, |r| r.0 + 1) };
            let delay = backoff(failures, opts.min_backoff, opts.max_backoff);
            tracing::warn!(camera = %id, ran_s = ran_for.as_secs(), retry_s = delay.as_secs_f32(), "agent exited");
            retry.insert(id, (failures, Instant::now() + delay));
        }
        let wanted = match scan(&opts.root) {
            Ok(w) => w,
            Err(e) => {
                tracing::error!(error = %e, root = %opts.root.display(), "cannot read the cameras directory");
                continue;
            }
        };
        let by_id: HashMap<&str, &Wanted> = wanted.iter().map(|w| (w.id.as_str(), w)).collect();
        let to_stop: Vec<String> = running
            .iter()
            .filter(|(id, r)| by_id.get(id.as_str()).map_or(true, |w| w.stamp != r.stamp))
            .map(|(id, _)| id.clone())
            .collect();
        for id in to_stop {
            if let Some(mut r) = running.remove(&id) {
                tracing::info!(camera = %id, "stopping the agent (camera disabled, removed or reconfigured)");
                stop(&mut r.child, opts.stop_grace).await;
                retry.remove(&id);
            }
        }
        retry.retain(|id, _| by_id.contains_key(id.as_str()));
        for w in &wanted {
            if running.contains_key(&w.id) || retry.get(&w.id).is_some_and(|r| Instant::now() < r.1) {
                continue;
            }
            match spawn_agent(&opts.agent_cmd, w) {
                Ok(child) => {
                    tracing::info!(camera = %w.id, "agent started");
                    running.insert(w.id.clone(), Running { child, stamp: w.stamp, started: Instant::now() });
                }
                Err(e) => {
                    let failures = retry.get(&w.id).map_or(1, |r| r.0 + 1);
                    tracing::error!(camera = %w.id, error = %e, "cannot start the agent");
                    retry.insert(w.id.clone(), (failures, Instant::now() + backoff(failures, opts.min_backoff, opts.max_backoff)));
                }
            }
        }
    }
    for r in running.values() {
        terminate(&r.child);
    }
    for (_, mut r) in running.drain() {
        stop(&mut r.child, opts.stop_grace).await;
    }
    Ok(())
}

/// SIGTERM or SIGINT ends `supervise`.
pub fn shutdown_signal() -> io::Result<watch::Receiver<bool>> {
    use tokio::signal::unix::{signal, SignalKind};
    let (tx, rx) = watch::channel(false);
    let mut term = signal(SignalKind::terminate())?;
    let mut int = signal(SignalKind::interrupt())?;
    tokio::spawn(async move {
        tokio::select! { _ = term.recv() => {}, _ = int.recv() => {} }
        let _ = tx.send(true);
    });
    Ok(rx)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    const A: &str = "01J00000000000000000000AAA";
    const B: &str = "01J00000000000000000000BBB";

    fn camera(root: &Path, id: &str) -> PathBuf {
        let dir = root.join(id);
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join(CONFIG_FILE), "[brain]\nurl = \"https://proxy:8443\"\n").unwrap();
        dir
    }

    fn options(root: &Path, agent: PathBuf) -> SupervisorOptions {
        SupervisorOptions {
            root: root.to_path_buf(),
            agent_cmd: agent,
            scan: Duration::from_millis(100),
            stop_grace: Duration::from_secs(2),
            min_backoff: Duration::from_millis(100),
            max_backoff: Duration::from_millis(400),
            healthy_run: Duration::from_secs(60),
        }
    }

    fn fake_agent(dir: &Path) -> PathBuf {
        let path = dir.join("fake-agent.sh");
        fs::copy(Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/fake-agent.sh"), &path).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
        path
    }

    fn pids(dir: &Path) -> Vec<i32> {
        fs::read_to_string(dir.join("runs.log"))
            .unwrap_or_default()
            .lines()
            .filter_map(|l| l.split_whitespace().next()?.parse().ok())
            .collect()
    }

    fn alive(pid: i32) -> bool {
        // SAFETY: signal 0 only checks that the process exists.
        unsafe { libc::kill(pid, 0) == 0 }
    }

    async fn eventually(check: impl Fn() -> bool) -> bool {
        for _ in 0..100 {
            if check() {
                return true;
            }
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
        false
    }

    #[test]
    fn only_camera_directories_with_a_config_run() {
        let tmp = tempfile::tempdir().unwrap();
        camera(tmp.path(), A);
        fs::create_dir_all(tmp.path().join(B)).unwrap(); // no config yet
        camera(tmp.path(), "not-a-camera");
        let disabled = camera(tmp.path(), "01J00000000000000000000CCC");
        fs::write(disabled.join(DISABLED_FILE), "").unwrap();
        let wanted: Vec<String> = scan(tmp.path()).unwrap().into_iter().map(|w| w.id).collect();
        assert_eq!(wanted, vec![A.to_string()]);
        assert!(is_camera_id(A) && !is_camera_id("not-a-camera") && !is_camera_id("01j00000000000000000000aaa"));
    }

    #[tokio::test]
    async fn every_camera_gets_its_own_agent_and_all_stop_with_the_supervisor() {
        let tmp = tempfile::tempdir().unwrap();
        let (a, b) = (camera(tmp.path(), A), camera(tmp.path(), B));
        let (stop_tx, stop_rx) = watch::channel(false);
        let task = tokio::spawn(supervise(options(tmp.path(), fake_agent(tmp.path())), stop_rx));
        assert!(eventually(|| pids(&a).len() == 1 && pids(&b).len() == 1).await);
        let started = [pids(&a)[0], pids(&b)[0]];
        stop_tx.send(true).unwrap();
        task.await.unwrap().unwrap();
        assert!(started.iter().all(|pid| !alive(*pid)), "no agent outlives the supervisor");
    }

    #[tokio::test]
    async fn a_disabled_camera_is_stopped() {
        let tmp = tempfile::tempdir().unwrap();
        let a = camera(tmp.path(), A);
        let (stop_tx, stop_rx) = watch::channel(false);
        let task = tokio::spawn(supervise(options(tmp.path(), fake_agent(tmp.path())), stop_rx));
        assert!(eventually(|| pids(&a).len() == 1).await);
        let pid = pids(&a)[0];
        fs::write(a.join(DISABLED_FILE), "").unwrap();
        assert!(eventually(|| !alive(pid)).await, "the agent stops within a scan");
        tokio::time::sleep(Duration::from_millis(400)).await;
        assert_eq!(pids(&a).len(), 1, "and is not started again");
        stop_tx.send(true).unwrap();
        task.await.unwrap().unwrap();
    }

    #[tokio::test]
    async fn a_changed_config_restarts_its_agent() {
        let tmp = tempfile::tempdir().unwrap();
        let a = camera(tmp.path(), A);
        let (stop_tx, stop_rx) = watch::channel(false);
        let task = tokio::spawn(supervise(options(tmp.path(), fake_agent(tmp.path())), stop_rx));
        assert!(eventually(|| pids(&a).len() == 1).await);
        fs::write(a.join(CONFIG_FILE), "[brain]\nurl = \"https://proxy:8443\"\npair_token = \"ABCD-EFGH\"\n").unwrap();
        assert!(eventually(|| pids(&a).len() == 2).await, "a new agent runs with the new config");
        assert!(!alive(pids(&a)[0]) && alive(pids(&a)[1]));
        stop_tx.send(true).unwrap();
        task.await.unwrap().unwrap();
    }

    #[tokio::test]
    async fn an_agent_that_exits_is_started_again_with_backoff() {
        let tmp = tempfile::tempdir().unwrap();
        let a = camera(tmp.path(), A);
        let exiting = tmp.path().join("exiting-agent.sh");
        fs::write(&exiting, format!("#!/bin/sh\nFAKE_AGENT_EXIT=1 exec {} \"$@\"\n", fake_agent(tmp.path()).display())).unwrap();
        fs::set_permissions(&exiting, fs::Permissions::from_mode(0o755)).unwrap();
        let (stop_tx, stop_rx) = watch::channel(false);
        let task = tokio::spawn(supervise(options(tmp.path(), exiting), stop_rx));
        assert!(eventually(|| pids(&a).len() >= 3).await, "restarted after each exit");
        stop_tx.send(true).unwrap();
        task.await.unwrap().unwrap();
    }

    #[test]
    fn options_have_the_container_defaults() {
        let o = options_from_args(&[]).unwrap();
        assert_eq!(o.root, PathBuf::from("/srv/loopi/cameras"));
        assert_eq!((o.scan, o.stop_grace), (Duration::from_secs(5), Duration::from_secs(15)));
        let o = options_from_args(&["--root".into(), "/x".into(), "--agent".into(), "/bin/agent".into()]).unwrap();
        assert_eq!((o.root, o.agent_cmd), (PathBuf::from("/x"), PathBuf::from("/bin/agent")));
        assert!(options_from_args(&["--bogus".into()]).is_err());
    }

    #[test]
    fn agent_lines_carry_the_camera_id() {
        assert_eq!(prefixed(A, "paired"), format!("[{A}] paired"));
    }
}
