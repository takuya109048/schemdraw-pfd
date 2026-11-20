# Vendored packages

| Package | Version | Source | License |
|---|---|---|---|
| [schemdraw](https://github.com/cdelker/schemdraw) | 0.23 | PyPI wheel `schemdraw-0.23` | MIT (`schemdraw/LICENSE.txt`) |

## schemdraw

The `schemdraw/` package is the 0.23 wheel's contents copied as-is (files
checked against the wheel's `RECORD` hashes), plus its `LICENSE.txt`.

Local changes (keep this list up to date):

- `elements/outlets.py`: the only module that imported its own package by
  absolute name (`from schemdraw.… import …`). Rewritten to relative imports so
  the copy works as `schemdraw_pfd._vendor.schemdraw` without a top-level
  `schemdraw` installed.

Optional extras are not vendored: the matplotlib backend uses the project's
`matplotlib` dependency; the SVG math renderer (`ziamath`) is imported lazily and
is not needed by schemdraw-pfd.

### Updating

1. Install the new release in a scratch environment and copy its `schemdraw/`
   package over this one (drop `__pycache__/`), together with `LICENSE.txt`.
2. Re-apply the local changes above; `grep -rnE "^(from|import) schemdraw"
   schemdraw/` must print nothing.
3. Update the version in the table above and run the test suite.
