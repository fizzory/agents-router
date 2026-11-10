fn main() {
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(
        tauri_build::AppManifest::new().commands(&[
            "get_snapshot",
            "refresh_usage",
            "hide_popover",
            "set_popover_height",
            "open_settings",
            "get_settings_target",
            "show_setting_menu",
            "config_show",
            "config_set",
            "config_unset",
            "config_revert",
            "open_settings_file",
            "choose_workspace",
            "get_open_at_login",
            "set_open_at_login",
            "login_account",
            "check_account",
            "check_for_updates",
            "quit_app",
        ]),
    ))
    .expect("Tauri build");
}
