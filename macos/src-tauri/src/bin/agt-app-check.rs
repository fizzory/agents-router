use agents_router_app::models::check_fixtures;
use std::path::Path;

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let roundtrip = match args.as_slice() {
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
    if args.first().is_some_and(|arg| arg.starts_with("--")) {
        eprintln!("{}: not yet implemented", args[0]);
        std::process::exit(2);
    }
    if args.len() != 1 {
        eprintln!("usage: agt-app-check <fixture-folder>");
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
