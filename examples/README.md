# Examples

Synthetic, shareable demo data (no real cohort data) showing the three table
shapes the tool handles. `Demo/` holds one CSV per panel:

| CSV | becomes |
|---|---|
| `grouped_treatment_by_dose.csv` | grouped-wide table **+** native Two-Way `__grouped` (treatment × dose) |
| `xy_dose_response.csv` | XY table (continuous `dose_mg`) |
| `matrix_correlation.csv` | column table **+** native Two-Way `__heatmap` (3×3) |

Run it (from the repo root) — this also exercises generic-folder mode and the
`--root`/`--out` flags:

```bash
python ../build_prism.py --root . --out ./out --combined --validate
```

You'll get `out/Demo.pzfx`, `out/All_figures.pzfx`, `out/MANIFEST.csv`, and
`out/VALIDATION.md`. The `out/` folder is reproducible and git-ignored.
