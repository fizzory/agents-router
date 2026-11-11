use agents_router_app::models::{check_document, check_fixtures};
use std::{io::Read, path::Path};

fn stdin_document(kind: &str, mut input: impl Read) -> Result<(), String> {
    let mut bytes = Vec::new();
    input
        .read_to_end(&mut bytes)
        .map_err(|e| format!("stdin: {e}"))?;
    check_document(kind, &bytes)?;
    println!("OK: decoded {kind} from stdin (schema range 1…1)");
    Ok(())
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let roundtrip = match args.as_slice() {
        [flag] if flag == "--install-cli" => Some(agents_router_app::install_check::run()),
        [flag, app_flag, app] if flag == "--install-cli" && app_flag == "--app" => Some(
            tokio::runtime::Runtime::new()
                .unwrap()
                .block_on(agents_router_app::install_check::from_app(Path::new(app))),
        ),
        [flag, kind] if flag == "--stdin" => Some(stdin_document(kind, std::io::stdin().lock())),
        [flag, root] if flag == "--install-cli-isolated" => Some(
            tokio::runtime::Runtime::new()
                .unwrap()
                .block_on(agents_router_app::install_check::isolated(Path::new(root))),
        ),
        [flag] if flag == "--roundtrip" => Some(agents_router_app::roundtrip::run()),
        [flag, root] if flag == "--roundtrip-isolated" => Some(
            tokio::runtime::Runtime::new()
                .unwrap()
                .block_on(agents_router_app::roundtrip::isolated(Path::new(root))),
        ),
        _ => None,
    };
    if let Some(result) = roundtrip {
        if let Err(error) = result {
            eprintln!("{error}");
            std::process::exit(1);
        }
        return;
    }
    if args.len() != 1 || args[0].starts_with("--") {
        eprintln!("usage: agt-app-check <fixture-folder>\n       agt-app-check --stdin <kind>\n       agt-app-check --install-cli [--app <path>]\n       agt-app-check --roundtrip");
        std::process::exit(2);
    }
    match check_fixtures(Path::new(&args[0])) {
        Ok(count) => println!("OK: decoded {count} agt fixtures (schema range 1…1)"),
        Err(error) => {
            eprintln!("{error}");
            std::process::exit(1);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stdin_usage_accepts_one_document() {
        let bytes = include_bytes!("../../../../tests/fixtures/app/usage.json");
        stdin_document("usage", bytes.as_slice()).unwrap();
        let mut two = bytes.to_vec();
        two.extend_from_slice(bytes);
        assert!(stdin_document("usage", two.as_slice())
            .unwrap_err()
            .contains("trailing characters"));
    }

    #[test]
    fn stdin_usage_reports_missing_and_nested_fields() {
        assert!(
            stdin_document("usage", b"{\"schema_version\":1}".as_slice())
                .unwrap_err()
                .contains("usage.json: invalid Usage JSON: agt_version:")
        );
        let mut value: serde_json::Value =
            serde_json::from_slice(include_bytes!("../../../../tests/fixtures/app/usage.json"))
                .unwrap();
        value["install"]["version"] = 1.into();
        assert!(
            stdin_document("usage", serde_json::to_vec(&value).unwrap().as_slice())
                .unwrap_err()
                .contains("install.version:")
        );
    }
}
