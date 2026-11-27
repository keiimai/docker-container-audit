# --- ローカルの Docker イメージを使う結合テスト（Python は tests/test_integration_docker.py）（CHECKS.md 1 節）---
# --- このファイルを走らせる間だけ子の根に移る（README.md「テスト」）---
withr::local_dir(testthat::test_path("..", ".."))

# --- 各結合テストの入力と証跡を用意し、終了時に片付ける（CHECKS.md 1 節）---
with_docker_integration <- function(code) {
    image <- Sys.getenv("AUDIT_TEST_R_IMAGE")
    lock <- Sys.getenv("AUDIT_TEST_R_LOCK")
    platform <- Sys.getenv("AUDIT_TEST_PLATFORM", "linux/arm64")
    if (!nzchar(image) || !nzchar(lock)) {
        testthat::skip("AUDIT_TEST_R_IMAGE と AUDIT_TEST_R_LOCK を指定してください。")
    }
    root_tmp <- file.path(normalizePath(getwd()), "_tmp")
    dir.create(root_tmp, showWarnings = FALSE)
    scratch <- tempfile("r-docker-test-", tmpdir = root_tmp)
    dir.create(scratch)
    previous <- options(audit.evidence_root = file.path(scratch, "evidence"))
    on.exit({
        options(previous)
        unlink(scratch, recursive = TRUE)
    })

    audit <- new.env()
    for (path in list.files("R", pattern = "[.][Rr]$", full.names = TRUE)) {
        sys.source(path, envir = audit)
    }
    audit$audit_setup(audit)

    check_result <- function(run_id, expected, check) {
        operation <- file.path(scratch, "evidence", run_id, "audit")
        summary <- jsonlite::read_json(file.path(operation, "result-summary.json"))
        checks <- read.delim(file.path(operation, "audit-checks.tsv"))
        inventory <- read.delim(file.path(operation, "installed-packages.tsv"))
        stopifnot(identical(summary$status, expected), identical(summary$language, "r"),
                  identical(checks$status[checks$check == check], expected),
                  setequal(inventory$ecosystem, c("r", "ubuntu")))
        if (expected == "PASS") stopifnot(all(checks$status == "PASS"))
        items <- read.delim(file.path(operation, "audit-items.tsv"), colClasses = "character",
                            na.strings = NULL)
        stopifnot(identical(names(items), audit$contract_table_columns("audit-items", "r")),
                  all(items$check %in% checks$check),
                  all(items$status %in% c("PASS", "REVIEW", "FAIL")))
        if (expected == "PASS") stopifnot(nrow(items) > 0L, all(items$status == "PASS"))
        stopifnot(identical(any(items$status[items$check == check] == "FAIL"), expected == "FAIL"))
        items
    }

    copy_lock <- function(name) {
        bundle <- file.path(scratch, name)
        dir.create(bundle)
        files <- audit$contract_lock_files("r")
        stopifnot(all(file.copy(file.path(dirname(lock), files), bundle)))
        bundle
    }
    rehash_lock <- function(bundle) {
        files <- audit$contract_lock_files("r")
        path <- file.path(bundle, "dependency-lock.tsv")
        audit$write_image_record(data.frame(path = files, sha256 = vapply(
            file.path(bundle, files), audit$file_sha256, character(1))), path)
        path
    }

    eval(substitute(code))
}

testthat::test_that("SIF と同じ取り込みで、環境変数の引用符と逆斜線が元の値に戻る", {
    with_docker_integration({
        path <- file.path(scratch, "probe.tar")
        withr::with_dir(scratch, {
            file.create("probe")
            utils::tar(path, "probe", compression = "none")
        })
        entry <- 'AUDIT_QUOTED=a"b\\c'
        changes <- audit$sif_import_changes(list(Env = entry))
        args <- c("import", "--platform", platform, "--change",
                  paste0("LABEL audit.test=", basename(scratch)), changes, path)
        id <- system2("docker", shQuote(args), stdout = TRUE)
        stopifnot(is.null(attr(id, "status")), length(id) == 1L, startsWith(id, "sha256:"))
        on.exit(system2("docker", c("image", "rm", id), stdout = FALSE), add = TRUE)
        actual <- system2("docker", shQuote(c("image", "inspect", "--format",
                                               "{{json .Config.Env}}", id)), stdout = TRUE)
        testthat::expect_true(entry %in% jsonlite::fromJSON(actual))
    })
})

testthat::test_that("見本のイメージの全項目が合格する", {
    with_docker_integration({
        audit$audit_r_image(image, lock, platform = platform, run_id = "integration-pass")
        items <- check_result("integration-pass", "PASS", "Base OS packages removed or changed")
        checks <- c("DESCRIPTION requirements", "R namespace loading",
                    "Base OS packages removed or changed", "Confirmed R versions",
                    "Confirmed R package hashes")
        testthat::expect_true(all(checks %in% items$check))
        testthat::expect_true(all(items$found[items$check == "R namespace loading"] == "loaded"))
    })
})

testthat::test_that("Ubuntu パッケージの版の違いを監査の失敗として記録する", {
    with_docker_integration({
        modified <- copy_lock("modified-baseline")
        packages <- file.path(modified, "base-ubuntu.tsv")
        rows <- readLines(packages)
        fields <- strsplit(rows[-(1:2)], "\t", fixed = TRUE)
        installed <- which(vapply(fields, function(row) identical(row[[4L]], "installed"),
                                  logical(1)))
        stopifnot(length(installed) > 0)
        first <- installed[[1L]]
        fields[[first]][[3L]] <- "0.0.0-test"
        rows[[first + 2L]] <- paste(fields[[first]], collapse = "\t")
        writeLines(rows, packages)
        testthat::expect_error(
            audit$audit_r_image(image, rehash_lock(modified), platform = platform,
                                run_id = "integration-os-version"),
            "の実行に失敗しました", fixed = TRUE
        )
        items <- check_result("integration-os-version", "FAIL",
                              "Base OS packages removed or changed")
        changed <- items[items$check == "Base OS packages removed or changed" &
                             items$status == "FAIL", ]
        stopifnot(nrow(changed) == 1L, identical(changed$expected, "0.0.0-test"),
                  identical(changed$item, paste(fields[[first]][[1L]], fields[[first]][[2L]],
                                                sep = ":")))
    })
})

testthat::test_that("必要な R パッケージの不足を監査の失敗として記録する", {
    with_docker_integration({
        invalid <- copy_lock("invalid-description")
        writeLines(c("Package: AuditFailureExample", "Version: 0.0.1",
                     "Imports: noSuchAuditPackageXYZ"), file.path(invalid, "DESCRIPTION"))
        environment <- audit$read_build_environment(file.path(invalid, "build-environment.json"))
        environment$inputs[["DESCRIPTION"]] <- audit$file_sha256(file.path(invalid, "DESCRIPTION"))
        audit$write_build_environment(environment, file.path(invalid, "build-environment.json"))
        testthat::expect_error(
            audit$audit_r_image(image, rehash_lock(invalid), platform = platform,
                                run_id = "integration-fail"),
            "の実行に失敗しました", fixed = TRUE
        )
        items <- check_result("integration-fail", "FAIL", "DESCRIPTION requirements")
        unmet <- items[items$check == "DESCRIPTION requirements", ]
        stopifnot(identical(unmet$item, "noSuchAuditPackageXYZ"), identical(unmet$expected, ""),
                  identical(unmet$found, ""), identical(unmet$status, "FAIL"))
    })
})
