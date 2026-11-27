# --- contracts/ の共通の決まりを読み、R の出力を照合する（Python は src/contract.py）（CHECKS.md 3 節）---

# --- contracts/ の場所（R の子の根の 1 つ上。Python の CONTRACT_DIR と同じ場所を、使うときに探す）（CHECKS.md 3 節）---
contract_dir <- function() {
    directory <- file.path(dirname(audit_root()), "contracts")
    if (!file_test("-f", file.path(directory, "tables.tsv"))) {
        stop("contracts/ が見つかりません。", call. = FALSE)
    }
    directory
}

# --- 照合で扱う JSON Schema の語（contracts/ で使うものだけ。ほかの語があれば止める）（CHECKS.md 3 節）---
.contract_known_keywords <- c("$schema", "$id", "title", "description", "type", "enum", "const",
                              "pattern", "required", "properties", "additionalProperties", "items")

# --- contracts/ の TSV を文字のまま読む（CHECKS.md 3 節）---
contract_read_tsv <- function(name) {
    read.delim(file.path(contract_dir(), name), colClasses = "character", na.strings = NULL,
               quote = "", comment.char = "", encoding = "UTF-8")
}

# --- その言語の操作名と工程名の行を返す（contracts/operations.tsv）（CHECKS.md 3 節）---
contract_operations <- function(language) {
    operations <- contract_read_tsv("operations.tsv")
    operations[operations$language == language, , drop = FALSE]
}

# --- その言語の報告書の工程名を返す（contracts/operations.tsv）（CHECKS.md 3 節）---
contract_step_names <- function(language) {
    unique(contract_operations(language)$step)
}

# --- 報告書の表の列の順を返す（contracts/tables.tsv の common か、その言語の表。無ければ止める）（CHECKS.md 3 節）---
contract_table_columns <- function(table, language) {
    tables <- contract_read_tsv("tables.tsv")
    row <- tables[tables$table == table & tables$language %in% c("common", language), ,
                  drop = FALSE]
    if (nrow(row) != 1L) {
        stop("contracts/tables.tsv に表がありません: ", table, call. = FALSE)
    }
    strsplit(row$columns, ",", fixed = TRUE)[[1L]]
}

# --- 列を tables.tsv の順にそろえる（余分な列は除き、足りない列は空欄にする）（Python の select_table と同じ）（CHECKS.md 3 節）---
contract_select_table <- function(table, language, value) {
    columns <- contract_table_columns(table, language)
    value <- as.data.frame(value, stringsAsFactors = FALSE, check.names = FALSE)
    for (column in setdiff(columns, names(value))) value[[column]] <- rep("", nrow(value))
    value[, columns, drop = FALSE]
}

# --- その言語の監査の検査名を返す（contracts/audit-checks.tsv の common とその言語）（CHECKS.md 3 節）---
contract_audit_checks <- function(language) {
    checks <- contract_read_tsv("audit-checks.tsv")
    checks$check[checks$language %in% c("common", language)]
}

# --- その言語のその工程の照合の検査名を返す（contracts/operation-checks.tsv の common とその言語）（CHECKS.md 3 節）---
contract_operation_checks <- function(language, operation) {
    checks <- contract_read_tsv("operation-checks.tsv")
    checks$check[checks$operation == operation & checks$language %in% c("common", language)]
}

# --- 値が当てはまる JSON Schema の型を返す（整数の値は integer と number の両方）（CHECKS.md 3 節）---
.contract_json_types <- function(value) {
    if (is.null(value)) return("null")
    if (is.list(value)) return(if (is.null(names(value))) "array" else "object")
    if (length(value) != 1L) return("unknown")
    if (is.character(value)) return("string")
    if (is.logical(value)) return("boolean")
    if (is.numeric(value)) return(if (value == round(value)) c("integer", "number") else "number")
    "unknown"
}

# --- 値を JSON Schema と照合し、合わない点の一覧を返す（知らない語があれば止める）（CHECKS.md 3 節）---
.contract_check <- function(value, schema, path) {
    unknown <- sort(setdiff(names(schema), .contract_known_keywords), method = "radix")
    if (length(unknown)) {
        stop("未対応のスキーマの語です: ", paste(unknown, collapse = ", "), call. = FALSE)
    }
    errors <- character()
    fail <- function(message) paste0(path, ": ", message)
    types <- .contract_json_types(value)
    if (!is.null(schema$type) && !any(unlist(schema$type) %in% types)) {
        return(fail(paste0(paste(unlist(schema$type), collapse = "/"), " ではありません")))
    }
    if ("const" %in% names(schema) && !identical(value, schema[["const"]])) {
        errors <- c(errors, fail("値が決まりと異なります"))
    }
    if ("enum" %in% names(schema) &&
        !any(vapply(schema$enum, function(item) identical(item, value), logical(1)))) {
        errors <- c(errors, fail("許されない値です"))
    }
    if (!is.null(schema$pattern) && identical(types, "string") &&
        !grepl(schema$pattern, value, perl = TRUE)) {
        errors <- c(errors, fail("形式が決まりと異なります"))
    }
    if ("object" %in% types) {
        missing <- setdiff(unlist(schema$required), names(value))
        if (length(missing)) {
            errors <- c(errors, fail(paste("必要な項目がありません:", paste(missing, collapse = ", "))))
        }
        for (name in names(value)) {
            child <- schema$properties[[name]]
            if (is.null(child)) child <- schema$additionalProperties
            if (isFALSE(child)) {
                errors <- c(errors, fail(paste("決まりにない項目です:", name)))
            } else if (is.list(child)) {
                index <- match(name, names(value))
                errors <- c(errors, .contract_check(value[[index]], child, paste0(path, ".", name)))
            }
        }
    }
    if ("array" %in% types && is.list(schema$items)) {
        for (i in seq_along(value)) {
            errors <- c(errors,
                        .contract_check(value[[i]], schema$items, paste0(path, "[", i, "]")))
        }
    }
    errors
}

# --- 書き出す JSON の文字列を照合する（R のリストでは {} と [] を区別できないため）（Python は contract.validate）（CHECKS.md 3 節）---
contract_validate_json <- function(json, schema_file, label) {
    schema <- jsonlite::read_json(file.path(contract_dir(), schema_file))
    value <- jsonlite::parse_json(json, simplifyVector = FALSE)
    errors <- .contract_check(value, schema, label)
    if (length(errors)) {
        stop("contracts/", schema_file, " に合いません。\n", paste(errors, collapse = "\n"),
             call. = FALSE)
    }
    invisible(value)
}

# --- result-summary.json の形と、操作名が contracts/operations.tsv にあるかを確かめる（README.md「結果の共通形式」）---
contract_validate_summary <- function(json) {
    value <- tryCatch(
        contract_validate_json(json, "result-summary.schema.json", "result-summary"),
        error = function(e) stop("結果の共通形式に合いません。\n", conditionMessage(e), call. = FALSE)
    )
    operations <- contract_read_tsv("operations.tsv")
    if (!any(operations$language == value$language & operations$operation == value$operation)) {
        stop("結果の共通形式に合いません。\nresult-summary.operation: contracts/operations.tsv にない操作です: ",
             value$language, " ", value$operation, call. = FALSE)
    }
    invisible(value)
}

# --- その言語の確定記録（dependency-lock.tsv）に載せるファイルを返す（contracts/lock-files.tsv）（CHECKS.md 3 節）---
contract_lock_files <- function(language) {
    files <- contract_read_tsv("lock-files.tsv")
    files$file[files$language %in% c("common", language)]
}

# --- build-environment.json を照合してから書き出す（Python も同じ名前）（CHECKS.md 3 節）---
write_build_environment <- function(environment, path) {
    json <- jsonlite::toJSON(environment, auto_unbox = TRUE, null = "null", pretty = TRUE)
    contract_validate_json(json, "build-environment.schema.json", "build-environment")
    writeLines(json, path, useBytes = TRUE)
    invisible(path)
}

# --- build-environment.json を読み、照合して返す（Python も同じ名前）（CHECKS.md 3 節）---
read_build_environment <- function(path) {
    json <- paste(readLines(path, warn = FALSE, encoding = "UTF-8"), collapse = "\n")
    contract_validate_json(json, "build-environment.schema.json", "build-environment")
}

# --- そのプラットフォームの SIF を作るイメージを返す（contracts/apptainer-images.tsv）（README.md「Apptainer の SIF」）---
contract_apptainer_image <- function(platform) {
    images <- contract_read_tsv("apptainer-images.tsv")
    image <- images$image[images$platform == platform]
    if (length(image) != 1L) {
        stop("contracts/apptainer-images.tsv に ", platform, " がありません。", call. = FALSE)
    }
    image
}

# --- SIF の中身を取り出すスクリプトを返す（contracts/sif-extract.sh）（README.md「Apptainer の SIF」）---
contract_sif_extract_script <- function() {
    paste(readLines(file.path(contract_dir(), "sif-extract.sh"), warn = FALSE, encoding = "UTF-8"),
          collapse = "\n")
}
