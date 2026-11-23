//! Command line: ``pfd spec.json -o out.svg``.

use std::process::ExitCode;

use pfd::elements::summary;
use pfd::{Flowsheet, render};

const USAGE: &str = "usage: pfd [-h] [-o OUTPUT] [--no-labels] [--no-frame] [--fontsize FONTSIZE]
           [--layout {order,flow}] [-j JOBS] [--revision ID]
           [--highlight {all,latest,none}] [--since ID] [--freeze PATH]
           [--snapshot PATH]
           [--list-elements] [--describe TYPE] [spec]

Generate a PFD from a JSON spec

positional arguments:
  spec                  JSON flowsheet specification

options:
  -h, --help            show this help message and exit
  -o, --output OUTPUT   svg output path (default: pfd.svg)
  --no-labels
  --no-frame            omit the sheet border
  --fontsize FONTSIZE   tag/spec font size (points, default 20)
  --layout {order,flow}
                        override the spec's layout: declared order, or from the flow direction
  -j, --jobs JOBS       worker threads for the builds (default: one per CPU; 1: none)
  --revision ID         draw the sheet as of this revision ('base': before any revision)
  --highlight {all,latest,none}
                        revisions drawn in their colour: all (default), only the latest, or none
  --since ID            draw only the revisions from this one on as changes (in colour); earlier
                        ones are drawn as settled
  --freeze PATH         write the spec with the sheet as built kept in it (through --revision,
                        default: the last revision), so later revisions are drawn onto exactly
                        that sheet
  --snapshot PATH       also write the placement, routes and drawing as JSON (as the
                        Python tool's snapshot tests list them)
  --list-elements       list element types (including the spec's custom elements) and exit
  --describe TYPE       show the default ports of an element type";

struct Args {
    spec: Option<String>,
    output: Option<String>,
    labels: bool,
    frame: bool,
    fontsize: Option<f64>,
    layout: Option<String>,
    jobs: Option<usize>,
    snapshot: Option<String>,
    revision: Option<String>,
    highlight: String,
    since: Option<String>,
    freeze: Option<String>,
    list: bool,
    describe: Option<String>,
}

fn parse(argv: &[String]) -> Result<Args, String> {
    let mut a = Args {
        spec: None,
        output: None,
        labels: true,
        frame: true,
        fontsize: None,
        layout: None,
        jobs: None,
        snapshot: None,
        revision: None,
        highlight: "all".into(),
        since: None,
        freeze: None,
        list: false,
        describe: None,
    };
    let mut it = argv.iter();
    while let Some(arg) = it.next() {
        let (flag, inline) = match arg.split_once('=') {
            Some((f, v)) if f.starts_with("--") => (f.to_string(), Some(v.to_string())),
            _ => (arg.clone(), None),
        };
        let mut value = |name: &str| -> Result<String, String> {
            inline
                .clone()
                .or_else(|| it.next().cloned())
                .ok_or(format!("argument {name}: expected one argument"))
        };
        match flag.as_str() {
            "-h" | "--help" => {
                println!("{USAGE}");
                std::process::exit(0);
            }
            "-o" | "--output" => a.output = Some(value("-o/--output")?),
            "--no-labels" => a.labels = false,
            "--no-frame" => a.frame = false,
            "--fontsize" => {
                let v = value("--fontsize")?;
                a.fontsize = Some(
                    v.parse()
                        .map_err(|_| format!("argument --fontsize: invalid float value: '{v}'"))?,
                );
            }
            "--layout" => {
                let v = value("--layout")?;
                if v != "order" && v != "flow" {
                    return Err(format!(
                        "argument --layout: invalid choice: '{v}' (choose from 'order', 'flow')"
                    ));
                }
                a.layout = Some(v);
            }
            "-j" | "--jobs" => {
                let v = value("-j/--jobs")?;
                a.jobs = Some(
                    v.parse()
                        .map_err(|_| format!("argument -j/--jobs: invalid int value: '{v}'"))?,
                );
            }
            "--snapshot" => a.snapshot = Some(value("--snapshot")?),
            "--list-elements" => a.list = true,
            "--describe" => a.describe = Some(value("--describe")?),
            "--revision" => a.revision = Some(value("--revision")?),
            "--since" => a.since = Some(value("--since")?),
            "--freeze" => a.freeze = Some(value("--freeze")?),
            "--highlight" => {
                let v = value("--highlight")?;
                if !["all", "latest", "none"].contains(&v.as_str()) {
                    return Err(format!(
                        "argument --highlight: invalid choice: '{v}' (choose from 'all', 'latest', 'none')"
                    ));
                }
                a.highlight = v;
            }
            s if s.starts_with('-') && s.len() > 1 => return Err(format!("unrecognized arguments: {s}")),
            _ => {
                if a.spec.is_some() {
                    return Err(format!("unrecognized arguments: {arg}"));
                }
                a.spec = Some(arg.clone());
            }
        }
    }
    Ok(a)
}

fn run(args: Args) -> Result<(), String> {
    let mut fs = match &args.spec {
        Some(path) => {
            let text = std::fs::read_to_string(path).map_err(|e| format!("{path}: {e}"))?;
            Flowsheet::from_json(&text).map_err(|e| e.to_string())?
        }
        None => Flowsheet::new("flow").map_err(|e| e.to_string())?,
    };
    if args.list {
        for name in fs.library.names() {
            println!("{name:18} {}", summary(&name));
        }
        return Ok(());
    }
    if let Some(t) = &args.describe {
        fs.library.check(t).map_err(|e| e.to_string())?;
        println!("{}", summary(t));
        println!("\ncommon params: rotation (0/90/180/270), mirror (true/false)");
        let geo = fs.library.build(t, &Default::default()).map_err(|e| e.to_string())?;
        let mut ports: Vec<&String> = geo.ports.keys().collect();
        ports.sort();
        println!("default ports: {}", pfd::pyval::list_repr(&ports));
        return Ok(());
    }
    if args.spec.is_none() {
        return Err("the spec argument is required".into());
    }
    if let Some(l) = args.layout {
        fs.layout = l;
    }
    if args.jobs.is_some() {
        fs.jobs = args.jobs;
    }
    if let Some(path) = &args.freeze {
        let d = fs.freeze(4, args.revision.as_deref()).map_err(|e| e.to_string())?;
        let through = args
            .revision
            .clone()
            .unwrap_or_else(|| fs.revisions.last().map_or("base".into(), |r| r.id.clone()));
        let text = fs.to_json().map_err(|e| e.to_string())?;
        std::fs::write(path, text).map_err(|e| format!("{path}: {e}"))?;
        for w in &d.warnings {
            eprintln!("warning: {w}");
        }
        println!("wrote {path} (frozen through {through})");
        if args.output.is_none() {
            return Ok(());
        }
    }
    let output = args.output.unwrap_or_else(|| "pfd.svg".into());
    let lower = output.to_lowercase();
    if !lower.ends_with(".svg") {
        return Err(format!("{output}: only .svg output is supported by the Rust port"));
    }
    let diagram = fs
        .build_until(4, true, args.revision.as_deref())
        .map_err(|e| e.to_string())?;
    let sc = render::scene_with(
        &diagram,
        args.labels,
        args.fontsize,
        args.frame,
        None,
        &args.highlight,
        args.since.as_deref(),
    )
    .map_err(|e| e.to_string())?;
    std::fs::write(&output, pfd::svg::to_svg(&sc)).map_err(|e| format!("{output}: {e}"))?;
    if let Some(path) = &args.snapshot {
        let snap =
            render::snapshot_with(&diagram, &args.highlight, args.since.as_deref()).map_err(|e| e.to_string())?;
        std::fs::write(path, serde_json::to_string_pretty(&snap).unwrap()).map_err(|e| format!("{path}: {e}"))?;
    }
    for w in &diagram.warnings {
        eprintln!("warning: {w}");
    }
    let bends: Vec<String> = diagram
        .routes
        .iter()
        .map(|(c, r)| format!("{}->{}: {}", c.src, c.dst, r.bends()))
        .collect();
    println!("wrote {output} (bends: {})", bends.join(", "));
    Ok(())
}

fn main() -> ExitCode {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    let args = match parse(&argv) {
        Ok(a) => a,
        Err(e) => {
            eprintln!("{}", USAGE.lines().take(3).collect::<Vec<_>>().join("\n"));
            eprintln!("pfd: error: {e}");
            return ExitCode::from(2);
        }
    };
    match run(args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("error: {e}");
            ExitCode::FAILURE
        }
    }
}
