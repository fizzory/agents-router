pub mod app;
pub mod client;
pub mod glyph;
pub mod models;
mod native;
pub mod placement;
pub mod terminal;
pub mod watcher;

pub fn run() {
    app::run();
}
