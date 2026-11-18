//! All AppKit access lives here and requires a main-thread marker.
use crate::{
    app,
    glyph::{self, Glyph, Input, Variant},
    placement::{self, Open, Point, Rect, Screen},
    watcher::WakeTrigger,
};
use objc2::{
    rc::Retained,
    runtime::{AnyObject, ProtocolObject},
    AnyThread, MainThreadMarker,
};
use objc2_app_kit::{
    NSAccessibility, NSAnimatablePropertyContainer, NSAnimationContext, NSApplication, NSEvent,
    NSFloatingWindowLevel, NSFont, NSImage, NSScreen, NSStatusBar, NSWindow,
    NSWindowCollectionBehavior, NSWindowNumberListOptions, NSWindowOcclusionState,
    NSWindowStyleMask, NSWorkspace, NSWorkspaceDidWakeNotification,
};
use objc2_foundation::{
    NSArray, NSAttributedString, NSData, NSDate, NSDateFormatter, NSDictionary, NSNotification,
    NSNotificationCenter, NSNumber, NSObjectProtocol, NSPoint, NSRect, NSSize, NSString,
};
use objc2_quartz_core::CAMediaTimingFunction;
use std::{
    cell::{Cell, RefCell},
    ptr::NonNull,
};
use tauri::Manager;
use tauri_nspanel::{ManagerExt, WebviewWindowExt};

mod panel_class {
    tauri_nspanel::tauri_panel! { panel!(AgtPopoverPanel { config: { can_become_key_window: true, can_become_main_window: false, is_floating_panel: true } }) }
}

struct WakeObserver {
    center: Retained<NSNotificationCenter>,
    token: Retained<ProtocolObject<dyn NSObjectProtocol>>,
}
impl Drop for WakeObserver {
    fn drop(&mut self) {
        let _mtm = MainThreadMarker::new().expect("wake observer dropped on main thread");
        unsafe {
            self.center
                .removeObserver(AsRef::<AnyObject>::as_ref(&*self.token));
        }
    }
}
thread_local! { static WAKE_OBSERVER: RefCell<Option<WakeObserver>> = const { RefCell::new(None) }; }
thread_local! { static CONTENT_HEIGHT: Cell<f64> = const { Cell::new(placement::MIN_POPOVER_HEIGHT) }; }
// The glyph last applied to the status button; an unchanged glyph is not redrawn.
thread_local! { static SHOWN_GLYPH: RefCell<Option<Glyph>> = const { RefCell::new(None) }; }

pub fn observe_wake(_mtm: MainThreadMarker, wake: WakeTrigger) {
    unsafe {
        let center = NSWorkspace::sharedWorkspace().notificationCenter();
        let block = block2::RcBlock::new(move |_note: NonNull<NSNotification>| {
            app::trace("wake received: queued on shared debounce");
            wake.notify();
        });
        let token = center.addObserverForName_object_queue_usingBlock(
            Some(NSWorkspaceDidWakeNotification),
            None,
            None,
            &block,
        );
        WAKE_OBSERVER.with(|slot| *slot.borrow_mut() = Some(WakeObserver { center, token }));
    }
}
pub fn stop_observing(_mtm: MainThreadMarker) {
    WAKE_OBSERVER.with(|slot| slot.borrow_mut().take());
}

#[cfg(debug_assertions)]
pub fn post_wake(_mtm: MainThreadMarker) {
    unsafe {
        let workspace = NSWorkspace::sharedWorkspace();
        workspace
            .notificationCenter()
            .postNotificationName_object(NSWorkspaceDidWakeNotification, Some(&workspace));
    }
    app::trace("self-test posted NSWorkspaceDidWakeNotification");
}

pub fn create_panel(
    app: &tauri::App,
    _mtm: MainThreadMarker,
) -> Result<(), Box<dyn std::error::Error>> {
    let window = app
        .get_webview_window("popover")
        .ok_or("popover window missing")?;
    let panel = window.to_panel::<panel_class::AgtPopoverPanel>()?;
    panel.set_style_mask(NSWindowStyleMask::Borderless | NSWindowStyleMask::NonactivatingPanel)?;
    panel.set_transparent(true);
    panel.set_level(NSFloatingWindowLevel as i64);
    // The app is an accessory that is never active: the panel is nonactivating
    // and becomes key without activating it. A panel that hides on deactivate
    // belongs to an inactive app the whole time it is open, and WindowServer
    // keeps it off screen: on macOS 26 it was ordered in, animated, key (a
    // click elsewhere blurred it), yet never on screen. It was set to true here.
    panel.set_hides_on_deactivate(false);
    panel.set_released_when_closed(false);
    // On every Space of every display, also beside a full-screen app. Never
    // with MoveToActiveSpace: AppKit throws when both are set, at launch.
    panel.set_collection_behavior(
        NSWindowCollectionBehavior::CanJoinAllSpaces
            | NSWindowCollectionBehavior::FullScreenAuxiliary,
    );
    panel.hide();
    let ns_panel = panel.as_panel();
    app::trace(&format!(
        "popover class={} NSPanel borderless=true transparent=true nonactivating=true hidden=true hides-on-deactivate={} level={} collection={:?}",
        ns_panel.class(),
        ns_panel.hidesOnDeactivate(),
        ns_panel.level(),
        ns_panel.collectionBehavior(),
    ));
    #[cfg(debug_assertions)]
    app::trace(&format!(
        "popover reduce-motion={}",
        NSWorkspace::sharedWorkspace().accessibilityDisplayShouldReduceMotion()
    ));
    Ok(())
}

fn rect(r: NSRect) -> Rect {
    Rect {
        x: r.origin.x,
        y: r.origin.y,
        width: r.size.width,
        height: r.size.height,
    }
}
fn ns_rect(r: Rect) -> NSRect {
    NSRect::new(NSPoint::new(r.x, r.y), NSSize::new(r.width, r.height))
}
fn show_rect(r: Rect) -> String {
    format!("({:.1},{:.1},{:.1},{:.1})", r.x, r.y, r.width, r.height)
}

/// The pointer in AppKit points, now. A click uses it only when tray-icon's
/// own reading of the click can't be turned back into points.
pub fn pointer(_mtm: MainThreadMarker) -> Point {
    let point = NSEvent::mouseLocation();
    Point {
        x: point.x,
        y: point.y,
    }
}
/// tray-icon's reading of a click, as `on_tray_icon_event` gets it: the pointer
/// and the status item window's frame, taken in tray-icon's `mouseUp:` handler
/// before Tauri queued the event. tray-icon sends physical values only.
pub fn tray_event(
    position: tauri::PhysicalPosition<f64>,
    rect: tauri::Rect,
) -> Option<placement::TrayEvent> {
    match (rect.position, rect.size) {
        (tauri::Position::Physical(at), tauri::Size::Physical(size)) => {
            Some(placement::TrayEvent {
                position: Point {
                    x: position.x,
                    y: position.y,
                },
                rect: Rect {
                    x: at.x.into(),
                    y: at.y.into(),
                    width: size.width.into(),
                    height: size.height.into(),
                },
            })
        }
        _ => None,
    }
}
/// The status item's window: its frame in AppKit points and its backing scale.
/// On macOS 26 with several displays it can be the item's copy on another
/// display, or a whole menu bar strip; `placement::anchor` checks the frame
/// before trusting it.
fn status_item_window(app: &tauri::AppHandle) -> Option<(Rect, f64)> {
    app.tray_by_id(app::TRAY_ID)?
        .with_inner_tray_icon(|tray| {
            let mtm = MainThreadMarker::new().expect("tray geometry on main thread");
            tray.ns_status_item()
                .and_then(|item| item.button(mtm))
                .and_then(|button| button.window())
                .map(|window| (rect(window.frame()), window.backingScaleFactor()))
        })
        .ok()
        .flatten()
}
/// The display id behind an `NSScreen`, 0 when it has none: it survives a
/// change of resolution or arrangement, which changes the frame.
fn display_id(screen: &NSScreen) -> u32 {
    let key = NSString::from_str("NSScreenNumber");
    screen
        .deviceDescription()
        .objectForKey(&key)
        .and_then(|value| value.downcast::<NSNumber>().ok())
        .map_or(0, |number| number.unsignedIntValue())
}
// Where the popover was last placed; `resize` reads its display again.
thread_local! { static ANCHOR: Cell<Option<placement::Anchor>> = const { Cell::new(None) }; }

/// What placement reads from AppKit, read fresh for every show and resize.
struct Now {
    screens: Retained<NSArray<NSScreen>>,
    geometry: Vec<Screen>,
    item: Option<(Rect, f64)>,
    thickness: f64,
}
impl Now {
    fn read(app: &tauri::AppHandle, mtm: MainThreadMarker) -> Self {
        let screens = NSScreen::screens(mtm);
        let geometry = screens
            .iter()
            .map(|s| Screen {
                id: display_id(&s),
                frame: rect(s.frame()),
                visible: rect(s.visibleFrame()),
            })
            .collect();
        Now {
            screens,
            geometry,
            item: status_item_window(app),
            thickness: NSStatusBar::systemStatusBar().thickness(),
        }
    }
    fn item_frame(&self) -> Option<Rect> {
        self.item.map(|(frame, _)| frame)
    }
    fn name(&self, index: usize) -> String {
        self.screens
            .iter()
            .nth(index)
            .map_or_else(String::new, |s| s.localizedName().to_string())
    }
}

/// Sets the panel's frame now, or animates to it. Both go through the
/// animator, so a new frame replaces a resize animation still running (a quick
/// hide and reopen); a plain `setFrame` could let that animation carry on to
/// its old target, on the display the popover was on before.
fn set_frame(window: &NSWindow, frame: NSRect, animation: Option<(f64, [f32; 4])>) {
    if animation.is_none() {
        window.setFrame_display(frame, false);
    }
    NSAnimationContext::beginGrouping();
    let context = NSAnimationContext::currentContext();
    match animation {
        Some((seconds, curve)) => {
            context.setDuration(seconds);
            context.setTimingFunction(Some(&CAMediaTimingFunction::functionWithControlPoints(
                curve[0], curve[1], curve[2], curve[3],
            )));
        }
        None => context.setDuration(0.0),
    }
    window
        .animator()
        .setFrame_display(frame, animation.is_some());
    NSAnimationContext::endGrouping();
}

#[link(name = "CoreGraphics", kind = "framework")]
extern "C" {
    // CFArrayRef CGWindowListCopyWindowInfo(CGWindowListOption, CGWindowID): a
    // Copy function, so the array (toll-free bridged to NSArray) comes at +1.
    fn CGWindowListCopyWindowInfo(
        option: u32,
        relative_to_window: u32,
    ) -> *mut NSArray<NSDictionary<NSString, AnyObject>>;
    // CGDirectDisplayID CGMainDisplayID(void)
    fn CGMainDisplayID() -> u32;
    // size_t CGDisplayPixelsHigh(CGDirectDisplayID)
    fn CGDisplayPixelsHigh(display: u32) -> usize;
}
/// The height tray-icon flips its click event against, read the way its
/// `flip_window_screen_coordinates` reads it, so `placement::tray_click` undoes
/// the flip with the same number in the same unit.
fn tray_flip() -> f64 {
    // SAFETY: neither takes a pointer; an unknown display gives 0.
    unsafe { CGDisplayPixelsHigh(CGMainDisplayID()) as f64 }
}
/// kCGWindowListOptionIncludingWindow: only the window named.
const CG_WINDOW_LIST_INCLUDING_WINDOW: u32 = 1 << 3;

/// WindowServer's own answer for one window: `Some(on screen)`, or `None` when
/// it doesn't list the window. AppKit's `isVisible` only says it was ordered in.
fn window_server_onscreen(number: isize) -> Option<bool> {
    let number = u32::try_from(number).ok().filter(|n| *n > 0)?;
    // SAFETY: a valid option and window id; the result is +1 or null.
    let list = unsafe {
        Retained::from_raw(CGWindowListCopyWindowInfo(
            CG_WINDOW_LIST_INCLUDING_WINDOW,
            number,
        ))
    }?;
    let id = NSString::from_str("kCGWindowNumber");
    // Absent when false.
    let onscreen = NSString::from_str("kCGWindowIsOnscreen");
    let flag = |info: &NSDictionary<NSString, AnyObject>, key: &NSString| {
        info.objectForKey(key)
            .and_then(|value| value.downcast::<NSNumber>().ok())
    };
    list.iter()
        .find(|info| flag(info, &id).is_some_and(|n| n.unsignedIntValue() == number))
        .map(|info| flag(&info, &onscreen).is_some_and(|n| n.boolValue()))
}

/// Logs what AppKit and WindowServer say about the shown popover, so one real
/// click tells "placed but kept off screen" from "on screen".
fn log_visibility(app: &tauri::AppHandle, mtm: MainThreadMarker, stage: &str) {
    let Ok(panel) = app.get_webview_panel("popover") else {
        return;
    };
    let window = panel.as_panel();
    let application = NSApplication::sharedApplication(mtm);
    let number = window.windowNumber();
    let listed = NSWindow::windowNumbersWithOptions(NSWindowNumberListOptions::AllSpaces, mtm)
        .is_some_and(|numbers| numbers.iter().any(|n| n.integerValue() == number));
    app::trace(&format!(
        "popover visibility stage={stage} visible={} key={} on-active-space={} occlusion-visible={} alpha={:.2} window={number} window-server-onscreen={} in-window-list={listed} screen={:?} frame={} hides-on-deactivate={} level={} collection={:?} app-active={} app-hidden={}",
        window.isVisible(),
        window.isKeyWindow(),
        window.isOnActiveSpace(),
        window
            .occlusionState()
            .contains(NSWindowOcclusionState::Visible),
        window.alphaValue(),
        window_server_onscreen(number).map_or("unlisted".into(), |on| on.to_string()),
        window
            .screen()
            .map_or_else(String::new, |s| s.localizedName().to_string()),
        show_rect(rect(window.frame())),
        window.hidesOnDeactivate(),
        window.level(),
        window.collectionBehavior(),
        application.isActive(),
        application.isHidden(),
    ));
}
/// Right after `show`, and again a runloop turn later: occlusion and the
/// window list settle asynchronously, so the `+100ms` line is the verdict.
fn log_visibility_now_and_later(app: &tauri::AppHandle, mtm: MainThreadMarker) {
    log_visibility(app, mtm, "shown");
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
        app::main_thread(&app, |app, mtm| log_visibility(&app, mtm, "+100ms"));
    });
}

/// A non-click open: a program, the first launch or the self-test.
pub fn show(app: &tauri::AppHandle, mtm: MainThreadMarker) {
    show_from(app, mtm, Now::read(app, mtm), Open::Other, "none");
}
/// Every frame change of the popover happens here or in `resize`, on the main
/// thread, synchronously. Nothing moves it later: no Tauri `set_position`
/// (tao dispatches it to the main queue, after this returns, in physical
/// pixels of whatever display the window was last on).
fn show_from(app: &tauri::AppHandle, mtm: MainThreadMarker, now: Now, open: Open, source: &str) {
    let Ok(panel) = app.get_webview_panel("popover") else {
        return;
    };
    let ns_panel = panel.as_panel();
    let placed = placement::anchor(
        &now.geometry,
        open,
        pointer(mtm),
        now.item_frame(),
        now.thickness,
    );
    if let Some(anchor) = placed {
        ANCHOR.set(Some(anchor));
        let placed = placement::fit(&anchor, ns_panel.frame().size.width, CONTENT_HEIGHT.get());
        set_frame(ns_panel, ns_rect(placed), None);
        // NonactivatingPanel can become key for keyboard navigation without activating the app.
        // tauri-nspanel's show_and_make_key: orderFrontRegardless, then makeKeyWindow.
        panel.show_and_make_key();
        app::trace(&format!(
            "popover placed reason={} click-source={source} screen={:?} anchor=({:.1},{:.1}) top={:.1} menu-bar={:.1} thickness={:.1} status-item={} frame={}",
            anchor.reason.as_str(),
            now.name(anchor.screen),
            anchor.point.x,
            anchor.point.y,
            anchor.top,
            anchor.menu_bar,
            now.thickness,
            now.item_frame().map_or("none".into(), show_rect),
            show_rect(rect(ns_panel.frame())),
        ));
    } else {
        panel.show_and_make_key();
        app::trace("popover placed reason=no-screen; frame unchanged");
    }
    app::trace("popover NSPanel shown; webview retained; application not activated");
    log_visibility_now_and_later(app, mtm);
    app::request_usage(app, true, "open");
}

/// Called only through the main-thread dispatcher. AppKit owns the animation,
/// using the same duration and cubic control points the page reads from CSS.
/// The display and centre stay where `show` put them; that display's menu bar,
/// Dock and frame are read again, since they can change while it is open.
pub fn resize(
    app: &tauri::AppHandle,
    mtm: MainThreadMarker,
    height: f64,
    duration_ms: f64,
    curve: [f32; 4],
) {
    CONTENT_HEIGHT.set(height);
    let Ok(panel) = app.get_webview_panel("popover") else {
        return;
    };
    let now = Now::read(app, mtm);
    // A popover not shown yet, or a display that went away since: placed as a
    // non-click open would, and kept, so the display's return moves nothing.
    let Some(anchor) = placement::reanchor(
        ANCHOR.get().as_ref(),
        &now.geometry,
        pointer(mtm),
        now.item_frame(),
        now.thickness,
    ) else {
        return;
    };
    ANCHOR.set(Some(anchor));
    let placed = placement::fit(&anchor, panel.as_panel().frame().size.width, height);
    let animate = duration_ms > 0.0 && panel.is_visible();
    set_frame(
        panel.as_panel(),
        ns_rect(placed),
        animate.then_some((duration_ms / 1000.0, curve)),
    );
    app::trace(&format!(
        "popover resized-to-content requested={height:.0} height={:.0} cap={:.0} top={:.0} mode={} reason={} screen={:?}",
        placed.height,
        placement::popover_cap(anchor.work),
        placed.y + placed.height,
        if animate { "animate" } else { "snap" },
        anchor.reason.as_str(),
        now.name(anchor.screen),
    ));
}
/// A left click on the status item. `event` is tray-icon's reading of it,
/// turned back into points with tray-icon's own flip height and the status
/// item window's scale; when that window no longer matches the event, the
/// pointer now.
pub fn toggle(app: &tauri::AppHandle, mtm: MainThreadMarker, event: Option<placement::TrayEvent>) {
    if app
        .get_webview_panel("popover")
        .is_ok_and(|p| p.is_visible())
    {
        hide(app, mtm, "toggle");
        return;
    }
    let now = Now::read(app, mtm);
    let (click, source) = placement::click(event, now.item, tray_flip(), pointer(mtm));
    show_from(app, mtm, now, Open::Click(click), source);
}
pub fn hide(app: &tauri::AppHandle, _mtm: MainThreadMarker, reason: &str) {
    if let Ok(panel) = app.get_webview_panel("popover") {
        panel.hide();
        app::trace(&format!(
            "popover NSPanel hidden reason={reason}; never closed; webview retained"
        ));
    }
}

/// One adapter for the image/template flag, attributed title, tooltip and VoiceOver label.
pub fn update_tray(
    app: &tauri::AppHandle,
    _mtm: MainThreadMarker,
    snapshot: &app::PopoverSnapshot,
) {
    let now = app::now();
    let input = if let Some(error) = &snapshot.error {
        Input::Unreadable(error)
    } else if let Some(usage) = &snapshot.usage {
        Input::Snapshot(usage)
    } else {
        Input::Reading
    };
    let glyph = glyph::choose(input, now, |timestamp, reset| {
        let formatter = NSDateFormatter::new();
        let date = NSDate::dateWithTimeIntervalSince1970(timestamp);
        // NSDateFormatter uses the person's current locale/timezone. A calendar day, not 24h.
        let today = NSDate::dateWithTimeIntervalSince1970(now);
        formatter.setDateFormat(Some(&NSString::from_str("yyyy-MM-dd")));
        let same_day = formatter.stringFromDate(&date) == formatter.stringFromDate(&today);
        formatter.setDateFormat(Some(&NSString::from_str(if reset && !same_day {
            "EEE"
        } else {
            "HH:mm"
        })));
        formatter.stringFromDate(&date).to_string()
    });
    let mut glyph = glyph;
    if crate::app_paths::is_test_build(&app.config().identifier) {
        // The tooltip doubles as the VoiceOver label.
        glyph.tooltip.push_str("\nTest build");
    }
    if !SHOWN_GLYPH.with(|shown| glyph::changed(shown.borrow().as_ref(), &glyph)) {
        return;
    }
    match apply_glyph(app, glyph.clone()) {
        Ok(true) => SHOWN_GLYPH.with(|shown| *shown.borrow_mut() = Some(glyph)),
        Ok(false) => (),
        Err(error) => app::log(&format!("tray update failed: {error}")),
    }
}
/// Returns whether the status button was there to update.
fn apply_glyph(app: &tauri::AppHandle, glyph: Glyph) -> tauri::Result<bool> {
    let Some(tray) = app.tray_by_id(app::TRAY_ID) else {
        return Ok(false);
    };
    tray.with_inner_tray_icon(move |tray| {
        let mtm = MainThreadMarker::new().expect("native tray adapter on main thread");
        let Some(button) = tray.ns_status_item().and_then(|item| item.button(mtm)) else {
            return false;
        };
        let bytes: &[u8] = match glyph.variant {
            Variant::Normal => include_bytes!("../icons/tray-normal@2x.png"),
            Variant::Badge => include_bytes!("../icons/tray-badge@2x.png"),
            Variant::Slash => include_bytes!("../icons/tray-slash@2x.png"),
        };
        let data = NSData::with_bytes(bytes);
        let image = NSImage::initWithData(NSImage::alloc(), &data).expect("bundled tray PNG");
        image.setSize(NSSize::new(18.0, 18.0));
        image.setTemplate(true);
        button.setImage(Some(&image));
        let font = NSFont::monospacedDigitSystemFontOfSize_weight(13.0, 0.0);
        unsafe {
            let attrs = NSDictionary::from_slices(
                &[objc2_app_kit::NSFontAttributeName],
                &[&*font as &AnyObject],
            );
            let title = NSAttributedString::initWithString_attributes(
                NSAttributedString::alloc(),
                &NSString::from_str(&glyph.title),
                Some(&attrs),
            );
            button.setAttributedTitle(&title);
        }
        // Clicks reach tray-icon only through its TaoTrayTarget, a subview of the
        // button sized at creation (16x22: no image, no title). The image and title
        // above grow the button behind tray-icon's back, so a click outside that
        // subview lands on the bare button, which has no action. tray-icon's own
        // setters resize the subview to the button (update_dimensions); set_tooltip
        // is the one that changes nothing else on screen (set_title drops the
        // monospaced digits, set_icon the template image). Call it after every
        // change to the image or title.
        if let Err(error) = tray.set_tooltip(Some(&glyph.tooltip)) {
            app::log(&format!("tray tooltip failed: {error}"));
        }
        let label = NSString::from_str(&glyph.tooltip);
        button.setAccessibilityLabel(Some(&label));
        let accessibility = button.accessibilityLabel().map(|s| s.to_string());
        debug_assert_eq!(accessibility.as_deref(), Some(glyph.tooltip.as_str()));
        let size = |frame: NSRect| format!("{:.0}x{:.0}", frame.size.width, frame.size.height);
        let target = button
            .subviews()
            .iter()
            .find(|view| view.class().name().to_bytes() == b"TaoTrayTarget")
            .map(|view| view.frame());
        app::trace(&format!(
            "tray template={} variant={:?} attributed-title={:?} accessibility-label={:?} button={} click-target={} click-target-fits={}",
            image.isTemplate(),
            glyph.variant,
            glyph.title,
            accessibility,
            size(button.bounds()),
            target.map_or("missing".into(), size),
            target == Some(button.bounds())
        ));
        true
    })
}

/// Retain the webview across close/reopen and bring Settings ahead of other apps.
pub fn open_settings(app: &tauri::AppHandle, mtm: MainThreadMarker, target: serde_json::Value) {
    use tauri::{Emitter, Manager};
    hide(app, mtm, "settings");
    *app.state::<app::AppState>()
        .settings_target
        .lock()
        .expect("settings target") = target.clone();
    let _ = app.set_activation_policy(tauri::ActivationPolicy::Regular);
    match app::settings_menu(app).and_then(|menu| app.set_menu(menu)) {
        Ok(_) => (),
        Err(error) => app::log(&format!("Settings menu failed: {error}")),
    }
    if let Some(window) = app.get_webview_window("settings") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
        let _ = app.show();
        let _ = app.emit_to("settings", "settings-target", target);
    }
    app::trace("Settings shown activation=Regular; webview retained; target delivered");
}

/// Launch Services resolves the default editor. Send a line position without
/// AppleScript or Automation permission; an unsuccessful open reveals in Finder.
pub fn open_settings_file(
    _mtm: MainThreadMarker,
    path: std::path::PathBuf,
    line: u64,
    reply: tokio::sync::oneshot::Sender<Result<(), String>>,
) {
    use objc2::{msg_send, ClassType};
    use objc2_app_kit::{NSRunningApplication, NSWorkspaceOpenConfiguration};
    use objc2_foundation::{NSAppleEventDescriptor, NSArray, NSBundle, NSError, NSURL};
    let workspace = NSWorkspace::sharedWorkspace();
    let file = NSURL::fileURLWithPath(&NSString::from_str(&path.to_string_lossy()));
    let Some(editor) = workspace.URLForApplicationToOpenURL(&file) else {
        workspace.activateFileViewerSelectingURLs(&NSArray::from_slice(&[&*file]));
        let _ = reply.send(Ok(()));
        return;
    };
    let bundle_id = NSBundle::bundleWithURL(&editor)
        .and_then(|b| b.bundleIdentifier())
        .map(|id| id.to_string());
    // VS Code's documented URL protocol preserves line selection in an already
    // running instance. Other editors receive the conventional kpos open event.
    let protocol = match bundle_id.as_deref() {
        Some("com.microsoft.VSCode") => Some("vscode"),
        Some("com.microsoft.VSCodeInsiders") => Some("vscode-insiders"),
        _ => None,
    };
    let url = protocol
        .and_then(|scheme| {
            let encoded = tauri::Url::from_file_path(&path).ok()?;
            NSURL::URLWithString(&NSString::from_str(&format!(
                "{scheme}://file{}:{line}:1",
                encoded.as_str().strip_prefix("file://")?
            )))
        })
        .unwrap_or_else(|| file.clone());
    let config = NSWorkspaceOpenConfiguration::new();
    if protocol.is_none() {
        let fourcc = |s: &[u8; 4]| u32::from_be_bytes(*s);
        // The bindings gate these stable Foundation methods behind an optional
        // CoreServices feature. Keep the existing locked dependency graph and
        // use their exact Objective-C signatures here instead.
        let event: Retained<NSAppleEventDescriptor> = unsafe {
            msg_send![NSAppleEventDescriptor::class(),
            appleEventWithEventClass: fourcc(b"aevt"), eventID: fourcc(b"odoc"),
            targetDescriptor: None::<&NSAppleEventDescriptor>, returnID: -1_i16, transactionID: 0_i32]
        };
        let files = NSAppleEventDescriptor::listDescriptor();
        files.insertDescriptor_atIndex(&NSAppleEventDescriptor::descriptorWithFileURL(&file), 1);
        unsafe {
            let _: () =
                msg_send![&*event, setParamDescriptor: &*files, forKeyword: fourcc(b"----")];
        }
        // Metrowerks/Xcode SelectionRange: two int16 fields, four int32 fields.
        // Explicit bytes avoid alignment/padding and native `long` size changes.
        let mut position = Vec::with_capacity(20);
        position.extend(0_i16.to_ne_bytes());
        position.extend(((line.saturating_sub(1)).min(i16::MAX as u64) as i16).to_ne_bytes());
        for value in [0_i32, 0, 0, 0] {
            position.extend(value.to_ne_bytes());
        }
        // SAFETY: Foundation copies the 20 initialized bytes during this call.
        let selection: Option<Retained<NSAppleEventDescriptor>> = unsafe {
            msg_send![NSAppleEventDescriptor::class(),
            descriptorWithDescriptorType: fourcc(b"TEXT"), bytes: position.as_ptr().cast::<std::ffi::c_void>(), length: position.len()]
        };
        if let Some(selection) = selection {
            unsafe {
                let _: () = msg_send![&*event, setParamDescriptor: &*selection, forKeyword: fourcc(b"kpos")];
            }
        }
        config.setAppleEvent(Some(&event));
    }
    let reply = std::sync::Mutex::new(Some(reply));
    let completion = block2::RcBlock::new(
        move |_application: *mut NSRunningApplication, error: *mut NSError| {
            if !error.is_null() {
                NSWorkspace::sharedWorkspace()
                    .activateFileViewerSelectingURLs(&NSArray::from_slice(&[&*file]));
            }
            if let Some(reply) = reply.lock().expect("editor reply").take() {
                let _ = reply.send(Ok(()));
            }
        },
    );
    workspace.openURLs_withApplicationAtURL_configuration_completionHandler(
        &NSArray::from_slice(&[&*url]),
        &editor,
        &config,
        Some(&completion),
    );
}

/// A native menu callback has no browser user-activation token.
pub fn copy_text(_mtm: MainThreadMarker, text: &str) -> Result<(), String> {
    let board = objc2_app_kit::NSPasteboard::generalPasteboard();
    board.clearContents();
    if board.setString_forType(&NSString::from_str(text), unsafe {
        objc2_app_kit::NSPasteboardTypeString
    }) {
        Ok(())
    } else {
        Err("Could not copy. Try Copy again.".into())
    }
}
