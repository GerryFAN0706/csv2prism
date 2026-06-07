#!/usr/bin/env python3
"""
build_prism.py  --  Hybrid GraphPad Prism builder (pzfx + prismWriter).

Two stages:
  1. R / pzfx  (make_pzfx.R): writes the complete base `FigN.pzfx` files
     (Column / XY / matrix / grouped-wide tables, CIs preserved, provenance
     notes) plus a machine-readable `_routing.csv` of grouped / matrix panels.
  2. Python / prismWriter (this script): builds Prism's NATIVE Two-Way tables
     (real Grouped tables + heatmap-ready matrices) and INJECTS them into each
     `FigN.pzfx`, normalising the XML-namespace mismatch between the two tools.

Extra functions:
  --combined        also write `All_figures.pzfx` with every table in one file
  --validate        audit every output file after building (integrity + cells)
  --validate-only   only audit existing files (no build)
  --root / --out    point at any data root / output dir
  --no-notes        skip provenance Info pages in stage 1

Usage (from repo root):
  python figure_data/pzfx/build_prism.py                 # stage 1 + 2
  python figure_data/pzfx/build_prism.py --combined --validate
  python figure_data/pzfx/build_prism.py --skip-r        # stage 2 only
  python figure_data/pzfx/build_prism.py --validate-only # just QA

Requires: pip install "git+https://github.com/smestern/prismWriter" pandas
          and Rscript on PATH for stage 1.
"""
import argparse
import copy
import csv
import logging
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

import pandas as pd

logging.disable(logging.INFO)  # silence prismWriter's chatty INFO logs
from prismWriter.prism_writer import PrismFile  # noqa: E402

HERE     = os.path.dirname(os.path.abspath(__file__))
PRISM_NS = "{http://graphpad.com/prism/Prism.htm}"
SUB      = "_sub"   # constant subgroup -> exactly one subcolumn (avoids diagonal-sprawl footgun)


# ----------------------------------------------------------------------------- R stage
def run_r(root, out, no_notes):
    rscript = shutil.which("Rscript") or r"C:\Program Files\R\R-4.6.0\bin\x64\Rscript.exe"
    if shutil.which("Rscript") is None and not os.path.exists(rscript):
        sys.exit("Rscript not found; install R or run with --skip-r after generating the base.")
    print("[stage 1] Rscript make_pzfx.R ...")
    subprocess.run([rscript, os.path.join(HERE, "make_pzfx.R"),
                    root, out, "nonotes" if no_notes else ""], check=True)


# ------------------------------------------------------------------- dataframe shaping
def _esc(s):
    # prismWriter substitutes strings into XML templates without escaping, and our
    # labels contain '<' (e.g. "BMI 35+ vs <25", "age | <65"); escape so XML stays valid.
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _escape_labels(df):
    # escape every non-numeric (label) column; pandas 3.x strings are Arrow-backed,
    # not 'object', so test by "not numeric" rather than dtype == object.
    for c in df.columns:
        if not pd.api.types.is_numeric_dtype(df[c]):
            df[c] = df[c].astype(str).map(_esc)
    return df


def prep_grouped(row):
    """Long tidy data -> (df, kwargs) for a native Two-Way Grouped table."""
    df = pd.read_csv(row["csv"])
    entity_cols = row["entity"].split("|")
    df["__entity"] = df[entity_cols].astype(str).agg(" | ".join, axis=1)
    df[SUB] = row["value"]                       # subcolumn title = the measured quantity (e.g. "OR")
    slim = df[[row["groupby"], "__entity", row["value"], SUB]].copy()
    slim = _escape_labels(slim)
    kwargs = dict(groupby=row["groupby"], subgroupby=SUB,
                  rowgroupby="__entity", cols=[row["value"]])
    return slim, kwargs


def prep_heatmap(row):
    """Square matrix -> (df, kwargs) for a heatmap-ready Two-Way table."""
    df = pd.read_csv(row["csv"])
    rl = row["row_label"]
    value_cols = [c for c in df.columns if c != rl and pd.api.types.is_numeric_dtype(df[c])]
    long = df.melt(id_vars=[rl], value_vars=value_cols, var_name="__col", value_name="__val")
    long[SUB] = "value"
    long = _escape_labels(long)
    kwargs = dict(groupby="__col", subgroupby=SUB, rowgroupby=rl, cols=["__val"])
    return long, kwargs


# ----------------------------------------------------------------------- XML namespace
def local(tag):
    return tag.split("}", 1)[1] if "}" in tag else tag


def base_namespace(root):
    return root.tag[: root.tag.index("}") + 1] if "}" in root.tag else ""


def retag(el, ns):
    for e in el.iter():
        e.tag = ns + local(e.tag)


def _next_table_id(tables):
    used = {int(m.group(1)) for t in tables
            for m in [re.match(r"Table(\d+)$", t.get("ID", ""))] if m}
    return (max(used) + 1) if used else 0


# --------------------------------------------------------------------------- the merge
def inject(base_path, donor_tables):
    """Insert donor Two-Way <Table> elements into a base pzfx file, namespace-normalised."""
    tree = ET.parse(base_path)
    root = tree.getroot()
    ns   = base_namespace(root)
    if ns:
        ET.register_namespace("", ns[1:-1])
    q = lambda t: ns + t  # noqa: E731

    seq = root.find(q("TableSequence"))
    next_id = _next_table_id(root.findall(q("Table")))
    ref_attrs = dict(seq.findall(q("Ref"))[0].attrib) if seq.findall(q("Ref")) else {}

    added = []
    for name, dtbl in donor_tables:
        retag(dtbl, ns)                          # match base file's namespace (or none)
        tid = f"Table{next_id}"; next_id += 1
        dtbl.set("ID", tid)
        if dtbl.find(q("Title")) is None:
            ET.SubElement(dtbl, q("Title")).text = name
        root.append(dtbl)
        ref = ET.SubElement(seq, q("Ref"))
        for k, v in ref_attrs.items():
            if local(k) != "ID":
                ref.set(k, v)
        ref.set("ID", tid)
        added.append(name)
    tree.write(base_path, encoding="UTF-8", xml_declaration=True)
    return added


def donor_tables_for(fig, frows):
    """Build all of one figure's Two-Way tables via prismWriter; return [(name, element)]."""
    pf = PrismFile()
    for r in frows:
        df, kwargs = prep_grouped(r) if r["type"] == "grouped" else prep_heatmap(r)
        pf.make_group_table(group_name=r["out_name"], group_values=df, **kwargs)
    tmp = os.path.join(HERE, f"_donor_{fig}.pzfx")
    pf.save(tmp)
    wanted = {r["out_name"] for r in frows}
    out = []
    for t in ET.parse(tmp).getroot().findall(PRISM_NS + "Table"):
        ttl = t.find(PRISM_NS + "Title")
        nm = ttl.text if ttl is not None else t.get("ID")
        if nm in wanted:
            out.append((nm, t))
    os.remove(tmp)
    return out


# ------------------------------------------------------------------- missing-value clean
def clean_na(out_dir):
    """Blank literal 'NA'/'NaN' in data cells (XColumn/YColumn) so Prism reads them as
    missing, not text. Row titles are left untouched."""
    total = files_touched = 0
    for f in sorted(x for x in os.listdir(out_dir) if x.endswith(".pzfx")):
        path = os.path.join(out_dir, f)
        tree = ET.parse(path)
        root = tree.getroot()
        ns = base_namespace(root)
        q = lambda t: ns + t  # noqa: E731
        changed = 0
        for t in root.findall(q("Table")):
            for col in t.findall(q("XColumn")) + t.findall(q("YColumn")):
                for d in col.iter():
                    if local(d.tag) == "d" and d.text and d.text.strip().upper() in {"NA", "NAN"}:
                        d.text = None
                        changed += 1
        if changed:
            if ns:
                ET.register_namespace("", ns[1:-1])
            tree.write(path, encoding="UTF-8", xml_declaration=True)
            total += changed
            files_touched += 1
    print(f"[clean] blanked {total} 'NA'/'NaN' data cells in {files_touched} file(s)")
    return total


# ------------------------------------------------------------------------ combined file
def build_combined(out_dir, fig_paths):
    """Write `All_figures.pzfx` containing every table from every figure file."""
    skeleton = ET.parse(fig_paths[0][1])
    broot = skeleton.getroot()
    ns = base_namespace(broot)
    if ns:
        ET.register_namespace("", ns[1:-1])
    q = lambda t: ns + t  # noqa: E731

    seq = broot.find(q("TableSequence"))
    for t in broot.findall(q("Table")):
        broot.remove(t)
    for r in list(seq):
        seq.remove(r)
    # keep one minimal Info page; drop the rest so notes refs don't dangle
    infos = broot.findall(q("Info"))
    for extra in infos[1:]:
        broot.remove(extra)
    iseq = broot.find(q("InfoSequence"))
    if iseq is not None:
        for r in list(iseq)[1:]:
            iseq.remove(r)

    next_id = 0
    n = 0
    for fig, path in fig_paths:
        tree = ET.parse(path)
        for tbl in tree.getroot().findall(base_namespace(tree.getroot()) + "Table"
                                          if base_namespace(tree.getroot()) else "Table"):
            t2 = copy.deepcopy(tbl)
            retag(t2, ns)
            tid = f"Table{next_id}"; next_id += 1
            t2.set("ID", tid)
            title = t2.find(q("Title"))
            if title is not None and title.text:
                title.text = f"{fig} | {title.text}"
            broot.append(t2)
            ET.SubElement(seq, q("Ref")).set("ID", tid)
            n += 1
    out = os.path.join(out_dir, "All_figures.pzfx")
    skeleton.write(out, encoding="UTF-8", xml_declaration=True)
    print(f"[combined] wrote {os.path.basename(out)}  ({n} tables)")
    return out


# --------------------------------------------------------------------------- validation
def _cells(col):
    """Yield text of every <d> under a column element (any namespace)."""
    for d in col.iter():
        if local(d.tag) == "d":
            yield (d.text or "").strip()


def validate(out_dir):
    """Pure-Python audit of every .pzfx in out_dir: integrity + cell sanity."""
    files = sorted(f for f in os.listdir(out_dir) if f.endswith(".pzfx"))
    lines = ["# Prism file validation\n", f"_{len(files)} files in `{out_dir}`_\n"]
    n_tables = n_issues = 0
    print(f"[validate] auditing {len(files)} files ...")
    for f in files:
        path = os.path.join(out_dir, f)
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as e:
            lines.append(f"\n## {f}\n- **PARSE ERROR**: {e}\n"); n_issues += 1; continue
        ns = base_namespace(root)
        q = lambda t: ns + t  # noqa: E731
        seq = root.find(q("TableSequence"))
        ref_ids = [r.get("ID") for r in seq.findall(q("Ref"))] if seq is not None else []
        tables = root.findall(q("Table"))
        tbl_ids = [t.get("ID") for t in tables]
        problems = []
        if set(ref_ids) != set(tbl_ids):
            miss = set(ref_ids) - set(tbl_ids)
            orph = set(tbl_ids) - set(ref_ids)
            if miss: problems.append(f"refs without table: {sorted(miss)}")
            if orph: problems.append(f"tables not in sequence: {sorted(orph)}")
        lines.append(f"\n## {f}  ({len(tables)} tables)\n")
        lines.append("| table | type | cols | rows | empty | non-numeric |\n|---|---|--:|--:|--:|--:|\n")
        for t in tables:
            ttl = t.find(q("Title"))
            name = (ttl.text if ttl is not None else t.get("ID")) or t.get("ID")
            ttype = t.get("TableType", "?")
            ycols = t.findall(q("YColumn"))
            rmax = empty = nonnum = 0
            is_xy = ttype == "XY"
            for ci, col in enumerate(list(t.findall(q("XColumn"))) + ycols):
                vals = list(_cells(col))
                rmax = max(rmax, len(vals))
                for v in vals:
                    if v == "":
                        empty += 1
                    else:
                        try:
                            float(v)
                        except ValueError:
                            nonnum += 1
            n_tables += 1
            flag = ""
            if nonnum and not is_xy:
                flag = " ⚠"; n_issues += 1
            lines.append(f"| {name} | {ttype} | {len(ycols)} | {rmax} | {empty} | {nonnum}{flag} |\n")
        if problems:
            n_issues += len(problems)
            lines.append("\n**integrity:** " + "; ".join(problems) + "\n")
    report = os.path.join(out_dir, "VALIDATION.md")
    with open(report, "w", encoding="utf-8") as fh:
        fh.writelines(lines)
    status = "OK" if n_issues == 0 else f"{n_issues} issue(s)"
    print(f"[validate] {n_tables} tables across {len(files)} files -> {status}")
    print(f"[validate] report: {report}")
    return n_issues


# --------------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Hybrid pzfx + prismWriter Prism builder")
    ap.add_argument("--root", default="figure_data", help="data root (default: figure_data)")
    ap.add_argument("--out", default=None, help="output dir (default: <root>/pzfx)")
    ap.add_argument("--skip-r", action="store_true", help="skip stage 1 (base must exist)")
    ap.add_argument("--no-notes", action="store_true", help="stage 1 without provenance Info pages")
    ap.add_argument("--combined", action="store_true", help="also write All_figures.pzfx")
    ap.add_argument("--validate", action="store_true", help="audit outputs after building")
    ap.add_argument("--validate-only", action="store_true", help="only audit existing outputs")
    args = ap.parse_args()

    out = args.out or os.path.join(args.root, "pzfx")

    if args.validate_only:
        sys.exit(1 if validate(out) else 0)

    if not args.skip_r:
        run_r(args.root, out, args.no_notes)

    routing = os.path.join(out, "_routing.csv")
    manifest = os.path.join(out, "MANIFEST.csv")
    if not os.path.exists(routing):
        sys.exit(f"routing file missing: {routing} (run without --skip-r first)")

    with open(routing, newline="") as f:
        rows = list(csv.DictReader(f))
    by_fig = {}
    for r in rows:
        by_fig.setdefault(r["figure"], []).append(r)

    print(f"[stage 2] injecting native Two-Way tables into {len(by_fig)} files ...")
    manifest_add, total = [], 0
    for fig, frows in by_fig.items():
        base = os.path.join(out, f"{fig}.pzfx")
        if not os.path.exists(base):
            print(f"  ! {fig}.pzfx missing, skipping"); continue
        donor = donor_tables_for(fig, frows)
        added = inject(base, donor)
        kinds = {r["out_name"]: r["type"] for r in frows}
        for n in added:
            manifest_add.append(dict(figure=fig, table=n, kind=f"TwoWay({kinds[n]})",
                                     nrow="", ncol="", note="native Prism Grouped table (prismWriter)"))
        total += len(added)
        print(f"  {fig}.pzfx  +{len(added)} TwoWay  ({', '.join(added)})")

    if os.path.exists(manifest) and manifest_add:
        man = pd.read_csv(manifest)
        man = pd.concat([man, pd.DataFrame(manifest_add)], ignore_index=True)
        man.sort_values(["figure", "table"], inplace=True, kind="stable")
        man.to_csv(manifest, index=False)

    print(f"\nDone: injected {total} native Two-Way tables across {len(by_fig)} figures.")

    clean_na(out)   # blank 'NA'/'NaN' data cells across all files (base + injected)

    if args.combined:
        fig_files = [(fig, os.path.join(out, f"{fig}.pzfx"))
                     for fig in by_fig if os.path.exists(os.path.join(out, f"{fig}.pzfx"))]
        # include figures that had no Two-Way injection too
        all_pzfx = sorted(f[:-5] for f in os.listdir(out)
                          if f.endswith(".pzfx") and f != "All_figures.pzfx")
        seen = {fig for fig, _ in fig_files}
        for fig in all_pzfx:
            if fig not in seen:
                fig_files.append((fig, os.path.join(out, f"{fig}.pzfx")))
        fig_files.sort(key=lambda fp: (("SuppFig" in fp[0]), int(re.sub(r"\D", "", fp[0]) or 0)))
        build_combined(out, fig_files)

    if args.validate:
        validate(out)


if __name__ == "__main__":
    main()
