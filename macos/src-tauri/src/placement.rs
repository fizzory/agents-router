//! Popover placement. Everything here is in AppKit points with a bottom-left
//! origin and y growing upwards: `NSScreen` frames, `NSEvent::mouseLocation`
//! and `NSWindow` frames all use it. The one input in another space is
//! tray-icon's click event, which `tray_click` turns back into points with the
//! height tray-icon flipped it against and the scale of the window that got
//! it; past that, no scale factor enters the maths.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Rect {
    pub x: f64,
    pub y: f64,
    pub width: f64,
    pub height: f64,
}
impl Rect {
    pub fn max_x(self) -> f64 {
        self.x + self.width
    }
    pub fn max_y(self) -> f64 {
        self.y + self.height
    }
    fn centre(self) -> Point {
        Point {
            x: self.x + self.width / 2.0,
            y: self.y + self.height / 2.0,
        }
    }
    /// Half-open on every side: a point on the edge two displays share is in one.
    fn contains(self, point: Point) -> bool {
        point.x >= self.x && point.y >= self.y && point.x < self.max_x() && point.y < self.max_y()
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Point {
    pub x: f64,
    pub y: f64,
}

/// One `NSScreen`: its display id (`deviceDescription`'s `NSScreenNumber`,
/// 0 when unknown), its `frame` and its `visibleFrame`.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Screen {
    pub id: u32,
    pub frame: Rect,
    pub visible: Rect,
}

/// How the popover is being opened.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Open {
    /// A click on the status item, at the point the click happened.
    Click(Point),
    /// Anything else: a program, a keyboard shortcut, the first launch.
    Other,
}

/// Why the popover sits where it does, as the log spells it.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Reason {
    /// Clicked, and the status item's own window is that icon on that display.
    ClickOnItem,
    /// Clicked, and the status item's window is elsewhere or a whole menu bar.
    ClickPointer,
    /// Not clicked, or clicked outside every menu bar: the status item's
    /// window is a real icon on a screen.
    Item,
    /// The same without a usable status item: the pointer display's top right.
    PointerDisplay,
}
impl Reason {
    pub fn as_str(self) -> &'static str {
        match self {
            Reason::ClickOnItem => "click-on-status-item",
            Reason::ClickPointer => "click-pointer",
            Reason::Item => "status-item",
            Reason::PointerDisplay => "pointer-display-top-right",
        }
    }
}

/// Where the popover goes. `show` makes one; `resize` reads the same display
/// again with `reanchor`, keeping its centre.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Anchor {
    /// Index into the screens `anchor` was given.
    pub screen: usize,
    /// That screen's display id, to find it again after the screens change.
    pub display: u32,
    /// That screen's frame: finds it when its id is unknown, and bounds the popover.
    pub screen_frame: Rect,
    /// The point the reason names: the click, the item's centre or the pointer.
    pub point: Point,
    /// The horizontal centre; `None` is the screen's right edge.
    pub centre_x: Option<f64>,
    /// The span the popover's width always covers: the clicked icon, or the
    /// click when the icon is unknown.
    pub keep: Option<(f64, f64)>,
    /// The popover's top edge: the bottom of that screen's menu bar.
    pub top: f64,
    /// The menu bar height used for `top` and for picking the screen.
    pub menu_bar: f64,
    /// Whether `menu_bar` was read off this display (its inset or the status
    /// item's window in its menu bar) rather than the fallback.
    pub measured: bool,
    /// The visible frame below `top`; the popover stays inside it.
    pub work: Rect,
    pub reason: Reason,
}
impl Anchor {
    fn is_on(&self, screen: &Screen) -> bool {
        if self.display != 0 {
            screen.id == self.display
        } else {
            screen.frame == self.screen_frame
        }
    }
}

pub const MIN_POPOVER_HEIGHT: f64 = 80.0;
pub fn popover_cap(work: Rect) -> f64 {
    (work.height - 24.0).clamp(0.0, 560.0)
}
/// Taller than this is not a menu bar, wider than this not one status item.
const MAX_MENU_BAR: f64 = 64.0;
const MAX_ITEM_WIDTH: f64 = 400.0;
/// The shortest real menu bar on macOS 26 (an external display's), and the
/// most gap a fallback height may leave below it.
const MENU_BAR: f64 = 30.0;
const MAX_GAP: f64 = 8.0;

/// The menu bar heights in use. A screen whose `visibleFrame` shows its menu
/// bar uses that inset. Otherwise the status item's window, when it sits at
/// that screen's top, is the real bar (30 points on a macOS 26 external display
/// whose `visibleFrame` equals its frame). Otherwise the fallback: the tallest
/// of the status bar's thickness (24 there, 6 points short), the item's bar on
/// another screen and 30. A gap of up to 8 points beats a popover whose top
/// edge hides under the menu bar; more than that (a notched display's bar
/// above 38 points) falls back to 30.
struct Bars {
    item: Option<Rect>,
    fallback: f64,
}
impl Bars {
    fn new(screens: &[Screen], item: Option<Rect>, thickness: f64) -> Self {
        let item = item.filter(|r| screens.iter().any(|s| Self::at_top(*r, *s)));
        let tallest = thickness.max(item.map_or(0.0, |r| r.height)).max(MENU_BAR);
        Bars {
            item,
            fallback: if tallest > MENU_BAR + MAX_GAP {
                MENU_BAR
            } else {
                tallest
            },
        }
    }
    fn at_top(r: Rect, screen: Screen) -> bool {
        r.height > 0.0
            && r.height <= MAX_MENU_BAR
            && (r.max_y() - screen.frame.max_y()).abs() <= 1.0
            && r.x < screen.frame.max_x()
            && r.max_x() > screen.frame.x
    }
    /// The height, and whether it was read off this screen.
    fn height(&self, screen: Screen) -> (f64, bool) {
        let inset = screen.frame.max_y() - screen.visible.max_y();
        match self.item {
            _ if inset > 0.0 => (inset, true),
            Some(r) if Self::at_top(r, screen) => (r.height, true),
            _ => (self.fallback, false),
        }
    }
    /// A point in this display's menu bar, its top edge included: `NSEvent`'s
    /// `mouseLocation` reports the top row of the built-in display as y = 982,
    /// the bottom edge of a display placed above it.
    fn holds(&self, screen: Screen, point: Point) -> bool {
        let top = screen.frame.max_y();
        point.x >= screen.frame.x
            && point.x < screen.frame.max_x()
            && point.y <= top
            && point.y > top - self.height(screen).0
    }
}
/// The screen a point belongs to: the one whose menu bar holds it, then the
/// half-open frames, then the nearest screen.
fn screen_at(screens: &[Screen], point: Point, bars: &Bars) -> Option<usize> {
    let distance = |s: &Screen| {
        let dx = (s.frame.x - point.x)
            .max(point.x - s.frame.max_x())
            .max(0.0);
        let dy = (s.frame.y - point.y)
            .max(point.y - s.frame.max_y())
            .max(0.0);
        dx * dx + dy * dy
    };
    screens
        .iter()
        .position(|s| bars.holds(*s, point))
        .or_else(|| screens.iter().position(|s| s.frame.contains(point)))
        .or_else(|| {
            (0..screens.len())
                .min_by(|&a, &b| distance(&screens[a]).total_cmp(&distance(&screens[b])))
        })
}
/// The screen whose menu bar holds a real, icon-sized status item. A whole
/// menu bar strip (what some macOS versions report as the item's window) or a
/// rect off every screen is not one.
fn item_screen(screens: &[Screen], item: Option<Rect>, bars: &Bars) -> Option<(usize, Rect)> {
    let item = item.filter(|r| r.width > 0.0 && r.height > 0.0 && r.height <= MAX_MENU_BAR)?;
    let index = screen_at(screens, item.centre(), bars)?;
    let frame = screens[index].frame;
    let icon = item.width <= MAX_ITEM_WIDTH.min(frame.width / 2.0) && frame.contains(item.centre());
    icon.then_some((index, item))
}

/// What tray-icon put in a click event, as Tauri delivers it. tray-icon 0.25.1
/// reads the pointer (`NSEvent::mouseLocation`) and the status item window's
/// frame in its own `mouseUp:` handler, before Tauri queues the event
/// (`send_mouse_event`, `get_tray_rect`). It flips both with
/// `flip_window_screen_coordinates`, which returns
/// `CGDisplayPixelsHigh(CGMainDisplayID()) - y`, takes the frame's top edge
/// (`flip(origin.y) - height`) and multiplies both by the backing scale of
/// that window, which is not always the clicked
/// display's: macOS 26 can deliver a click on one display's copy of the item to
/// its window on another. Tauri rounds the rect's position to whole pixels and
/// dpi its size; the position stays fractional.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct TrayEvent {
    pub position: Point,
    pub rect: Rect,
}
/// The click in AppKit points: tray-icon's sums undone. `item` and `scale` are
/// the status item window's frame and `backingScaleFactor` now, `flip`
/// `CGDisplayPixelsHigh(CGMainDisplayID())` now: the very number tray-icon
/// subtracted from, whatever its unit. `None` when they no longer give the
/// rect the event carries, to a pixel: the window moved or changed scale, or
/// the main display changed, since.
pub fn tray_click(event: TrayEvent, item: Rect, scale: f64, flip: f64) -> Option<Point> {
    let near = |a: f64, b: f64| (a - b).abs() <= 1.0;
    let same = scale.is_finite()
        && scale > 0.0
        && flip.is_finite()
        && near(event.rect.x, item.x * scale)
        && near(event.rect.y, (flip - item.max_y()) * scale)
        && near(event.rect.width, item.width * scale)
        && near(event.rect.height, item.height * scale);
    let point = Point {
        x: event.position.x / scale,
        y: flip - event.position.y / scale,
    };
    (same && point.x.is_finite() && point.y.is_finite()).then_some(point)
}
/// The point a click opens from, and the log's `click-source=`: tray-icon's
/// reading of it when `tray_click` can undo it against the status item window
/// (`item`, its frame and scale) and `flip`, else `pointer`, the pointer now.
pub fn click(
    event: Option<TrayEvent>,
    item: Option<(Rect, f64)>,
    flip: f64,
    pointer: Point,
) -> (Point, &'static str) {
    event
        .zip(item)
        .and_then(|(event, (item, scale))| tray_click(event, item, scale, flip))
        .map_or((pointer, "pointer-now"), |click| (click, "tray-event"))
}

/// Where to open the popover. `pointer` is `NSEvent::mouseLocation` now,
/// `item` the status item window's frame, `thickness` the status bar's. A
/// click outside every menu bar (pressed on the icon and released elsewhere,
/// or read too late) is not trusted: the popover opens as a non-click open
/// would, from the status item's window, else the pointer's display.
pub fn anchor(
    screens: &[Screen],
    open: Open,
    pointer: Point,
    item: Option<Rect>,
    thickness: f64,
) -> Option<Anchor> {
    let bars = Bars::new(screens, item, thickness);
    let on_item = item_screen(screens, item, &bars);
    let span = |r: Rect| Some((r.x, r.max_x()));
    let (screen, point, centre_x, keep, reason) = match open {
        Open::Click(click) if screens.iter().any(|s| bars.holds(*s, click)) => {
            let screen = screen_at(screens, click, &bars)?;
            match on_item {
                Some((index, item))
                    if index == screen && click.x >= item.x && click.x < item.max_x() =>
                {
                    let centre = item.centre().x;
                    (screen, click, Some(centre), span(item), Reason::ClickOnItem)
                }
                _ => (
                    screen,
                    click,
                    Some(click.x),
                    Some((click.x, click.x)),
                    Reason::ClickPointer,
                ),
            }
        }
        _ => match on_item {
            Some((index, item)) => {
                let centre = item.centre();
                (index, centre, Some(centre.x), span(item), Reason::Item)
            }
            None => (
                screen_at(screens, pointer, &bars)?,
                pointer,
                None,
                None,
                Reason::PointerDisplay,
            ),
        },
    };
    let bar = bars.height(screens[screen]);
    Some(place(screens, screen, bar, point, centre_x, keep, reason))
}

/// The anchor for a resize: the display `previous` chose, found by its id,
/// with its menu bar, top edge and work area read again, since the Dock or the
/// menu bar may have changed since. The centre, the kept span and the reason
/// stay, moved with the display if it moved. A height read off the display
/// beats the fallback: the status item's window can move to its copy on
/// another display between `show` and `resize`. Without `previous`, or when
/// its display went away, a non-click open's anchor, which the caller keeps.
pub fn reanchor(
    previous: Option<&Anchor>,
    screens: &[Screen],
    pointer: Point,
    item: Option<Rect>,
    thickness: f64,
) -> Option<Anchor> {
    let Some((previous, index)) =
        previous.and_then(|p| Some((p, screens.iter().position(|s| p.is_on(s))?)))
    else {
        return anchor(screens, Open::Other, pointer, item, thickness);
    };
    let bars = Bars::new(screens, item, thickness);
    let fresh = bars.height(screens[index]);
    let bar = if !fresh.1 && previous.measured {
        (previous.menu_bar, true)
    } else {
        fresh
    };
    let frame = screens[index].frame;
    let (dx, dy) = (
        frame.x - previous.screen_frame.x,
        frame.max_y() - previous.screen_frame.max_y(),
    );
    let point = Point {
        x: previous.point.x + dx,
        y: previous.point.y + dy,
    };
    Some(place(
        screens,
        index,
        bar,
        point,
        previous.centre_x.map(|x| x + dx),
        previous.keep.map(|(left, right)| (left + dx, right + dx)),
        previous.reason,
    ))
}

fn place(
    screens: &[Screen],
    screen: usize,
    (menu_bar, measured): (f64, bool),
    point: Point,
    centre_x: Option<f64>,
    keep: Option<(f64, f64)>,
    reason: Reason,
) -> Anchor {
    let s = screens[screen];
    let top = s.frame.max_y() - menu_bar;
    let bottom = s.visible.y.min(top);
    Anchor {
        screen,
        display: s.id,
        screen_frame: s.frame,
        point,
        centre_x,
        keep,
        top,
        menu_bar,
        measured,
        work: Rect {
            x: s.visible.x,
            y: bottom,
            width: s.visible.width,
            height: top - bottom,
        },
        reason,
    }
}

/// The popover's frame at this anchor: its top edge at `anchor.top`, its height
/// the content's between an 80-point floor and `popover_cap`. Horizontally it
/// is centred on the anchor and clamped into the visible frame. Its width then
/// always covers the kept span, the clicked icon: where a Dock at the side
/// narrows the visible frame past the icon, the popover reaches over the
/// Dock's column instead, never off the display.
pub fn fit(anchor: &Anchor, width: f64, content_height: f64) -> Rect {
    let work = anchor.work;
    let cap = popover_cap(work);
    let requested = if content_height.is_finite() {
        content_height
    } else {
        MIN_POPOVER_HEIGHT
    };
    let height = requested.clamp(MIN_POPOVER_HEIGHT.min(cap), cap);
    let width = width.min(work.width);
    let right = work.max_x() - width;
    let mut x = anchor.centre_x.map_or(right, |centre| {
        (centre - width / 2.0).round().clamp(work.x, right)
    });
    if let Some((left, end)) = anchor.keep {
        let frame = anchor.screen_frame;
        x = x
            .min(left.floor())
            .max((end - width).ceil())
            .min(frame.max_x() - width)
            .max(frame.x);
    }
    Rect {
        x,
        y: anchor.top - height,
        width,
        height,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    const WIDTH: f64 = 360.0;
    const THICKNESS: f64 = 24.0;
    fn rect(x: f64, y: f64, width: f64, height: f64) -> Rect {
        Rect {
            x,
            y,
            width,
            height,
        }
    }
    fn at(x: f64, y: f64) -> Point {
        Point { x, y }
    }
    /// The reported Mac: the built-in Retina display (scale 2) is the main
    /// screen, with a 33-point menu bar in its visibleFrame; the LG FULL HD
    /// (scale 1) sits above it, and its visibleFrame equals its frame.
    const BUILT_IN: Screen = Screen {
        id: 1,
        frame: Rect {
            x: 0.0,
            y: 0.0,
            width: 1512.0,
            height: 982.0,
        },
        visible: Rect {
            x: 0.0,
            y: 0.0,
            width: 1512.0,
            height: 949.0,
        },
    };
    const LG: Screen = Screen {
        id: 2,
        frame: Rect {
            x: -184.0,
            y: 982.0,
            width: 1920.0,
            height: 1080.0,
        },
        visible: Rect {
            x: -184.0,
            y: 982.0,
            width: 1920.0,
            height: 1080.0,
        },
    };
    const SCREENS: [Screen; 2] = [BUILT_IN, LG];
    /// The LG's real menu bar, 30 points: its bottom edge.
    const LG_BAR_BOTTOM: f64 = 2062.0 - 30.0;
    /// The item as Control Center draws it on each menu bar, in AppKit points
    /// (CGWindowList: built-in (892,0,77,33), LG (1074,-1080,77,30), y down).
    const BUILT_IN_ITEM: Rect = Rect {
        x: 892.0,
        y: 949.0,
        width: 77.0,
        height: 33.0,
    };
    const LG_ITEM: Rect = Rect {
        x: 1074.0,
        y: 2032.0,
        width: 77.0,
        height: 30.0,
    };
    fn open(open: Open, pointer: Point, item: Option<Rect>) -> (Anchor, Rect) {
        let anchor = anchor(&SCREENS, open, pointer, item, THICKNESS).expect("a screen");
        (anchor, fit(&anchor, WIDTH, 238.0))
    }
    /// `CGDisplayPixelsHigh(CGMainDisplayID())` on the reported Mac, as
    /// tray-icon reads it: the built-in's 982 points at 2x in pixels, as the
    /// review read the API, or in points, as a command-line process reads it
    /// there. `tray_click` takes it from the same call, so both round-trip.
    const FLIPS: [f64; 2] = [1964.0, 982.0];
    /// Far from both items: a click read through it is `pointer-now`.
    const POINTER: Point = Point { x: 20.0, y: 500.0 };
    /// What tray-icon 0.25.1 sends, as Tauri delivers it, for a click at
    /// `click` through the item window `window` at `scale`, flipped against
    /// `flip`: `send_mouse_event`'s cursor `(x, flip - y) * scale`, unrounded,
    /// and `get_tray_rect`'s `(x, flip - y - height) * scale`, whose position
    /// Tauri rounds to `i32` and whose size dpi rounds to `u32`.
    fn tray_event(click: Point, window: Rect, scale: f64, flip: f64) -> TrayEvent {
        TrayEvent {
            position: at(click.x * scale, (flip - click.y) * scale),
            rect: rect(
                (window.x * scale).round(),
                ((flip - window.y - window.height) * scale).round(),
                (window.width * scale).round(),
                (window.height * scale).round(),
            ),
        }
    }

    #[test]
    fn the_reported_click_opens_under_the_built_in_menu_bar_not_mid_lg() {
        // The status item's window was the LG copy; the click was on the built-in.
        let click = at(930.0, 965.0);
        let (anchor, frame) = open(Open::Click(click), click, Some(LG_ITEM));
        assert_eq!((anchor.screen, anchor.reason), (0, Reason::ClickPointer));
        assert_eq!((anchor.top, anchor.menu_bar), (949.0, 33.0));
        assert_eq!(frame, rect(750.0, 711.0, 360.0, 238.0));
        // Where the positioner's async move left it: CG (376,-540), mid-LG.
        assert_ne!(frame, rect(376.0, 1284.0, 360.0, 238.0));
    }
    #[test]
    fn a_click_on_each_menu_bar_opens_on_that_display() {
        use Reason::{ClickOnItem, ClickPointer};
        // (click, status item window, screen, reason, top, x)
        let built_in = at(930.0, 965.0);
        let lg = at(1112.0, 2047.0);
        let cases = [
            (built_in, Some(BUILT_IN_ITEM), 0, ClickOnItem, 949.0, 751.0),
            (built_in, Some(LG_ITEM), 0, ClickPointer, 949.0, 750.0),
            (built_in, None, 0, ClickPointer, 949.0, 750.0),
            (lg, Some(LG_ITEM), 1, ClickOnItem, 2032.0, 933.0),
            // The built-in's 33-point item is the tallest bar known: 3 points of gap.
            (lg, Some(BUILT_IN_ITEM), 1, ClickPointer, 2029.0, 932.0),
            (lg, None, 1, ClickPointer, LG_BAR_BOTTOM, 932.0),
        ];
        for (click, window, screen, reason, top, x) in cases {
            let (anchor, frame) = open(Open::Click(click), click, window);
            assert_eq!(
                (anchor.screen, anchor.reason),
                (screen, reason),
                "{click:?} {window:?}"
            );
            assert_eq!((frame.max_y(), frame.x), (top, x), "{click:?} {window:?}");
            assert_eq!(anchor.point, click);
            let visible = SCREENS[screen].visible;
            assert!(frame.x >= visible.x && frame.max_x() <= visible.max_x());
            assert!(SCREENS[screen].frame.contains(at(frame.x, frame.y)));
        }
    }
    #[test]
    fn the_lg_menu_bar_uses_the_real_bar_or_a_fallback_of_at_least_30() {
        // visibleFrame equals the frame there: the item's 30-point window is
        // the real bar; without it, 30 rather than NSStatusBar's 24.
        let click = at(1112.0, 2047.0);
        let (with_item, _) = open(Open::Click(click), click, Some(LG_ITEM));
        assert_eq!((with_item.menu_bar, with_item.top), (30.0, 2032.0));
        assert!(with_item.measured);
        let (without, _) = open(Open::Click(click), click, None);
        assert_eq!((without.menu_bar, without.top), (30.0, 2032.0));
        assert!(!without.measured);
        // A taller window elsewhere never overrides the inset a visibleFrame shows.
        let tall = rect(0.0, 2002.0, 77.0, 60.0);
        let (built_in, _) = open(Open::Click(at(930.0, 965.0)), click, Some(tall));
        assert_eq!(built_in.menu_bar, 33.0);
    }
    #[test]
    fn the_lg_fallback_never_overlaps_its_bar_and_leaves_at_most_8_points() {
        // No inset on the LG and no item window in its menu bar: the fallback.
        // (item window, menu bar used)
        let notched = |height: f64| rect(892.0, 982.0 - height, 77.0, height);
        let cases = [
            (None, 30.0),
            (Some(rect(5000.0, 5000.0, 77.0, 22.0)), 30.0),
            (Some(BUILT_IN_ITEM), 33.0),
            // A notched built-in's 37 or 38-point bar: 7 or 8 points of gap.
            (Some(notched(37.0)), 37.0),
            (Some(notched(38.0)), 38.0),
            // Taller would leave more than 8: 30 instead.
            (Some(notched(44.0)), 30.0),
        ];
        let click = at(1112.0, 2050.0);
        for thickness in [22.0, 24.0] {
            for (item, menu_bar) in cases {
                let placed =
                    anchor(&SCREENS, Open::Click(click), click, item, thickness).expect("a screen");
                let frame = fit(&placed, WIDTH, 238.0);
                assert_eq!((placed.screen, placed.menu_bar), (1, menu_bar), "{item:?}");
                let overlap = (frame.max_y() - LG_BAR_BOTTOM).max(0.0);
                let gap = LG_BAR_BOTTOM - frame.max_y();
                assert_eq!(overlap, 0.0, "{item:?} {thickness}");
                assert!((0.0..=8.0).contains(&gap), "{item:?} {thickness}: {gap}");
            }
        }
        // A larger NSStatusBar thickness is capped the same way.
        let placed = anchor(&SCREENS, Open::Click(click), click, None, 40.0).unwrap();
        assert_eq!(placed.top, LG_BAR_BOTTOM);
    }
    #[test]
    fn a_point_on_the_shared_edge_is_the_built_in_menu_bar() {
        // y = 982 is the built-in's top row and the LG's bottom edge.
        for x in [0.0, 930.0, 1511.0] {
            let click = at(x, 982.0);
            let (anchor, frame) = open(Open::Click(click), click, None);
            assert_eq!(
                (anchor.screen, anchor.reason),
                (0, Reason::ClickPointer),
                "x={x}"
            );
            assert_eq!(frame.max_y(), 949.0);
        }
        // Past the built-in's right edge the same y is only the LG's bottom row.
        let (anchor, _) = open(Open::Other, at(1600.0, 982.0), None);
        assert_eq!(anchor.screen, 1);
        // Just above it is the LG; just below, still the built-in's menu bar.
        let (above, _) = open(Open::Other, at(930.0, 982.5), None);
        let (below, _) = open(Open::Other, at(930.0, 981.5), None);
        assert_eq!((above.screen, below.screen), (1, 0));
    }
    #[test]
    fn a_click_outside_every_menu_bar_opens_from_the_status_item() {
        // Pressed on the icon, released mid-LG: the item's window, else the
        // pointer's display.
        let off = at(600.0, 1500.0);
        let (anchor, frame) = open(Open::Click(off), off, Some(BUILT_IN_ITEM));
        assert_eq!((anchor.screen, anchor.reason), (0, Reason::Item));
        assert_eq!((frame.x, frame.max_y()), (751.0, 949.0));
        let (anchor, frame) = open(Open::Click(off), off, None);
        assert_eq!((anchor.screen, anchor.reason), (1, Reason::PointerDisplay));
        assert_eq!((frame.x, frame.max_y()), (1736.0 - 360.0, LG_BAR_BOTTOM));
        // Just under a menu bar is outside it too; inside it is a click.
        let (under, _) = open(Open::Click(at(930.0, 948.0)), at(0.0, 0.0), Some(LG_ITEM));
        let (inside, _) = open(Open::Click(at(930.0, 950.0)), at(0.0, 0.0), Some(LG_ITEM));
        assert_eq!(
            (under.reason, inside.reason),
            (Reason::Item, Reason::ClickPointer)
        );
    }
    #[test]
    fn a_click_on_the_built_in_item_round_trips_as_a_tray_event() {
        for flip in FLIPS {
            for pressed in [at(930.0, 970.0), at(892.5, 949.5), at(968.0, 982.0)] {
                let event = tray_event(pressed, BUILT_IN_ITEM, 2.0, flip);
                assert_eq!(
                    click(Some(event), Some((BUILT_IN_ITEM, 2.0)), flip, POINTER),
                    (pressed, "tray-event"),
                    "{pressed:?} flipped against {flip}"
                );
            }
        }
    }
    #[test]
    fn a_click_on_the_lg_item_round_trips_as_a_tray_event() {
        for flip in FLIPS {
            for pressed in [at(1112.0, 2050.0), at(1074.5, 2032.5), at(1150.0, 2062.0)] {
                let event = tray_event(pressed, LG_ITEM, 1.0, flip);
                assert_eq!(
                    click(Some(event), Some((LG_ITEM, 1.0)), flip, POINTER),
                    (pressed, "tray-event"),
                    "{pressed:?} flipped against {flip}"
                );
            }
        }
    }
    #[test]
    fn a_moved_item_window_falls_back_to_the_pointer() {
        let pointer_now = (POINTER, "pointer-now");
        for flip in FLIPS {
            for (pressed, window, scale) in [
                (at(930.0, 970.0), BUILT_IN_ITEM, 2.0),
                (at(1112.0, 2050.0), LG_ITEM, 1.0),
            ] {
                let event = tray_event(pressed, window, scale, flip);
                let moved = [
                    rect(window.x + 40.0, window.y, window.width, window.height),
                    rect(window.x, window.y - 3.0, window.width, window.height),
                    rect(window.x, window.y, window.width + 12.0, window.height),
                ];
                for now in moved {
                    assert_eq!(
                        click(Some(event), Some((now, scale)), flip, POINTER),
                        pointer_now
                    );
                }
                // Changed scale, no item window, no event.
                assert_eq!(
                    click(Some(event), Some((window, 3.0 - scale)), flip, POINTER),
                    pointer_now
                );
                assert_eq!(click(Some(event), None, flip, POINTER), pointer_now);
                assert_eq!(
                    click(None, Some((window, scale)), flip, POINTER),
                    pointer_now
                );
            }
        }
    }
    #[test]
    fn tray_events_decode_with_the_scale_of_the_window_that_got_them() {
        let built_in = at(930.0, 970.0);
        let lg = at(1112.0, 2050.0);
        for flip in FLIPS {
            // (click, the item window that got it, its scale): each display's
            // own window, and macOS 26 handing the click on one display's copy
            // to the window on the other.
            for (click, window, scale) in [
                (built_in, BUILT_IN_ITEM, 2.0),
                (lg, LG_ITEM, 1.0),
                (built_in, LG_ITEM, 1.0),
                (lg, BUILT_IN_ITEM, 2.0),
                (at(930.0, 982.0), BUILT_IN_ITEM, 2.0),
            ] {
                let event = tray_event(click, window, scale, flip);
                assert_eq!(
                    tray_click(event, window, scale, flip),
                    Some(click),
                    "{click:?} via {window:?}@{scale} flipped against {flip}"
                );
            }
            // The built-in's click through the LG window, read at the built-in's
            // scale: the rect doesn't match, so no point.
            let event = tray_event(built_in, LG_ITEM, 1.0, flip);
            assert_eq!(event.position, at(930.0, flip - 970.0));
            assert_eq!(tray_click(event, LG_ITEM, 2.0, flip), None);
            // A window that moved or changed scale since: no point.
            let event = tray_event(built_in, BUILT_IN_ITEM, 2.0, flip);
            assert_eq!(tray_click(event, LG_ITEM, 2.0, flip), None);
            assert_eq!(tray_click(event, BUILT_IN_ITEM, 1.0, flip), None);
            assert_eq!(tray_click(event, BUILT_IN_ITEM, 0.0, flip), None);
            assert_eq!(tray_click(event, BUILT_IN_ITEM, f64::NAN, flip), None);
            assert_eq!(tray_click(event, BUILT_IN_ITEM, 2.0, f64::NAN), None);
        }
        // Undone against another height than tray-icon flipped it against (the
        // main display's frame in points when tray-icon used pixels, or the
        // reverse): the rect doesn't match, so no point rather than a wrong one.
        let [pixels, points] = FLIPS;
        for (flipped, undone) in [(pixels, points), (points, pixels)] {
            for (click, window, scale) in [(built_in, BUILT_IN_ITEM, 2.0), (lg, LG_ITEM, 1.0)] {
                let event = tray_event(click, window, scale, flipped);
                assert_eq!(tray_click(event, window, scale, undone), None);
            }
        }
        // The decoded click places like the pointer it was.
        let point = tray_click(tray_event(lg, LG_ITEM, 1.0, 1964.0), LG_ITEM, 1.0, 1964.0).unwrap();
        let (anchor, frame) = open(Open::Click(point), at(0.0, 0.0), Some(LG_ITEM));
        assert_eq!(
            (anchor.reason, frame.x, frame.max_y()),
            (Reason::ClickOnItem, 933.0, 2032.0)
        );
    }
    #[test]
    fn an_icon_near_either_edge_is_clamped_inside_its_display() {
        let right = rect(1480.0, 949.0, 30.0, 33.0);
        let click = at(1500.0, 970.0);
        let (anchor, frame) = open(Open::Click(click), click, Some(right));
        assert_eq!(anchor.reason, Reason::ClickOnItem);
        assert_eq!((frame.x, frame.max_y()), (1512.0 - 360.0, 949.0));
        let left = rect(-180.0, 2032.0, 40.0, 30.0);
        let click = at(-170.0, 2050.0);
        let (anchor, frame) = open(Open::Click(click), click, Some(left));
        assert_eq!(anchor.reason, Reason::ClickOnItem);
        assert_eq!((frame.x, frame.max_y()), (-184.0, 2032.0));
    }
    #[test]
    fn the_clicked_item_always_sits_inside_the_popovers_width() {
        // A Dock on the right or left narrows the visibleFrame under the menu bar.
        let right_dock = Screen {
            visible: rect(0.0, 0.0, 1440.0, 949.0),
            ..BUILT_IN
        };
        let left_dock = Screen {
            visible: rect(80.0, 0.0, 1432.0, 949.0),
            ..BUILT_IN
        };
        for screen in [BUILT_IN, right_dock, left_dock] {
            let screens = [screen, LG];
            let frame_of = |open: Open, item: Option<Rect>| {
                let placed = anchor(&screens, open, at(0.0, 0.0), item, THICKNESS).unwrap();
                (placed.reason, fit(&placed, WIDTH, 238.0))
            };
            for (bar, item_x, width) in [
                (screen, 0.0, 30.0),
                (screen, 40.0, 77.0),
                (screen, 700.0, 77.0),
                (screen, 1400.0, 77.0),
                (screen, 1482.0, 30.0),
                (LG, -184.0, 40.0),
                (LG, 1650.0, 86.0),
            ] {
                let top = bar.frame.max_y();
                let height = top - bar.visible.max_y();
                let height = if height > 0.0 { height } else { 30.0 };
                let item = rect(item_x, top - height, width, height);
                let display = bar.frame;
                // A click on the icon, the same click with the item's window on
                // the other display, and a non-click open from the icon.
                for (open, window) in [
                    (Open::Click(at(item_x + 1.0, top - 2.0)), Some(item)),
                    (Open::Click(at(item_x + 1.0, top - 2.0)), None),
                    (Open::Other, Some(item)),
                ] {
                    let (reason, frame) = frame_of(open, window);
                    let span = match open {
                        Open::Click(click) if window.is_none() => (click.x, click.x),
                        _ => (item.x, item.max_x()),
                    };
                    let case = format!("{screen:?} {item:?} {reason:?} {frame:?}");
                    assert!(frame.x <= span.0 && span.1 <= frame.max_x(), "{case}");
                    assert!(
                        frame.x >= display.x && frame.max_x() <= display.max_x(),
                        "{case}"
                    );
                    if span.0 >= bar.visible.x && span.1 <= bar.visible.max_x() {
                        let visible = bar.visible;
                        assert!(
                            frame.x >= visible.x && frame.max_x() <= visible.max_x(),
                            "{case}"
                        );
                    }
                }
            }
        }
    }
    #[test]
    fn a_display_without_a_menu_bar_inset_still_opens_below_its_bar() {
        let only = Screen {
            id: 7,
            frame: rect(0.0, 0.0, 1920.0, 1080.0),
            visible: rect(0.0, 0.0, 1920.0, 1080.0),
        };
        let click = at(1000.0, 1075.0);
        let placed = anchor(&[only], Open::Click(click), click, None, 22.0).unwrap();
        let frame = fit(&placed, WIDTH, 238.0);
        assert_eq!(
            (placed.top, frame.max_y(), frame.x),
            (1050.0, 1050.0, 820.0)
        );
        // The Dock at the bottom only shortens the work area.
        let docked = Screen {
            visible: rect(0.0, 80.0, 1920.0, 1000.0),
            ..only
        };
        let placed = anchor(&[docked], Open::Click(click), click, None, 22.0).unwrap();
        assert_eq!(
            (placed.top, placed.work),
            (1050.0, rect(0.0, 80.0, 1920.0, 970.0))
        );
    }
    #[test]
    fn mixed_scale_displays_need_no_scale_factor() {
        // All inputs are AppKit points, so the scale-1 LG above the scale-2
        // built-in places like any other layout: under the LG's icon, at
        // whole points. The old path halved the LG's physical coordinates.
        let click = at(1112.0, 2047.0);
        let (anchor, frame) = open(Open::Click(click), click, Some(LG_ITEM));
        assert_eq!((anchor.screen, anchor.reason), (1, Reason::ClickOnItem));
        assert_eq!(frame, rect(933.0, 1794.0, 360.0, 238.0));
    }
    #[test]
    fn other_opens_use_a_real_status_item_or_the_pointer_display() {
        let pointer = at(300.0, 400.0);
        let (anchor, frame) = open(Open::Other, pointer, Some(BUILT_IN_ITEM));
        assert_eq!((anchor.reason, anchor.screen), (Reason::Item, 0));
        assert_eq!((frame.x, frame.max_y()), (751.0, 949.0));
        let (anchor, frame) = open(Open::Other, pointer, Some(LG_ITEM));
        assert_eq!((anchor.reason, anchor.screen), (Reason::Item, 1));
        assert_eq!((frame.x, frame.max_y()), (933.0, 2032.0));
        // A whole menu bar strip, an off-screen rect, an empty rect or no item:
        // the pointer display's top right, under its menu bar.
        for item in [
            Some(rect(0.0, 949.0, 1512.0, 33.0)),
            Some(rect(-184.0, 2032.0, 1920.0, 30.0)),
            Some(rect(5000.0, 5000.0, 77.0, 22.0)),
            Some(rect(892.0, 949.0, 0.0, 0.0)),
            None,
        ] {
            let (anchor, frame) = open(Open::Other, pointer, item);
            assert_eq!(
                (anchor.reason, anchor.screen),
                (Reason::PointerDisplay, 0),
                "{item:?}"
            );
            assert_eq!((frame.x, frame.max_y()), (1512.0 - 360.0, 949.0));
            let (anchor, frame) = open(Open::Other, at(1500.0, 1500.0), item);
            assert_eq!((anchor.screen, frame.x), (1, 1736.0 - 360.0), "{item:?}");
            assert!(anchor.menu_bar >= 30.0);
            assert_eq!(frame.max_y(), 2062.0 - anchor.menu_bar);
        }
        // No screens at all: nothing to place on.
        assert_eq!(
            super::anchor(&[], Open::Other, pointer, None, THICKNESS),
            None
        );
    }
    #[test]
    fn a_pointer_off_every_screen_uses_the_nearest() {
        let (anchor, _) = open(Open::Other, at(3000.0, 100.0), None);
        assert_eq!(anchor.screen, 0);
        let (anchor, _) = open(Open::Other, at(-500.0, 3000.0), None);
        assert_eq!(anchor.screen, 1);
    }
    #[test]
    fn resizing_keeps_the_top_edge_centre_and_display() {
        let click = at(930.0, 965.0);
        let (anchor, first) = open(Open::Click(click), click, None);
        for request in [80.0, 160.0, 238.0, 560.0, 1200.0] {
            let next = fit(&anchor, WIDTH, request);
            assert_eq!((next.max_y(), next.x), (first.max_y(), first.x));
            assert!(BUILT_IN.frame.contains(at(next.x, next.y)), "{request}");
        }
        // Growing on the LG never reaches the built-in below it.
        let click = at(1112.0, 2047.0);
        let (anchor, _) = open(Open::Click(click), click, Some(LG_ITEM));
        let tall = fit(&anchor, WIDTH, 5000.0);
        assert_eq!((tall.max_y(), tall.height), (2032.0, 560.0));
        assert!(tall.y >= LG.frame.y);
    }
    #[test]
    fn reanchoring_unchanged_screens_gives_the_shown_frame() {
        let built_in = at(930.0, 965.0);
        let lg = at(1112.0, 2047.0);
        for (open_with, item) in [
            (Open::Click(built_in), Some(BUILT_IN_ITEM)),
            (Open::Click(built_in), Some(LG_ITEM)),
            (Open::Click(built_in), None),
            (Open::Click(lg), Some(LG_ITEM)),
            (Open::Click(lg), Some(BUILT_IN_ITEM)),
            (Open::Click(lg), None),
            (Open::Other, Some(LG_ITEM)),
            (Open::Other, None),
        ] {
            let (shown, frame) = open(open_with, at(300.0, 400.0), item);
            let again = reanchor(Some(&shown), &SCREENS, at(0.0, 0.0), item, THICKNESS);
            assert_eq!(again, Some(shown), "{open_with:?} {item:?}");
            // The item's window moving to the other display's copy in between
            // never moves the top edge on the LG.
            for moved in [Some(BUILT_IN_ITEM), Some(LG_ITEM), None] {
                let again =
                    reanchor(Some(&shown), &SCREENS, at(0.0, 0.0), moved, THICKNESS).unwrap();
                let next = fit(&again, WIDTH, 238.0);
                assert_eq!((again.screen, next.x), (shown.screen, frame.x));
                if shown.measured {
                    assert_eq!(
                        next.max_y(),
                        frame.max_y(),
                        "{open_with:?} {item:?} {moved:?}"
                    );
                }
            }
        }
    }
    #[test]
    fn reanchoring_reads_the_menu_bar_and_dock_again_on_the_same_display() {
        let click = at(930.0, 965.0);
        let (shown, frame) = open(Open::Click(click), click, Some(BUILT_IN_ITEM));
        // The Dock appears at the bottom and the menu bar grows to 37 points.
        let changed = Screen {
            visible: rect(0.0, 70.0, 1512.0, 875.0),
            ..BUILT_IN
        };
        let again = reanchor(Some(&shown), &[changed, LG], click, None, THICKNESS).unwrap();
        assert_eq!((again.screen, again.reason), (0, Reason::ClickOnItem));
        assert_eq!((again.top, again.menu_bar), (945.0, 37.0));
        assert_eq!(again.work, rect(0.0, 70.0, 1512.0, 875.0));
        let next = fit(&again, WIDTH, 2000.0);
        assert_eq!((next.x, next.max_y(), next.height), (frame.x, 945.0, 560.0));
        // The LG is rearranged to the built-in's right and changes scale
        // (1920 to 2560 wide): found by its id, the centre moves with it.
        let lg_click = at(1112.0, 2047.0);
        let (shown, frame) = open(Open::Click(lg_click), lg_click, Some(LG_ITEM));
        let moved = Screen {
            id: 2,
            frame: rect(1512.0, 0.0, 2560.0, 1440.0),
            visible: rect(1512.0, 0.0, 2560.0, 1440.0),
        };
        let again = reanchor(
            Some(&shown),
            &[BUILT_IN, moved],
            at(0.0, 0.0),
            None,
            THICKNESS,
        )
        .unwrap();
        assert_eq!((again.screen, again.display, again.menu_bar), (1, 2, 30.0));
        let next = fit(&again, WIDTH, 238.0);
        assert_eq!((next.x, next.max_y()), (frame.x + 1696.0, 1410.0));
    }
    #[test]
    fn a_display_that_went_away_is_replaced_and_the_replacement_kept() {
        let click = at(1112.0, 2047.0);
        let (shown, _) = open(Open::Click(click), click, Some(LG_ITEM));
        // The LG unplugged: a non-click open's anchor on what is left.
        let replaced =
            reanchor(Some(&shown), &[BUILT_IN], at(300.0, 400.0), None, THICKNESS).unwrap();
        assert_eq!(
            (replaced.screen, replaced.display, replaced.reason),
            (0, 1, Reason::PointerDisplay)
        );
        // Plugged back in: the kept replacement stays on the built-in.
        let again = reanchor(
            Some(&replaced),
            &SCREENS,
            at(1112.0, 2047.0),
            Some(LG_ITEM),
            THICKNESS,
        )
        .unwrap();
        assert_eq!((again.screen, again.display), (0, 1));
        assert_eq!(fit(&again, WIDTH, 238.0), fit(&replaced, WIDTH, 238.0));
        // No anchor yet: a non-click open's.
        let fresh = reanchor(None, &SCREENS, at(300.0, 400.0), Some(LG_ITEM), THICKNESS).unwrap();
        assert_eq!((fresh.screen, fresh.reason), (1, Reason::Item));
        // A display without an id is found by its frame.
        let unnamed = [Screen { id: 0, ..BUILT_IN }];
        let (placed, _) = (
            anchor(&unnamed, Open::Other, at(1.0, 1.0), None, THICKNESS).unwrap(),
            (),
        );
        let again = reanchor(Some(&placed), &unnamed, at(1.0, 1.0), None, THICKNESS).unwrap();
        assert_eq!(again, placed);
    }
    #[test]
    fn content_height_has_a_one_row_floor_and_a_560_point_cap() {
        let (anchor, _) = open(Open::Click(at(930.0, 965.0)), at(0.0, 0.0), None);
        for request in [-20.0, 0.0, 32.0, f64::NAN, f64::INFINITY] {
            assert_eq!(fit(&anchor, WIDTH, request).height, 80.0);
        }
        assert_eq!(fit(&anchor, WIDTH, 224.0).height, 224.0);
        assert_eq!(fit(&anchor, WIDTH, 2000.0).height, 560.0);
    }
    #[test]
    fn work_area_cap_leaves_24_points_even_on_a_short_display() {
        let short = |height: f64| Screen {
            id: 3,
            frame: rect(0.0, 0.0, 1200.0, height + 24.0),
            visible: rect(0.0, 0.0, 1200.0, height),
        };
        let anchor_on =
            |screen: Screen| anchor(&[screen], Open::Other, at(10.0, 10.0), None, 24.0).unwrap();
        assert_eq!(fit(&anchor_on(short(400.0)), WIDTH, 900.0).height, 376.0);
        // An unusually small work area takes priority over the normal floor.
        assert_eq!(fit(&anchor_on(short(64.0)), WIDTH, 0.0).height, 40.0);
        // A popover wider than the display fills its width.
        assert_eq!(fit(&anchor_on(short(400.0)), 2000.0, 100.0).width, 1200.0);
    }
}
