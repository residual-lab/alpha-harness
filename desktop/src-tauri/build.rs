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
    tauri_build::build();
}
