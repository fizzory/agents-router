pub mod app;
pub mod client;
pub mod glyph;
pub mod install;
mod install_app;
pub mod install_check;
pub mod models;
mod native;
pub mod panes;
pub mod placement;
pub mod roundtrip;
pub mod settings;
pub mod terminal;
pub mod update;
mod update_app;
pub mod watcher;

pub fn run() {
    #[cfg(debug_assertions)]
    update_app::configure_self_test();
    app::run();
}
