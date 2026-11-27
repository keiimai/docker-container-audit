# --- 検査用の R パッケージ（renv-pins.tsv）を、SHA-256 と照合して入れる（Python の uv sync --locked）（README.md「R」）---

# --- 引数の renv-pins.tsv を読む（列は package・version・sha256）（README.md「R」）---
args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 1L) stop("Expected a renv-pins.tsv path")
pins <- read.delim(args[[1L]], colClasses = "character")
if (!identical(names(pins), c("package", "version", "sha256")) || anyNA(pins) || !nrow(pins) ||
    anyDuplicated(pins$package) || !all(grepl("^[0-9a-f]{64}$", pins$sha256))) {
    stop("renv-pins.tsv needs unique packages with the columns package, version and sha256")
}
# --- R パッケージの取得元（Dockerfile.runner の ARG R_REPOSITORY）（README.md「R」）---
repo <- Sys.getenv("R_REPOSITORY")
if (!nzchar(repo)) stop("R repository is not specified")
# --- P3M のバイナリを受け取る HTTPUserAgent にし、警告はエラーにする（README.md「R」）---
version <- getRversion()
platform <- paste(R.version$platform, R.version$arch, R.version$os)
options(HTTPUserAgent = sprintf("R/%s R (%s %s)", version, version, platform),
        download.file.method = "libcurl", timeout = 600, warn = 2)
# --- 取得元の版が固定と同じか確かめ、配布物を取って SHA-256 と照合した手元の取得元を作る（README.md「R」）---
available <- available.packages(repos = repo, type = "source")
if (!all(pins$package %in% rownames(available)) ||
    !identical(unname(available[pins$package, "Version"]), pins$version)) {
    stop("The repository does not match the pinned R versions")
}
local <- tempfile("r-verified-")
contrib <- file.path(local, "src", "contrib")
dir.create(contrib, recursive = TRUE)
archives <- download.packages(pins$package, destdir = contrib, available = available, repos = repo,
                              type = "source")
digests <- sub("[[:space:]].*$", "", system2("sha256sum", shQuote(archives[, 2L]), stdout = TRUE))
actual <- digests[match(pins$package, archives[, 1L])]
if (!identical(actual, pins$sha256)) {
    stop("Downloaded R packages do not match the pinned SHA-256: ",
         paste(pins$package[is.na(actual) | actual != pins$sha256], collapse = ", "))
}
tools::write_PACKAGES(contrib, type = "source")
# --- 照合した配布物だけを入れ、入った版を確かめる（README.md「R」）---
install.packages(pins$package, repos = paste0("file://", local), dependencies = FALSE,
                 type = "source")
installed <- installed.packages(noCache = TRUE)
if (!all(pins$package %in% rownames(installed)) ||
    !identical(unname(installed[pins$package, "Version"]), pins$version)) {
    stop("Installed R versions do not match the pinned versions")
}
unlink(local, recursive = TRUE)
