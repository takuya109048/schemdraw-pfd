"""The single-file bundle (``tools/build_single.py``): it embeds every module
it needs, runs from its own copy rather than the installed package, and lays
out and routes every sheet exactly as the package does.

Both the bundle and the package it is compared with run in fresh
interpreters, so the comparison does not see what other tests changed in
this one (e.g. elements registered in the default library)."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

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
# With "-" in place of the bundle, the code runs on the installed package.
LOAD = (
    "import runpy, sys\n"
    "bundle = runpy.run_path(sys.argv[1], run_name='pfd') "
    "if sys.argv[1] != '-' else None\n"
)


def _python(*args: str) -> str:
    """stdout of a fresh interpreter run with ``args``."""
    out = subprocess.run(
        [sys.executable, "-W", "ignore", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout


def _run(bundle: Path | None, code: str, *args: str) -> str:
    """stdout of ``code`` run on the bundle (None: on the installed package)."""
    return _python("-c", LOAD + code, str(bundle) if bundle else "-", *args)


def _cli(bundle: Path | None, *args: str) -> str:
    """stdout of the bundle's CLI (None: the installed package's)."""
    return _python(*([str(bundle)] if bundle else ["-m", "schemdraw_pfd.cli"]), *args)


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
        "pkg = Path(sys.modules['schemdraw_pfd'].__file__)\n"
        "assert (bundle is None) != pkg.is_relative_to(sys.argv[1]), pkg\n"
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
    want = json.loads(_run(None, code, *map(str, sheets)))
    assert got.keys() == want.keys()
    for name in want:
        assert got[name] == want[name], name


@pytest.mark.parametrize("suffix", [".svg", ".png", ".xlsx"])
def test_cli(bundle, tmp_path, suffix):
    out, ref = tmp_path / f"out{suffix}", tmp_path / f"ref{suffix}"
    got = _cli(bundle, str(EXAMPLE), "-o", str(out))
    want = _cli(None, str(EXAMPLE), "-o", str(ref))
    assert out.stat().st_size > 0
    assert got == want.replace(str(ref), str(out))


def test_cli_list_elements(bundle):
    assert _cli(bundle, "--list-elements") == _cli(None, "--list-elements")
