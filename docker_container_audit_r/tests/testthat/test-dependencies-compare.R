# --- 保存した監査結果の比較のテスト（Docker なし。Python は tests/test_dependencies_compare.py）（README.md「監査結果の比較」）---
# --- このファイルを走らせる間だけ子の根に移る（README.md「テスト」）---
withr::local_dir(testthat::test_path("..", ".."))
# --- R/ の関数を全部読み込み、.env と記録層の一時ファイルの置き場所を整える（報告書の setup と同じ）---
for (path in list.files("R", pattern = "[.][Rr]$", full.names = TRUE)) source(path)
audit_setup()

# --- 子の根の _tmp/ の中に、比べる表を置く一時フォルダを作る（CHECKS.md 1 節）---
dir.create("_tmp", showWarnings = FALSE)

# --- 1 つの報告書の、監査の文脈と表を書く（NULL の表は書かない）（README.md「監査結果の比較」）---
write_run <- function(tables, run_id, packages, checks, platform = "linux/amd64") {
    directory <- file.path(tables, run_id)
    dir.create(directory, showWarnings = FALSE)
    jsonlite::write_json(list(language = "r", platform = platform,
                              input_hashes = list(DESCRIPTION = run_id)),
                         file.path(directory, "audit-context.json"), auto_unbox = TRUE)
    if (!is.null(packages))
        repro_write_csv(packages, file.path(directory, "installed-packages.csv"))
    if (!is.null(checks)) repro_write_csv(checks, file.path(directory, "audit-checks.csv"))
}

# --- 版の変化・判定の変化・片方に無い一覧を比べる（README.md「監査結果の比較」）---
local({
    tables <- tempfile("r-comparison-", tmpdir = "_tmp")
    dir.create(tables)
    on.exit(unlink(tables, recursive = TRUE))
    packages <- function(ecosystem, package, architecture, version) {
        data.frame(ecosystem, package, architecture, version)
    }
    checks <- function(check, status) data.frame(check, status, detail = "")
    write_run(tables, "baseline",
              packages(c("r", "r", "ubuntu"), c("a", "b", "lib"), c("", "", "amd64"), "1"),
              checks("load", "PASS"))
    write_run(tables, "current", packages("r", c("a", "c"), "", c("2", "1")),
              checks(c("apt", "load"), c("PASS", "FAIL")))
    result <- compare_dependencies("baseline", "current", tables)
    stopifnot(identical(result[["comparison-packages"]]$package, c("a", "b", "c")))
    stopifnot(identical(result[["comparison-packages"]]$change, c("changed", "removed", "added")))
    stopifnot(identical(result[["comparison-checks"]]$check, c("apt", "load")))
    stopifnot(identical(result[["comparison-checks"]]$change, c("added", "changed")))
    context <- result[["comparison-context"]]
    stopifnot(identical(context$current[context$item == "inventory:ubuntu"], "unavailable"))
    stopifnot(identical(context$current[context$item == "DESCRIPTION"], "current"))
    stopifnot(identical(context$baseline[context$item == "inventory:ubuntu"], "available"))
    stopifnot(identical(context$baseline[context$item == "DESCRIPTION"], "baseline"))
})

# --- プラットフォームの違う報告書は比べない（README.md「監査結果の比較」）---
local({
    tables <- tempfile("r-comparison-", tmpdir = "_tmp")
    dir.create(tables)
    on.exit(unlink(tables, recursive = TRUE))
    write_run(tables, "baseline", NULL, NULL)
    write_run(tables, "current", NULL, NULL, "linux/arm64")
    error <- tryCatch(compare_dependencies("baseline", "current", tables), error = identity)
    stopifnot(inherits(error, "error"),
              grepl("同じプラットフォームの監査を指定してください", conditionMessage(error),
                    fixed = TRUE))
})

# --- 同じ ID・形の不正な ID・audit-context.json の無い報告書は比べない（README.md「監査結果の比較」）---
local({
    tables <- tempfile("r-comparison-", tmpdir = "_tmp")
    dir.create(tables)
    on.exit(unlink(tables, recursive = TRUE))
    write_run(tables, "baseline", NULL, NULL)
    cases <- list(
        list(run_ids = c("baseline", "baseline"),
             message = "異なる 2 つの報告書の実行 ID を指定してください。"),
        list(run_ids = c("../baseline", "current"),
             message = "報告書の実行 ID の形式が不正です。"),
        list(run_ids = c("baseline", "absent"),
             message = "監査コンテキストがありません。報告書を再生成してください: absent")
    )
    for (case in cases) {
        error <- tryCatch(compare_dependencies(case$run_ids[[1L]], case$run_ids[[2L]], tables),
                          error = identity)
        stopifnot(inherits(error, "error"),
                  grepl(case$message, conditionMessage(error), fixed = TRUE))
    }
})

# --- 結果の表の列が contracts/tables.tsv と違えば止める（README.md「監査結果の比較」）---
local({
    tables <- tempfile("r-comparison-", tmpdir = "_tmp")
    dir.create(tables)
    on.exit(unlink(tables, recursive = TRUE))
    write_run(tables, "baseline", NULL, NULL)
    write_run(tables, "current", NULL, NULL)
    comparer <- new.env()
    sys.source("R/dependencies-compare.R", envir = comparer)
    original <- contract_table_columns
    for (bad_table in c("comparison-context", "comparison-packages", "comparison-checks")) {
        comparer$contract_table_columns <- function(table, language) {
            if (identical(table, bad_table)) return("bad")
            original(table, language)
        }
        error <- tryCatch(comparer$compare_dependencies("baseline", "current", tables),
                          error = identity)
        stopifnot(inherits(error, "error"),
                  grepl(paste("contracts/tables.tsv と一致しません:", bad_table),
                        conditionMessage(error), fixed = TRUE))
    }
})

# --- 長い説明がある表でも判定の変更を読み取る（README.md「監査結果の比較」）---
local({
    tables <- tempfile("r-comparison-", tmpdir = "_tmp")
    dir.create(tables)
    on.exit(unlink(tables, recursive = TRUE))
    write_run(tables, "baseline", NULL, data.frame(check = "load", status = "PASS", detail = ""))
    write_run(tables, "current", NULL,
              data.frame(check = "load", status = "FAIL", detail = strrep("x", 140000L)))
    changes <- compare_dependencies("baseline", "current", tables)[["comparison-checks"]]
    stopifnot(identical(changes$check, "load"), identical(changes$change, "changed"))
})

# --- 欄の数が見出しと違う行・中身の空の表は止め、引用の中の改行は通す（README.md「監査結果の比較」）---
local({
    tables <- tempfile("r-comparison-", tmpdir = "_tmp")
    dir.create(tables)
    on.exit(unlink(tables, recursive = TRUE))
    write_run(tables, "baseline", NULL, NULL)
    write_run(tables, "current", NULL, NULL)
    path <- file.path(tables, "current", "audit-checks.csv")
    for (text in c("check,status,detail\nload,PASS\n", "check,status,detail\nload,PASS,,extra\n",
                   "")) {
        writeLines(text, path, sep = "", useBytes = TRUE)
        error <- tryCatch(compare_dependencies("baseline", "current", tables), error = identity)
        stopifnot(inherits(error, "error"),
                  grepl("比較用の表の形式が不正です", conditionMessage(error), fixed = TRUE))
    }
    writeLines("check,status,detail\nload,FAIL,\n",
               file.path(tables, "baseline", "audit-checks.csv"), sep = "", useBytes = TRUE)
    writeLines("check,status,detail\nload,PASS,\"two\nlines\"\n", path, sep = "", useBytes = TRUE)
    result <- compare_dependencies("baseline", "current", tables)
    stopifnot(identical(result[["comparison-checks"]]$change, "changed"))
})

# --- 監査コンテキストの形や値の型が違えば止める（名前の一部が合うだけの項目は使わない）（README.md「監査結果の比較」）---
local({
    tables <- tempfile("r-comparison-", tmpdir = "_tmp")
    dir.create(tables)
    on.exit(unlink(tables, recursive = TRUE))
    write_run(tables, "baseline", NULL, NULL)
    write_run(tables, "current", NULL, NULL)
    path <- file.path(tables, "current", "audit-context.json")
    for (text in c('"r"', '{"language": "r", "platform": "linux/amd64", "status": true}',
                   '{"language": "r", "platform": "linux/amd64", "input_hashes": ["x"]}',
                   '{"language": "r", "platform": "linux/amd64", "input_hashes": {"a": 1}}',
                   '{"language_x": "r", "platform": "linux/amd64"}')) {
        writeLines(text, path, useBytes = TRUE)
        error <- tryCatch(compare_dependencies("baseline", "current", tables), error = identity)
        stopifnot(inherits(error, "error"),
                  grepl("R の監査コンテキストとして比較できません: current",
                        conditionMessage(error), fixed = TRUE))
    }
})

# --- 2 つの実行 ID の形を、監査コンテキストより先に確かめる（README.md「監査結果の比較」）---
local({
    tables <- tempfile("r-comparison-", tmpdir = "_tmp")
    dir.create(tables)
    on.exit(unlink(tables, recursive = TRUE))
    error <- tryCatch(compare_dependencies("absent", "../current", tables), error = identity)
    stopifnot(inherits(error, "error"),
              grepl("報告書の実行 ID の形式が不正です。", conditionMessage(error), fixed = TRUE))
})

# --- ハッシュがない文脈・空名のハッシュ・数値の ID・タブを含む別々のキーを比較する（README.md「監査結果の比較」）---
local({
    tables <- tempfile("r-comparison-", tmpdir = "_tmp")
    dir.create(tables)
    on.exit(unlink(tables, recursive = TRUE))
    for (id in c("baseline", "current")) {
        write_run(tables, id, NULL, NULL)
        writeLines('{"language":"r","platform":"linux/amd64"}',
                   file.path(tables, id, "audit-context.json"))
    }
    compare_dependencies("baseline", "current", tables)
    for (id in c("baseline", "current")) {
        writeLines(paste0('{"language":"r","platform":"linux/amd64",',
                          '"input_hashes":{"":"', id, '"}}'),
                   file.path(tables, id, "audit-context.json"))
        packages <- data.frame(ecosystem = "ubuntu", package = c("a\tb", "a"),
                               architecture = c("c", "b\tc"), version = id)
        repro_write_csv(packages, file.path(tables, id, "installed-packages.csv"))
    }
    results <- compare_dependencies("baseline", "current", tables)
    changes <- results[["comparison-packages"]]
    context <- results[["comparison-context"]]
    stopifnot(identical(changes$package, c("a", "a\tb")),
              identical(changes$change, c("changed", "changed")),
              identical(context$baseline[context$item == ""], "baseline"),
              identical(context$current[context$item == ""], "current"))
    error <- tryCatch(compare_dependencies("baseline", 1, tables), error = identity)
    stopifnot(identical(conditionMessage(error), "報告書の実行 ID の形式が不正です。"))
})

# --- 引用していない #、引用内の改行、先頭に BOM のある監査コンテキストを比較する（README.md「監査結果の比較」）---
local({
    tables <- tempfile("r-comparison-", tmpdir = "_tmp")
    dir.create(tables)
    on.exit(unlink(tables, recursive = TRUE))
    for (id in c("baseline", "current")) {
        write_run(tables, id, NULL, NULL)
        directory <- file.path(tables, id)
        status <- if (id == "baseline") "PASS" else "FAIL"
        name <- if (id == "baseline") "Os\r\nload" else "Os\nload"
        csv <- paste0("check,status,detail\na#b,", status, ",\n\"", name, "\",", status, ",\n")
        writeBin(charToRaw(csv), file.path(directory, "audit-checks.csv"))
    }
    directory <- file.path(tables, "current")
    path <- file.path(directory, "audit-context.json")
    json <- charToRaw('{"language":"r","platform":"linux/amd64"}')
    writeBin(c(as.raw(c(0xef, 0xbb, 0xbf)), json), path)
    changes <- compare_dependencies("baseline", "current", tables)[["comparison-checks"]]
    stopifnot(identical(changes$check, c("Os\nload", "a#b")),
              identical(changes$change, c("changed", "changed")))
    for (name in c("audit-checks.csv", "audit-context.json")) {
        path <- file.path(directory, name)
        unlink(path)
        dir.create(path)
        error <- tryCatch(compare_dependencies("baseline", "current", tables), error = identity)
        stopifnot(inherits(error, "error"),
                  identical(conditionMessage(error), paste("比較用の入力が通常ファイルではありません:", path)))
        unlink(path, recursive = TRUE)
    }
})

cat("R の比較のテストに合格しました。\n")
