//! The cameras container's entry point; the logic is `loopi_agent::cameras`.
use std::process::ExitCode;

fn main() -> ExitCode {
    tracing_subscriber::fmt()
        .with_env_filter(tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()))
        .init();
    let args: Vec<String> = std::env::args().skip(1).collect();
    let opts = match loopi_agent::cameras::options_from_args(&args) {
        Ok(opts) => opts,
        Err(e) => {
            eprintln!("{e}");
            return ExitCode::from(2);
        }
    };
    let runtime = match tokio::runtime::Builder::new_current_thread().enable_all().build() {
        Ok(rt) => rt,
        Err(e) => {
            eprintln!("runtime: {e}");
            return ExitCode::FAILURE;
        }
    };
    let result = runtime.block_on(async {
        let shutdown = loopi_agent::cameras::shutdown_signal()?;
        loopi_agent::cameras::supervise(opts, shutdown).await
    });
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            tracing::error!(error = %e, "loopi-cameras stopped");
            ExitCode::FAILURE
        }
    }
}
