pub mod client;
pub mod models;
pub mod terminal;
pub mod watcher;

/// The UI, native panel and lifecycle are added in steps 4–6.
/// No frontend execution command is exposed by this shell.
pub fn run() {
    tauri::Builder::default()
        // Updater 2.14.1 requires plugins.updater.pubkey during initialization.
        // Register it in step 10 together with the real public key and endpoint.
        .run(tauri::generate_context!())
        .expect("could not run Agents Router");
}
