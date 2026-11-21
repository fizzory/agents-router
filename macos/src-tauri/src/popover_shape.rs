//! Native popover shape and shadow policy, independent of AppKit's run loop.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Shape {
    pub corner_radius: f64,
    pub continuous: bool,
    pub masks_to_bounds: bool,
    pub has_shadow: bool,
}

pub const SHAPE: Shape = Shape {
    corner_radius: 16.0,
    continuous: true,
    masks_to_bounds: true,
    has_shadow: true,
};

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Resize {
    Animated,
    Snap,
}
#[derive(Clone, Copy, Debug)]
pub enum ShadowUpdate {
    AnimationCompleted,
    SnapCompleted,
}
impl Resize {
    // Every frame notification refreshes the shadow, in either mode.
    pub const SHADOW_ON_RESIZE: bool = true;
    pub fn choose(visible: bool, reduce_motion: bool, duration: f64) -> Self {
        if visible && !reduce_motion && duration > 0.0 {
            Self::Animated
        } else {
            Self::Snap
        }
    }

    pub fn invalidate_shadow(self, update: ShadowUpdate) -> bool {
        match update {
            ShadowUpdate::AnimationCompleted => self == Self::Animated,
            ShadowUpdate::SnapCompleted => self == Self::Snap,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rounded_content_clips_continuously_and_keeps_the_window_shadow() {
        assert_eq!(SHAPE.corner_radius, 16.0);
        assert!(SHAPE.continuous);
        assert!(SHAPE.masks_to_bounds);
        assert!(SHAPE.has_shadow);
    }

    #[test]
    fn animated_resize_updates_the_shadow_on_frames_and_completion() {
        let resize = Resize::choose(true, false, 1.0);
        assert_eq!(resize, Resize::Animated);
        assert!(Resize::SHADOW_ON_RESIZE);
        assert!(resize.invalidate_shadow(ShadowUpdate::AnimationCompleted));
        assert!(!resize.invalidate_shadow(ShadowUpdate::SnapCompleted));
    }

    #[test]
    fn reduce_motion_hidden_and_zero_duration_resizes_snap_and_update_the_shadow() {
        for (visible, reduce_motion, duration) in
            [(true, true, 1.0), (false, false, 1.0), (true, false, 0.0)]
        {
            let resize = Resize::choose(visible, reduce_motion, duration);
            assert_eq!(resize, Resize::Snap);
            assert!(Resize::SHADOW_ON_RESIZE);
            assert!(resize.invalidate_shadow(ShadowUpdate::SnapCompleted));
            assert!(!resize.invalidate_shadow(ShadowUpdate::AnimationCompleted));
        }
    }
}
