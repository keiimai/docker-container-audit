# --- 第 3 段階で確定記録を照合し、イメージをビルドして、その中の依存を監査する（Python は src/image.py）（README.md「構成」）---

# --- 対象イメージの中で R と Ubuntu を監査し、ログまたは判定を含む詳細を返す（README.md「R」）---
run_r_image_audit <- function(image, directory, baseline, description, platform = "linux/arm64",
                              confirmed, details = FALSE) {
    image <- local_image_id(image, platform)
    manifest <- normalizePath(description, mustWork = TRUE)
    inspector <- normalizePath(project_file(INSPECT_SCRIPT), mustWork = TRUE)
    sources <- c(inspector, manifest, baseline, confirmed, directory)
    log <- file.path(directory, "r-audit.log")
    jsonlite::write_json(list(
        platform = platform, image_id = image, description = readLines(manifest, warn = FALSE)
    ), file.path(directory, "r-input.json"), auto_unbox = TRUE, pretty = TRUE)
    cat("Platform: ", platform, "\n", file = log, append = TRUE, sep = "")
    cat("Docker image ID: ", image, "\n", file = log, append = TRUE, sep = "")
    wait_for_bind_sources(sources, image, platform, log)
    mount <- function(path, target) {
        if (!is.null(path)) c("--mount", bind_mount(normalizePath(path), target, "readonly"))
    }
    user <- paste(system2("id", "-u", stdout = TRUE), system2("id", "-g", stdout = TRUE), sep = ":")
    run_command("docker", c("run", "--rm", "--platform", platform, "--network", "none",
        "--user", user, "--env", "HOME=/tmp", "--env", "TMPDIR=/tmp",
        "--tmpfs", "/tmp:rw,exec,mode=1777",
        "--mount", bind_mount(inspector, "/audit/inspect.R", "readonly"),
        "--mount", bind_mount(manifest, "/audit/DESCRIPTION", "readonly"),
        "--mount", bind_mount(normalizePath(directory), "/result"),
        mount(baseline, "/audit/baseline"), mount(confirmed, "/audit/confirmed"),
        "--entrypoint", "Rscript", image, "--vanilla", "/audit/inspect.R", "audit",
        "/audit/baseline"), log = log)
    report <- file.path(directory, "r-audit.json")
    if (!file_test("-f", report)) stop("監査の記録が見つかりません: ", report, call. = FALSE)
    status <- jsonlite::read_json(report)$status
    if (!details) return(invisible(log))
    review <- ""
    if (identical(status, "REVIEW")) {
        checks <- read.delim(file.path(directory, "audit-checks.tsv"), colClasses = "character")
        review <- paste("要確認:", paste(checks$check[checks$status == "REVIEW"], collapse = ", "))
    }
    list(report = report, log = log, status = status, review = review)
}

# --- 確定記録の見出しと2列のデータを読む（README.md「結果の共通形式」）---
read_lock_entries <- function(lock) {
    entries <- tryCatch(read.delim(lock, colClasses = "character", row.names = NULL,
                                  check.names = FALSE, fill = FALSE),
                        error = function(e) NULL)
    if (!identical(names(entries), c("path", "sha256"))) {
        stop("確定記録の形式が不正です。", call. = FALSE)
    }
    entries
}

# --- 確定記録の中だけで照合し、フォルダと中身を返す（ハッシュ・build-environment.json・ベース一覧・プラットフォーム）（REPRODUCIBILITY.md 3 節）---
verify_r_dependency_lock <- function(lock, platform = NULL) {
    if (basename(lock) != "dependency-lock.tsv") {
        stop("dependency-lock.tsv を指定してください。", call. = FALSE)
    }
    bundle <- dirname(normalizePath(lock, mustWork = TRUE))
    entries <- read_lock_entries(lock)
    if (anyNA(entries) ||
        anyDuplicated(entries$path) || !setequal(entries$path, contract_lock_files("r")) ||
        !all(grepl("^[0-9a-f]{64}$", entries$sha256))) {
        stop("確定記録の形式が不正です。", call. = FALSE)
    }
    for (i in seq_len(nrow(entries))) {
        path <- file.path(bundle, entries$path[[i]])
        if (!file_test("-f", path)) {
            stop("確定後に記録が見つかりません: ", entries$path[[i]], call. = FALSE)
        }
        if (!identical(file_sha256(path), entries$sha256[[i]])) {
            stop("確定後に記録が変更されました: ", path, call. = FALSE)
        }
    }
    environment <- read_build_environment(file.path(bundle, "build-environment.json"))
    if (!identical(environment$language, "r")) {
        stop("R の確定記録ではありません: ", lock, call. = FALSE)
    }
    pattern <- "^rocker/rstudio:[0-9]+[.][0-9]+[.][0-9]+@sha256:[a-f0-9]{64}$"
    if (!grepl(pattern, environment$base_image) || is.null(environment$apt_snapshot) ||
        !nzchar(environment$r_repository %||% "")) {
        stop("確定記録の固定ベース・APT スナップショット・R パッケージの取得元が不正です。",
             call. = FALSE)
    }
    if (!identical(environment$inputs[["DESCRIPTION"]],
                   file_sha256(file.path(bundle, "DESCRIPTION")))) {
        stop("確定記録の DESCRIPTION が、第 1 段階の入力と一致しません。", call. = FALSE)
    }
    validate_base_inventories(bundle, environment$base_image, environment$platform)
    if (!is.null(platform) && !identical(environment$platform, validate_platform(platform))) {
        stop("確定記録のプラットフォームは ", environment$platform, " で、", platform, " ではありません。",
             call. = FALSE)
    }
    list(bundle = bundle, environment = environment)
}

# --- 第 3 段階で確定記録を前後で照合し、projects/<名前>/dockerfile をビルドする（README.md「第 3 段階の中身」）---
build_r_image <- function(image = NULL, lock = NULL, project = DEFAULT_PROJECT, run_id = NULL,
                          platform = "linux/arm64", details = FALSE) {
    validate_platform(platform)
    lock <- required_lock(lock)
    dockerfile <- project_paths(project)$dockerfile
    if (is.null(image) || identical(image, "")) image <- paste0(project, ":local")
    inputs <- c(dockerfile_files(dockerfile), lock_inputs(lock))
    parameters <- list(image = image, project = project, platform = platform,
                       dependency_lock = shown_path(lock))
    run_operation("build-r", parameters, run_id = run_id, inputs = inputs,
                  action = function(directory) {
        log <- file.path(directory, "build.log")
        check <- operation_checks(directory, "build")
        items <- operation_check_items(directory, "build")
        check("Dependency lock", function() {
            items("Dependency lock", lock_item_rows("Dependency lock", lock))
            verify_r_dependency_lock(lock, platform)
        }, detail = shown_path(lock))
        check("Image build", function() {
            run_command("docker", c("buildx", "build", "--load", "--platform", platform,
                "--tag", image, "--file", file.path(dockerfile, "Dockerfile"), dockerfile),
                log = log)
        }, detail = image)
        check("Dependency lock after build", function() {
            items("Dependency lock after build",
                  lock_item_rows("Dependency lock after build", lock))
            verify_r_dependency_lock(lock, platform)
        }, detail = shown_path(lock))
        image_id <- check("Image platform", function() local_image_id(image, platform),
                          detail = identity)
        value <- if (details) {
            list(image = image, image_id = image_id, log = log, status = "PASS",
                 run_id = run_id, evidence = directory)
        } else {
            image
        }
        list(value = value, image_id = image_id)
    })
}
