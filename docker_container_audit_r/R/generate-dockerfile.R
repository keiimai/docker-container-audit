# --- 確定記録からビルド用ファイルを生成する（Python は src/generate_dockerfile.py）（README.md「第 2 段階の中身」）---

# --- 確定記録を今の第 1 段階の入力と照らし合わせる（README.md「第 2 段階の中身」）---
.generate_verify_request <- function(environment, request) {
    expected <- lapply(target_inputs(request), file_sha256)
    recorded <- environment$inputs
    for (name in union(names(expected), "ubuntu-packages.txt")) {
        if (!identical(expected[[name]], recorded[[name]])) {
            stop("第 1 段階の確定記録と request/ が一致しません: ", name, call. = FALSE)
        }
    }
    base <- target_config(request)
    if (!identical(base[["BASE_IMAGE"]], environment$base_image)) {
        stop("第 1 段階の確定記録と request/base.txt の BASE_IMAGE が一致しません。",
             call. = FALSE)
    }
    if (!identical(base[["R_REPOSITORY"]], environment$r_repository)) {
        stop("第 1 段階の確定記録と request/base.txt の R_REPOSITORY が一致しません。",
             call. = FALSE)
    }
}

# --- 確定した固定ベースと取得元の値をひな形に入れる（README.md「第 2 段階の中身」）---
.generate_render_dockerfile <- function(environment, destination) {
    base_pattern <- "\\Arocker/rstudio:[0-9]+[.][0-9]+[.][0-9]+@sha256:[a-f0-9]{64}\\z"
    repo_pattern <- paste0("\\Ahttps://[A-Za-z0-9._~:/%+@?&=;,()*!\\[\\]-]+/",
                           "[0-9]{4}-[0-9]{2}-[0-9]{2}\\z")
    if (!grepl(base_pattern, environment$base_image, perl = TRUE)) {
        stop("確定記録の固定ベースが不正です。", call. = FALSE)
    }
    if (!grepl("\\A[0-9]{8}T[0-9]{6}Z\\z", environment$apt_snapshot, perl = TRUE)) {
        stop("確定記録の APT スナップショットが不正です。", call. = FALSE)
    }
    if (!grepl(repo_pattern, environment$r_repository, perl = TRUE)) {
        stop("確定記録の R パッケージの取得元が不正です。", call. = FALSE)
    }
    template <- project_file(file.path("dockerfile-templates", "Dockerfile.generate"))
    lines <- readLines(template, encoding = "UTF-8")
    values <- c(BASE_IMAGE = environment$base_image, APT_SNAPSHOT = environment$apt_snapshot,
                R_REPOSITORY = environment$r_repository)
    for (name in names(values)) {
        marker <- paste0("@@", name, "@@")
        prefix <- paste0("ARG ", name, "=")
        row <- which(lines == paste0(prefix, marker))
        occurrences <- sum(lengths(regmatches(lines, gregexpr(marker, lines, fixed = TRUE))))
        if (length(row) != 1L || occurrences != 1L) {
            stop("Dockerfile のひな形に ", name, " の差し込み先が 1 つ必要です。", call. = FALSE)
        }
        lines[[row]] <- paste0(prefix, values[[name]])
    }
    writeLines(enc2utf8(lines), destination, useBytes = TRUE)
}

# --- 対象の dockerfile/ に Dockerfile・固定版の一覧・導入スクリプトをそろえる（README.md「第 2 段階の中身」）---
generate_r_dockerfile <- function(project, replace = FALSE) {
    paths <- project_paths(project)
    config <- load_project_config(project)
    lock <- required_lock(config_text(config$image$dependency_lock, "image.dependency_lock"))
    if (!file_test("-f", lock)) stop("確定記録が見つかりません: ", lock, call. = FALSE)
    lock_hash <- file_sha256(lock)
    verified <- verify_r_dependency_lock(lock, config$platform)
    .generate_verify_request(verified$environment, paths$request)
    entries <- read_lock_entries(lock)
    pins <- c("ubuntu-pins.txt", "r-pins.tsv")
    installer <- project_file(file.path("tools", "install-locked-r-dependencies.R"))
    publisher <- file.path(dirname(audit_root()), "tools", "publish-dockerfile.sh")
    with_work_directory("generate-dockerfile-", function(work) {
        .generate_render_dockerfile(verified$environment, file.path(work, "Dockerfile"))
        if (!all(file.copy(file.path(verified$bundle, pins), work)) ||
            !file.copy(installer, file.path(work, "install-dependencies.R"))) {
            stop("ビルド用ファイルを用意できませんでした。", call. = FALSE)
        }
        for (name in pins) {
            if (!identical(file_sha256(file.path(work, name)),
                           entries$sha256[match(name, entries$path)])) {
                stop("確定後に記録が変更されました: ", file.path(verified$bundle, name),
                     call. = FALSE)
            }
        }
        if (!identical(file_sha256(lock), lock_hash)) {
            stop("確定後に記録が変更されました: ", lock, call. = FALSE)
        }
        .generate_verify_request(verified$environment, paths$request)
        args <- c(publisher, work, paths$dockerfile, if (replace) "--replace")
        errors <- file.path(work, "publish.stderr")
        if (system2("bash", shQuote(args), stderr = errors) != 0L) {
            reasons <- readLines(errors, warn = FALSE, encoding = "UTF-8")
            if (!length(reasons)) reasons <- "ビルド用ファイルを保存できませんでした。"
            stop(paste(reasons, collapse = "\n"), call. = FALSE)
        }
    })
    invisible(paths$dockerfile)
}

# --- 検査用のイメージの中で引数を読み、対象のビルド用ファイルを生成する（README.md「第 2 段階の中身」）---
.generate_main <- function(args = commandArgs(trailingOnly = TRUE)) {
    usage <- paste("使い方: bash tools/runner.sh run Rscript --vanilla",
                   "R/generate-dockerfile.R <プロジェクト名> [--replace]")
    if (identical(args, "--help")) {
        cat(usage, "\n", sep = "")
        return(invisible(NULL))
    }
    if (!length(args) || startsWith(args[[1L]], "-") ||
        (length(args) > 1L && !identical(args[-1L], "--replace"))) {
        stop(usage, call. = FALSE)
    }
    if (!identical(Sys.getenv("AUDIT_RUNNER"), "r")) {
        stop("検査用のイメージの中で実行してください。\n", usage, call. = FALSE)
    }
    files <- c("repro.R", "project.R", "container.R", "contract.R", "discovery.R", "image.R")
    for (file in files) sys.source(file.path("R", file), envir = environment(.generate_main))
    destination <- generate_r_dockerfile(args[[1L]], replace = length(args) == 2L)
    cat("ビルド用ファイルを用意しました: ", shown_path(destination), "\n", sep = "")
}

if (sys.nframe() == 0L) {
    tryCatch(.generate_main(), error = function(error) {
        message(conditionMessage(error))
        quit(save = "no", status = 1L)
    })
}
