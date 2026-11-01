//! Root disks that are qcow2 overlays on an earlier run's root disk
//! (`tools/ios_disk_overlay.py`): which file an install's root disk is, what
//! its backing chain reads, and whether the next start may make an overlay.
//! Only headers are read here; nothing is opened for writing.
use super::*;

pub(super) const ROOT_RAW: &str = "system.raw";
pub(super) const ROOT_OVERLAY: &str = "system.qcow2";
/// Overlays on a base before the next start flattens the chain into raw
/// (`MAX_CHAIN` in the script).
pub(super) const MAX_CHAIN: usize = 8;

/// The root disk of an install: its overlay when it has one, else raw.
pub(super) fn root_disk(install: &Path) -> PathBuf {
    let overlay = install.join(ROOT_OVERLAY);
    if fs::symlink_metadata(&overlay).is_ok_and(|meta| meta.is_file()) {
        overlay
    } else {
        install.join(ROOT_RAW)
    }
}

/// A qcow2 header's virtual size and backing file name, or None for a file
/// that is not qcow2 or cannot be read.
pub(super) fn qcow2(path: &Path) -> Option<(u64, Option<PathBuf>)> {
    use std::io::{Read, Seek, SeekFrom};
    let mut file = File::open(path).ok()?;
    let mut head = [0u8; 32];
    file.read_exact(&mut head).ok()?;
    if &head[..4] != b"QFI\xfb" {
        return None;
    }
    let offset = u64::from_be_bytes(head[8..16].try_into().ok()?);
    let length = u32::from_be_bytes(head[16..20].try_into().ok()?) as usize;
    let size = u64::from_be_bytes(head[24..32].try_into().ok()?);
    if offset == 0 {
        return Some((size, None));
    }
    if length == 0 || length > 1023 {
        return None;
    }
    let mut name = vec![0u8; length];
    file.seek(SeekFrom::Start(offset)).ok()?;
    file.read_exact(&mut name).ok()?;
    Some((size, Some(PathBuf::from(String::from_utf8(name).ok()?))))
}

/// The files the root disk of `install` reads, itself first. Stops at a
/// raw disk, a relative or missing backing name, a loop, or `MAX_CHAIN + 2`
/// layers, so a bad chain is short rather than an error here.
pub(super) fn chain(install: &Path) -> Vec<PathBuf> {
    let mut layers = vec![root_disk(install)];
    while layers.len() <= MAX_CHAIN + 1 {
        let Some((_, Some(backing))) = qcow2(layers.last().unwrap()) else {
            break;
        };
        if !backing.is_absolute() || layers.contains(&backing) || !backing.is_file() {
            break;
        }
        layers.push(backing);
    }
    layers
}

/// The install directories an install's root disk is backed by, not itself.
pub(super) fn backing_installs(install: &Path) -> Vec<PathBuf> {
    chain(install)
        .into_iter()
        .skip(1)
        .filter_map(|layer| layer.parent().map(Path::to_path_buf))
        .collect()
}

/// These installs and every install an overlay among them reads.
pub(super) fn with_backing(installs: &[PathBuf]) -> Vec<PathBuf> {
    let mut kept: Vec<PathBuf> = installs.to_vec();
    for install in installs {
        kept.extend(backing_installs(install));
    }
    let resolved: Vec<PathBuf> = kept.iter().filter_map(|p| p.canonicalize().ok()).collect();
    kept.extend(resolved);
    kept.sort();
    kept.dedup();
    kept
}

/// Whether `path` is among `kept`, by name or by its resolved path.
pub(super) fn among(kept: &[PathBuf], path: &Path) -> bool {
    kept.iter().any(|item| item == path)
        || path
            .canonicalize()
            .is_ok_and(|resolved| kept.contains(&resolved))
}

/// qemu-img beside the profile's helper engine, else on PATH.
pub(super) fn qemu_img(profile: &Value) -> Option<PathBuf> {
    let helper = profile["prepare"]["helper_engine"].as_str().unwrap_or("");
    if !helper.is_empty() {
        let beside = Path::new(helper).with_file_name("qemu-img");
        if fs::metadata(&beside)
            .is_ok_and(|meta| meta.is_file() && meta.permissions().mode() & 0o111 != 0)
        {
            return Some(beside);
        }
    }
    toolchain::find("qemu-img").ok().map(PathBuf::from)
}

/// How the next start makes its root disk from the base `install`
/// (`plan` in the script): overlay, copy or flatten, with the reason.
pub(super) fn start_method(profile: &Value, install: &Path) -> (&'static str, String) {
    let layers = chain(install);
    let tool = qemu_img(profile);
    let overlay_base = qcow2(&layers[0]).is_some();
    if profile["start_disks"] == "overlay" {
        if tool.is_none() {
            let method = if overlay_base { "flatten" } else { "copy" };
            return (
                method,
                "no qemu-img beside the helper engine or on PATH".into(),
            );
        }
        if layers.len() <= MAX_CHAIN {
            return ("overlay", "an overlay on the base's root disk".into());
        }
        return (
            "flatten",
            format!(
                "the base's chain has {} layers, the most is {MAX_CHAIN}",
                layers.len()
            ),
        );
    }
    let method = if overlay_base { "flatten" } else { "copy" };
    (method, "the profile asks for copies".into())
}

/// The bytes of an install's root disk as a raw disk: a raw file's length,
/// an overlay's virtual size.
pub(super) fn root_bytes(install: &Path) -> Option<u64> {
    let disk = root_disk(install);
    match qcow2(&disk) {
        Some((size, _)) => Some(size),
        None => fs::metadata(&disk)
            .ok()
            .filter(|meta| meta.is_file())
            .map(|meta| meta.len()),
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    /// A qcow2 v3 header naming `backing`, as qemu-img writes one.
    pub(crate) fn overlay(path: &Path, size: u64, backing: &Path) {
        let name = backing.to_string_lossy().into_owned();
        let mut head = vec![0u8; 512];
        head[..4].copy_from_slice(b"QFI\xfb");
        head[4..8].copy_from_slice(&3u32.to_be_bytes());
        head[8..16].copy_from_slice(&200u64.to_be_bytes());
        head[16..20].copy_from_slice(&(name.len() as u32).to_be_bytes());
        head[20..24].copy_from_slice(&16u32.to_be_bytes());
        head[24..32].copy_from_slice(&size.to_be_bytes());
        head[200..200 + name.len()].copy_from_slice(name.as_bytes());
        fs::write(path, head).unwrap();
    }

    #[test]
    fn a_chain_follows_absolute_backing_names_to_the_raw_disk() {
        let root = tempfile::tempdir().unwrap();
        let (first, second, third) = (
            root.path().join("guest-install1"),
            root.path().join("guest-install2"),
            root.path().join("guest-install3"),
        );
        for install in [&first, &second, &third] {
            fs::create_dir(install).unwrap();
        }
        fs::write(first.join(ROOT_RAW), vec![0u8; 4096]).unwrap();
        overlay(&second.join(ROOT_OVERLAY), 4096, &first.join(ROOT_RAW));
        overlay(&third.join(ROOT_OVERLAY), 4096, &second.join(ROOT_OVERLAY));
        assert_eq!(root_disk(&first), first.join(ROOT_RAW));
        assert_eq!(root_disk(&third), third.join(ROOT_OVERLAY));
        assert_eq!(
            backing_installs(&third),
            vec![second.clone(), first.clone()]
        );
        assert_eq!(root_bytes(&third), Some(4096));
        let kept = with_backing(std::slice::from_ref(&third));
        assert!(among(&kept, &first) && among(&kept, &second) && among(&kept, &third));
        // A relative or missing backing name ends the chain instead of guessing.
        overlay(
            &third.join(ROOT_OVERLAY),
            4096,
            Path::new("../guest-install2/system.qcow2"),
        );
        assert!(backing_installs(&third).is_empty());
        overlay(
            &third.join(ROOT_OVERLAY),
            4096,
            &root.path().join("gone/system.raw"),
        );
        assert!(backing_installs(&third).is_empty());
        // A loop stops at the repeated layer.
        overlay(&second.join(ROOT_OVERLAY), 4096, &third.join(ROOT_OVERLAY));
        overlay(&third.join(ROOT_OVERLAY), 4096, &second.join(ROOT_OVERLAY));
        assert_eq!(chain(&third).len(), 2);
    }

    #[test]
    fn the_start_method_follows_the_profile_the_tool_and_the_chain() {
        let root = tempfile::tempdir().unwrap();
        let tool = root.path().join("helper/qemu-img");
        fs::create_dir(root.path().join("helper")).unwrap();
        fs::write(&tool, "#!/bin/sh\n").unwrap();
        fs::set_permissions(&tool, fs::Permissions::from_mode(0o755)).unwrap();
        let helper = root.path().join("helper/qemu-system-x86_64");
        let mut installs: Vec<PathBuf> = Vec::new();
        for index in 0..=MAX_CHAIN {
            let install = root.path().join(format!("guest-install{index}"));
            fs::create_dir(&install).unwrap();
            if index == 0 {
                fs::write(install.join(ROOT_RAW), vec![0u8; 4096]).unwrap();
            } else {
                overlay(
                    &install.join(ROOT_OVERLAY),
                    4096,
                    &root_disk(&installs[index - 1]),
                );
            }
            installs.push(install);
        }
        let profile = json!({"start_disks":"overlay","prepare":{"helper_engine":helper}});
        assert_eq!(start_method(&profile, &installs[0]).0, "overlay");
        assert_eq!(
            start_method(&profile, &installs[MAX_CHAIN - 1]).0,
            "overlay"
        );
        let (method, reason) = start_method(&profile, &installs[MAX_CHAIN]);
        assert_eq!(method, "flatten");
        assert!(reason.contains("layers"), "{reason}");
        let copies = json!({"prepare":{"helper_engine":helper}});
        assert_eq!(start_method(&copies, &installs[0]).0, "copy");
        assert_eq!(start_method(&copies, &installs[1]).0, "flatten");
    }
}
