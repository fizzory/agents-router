//! Pure tray policy. The caller supplies local calendar formatting, never AppKit here.
use crate::models::{Availability, Usage};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Variant {
    Normal,
    Badge,
    Slash,
}
#[derive(Debug, PartialEq, Eq)]
pub struct Glyph {
    pub variant: Variant,
    pub title: String,
    /// Also used verbatim as the status button's accessibility label.
    pub tooltip: String,
}
pub enum Input<'a> {
    Reading,
    Unreadable(&'a str),
    Snapshot(&'a Usage),
}

pub fn choose(input: Input<'_>, now: f64, format: impl Fn(f64, bool) -> String) -> Glyph {
    let usage = match input {
        Input::Reading => {
            return Glyph {
                variant: Variant::Normal,
                title: String::new(),
                tooltip: "Reading agt…".into(),
            }
        }
        Input::Unreadable(error) => {
            return Glyph {
                variant: Variant::Slash,
                title: String::new(),
                tooltip: error.into(),
            }
        }
        Input::Snapshot(usage) => usage,
    };
    let accounts: Vec<_> = usage
        .providers
        .iter()
        .flat_map(|p| p.accounts.iter().filter(|a| a.enabled).map(move |a| (p, a)))
        .collect();
    let usable = accounts
        .iter()
        .any(|(_, a)| matches!(a.availability, Availability::Ok | Availability::Recovering));
    // Busy and unknown never add a badge. If *all* accounts are blocked, the whole mark is slashed.
    let worst = accounts
        .iter()
        .filter(|(_, a)| {
            matches!(
                a.availability,
                Availability::LoggedOut | Availability::Unhealthy | Availability::Cooling
            )
        })
        .min_by_key(|(_, a)| match a.availability {
            Availability::LoggedOut => 0,
            Availability::Unhealthy => 1,
            _ => 2,
        });
    let mut notes = Vec::new();
    if let Some((p, a)) = worst {
        let state = match a.availability {
            Availability::LoggedOut => "logged out",
            Availability::Unhealthy => "unhealthy",
            _ => "cooling",
        };
        let back = a
            .until
            .filter(|t| t.is_finite() && *t > now)
            .map(|t| format!(", back {}", format(t, false)))
            .unwrap_or_default();
        notes.push(format!("{} {}, {state}{back}", p.label, a.name));
    } else if usable {
        notes.push("All available".into());
    } else {
        notes.push("Nothing usable".into());
    }
    let best = accounts
        .iter()
        .flat_map(|(p, a)| a.windows.iter().map(move |w| (*p, *a, w)))
        .filter(|(_, _, w)| {
            !w.stale
                && !w.reset
                && w.used_percent.is_finite()
                && w.observed_at.is_some_and(|t| t.is_finite())
                && !w.resets_at.is_some_and(|t| t <= now)
        })
        .max_by(|a, b| a.2.used_percent.total_cmp(&b.2.used_percent));
    if let Some((p, a, w)) = best {
        let reset = w
            .resets_at
            .filter(|t| t.is_finite())
            .map(|t| format!(", resets {}", format(t, true)))
            .unwrap_or_default();
        notes.insert(
            0,
            format!(
                "{} {}, {} {:.0}%{reset}",
                p.label,
                a.name,
                w.label,
                w.used_percent.clamp(0.0, 100.0)
            ),
        );
    } else {
        let newest = accounts
            .iter()
            .flat_map(|(_, a)| &a.windows)
            .filter_map(|w| w.observed_at)
            .filter(|t| t.is_finite())
            .max_by(f64::total_cmp);
        let age = newest
            .map(|t| format!(", {}m old", ((now - t).max(0.0) / 60.0).floor() as u64))
            .unwrap_or_default();
        notes.insert(0, format!("No fresh usage{age}"));
    }
    let title = if !usable {
        accounts
            .iter()
            .filter_map(|(_, a)| a.until)
            .filter(|t| t.is_finite() && *t > now)
            .min_by(f64::total_cmp)
            .map(|t| format(t, false))
            .unwrap_or_default()
    } else {
        best.map(|(_, _, w)| {
            let digits = format!("{:.0}", w.used_percent.clamp(0.0, 100.0));
            format!(
                "{}{}%",
                "\u{2007}".repeat(3usize.saturating_sub(digits.len())),
                digits
            )
        })
        .unwrap_or_default()
    };
    if !usable && !title.is_empty() {
        notes.push(format!("Nothing usable, back {title}"));
    }
    Glyph {
        variant: if !usable {
            Variant::Slash
        } else if worst.is_some() {
            Variant::Badge
        } else {
            Variant::Normal
        },
        title,
        tooltip: notes.join("\n"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn fixture() -> Usage {
        serde_json::from_slice(include_bytes!("../../../tests/fixtures/app/usage.json")).unwrap()
    }
    fn single() -> Usage {
        let mut u = fixture();
        u.providers.truncate(1);
        u.providers[0].accounts.truncate(1);
        u
    }
    fn glyph(u: &Usage) -> Glyph {
        choose(Input::Snapshot(u), u.generated_at, |t, reset| {
            if reset {
                "Wed".into()
            } else if t < u.generated_at + 200.0 {
                "20:15".into()
            } else {
                "21:40".into()
            }
        })
    }
    #[test]
    fn highest_fresh_percent_padding_and_tooltip() {
        let mut u = single();
        u.providers[0].accounts[0].windows.truncate(1);
        let mut second = u.providers[0].accounts[0].windows[0].clone();
        second.label = "week".into();
        second.used_percent = 86.0;
        second.stale = false;
        second.reset = false;
        second.resets_at = Some(u.generated_at + 1000.0);
        u.providers[0].accounts[0].windows.push(second);
        let g = glyph(&u);
        assert_eq!(g.title, "\u{2007}86%");
        assert_eq!(g.variant, Variant::Normal);
        for text in [
            "Claude",
            "claude-work",
            "week 86%",
            "resets Wed",
            "All available",
        ] {
            assert!(g.tooltip.contains(text), "{}", g.tooltip);
        }
        u.providers[0].accounts[0].windows[1].used_percent = 7.0;
        u.providers[0].accounts[0].windows[0].used_percent = 0.0;
        assert_eq!(glyph(&u).title, "\u{2007}\u{2007}7%");
        u.providers[0].accounts[0].windows[1].used_percent = 100.0;
        assert_eq!(glyph(&u).title, "100%");
    }
    #[test]
    fn marks_only_enabled_blocked_accounts_and_names_worst() {
        for state in [
            Availability::LoggedOut,
            Availability::Unhealthy,
            Availability::Cooling,
            Availability::Off,
            Availability::Busy,
            Availability::Unknown,
            Availability::Recovering,
        ] {
            let mut u = single();
            let mut a = u.providers[0].accounts[0].clone();
            a.name = "worst".into();
            a.availability = state.clone();
            a.until = Some(u.generated_at + 400.0);
            u.providers[0].accounts.push(a);
            let marked = matches!(
                state,
                Availability::LoggedOut | Availability::Unhealthy | Availability::Cooling
            );
            assert_eq!(
                glyph(&u).variant,
                if marked {
                    Variant::Badge
                } else {
                    Variant::Normal
                }
            );
            if marked {
                assert!(glyph(&u).tooltip.contains("worst"));
                assert!(glyph(&u).tooltip.contains("back 21:40"));
            }
            u.providers[0].accounts[1].enabled = false;
            assert_eq!(glyph(&u).variant, Variant::Normal);
        }
    }
    #[test]
    fn highest_is_across_providers_and_ignores_stale_and_disabled_figures() {
        let mut u = single();
        let mut p = u.providers[0].clone();
        p.label = "Codex".into();
        p.runtime = "codex".into();
        p.accounts[0].name = "codex-work".into();
        p.accounts[0].windows[0].used_percent = 86.0;
        p.accounts[0].windows[1].used_percent = 99.0;
        p.accounts[0].windows[1].stale = true;
        u.providers.push(p);
        assert_eq!(glyph(&u).title, "\u{2007}86%");
        assert!(glyph(&u).tooltip.starts_with("Codex codex-work, 5h 86%"));
        u.providers[1].accounts[0].enabled = false;
        assert_eq!(glyph(&u).title, "\u{2007}42%");
    }
    #[test]
    fn slash_earliest_back_and_unreadable_without_title() {
        let mut u = single();
        u.providers[0].accounts[0].availability = Availability::Cooling;
        u.providers[0].accounts[0].until = Some(u.generated_at + 500.0);
        let mut a = u.providers[0].accounts[0].clone();
        a.until = Some(u.generated_at + 100.0);
        u.providers[0].accounts.push(a);
        assert_eq!(glyph(&u).variant, Variant::Slash);
        assert_eq!(glyph(&u).title, "20:15");
        assert!(glyph(&u).tooltip.contains("back 20:15"));
        for a in &mut u.providers[0].accounts {
            a.until = None;
        }
        assert_eq!(glyph(&u).title, "");
        let g = choose(
            Input::Unreadable("agt not found"),
            0.0,
            |_, _| unreachable!(),
        );
        assert_eq!(g.variant, Variant::Slash);
        assert_eq!(g.title, "");
        assert_eq!(g.tooltip, "agt not found");
        let g = choose(Input::Reading, 0.0, |_, _| unreachable!());
        assert_eq!(g.variant, Variant::Normal);
        assert_eq!(g.tooltip, "Reading agt…");
    }
    #[test]
    fn stale_reset_expired_and_missing_observation_never_supply_a_percent() {
        for case in 0..4 {
            let mut u = single();
            u.providers[0].accounts[0].windows.truncate(1);
            let w = &mut u.providers[0].accounts[0].windows[0];
            match case {
                0 => w.stale = true,
                1 => w.reset = true,
                2 => w.resets_at = Some(u.generated_at - 1.0),
                _ => w.observed_at = None,
            }
            assert_eq!(glyph(&u).title, "");
            assert!(glyph(&u).tooltip.contains("No fresh usage"));
        }
        let mut u = single();
        u.providers[0].accounts[0].windows.clear();
        assert_eq!(glyph(&u).title, "");
    }
}
