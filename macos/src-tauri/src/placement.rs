//! Placement in AppKit coordinates (points, bottom-left origin).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Rect {
    pub x: f64,
    pub y: f64,
    pub width: f64,
    pub height: f64,
}
impl Rect {
    pub fn contains(self, x: f64, y: f64) -> bool {
        x >= self.x && y >= self.y && x < self.x + self.width && y < self.y + self.height
    }
    pub fn overlaps(self, other: Self) -> bool {
        self.x < other.x + other.width
            && self.x + self.width > other.x
            && self.y < other.y + other.height
            && self.y + self.height > other.y
    }
}
pub fn place(frame: Rect, work: Rect, fallback: bool) -> Rect {
    let width = frame.width.min(work.width);
    let height = frame.height.min(work.height);
    Rect {
        x: if fallback {
            work.x + work.width - width
        } else {
            frame.x.clamp(work.x, work.x + work.width - width)
        },
        y: if fallback {
            work.y + work.height - height
        } else {
            frame.y.clamp(work.y, work.y + work.height - height)
        },
        width,
        height,
    }
}

pub const MIN_POPOVER_HEIGHT: f64 = 80.0;
pub fn popover_cap(work: Rect) -> f64 {
    (work.height - 24.0).clamp(0.0, 560.0)
}

/// Growing downward keeps the AppKit top edge (y + height) under the tray.
/// A removed/off-screen anchor uses the pointer display's top-right corner.
pub fn resize(frame: Rect, work: Rect, content_height: f64, fallback: bool) -> Rect {
    let cap = popover_cap(work);
    let requested = if content_height.is_finite() {
        content_height
    } else {
        MIN_POPOVER_HEIGHT
    };
    let height = requested.clamp(MIN_POPOVER_HEIGHT.min(cap), cap);
    let width = frame.width.min(work.width);
    let top = if fallback {
        work.y + work.height
    } else {
        (frame.y + frame.height).clamp(work.y + height, work.y + work.height)
    };
    Rect {
        x: if fallback {
            work.x + work.width - width
        } else {
            frame.x.clamp(work.x, work.x + work.width - width)
        },
        y: top - height,
        width,
        height,
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    const WORK: Rect = Rect {
        x: -1200.0,
        y: 40.0,
        width: 1200.0,
        height: 760.0,
    };
    const FRAME: Rect = Rect {
        x: -500.0,
        y: 400.0,
        width: 360.0,
        height: 400.0,
    };
    #[test]
    fn content_height_has_a_one_row_floor_and_a_560_point_cap() {
        for request in [-20.0, 0.0, 32.0, f64::NAN, f64::INFINITY] {
            assert_eq!(resize(FRAME, WORK, request, false).height, 80.0);
        }
        assert_eq!(resize(FRAME, WORK, 224.0, false).height, 224.0);
        assert_eq!(resize(FRAME, WORK, 2000.0, false).height, 560.0);
    }
    #[test]
    fn work_area_cap_leaves_24_points_even_on_a_short_display() {
        let work = Rect {
            height: 400.0,
            ..WORK
        };
        assert_eq!(resize(FRAME, work, 900.0, false).height, 376.0);
        // An unusually small work area takes priority over the normal floor.
        assert_eq!(
            resize(
                FRAME,
                Rect {
                    height: 64.0,
                    ..WORK
                },
                0.0,
                false
            )
            .height,
            40.0
        );
    }
    #[test]
    fn top_edge_is_fixed_while_growing_and_shrinking() {
        for request in [80.0, 160.0, 224.0, 560.0, 1200.0] {
            let next = resize(FRAME, WORK, request, false);
            assert_eq!(next.y + next.height, FRAME.y + FRAME.height);
            assert_eq!(next.x, FRAME.x);
        }
    }
    #[test]
    fn resize_fallback_is_top_right_and_clamped_to_the_pointer_work_area() {
        let next = resize(
            Rect {
                x: 3000.0,
                y: -4000.0,
                ..FRAME
            },
            WORK,
            160.0,
            true,
        );
        assert_eq!((next.x, next.y, next.height), (-360.0, 640.0, 160.0));
    }
    #[test]
    fn work_area_clamp_and_pointer_display_fallback() {
        let work = Rect {
            x: -1200.0,
            y: 40.0,
            width: 1200.0,
            height: 760.0,
        };
        let frame = Rect {
            x: -1300.0,
            y: 900.0,
            width: 360.0,
            height: 400.0,
        };
        assert_eq!(
            place(frame, work, false),
            Rect {
                x: -1200.0,
                y: 400.0,
                width: 360.0,
                height: 400.0
            }
        );
        assert_eq!(
            place(frame, work, true),
            Rect {
                x: -360.0,
                y: 400.0,
                width: 360.0,
                height: 400.0
            }
        );
        assert_eq!(
            place(
                Rect {
                    width: 2000.0,
                    height: 2000.0,
                    ..frame
                },
                work,
                true
            ),
            work
        );
    }
}
