"""Bundle schemdraw-pfd into one runnable file: ``python tools/build_single.py``.

The output (``dist/pfd.py`` by default) holds the source of every module of
the ``schemdraw_pfd`` package, the vendored SchemDraw included, compressed and
embedded as data.  A small import hook at its top serves those modules from
memory, so the package code runs unchanged::

    python pfd.py SPEC.json -o out.svg

The only thing the file needs besides Python >= 3.12 is matplotlib
(``pip install matplotlib``): it has compiled parts and cannot be embedded.

SchemDraw subpackages that schemdraw-pfd never imports (logic, dsp, ...) are
left out to keep the file small.  The build is reproducible: the same sources
give the same file, byte for byte.
"""

from __future__ import annotations

import argparse
import base64
import sys
import tomllib
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "schemdraw_pfd"
VENDOR = PACKAGE / "_vendor" / "schemdraw"
DEFAULT_OUT = ROOT / "dist" / "pfd.py"

# Vendored SchemDraw subpackages that schemdraw-pfd never imports.
UNUSED_VENDOR = ("dsp", "flow", "logic", "parsing", "pictorial")

LINE = 76  # width of the embedded data lines

TEMPLATE = '''\
#!/usr/bin/env python3
"""schemdraw-pfd {version}: PFD / P&ID generator in one file.

    python pfd.py SPEC.json -o out.svg      (svg/png/pdf/xlsx)
    python pfd.py --help

Requires Python >= 3.12 and matplotlib (``pip install matplotlib``).

The package can also be imported from Python once this file has been loaded
(``import pfd`` with this file on the path), e.g.
``from schemdraw_pfd import Flowsheet``.

GENERATED FILE -- do not edit.  Built by tools/build_single.py from the
schemdraw-pfd sources ({count} modules); rebuild it instead.

Includes SchemDraw {schemdraw_version} (https://github.com/cdelker/schemdraw),
under the MIT license:

{license}"""

import base64 as _base64
import importlib.abc as _abc
import importlib.util as _util
import os as _os
import sys as _sys
import zlib as _zlib

_MODULES = {{
{entries}
}}


class _BundleImporter(_abc.MetaPathFinder, _abc.InspectLoader):
    """Serves the modules embedded in this file."""

    def __init__(self, origin):
        self.origin = origin

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in _MODULES:
            return None
        is_pkg = _MODULES[fullname][0]
        spec = _util.spec_from_loader(fullname, self, is_package=is_pkg)
        spec.origin = self._filename(fullname)
        spec.has_location = True  # sets __file__, so tracebacks show the source
        return spec

    def _filename(self, fullname):
        is_pkg = _MODULES[fullname][0]
        parts = fullname.split(".") + (["__init__"] if is_pkg else [])
        return _os.path.join(self.origin, *parts) + ".py"

    def is_package(self, fullname):
        return _MODULES[fullname][0]

    def get_source(self, fullname):
        if fullname not in _MODULES:
            raise ImportError(fullname, name=fullname)
        data = _base64.b85decode("".join(_MODULES[fullname][1]))
        return _zlib.decompress(data).decode("utf-8")

    def get_code(self, fullname):
        return compile(
            self.get_source(fullname), self._filename(fullname), "exec",
            dont_inherit=True,
        )


def _install():
    origin = _os.path.abspath(__file__)
    if any(getattr(f, "origin", None) == origin for f in _sys.meta_path):
        return  # this file was loaded before (e.g. as __main__ and as a module)
    # first, so the embedded copy wins over an installed schemdraw_pfd
    _sys.meta_path.insert(0, _BundleImporter(origin))


_install()


def main(argv=None):
    from schemdraw_pfd.cli import main as cli_main

    try:
        return cli_main(argv)
    except ModuleNotFoundError as e:  # matplotlib is imported when drawing
        if e.name and e.name.split(".")[0] == "matplotlib":
            print(
                "pfd: matplotlib is required: pip install matplotlib",
                file=_sys.stderr,
            )
            return 1
        raise


if __name__ == "__main__":
    raise SystemExit(main())
'''


def module_name(path: Path) -> tuple[str, bool]:
    """Dotted module name of a source file under ``src/``, and whether it is
    a package (``__init__.py``)."""
    rel = path.relative_to(PACKAGE.parent).with_suffix("")
    parts = list(rel.parts)
    is_pkg = parts[-1] == "__init__"
    if is_pkg:
        parts.pop()
    return ".".join(parts), is_pkg


def sources() -> list[Path]:
    """The package's source files to embed, in a stable order."""
    skip = [VENDOR / name for name in UNUSED_VENDOR]
    return sorted(
        p
        for p in PACKAGE.rglob("*.py")
        if "__pycache__" not in p.parts and not any(p.is_relative_to(s) for s in skip)
    )


def encode(source: bytes) -> list[str]:
    """``source`` compressed and base85 encoded, cut into lines."""
    data = base64.b85encode(zlib.compress(source, 9)).decode("ascii")
    return [data[i : i + LINE] for i in range(0, len(data), LINE)]


def entry(name: str, is_pkg: bool, lines: list[str]) -> str:
    body = "\n".join(f'        "{line}"' for line in lines)
    return f'    "{name}": ({is_pkg}, (\n{body}\n    )),'


def schemdraw_version() -> str:
    for line in (VENDOR / "__init__.py").read_text("utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip("'\"")
    raise RuntimeError("no __version__ in the vendored schemdraw")


def build(out: Path = DEFAULT_OUT) -> Path:
    """Write the single-file bundle to ``out`` and return its path."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]
    files = sources()
    entries = []
    for path in files:
        name, is_pkg = module_name(path)
        entries.append(entry(name, is_pkg, encode(path.read_bytes())))
    license_text = (VENDOR / "LICENSE.txt").read_text("utf-8").strip()
    text = TEMPLATE.format(
        version=project["version"],
        count=len(files),
        schemdraw_version=schemdraw_version(),
        license="\n".join(
            f"    {line}".rstrip() for line in license_text.splitlines()
        ).replace("\\", "\\\\"),
        entries="\n".join(entries),
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, "utf-8", newline="\n")
    out.chmod(0o755)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUT,
        help=f"output path (default: {DEFAULT_OUT.relative_to(ROOT)})",
    )
    args = ap.parse_args(argv)
    out = build(args.output)
    size = out.stat().st_size
    print(f"wrote {out} ({size / 1024:.0f} KiB)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
