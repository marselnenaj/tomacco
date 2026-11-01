//! The product's name, its repository and where it installs, as the updater
//! knows them. A rename changes these constants and nothing in `update.rs`.

pub const PRODUCT_NAME: &str = "Tomacco";
pub const REPO_OWNER: &str = "marselnenaj";
pub const REPO_NAME: &str = "tomacco";
/// `<PACKAGE_PREFIX>-<version>-Linux-<arch>.tar.gz`, as CMake's package names it;
/// `<arch>` is the host's, so an update only offers the package of this computer.
pub const PACKAGE_PREFIX: &str = "tomacco";
pub const CLI_NAME: &str = "tomaccoctl";
pub const DESKTOP_NAME: &str = "tomacco";
/// Relative to the home an installation was made for; the same paths as install.py.
pub const INSTALLED_LIBRARY: &str = ".local/lib/tomacco";
pub const INSTALLED_LAUNCHERS: &str = ".local/bin";
pub const INSTALL_BACKUPS: &str = ".local/share/tomacco/install-backups";
/// The line install.py writes into every launcher it owns.
pub const INSTALLER_MARKER: &str = "# Managed by tomacco/install.py";
/// Set to `off`, this environment variable turns every update request off.
pub const UPDATES_ENVIRONMENT: &str = "TOMACCO_UPDATES";

/// The public half of the release signing key: base64 of its 32 raw bytes.
/// While this is empty no update can be verified, so none is installed.
pub const RELEASE_SIGNING_PUBLIC_KEY_BASE64: &str = "eUZ5PV5fqziL3khcVKTg8wiIbOYeIggiMD6mWLkEYwM=";
/// During a key rotation, the public half of the key that is to sign later
/// releases, in the same form; empty otherwise. A package signed by either key
/// is accepted. docs/building.md#rotating-the-release-key has the steps.
pub const NEXT_RELEASE_SIGNING_PUBLIC_KEY_BASE64: &str = "";
