# --- 第 1 段階の依存の調査と確定を行う（Python は src/discovery.py）（README.md「構成」）---

# --- APT スナップショットの指定を確かめ、使う時点を返す（指定が無ければ今の時点）（README.md「R」）---
checked_apt_snapshot <- function(apt_snapshot) {
    if (is.null(apt_snapshot) || identical(apt_snapshot, "")) apt_snapshot <- new_apt_snapshot()
    if (!grepl("^[0-9]{8}T[0-9]{6}Z$", apt_snapshot)) {
        stop("APT スナップショットは YYYYMMDDTHHMMSSZ の形で指定してください。", call. = FALSE)
    }
    apt_snapshot
}

# --- R の調査の環境（Dockerfile.inspect）をビルドし、指定した段の結果を destination に書き出す（README.md「R」）---
run_inspect_build <- function(destination, context, platform, target, log,
                              packages = character(), extra = character(), apt_snapshot = NULL) {
    validate_platform(platform)
    config <- target_config(context)
    base <- config[["BASE_IMAGE"]]
    run_command("docker", c("pull", "--platform", platform, base), log = log)
    with_work_directory("inspect-context-", function(work) {
        writeLines(packages, file.path(work, "packages.txt"))
        writeLines(extra, file.path(work, "ubuntu-packages.txt"))
        build_args <- c(BASE_IMAGE = base, RSTUDIO_TAG = config[["RSTUDIO_TAG"]],
                        RSTUDIO_SHA256 = config[["RSTUDIO_SHA256"]],
                        R_REPOSITORY = config[["R_REPOSITORY"]],
                        APT_SNAPSHOT = if (is.null(apt_snapshot)) "" else apt_snapshot)
        output <- gsub('"', '""', normalizePath(destination, mustWork = FALSE), fixed = TRUE)
        run_command("docker", c(
            "buildx", "build", "--target", target, "--platform", platform,
            as.vector(rbind("--build-arg", paste0(names(build_args), "=", build_args))),
            "--build-context", paste0("audit_tools=", normalizePath(project_file("R"))),
            "--file", normalizePath(project_file(INSPECT_DOCKERFILE)),
            "--output", paste0('type=local,"dest=', output, '"'), work
        ), log = log)
    })
    invisible(destination)
}

# --- ベース一覧（R と Ubuntu）のパス（README.md「R」）---
base_inventory_paths <- function(directory) {
    file.path(directory, c("base-r.tsv", "base-ubuntu.tsv"))
}

# --- ベース一覧の 1 行目にあるプラットフォーム（Python は build-environment.json の _discovery_platform）（README.md「R」）---
inventory_platform <- function(directory) {
    header <- readLines(base_inventory_paths(directory)[[1L]], n = 1L, warn = FALSE)
    validate_platform(sub("^.* ", "", header))
}

# --- ベース一覧の 1 行目から固定ベースを読む（README.md「R」）---
inventory_base <- function(directory) {
    header <- readLines(base_inventory_paths(directory)[[1L]], n = 1L, warn = FALSE)
    sub("^# ([^ ]+) .*$", "\\1", header)
}

# --- ベース一覧 2 つを固定ベース・プラットフォーム（NULL なら一覧の値）・形式と照らす（Python は read_base_inventory）（README.md「R」）---
validate_base_inventories <- function(directory, base = analysis_base_image(), platform = NULL) {
    paths <- base_inventory_paths(directory)
    columns <- c("Package\tVersion", "Package\tArchitecture\tVersion\tStatus")
    for (i in seq_along(paths)) {
        if (!file_test("-f", paths[[i]])) {
            stop("ベース一覧が見つかりません: ", paths[[i]], call. = FALSE)
        }
    }
    if (is.null(platform)) platform <- inventory_platform(directory)
    header <- paste("#", base, validate_platform(platform))
    for (i in seq_along(paths)) {
        lines <- readLines(paths[[i]], n = if (i == 2L) -1L else 3L, warn = FALSE)
        data <- lines[-c(1L, 2L)]
        rows <- which(nzchar(data))
        if (length(lines) < 3L || !identical(lines[1:2], c(header, columns[[i]])) ||
            (i == 2L && !length(rows))) {
            stop("ベース一覧が固定イメージ、プラットフォームまたは一覧の形式と一致しません: ",
                 paths[[i]], call. = FALSE)
        }
        if (i == 2L) {
            bad <- rows[nchar(gsub("[^\t]", "", data[rows])) != 3L]
            if (length(bad)) {
                stop("ベース一覧の行が4列ではありません: ", paths[[i]], "（", bad[[1L]] + 2L,
                     "行）", call. = FALSE)
            }
        }
    }
    invisible(paths)
}

# --- 監査に使う固定ベースの一覧の説明（固定ベースと Ubuntu パッケージの数）（README.md「結果の共通形式」）---
base_inventory_detail <- function(directory) {
    path <- base_inventory_paths(directory)[[2L]]
    count <- nrow(read.delim(path, skip = 1L, colClasses = "character"))
    paste0(inventory_base(directory), "、Ubuntu パッケージ ", count)
}

# --- 監査に使うベース一覧を、確定記録から記録先に写す（固定ベースとプラットフォームを確かめる）（README.md「R」）---
stage_base_inventories <- function(directory, source, base, platform = "linux/arm64") {
    source_paths <- validate_base_inventories(source, base, platform)
    dir.create(directory, recursive = TRUE, showWarnings = FALSE)
    if (!all(file.copy(source_paths, directory))) {
        stop("監査に使うベース一覧をコピーできません: ", directory, call. = FALSE)
    }
    invisible(base_inventory_paths(directory))
}

# --- 調査の環境で R の依存と Ubuntu の依存を調べ、結果を記録先に置く（README.md「R」）---
run_discovery <- function(directory, context, platform, packages, apt_snapshot = NULL) {
    if (!is.character(packages) || !length(packages) || anyNA(packages) ||
        any(!grepl("^[A-Za-z][A-Za-z0-9.]*$", packages))) {
        stop("CRAN の R パッケージ名を文字ベクトルで指定してください。", call. = FALSE)
    }
    resolution <- file.path(directory, "resolution")
    log <- file.path(directory, "discovery.log")
    writeLines(c(paste("Base image", analysis_base_image(context)), paste("Platform", platform),
                 if (!is.null(apt_snapshot)) paste("APT snapshot", apt_snapshot)), log)
    if (!is.null(apt_snapshot)) writeLines(apt_snapshot, file.path(directory, "apt-snapshot.txt"))
    message("調査記録 ", directory)
    run_inspect_build(resolution, context, platform, "dependency_report", log,
                      unique(packages), target_ubuntu_packages(context), apt_snapshot)
    for (name in c("r-packages.tsv", "native-libraries.tsv", "ubuntu-packages.tsv", "review.tsv",
                   "base-r.tsv", "base-ubuntu.tsv")) {
        source <- file.path(resolution, name)
        if (file_test("-f", source) && !file.copy(source, file.path(directory, name))) {
            stop("調査の結果を記録先に置けません: ", name, call. = FALSE)
        }
    }
    status <- function(name) {
        path <- file.path(resolution, name)
        lines <- if (file_test("-f", path)) readLines(path, warn = FALSE) else character()
        if (length(lines)) trimws(lines[[1L]]) else ""
    }
    check <- operation_checks(directory, "discover")
    failed <- function(label) {
        stop(label, " の依存の調査に失敗しました。ログ ", shown_path(log), call. = FALSE)
    }
    table_rows <- function(name) {
        path <- file.path(directory, name)
        if (file_test("-f", path)) read.delim(path, colClasses = "character") else data.frame()
    }
    if (!identical(status("r-status"), "0")) {
        check("R dependency resolution", function() failed("R"))
    }
    check("R dependency resolution",
          detail = sprintf("%d 件のパッケージと版条件を確認", nrow(table_rows("r-packages.tsv"))))
    if (!identical(status("ubuntu-status"), "0")) {
        check("Ubuntu dependency plan", function() failed("Ubuntu"))
    }
    plan <- table_rows("ubuntu-packages.tsv")
    review <- table_rows("review.tsv")
    count <- table(factor(plan$action, levels = c("keep", "install", "update")))
    check("Ubuntu dependency plan", status = if (nrow(review)) "REVIEW" else "PASS",
          detail = sprintf("維持 %d、追加 %d、更新 %d、要確認 %d", count[["keep"]],
                           count[["install"]], count[["update"]], nrow(review)))
    invisible(directory)
}

# --- 調査の環境の定義と、調べるスクリプト（記録の入力にする）（README.md「R」）---
discovery_definition <- function() {
    c(project_file(INSPECT_DOCKERFILE), project_file("R/dependencies-inspect.R"))
}

# --- DESCRIPTION の Depends・Imports・LinkingTo の版条件を読む（README.md「R」）---
read_description_specs <- function(description) {
    desc <- read.dcf(description, all = TRUE)[1L, ]
    fields <- intersect(c("Depends", "Imports", "LinkingTo"), names(desc))
    value <- paste(na.omit(desc[fields]), collapse = ",")
    if (!nzchar(trimws(value))) return(list())
    tools:::.split_dependencies(strsplit(value, ",", fixed = TRUE)[[1L]])
}

# --- 版条件（DESCRIPTION の Depends・Imports・LinkingTo）を、版と 1 つずつ照合した行にする（README.md「R」）---
spec_item_rows <- function(check, specs, versions) {
    names <- vapply(specs, function(spec) spec$name, character(1))
    expected <- vapply(specs, function(spec) {
        if (is.null(spec$op)) "" else paste(spec$op, as.character(spec$version))
    }, character(1))
    found <- unname(versions[names])
    ok <- vapply(seq_along(specs), function(i) {
        spec <- specs[[i]]
        !is.na(found[[i]]) && (is.null(spec$op) ||
            isTRUE(do.call(spec$op, list(package_version(found[[i]]), spec$version))))
    }, logical(1))
    check_item_rows(check, names, expected, found, ifelse(ok, "PASS", "FAIL"))
}

# --- 調査の間に、プロジェクトの request/ の入力が変わっていないかを確かめて記録する（README.md「R」）---
check_inputs_unchanged <- function(check, context, started, items = NULL) {
    check("Build inputs unchanged", function() {
        current <- target_input_hashes(context)
        if (!is.null(items)) items("Build inputs unchanged",
                                   hash_item_rows("Build inputs unchanged", started, current))
        if (!identical(current, started)) {
            stop("調査の間にプロジェクトの request/ の入力が変わりました。調査をやり直してください。",
                 call. = FALSE)
        }
        length(started)
    }, detail = function(count) paste("入力", count))
}

# --- 今の UTC 時刻を APT スナップショットの時点にする（README.md「R」）---
new_apt_snapshot <- function() format(Sys.time(), "%Y%m%dT%H%M%SZ", tz = "UTC")

# --- 確定で照合する、調査の記録のファイル（記録先からの相対パスで呼ぶ）（README.md「R」）---
discovery_evidence <- function(report) {
    files <- c("r-packages.tsv", "ubuntu-packages.tsv", "review.tsv", "checks.tsv", "base-r.tsv",
               "base-ubuntu.tsv", "native-libraries.tsv", "apt-snapshot.txt",
               "resolution/r-discovery.rds")
    paths <- setNames(file.path(report, files), files)
    paths[file_test("-f", paths)]
}

# --- 調査の記録に build-environment.json を書く（入力と調査の記録のハッシュ。確定が照合する）（README.md「R」）---
write_discovery_environment <- function(directory, context, platform, apt_snapshot, review = "") {
    evidence <- discovery_evidence(directory)
    write_build_environment(list(
        schema_version = "1.0", language = "r", base_image = analysis_base_image(context),
        platform = platform, apt_snapshot = apt_snapshot, inputs = target_input_hashes(context),
        r_repository = target_config(context)[["R_REPOSITORY"]], review = review,
        evidence = as.list(vapply(evidence, file_sha256, character(1)))
    ), file.path(directory, "build-environment.json"))
}

# --- 第 1 段階の調査の環境で、DESCRIPTION の R の依存と Ubuntu の依存を 1 つの工程で調べる（README.md「第 1 段階の中身」）---
resolve_r_dependencies <- function(project = DEFAULT_PROJECT, platform = "linux/arm64",
                                   apt_snapshot = NULL, run_id = NULL) {
    validate_platform(platform)
    context <- project_request(project)
    inputs <- target_inputs(context)
    apt_snapshot <- checked_apt_snapshot(apt_snapshot)
    specs <- read_description_specs(inputs[["DESCRIPTION"]])
    packages <- setdiff(names(specs), "R")
    if (!length(packages)) stop("調査対象の R パッケージがありません。", call. = FALSE)
    base <- analysis_base_image(context)
    parameters <- list(packages = packages, image = base, project = project,
                       platform = platform, apt_snapshot = apt_snapshot)
    run_operation("resolve-r", parameters, run_id = run_id,
        inputs = c(unname(target_inputs(context)), discovery_definition()),
        action = function(directory) {
            started <- target_input_hashes(context)
            run_discovery(directory, context, platform, packages, apt_snapshot)
            check <- operation_checks(directory, "discover")
            items <- operation_check_items(directory, "discover")
            r_data <- read.delim(file.path(directory, "r-packages.tsv"), colClasses = "character")
            result <- list(r = r_data)
            versions <- c(setNames(result$r$version, result$r$package),
                          R = target_config(context)[["RSTUDIO_TAG"]])
            check("DESCRIPTION requirements", function() {
                items("DESCRIPTION requirements", spec_item_rows("DESCRIPTION requirements",
                                                                 specs, versions))
                for (spec in specs) {
                    actual <- unname(versions[spec$name])
                    violated <- length(actual) != 1L || is.na(actual) || (!is.null(spec$op) &&
                        !isTRUE(do.call(spec$op, list(package_version(actual), spec$version))))
                    if (violated) {
                        stop("DESCRIPTION の版条件を満たす候補がありません: ", spec$name,
                             call. = FALSE)
                    }
                }
            }, detail = paste("条件", length(specs)))
            review <- read.delim(file.path(directory, "review.tsv"), colClasses = "character")
            check_inputs_unchanged(check, context, started, items)
            text <- if (nrow(review)) paste("要確認:", paste(review$item, collapse = ", ")) else ""
            write_discovery_environment(directory, context, platform, apt_snapshot, text)
            result$ubuntu <- read.delim(file.path(directory, "ubuntu-packages.tsv"),
                                        colClasses = "character")
            result$review <- review
            result$checks <- read.delim(file.path(directory, "checks.tsv"),
                                        colClasses = "character", na.strings = NULL, quote = "")
            result$report <- directory
            list(value = result, message = if (nzchar(text)) text,
                 image_id = local_image_id(base, platform))
        })
}

# --- 第 1 段階で調査の記録を照合して採用する版を確定し、第 2・第 3 段階で使う確定記録を作る（README.md「第 1 段階の中身」）---
confirm_r_dependencies <- function(discovery, project = DEFAULT_PROJECT, review_notes = NULL,
                                   run_id = NULL, platform = NULL) {
    report <- if (is.list(discovery)) discovery$report else discovery
    if (!is.character(report) || length(report) != 1L || is.na(report) || !dir.exists(report)) {
        stop("調査の記録先（discover の directory）を指定してください。", call. = FALSE)
    }
    r_file <- file.path(report, "r-packages.tsv")
    plan_file <- file.path(report, "ubuntu-packages.tsv")
    reviewed <- file.path(report, "review.tsv")
    snapshot_file <- file.path(report, "apt-snapshot.txt")
    context <- project_request(project)
    input <- target_inputs(context)
    saved_file <- file.path(report, "build-environment.json")
    files <- c(unname(input), unname(discovery_evidence(report)), saved_file, review_notes)
    recorded_platform <- inventory_platform(report)
    platform <- if (is.null(platform)) recorded_platform else validate_platform(platform)
    base <- analysis_base_image(context)
    parameters <- list(image = base, project = project, platform = platform)
    run_operation("confirm-r", parameters, run_id = run_id, inputs = files,
                  action = function(directory) {
        check <- operation_checks(directory, "confirm")
        items <- operation_check_items(directory, "confirm")
        saved <- read_build_environment(saved_file)
        check("Discovery platform", function() {
            if (!identical(saved$platform, recorded_platform) ||
                !identical(saved$platform, platform)) {
                stop("調査の記録のプラットフォームが一致しません: ", saved$platform, " / ",
                     recorded_platform, " / ", platform, call. = FALSE)
            }
        }, detail = platform)
        hashes <- function(x) unlist(x)[sort(names(unlist(x)))]
        snapshot <- if (file_test("-f", snapshot_file)) readLines(snapshot_file, warn = FALSE)
        check("Discovery unchanged", function() {
            current <- lapply(discovery_evidence(report), file_sha256)
            items("Discovery unchanged", rbind(
                check_item_rows("Discovery unchanged", "base_image", saved$base_image %||% NA,
                                base),
                hash_item_rows("Discovery unchanged", saved$inputs, target_input_hashes(context),
                               "input:"),
                hash_item_rows("Discovery unchanged", saved$evidence, current, "evidence:")))
            if (!identical(saved$base_image, base) ||
                !identical(hashes(saved$inputs), hashes(target_input_hashes(context))) ||
                !identical(hashes(saved$evidence), hashes(current))) {
                stop("調査のあとで入力か調査の記録が変わっています。調査をやり直してください。",
                     call. = FALSE)
            }
            if (length(snapshot) != 1L || !grepl("^[0-9]{8}T[0-9]{6}Z$", snapshot) ||
                !identical(snapshot, saved$apt_snapshot)) {
                stop("調査の記録の APT スナップショットが一致しません。", call. = FALSE)
            }
            length(current)
        }, detail = function(count) paste0("入力 ", length(saved$inputs), "、調査の記録 ", count))
        baseline <- validate_base_inventories(report, base, platform)
        read <- function(path) read.delim(path, colClasses = "character", check.names = FALSE)
        r_data <- read(r_file)
        plan <- read(plan_file)
        check("Discovery completed", function() {
            rows <- read.delim(file.path(report, "checks.tsv"), colClasses = "character",
                               na.strings = NULL, quote = "")
            r_status <- rows$status[rows$check == "R dependency resolution"]
            ubuntu_status <- rows$status[rows$check == "Ubuntu dependency plan"]
            if (!identical(r_status, "PASS") || length(ubuntu_status) != 1L ||
                !all(rows$status %in% c("PASS", "REVIEW"))) {
                stop("完了済みの調査記録を指定してください。", call. = FALSE)
            }
            ubuntu_status
        }, detail = function(status) paste0("R PASS、Ubuntu ", status))
        check("DESCRIPTION requirements", function() {
            requirements <- read_description_specs(input[["DESCRIPTION"]])
            versions <- c(R = target_config(context)[["RSTUDIO_TAG"]],
                          setNames(r_data$version, r_data$package))
            items("DESCRIPTION requirements",
                  spec_item_rows("DESCRIPTION requirements", requirements, versions))
            satisfied <- vapply(requirements, function(item) {
                version <- unname(versions[item$name])
                !is.na(version) && (is.null(item$op) ||
                    isTRUE(do.call(item$op, list(package_version(version), item$version))))
            }, logical(1))
            selected <- r_data$package[r_data$relation == "selected"]
            if (!setequal(setdiff(names(requirements), "R"), selected) || !all(satisfied)) {
                stop("DESCRIPTION と調査結果の対象または版条件が一致しません。", call. = FALSE)
            }
            length(requirements)
        }, detail = function(count) paste("条件", count))
        findings <- read.delim(reviewed, colClasses = "character", na.strings = NULL,
                               check.names = FALSE)
        findings$decision <- rep("", nrow(findings))
        changes <- plan$package[plan$action == "update"]
        if (length(changes)) {
            findings <- rbind(findings, data.frame(
                item = changes, detail = "固定ベースのパッケージを更新する候補", decision = ""))
        }
        r_pins <- r_data[r_data$package_format != "base", c("package", "version", "sha256")]
        check("Pinned package hashes", function() {
            valid <- grepl("^[0-9a-f]{64}$", r_pins$sha256) &
                grepl("^[A-Za-z][A-Za-z0-9.]*$", r_pins$package) &
                grepl("^[0-9]+([.-][0-9]+)+$", r_pins$version) &
                !duplicated(r_pins$package)
            items("Pinned package hashes", paste(r_pins$package, r_pins$version), "",
                  r_pins$sha256, ifelse(valid, "PASS", "FAIL"))
            if (!all(grepl("^[0-9a-f]{64}$", r_pins$sha256))) {
                stop("調査記録に R パッケージの SHA-256 がありません。調査をやり直してください。",
                     call. = FALSE)
            }
            if (anyDuplicated(r_pins$package) ||
                !all(grepl("^[A-Za-z][A-Za-z0-9.]*$", r_pins$package)) ||
                !all(grepl("^[0-9]+([.-][0-9]+)+$", r_pins$version))) {
                stop("調査記録の R パッケージの名前か版が不正か重なっています。", call. = FALSE)
            }
            nrow(r_pins)
        }, detail = function(count) paste("R パッケージ", count))
        if (!is.null(review_notes)) {
            findings <- check("Review notes", function() {
                notes <- read.delim(review_notes, colClasses = "character", na.strings = NULL,
                                    check.names = FALSE, row.names = NULL, fill = FALSE)
                key <- function(x) {
                    paste(encodeString(x$item, quote = '"'), encodeString(x$detail, quote = '"'),
                          sep = "\t")
                }
                if (!identical(names(notes), c("item", "detail", "reason")) || anyNA(notes) ||
                    anyDuplicated(key(notes)) || !all(key(notes) %in% key(findings)) ||
                    !all(nzchar(trimws(notes$reason)))) {
                    stop("確認根拠の TSV が調査の要確認項目と一致しません。", call. = FALSE)
                }
                found <- match(key(findings), key(notes))
                findings$decision[!is.na(found)] <- notes$reason[found[!is.na(found)]]
                findings
            }, detail = shown_path(review_notes))
        }
        unresolved <- sum(!nzchar(findings$decision))
        check("Review items resolved", detail = paste0("要確認 ", nrow(findings), "、残り ",
                                                       unresolved),
              status = if (unresolved) "REVIEW" else "PASS")
        quoted <- lapply(findings, function(value) {
            needs_quote <- grepl('[\t\r\n"]', value)
            if (any(needs_quote)) {
                value[needs_quote] <- paste0('"', gsub('"', '""', value[needs_quote], fixed = TRUE),
                                             '"')
            }
            value
        })
        write_image_record(as.data.frame(quoted), file.path(directory, "dependency-review.tsv"))
        lock <- NULL
        if (!unresolved) {
            copied <- c(baseline, r_file, plan_file, input[["DESCRIPTION"]])
            sources <- setNames(copied, basename(copied))
            if (!all(file.copy(unname(sources), file.path(directory, names(sources))))) {
                stop("調査の確定記録を保存できません。", call. = FALSE)
            }
            if (!identical(file_sha256(file.path(directory, "DESCRIPTION")),
                           saved$inputs[["DESCRIPTION"]])) {
                stop("確定の間に DESCRIPTION が変わりました。調査をやり直してください。",
                     call. = FALSE)
            }
            write_image_record(r_pins, file.path(directory, "r-pins.tsv"))
            installs <- plan[plan$action %in% c("install", "update"), ]
            if (!all(grepl("^[a-z0-9][a-z0-9.+-]*$", plan$package)) ||
                !"architecture" %in% names(plan) ||
                !all(grepl("^[a-z0-9]+$", plan$architecture)) ||
                !all(grepl("^[A-Za-z0-9.:+~-]+$", plan$version))) {
                stop("Ubuntu パッケージの名前・アーキテクチャ・版が不正です。", call. = FALSE)
            }
            pins <- if (nrow(installs)) {
                paste0(installs$package, "=", installs$version)
            } else {
                character()
            }
            writeLines(pins, file.path(directory, "ubuntu-pins.txt"))
            write_build_environment(list(
                schema_version = "1.0", language = "r", base_image = base,
                platform = platform, apt_snapshot = snapshot,
                inputs = saved$inputs,
                r_repository = target_config(context)[["R_REPOSITORY"]]
            ), file.path(directory, "build-environment.json"))
            names <- contract_lock_files("r")
            lock <- file.path(directory, "dependency-lock.tsv")
            write_image_record(data.frame(path = names, sha256 = vapply(
                file.path(directory, names), file_sha256, character(1))), lock)
        }
        checks <- data.frame(status = if (unresolved) "REVIEW" else "PASS",
                             language_packages = sum(r_data$package_format != "base"),
                             ubuntu_packages = nrow(plan), unresolved = unresolved)
        open <- findings$item[!nzchar(findings$decision)]
        list(value = list(checks = checks, findings = findings, lock = lock),
             message = if (length(open)) paste("要確認:", paste(open, collapse = ", ")))
    })
}

# --- Dockerfile の生成に使う値の表（dockerfile-inputs。列は item・value）を確定記録から作る（無ければ空）（README.md「第 1 段階の中身」）---
dockerfile_input_rows <- function(lock, project) {
    if (is.null(lock)) return(data.frame(item = character(), value = character()))
    if (!file_test("-f", lock)) stop("確定記録のファイルを指定してください: ", lock, call. = FALSE)
    project <- project_name(project)
    bundle <- dirname(lock)
    environment <- read_build_environment(file.path(bundle, "build-environment.json"))
    data.frame(
        item = c("project", "base_image", "platform", "apt_snapshot", "dependency_lock",
                 "ubuntu_pins", "language_pins", "r_repository"),
        value = c(project, environment$base_image, environment$platform, environment$apt_snapshot,
                  shown_path(lock), shown_path(file.path(bundle, "ubuntu-pins.txt")),
                  shown_path(file.path(bundle, "r-pins.tsv")), environment$r_repository))
}
