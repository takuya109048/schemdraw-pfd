# Python Project Rules

## Manage packages

This project uses pip and the standard-library `venv`. Do not use uv, pip-tools,
poetry, or conda. There is no lockfile.

- Create the environment: `python -m venv .venv`
- Activate it: `source .venv/bin/activate` (Windows: `.venv\Scripts\activate`)
- Install the project and dev tools (editable): `pip install -e ".[dev]"`
- Runtime dependencies live in `[project] dependencies` in `pyproject.toml`
- Dev tools (pytest, ruff, pyrefly) live in `[project.optional-dependencies] dev`
- After changing dependencies in `pyproject.toml`, re-run `pip install -e ".[dev]"`
- SchemDraw is vendored in `src/schemdraw_pfd/_vendor/schemdraw/`; do not add
  `schemdraw` as a dependency. See `src/schemdraw_pfd/_vendor/README.md`.

## Run code

Run everything inside the activated `.venv`, or call the tools through
`.venv/bin/` (for example `.venv/bin/pytest`). Do not install packages globally.

- Run a script: `python script.py`
- Run a module: `python -m module_name`
- Run the CLI: `pfd SPEC.json -o out.svg`
- Build the single-file bundle: `python tools/build_single.py` (writes
  `dist/pfd.py`, a generated file; never edit it or commit it)

## Create new projects

- Always use `pyproject.toml` for metadata (PEP 621). Never create `setup.py`,
  `setup.cfg`, or `requirements.txt`.
- Build backend: setuptools (`setuptools.build_meta`)

## Test code

- Framework: pytest
- Run tests: `pytest`
- Test files go in `tests/` at the project root
- Test file naming: `test_*.py`
- Test function naming: `test_*`
- No `__init__.py` needed in `tests/`
- Update layout snapshots after an intended change:
  `PFD_UPDATE_SNAPSHOTS=1 pytest tests/test_snapshots.py tests/test_cases.py`

## Configure continuous integration

Use `actions/setup-python` in GitHub Actions, then `pip install -e ".[dev]"`.

## Lint and format code

- Tool: ruff (handles both linting and formatting)
- Lint: `ruff check .`
- Lint and auto-fix: `ruff check --fix .`
- Format: `ruff format .`
- Check formatting: `ruff format --check .`
- Configuration lives in `pyproject.toml` under `[tool.ruff]`

## Check types

- Tool: pyrefly
- Run: `pyrefly check`
- Configuration lives in `pyproject.toml` under `[tool.pyrefly]`

## Follow code style

- Follow ruff's defaults for formatting (88 char line length, double quotes, spaces)
- Import sorting is handled by ruff (`isort` rules enabled via `select = ["I"]`)
- Do not add `# type: ignore` comments without an error code

## Avoid these practices

- Do not use uv (`uv`, `uvx`, `uv.lock`, `uv_build`).
- Do not install packages globally or outside `.venv`.
- Do not create `requirements.txt` for dependency management. Use `pyproject.toml`.
- Do not run `python setup.py` commands.
