# csv2prism

[![CI](https://github.com/GerryFAN0706/csv2prism/actions/workflows/ci.yml/badge.svg)](https://github.com/GerryFAN0706/csv2prism/actions/workflows/ci.yml)

Turn tidy **CSV** data into **editable GraphPad Prism** (`.pzfx`) files — one file
per group of CSVs, one Prism **data table per CSV**, with the right table type
chosen automatically. Open the file in Prism, click a table, **New Graph → pick a
type**, and style it in the GUI.

> Why this exists: GraphPad Prism has **no plotting API** — you can't script
> "draw this axis/colour". But you *can* generate Prism's native `.pzfx` data
> tables; Prism then renders the graph from the table. `csv2prism` does exactly
> that, so you can keep your analysis in code and still hand off real, editable
> Prism files.

It's a **hybrid** of two excellent MIT-licensed libraries, using each for what
it does best:

| Stage | Tool | Produces |
|---|---|---|
| 1 — `make_pzfx.R` | [`pzfx`](https://github.com/Yue-Jiang/pzfx) (R) | Column, XY, matrix, and grouped-wide tables (with confidence intervals preserved) |
| 2 — `build_prism.py` | [`prismWriter`](https://github.com/smestern/prismWriter) (Python) | Prism's **native Two-Way (Grouped)** tables + heatmap-ready matrices |

`pzfx` is mature and handles **XY/scatter** tables (which prismWriter can't) and
keeps your exact `ci_lo`/`ci_hi`; `prismWriter` is the only one that writes true
**Grouped** tables. csv2prism merges their output into one file per group,
reconciling the XML-namespace difference between the two tools.

## Install

```bash
# R, once:
Rscript -e 'install.packages("pzfx")'        # v0.3.1+
# Python, once:
pip install -r requirements.txt              # pandas + prismWriter
```

You need **R (≥4.0)** for stage 1 and **Python (≥3.11)** for stage 2.

## Quick start

```bash
# run the bundled synthetic demo (also exercises generic mode + flags):
python build_prism.py --root examples --out examples/out --combined --validate
```

This reads `examples/Demo/*.csv` and writes `examples/out/Demo.pzfx`,
`All_figures.pzfx`, `MANIFEST.csv`, and `VALIDATION.md`. Open `Demo.pzfx` in
Prism.

## Use on your own data

1. Put one tidy CSV per panel under `<root>/<group>/`. Each sub-folder of CSVs
   becomes one `.pzfx` file (so group related panels together). A flat folder of
   CSVs works too.
2. Run it:
   ```bash
   python build_prism.py --root path/to/data --out path/to/out
   ```
3. Conventions the auto-typing uses:
   - a text column → Prism **row titles** (`*_display`/`*_label` preferred over
     raw codes; `*_internal`/`*_code` twins dropped);
   - low/high CI columns in any common spelling — `ci_lo`/`ci_hi`, `lo`/`hi`,
     `x_lo`/`x_hi`, `lo_x`/`hi_x` — get a matching `*_halfwidth` added;
   - `*_vs_*` in the filename, or an ascending first numeric column → **XY**;
   - a square numeric block keyed by one label column → **heatmap**;
   - a balanced *entity × factor* crossing → a native **Grouped** table.

## Command-line options (`build_prism.py`)

| Flag | Effect |
|---|---|
| *(none)* | run both stages → `.pzfx` files with native Two-Way tables |
| `--root PATH` | input data root (default `figure_data`) |
| `--out PATH` | output dir (default `<root>/pzfx`) |
| `--combined` | also write `All_figures.pzfx` (every table in one file) |
| `--validate` | audit all outputs after building → `VALIDATION.md` |
| `--validate-only` | just audit existing outputs (no rebuild) |
| `--skip-r` | stage 2 only (stage-1 output must already exist) |
| `--no-notes` | skip the provenance Info page in each table |

Prefer `make_pzfx.R` alone (no Python) if you only need Column/XY/grouped-wide
tables:

```bash
Rscript make_pzfx.R [root] [out] [nonotes]
```

## What you get

- **One `.pzfx` per group**, each table auto-typed and named after its CSV.
- **Grouped panels** get *both* a CI-bearing wide table *and* a native `__grouped`
  Two-Way table; **matrices** get a `__heatmap` Two-Way table.
- **Provenance**: each table carries an in-Prism Info page (source CSV, layout,
  tool).
- **Clean data**: `NA`/`NaN` become blank cells (Prism reads them as missing);
  labels containing `<`, `>`, `&` are XML-escaped.
- **`MANIFEST.csv`** indexes every table; **`VALIDATION.md`** reports a QA audit.

## Notes & limits

- Prism can't store **native asymmetric** error bars, so exact `ci_lo`/`ci_hi`
  are kept as columns (plus `ci_halfwidth` for quick symmetric bars). Native
  `__grouped` tables hold the point estimate; use the wide table for CIs.
- Very large tables (>1000 rows; configurable via `ROW_CAP` in `make_pzfx.R`) are
  skipped — the underlying writer is ~O(n²). Use the CSV directly for those.
- Re-running `--skip-r` on already-merged files would double-inject; the default
  (no flag) regenerates a clean base first.

## Credits

Built on, and grateful to:

- **pzfx** — Yue Jiang & contributors, MIT — <https://github.com/Yue-Jiang/pzfx>
- **prismWriter** — smestern, MIT — <https://github.com/smestern/prismWriter>

The merge logic, routing, shape heuristics, label-escaping, missing-value
cleanup, validator, and CLI are csv2prism's own.

## License

MIT — see [`LICENSE`](LICENSE).

> "GraphPad" and "Prism" are trademarks of GraphPad Software / Dotmatics.
> This project is independent and only reads/writes the `.pzfx` file format.
