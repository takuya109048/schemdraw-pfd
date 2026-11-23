//! Every case under ``tests/cases/invalid`` is refused with the error and
//! message the Python tests expect (``INVALID`` in ``tests/test_cases.py``).

use std::path::Path;

use pfd::Flowsheet;

#[test]
fn invalid_cases_are_refused_with_their_message() {
    let repo = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let source = std::fs::read_to_string(repo.join("tests/test_cases.py")).unwrap();
    let mut failed = Vec::new();
    let mut count = 0;
    for line in source.lines() {
        let line = line.trim();
        let Some(rest) = line.strip_prefix("\"invalid/") else {
            continue;
        };
        let (name, rest) = rest.split_once("\": (").unwrap();
        let (kind, msg) = rest.split_once(", \"").unwrap();
        let msg = msg.trim_end_matches("),").trim_end_matches('"');
        let text = std::fs::read_to_string(repo.join(format!("tests/cases/invalid/{name}.json"))).unwrap();
        count += 1;
        let result = Flowsheet::from_json(&text).and_then(|fs| fs.build(4, true));
        match result {
            Err(e) if e.kind() == kind && e.message().contains(msg) => eprintln!("ok   {name}"),
            Err(e) => {
                eprintln!("FAIL {name}: {e} (want {kind}: {msg})");
                failed.push(name.to_string());
            }
            Ok(_) => {
                eprintln!("FAIL {name}: built (want {kind}: {msg})");
                failed.push(name.to_string());
            }
        }
    }
    assert!(count > 30);
    assert!(failed.is_empty(), "{failed:?}");
}
