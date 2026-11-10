//! All AppKit access lives here and requires a main-thread marker.
use crate::{
    app,
    glyph::{self, Glyph, Input, Variant},
    placement::{self, Rect},
    watcher::WakeTrigger,
};
use objc2::{
    rc::Retained,
    runtime::{AnyObject, ProtocolObject},
    AnyThread, MainThreadMarker,
};
use objc2_app_kit::{
    NSAccessibility, NSAnimatablePropertyContainer, NSAnimationContext, NSEvent, NSFont, NSImage,
    NSScreen, NSWindowCollectionBehavior, NSWindowStyleMask, NSWorkspace,
    NSWorkspaceDidWakeNotification,
};
use objc2_foundation::{
    NSAttributedString, NSData, NSDate, NSDateFormatter, NSDictionary, NSNotification,
    NSNotificationCenter, NSObjectProtocol, NSPoint, NSRect, NSSize, NSString,
};
use objc2_quartz_core::CAMediaTimingFunction;
use std::{
    cell::{Cell, RefCell},
    ptr::NonNull,
};
use tauri::{tray::TrayIconEvent, Manager};
use tauri_nspanel::{ManagerExt, WebviewWindowExt};
use tauri_plugin_positioner::{Position, WindowExt};

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

pub fn observe_wake(_mtm: MainThreadMarker, wake: WakeTrigger) {
    unsafe {
        let center = NSWorkspace::sharedWorkspace().notificationCenter();
        let block = block2::RcBlock::new(move |_note: NonNull<NSNotification>| {
            app::log("wake received: queued on shared debounce");
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
    app::log("self-test posted NSWorkspaceDidWakeNotification");
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
    panel.set_level(3);
    panel.set_hides_on_deactivate(true);
    panel.set_released_when_closed(false);
    panel.set_collection_behavior(
        NSWindowCollectionBehavior::CanJoinAllSpaces
            | NSWindowCollectionBehavior::FullScreenAuxiliary,
    );
    panel.hide();
    app::log(&format!(
        "popover class={} NSPanel borderless=true transparent=true nonactivating=true hidden=true",
        panel.as_panel().class()
    ));
    #[cfg(debug_assertions)]
    app::log(&format!(
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
pub fn show(app: &tauri::AppHandle, mtm: MainThreadMarker) {
    let Some(window) = app.get_webview_window("popover") else {
        return;
    };
    let Ok(panel) = app.get_webview_panel("popover") else {
        return;
    };
    let mut positioned = false;
    let mut tray_frame = None;
    if let Some(tray) = app.tray_by_id(app::TRAY_ID) {
        // Native tray geometry avoids mixing logical AppKit points with Tauri's
        // physical, top-left coordinates on displays with different scale factors.
        tray_frame = tray
            .with_inner_tray_icon(|tray| {
                let mtm = MainThreadMarker::new().expect("tray geometry on main thread");
                tray.ns_status_item()
                    .and_then(|item| item.button(mtm))
                    .and_then(|button| button.window())
                    .map(|window| rect(window.frame()))
            })
            .ok()
            .flatten();
        if let Ok(Some(rect)) = tray.rect() {
            let event = TrayIconEvent::Enter {
                id: tray.id().clone(),
                position: tauri::PhysicalPosition::new(0.0, 0.0),
                rect,
            };
            tauri_plugin_positioner::on_tray_event(app, &event);
            positioned = window.move_window(Position::TrayCenter).is_ok();
        }
    }
    let frame = rect(panel.as_panel().frame());
    let screens = NSScreen::screens(mtm);
    let pointer = NSEvent::mouseLocation();
    let target = if positioned {
        screens.iter().find(|s| {
            tray_frame.is_some_and(|tray| {
                rect(s.frame()).contains(tray.x + tray.width / 2.0, tray.y + tray.height / 2.0)
            }) && frame.overlaps(rect(s.frame()))
        })
    } else {
        None
    };
    let fallback = target.is_none();
    if let Some(screen) = target
        .or_else(|| {
            screens
                .iter()
                .find(|s| rect(s.frame()).contains(pointer.x, pointer.y))
        })
        .or_else(|| screens.iter().next())
    {
        let placed = placement::resize(
            frame,
            rect(screen.visibleFrame()),
            CONTENT_HEIGHT.get(),
            fallback,
        );
        panel.as_panel().setFrame_display(
            NSRect::new(
                NSPoint::new(placed.x, placed.y),
                NSSize::new(placed.width, placed.height),
            ),
            false,
        );
        app::log(&format!(
            "popover placement={} clamped-to-work-area=true",
            if fallback {
                "pointer-display-top-right"
            } else {
                "TrayCenter"
            }
        ));
    }
    // NonactivatingPanel can become key for keyboard navigation without activating the app.
    panel.show_and_make_key();
    app::log("popover NSPanel shown; webview retained; application not activated");
    app::request_usage(app, true, "open");
}

/// Called only through the main-thread dispatcher. AppKit owns the animation,
/// using the same duration and cubic control points the page reads from CSS.
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
    let frame = rect(panel.as_panel().frame());
    let screens = NSScreen::screens(mtm);
    let pointer = NSEvent::mouseLocation();
    let anchored = screens.iter().find(|screen| {
        rect(screen.visibleFrame())
            .contains(frame.x + frame.width / 2.0, frame.y + frame.height - 1.0)
    });
    let fallback = anchored.is_none();
    let Some(screen) = anchored
        .or_else(|| {
            screens
                .iter()
                .find(|screen| rect(screen.frame()).contains(pointer.x, pointer.y))
        })
        .or_else(|| screens.iter().next())
    else {
        return;
    };
    let work = rect(screen.visibleFrame());
    let placed = placement::resize(frame, work, height, fallback);
    let next = NSRect::new(
        NSPoint::new(placed.x, placed.y),
        NSSize::new(placed.width, placed.height),
    );
    let animate = duration_ms > 0.0 && panel.is_visible();
    if animate {
        NSAnimationContext::beginGrouping();
        let context = NSAnimationContext::currentContext();
        context.setDuration(duration_ms / 1000.0);
        context.setTimingFunction(Some(&CAMediaTimingFunction::functionWithControlPoints(
            curve[0], curve[1], curve[2], curve[3],
        )));
        panel.as_panel().animator().setFrame_display(next, true);
        NSAnimationContext::endGrouping();
    } else {
        panel.as_panel().setFrame_display(next, false);
    }
    app::log(&format!(
        "popover resized-to-content requested={height:.0} height={:.0} cap={:.0} top={:.0} mode={}",
        placed.height,
        placement::popover_cap(work),
        placed.y + placed.height,
        if animate { "animate" } else { "snap" }
    ));
}
pub fn toggle(app: &tauri::AppHandle, mtm: MainThreadMarker) {
    if app
        .get_webview_panel("popover")
        .is_ok_and(|p| p.is_visible())
    {
        hide(app, mtm, "toggle");
    } else {
        show(app, mtm);
    }
}
pub fn hide(app: &tauri::AppHandle, _mtm: MainThreadMarker, reason: &str) {
    if let Ok(panel) = app.get_webview_panel("popover") {
        panel.hide();
        app::log(&format!(
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
    if let Err(error) = apply_glyph(app, glyph) {
        app::log(&format!("tray update failed: {error}"));
    }
}
fn apply_glyph(app: &tauri::AppHandle, glyph: Glyph) -> tauri::Result<()> {
    let Some(tray) = app.tray_by_id(app::TRAY_ID) else {
        return Ok(());
    };
    tray.with_inner_tray_icon(move |tray| {
        let mtm = MainThreadMarker::new().expect("native tray adapter on main thread");
        if let Some(button) = tray.ns_status_item().and_then(|item| item.button(mtm)) {
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
            let label = NSString::from_str(&glyph.tooltip);
            button.setToolTip(Some(&label));
            button.setAccessibilityLabel(Some(&label));
            let accessibility = button.accessibilityLabel().map(|s| s.to_string());
            debug_assert_eq!(accessibility.as_deref(), Some(glyph.tooltip.as_str()));
            app::log(&format!(
                "tray template={} variant={:?} attributed-title={:?} accessibility-label={:?}",
                image.isTemplate(),
                glyph.variant,
                glyph.title,
                accessibility
            ));
        }
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
    app::log("Settings shown activation=Regular; webview retained; target delivered");
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
