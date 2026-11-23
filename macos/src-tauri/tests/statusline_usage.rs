use agents_router_app::{
    app::PopoverSnapshot,
    models::{decode, CommandKind, Snapshot},
};
use serde_json::{json, Value};

fn page_snapshot(account: &str) -> Value {
    let producer = json!({
        "schema_version": 1, "agt_version": "0.20.0",
        "install": { "source": "app", "version": "0.20.0" },
        "generated_at": 1800000000.0,
        "providers": [{ "runtime": "claude", "label": "Claude",
            "accounts": [serde_json::from_str::<Value>(account).unwrap()] }],
        "jobs": { "active": 0, "queued": 0, "starting": 0, "running": 0,
            "unassigned": 0, "removed_accounts": 0, "surviving_workers": 0 },
        "uninstall_blockers": []
    });
    let Snapshot::Usage(usage) =
        decode(CommandKind::Usage, &serde_json::to_vec(&producer).unwrap()).unwrap()
    else {
        panic!("expected usage");
    };
    serde_json::to_value(PopoverSnapshot {
        usage: Some(usage),
        ..Default::default()
    })
    .unwrap()
}

#[test]
fn statusline_usage_survives_producer_to_page_snapshot_including_future_values() {
    let account = r#"{
        "name": "claude-ntb", "enabled": true, "availability": "ok",
        "reason": null, "until": null, "running_jobs": 0, "busy_slots": 0,
        "capacity": null, "unhealthy_models": [], "windows": [], "problem": null,
        "statusline_usage": "missing"
    }"#;
    for state in [
        "live",
        "missing",
        "replaced",
        "off",
        "unset",
        "unsupported",
        "no_limits",
        "future-state",
    ] {
        let page = page_snapshot(&account.replace("missing", state));
        assert_eq!(
            page["usage"]["providers"][0]["accounts"][0]["statusline_usage"],
            state
        );
    }
}

#[test]
fn older_usage_without_statusline_usage_still_decodes_for_the_page() {
    let page = page_snapshot(
        r#"{
        "name": "claude-ntb", "enabled": true, "availability": "ok",
        "reason": null, "until": null, "running_jobs": 0, "busy_slots": 0,
        "capacity": null, "unhealthy_models": [], "windows": [], "problem": null
    }"#,
    );
    let account = &page["usage"]["providers"][0]["accounts"][0];
    assert_eq!(account["name"], "claude-ntb");
    assert!(account.get("statusline_usage").is_none_or(Value::is_null));
}

#[test]
fn window_source_survives_producer_to_page_snapshot_including_future_values() {
    let mut account = json!({
        "name": "claude-ntb", "enabled": true, "availability": "ok",
        "reason": null, "until": null, "running_jobs": 0, "busy_slots": 0,
        "capacity": null, "unhealthy_models": [], "problem": null,
        "windows": [{"name": "five_hour", "label": "5h", "used_percent": 17,
            "resets_at": 1800003600.0, "observed_at": 1799999940.0,
            "stale": false, "reset": false}]
    });
    for source in [Some("terminal"), Some("job"), Some("future-source"), None] {
        account["windows"][0]["source"] = json!(source);
        let page = page_snapshot(&account.to_string());
        let window = &page["usage"]["providers"][0]["accounts"][0]["windows"][0];
        assert_eq!(window["observed_at"], 1799999940.0);
        if let Some(source) = source {
            assert_eq!(window["source"], source);
        } else {
            assert!(window.get("source").is_none_or(Value::is_null));
        }
    }
    account["windows"][0].as_object_mut().unwrap().remove("source");
    let page = page_snapshot(&account.to_string());
    let window = &page["usage"]["providers"][0]["accounts"][0]["windows"][0];
    assert_eq!(window["used_percent"], 17.0);
    assert!(window.get("source").is_none_or(Value::is_null));
}
