//! Pure native More menu policy. Coordinates are logical, from the button's
//! left and bottom edges in the webview, never screen pixels or AppKit points.
use crate::placement::{Point, Rect};
use std::sync::Mutex;

pub const CHECK_UPDATES: &str = "popover-check-updates";
pub const QUIT: &str = "popover-quit";

pub fn logical_anchor(left: f64, bottom: f64) -> Result<Point, &'static str> {
    if !left.is_finite() || !bottom.is_finite() || left < 0.0 || bottom < 0.0 {
        return Err("Choose a button inside the popover");
    }
    Ok(Point { x: left, y: bottom })
}

/// Both production handlers and tests use this dispatch; tests never exit.
pub fn dispatch(id: &str, update: impl FnOnce(), quit: impl FnOnce()) -> bool {
    match id {
        CHECK_UPDATES => update(),
        QUIT => quit(),
        _ => return false,
    }
    true
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Dismissal {
    KeepPopover,
    Outside,
}

#[derive(Clone, Copy)]
pub enum ClosingEvent {
    Key,
    Click { point: Point, frame: Rect },
    Other,
}

/// Native event types are mapped by the adapter; key and focus reads stay lazy
/// so a menu selection cannot be reclassified using an off-panel click or blur.
pub fn dismissal(
    visible: bool,
    item_selected: bool,
    event: ClosingEvent,
    key_code: impl FnOnce() -> u16,
    panel_is_key: impl FnOnce() -> bool,
) -> Dismissal {
    if !visible {
        return Dismissal::Outside;
    }
    if item_selected {
        return Dismissal::KeepPopover;
    }
    let keep = match event {
        ClosingEvent::Key if key_code() == 53 => true,
        ClosingEvent::Click { point, frame } => {
            point.x >= frame.x
                && point.x < frame.max_x()
                && point.y >= frame.y
                && point.y < frame.max_y()
        }
        _ => panel_is_key(),
    };
    if keep {
        Dismissal::KeepPopover
    } else {
        Dismissal::Outside
    }
}

/// Release the session guard before AppKit hides or restores key focus: either
/// operation can synchronously deliver focus notifications back into the guard.
pub fn close(
    session: &Mutex<MenuSession>,
    read_dismissal: impl FnOnce() -> Dismissal,
    hide: impl FnOnce(),
    focus: impl FnOnce(),
) {
    let dismissal = read_dismissal();
    let hide_popover = session.lock().expect("popover menu").end(dismissal);
    if hide_popover {
        hide();
    } else {
        focus();
    }
}

#[derive(Default)]
pub struct MenuSession {
    open: bool,
}
impl MenuSession {
    pub fn begin(&mut self) -> bool {
        if self.open {
            return false;
        }
        self.open = true;
        true
    }
    pub fn end(&mut self, dismissal: Dismissal) -> bool {
        self.open = false;
        dismissal == Dismissal::Outside
    }
    /// Consult current native key status too: a blur queued by menu tracking
    /// can arrive after the menu returns and the panel has become key again.
    pub fn hide_on_blur(&self, panel_is_key: bool) -> bool {
        !self.open && !panel_is_key
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::{Cell, RefCell};

    fn click(x: f64) -> ClosingEvent {
        ClosingEvent::Click {
            point: Point { x, y: 20.0 },
            frame: Rect {
                x: 0.0,
                y: 0.0,
                width: 360.0,
                height: 420.0,
            },
        }
    }

    fn no_key() -> u16 {
        panic!("mouse/other events must not read keyCode")
    }

    #[test]
    fn logical_anchor_is_below_the_button_rect_without_scaling() {
        let rect = Rect {
            x: 308.5,
            y: 4.0,
            width: 24.0,
            height: 24.0,
        };
        assert_eq!(
            logical_anchor(rect.x, rect.max_y()).unwrap(),
            Point { x: 308.5, y: 28.0 }
        );
    }

    #[test]
    fn logical_anchor_keeps_button_rects_at_popover_edges() {
        for (rect, expected) in [
            (
                Rect {
                    x: 0.0,
                    y: 0.0,
                    width: 24.0,
                    height: 24.0,
                },
                Point { x: 0.0, y: 24.0 },
            ),
            (
                Rect {
                    x: 336.0,
                    y: 396.0,
                    width: 24.0,
                    height: 24.0,
                },
                Point { x: 336.0, y: 420.0 },
            ),
        ] {
            assert_eq!(logical_anchor(rect.x, rect.max_y()).unwrap(), expected);
        }
        for (x, y) in [(f64::NAN, 0.0), (0.0, f64::INFINITY), (-1.0, 0.0)] {
            assert!(logical_anchor(x, y).is_err());
        }
    }

    #[test]
    fn item_ids_dispatch_to_the_existing_handlers_with_fakes() {
        let updates = Cell::new(0);
        let quits = Cell::new(0);
        for (id, expected) in [
            (CHECK_UPDATES, (1, 0)),
            (QUIT, (1, 1)),
            ("pane-action:token:quit", (1, 1)),
            ("quit", (1, 1)),
        ] {
            let handled = dispatch(
                id,
                || updates.set(updates.get() + 1),
                || quits.set(quits.get() + 1),
            );
            assert_eq!(handled, [CHECK_UPDATES, QUIT].contains(&id));
            assert_eq!((updates.get(), quits.get()), expected, "handler for {id}");
        }
        assert_eq!((updates.get(), quits.get()), (1, 1));
    }

    #[test]
    fn menu_blur_and_queued_blur_preserve_visibility_after_escape_or_inside_click() {
        for event in [ClosingEvent::Key, click(20.0)] {
            let mut session = MenuSession::default();
            assert!(session.begin());
            assert!(!session.begin());
            assert!(!session.hide_on_blur(false));
            assert!(!session.end(dismissal(true, false, event, || 53, || false)));
            assert!(!session.hide_on_blur(true));
            // A later, real focus loss is never swallowed by the session.
            assert!(session.hide_on_blur(false));
        }
    }

    #[test]
    fn outside_dismissal_hides_and_closes_the_guard() {
        let mut session = MenuSession::default();
        assert!(session.begin());
        assert!(!session.hide_on_blur(false));
        assert!(session.end(dismissal(true, false, click(400.0), no_key, || true)));
        assert!(session.hide_on_blur(false));
        assert!(session.begin());
        assert!(!session.end(Dismissal::KeepPopover));
        assert_eq!(
            dismissal(false, false, ClosingEvent::Key, || 53, || true),
            Dismissal::Outside
        );
        assert_eq!(
            dismissal(true, false, ClosingEvent::Other, no_key, || false),
            Dismissal::Outside
        );
    }

    #[test]
    fn off_panel_menu_selection_never_counts_as_an_outside_click() {
        assert_eq!(
            dismissal(true, true, click(400.0), no_key, || panic!(
                "selection must ignore blur"
            )),
            Dismissal::KeepPopover,
        );
        // The same pointer with no selected menu item is a real outside click.
        assert_eq!(
            dismissal(true, false, click(400.0), no_key, || true),
            Dismissal::Outside
        );
        assert_eq!(
            dismissal(false, true, click(400.0), no_key, || true),
            Dismissal::Outside
        );
    }

    #[test]
    fn dismissal_reads_only_the_key_or_focus_required_by_the_closing_event() {
        let focus = Cell::new(true);
        let is_key = || focus.get();
        assert_eq!(
            dismissal(
                true,
                false,
                ClosingEvent::Key,
                || 53,
                || panic!("Escape ignores blur")
            ),
            Dismissal::KeepPopover
        );
        assert_eq!(
            dismissal(true, false, click(20.0), no_key, || panic!(
                "inside click ignores blur"
            )),
            Dismissal::KeepPopover
        );
        for event in [ClosingEvent::Key, ClosingEvent::Other] {
            assert_eq!(
                dismissal(true, false, event, || 0, is_key),
                Dismissal::KeepPopover
            );
            focus.set(false);
            assert_eq!(
                dismissal(true, false, event, || 0, is_key),
                Dismissal::Outside
            );
            focus.set(true);
        }
        for point in [
            Point { x: 360.0, y: 20.0 },
            Point { x: 20.0, y: 420.0 },
            Point { x: -1.0, y: 20.0 },
        ] {
            let ClosingEvent::Click { frame, .. } = click(0.0) else {
                unreachable!()
            };
            assert_eq!(
                dismissal(
                    true,
                    false,
                    ClosingEvent::Click { point, frame },
                    no_key,
                    || true
                ),
                Dismissal::Outside
            );
        }
    }

    #[test]
    fn close_releases_the_guard_before_hiding_or_restoring_focus() {
        for dismissal in [Dismissal::Outside, Dismissal::KeepPopover] {
            let session = Mutex::new(MenuSession::default());
            assert!(session.lock().unwrap().begin());
            let calls = RefCell::new(Vec::new());
            let finish = |action| {
                let session = session.try_lock().expect("callback can reenter the guard");
                assert!(session.hide_on_blur(false));
                calls.borrow_mut().push(action);
            };
            close(
                &session,
                || {
                    assert!(!session.try_lock().unwrap().hide_on_blur(false));
                    calls.borrow_mut().push("read");
                    dismissal
                },
                || finish("hide"),
                || finish("focus"),
            );
            let expected = if dismissal == Dismissal::Outside {
                "hide"
            } else {
                "focus"
            };
            assert_eq!(*calls.borrow(), ["read", expected]);
            assert!(session.lock().unwrap().begin());
        }
    }
}
