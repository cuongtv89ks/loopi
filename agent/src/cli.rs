//! Command-line arguments of the binaries, kept out of `main.rs` so they are tested.
use std::path::PathBuf;

/// The Pi's config, imported from the boot partition by `loopi-agent provision`.
pub const DEFAULT_CONFIG: &str = "/data/config/loopi.toml";

/// `loopi-agent run [--config PATH]`: the config the agent runs with (a network camera's lives in its own directory).
pub fn run_config_path(args: &[String]) -> Result<PathBuf, String> {
    let mut path = PathBuf::from(DEFAULT_CONFIG);
    let mut it = args.iter();
    while let Some(flag) = it.next() {
        match flag.as_str() {
            "--config" => path = it.next().ok_or_else(|| "--config needs a path".to_string())?.into(),
            other => return Err(format!("unknown flag {other:?} for run; use run [--config PATH]")),
        }
    }
    Ok(path)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(list: &[&str]) -> Vec<String> {
        list.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn run_uses_the_pi_config_unless_told_otherwise() {
        assert_eq!(run_config_path(&args(&[])).unwrap(), PathBuf::from(DEFAULT_CONFIG));
        assert_eq!(run_config_path(&args(&["--config", "/srv/c/loopi.toml"])).unwrap(), PathBuf::from("/srv/c/loopi.toml"));
        assert!(run_config_path(&args(&["--config"])).unwrap_err().contains("needs a path"));
        assert!(run_config_path(&args(&["--data", "/x"])).unwrap_err().contains("unknown flag"));
    }
}
