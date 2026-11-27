# --- 保存した監査の表を、Docker を使わずに比べる（Python は src/dependencies_compare.py）（README.md「監査結果の比較」）---

# --- 監査コンテキストの中で比べる項目（README.md「監査結果の比較」）---
.compare_context_items <- c("language", "platform", "status", "image_id", "base_image", "os_id",
                            "os_version")

# --- 値が 1 つの文字か null か（README.md「監査結果の比較」）---
.compare_text_or_null <- function(value) {
    is.null(value) || (is.character(value) && length(value) == 1L)
}

# --- 監査コンテキストが、名前の付いた項目の集まりで、比べる値がどれも文字か null か（README.md「監査結果の比較」）---
.compare_valid_context <- function(context) {
    if (!is.list(context) || is.null(names(context))) return(FALSE)
    hashes <- context[["input_hashes"]]
    all(vapply(context[.compare_context_items], .compare_text_or_null, logical(1))) &&
        (is.null(hashes) || (is.list(hashes) && !is.null(names(hashes)) &&
                                 all(vapply(hashes, .compare_text_or_null, logical(1)))))
}

# --- 保存した表を、キーを行の名前にした表で読む（無ければ空。欄の数・形・重複を確かめる）（README.md「監査結果の比較」）---
.compare_read_table <- function(path, columns, keys) {
    empty <- as.data.frame(setNames(rep(list(character()), length(columns)), columns))
    if (!file.exists(path)) return(empty)
    if (!fs::is_file(normalizePath(path, mustWork = TRUE), follow = FALSE))
        stop("比較用の入力が通常ファイルではありません: ", path, call. = FALSE)
    fields <- count.fields(path, sep = ",", quote = "\"", comment.char = "")
    if (!length(fields) || any(fields[!is.na(fields)] != length(columns)))
        stop("比較用の表の形式が不正です: ", path, call. = FALSE)
    data <- read.csv(path, fileEncoding = "UTF-8-BOM", colClasses = "character",
                     na.strings = NULL, check.names = FALSE)
    if (!identical(names(data), columns) || anyNA(data))
        stop("比較用の表の形式が不正です: ", path, call. = FALSE)
    key <- do.call(paste, c(lapply(data[keys], encodeString, quote = '"'), sep = "\t"))
    duplicate <- anyDuplicated(key)
    if (duplicate) {
        stop("比較用の表に重複があります: ", path, ": ", paste(data[duplicate, keys], collapse = "\t"),
             call. = FALSE)
    }
    rownames(data) <- key
    data
}

# --- 2 つの表の 1 つの欄を比べ、追加・削除・変更の行を返す（README.md「監査結果の比較」）---
.compare_table_changes <- function(before, after, field, key_columns) {
    keys <- union(rownames(before), rownames(after))
    combined <- rbind(before[key_columns], after[key_columns])
    values <- combined[match(keys, c(rownames(before), rownames(after))), , drop = FALSE]
    positions <- do.call(order, c(values, method = "radix"))
    keys <- keys[positions]
    values <- values[positions, , drop = FALSE]
    rownames(values) <- NULL
    old_index <- match(keys, rownames(before))
    new_index <- match(keys, rownames(after))
    old <- before[[field]][old_index]
    new <- after[[field]][new_index]
    keep <- is.na(old_index) | is.na(new_index) | old != new
    changes <- ifelse(is.na(old_index), "added", ifelse(is.na(new_index), "removed", "changed"))
    values[[paste0("previous_", field)]] <- replace(old, is.na(old), "")
    values[[paste0("current_", field)]] <- replace(new, is.na(new), "")
    values$change <- as.character(changes)
    values[keep, , drop = FALSE]
}

# --- 2 つの R の報告書の実行 ID を比べ、変化と取得の有無の表を返す（README.md「監査結果の比較」）---
compare_dependencies <- function(baseline, current, tables = file.path("outputs", "tables")) {
    if (identical(baseline, current)) stop("異なる 2 つの報告書の実行 ID を指定してください。", call. = FALSE)
    valid_id <- function(id) {
        is.character(id) && length(id) == 1L && !is.na(id) &&
            grepl("^[A-Za-z0-9][A-Za-z0-9_-]*$", id)
    }
    if (!valid_id(baseline) || !valid_id(current))
        stop("報告書の実行 ID の形式が不正です。", call. = FALSE)
    run_ids <- c(baseline, current)
    directories <- file.path(tables, run_ids)
    contexts <- lapply(run_ids, function(run_id) {
        context_path <- file.path(tables, run_id, "audit-context.json")
        if (!file.exists(context_path)) {
            stop("監査コンテキストがありません。報告書を再生成してください: ", run_id,
                 call. = FALSE)
        }
        if (!fs::is_file(normalizePath(context_path, mustWork = TRUE), follow = FALSE))
            stop("比較用の入力が通常ファイルではありません: ", context_path, call. = FALSE)
        lines <- readLines(context_path, warn = FALSE, encoding = "UTF-8")
        context <- jsonlite::parse_json(paste(lines, collapse = "\n"))
        if (!.compare_valid_context(context) || !identical(context[["language"]], "r") ||
            !isTRUE(context[["platform"]] %in% c("linux/amd64", "linux/arm64")))
            stop("R の監査コンテキストとして比較できません: ", run_id, call. = FALSE)
        context
    })
    before <- contexts[[1L]]
    after <- contexts[[2L]]
    if (!identical(before[["platform"]], after[["platform"]]))
        stop("同じプラットフォームの監査を指定してください。", call. = FALSE)

    package_columns <- contract_table_columns("installed-packages", "r")
    package_keys <- head(package_columns, -1L)
    packages <- lapply(file.path(directories, "installed-packages.csv"),
                       .compare_read_table, package_columns, package_keys)
    checks <- lapply(file.path(directories, "audit-checks.csv"), .compare_read_table,
                     contract_table_columns("audit-checks", "r"), "check")
    state <- new.env()
    state$context <- data.frame(item = "report_id", baseline = baseline, current = current)
    add_context <- function(item, old, new) {
        state$context[nrow(state$context) + 1L, ] <- list(item, if (is.null(old)) "" else old,
                                                          if (is.null(new)) "" else new)
    }
    for (item in .compare_context_items)
        add_context(item, before[[item]], after[[item]])
    old_hashes <- before[["input_hashes"]]
    new_hashes <- after[["input_hashes"]]
    hash_names <- as.character(union(names(old_hashes), names(new_hashes)))
    value_at <- function(values, name) {
        index <- match(name, names(values))
        if (!is.na(index)) values[[index]]
    }
    for (item in sort(hash_names, method = "radix"))
        add_context(item, value_at(old_hashes, item), value_at(new_hashes, item))

    available <- function(data) if (nrow(data)) "available" else "unavailable"
    package_changes <- lapply(c("r", "ubuntu"), function(ecosystem) {
        inventories <- lapply(packages, function(data) {
            data[data$ecosystem == ecosystem, , drop = FALSE]
        })
        add_context(paste0("inventory:", ecosystem), available(inventories[[1L]]),
                    available(inventories[[2L]]))
        if (!all(vapply(inventories, nrow, integer(1L)) > 0))
            inventories <- lapply(inventories, head, 0L)
        .compare_table_changes(inventories[[1L]], inventories[[2L]], "version", package_keys)
    })
    add_context("checks", available(checks[[1L]]), available(checks[[2L]]))
    if (!all(vapply(checks, nrow, integer(1L)) > 0)) checks <- lapply(checks, head, 0L)
    results <- list("comparison-context" = state$context,
                    "comparison-packages" = do.call(rbind, package_changes),
                    "comparison-checks" = .compare_table_changes(checks[[1L]], checks[[2L]],
                                                                   "status", "check"))
    for (name in names(results)) {
        if (!identical(names(results[[name]]), contract_table_columns(name, "r")))
            stop("比較の表の列が contracts/tables.tsv と一致しません: ", name, call. = FALSE)
    }
    results
}
