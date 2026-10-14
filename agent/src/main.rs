//! `loopi-agent run | provision | selftest` — wires real hardware into the library.
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::Arc;

use anyhow::{bail, Context, Result};
use loopi_agent::button::Press;
use loopi_agent::clock::SystemClock;
use loopi_agent::config::{import_boot_config, Config};
use loopi_agent::disk::StatvfsUsage;
use loopi_agent::led::{blink_while, LedDriver};
use loopi_agent::pairing::{ensure_paired, retry_delay, Identity};
use loopi_agent::recorder::audio::AudioSourceFactory;
use loopi_agent::run::{run, Deps, Timings};
use loopi_agent::liveness::Liveness;
use loopi_agent::selftest::{failed_count, format_report, run_selftest};
use loopi_agent::sysinfo::{hw_info, LinuxProbe};
use loopi_agent::transport::{build_client, HttpTransport, Transport};
use loopi_agent::tls;
use tokio::sync::{mpsc, watch};

const PAIR_BEAT: std::time::Duration = std::time::Duration::from_secs(5);
const DATA_CONFIG: &str = "/data/config/loopi.toml";
#[cfg(target_os = "linux")]
const WATCHDOG_PING: std::time::Duration = std::time::Duration::from_secs(10);

fn main() -> ExitCode {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()))
        .without_time() // journald adds timestamps
        .init();
    let args: Vec<String> = std::env::args().skip(1).collect();
    let result = match args.first().map(String::as_str) {
        None | Some("run") => runtime().and_then(|rt| rt.block_on(agent_main())),
        Some("provision") => provision(&args[1..]),
        Some("selftest") => runtime().and_then(|rt| {
            let checks = rt.block_on(run_selftest(Path::new(DATA_CONFIG)));
            print!("{}", format_report(&checks));
            match failed_count(&checks) {
                0 => Ok(()),
                n => Err(anyhow::anyhow!("selftest: {n} check(s) failed")),
            }
        }),
        Some(other) => Err(anyhow::anyhow!("unknown command {other:?}; use run | provision | selftest")),
    };
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            tracing::error!("{e:#}");
            ExitCode::FAILURE
        }
    }
}

fn runtime() -> Result<tokio::runtime::Runtime> {
    Ok(tokio::runtime::Builder::new_current_thread().enable_all().build()?)
}

fn provision(args: &[String]) -> Result<()> {
    let (mut boot, mut data) = (PathBuf::from("/boot/loopi.toml"), PathBuf::from("/data"));
    let mut it = args.iter();
    while let Some(flag) = it.next() {
        let value = it.next().with_context(|| format!("{flag} needs a value"))?;
        match flag.as_str() {
            "--boot" => boot = value.into(),
            "--data" => data = value.into(),
            _ => bail!("unknown flag {flag}"),
        }
    }
    let outcome = import_boot_config(&boot, &data)?;
    println!("{outcome:?}");
    Ok(())
}

/// `None` when the privacy LED cannot be opened: the agent then never records (see `run`).
#[cfg(target_os = "linux")]
fn open_led(config: &Config) -> Option<Box<dyn LedDriver>> {
    match loopi_agent::led::GpioLed::open(&config.gpio.chip, config.gpio.led) {
        Ok(led) => Some(Box::new(led)),
        Err(e) => {
            tracing::error!(error = %e, chip = %config.gpio.chip, line = config.gpio.led, "privacy LED unavailable: recording disabled");
            None
        }
    }
}

/// Dev hosts have no LED.
#[cfg(not(target_os = "linux"))]
fn open_led(_config: &Config) -> Option<Box<dyn LedDriver>> {
    Some(Box::new(loopi_agent::led::NoopLed))
}

#[cfg(target_os = "linux")]
fn open_button(config: &Config) -> mpsc::Receiver<Press> {
    let (tx, rx) = mpsc::channel(8);
    if let Err(e) = loopi_agent::button::spawn_button_thread(config.gpio.chip.clone(), config.gpio.button, tx) {
        tracing::warn!(error = %e, "button unavailable");
    }
    rx
}

#[cfg(not(target_os = "linux"))]
fn open_button(_config: &Config) -> mpsc::Receiver<Press> {
    mpsc::channel(1).1
}

#[cfg(target_os = "linux")]
fn audio_factory(config: &Config) -> AudioSourceFactory {
    loopi_agent::recorder::audio::alsa_source::factory(config.capture.audio_device.clone())
}

#[cfg(not(target_os = "linux"))]
fn audio_factory(_config: &Config) -> AudioSourceFactory {
    Box::new(|| Err(std::io::Error::new(std::io::ErrorKind::Unsupported, "audio unsupported on this OS")))
}

/// Tell systemd what the agent is doing (`systemctl status`); Linux only.
#[cfg(target_os = "linux")]
fn set_status(text: &str) {
    let _ = sd_notify::notify(false, &[sd_notify::NotifyState::Status(text)]);
}

#[cfg(not(target_os = "linux"))]
fn set_status(_text: &str) {}

/// READY=1 is sent right away, before pairing, on purpose: pairing can wait indefinitely for a
/// token and a late READY would hit TimeoutStartSec under Type=notify and loop the unit through
/// restarts. Nothing depends on loopi-agent. WATCHDOG=1 is sent only while `liveness` is fresh,
/// so a wedged main loop (which would also stop applying pause/gates) gets the unit restarted.
#[cfg(target_os = "linux")]
fn spawn_watchdog(liveness: Liveness) {
    use loopi_agent::liveness::{is_fresh, MAX_STALE};
    use sd_notify::NotifyState;

    let _ = sd_notify::notify(false, &[NotifyState::Ready]);
    tokio::spawn(async move {
        let mut ticker = tokio::time::interval(WATCHDOG_PING);
        let mut stale_logged = false;
        loop {
            ticker.tick().await;
            if is_fresh(liveness.age(), MAX_STALE) {
                stale_logged = false;
                let _ = sd_notify::notify(false, &[NotifyState::Watchdog]);
            } else if !stale_logged {
                stale_logged = true;
                tracing::error!(age_s = liveness.age().as_secs(), "main loop made no progress; withholding watchdog ping");
            }
        }
    });
}

#[cfg(not(target_os = "linux"))]
fn spawn_watchdog(_liveness: Liveness) {}

/// Retry pairing until it succeeds. The LED blinks meanwhile (caller) and no recorder runs.
async fn pair_until_done(
    config: &Config,
    keys: &Path,
    mut shutdown: watch::Receiver<bool>,
    liveness: &Liveness,
) -> Result<Identity> {
    let client = build_client(tls::pairing_config(config.brain.ca_fingerprint.as_deref())?)?;
    let mut beat = tokio::time::interval(PAIR_BEAT);
    loop {
        // Beat while the request is in flight too: it can take longer than the watchdog window.
        let attempt = ensure_paired(config, keys, hw_info(), &client);
        tokio::pin!(attempt);
        let result = loop {
            tokio::select! {
                result = &mut attempt => break result,
                _ = beat.tick() => liveness.beat(),
            }
        };
        let delay = match result {
            Ok(identity) => return Ok(identity),
            Err(e) => {
                let delay = retry_delay(&e);
                tracing::warn!(error = %e, retry_in_s = delay.as_secs(), "pairing failed");
                delay
            }
        };
        let sleep = tokio::time::sleep(delay);
        tokio::pin!(sleep);
        loop {
            tokio::select! {
                _ = &mut sleep => break,
                _ = beat.tick() => liveness.beat(),
                _ = shutdown.changed() => bail!("shutdown before pairing completed"),
            }
        }
    }
}

fn shutdown_signal() -> Result<watch::Receiver<bool>> {
    use tokio::signal::unix::{signal, SignalKind};

    let mut term = signal(SignalKind::terminate()).context("installing SIGTERM handler")?;
    let (tx, rx) = watch::channel(false);
    tokio::spawn(async move {
        tokio::select! {
            _ = term.recv() => {}
            _ = tokio::signal::ctrl_c() => {}
        }
        let _ = tx.send(true);
    });
    Ok(rx)
}

async fn agent_main() -> Result<()> {
    let config_path = Path::new(DATA_CONFIG);
    if !config_path.exists() {
        bail!("not provisioned: run `loopi-agent provision` first");
    }
    let config = Config::load(config_path).with_context(|| format!("loading {DATA_CONFIG}"))?;
    let shutdown = shutdown_signal()?;
    let liveness = Liveness::new();
    liveness.beat();
    spawn_watchdog(liveness.clone());
    set_status("pairing with brain...");

    let mut led = open_led(&config);
    let keys = config.paths.data_dir.join("keys");
    let pairing = pair_until_done(&config, &keys, shutdown.clone(), &liveness);
    let identity = match led.as_deref_mut() {
        Some(led) => blink_while(led, pairing).await?,
        None => pairing.await?,
    };
    tracing::info!(device_id = %identity.device_id, "paired");
    set_status(if led.is_some() { "running" } else { "privacy LED unavailable: recording disabled" });

    let tls = tls::mtls_config(&identity.ca_cert_pem, &identity.cert_pem, &identity.key_pem)?;
    let transport: Arc<dyn Transport> = Arc::new(HttpTransport::new(build_client(tls)?, &config.brain.url));
    let deps = Deps {
        clock: Arc::new(SystemClock::new(config.paths.sync_flag.clone())),
        disk: Arc::new(StatvfsUsage::new(&config.paths.data_dir)?),
        transport,
        audio_factory: audio_factory(&config),
        led,
        presses: open_button(&config),
        probe: Arc::new(LinuxProbe),
        timings: Timings::default(),
        liveness,
        config,
    };
    run(deps, shutdown).await
}
