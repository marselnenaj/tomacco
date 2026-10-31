//! Bounded project file access with root confinement and optimistic write checks.
use crate::core::*;
use serde_json::{Value, json};
use std::{
    collections::VecDeque,
    fs,
    io::Read,
    os::unix::fs::PermissionsExt,
    path::{Component, Path, PathBuf},
};
const LIMIT: u64 = 1024 * 1024;
const GENERATED: &[&str] = &[
    ".git",
    ".tooling",
    "node_modules",
    "build",
    "target",
    ".gradle",
    ".dart_tool",
    ".build",
    ".idea",
];
fn target(root: &Path, relative: &str, exists: bool) -> Result<PathBuf> {
    let p = Path::new(relative);
    if p.is_absolute()
        || p.components().any(|c| {
            matches!(
                c,
                Component::ParentDir | Component::RootDir | Component::Prefix(_)
            )
        })
    {
        return Err(Error::new(
            "outside_project",
            "Use a path relative to the project root",
        ));
    }
    let joined = root.join(p);
    let resolved = path(&joined.to_string_lossy(), false, exists)?;
    if !resolved.starts_with(root) {
        return Err(Error::new(
            "outside_project",
            "Symlink points outside this project",
        ));
    }
    Ok(resolved)
}
fn bytes(file: &Path) -> Result<Vec<u8>> {
    if !file.is_file() {
        return Err(Error::new("not_file", "Select a regular file"));
    }
    let mut data = Vec::new();
    fs::File::open(file)?
        .take(LIMIT + 1)
        .read_to_end(&mut data)?;
    if data.len() as u64 > LIMIT {
        return Err(Error::new(
            "file_too_large",
            "Text editing is limited to files up to 1 MiB",
        ));
    }
    Ok(data)
}
fn read(root: &Path, relative: &str) -> Result {
    let file = target(root, relative, true)?;
    let data = bytes(&file)?;
    let hash = digest(&data);
    let text = String::from_utf8(data)
        .map_err(|_| Error::new("binary_file", "This file is not UTF-8 text"))?;
    if text.contains('\0') {
        return Err(Error::new("binary_file", "This file contains binary data"));
    }
    Ok(
        json!({"path":root,"file":relative,"text":text,"sha256":hash,"bytes":text.len(),"writable":!fs::metadata(file)?.permissions().readonly()}),
    )
}
fn list(root: &Path, a: &Value) -> Result {
    let folder = target(root, s(a, "directory"), true)?;
    if !folder.is_dir() {
        return Err(Error::new("not_directory", "Choose a folder"));
    }
    let mut entries = Vec::new();
    let mut truncated = false;
    for entry in fs::read_dir(folder)? {
        let entry = entry?;
        let name = entry.file_name().to_string_lossy().into_owned();
        if !b(a, "hidden") && name.starts_with('.') {
            continue;
        }
        if !b(a, "generated") && GENERATED.contains(&name.as_str()) {
            continue;
        }
        let file = entry.path();
        let relative = file
            .strip_prefix(root)
            .unwrap()
            .to_string_lossy()
            .into_owned();
        let symlink = entry.file_type()?.is_symlink();
        let within = file.canonicalize().is_ok_and(|p| p.starts_with(root));
        let meta = entry.metadata()?;
        entries.push(json!({"name":name,"file":relative,"directory":within&&file.is_dir(),"symlink":symlink,"accessible":within,"bytes":meta.len()}));
        if entries.len() >= 2000 {
            truncated = true;
            break;
        }
    }
    entries.sort_by(|left, right| {
        b(right, "directory")
            .cmp(&b(left, "directory"))
            .then_with(|| {
                s(left, "name")
                    .to_lowercase()
                    .cmp(&s(right, "name").to_lowercase())
            })
    });
    Ok(json!({"path":root,"directory":s(a,"directory"),"entries":entries,"truncated":truncated}))
}
pub fn handle(op: &str, a: &Value) -> Result {
    let root = path(s(a, "path"), true, true)?;
    match op {
        "files.list" => list(&root, a),
        "files.read" => read(&root, s(a, "file")),
        "files.search" => search(&root, a),
        "files.write" => {
            let file = target(&root, s(a, "file"), false)?;
            if file == root {
                return Err(Error::new(
                    "invalid_path",
                    "Choose a file inside the project",
                ));
            }
            let text = s(a, "text");
            if text.len() as u64 > LIMIT {
                return Err(Error::new(
                    "file_too_large",
                    "Text editing is limited to 1 MiB",
                ));
            }
            let lock = Lock::open(&home()?.join("locks").join(format!(
                "files-{}.lock",
                digest(root.to_string_lossy().as_bytes())
            )))?;
            lock.acquire(false)?;
            let expected = s(a, "if_match");
            let mode = if file.exists() {
                let current = bytes(&file)?;
                let actual = digest(&current);
                if actual != expected {
                    return Err(Error::new(
                        "file_conflict",
                        "The file changed after it was opened. Reload or merge before saving.",
                    )
                    .details(json!({"expected":expected,"actual":actual,"file":s(a,"file")})));
                }
                fs::metadata(&file)?.permissions().mode() & 0o777
            } else {
                if !expected.is_empty() {
                    return Err(Error::new(
                        "file_conflict",
                        "The original file no longer exists",
                    ));
                }
                0o644
            };
            if !file.parent().unwrap().is_dir() {
                return Err(Error::new(
                    "parent_missing",
                    "Create the parent folder first",
                ));
            }
            atomic_write(&file, text.as_bytes())?;
            fs::set_permissions(&file, fs::Permissions::from_mode(mode))?;
            read(&root, s(a, "file"))
        }
        "files.mkdir" => {
            let folder = target(&root, s(a, "directory"), false)?;
            if folder == root || folder.exists() {
                return Err(Error::new("path_exists", "The folder already exists"));
            }
            fs::create_dir(&folder)?;
            Ok(json!({"created":s(a,"directory")}))
        }
        _ => Err(Error::new("operation_not_found", op)),
    }
}
fn search(root: &Path, a: &Value) -> Result {
    let query = s(a, "query");
    if query.is_empty() {
        return Err(Error::new("invalid_argument", "Enter a search query"));
    }
    let pattern = if b(a, "regex") {
        query.to_string()
    } else {
        regex::escape(query)
    };
    let re = regex::RegexBuilder::new(&pattern)
        .case_insensitive(!b(a, "case_sensitive"))
        .size_limit(2 * 1024 * 1024)
        .build()
        .map_err(|e| Error::new("invalid_pattern", e.to_string()))?;
    let mut dirs = VecDeque::from([root.to_path_buf()]);
    let mut matches = Vec::new();
    let mut scanned = 0;
    let mut total = 0u64;
    let mut truncated = false;
    'scan: while let Some(folder) = dirs.pop_front() {
        for entry in fs::read_dir(folder)? {
            let entry = entry?;
            let name = entry.file_name().to_string_lossy().into_owned();
            if GENERATED.contains(&name.as_str())
                || (!b(a, "hidden") && name.starts_with('.'))
                || entry.file_type()?.is_symlink()
            {
                continue;
            }
            if entry.file_type()?.is_dir() {
                if dirs.len() < 10000 {
                    dirs.push_back(entry.path());
                } else {
                    truncated = true;
                }
                continue;
            }
            if !entry.file_type()?.is_file() {
                continue;
            }
            scanned += 1;
            if scanned > 10000 || total > 64 * 1024 * 1024 {
                truncated = true;
                break 'scan;
            }
            let data = match bytes(&entry.path()) {
                Ok(v) => v,
                Err(e) if ["file_too_large", "io_error"].contains(&e.code.as_str()) => continue,
                Err(e) => return Err(e),
            };
            total += data.len() as u64;
            let Ok(text) = String::from_utf8(data) else {
                continue;
            };
            if text.contains('\0') {
                continue;
            }
            let file = entry
                .path()
                .strip_prefix(root)
                .unwrap()
                .to_string_lossy()
                .into_owned();
            for (index, line) in text.lines().enumerate() {
                if let Some(found) = re.find(line) {
                    matches.push(json!({"file":file,"line":index+1,"column":line[..found.start()].chars().count()+1,"text":line.chars().take(400).collect::<String>()}));
                    if matches.len() >= n(a, "limit") as usize {
                        truncated = true;
                        break 'scan;
                    }
                }
            }
        }
    }
    Ok(
        json!({"matches":matches,"files_scanned":scanned,"bytes_scanned":total,"truncated":truncated}),
    )
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn paths_cannot_escape_through_parent_or_symlink() {
        let dir = tempfile::tempdir().unwrap();
        let outside = tempfile::tempdir().unwrap();
        std::os::unix::fs::symlink(outside.path(), dir.path().join("escape")).unwrap();
        assert!(target(dir.path(), "../no", false).is_err());
        assert!(target(dir.path(), "escape/new", false).is_err());
        assert!(target(dir.path(), "src", false).is_ok());
    }
}
