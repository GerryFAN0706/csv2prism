#!/usr/bin/env Rscript
# =============================================================================
# make_pzfx.R
# Convert tidy <root>/<group>/*.csv data files into EDITABLE GraphPad Prism
# .pzfx files -- one .pzfx per group/figure, one Prism data table per CSV.
#
# GraphPad Prism has no plotting API: you cannot script "draw this axis / colour".
# What you CAN do is hand Prism its native .pzfx (XML) data tables; Prism then
# renders the graph (New Graph -> pick type) and you tweak styling in the GUI.
# This script builds those data tables from your tidy CSV data.
#
# Usage:
#   Rscript make_pzfx.R                 # converts ./figure_data
#   Rscript make_pzfx.R <root_dir>      # converts another dir
#   Rscript make_pzfx.R <root> <out>    # custom output dir
#
# Output: <out>/<group>.pzfx for each group of CSVs (e.g. Fig2.pzfx, Demo.pzfx).
#
# Requires: install.packages("pzfx")   (v0.3.1+)
# =============================================================================

suppressMessages(library(pzfx))

## ---- config -----------------------------------------------------------------
# Usage: Rscript make_pzfx.R [root_dir] [out_dir] [nonotes]
args   <- commandArgs(trailingOnly = TRUE)
root   <- if (length(args) >= 1 && nzchar(args[[1]])) args[[1]] else "figure_data"
outdir <- if (length(args) >= 2 && nzchar(args[[2]])) args[[2]] else file.path(root, "pzfx")
ADD_NOTES <- !(length(args) >= 3 && identical(args[[3]], "nonotes"))  # provenance Info pages
dir.create(outdir, showWarnings = FALSE, recursive = TRUE)
N_DIGITS <- 8L   # display precision in Prism (underlying CSV values are exact)
# write_pzfx builds XML ~O(n^2): ~10 s at 855 rows, ~55 s at 1971. Tables larger
# than this are raw per-subject/per-epoch enrichment dumps (not plotted panels),
# so we skip them from the .pzfx and leave them as CSV (logged in the manifest).
ROW_CAP <- 1000L

if (!dir.exists(root)) stop("figure_data root not found: ", normalizePath(root, mustWork = FALSE))

## ---- helpers ----------------------------------------------------------------

is_charish <- function(v) is.character(v) || is.factor(v)

# Drop all-NA columns (e.g. empty ci_lo/ci_hi placeholders that read as logical NA)
# and coerce any genuine logical column to integer so it stays numeric for Prism.
clean_columns <- function(df) {
  keep <- vapply(df, function(v) any(!is.na(v)), logical(1))
  df   <- df[, keep, drop = FALSE]
  for (c in names(df)) if (is.logical(df[[c]])) df[[c]] <- as.integer(df[[c]])
  df
}

# Prefer the human-readable name and drop its raw code twin, in either spelling:
#   "<x>_display"/"<x>_label" (drop "<x>")  and  "<x>_internal"/"<x>_code" (drop the twin).
drop_redundant_codes <- function(df) {
  for (lab in grep("(_display|_label)$", names(df), value = TRUE)) {
    code <- sub("(_display|_label)$", "", lab)
    if (code %in% names(df)) df[[code]] <- NULL
  }
  for (code in grep("(_internal|_code)$", names(df), value = TRUE)) {
    base <- sub("(_internal|_code)$", "", code)
    if (base %in% names(df)) df[[code]] <- NULL
  }
  df
}

# Append a "<prefix>ci_halfwidth" wherever a matching ci_lo / ci_hi pair exists.
add_ci_halfwidth <- function(df) {
  nm <- names(df)
  los <- grep("ci_lo$", nm, value = TRUE)
  for (lo in los) {
    hi <- sub("ci_lo$", "ci_hi", lo)
    if (hi %in% nm && is.numeric(df[[lo]]) && is.numeric(df[[hi]])) {
      pref <- sub("ci_lo$", "", lo)
      df[[paste0(pref, "ci_halfwidth")]] <- (df[[hi]] - df[[lo]]) / 2
    }
  }
  df
}

# Decide whether a multi-label table is a genuine crossing (entity x factor)
# that should pivot to grouped-wide, vs. an entity with a 1:1 attribute (which
# should NOT pivot). The grouping factor is the lowest-cardinality char column;
# it qualifies only if the remaining entity actually REPEATS across its levels
# (a real crossing) and each (entity, level) pair is unique (a clean pivot).
choose_pivot <- function(df, char_cols) {
  if (length(char_cols) < 2) return(NULL)
  best <- NULL; best_lev <- Inf
  for (C in char_cols) {
    entity <- setdiff(char_cols, C)
    ent_id <- do.call(paste, c(lapply(entity, function(x) as.character(df[[x]])), sep = " | "))
    n_ent  <- length(unique(ent_id)); n_lev <- length(unique(df[[C]]))
    if (n_ent >= nrow(df)) next                                   # C doesn't collapse rows -> attribute
    if (nrow(df) != n_ent * n_lev) next                          # not a balanced complete crossing
    if (anyDuplicated(data.frame(id = ent_id, lv = as.character(df[[C]])))) next
    if (n_lev < best_lev) { best_lev <- n_lev; best <- list(factor = C, entity = entity) }
  }
  best                                              # smallest-level balanced crossing, or NULL
}

# Long -> wide: each numeric value column becomes value__<level> columns.
pivot_wide <- function(df, char_cols, spread_col, value_cols) {
  rest <- setdiff(char_cols, spread_col)
  id   <- if (length(rest))
    do.call(paste, c(lapply(rest, function(x) as.character(df[[x]])), sep = " | "))
  else rep("row", nrow(df))
  ids  <- unique(id)
  levs <- unique(as.character(df[[spread_col]]))
  out  <- data.frame(row.names = ids, check.names = FALSE)
  for (vc in value_cols) {
    for (lv in levs) {
      sel <- as.character(df[[spread_col]]) == lv
      out[[paste0(vc, "__", lv)]] <- df[[vc]][sel][match(ids, id[sel])]
    }
  }
  out
}

# Build a safe, descriptive Prism table name from a CSV path.
table_name <- function(path) {
  b <- sub("\\.csv$", "", basename(path))
  b <- sub("^(Supp)?Fig[0-9]+_", "", b)      # strip "Fig3_" / "SuppFig5_"
  substr(b, 1, 60)
}

# The point-estimate column among numeric cols (not a CI / p / n / SE column).
estimate_col <- function(num_cols) {
  bad  <- grepl("(^|_)ci_(lo|hi|halfwidth)$|^p$|^n$|(^|_)se$|^count$", num_cols, ignore.case = TRUE)
  keep <- num_cols[!bad]
  if (length(keep)) keep[1] else num_cols[1]
}

# Square numeric block keyed by one label column -> a heatmap-ready matrix.
is_square_matrix <- function(char_cols, num_cols, nrow_) {
  length(char_cols) == 1 && length(num_cols) >= 3 && nrow_ == length(num_cols)
}

## ---- core: one CSV -> one Prism-ready data frame + table metadata -----------

build_table <- function(path) {
  df <- read.csv(path, check.names = FALSE, stringsAsFactors = FALSE)
  if (nrow(df) == 0 || ncol(df) == 0) return(NULL)
  df <- drop_redundant_codes(df)
  df <- clean_columns(df)
  df <- add_ci_halfwidth(df)

  nm        <- names(df)
  char_cols <- nm[vapply(df, is_charish, logical(1))]
  num_cols  <- setdiff(nm, char_cols)
  fname     <- basename(path)

  meta <- list(x_col = NA_integer_, row_names = FALSE, kind = "column", note = "",
               route_type = NA_character_, groupby = NA_character_,
               entity = NA_character_, value = NA_character_, row_label = NA_character_)

  # ---- Case A: no labels -> XY if first column is a clean ascending axis -----
  if (length(char_cols) == 0) {
    if (length(num_cols) >= 2 && !anyDuplicated(df[[1]]) &&
        isTRUE(all.equal(order(df[[1]]), seq_len(nrow(df))))) {
      meta$x_col <- 1L; meta$kind <- "xy"; meta$note <- "XY: col1 = X axis"
    } else {
      meta$note <- "Column (numeric matrix / heatmap-ready)"
    }
    return(list(data = df, meta = meta))
  }

  # ---- Case B: scatter "<x>_vs_<y>" with point labels -----------------------
  if (grepl("_vs_", fname) && length(char_cols) == 1 && length(num_cols) >= 2) {
    rn  <- as.character(df[[char_cols]])
    dat <- df[, num_cols, drop = FALSE]
    rownames(dat) <- make.unique(rn, sep = " #")
    meta$x_col <- 1L; meta$row_names <- TRUE; meta$kind <- "xy"
    meta$note  <- paste0("XY scatter: X = ", num_cols[1], ", points labelled by ", char_cols)
    return(list(data = dat, meta = meta))
  }

  # ---- Case C: single label column -> Column table (rows = that label) -------
  if (length(char_cols) == 1) {
    dat <- df[, num_cols, drop = FALSE]
    rownames(dat) <- make.unique(as.character(df[[char_cols]]), sep = " #")
    meta$row_names <- TRUE; meta$note <- paste0("Column: rows = ", char_cols)
    if (is_square_matrix(char_cols, num_cols, nrow(df))) {  # also offer a native heatmap table
      meta$route_type <- "heatmap"; meta$row_label <- char_cols
      meta$note <- paste0(meta$note, "  (+ heatmap)")
    }
    return(list(data = dat, meta = meta))
  }

  # ---- Case D: genuine entity x factor crossing -> pivot to grouped-wide -----
  pv <- choose_pivot(df, char_cols)
  if (!is.null(pv)) {
    dat <- pivot_wide(df, char_cols, pv$factor, num_cols)
    meta$row_names <- TRUE; meta$kind <- "grouped"
    meta$note <- paste0("Grouped (pivoted): rows = ",
                        paste(pv$entity, collapse = " | "), ", columns split by ", pv$factor)
    meta$route_type <- "grouped"; meta$groupby <- pv$factor
    meta$entity <- paste(pv$entity, collapse = "|"); meta$value <- estimate_col(num_cols)
    return(list(data = dat, meta = meta))
  }

  # ---- Case E: multiple labels, no clean crossing -> composite-label column --
  lab <- do.call(paste, c(lapply(char_cols, function(c) as.character(df[[c]])), sep = " | "))
  if (!anyDuplicated(lab)) {
    dat <- df[, num_cols, drop = FALSE]
    rownames(dat) <- lab
    meta$row_names <- TRUE
    meta$note <- paste0("Column: rows = ", paste(char_cols, collapse = " | "))
    return(list(data = dat, meta = meta))
  }

  # ---- Fallback: suffix-deduplicate the combined label -----------------------
  dat <- df[, num_cols, drop = FALSE]
  rownames(dat) <- make.unique(lab, sep = " #")
  meta$row_names <- TRUE; meta$note <- "Column (labels de-duplicated)"
  list(data = dat, meta = meta)
}

## ---- driver: one figure folder -> one .pzfx --------------------------------

convert_folder <- function(folder) {
  fig   <- basename(folder)
  csvs  <- sort(list.files(folder, pattern = "\\.csv$", full.names = TRUE))
  if (!length(csvs)) return(NULL)

  tables <- list(); x_cols <- integer(0); rownms <- logical(0); rows <- list()
  route  <- list(); notes <- list()
  for (p in csvs) {
    tb <- tryCatch(build_table(p), error = function(e) {
      message("  ! ", basename(p), ": ", conditionMessage(e)); NULL
    })
    if (is.null(tb)) next
    nm <- table_name(p)
    if (nrow(tb$data) > ROW_CAP) {
      message(sprintf("  - skip %-40s %d rows (raw data; kept as CSV)", nm, nrow(tb$data)))
      rows[[nm]] <- data.frame(figure = fig, table = nm, kind = "skipped(raw)",
                               nrow = nrow(tb$data), ncol = ncol(tb$data),
                               note = "too large to plot; use the CSV directly",
                               stringsAsFactors = FALSE)
      next
    }
    tables[[nm]] <- tb$data
    x_cols       <- c(x_cols, tb$meta$x_col)
    rownms       <- c(rownms, tb$meta$row_names)
    rows[[nm]]   <- data.frame(figure = fig, table = nm, kind = tb$meta$kind,
                               nrow = nrow(tb$data), ncol = ncol(tb$data),
                               note = tb$meta$note, stringsAsFactors = FALSE)
    notes[[nm]]  <- data.frame(
      Name  = c("Table", "Source CSV", "Layout", "Generated by"),
      Value = c(nm, p, tb$meta$note, "csv2prism (pzfx + prismWriter)"),
      stringsAsFactors = FALSE)
    # routing for the Python/prismWriter stage (native TwoWay tables)
    if (!is.na(tb$meta$route_type)) {
      suffix <- if (tb$meta$route_type == "grouped") "__grouped" else "__heatmap"
      route[[nm]] <- data.frame(figure = fig, csv = p, out_name = paste0(nm, suffix),
                                type = tb$meta$route_type, groupby = tb$meta$groupby,
                                entity = tb$meta$entity, value = tb$meta$value,
                                row_label = tb$meta$row_label, stringsAsFactors = FALSE)
    }
  }
  if (!length(tables)) return(NULL)

  out <- file.path(outdir, paste0(fig, ".pzfx"))
  if (ADD_NOTES) {
    write_pzfx(tables, path = out, row_names = rownms, x_col = x_cols,
               n_digits = N_DIGITS, notes = notes)
  } else {
    write_pzfx(tables, path = out, row_names = rownms, x_col = x_cols, n_digits = N_DIGITS)
  }
  message(sprintf("  wrote %-14s  (%d tables)", basename(out), length(tables)))
  list(manifest = do.call(rbind, rows), routing = do.call(rbind, route))
}

## ---- run --------------------------------------------------------------------

has_csv <- function(d) length(list.files(d, pattern = "\\.csv$")) > 0

run_all <- function() {
all_dirs <- list.dirs(root, recursive = FALSE)
folders  <- all_dirs[grepl("(^|/)(Supp)?Fig[0-9]+$", all_dirs)]
if (length(folders)) {
  # numeric-aware ordering: Fig2..Fig6 then SuppFig1..SuppFig10
  ord <- order(grepl("SuppFig", basename(folders)),
               as.integer(sub("\\D+", "", basename(folders))))
  folders <- folders[ord]
} else {
  # generic mode: any subfolders with CSVs, else the root itself as a single figure
  folders <- all_dirs[vapply(all_dirs, has_csv, logical(1))]
  if (!length(folders) && has_csv(root)) folders <- root
  if (!length(folders)) stop("no CSV files found under ", normalizePath(root))
  message("(generic mode: no Fig*/SuppFig* folders; treating ", length(folders),
          " CSV folder(s) as figures)")
}

message("Converting ", length(folders), " figure folders -> ", normalizePath(outdir), "\n")
res      <- lapply(folders, convert_folder)
manifest <- do.call(rbind, lapply(res, `[[`, "manifest"))
routing  <- do.call(rbind, lapply(res, `[[`, "routing"))

mpath <- file.path(outdir, "MANIFEST.csv")
write.csv(manifest, mpath, row.names = FALSE)
message("\nManifest: ", mpath, "  (", nrow(manifest), " tables across ",
        length(unique(manifest$figure)), " figures)")

rpath <- file.path(outdir, "_routing.csv")
if (!is.null(routing)) {
  write.csv(routing, rpath, row.names = FALSE)
  message("Routing : ", rpath, "  (", nrow(routing), " native TwoWay tables for build_prism.py: ",
          sum(routing$type == "grouped"), " grouped + ", sum(routing$type == "heatmap"), " heatmap)")
}
}

if (sys.nframe() == 0L) run_all()
