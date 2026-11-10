fn main() {
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "get_snapshot",
            "refresh_usage",
            "hide_popover",
            "set_popover_height",
            "open_settings",
            "login_account",
            "check_account",
            "check_for_updates",
            "quit_app",
        ]),
    ))
    .expect("Tauri build");
}
