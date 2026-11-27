# --- contracts/ の共通の決まりと R の実装が一致するかのテスト（Python は tests/test_contract.py）（CHECKS.md 3 節）---
# --- このファイルを走らせる間だけ子の根に移る（README.md「テスト」）---
withr::local_dir(testthat::test_path("..", ".."))
# --- R/ の関数を全部読み込み、.env と記録層の一時ファイルの置き場所を整える（報告書の setup と同じ）---
for (path in list.files("R", pattern = "[.][Rr]$", full.names = TRUE)) source(path)
audit_setup()

# --- 共通の見本を、Python と同じく verdict が valid なら受け入れ、invalid なら拒否する（CHECKS.md 3 節）---
examples <- contract_read_tsv("result-summary-examples.tsv")
stopifnot(identical(names(examples), c("example", "verdict", "schema_verdict", "json")),
          nrow(examples) > 0L, !anyDuplicated(examples$example),
          all(examples$verdict %in% c("valid", "invalid")),
          all(examples$schema_verdict %in% c("valid", "invalid")),
          !any(examples$verdict == "valid" & examples$schema_verdict == "invalid"))
for (i in seq_len(nrow(examples))) {
    verdict <- tryCatch({
        contract_validate_summary(examples$json[[i]])
        "valid"
    }, error = function(e) "invalid")
    if (!identical(verdict, examples$verdict[[i]])) {
        stop("見本の判定が違います: ", examples$example[[i]], " -> ", verdict)
    }
}

# --- 操作側の全ファイルを読み、契約と照らすためのソースにする（CHECKS.md 3 節）---
files <- file.path("R", paste0(c("container", "evidence", "project", "discovery",
                                 "image", "archive"), ".R"))
controller_source <- paste(unlist(lapply(files, readLines, warn = FALSE, encoding = "UTF-8")),
                           collapse = "\n")

# --- 操作側が使う操作名が contracts/operations.tsv のその言語の分と一致する（CHECKS.md 3 節）---
operation_names <- regmatches(controller_source,
                              gregexpr('run_operation\\("[a-z-]+"', controller_source))[[1L]]
operation_names <- sub('^run_operation\\("', "", sub('"$', "", operation_names))
stopifnot(!anyDuplicated(operation_names))
audits <- regmatches(controller_source,
                     gregexpr('"audit-r(-archive|-sif)?"', controller_source))[[1L]]
operation_names <- unique(c(operation_names, gsub('"', "", audits)))
stopifnot(setequal(operation_names, contract_operations("r")$operation))

# --- 第 1・第 3 段階の報告書の工程名と表が contracts/ に定義され、段階ごとの工程がそろう（CHECKS.md 3 節）---
reports <- c("r-dependency-discovery", "r-image-build")
qmds <- vapply(reports, function(name) {
    paste(readLines(file.path("notebooks", paste0(name, ".qmd")), warn = FALSE,
                    encoding = "UTF-8"), collapse = "\n")
}, character(1L))
report_steps <- lapply(qmds, function(qmd) {
    unique(sub('^(run|add)_step\\("', "", sub('"$', "",
        regmatches(qmd, gregexpr('(run|add)_step\\("[a-z-]+"', qmd))[[1L]])))
})
stopifnot(setequal(unlist(report_steps), contract_step_names("r")),
          setequal(report_steps[["r-dependency-discovery"]], c("discover", "confirm")),
          setequal(report_steps[["r-image-build"]],
                   setdiff(contract_step_names("r"), c("discover", "confirm"))),
          !file.exists("notebooks/r-container-audit.qmd"))
table_source <- paste(c(qmds, readLines("R/evidence.R", warn = FALSE, encoding = "UTF-8")),
                      collapse = "\n")
tables <- unique(sub('^show_table\\("', "", sub('"$', "",
    regmatches(table_source, gregexpr('show_table\\("[a-z-]+"', table_source))[[1L]])))
for (table in tables) contract_table_columns(table, "r")
for (qmd in qmds) {
    front <- regmatches(qmd, regexpr("(?s)^---\n.*?\n---\n", qmd, perl = TRUE))
    labels <- sub("^#\\| label: ", "",
        regmatches(qmd, gregexpr("(?m)^#\\| label: [a-z-]+$", qmd, perl = TRUE))[[1L]])
    stopifnot(identical(regmatches(front, regexpr("params:\n(  [^\n]*\n)*", front)),
                        'params:\n  project: "example-r"\n'),
              identical(c(labels[[1L]], labels[[2L]], labels[[length(labels)]]),
                        c("setup", "begin", "finish")),
              sum(gregexpr("repro_run(", qmd, fixed = TRUE)[[1L]] > 0L) == 1L,
              sum(gregexpr("repro_finish(state)", qmd, fixed = TRUE)[[1L]] > 0L) == 1L,
              grepl("repro_fail(", qmd, fixed = TRUE),
              !grepl("AUDIT_REPORT_ID", qmd, fixed = TRUE))
}
stopifnot(inherits(try(contract_table_columns("python-packages", "r"), silent = TRUE),
                   "try-error"))

# --- コンテナ内の監査が出す検査名は contracts/audit-checks.tsv の R の分と同じ集合（CHECKS.md 3 節）---
inspect_source <- paste(readLines("R/dependencies-inspect.R", warn = FALSE, encoding = "UTF-8"),
                        collapse = "\n")
inspect_source <- substring(inspect_source,
                            regexpr("state$checks <-", inspect_source, fixed = TRUE))
checks <- unique(sub('^record(_review|_unlisted)?\\("', "", sub('"$', "",
    regmatches(inspect_source,
               gregexpr('record(_review|_unlisted)?\\("[^"]+"', inspect_source))[[1L]])))
stopifnot(setequal(checks, contract_audit_checks("r")),
          !anyDuplicated(contract_read_tsv("audit-checks.tsv")$check))

# --- operation-checks.tsv の照合の名前は重ならず、操作側が使う名前とそろう（CHECKS.md 3 節）---
operation_check_rows <- contract_read_tsv("operation-checks.tsv")
stopifnot(all(operation_check_rows$operation %in% c("discover", "confirm", "build", "audit",
                                                    "save", "sif")),
          all(operation_check_rows$language %in% c("common", "r", "python")),
          !anyDuplicated(paste(operation_check_rows$operation, operation_check_rows$check)))
check_names <- unique(sub('^check\\("', "", sub('"$', "",
    regmatches(controller_source, gregexpr('check\\("[^"]+"', controller_source))[[1L]])))
stopifnot(setequal(check_names, operation_check_rows$check[
    operation_check_rows$language %in% c("common", "r")]))

# --- 照合が扱わない JSON Schema の語は止める（CHECKS.md 3 節）---
unsupported <- try(.contract_check(list(), list(oneOf = list()), "x"), silent = TRUE)
stopifnot(inherits(unsupported, "try-error"),
          grepl("未対応のスキーマの語です", as.character(unsupported), fixed = TRUE))

# --- 知らない語は文字の順に並べて知らせ、const は null と型まで比べる（CHECKS.md 3 節）---
unsupported <- try(.contract_check(list(), list(zeta = 1, oneOf = list()), "x"), silent = TRUE)
stopifnot(grepl("未対応のスキーマの語です: oneOf, zeta", as.character(unsupported), fixed = TRUE))
null_schema <- jsonlite::parse_json('{"const": null}', simplifyVector = FALSE)
stopifnot(length(.contract_check("a", null_schema, "x")) == 1L,
          length(.contract_check(NULL, null_schema, "x")) == 0L,
          length(.contract_check(TRUE, list(const = 1L), "x")) == 1L)

# --- contracts/ が無いとき・決まりが読めないときの止め方と、sif-extract.sh の最後の改行（CHECKS.md 3 節）---
local({
    original <- audit_root
    on.exit(assign("audit_root", original, envir = globalenv()))
    root <- tempfile("r-contract-dir-", tmpdir = "_tmp")
    dir.create(file.path(root, "child"), recursive = TRUE)
    on.exit(unlink(root, recursive = TRUE), add = TRUE)
    assign("audit_root", function() file.path(normalizePath(root), "child"), envir = globalenv())
    missing <- try(contract_dir(), silent = TRUE)
    stopifnot(grepl("contracts/ が見つかりません。", as.character(missing), fixed = TRUE))
    dir.create(file.path(root, "contracts"))
    writeLines("table\tlanguage\tcolumns", file.path(root, "contracts", "tables.tsv"))
    operations_path <- file.path(root, "contracts", "operations.tsv")
    bom <- as.raw(c(0xef, 0xbb, 0xbf))
    writeBin(c(bom, charToRaw("language\toperation\tstep\nr\tbuild-r\tbuild\n")), operations_path)
    stopifnot(identical(contract_operations("r")$step, "build"))
    writeLines(c("a", ""), file.path(root, "contracts", "sif-extract.sh"))
    stopifnot(identical(contract_sif_extract_script(), "a\n"))
    unreadable <- suppressWarnings(try(contract_validate_summary("{}"), silent = TRUE))
    stopifnot(grepl("結果の共通形式に合いません。", as.character(unreadable), fixed = TRUE))
})

# --- 列は契約の順にし、余分な列を除き、足りない列を空欄にする（CHECKS.md 3 節）---
selected <- contract_select_table("dependency-review", "r",
                                  data.frame(detail = "d", extra = 1, item = "i"))
stopifnot(identical(names(selected), c("item", "detail", "decision")),
          identical(selected$item, "i"), identical(selected$detail, "d"),
          identical(selected$decision, ""))

# --- 工程の証跡（result-summary.json・REVIEW・確定記録）（README.md「結果の共通形式」）---
local({
    root <- normalizePath(getwd())
    scratch <- tempfile("r-contract-test-", tmpdir = file.path(root, "_tmp"))
    dir.create(scratch)
    on.exit({
        setwd(root)
        unlink(scratch, recursive = TRUE)
    })
    setwd(scratch)
    unknown <- try(run_operation("save-r-unknown", list(image = "example:test"),
                                 action = function(directory) list(value = 1),
                                 run_id = "contract-pass"), silent = TRUE)
    stopifnot(inherits(unknown, "try-error"),
              grepl("operations.tsv", as.character(unknown), fixed = TRUE),
              !dir.exists("outputs/evidence"))
    image_id <- paste0("sha256:", strrep("b", 64L))
    run_operation("build-r", list(image = "example:test", platform = "linux/arm64"),
        action = function(directory) {
            writeLines("ok", file.path(directory, "build.log"))
            list(value = "example:test", image_id = image_id)
        }, run_id = "contract-pass")
    summaries <- list.files("outputs/evidence", pattern = "^result-summary[.]json$",
                            recursive = TRUE, full.names = TRUE)
    stopifnot(identical(summaries, "outputs/evidence/contract-pass/build/result-summary.json"))
    summary <- jsonlite::read_json(summaries[[1L]])
    stopifnot(identical(summary$operation, "build-r"), identical(summary$status, "PASS"),
              identical(summary$platform, "linux/arm64"), identical(summary$image_id, image_id),
              identical(unlist(summary$artifacts), c("build.log", "inputs.tsv", "parameters.json",
                                                     "tool-versions.tsv")))
    versions <- read.delim(sub("result-summary[.]json$", "tool-versions.tsv", summaries[[1L]]),
                           colClasses = "character")
    runner <- if (nzchar(Sys.getenv("AUDIT_RUNNER"))) {
        c("runner image", "runner image id", "runner definition")
    }
    stopifnot(identical(versions$tool, c("R", "docker", "docker engine", "docker buildx", runner)),
              identical(versions$version[[1L]], as.character(getRversion())))
    steps_before <- list.files("outputs/evidence/contract-pass")
    failed <- try(run_operation("save-r-unknown", list(image = "example:test"),
                                action = function(directory) list(value = 1),
                                run_id = "contract-pass"), silent = TRUE)
    stopifnot(inherits(failed, "try-error"),
              grepl("operations.tsv", as.character(failed), fixed = TRUE),
              identical(list.files("outputs/evidence/contract-pass"), steps_before))
    summary_of <- function(id, step) {
        jsonlite::read_json(file.path("outputs/evidence", id, step, "result-summary.json"))
    }
    run_operation("confirm-r", list(image = "x", platform = "linux/arm64"),
        action = function(directory) {
            list(value = list(checks = data.frame(status = "REVIEW")),
                 message = "要確認: libfoo.so.1")
        }, run_id = "contract-review")
    review <- summary_of("contract-review", "confirm")
    stopifnot(identical(review$status, "REVIEW"),
              identical(review$message, "要確認: libfoo.so.1"))
    run_operation("build-r", list(image = "x", platform = "linux/arm64"),
        action = function(directory) {
            check <- operation_checks(directory, "build")
            check("Image build", detail = '値は "例" です', status = "REVIEW")
            check("Image platform", detail = "linux/arm64")
            list(value = "x", message = "要確認: Image build")
        }, run_id = "contract-checks")
    reported <- summary_of("contract-checks", "build")
    saved <- read.delim("outputs/evidence/contract-checks/build/checks.tsv", quote = "")
    stopifnot(identical(reported$status, "REVIEW"), identical(saved$detail[1L], '値は "例" です'))
    interrupted <- tryCatch(run_operation("build-r", list(image = "x", platform = "linux/arm64"),
        action = function(directory) {
            check <- operation_checks(directory, "build")
            check("Image build", function() {
                stop(structure(list(message = "中断"), class = c("interrupt", "condition")))
            })
        }, run_id = "contract-interrupt"), interrupt = identity)
    stopifnot(inherits(interrupted, "interrupt"),
              identical(summary_of("contract-interrupt", "build")$status, "FAIL"),
              identical(read.delim("outputs/evidence/contract-interrupt/build/checks.tsv")$status,
                        "FAIL"))
    raw <- paste0('{"schema_version":"1.0","language":"r","platform":"linux/arm64",',
                  '"base_image":"x","apt_snapshot":null,"inputs":{"":"', strrep("a", 64L), '"}}')
    parsed <- contract_validate_json(raw, "build-environment.schema.json", "build-environment")
    stopifnot(identical(parsed$inputs[[1L]], strrep("a", 64L)))
    bom <- as.raw(c(0xef, 0xbb, 0xbf))
    bytes <- c(bom, charToRaw(raw))
    writeBin(bytes, "bom-environment.json")
    stopifnot(identical(read_build_environment("bom-environment.json"), parsed),
              identical(readBin("bom-environment.json", "raw", length(bytes)), bytes))

    run_operation("audit-r", list(input_hashes = jsonlite::parse_json("{}")),
        action = function(directory) {
            writeLines(paste("Docker image ID:", image_id), file.path(directory, "r-audit.log"))
            list(value = "x")
        }, run_id = "log-id")
    from_log <- summary_of("log-id", "audit")
    stopifnot(identical(from_log$image_id, image_id),
              identical(from_log$input_hashes, jsonlite::parse_json("{}")),
              !"base_image" %in% names(from_log),
              file.mode("outputs/evidence/log-id/audit") == as.octmode("0700"))

    stopifnot(file.copy(file.path(root, "projects"), scratch, recursive = TRUE))
    dir.create("bad")
    writeLines("not a lock", "bad/dependency-lock.tsv")
    lock <- file.path(scratch, "bad", "dependency-lock.tsv")
    built <- try(build_r_image("x:test", lock = lock, project = "example-r",
                               platform = "linux/arm64", run_id = "contract-lock"),
                 silent = TRUE)
    stopifnot(inherits(built, "try-error"),
              grepl("確定記録の形式が不正です", built, fixed = TRUE),
              identical(summary_of("contract-lock", "build")$status, "FAIL"))
    parameters <- jsonlite::read_json("outputs/evidence/contract-lock/build/parameters.json")
    stopifnot(identical(parameters$project, "example-r"),
              identical(parameters$dependency_lock, shown_path(lock)))
    stopifnot(!dir.exists("outputs/repro"))
})
cat("R の契約のテストに合格しました。\n")
