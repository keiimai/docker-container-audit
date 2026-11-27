#!/usr/bin/env Rscript
# --- Git が追跡する R と Quarto のファイルを、骨格の .lintr で lint する（Python は ruff check）（CHECKS.md 2 節）---

# --- R の子の根で走らせる（git ls-files が子の根からのパスで返す）（CHECKS.md 2 節）---
if (!file.exists(".lintr")) stop("R の子の根で実行してください。", call. = FALSE)
# --- 追跡中の R と Quarto のファイル（git が使えなければフォルダから探す）（CHECKS.md 2 節）---
files <- suppressWarnings(system2("git", c("ls-files", "*.R", "*.qmd"),
                                  stdout = TRUE, stderr = FALSE))
if (!is.null(attr(files, "status")) || !length(files)) {
    files <- list.files(c("R", "tools", "tests", "notebooks", "projects"),
                        pattern = "[.](R|qmd)$",
                        recursive = TRUE, full.names = TRUE)
}
# --- lint して件数を出し、指摘があれば終了コード 1 で終える（CHECKS.md 2 節）---
lints <- structure(do.call(c, lapply(files, lintr::lint)), class = "lints")
print(lints)
cat(length(lints), "件の指摘（", length(files), " ファイル）\n", sep = "")
if (length(lints)) quit(status = 1L)
