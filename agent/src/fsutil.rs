//! Small filesystem helpers shared by config, queue and pairing.
use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};

/// Hidden temp path next to `path`: `dir/.name.tmp`. Anything ending in `.tmp` is garbage after a crash.
pub fn tmp_path(path: &Path) -> PathBuf {
    let name = path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
    path.with_file_name(format!(".{name}.tmp"))
}

/// Write `bytes` to `path` atomically (temp file, fsync, rename, fsync dir) with permissions `mode`.
pub fn write_atomic(path: &Path, bytes: &[u8], mode: u32) -> io::Result<()> {
    let tmp = tmp_path(path);
    {
        let mut file = OpenOptions::new().write(true).create(true).truncate(true).mode(mode).open(&tmp)?;
        file.write_all(bytes)?;
        file.sync_all()?;
    }
    fs::rename(&tmp, path)?;
    if let Some(dir) = path.parent() {
        // Best effort: directory fsync is not supported on every filesystem (e.g. FAT).
        if let Ok(handle) = File::open(dir) {
            let _ = handle.sync_all();
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    #[test]
    fn tmp_path_is_hidden_sibling() {
        assert_eq!(tmp_path(Path::new("/a/b/state.json")), PathBuf::from("/a/b/.state.json.tmp"));
    }

    #[test]
    fn write_atomic_sets_mode_and_leaves_no_temp() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("f.txt");
        write_atomic(&path, b"one", 0o600).unwrap();
        write_atomic(&path, b"two", 0o600).unwrap();
        assert_eq!(fs::read(&path).unwrap(), b"two");
        assert_eq!(fs::metadata(&path).unwrap().permissions().mode() & 0o777, 0o600);
        assert!(!tmp_path(&path).exists());
    }
}
