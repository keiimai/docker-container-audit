#!/usr/bin/env Rscript
# --- 操作側のパッケージの版を renv.lock に、配布物の SHA-256 を renv-pins.tsv に固定する（Python は uv lock）（README.md「R」）---

# --- 本番（記録層・報告書）で使うパッケージと、固定を作る renv（検査対象の DESCRIPTION とは別）（REPRODUCIBILITY.md 5 節）---
packages <- c(
    "digest", "jsonlite", "yaml",
    "knitr", "rmarkdown",
    "renv"
)
# --- 開発（テスト・lint）で使うパッケージ（Python の dependency-groups の dev にあたる）（REPRODUCIBILITY.md 5 節）---
dev_packages <- c(
    "testthat", "withr",
    "lintr"
)
# --- 使う R の版（検査用の固定ベースの版。Python の requires-python にあたる）（README.md「R」）---
r_version <- "4.5"
# --- 配布物の SHA-256 を書く表（列は package・version・sha256。見本の r-pins.tsv と同じ形）（README.md「R」）---
pins_file <- "renv-pins.tsv"

# --- renv があり、R の子の根で、決めた版の R で走らせているか確かめる（README.md「テスト」）---
if (!requireNamespace("renv", quietly = TRUE)) {
    stop("renv が必要です: install.packages(\"renv\")", call. = FALSE)
}
if (!file.exists("tools/lock-r-environment.R")) stop("R の子の根で実行してください。", call. = FALSE)
if (!identical(paste(R.version$major, sub("\\..*$", "", R.version$minor), sep = "."), r_version)) {
    stop("R ", r_version, " で実行してください（今は ", getRversion(), "）。", call. = FALSE)
}
# --- パッケージの取得元（検査用の Dockerfile.runner の R_REPOSITORY と同じ、日付で固定した P3M）（README.md「R」）---
repository <- sub("^ARG R_REPOSITORY=", "",
                  grep("^ARG R_REPOSITORY=", readLines("Dockerfile.runner", warn = FALSE),
                       value = TRUE))
if (length(repository) != 1L) {
    stop("Dockerfile.runner に ARG R_REPOSITORY が 1 つだけ必要です。", call. = FALSE)
}
repos <- c(CRAN = repository)
# --- 引数を読み、取得元と、P3M のバイナリを受け取る HTTPUserAgent を設定する（README.md「R」）---
args <- commandArgs(trailingOnly = TRUE)
running_version <- getRversion()
platform <- paste(R.version$platform, R.version$arch, R.version$os)
options(repos = repos, download.file.method = "libcurl", timeout = 600,
        HTTPUserAgent = sprintf("R/%s R (%s %s)", running_version, running_version, platform))

# --- 取得元から配布物を取り、パッケージごとの SHA-256 を返す（取得元の版と違えば止める）（README.md「R」）---
archive_sha256 <- function(versions) {
    available <- available.packages(repos = repos, type = "source")
    current <- unname(available[names(versions), "Version"])
    if (anyNA(current) || !identical(current, unname(versions))) {
        stop("取得元の版が renv.lock と違います: ",
             paste(names(versions)[is.na(current) | current != versions], collapse = ", "),
             call. = FALSE)
    }
    destination <- tempfile("renv-pins-")
    dir.create(destination)
    on.exit(unlink(destination, recursive = TRUE))
    archives <- download.packages(names(versions), destdir = destination, available = available,
                                  repos = repos, type = "source", quiet = TRUE)
    digests <- sub("[[:space:]].*$", "", system2("sha256sum", shQuote(archives[, 2L]),
                                                 stdout = TRUE))
    digests[match(names(versions), archives[, 1L])]
}

# --- renv.lock のパッケージの版を、名前付きの文字で返す（README.md「R」）---
lock_versions <- function() {
    lock <- jsonlite::read_json("renv.lock")
    vapply(lock$Packages, function(x) x$Version, character(1))
}

# --- --check なら照合し、引数が無ければ renv.lock と renv-pins.tsv を作り直す（CHECKS.md 1 節）---
if (identical(args, "--check")) {
    lock <- jsonlite::read_json("renv.lock")
    recorded <- lock_versions()
    sources <- vapply(lock$R$Repositories, function(x) x$URL, character(1))
    missing <- setdiff(c(packages, dev_packages), names(recorded))
    installed <- vapply(names(recorded), function(name) {
        path <- find.package(name, quiet = TRUE)
        if (length(path)) {
            read.dcf(file.path(path[[1L]], "DESCRIPTION"), fields = "Version")[[1L]]
        } else {
            NA_character_
        }
    }, character(1))
    different <- names(recorded)[is.na(installed) | installed != recorded]
    pins <- read.delim(pins_file, colClasses = "character")
    pinned <- setNames(pins$version, pins$package)
    problems <- c(
        if (!identical(lock$R$Version, as.character(getRversion()))) {
            sprintf("  R の版: lock %s / 実行 %s\n", lock$R$Version, getRversion())
        },
        if (!identical(unname(sources), sub("/__linux__/[^/]+/", "/", unname(repos)))) {
            sprintf("  取得元: lock %s / Dockerfile.runner %s\n",
                    paste(sources, collapse = ", "), repos)
        },
        if (length(missing)) {
            paste0("  renv.lock にない: ", paste(missing, collapse = ", "), "\n")
        },
        if (length(different)) paste0("  版が違う・入っていない: ",
                                      paste(sprintf("%s（lock %s / 実行 %s）", different,
                                                    recorded[different], installed[different]),
                                            collapse = ", "), "\n"),
        if (!identical(pinned[sort(names(pinned))], recorded[sort(names(recorded))])) {
            paste0("  ", pins_file, " の版が renv.lock と違う\n")
        }
    )
    if (length(problems)) stop("renv.lock と一致しません。\n", problems, call. = FALSE)
    cat("renv.lock は操作側の環境と一致しています（", length(recorded), " パッケージ）。\n", sep = "")
} else if (!length(args)) {
    loadNamespace("renv")
    lock_library <- file.path(tempdir(), "lock-library")
    dir.create(lock_library)
    .libPaths(c(lock_library, .Library))
    available <- available.packages(repos = repos, type = "source")
    base <- rownames(installed.packages(priority = "base"))
    needed <- unique(c(packages, dev_packages, unlist(tools::package_dependencies(
        c(packages, dev_packages), db = available, recursive = TRUE,
        which = c("Depends", "Imports", "LinkingTo")
    ))))
    install.packages(setdiff(needed, base), lib = lock_library, dependencies = FALSE,
                     type = "source")
    renv::snapshot(project = getwd(), library = lock_library, lockfile = "renv.lock",
                   packages = c(packages, dev_packages), prompt = FALSE)
    versions <- lock_versions()
    write.table(data.frame(package = names(versions), version = unname(versions),
                           sha256 = archive_sha256(versions)),
                pins_file, sep = "\t", row.names = FALSE, quote = FALSE)
} else {
    stop("引数は無しか --check です。", call. = FALSE)
}
