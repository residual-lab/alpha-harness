fn main() {
    // The release version is the tag, stamped by CI; a local build reports 0.0.0.
    println!("cargo:rerun-if-env-changed=ALPHA_HARNESS_VERSION");
    // The tag's `v` goes into URLs as `vv2026.…` and leaves nothing to compare versions by.
    if let Ok(version) = std::env::var("ALPHA_HARNESS_VERSION") {
        assert!(
            !version.starts_with('v'),
            "ALPHA_HARNESS_VERSION must be bare, as 2026.10.21, not {version}"
        );
    }
    // The wheel's hash, stamped by CI, which the first install is checked against. Malformed, it
    // would fail every install, so it fails the build instead.
    println!("cargo:rerun-if-env-changed=ALPHA_HARNESS_WHEEL_SHA256");
    if let Ok(hash) = std::env::var("ALPHA_HARNESS_WHEEL_SHA256") {
        assert!(
            hash.len() == 64 && hash.bytes().all(|b| b.is_ascii_hexdigit()),
            "ALPHA_HARNESS_WHEEL_SHA256 must be the wheel's SHA-256 in hex, not {hash:?}"
        );
    }
    tauri_build::build();
}
