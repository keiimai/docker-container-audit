# --- 第 3 段階の R パッケージ導入（Python は Dockerfile.generate）（README.md「第 3 段階の中身」）---

# --- 引数の r-pins.tsv を読む（列は package・version・sha256）（README.md「第 3 段階の中身」）---
args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 1L) stop("r-pins.tsv のパスを 1 つ指定してください。", call. = FALSE)
pins <- read.delim(args[[1L]], colClasses = "character")
if (!identical(names(pins), c("package", "version", "sha256")) || anyNA(pins) ||
    anyDuplicated(pins$package) || !all(grepl("^[0-9a-f]{64}$", pins$sha256))) {
    stop("r-pins.tsv の列は package、version、sha256 とし、パッケージ名は重ねないでください。",
         call. = FALSE)
}
# --- R パッケージの取得元（Dockerfile の ARG R_REPOSITORY）（README.md「第 3 段階の中身」）---
repo <- Sys.getenv("R_REPOSITORY")
if (!nzchar(repo)) stop("R パッケージの取得元を指定してください。", call. = FALSE)
# --- P3M のバイナリを受け取る HTTPUserAgent にし、警告はエラーにする（README.md「第 3 段階の中身」）---
version <- getRversion()
platform <- paste(R.version$platform, R.version$arch, R.version$os)
options(HTTPUserAgent = sprintf("R/%s R (%s %s)", version, version, platform),
        download.file.method = "libcurl", timeout = 600, warn = 2)
# --- 導入を 3 回まで試す（P3M との通信の一時的な失敗に備える）（README.md「第 3 段階の中身」）---
.install_with_retry <- function(...) {
    for (attempt in 1:3) {
        done <- tryCatch({
            install.packages(...)
            TRUE
        }, error = function(e) {
            if (attempt == 3L) stop(e)
            message("R パッケージの導入をやり直します（", attempt + 1L, " 回目）: ",
                    conditionMessage(e))
            FALSE
        })
        if (done) return(invisible(TRUE))
    }
}
# --- 確定した版を取得し、SHA-256 と照合した手元の取得元を作る（README.md「第 3 段階の中身」）---
.install_verified_repository <- function(pins, snapshot) {
    local <- tempfile("r-verified-")
    contrib <- file.path(local, "src", "contrib")
    dir.create(contrib, recursive = TRUE)
    archives <- matrix(character(), ncol = 2L)
    for (attempt in 1:3) {
        remaining <- setdiff(pins$package, archives[, 1L])
        if (!length(remaining)) break
        archives <- rbind(archives, tryCatch(download.packages(
            remaining, destdir = contrib, available = snapshot, repos = repo, type = "source"
        ), error = function(e) {
            message("R パッケージの取得をやり直します（", attempt + 1L, " 回目）: ",
                    conditionMessage(e))
            matrix(character(), ncol = 2L)
        }))
    }
    if (!setequal(pins$package, archives[, 1L])) {
        stop("確定した R パッケージを取得できませんでした。", call. = FALSE)
    }
    digests <- system2("sha256sum", shQuote(archives[, 2L]), stdout = TRUE)
    digests <- sub("[[:space:]].*$", "", digests)
    actual <- digests[match(pins$package, archives[, 1L])]
    if (!identical(actual, pins$sha256)) {
        stop("取得した R パッケージが確定記録の SHA-256 と一致しません: ",
             paste(pins$package[actual != pins$sha256], collapse = ", "), call. = FALSE)
    }
    tools::write_PACKAGES(contrib, type = "source")
    paste0("file://", local)
}
# --- 確定した版だけを入れ、入った版を照合する（README.md「第 3 段階の中身」）---
if (nrow(pins)) {
    snapshot <- available.packages(repos = repo, type = "source")
    if (!all(pins$package %in% rownames(snapshot)) ||
        !identical(unname(snapshot[pins$package, "Version"]), pins$version))
        stop("取得元の R パッケージの版が確定記録と一致しません。", call. = FALSE)
    local <- .install_verified_repository(pins, snapshot)
    .install_with_retry(pins$package, repos = local, dependencies = FALSE, type = "source")
    versions <- installed.packages(noCache = TRUE)
    if (!all(pins$package %in% rownames(versions)) ||
        !identical(unname(versions[pins$package, "Version"]), pins$version))
        stop("導入した R パッケージの版が確定記録と一致しません。", call. = FALSE)
    unlink(sub("^file://", "", local), recursive = TRUE)
}
# --- 照合した版と SHA-256 をイメージに残す（README.md「第 3 段階の中身」）---
write.table(pins, file.path(R.home(), "r-install-report.tsv"), sep = "\t", row.names = FALSE,
            quote = FALSE)
