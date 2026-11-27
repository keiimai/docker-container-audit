# --- 工程の証跡、照合の行、道具の版と報告書の表をまとめる（Python は src/evidence.py）（README.md「構成」）---

# --- 根の .env の値を環境変数に読み込む（既にある変数は上書きしない）（REPRODUCIBILITY.md 4 節）---
load_env_file <- function(path = ".env") {
    if (!file.exists(path)) return(invisible(character()))
    pattern <- "^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=(.*)$"
    lines <- grep(pattern, readLines(path, warn = FALSE, encoding = "UTF-8"), value = TRUE)
    names <- sub(pattern, "\\1", lines)
    values <- trimws(sub("[[:space:]]+#.*$", "", sub(pattern, "\\2", lines)))
    values <- gsub("^[\"']|[\"']$", "", values)
    keep <- !duplicated(names, fromLast = TRUE) & !nzchar(Sys.getenv(names))
    if (any(keep)) do.call(Sys.setenv, as.list(setNames(values[keep], names[keep])))
    invisible(names[keep])
}

# --- R/ を読み込んだ後に 1 度呼ぶ（.env を読み、記録層の git の一時ファイルを _tmp/ に置く）（Python は読み込み時に読む）（CHECKS.md 1 節）---
audit_setup <- function(envir = parent.frame()) {
    load_env_file()
    original <- get(".repro_git_version", envir = envir)
    if (isTRUE(attr(original, "audit_tmp"))) return(invisible(envir))
    scope <- new.env(parent = environment(original))
    scope$tempfile <- function(pattern = "file", tmpdir = tempdir(), fileext = "") {
        dir.create("_tmp", showWarnings = FALSE)
        base::tempfile(pattern = pattern, tmpdir = normalizePath("_tmp", mustWork = TRUE),
                       fileext = fileext)
    }
    environment(original) <- scope
    attr(original, "audit_tmp") <- TRUE
    assign(".repro_git_version", original, envir = envir)
    invisible(envir)
}

# --- 証跡の根（既定は outputs/evidence。テストは options で替える）（Python は EVIDENCE_DIR）（README.md「結果の共通形式」）---
evidence_root <- function() getOption("audit.evidence_root", file.path("outputs", "evidence"))

# --- 操作名に対応する、報告書の工程の名前を返す（contracts/operations.tsv の step）（README.md「結果の共通形式」）---
operation_step <- function(name) {
    operations <- contract_operations("r")
    step <- operations$step[operations$operation == name]
    if (length(step) != 1L) {
        stop("contracts/operations.tsv にない操作です: r ", name, call. = FALSE)
    }
    step
}

# --- 工程を証跡の枠で包み、証跡と result-summary.json を outputs/evidence/<実行 ID>/<工程>/ に書く（README.md「結果の共通形式」）---
run_operation <- function(operation, parameters, inputs = NULL, action, run_id = NULL) {
    step <- operation_step(operation)
    if (!is.character(run_id) || length(run_id) != 1L || is.na(run_id) ||
        !grepl("^[A-Za-z0-9][A-Za-z0-9_.-]*$", run_id)) {
        stop("報告書の実行 ID（run_id）を渡してください（報告書の記録の state$run_id）。",
             call. = FALSE)
    }
    directory <- file.path(evidence_root(), run_id, step)
    if (dir.exists(directory)) {
        stop("この実行 ID の工程の証跡が既にあります: ", directory, call. = FALSE)
    }
    logs <- function() list.files(directory, full.names = TRUE, recursive = TRUE)
    write_summary <- function(status, message = NULL, image_id = NULL) {
        audit_log <- file.path(directory, "r-audit.log")
        log <- if (file.exists(audit_log)) readLines(audit_log, warn = FALSE) else character()
        read_value <- function(prefix) {
            rows <- grep(paste0("^", prefix), log, value = TRUE)
            if (length(rows)) substring(rows[[1L]], nchar(prefix) + 1L)
        }
        files <- setdiff(logs(), file.path(directory, "result-summary.json"))
        payload <- list(schema_version = "1.0", language = "r",
                        operation = operation, status = status, image = parameters$image,
                        platform = parameters$platform,
                        image_id = if (is.null(image_id)) {
                            read_value("Docker image ID: ")
                        } else {
                            image_id
                        },
                        os_id = read_value("AUDIT_OS_ID="),
                        os_version = read_value("AUDIT_OS_VERSION_ID="),
                        completed_at = format(Sys.time(), "%Y-%m-%dT%H:%M:%SZ", tz = "UTC"),
                        message = message,
                        artifacts = as.list(substring(files, nchar(directory) + 2L)))
        if (operation %in% c("audit-r", "audit-r-archive", "audit-r-sif")) {
            extra <- list(base_image = parameters$base_image,
                          input_hashes = parameters$input_hashes)
            payload <- c(payload, extra[!vapply(extra, is.null, logical(1))])
        }
        json <- jsonlite::toJSON(payload, auto_unbox = TRUE, null = "null", pretty = TRUE)
        contract_validate_summary(json)
        writeLines(json, file.path(directory, "result-summary.json"), useBytes = TRUE)
    }
    if (!dir.create(directory, recursive = TRUE, mode = "0700")) {
        stop("証跡のフォルダを作成できません: ", directory, call. = FALSE)
    }
    failed <- function(e) {
        message <- conditionMessage(e)
        if (!length(message) || !nzchar(message)) message <- "工程が中断されました。"
        try(write_summary("FAIL", message), silent = TRUE)
        stop(e)
    }
    tryCatch({
        jsonlite::write_json(parameters, file.path(directory, "parameters.json"),
                             auto_unbox = TRUE, null = "null", pretty = TRUE)
        inputs <- unique(as.character(inputs))
        write_image_record(data.frame(path = inputs, sha256 = vapply(inputs, function(path) {
            if (file_test("-f", path)) file_sha256(path) else ""
        }, character(1), USE.NAMES = FALSE)), file.path(directory, "inputs.tsv"))
        write_tool_versions(directory)
        result <- action(directory)
        statuses <- if (is.list(result$value)) result$value$checks$status
        path <- file.path(directory, "checks.tsv")
        if (file_test("-f", path)) {
            recorded <- read.delim(path, quote = "", colClasses = "character", na.strings = NULL)
            statuses <- c(statuses, recorded$status)
        }
        outcome <- worst_status(statuses)
        write_summary(outcome, message = if (outcome == "REVIEW") result$message,
                      image_id = result$image_id)
        invisible(result$value)
    }, error = failed, interrupt = failed)
}

# --- 見出しを最初に一度書き、照合した行だけを TSV の末尾に加える（README.md「結果の共通形式」）---
.evidence_append_rows <- function(rows, path) {
    append <- file.exists(path) && file.size(path) > 0
    write.table(rows, path, sep = "\t", row.names = FALSE, na = "", quote = FALSE,
                append = append, col.names = !append)
}

# --- 工程の照合を 1 つずつ走らせ、checks.tsv に足す関数を返す（止まれば FAIL を書いて止める）（README.md「結果の共通形式」）---
operation_checks <- function(directory, operation) {
    known <- contract_operation_checks("r", operation)
    path <- file.path(directory, "checks.tsv")
    add <- function(check, status, detail) {
        if (!check %in% known) {
            stop("contracts/operation-checks.tsv にない検査です: ", operation, " ", check,
                 call. = FALSE)
        }
        detail <- gsub("[\t\r\n]+", " ", paste(detail, collapse = " "))
        row <- data.frame(check = check, status = status, detail = detail)
        .evidence_append_rows(row, path)
    }
    function(check, action = NULL, detail = "", status = "PASS") {
        value <- if (!is.null(action)) {
            failed <- function(e) {
                reason <- conditionMessage(e)
                if (!length(reason) || !nzchar(reason)) reason <- "工程が中断されました。"
                add(check, "FAIL", reason)
                stop(e)
            }
            tryCatch(action(), error = failed, interrupt = failed)
        }
        given <- function(x) if (is.function(x)) x(value) else x
        add(check, given(status), given(detail))
        invisible(value)
    }
}

# --- 照合した項目を 1 つずつの行にする（status を省けば expected と found の一致で PASS・FAIL）（README.md「結果の共通形式」）---
check_item_rows <- function(check, item, expected = "", found = "", status = NULL) {
    n <- length(item)
    text <- function(x) {
        x <- if (n) rep_len(as.character(unname(x)), n) else character()
        x[is.na(x)] <- ""
        gsub("[\t\r\n]+", " ", x)
    }
    if (is.null(status)) {
        raw <- if (n) rep_len(as.character(unname(found)), n) else character()
        status <- ifelse(!is.na(raw) & raw == text(expected), "PASS", "FAIL")
    }
    data.frame(check = rep(check, n), item = text(item), expected = text(expected),
               found = text(found), status = text(status))
}

# --- 1 つずつ確かめた項目を check-items.tsv に足す関数を返す（契約に無い照合名は止める）（README.md「結果の共通形式」）---
operation_check_items <- function(directory, operation) {
    known <- contract_operation_checks("r", operation)
    path <- file.path(directory, "check-items.tsv")
    function(check, item, expected = "", found = "", status = NULL) {
        rows <- if (is.data.frame(item)) item else
            check_item_rows(check, item, expected, found, status)
        if (!all(c(check, rows$check) %in% known)) {
            stop("contracts/operation-checks.tsv にない検査です: ", operation, " ", check,
                 call. = FALSE)
        }
        .evidence_append_rows(rows, path)
        invisible(rows)
    }
}

# --- 記録したハッシュと今のハッシュを名前ごとに照合した行にする（片方だけの名前も行にする。名前に prefix を付けられる）（README.md「結果の共通形式」）---
hash_item_rows <- function(check, expected, current, prefix = "") {
    expected <- unlist(expected)
    current <- unlist(current)
    names <- sort(union(names(expected), names(current)), method = "radix")
    if (!length(names)) return(check_item_rows(check, character()))
    check_item_rows(check, paste0(prefix, names), expected[names], current[names])
}

# --- 確定記録（dependency-lock.tsv）のファイルを 1 つずつ照合した行を返す（読めなければ空）（REPRODUCIBILITY.md 3 節）---
lock_item_rows <- function(check, lock) {
    entries <- tryCatch(read_lock_entries(lock), error = function(e) NULL)
    if (is.null(entries)) {
        return(check_item_rows(check, character()))
    }
    paths <- file.path(dirname(lock), entries$path)
    current <- vapply(paths, function(path) {
        if (file_test("-f", path)) file_sha256(path) else NA_character_
    }, character(1))
    hash_item_rows(check, setNames(entries$sha256, entries$path),
                   setNames(current, entries$path))
}

# --- 判定のうち最も悪いものを返す（FAIL・REVIEW・PASS の順）（README.md「結果の共通形式」）---
worst_status <- function(statuses) {
    for (status in c("FAIL", "REVIEW")) if (any(statuses == status)) return(status)
    "PASS"
}

# --- 判定ごとの項目の数の説明（README.md「結果の共通形式」）---
status_detail <- function(statuses) {
    paste(sprintf("%s %d", c("PASS", "REVIEW", "FAIL"),
                  vapply(c("PASS", "REVIEW", "FAIL"), function(x) sum(statuses == x), integer(1))),
          collapse = "、")
}

# --- 検査用のイメージ（tools/runner.sh が渡す名前・ID・定義のハッシュ）の行（検査用の外では空）（README.md「本番の手順」）---
runner_rows <- function() {
    values <- Sys.getenv(c("AUDIT_RUNNER_IMAGE", "AUDIT_RUNNER_IMAGE_ID",
                           "AUDIT_RUNNER_DEFINITION"))
    if (!nzchar(Sys.getenv("AUDIT_RUNNER"))) return(NULL)
    values[!nzchar(values)] <- "unavailable"
    data.frame(tool = c("runner image", "runner image id", "runner definition"),
               version = unname(values))
}

# --- 道具の版を tool-versions.tsv に足す（言語・Docker・buildx。extra は足す行）（README.md「結果の共通形式」）---
write_tool_versions <- function(directory, extra = NULL) {
    path <- file.path(directory, "tool-versions.tsv")
    rows <- extra
    if (!file.exists(path)) {
        docker <- tryCatch(find_command("docker"), error = function(e) NA_character_)
        version <- function(args) {
            if (is.na(docker)) return("unavailable")
            found <- suppressWarnings(system2(docker, shQuote(args), stdout = TRUE,
                                              stderr = FALSE))
            if (!is.null(attr(found, "status")) || !length(found) || !nzchar(found[[1L]])) {
                return("unavailable")
            }
            trimws(found[[1L]])
        }
        rows <- rbind(data.frame(
            tool = c("R", "docker", "docker engine", "docker buildx"),
            version = c(as.character(getRversion()),
                        version(c("version", "--format", "{{.Client.Version}}")),
                        version(c("version", "--format", "{{.Server.Version}}")),
                        version(c("buildx", "version")))), runner_rows(), rows)
    } else {
        rows <- rbind(read.delim(path, colClasses = "character", na.strings = NULL), rows)
    }
    write_image_record(rows, path)
}

# --- 報告書の表・工程・証跡をまとめる（第 1・第 3 段階の報告書が使う）（README.md「記録」）---
report_session <- function(run_id) {
    table_dir <- file.path("outputs", "tables", run_id)
    dir.create(table_dir, recursive = TRUE, showWarnings = FALSE)
    state <- new.env()
    state$steps <- data.frame(step = character(), status = character(), run_id = character(),
                              detail = character())
    state$operation_check_rows <- data.frame(step = character(), check = character(),
                                             status = character(), detail = character())
    state$operation_check_item_rows <- data.frame(step = character(), check = character(),
                                                  item = character(), expected = character(),
                                                  found = character(), status = character())
    quarto <- file.path(Sys.getenv("QUARTO_BIN_PATH"), "quarto")
    found <- if (file_test("-x", quarto)) {
        suppressWarnings(system2(quarto, "--version", stdout = TRUE, stderr = FALSE))
    }
    state$tool_version_rows <- data.frame(step = "render", tool = "quarto",
                                          version = if (length(found) &&
                                                        is.null(attr(found, "status"))) {
                                              trimws(found[[1L]])
                                          } else {
                                              "unavailable"
                                          })
    show_table <- function(name, value) {
        value <- contract_select_table(name, "r", value)
        path <- file.path(table_dir, paste0(name, ".csv"))
        repro_write_csv(value, path, na = "")
        if (nrow(value)) {
            cat(knitr::kable(value, format = "html", escape = TRUE, table.attr = 'class="table"'),
                sep = "\n")
            cat("\n\n")
        }
        cat("保存先 `", path, "`\n\n", sep = "")
    }
    add_step <- function(name, status, detail = "", evidence = "") {
        if (!name %in% contract_step_names("r")) {
            stop("contracts/operations.tsv にない工程です: ", name, call. = FALSE)
        }
        state$steps[nrow(state$steps) + 1L, ] <- list(name, status,
                                                      if (nzchar(evidence)) run_id else "",
                                                      detail)
        cat("**判定** ", status, "\n\n", sep = "")
        if (nzchar(evidence)) cat("証跡 `", evidence, "`\n\n", sep = "")
        if (nzchar(detail)) cat(paste0("> ", strsplit(detail, "\n", fixed = TRUE)[[1]]), sep = "\n")
        cat("\n\n")
    }
    collect <- function(name, directory) {
        read <- function(file) {
            path <- file.path(directory, file)
            if (!file.exists(path)) return(NULL)
            rows <- read.delim(path, colClasses = "character", na.strings = NULL, quote = "")
            data.frame(step = rep(name, nrow(rows)), rows)
        }
        state$operation_check_rows <- rbind(state$operation_check_rows, read("checks.tsv"))
        state$operation_check_item_rows <- rbind(state$operation_check_item_rows,
                                                 read("check-items.tsv"))
        state$tool_version_rows <- rbind(state$tool_version_rows, read("tool-versions.tsv"))
    }
    run_step <- function(name, expr) {
        result <- tryCatch(list(value = force(expr), error = ""), error = function(e) {
            list(value = NULL, error = conditionMessage(e))
        })
        directory <- file.path(evidence_root(), run_id, name)
        summary <- file.path(directory, "result-summary.json")
        recorded <- if (file.exists(summary)) {
            jsonlite::read_json(summary)
        } else {
            list(status = "FAIL")
        }
        detail <- if (nzchar(result$error)) {
            result$error
        } else if (identical(recorded$status, "REVIEW") && !is.null(recorded$message)) {
            recorded$message
        } else {
            ""
        }
        add_step(name, recorded$status, detail, if (dir.exists(directory)) directory else "")
        collect(name, directory)
        c(result, list(status = recorded$status, directory = directory))
    }
    show_operation_checks <- function() {
        show_table("operation-checks", state$operation_check_rows)
        meanings <- contract_read_tsv("operation-checks.tsv")
        meanings <- meanings[meanings$language %in% c("common", "r"), ]
        found <- meanings[match(unique(state$operation_check_rows$check), meanings$check), ]
        if (nrow(found)) {
            cat("各照合の意味\n\n", paste0("- `", found$check, "`：", found$meaning, "\n"), "\n",
                sep = "")
        }
        show_table("operation-check-items", state$operation_check_item_rows)
    }
    status <- function(name) {
        found <- state$steps$status[state$steps$step == name]
        if (length(found)) found[[length(found)]] else "NOT_RUN"
    }
    artifacts <- function() {
        folders <- c(table_dir, file.path(evidence_root(), run_id),
                     file.path("outputs", "images", run_id))
        list.files(folders[dir.exists(folders)], recursive = TRUE, full.names = TRUE)
    }
    ledger <- function(...) {
        run <- state$steps$status[state$steps$status %in% c("FAIL", "REVIEW", "PASS")]
        names <- paste0("step_", gsub("-", "_", state$steps$step))
        c(list(...), setNames(as.list(state$steps$status), names),
          list(result = if (length(run)) worst_status(run) else "NOT_RUN"))
    }
    list(run_id = run_id, table_dir = table_dir, show_table = show_table, add_step = add_step,
         run_step = run_step, status = status, artifacts = artifacts, ledger = ledger,
         show_operation_checks = show_operation_checks,
         steps = function() state$steps,
         operation_checks = function() state$operation_check_rows,
         operation_check_items = function() state$operation_check_item_rows,
         tool_versions = function() state$tool_version_rows)
}
