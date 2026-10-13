//! Physical button: short press = mark a moment, hold ≥ 2 s = toggle privacy pause.
use std::time::Duration;

pub const SHORT_MAX: Duration = Duration::from_millis(1000);
pub const LONG_MIN: Duration = Duration::from_millis(2000);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Press {
    Short,
    Long,
}

/// Presses between 1 s and 2 s are ignored so an accidental hold does nothing.
pub fn classify(held: Duration) -> Option<Press> {
    if held < SHORT_MAX {
        Some(Press::Short)
    } else if held >= LONG_MIN {
        Some(Press::Long)
    } else {
        None
    }
}

/// Watch an active-low button (GPIO to GND, internal pull-up) on a dedicated thread.
#[cfg(target_os = "linux")]
pub fn spawn_button_thread(chip: String, line: u32, tx: tokio::sync::mpsc::Sender<Press>) -> anyhow::Result<()> {
    use gpiocdev::line::{Bias, EdgeDetection, EdgeKind};

    let request = gpiocdev::Request::builder()
        .on_chip(&chip)
        .with_consumer("loopi-button")
        .with_line(line)
        .as_input()
        .with_bias(Bias::PullUp)
        .with_edge_detection(EdgeDetection::BothEdges)
        .with_debounce_period(Duration::from_millis(20))
        .request()?;
    std::thread::Builder::new().name("button".into()).spawn(move || {
        let mut pressed_at: Option<std::time::Instant> = None;
        loop {
            let event = match request.read_edge_event() {
                Ok(event) => event,
                Err(e) => {
                    tracing::warn!(error = %e, "button read failed");
                    std::thread::sleep(Duration::from_secs(1));
                    continue;
                }
            };
            match event.kind {
                EdgeKind::Falling => pressed_at = Some(std::time::Instant::now()),
                EdgeKind::Rising => {
                    let press = pressed_at.take().and_then(|start| classify(start.elapsed()));
                    if let Some(press) = press {
                        if tx.blocking_send(press).is_err() {
                            return;
                        }
                    }
                }
            }
        }
    })?;
    Ok(())
}

