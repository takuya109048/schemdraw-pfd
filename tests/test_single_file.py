"""The single-file bundle (``tools/build_single.py``): it embeds every module
it needs, runs from its own copy rather than the installed package, and lays
out and routes every sheet exactly as the package does."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from schemdraw_pfd import Flowsheet
from schemdraw_pfd.cli import main as cli_main

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "tests" / "cases"
EXAMPLE = ROOT / "examples" / "cooling_water.json"
VALID = sorted(p for p in CASES.rglob("*.json") if "invalid" not in p.parts)

_spec = importlib.util.spec_from_file_location(
    "build_single", ROOT / "tools" / "build_single.py"
)
assert _spec and _spec.loader
build_single = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_single)

# Code run in a child interpreter starts here: the bundle sys.argv[1] is
# loaded (its globals in ``bundle``) and the rest of argv is for the code.
LOAD = "import runpy, sys\nbundle = runpy.run_path(sys.argv[1], run_name='pfd')\n"


def _layout(path: Path) -> list:
    """Placement and routes of the sheet ``path`` (plain JSON data)."""
    d = Flowsheet.from_json(path).build()
    return [
        [[p.id, p.x, p.y, p.geo.width, p.geo.height] for p in d.placed],
        [[c.src, c.dst, c.kind, r.points] for c, r in d.routes],
        d.warnings,
    ]


def _run(bundle: Path, code: str, *args: str) -> str:
    out = subprocess.run(
        [sys.executable, "-W", "ignore", "-c", LOAD + code, str(bundle), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout


@pytest.fixture(scope="module")
def bundle(tmp_path_factory) -> Path:
    return build_single.build(tmp_path_factory.mktemp("bundle") / "pfd.py")


def test_build_is_reproducible(bundle, tmp_path):
    again = build_single.build(tmp_path / "pfd.py")
    assert again.read_bytes() == bundle.read_bytes()


def test_every_module_imports_from_the_bundle(bundle):
    code = (
        "import importlib, json\n"
        "names = sorted(bundle['_MODULES'])\n"
        "files = [importlib.import_module(n).__file__ for n in names]\n"
        "print(json.dumps([names, files]))\n"
    )
    names, files = json.loads(_run(bundle, code))
    assert "schemdraw_pfd.cli" in names
    assert "schemdraw_pfd._vendor.schemdraw.backends.svg" in names
    assert not any(n.startswith("schemdraw_pfd._vendor.schemdraw.logic") for n in names)
    assert all(Path(f).is_relative_to(bundle) for f in files), files


def test_same_layout_as_the_package(bundle):
    code = (
        "import json\n"
        "from pathlib import Path\n"
        "from schemdraw_pfd import Flowsheet\n"
        "assert Path(sys.modules['schemdraw_pfd'].__file__)"
        ".is_relative_to(sys.argv[1])\n"
        "out = {}\n"
        "for p in sys.argv[2:]:\n"
        "    d = Flowsheet.from_json(p).build()\n"
        "    out[p] = [\n"
        "        [[q.id, q.x, q.y, q.geo.width, q.geo.height] for q in d.placed],\n"
        "        [[c.src, c.dst, c.kind, r.points] for c, r in d.routes],\n"
        "        d.warnings,\n"
        "    ]\n"
        "print(json.dumps(out))\n"
    )
    sheets = [*VALID, EXAMPLE]
    got = json.loads(_run(bundle, code, *map(str, sheets)))
    want = json.loads(json.dumps({str(p): _layout(p) for p in sheets}))
    assert got.keys() == want.keys()
    for name in want:
        assert got[name] == want[name], name


@pytest.mark.parametrize("suffix", [".svg", ".png", ".xlsx"])
def test_cli(bundle, tmp_path, capsys, suffix):
    out = tmp_path / f"out{suffix}"
    ref = tmp_path / f"ref{suffix}"
    run = subprocess.run(
        [sys.executable, "-W", "ignore", str(bundle), str(EXAMPLE), "-o", str(out)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stat().st_size > 0
    cli_main([str(EXAMPLE), "-o", str(ref)])
    want = capsys.readouterr().out.replace(str(ref), str(out))
    assert run.stdout == want


def test_cli_list_elements(bundle, capsys):
    run = subprocess.run(
        [sys.executable, str(bundle), "--list-elements"],
        capture_output=True,
        text=True,
        check=True,
    )
    cli_main(["--list-elements"])
    assert run.stdout == capsys.readouterr().out
