# --- 操作側の各部品のテスト（Docker を使わない。Python は tests/test_container_controller.py）（CHECKS.md 1 節）---
# --- このファイルを走らせる間だけ子の根に移る（README.md「テスト」）---
withr::local_dir(testthat::test_path("..", ".."))
# --- R/ の関数を全部読み込み、.env と記録層の一時ファイルの置き場所を整える（報告書の setup と同じ）---
for (path in list.files("R", pattern = "[.][Rr]$", full.names = TRUE)) source(path)
audit_setup()
# --- R/ の関数を差し替える（元の関数は、差し替えたブロックの on.exit で必ず戻す）（CHECKS.md 1 節）---
stub <- function(name, value) assign(name, value, envir = globalenv())
# --- テストの APT スナップショットの時点（README.md「第 1 段階の中身」）---
SNAPSHOT <- "20261101T000000Z"
# --- 工程の記録先の checks.tsv を読む（README.md「結果の共通形式」）---
read_checks <- function(directory) {
    read.delim(file.path(directory, "checks.tsv"), colClasses = "character", na.strings = NULL)
}
# --- 工程の記録先の check-items.tsv を読む（README.md「結果の共通形式」）---
read_items <- function(directory) {
    read.delim(file.path(directory, "check-items.tsv"), colClasses = "character",
               na.strings = NULL)
}
# --- テスト用の確定記録を一時フォルダに作り、dependency-lock.tsv のパスを返す（README.md「第 1 段階の中身」）---
example_lock <- function(bundle, platform = "linux/arm64") {
    dir.create(bundle, recursive = TRUE)
    base <- analysis_base_image("projects/example-r/request")
    writeLines(c("Package: LockOnly", "Imports: cli (>= 3.0)"), file.path(bundle, "DESCRIPTION"))
    header <- paste("#", base, platform)
    writeLines(c(header, "Package\tVersion", "base\t4.5.3"), file.path(bundle, "base-r.tsv"))
    writeLines(c(header, "Package\tArchitecture\tVersion\tStatus", "bash\tarm64\t5.2\tinstalled"),
               file.path(bundle, "base-ubuntu.tsv"))
    for (name in c("dependency-review.tsv", "ubuntu-pins.txt", "ubuntu-packages.tsv",
                   "r-packages.tsv", "r-pins.tsv")) {
        writeLines("x", file.path(bundle, name))
    }
    write_build_environment(list(
        schema_version = "1.0", language = "r", base_image = base, platform = platform,
        apt_snapshot = SNAPSHOT,
        inputs = list(DESCRIPTION = file_sha256(file.path(bundle, "DESCRIPTION")),
                      base.txt = strrep("1", 64L)),
        r_repository = "https://p3m.dev/cran/__linux__/noble/2026-04-23"
    ), file.path(bundle, "build-environment.json"))
    files <- contract_lock_files("r")
    lock <- file.path(bundle, "dependency-lock.tsv")
    hashes <- vapply(file.path(bundle, files), file_sha256, character(1), USE.NAMES = FALSE)
    write_image_record(data.frame(path = files, sha256 = hashes), lock)
    lock
}
# --- 確定記録から Dockerfile を生成し、置き換えの許可と入力の照合を確かめる（README.md「第 2 段階の中身」）---
testthat::test_that("Dockerfile の生成は確定記録を使い、置き換えには明示の指定が要る", {
    with_work_directory("generate-test-", function(directory) {
        lock <- example_lock(file.path(directory, "lock"))
        bundle <- dirname(lock)
        project <- file.path(directory, "example")
        request <- file.path(project, "request")
        dir.create(request, recursive = TRUE)
        file.copy(c(file.path(bundle, "DESCRIPTION"), "projects/example-r/request/base.txt"),
                  request)
        writeLines(c("experiment_name: example", "seed: 1", "project: example",
                     "platform: linux/arm64", "discovery:", "  apt_snapshot: ''", "image:",
                     paste0("  dependency_lock: '", lock, "'")), file.path(project, "config.yaml"))
        environment_path <- file.path(bundle, "build-environment.json")
        environment <- read_build_environment(environment_path)
        base <- target_config(request)
        environment$inputs[["base.txt"]] <- file_sha256(file.path(request, "base.txt"))
        environment$r_repository <- base[["R_REPOSITORY"]]
        write_build_environment(environment, environment_path)
        entries <- read_lock_entries(lock)
        entries$sha256 <- vapply(file.path(bundle, entries$path), file_sha256, character(1))
        write_image_record(entries, lock)
        withr::local_options(list(audit.projects_root = directory))

        destination <- generate_r_dockerfile("example")
        testthat::expect_setequal(list.files(destination),
                                  c("Dockerfile", "ubuntu-pins.txt", "r-pins.tsv",
                                    "install-dependencies.R"))
        dockerfile <- file.path(destination, "Dockerfile")
        original <- readLines(dockerfile)
        testthat::expect_true(all(c(paste0("ARG BASE_IMAGE=", base[["BASE_IMAGE"]]),
                                   paste0("ARG APT_SNAPSHOT=", SNAPSHOT),
                                   paste0("ARG R_REPOSITORY=", base[["R_REPOSITORY"]])) %in%
                                 original))
        testthat::expect_identical(file_sha256(file.path(destination, "r-pins.tsv")),
                                   file_sha256(file.path(bundle, "r-pins.tsv")))
        testthat::expect_identical(generate_r_dockerfile("example"), destination)

        writeLines("手書き", dockerfile)
        writeLines("追加ファイル", file.path(destination, "extra.txt"))
        testthat::expect_error(generate_r_dockerfile("example"), "内容が異なる既存ファイル")
        testthat::expect_identical(readLines(dockerfile), "手書き")
        generate_r_dockerfile("example", replace = TRUE)
        testthat::expect_identical(readLines(dockerfile), original)
        testthat::expect_identical(readLines(file.path(destination, "extra.txt")), "追加ファイル")
        writeLines("Package: Changed", file.path(request, "DESCRIPTION"))
        testthat::expect_error(generate_r_dockerfile("example", replace = TRUE),
                               "確定記録と request/ が一致しません")
        testthat::expect_identical(readLines(dockerfile), original)
    })
})
# --- 確定記録の空行・余分な欄と、記録されたファイルの欠落を区別する（README.md「結果の共通形式」）---
with_work_directory("lock-format-", function(directory) {
    lock <- example_lock(file.path(directory, "lock"))
    lines <- readLines(lock)
    writeLines(c(lines[[1L]], "", lines[-1L]), lock)
    stopifnot(identical(verify_r_dependency_lock(lock)$bundle, normalizePath(dirname(lock))))
    writeLines(c(lines[[1L]], paste(seq_along(lines[-1L]), lines[-1L], sep = "\t")), lock)
    error <- try(verify_r_dependency_lock(lock), silent = TRUE)
    stopifnot(inherits(error, "try-error"), grepl("形式が不正", error, fixed = TRUE))
    writeLines(lines, lock)
    unlink(file.path(dirname(lock), "ubuntu-pins.txt"))
    error <- try(verify_r_dependency_lock(lock), silent = TRUE)
    stopifnot(inherits(error, "try-error"),
              grepl("確定後に記録が見つかりません: ubuntu-pins.txt", error, fixed = TRUE))
})
# --- プラットフォームは linux/arm64 か linux/amd64 だけ（README.md「検査対象のフォルダ」）---
local({
    stopifnot(identical(validate_platform("linux/arm64"), "linux/arm64"))
    stopifnot(inherits(try(validate_platform("linux/s390x"), silent = TRUE), "try-error"))
})
# --- イメージの ID を決めた後は、プラットフォームの確認にも同じ ID を使う（README.md「結果の共通形式」）---
local({
    root <- normalizePath(getwd())
    scratch <- tempfile("image-id-", tmpdir = file.path(root, "_tmp"))
    dir.create(scratch)
    on.exit({
        setwd(root)
        unlink(scratch, recursive = TRUE)
    })
    setwd(scratch)
    dir.create("bin")
    id <- paste0("sha256:", strrep("c", 64L))
    writeLines(c("#!/bin/sh",
                 'last=""; for a in "$@"; do last="$a"; done',
                 'case "$*" in',
                 sprintf('  *"{{.Id}}"*) echo "%s" ;;', id),
                 sprintf('  *"--platform"*) [ "$last" = "%s" ] && echo linux/arm64 || exit 1 ;;',
                         id),
                 "esac"), "bin/docker")
    Sys.chmod("bin/docker", "0755")
    path <- Sys.getenv("PATH")
    Sys.setenv(PATH = paste(normalizePath("bin"), path, sep = ":"))
    found <- try(local_image_id("example:tag", "linux/arm64"), silent = TRUE)
    Sys.setenv(PATH = path)
    stopifnot(identical(found, id))
})
# --- bind の指定は source の引用符を二重にし、読み取り専用なら readonly を付ける（README.md「構成」）---
local({
    root <- normalizePath(getwd())
    source <- file.path(root, 'a"b')
    stopifnot(identical(bind_mount(source, "/project", "readonly"),
                        paste0('type=bind,"source=', root, '/a""b",destination=/project,readonly')),
              identical(bind_mount(source, "/project"),
                        paste0('type=bind,"source=', root, '/a""b",destination=/project')))
})
# --- 記録に書くパスは、根の中なら根からの相対パス、外なら受け取ったまま（README.md「結果の共通形式」）---
local({
    root <- normalizePath(getwd())
    outside <- file.path(dirname(root), "outside.txt")
    stopifnot(identical(shown_path(file.path(root, "projects", "x.txt")), "projects/x.txt"),
              identical(shown_path(outside), outside),
              is.null(shown_path(NULL)))
})
# --- Docker の共有確認は、根の外のパスなら docker を呼ばずに止める（README.md「構成」）---
local({
    originals <- list(run_command = run_command)
    on.exit(for (name in names(originals)) stub(name, originals[[name]]))
    calls <- list()
    stub("run_command", function(command, args, ...) calls[[length(calls) + 1L]] <<- args)
    for (path in c(R.home(), audit_root())) {
        failed <- try(wait_for_bind_sources(path, "example:test", "linux/arm64"), silent = TRUE)
        stopifnot(inherits(failed, "try-error"),
                  grepl("プロジェクト内のパス", as.character(failed), fixed = TRUE))
    }
    missing <- tempfile(tmpdir = project_file("_tmp"))
    stopifnot(inherits(try(wait_for_bind_sources(missing, "example:test", "linux/arm64"),
                           silent = TRUE), "try-error"), !length(calls))
    wait_for_bind_sources(c("R", project_file("R")), "example:test", "linux/arm64")
    stopifnot(length(calls) == 1L, sum(calls[[1L]] == "R") == 1L)
})
# --- 明示した環境変数を上書きし、ほかの環境変数を引き継ぐ（README.md「構成」）---
with_work_directory("run-env-", function(directory) {
    log <- file.path(directory, "run.log")
    withr::with_envvar(c(AUDIT_TEST_KEEP = "keep", AUDIT_TEST_CHANGE = "old"), {
        run_command("sh", c("-c", 'echo "$AUDIT_TEST_KEEP/$AUDIT_TEST_CHANGE"'),
                    env = "AUDIT_TEST_CHANGE=new", log = log)
    })
    stopifnot(identical(readLines(log), "keep/new"))
})
# --- バイト数と SHA-256 の説明は「<バイト数> バイト、SHA-256 <ハッシュ>」の文（README.md「結果の共通形式」）---
local({
    scratch <- tempfile("size-", tmpdir = file.path(normalizePath(getwd()), "_tmp"))
    dir.create(scratch)
    on.exit(unlink(scratch, recursive = TRUE))
    path <- file.path(scratch, "a.txt")
    writeBin(charToRaw("abc"), path)
    stopifnot(identical(size_and_sha256(path), paste0(
        "3 バイト、SHA-256 ",
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )))
})
# --- 工程は outputs/evidence/<実行 ID>/<工程>/ に証跡を書き、記録層の関数は呼ばない（README.md「結果の共通形式」）---
local({
    root <- normalizePath(getwd())
    scratch <- tempfile("evidence-", tmpdir = file.path(root, "_tmp"))
    dir.create(scratch)
    previous <- options(audit.evidence_root = file.path(scratch, "evidence"))
    originals <- list(repro_run = repro_run, repro_finish = repro_finish, repro_fail = repro_fail)
    on.exit({
        options(previous)
        for (name in names(originals)) stub(name, originals[[name]])
        unlink(scratch, recursive = TRUE)
    })
    forbidden <- function(...) stop("工程の処理が記録層の関数を呼びました")
    stub("repro_run", forbidden)
    stub("repro_finish", forbidden)
    stub("repro_fail", forbidden)
    writeLines("x", file.path(scratch, "input.txt"))
    value <- run_operation("build-r", list(image = "x:test", platform = "linux/arm64"),
                           inputs = file.path(scratch, "input.txt"),
                           action = function(directory) {
        writeLines("ok", file.path(directory, "build.log"))
        list(value = "x:test", image_id = paste0("sha256:", strrep("a", 64L)))
    }, run_id = "rid_1")
    directory <- file.path(scratch, "evidence", "rid_1", "build")
    summary <- jsonlite::read_json(file.path(directory, "result-summary.json"))
    stopifnot(identical(value, "x:test"), identical(summary$operation, "build-r"),
              identical(summary$status, "PASS"),
              setequal(unlist(summary$artifacts), c("build.log", "inputs.tsv", "parameters.json",
                                                    "tool-versions.tsv")))
    inputs <- read.delim(file.path(directory, "inputs.tsv"), colClasses = "character")
    stopifnot(identical(inputs$sha256, file_sha256(file.path(scratch, "input.txt"))),
              identical(jsonlite::read_json(file.path(directory, "parameters.json"))$image,
                        "x:test"))
    stopifnot(identical(operation_step("audit-r-archive"), "audit-archive"),
              identical(operation_step("audit-r-sif"), "audit-sif"),
              identical(operation_step("save-r-sif"), "sif"),
              identical(operation_step("resolve-r"), "discover"),
              inherits(try(operation_step("discover-r"), silent = TRUE), "try-error"))
    failed <- try(run_operation("audit-r", list(image = "x", platform = "linux/arm64"),
                                action = function(directory) stop("boom"), run_id = "rid_1"),
                  silent = TRUE)
    fail <- jsonlite::read_json(file.path(scratch, "evidence", "rid_1", "audit",
                                          "result-summary.json"))
    stopifnot(inherits(failed, "try-error"), identical(fail$status, "FAIL"),
              identical(fail$message, "boom"))
    stopifnot(inherits(try(run_operation("build-r", list(), action = function(d) list(),
                                         run_id = "rid_1"), silent = TRUE), "try-error"),
              inherits(try(run_operation("build-r", list(), action = function(d) list(),
                                         run_id = ""), silent = TRUE), "try-error"),
              inherits(try(run_operation("build-r", list(), action = function(d) list()),
                           silent = TRUE), "try-error"),
              inherits(try(run_operation("build-r", list(), action = function(d) list(),
                                         run_id = "a/b"), silent = TRUE), "try-error"))
})
# --- R/ の工程の処理と証跡の枠は、記録層の repro_run・repro_finish・repro_fail を呼ばない（REPRODUCIBILITY.md 2 節）---
local({
    files <- list.files("R", pattern = "[.]R$", full.names = TRUE)
    files <- setdiff(files, "R/repro.R")
    for (path in files) {
        code <- sub("#.*$", "", readLines(path, warn = FALSE, encoding = "UTF-8"))
        calls <- grep("repro_(run|finish|fail)[[:space:]]*\\(", code, value = TRUE)
        if (length(calls)) stop(path, " が記録層の関数を呼んでいます: ", calls[[1L]])
    }
})
# --- 報告書の記録に渡すファイル（第 1 段階と第 3 段階。どちらも親の contracts/ を含む）---
local({
    discovery <- report_inputs("example-r", "discovery")
    stopifnot(all(c("projects/example-r/request/DESCRIPTION", "projects/example-r/request/base.txt",
                    "projects/example-r/config.yaml", "Dockerfile.inspect",
                    "../contracts/tables.tsv", "../contracts/operations.tsv") %in% discovery),
              !any(dir.exists(discovery)),
              inherits(try(report_inputs("example-r", "image"), silent = TRUE), "try-error"),
              inherits(try(report_inputs("example-r", "audit"), silent = TRUE), "try-error"))
    image <- report_inputs("example-r", "image", "outputs/evidence/x/confirm/dependency-lock.tsv")
    stopifnot(all(c("projects/example-r/dockerfile/Dockerfile", "projects/example-r/config.yaml",
                    "outputs/evidence/x/confirm/dependency-lock.tsv",
                    "../contracts/lock-files.tsv") %in% image))
})
# --- 子の根と親の contracts/ を、作業フォルダが _tmp/ の下に移っても見つける（README.md「構成」）---
local({
    root <- normalizePath(getwd())
    stopifnot(identical(audit_root(), root),
              identical(normalizePath(contract_dir()),
                        normalizePath(file.path(root, "..", "contracts"))),
              file.exists(project_file("Dockerfile.inspect")))
    scratch <- tempfile("root-", tmpdir = file.path(root, "_tmp"))
    dir.create(scratch)
    on.exit({
        setwd(root)
        unlink(scratch, recursive = TRUE)
    })
    setwd(scratch)
    stopifnot(identical(audit_root(), root), identical(project_file("R"), file.path(root, "R")))
    previous <- options(audit.projects_root = file.path(root, "projects"))
    on.exit(options(previous), add = TRUE)
    stopifnot("../contracts/tables.tsv" %in% report_inputs("example-r", "discovery"))
})
# --- 見本のプロジェクトの DESCRIPTION と、base.txt の固定ベースを読む（README.md「検査対象のフォルダ」）---
stopifnot("Package" %in% colnames(read.dcf("projects/example-r/request/DESCRIPTION")))
stopifnot(identical(analysis_base_image(), sub("^BASE_IMAGE=", "",
    grep("^BASE_IMAGE=", readLines("projects/example-r/request/base.txt"), value = TRUE))))
# --- R と Python の記録が同じ台帳にそろう（REPRODUCIBILITY.md 3 節）---
local({
    root <- normalizePath(getwd())
    scratch <- tempfile("mixed-records-", tmpdir = file.path(root, "_tmp"))
    dir.create(scratch)
    on.exit({
        setwd(root)
        unlink(scratch, recursive = TRUE)
    })
    setwd(scratch)
    for (id in c("r-test", "python-test")) {
        dir.create(file.path("outputs/repro", id), recursive = TRUE)
    }
    for (record in list(
        list(run_id = "r-test", status = "success", experiment_name = "r-audit",
             started_at = "2026-01-01T00:00:00Z"),
        list(run_id = "python-test", status = "success", experiment_name = "audit-python",
             started_at = "2026-01-02T00:00:00+0900", finished_at = "2026-01-02T00:01:00+0900")
    )) jsonlite::write_json(record, file.path("outputs/repro", record$run_id,
                                              paste0(record$run_id, "_run.json")),
                            auto_unbox = TRUE, null = "null")
    ledger <- repro_runs()
    stopifnot(nrow(ledger) == 2L, setequal(ledger$run_id, c("r-test", "python-test")),
              ledger$experiment[match("python-test", ledger$run_id)] == "audit-python")
})
# --- 第 3 段階のビルドは dockerfile/ をコンテキストにし、--build-arg を渡さず、確定記録を前後で照合する（README.md「第 3 段階の中身」）---
local({
    root <- normalizePath(getwd())
    scratch <- tempfile("build-", tmpdir = file.path(root, "_tmp"))
    project <- file.path(scratch, "projects", "test-r")
    dir.create(file.path(project, "request"), recursive = TRUE)
    stopifnot(all(file.copy(file.path("projects/example-r/request",
                                      c("DESCRIPTION", "base.txt")),
                            file.path(project, "request"))))
    dockerfile <- file.path(project, "dockerfile")
    dir.create(file.path(dockerfile, ".git"), recursive = TRUE)
    dir.create(file.path(dockerfile, "sub"))
    writeLines("FROM scratch", file.path(dockerfile, "Dockerfile"))
    writeLines("x", file.path(dockerfile, ".hidden"))
    writeLines("x", file.path(dockerfile, ".gitignore"))
    writeLines("x", file.path(dockerfile, "sub", "r-pins.tsv"))
    writeLines("x", file.path(dockerfile, ".git", "config"))
    dir.create(file.path(scratch, "projects", "empty-r", "request"), recursive = TRUE)
    file.copy(file.path(project, "request", "DESCRIPTION"),
              file.path(scratch, "projects", "empty-r", "request"))
    lock <- example_lock(file.path(scratch, "lock"))
    previous <- options(audit.projects_root = file.path(scratch, "projects"))
    originals <- list(run_operation = run_operation, run_command = run_command,
                      local_image_id = local_image_id,
                      verify_r_dependency_lock = verify_r_dependency_lock)
    on.exit({
        options(previous)
        for (name in names(originals)) stub(name, originals[[name]])
        unlink(scratch, recursive = TRUE)
    })
    calls <- list()
    recorded <- NULL
    stub("run_operation", function(operation, parameters, inputs, action, run_id) {
        recorded <<- list(name = operation, parameters = parameters, inputs = inputs)
        directory <- file.path(scratch, "record")
        dir.create(directory, showWarnings = FALSE)
        action(directory)$value
    })
    stub("run_command", function(command, args, log) calls[[length(calls) + 1L]] <<- args)
    stub("local_image_id", function(image, platform) {
        stopifnot(identical(platform, "linux/arm64"))
        "test-image-id"
    })
    verified <- character()
    stub("verify_r_dependency_lock", function(lock, platform = NULL) {
        verified <<- c(verified, platform)
        list(bundle = dirname(lock), environment = list())
    })
    stopifnot(identical(build_r_image(NULL, lock, "test-r"), "test-r:local"))
    args <- calls[[1L]]
    stopifnot(length(calls) == 1L, identical(tail(args, 1L), dockerfile),
              identical(args[match("--file", args) + 1L], file.path(dockerfile, "Dockerfile")),
              identical(args[match("--platform", args) + 1L], "linux/arm64"),
              identical(args[match("--tag", args) + 1L], "test-r:local"),
              !any(args == "--build-arg"), "--load" %in% args)
    stopifnot(identical(verified, c("linux/arm64", "linux/arm64")))
    checks <- read_checks(file.path(scratch, "record"))
    stopifnot(identical(checks$check, c("Dependency lock", "Image build",
                                        "Dependency lock after build", "Image platform")))
    bundle_files <- file.path(dirname(lock), contract_lock_files("r"))
    stopifnot(identical(recorded$name, "build-r"),
              lock %in% recorded$inputs,
              file.path(dirname(lock), "DESCRIPTION") %in% recorded$inputs,
              setequal(setdiff(recorded$inputs, c(lock, bundle_files)),
                       file.path(dockerfile, c(".gitignore", ".hidden", "Dockerfile",
                                               "sub/r-pins.tsv"))),
              identical(names(recorded$parameters),
                        c("image", "project", "platform", "dependency_lock")),
              identical(recorded$parameters$project, "test-r"))
    stopifnot(identical(build_r_image("given:tag", lock, "test-r"), "given:tag"))
    details <- build_r_image("given:tag", lock, "test-r", run_id = "details", details = TRUE)
    stopifnot(identical(details$image, "given:tag"), identical(details$status, "PASS"),
              identical(details$run_id, "details"),
              identical(details$log, file.path(details$evidence, "build.log")))
    stopifnot(inherits(try(build_r_image(NULL, NULL, "test-r"), silent = TRUE), "try-error"),
              inherits(try(build_r_image(NULL, lock, "empty-r"), silent = TRUE), "try-error"),
              inherits(try(build_r_image(NULL, lock, "missing-r"), silent = TRUE), "try-error"),
              length(calls) == 3L)
})
# --- 保存する .tar の名前にアーキテクチャが入る（README.md「Apptainer の SIF」）---
stopifnot(identical(image_archive_name("example/app:test", "linux/arm64"),
                    "example_app_test_arm64.tar"),
          identical(image_archive_name("example:test"), "example_test_arm64.tar"),
          identical(image_archive_name("é:test"), "__test_arm64.tar"))
# --- 読み戻すときの名前と、SIF を取り出すボリュームの名前（README.md「第 3 段階の中身」）---
stopifnot(identical(readback_image_name("Example/App:Test", "linux/arm64"),
                    "audit-readback/example_app_test:arm64"),
          identical(sif_volume_name("Report_1-audit sif"), "audit-sif-Report_1-audit-sif"))
# --- SIF の監査（ボリュームに取り出して docker import。SHA-256 が違えば止める）（README.md「Apptainer の SIF」）---
local({
    root <- normalizePath(getwd())
    scratch <- tempfile("sif-audit-", tmpdir = file.path(root, "_tmp"))
    directory <- file.path(scratch, "outputs", "evidence", "rid", "audit-sif")
    dir.create(directory, recursive = TRUE)
    sif <- file.path(scratch, "x_arm64.sif")
    writeLines("sif", sif)
    originals <- list(run_command = run_command, image_config = image_config,
                      local_image_id = local_image_id,
                      wait_for_bind_sources = wait_for_bind_sources,
                      find_command = find_command,
                      run_operation = run_operation)
    on.exit({
        for (name in names(originals)) stub(name, originals[[name]])
        unlink(scratch, recursive = TRUE)
    })
    seen <- new.env()
    seen$calls <- list()
    fail_on <- ""
    stub("run_command", function(command, args, env = character(), log = NULL) {
        seen$calls[[length(seen$calls) + 1L]] <- list(command = command, args = args)
        if (nzchar(fail_on) && fail_on %in% args) stop("docker failed")
    })
    stub("image_config", function(image, platform) {
        list(Env = list("PATH=/usr/local/bin:/usr/bin", 'NOTE=a b"c'), WorkingDir = "/home",
             User = "")
    })
    stub("local_image_id", function(image, platform) paste0("sha256:", strrep("e", 64L)))
    stub("wait_for_bind_sources", function(...) invisible(NULL))
    stub("find_command", function(command) command)
    name <- sif_image_name("example/app:test", "linux/arm64")
    stopifnot(identical(name, "audit-sif/example_app_test:arm64"))
    stopifnot(identical(sif_import_changes(image_config("x", "linux/arm64")),
                        c("--change", 'ENV PATH="/usr/local/bin:/usr/bin"',
                          "--change", 'ENV NOTE="a b\\"c"', "--change", "WORKDIR /home")),
              identical(sif_import_changes(list(Env = list(), WorkingDir = "", User = "")),
                        character()))
    seen$checked <- character()
    check <- function(label, action = NULL, detail = "", status = "PASS") {
        seen$checked <- c(seen$checked, label)
        action()
    }
    items <- function(...) invisible(NULL)
    apptainer <- contract_apptainer_image("linux/arm64")
    stopifnot(identical(import_sif_image(sif, "audit-readback/example_app_test:arm64", name,
                                         "linux/arm64", directory, check, items), name))
    volume <- "audit-sif-rid"
    calls <- seen$calls
    stopifnot(identical(seen$checked, c("SIF file system extracted", "SIF image imported")),
              identical(calls[[1L]]$args, c("volume", "create", volume)))
    extract <- calls[[2L]]$args
    script <- contract_sif_extract_script()
    stopifnot(identical(calls[[2L]]$command, "docker"),
              paste0("type=volume,source=", volume, ",destination=/work") %in% extract,
              identical(extract[match("--entrypoint", extract) + 1L], "sh"),
              identical(extract[match("--entrypoint", extract) + 2L], apptainer),
              identical(tail(extract, 5L), c("-c", script, "sh", "/input/x_arm64.sif", "arm64")),
              bind_mount(normalizePath(sif), "/input/x_arm64.sif", "readonly") %in% extract,
              !("--sandbox" %in% extract),
              grepl("apptainer sif dump", script, fixed = TRUE),
              grepl("unsquashfs -d /work/rootfs", script, fixed = TRUE))
    import <- calls[[3L]]$args
    stopifnot(identical(calls[[3L]]$command, "bash"),
              identical(head(import, 3L), c("-o", "pipefail", "-c")),
              grepl('import --platform "$platform"', import[[4L]], fixed = TRUE),
              identical(import[6:10], c("docker", "linux/arm64",
                                       paste0("type=volume,source=", volume,
                                              ",destination=/work,readonly"),
                                       apptainer, name)),
              identical(tail(import, 6L), sif_import_changes(image_config("x", "linux/arm64"))))
    stopifnot(length(calls) == 4L, identical(calls[[4L]]$args, c("volume", "rm", "-f", volume)))
    seen$calls <- list()
    fail_on <- script
    stopifnot(inherits(try(import_sif_image(sif, "x", name, "linux/arm64", directory, check,
                                            items), silent = TRUE), "try-error"),
              identical(seen$calls[[length(seen$calls)]]$args,
                        c("volume", "rm", "-f", volume)))
    fail_on <- ""
    seen$calls <- list()
    record <- file.path(scratch, "sif-record")
    dir.create(record)
    writeLines(c("item\tpath\tsha256", paste("sif", "images/x_arm64.sif", strrep("0", 64L),
                                             sep = "\t")), file.path(record, "sif.tsv"))
    seen$recorded <- NULL
    stub("run_operation", function(operation, parameters, inputs, action, run_id) {
        seen$recorded <- list(name = operation, parameters = parameters, inputs = inputs)
        action(directory)
    })
    lock <- file.path(scratch, "lock", "dependency-lock.tsv")
    stopifnot(inherits(try(audit_r_sif(sif, "audit-readback/example_app_test:arm64", lock,
                                       record, "linux/arm64", run_id = "rid"), silent = TRUE),
                       "try-error"),
              !length(seen$calls))
    recorded <- seen$recorded
    stopifnot(identical(recorded$name, "audit-r-sif"),
              identical(recorded$parameters$image,
                        sif_image_name("audit-readback/example_app_test:arm64", "linux/arm64")),
              all(c(project_file(INSPECT_SCRIPT), sif, file.path(record, "sif.tsv")) %in%
                  recorded$inputs))
    rows <- read_items(directory)
    stopifnot(identical(rows$check, "SIF unchanged"), identical(rows$status, "FAIL"),
              identical(rows$found, file_sha256(sif)))
    entry <- paste("sif", "images/x_arm64.sif", file_sha256(sif), sep = "\t")
    writeLines(c("item\tpath\tsha256", entry, entry), file.path(record, "sif.tsv"))
    valid_lock <- example_lock(file.path(scratch, "duplicate-lock"))
    duplicate <- try(audit_r_sif(sif, "x", valid_lock, record, run_id = "rid"), silent = TRUE)
    stopifnot(inherits(duplicate, "try-error"),
              grepl("sif.tsv）と一致しません", duplicate, fixed = TRUE), !length(seen$calls),
              identical(tail(read_items(directory)$expected, 1L), ""),
              identical(tail(read_items(directory)$status, 1L), "FAIL"))
    direct <- try(audit_r_image("x", lock, operation = "audit-r-sif", run_id = "rid"),
                  silent = TRUE)
    stopifnot(inherits(direct, "try-error"), grepl("SIF を監査するには", direct, fixed = TRUE))
    stopifnot(inherits(try(audit_r_sif(sif, "x", NULL, record), silent = TRUE), "try-error"),
              inherits(try(audit_r_sif(sif, "x", lock, NULL), silent = TRUE), "try-error"))
})
# --- ベース一覧の 1 行目からプラットフォームを読み、違えば止める（README.md「R」）---
local({
    root <- normalizePath(getwd())
    scratch <- tempfile("inventories-", tmpdir = file.path(root, "_tmp"))
    dir.create(scratch)
    on.exit(unlink(scratch, recursive = TRUE))
    header <- paste("#", analysis_base_image(), "linux/arm64")
    writeLines(c(header, "Package\tVersion", "base\t4.5.3"), file.path(scratch, "base-r.tsv"))
    writeLines(c(header, "Package\tArchitecture\tVersion\tStatus", "bash\tarm64\t5.2\tinstalled"),
               file.path(scratch, "base-ubuntu.tsv"))
    stopifnot(identical(inventory_platform(scratch), "linux/arm64"))
    validate_base_inventories(scratch)
    validate_base_inventories(scratch, platform = "linux/arm64")
    stopifnot(inherits(try(validate_base_inventories(scratch, platform = "linux/amd64"),
                           silent = TRUE), "try-error"))
})
# --- 調査は dependency_report の段で記録先に直接書き、失敗した段は FAIL の行を書いてから止める（Python と同じ）（README.md「結果の共通形式」）---
local({
    root <- normalizePath(getwd())
    scratch <- tempfile("discovery-", tmpdir = file.path(root, "_tmp"))
    dir.create(scratch)
    originals <- list(run_inspect_build = run_inspect_build)
    on.exit({
        for (name in names(originals)) stub(name, originals[[name]])
        unlink(scratch, recursive = TRUE)
    })
    calls <- list()
    r_status <- "0"
    ubuntu_status <- "0"
    stub("run_inspect_build", function(destination, context, platform, target, log,
                                       packages = character(), extra = character(),
                                       apt_snapshot = NULL) {
        calls[[length(calls) + 1L]] <<- list(target = target, platform = platform,
                                             packages = packages, apt_snapshot = apt_snapshot)
        dir.create(destination, recursive = TRUE, showWarnings = FALSE)
        for (name in c("r-packages.tsv", "native-libraries.tsv", "ubuntu-packages.tsv",
                       "review.tsv", "base-r.tsv", "base-ubuntu.tsv")) {
            writeLines("x", file.path(destination, name))
        }
        writeLines(r_status, file.path(destination, "r-status"))
        writeLines(ubuntu_status, file.path(destination, "ubuntu-status"))
        invisible(destination)
    })
    request <- "projects/example-r/request"
    run_discovery(scratch, request, "linux/arm64", c("cli", "magick"), SNAPSHOT)
    stopifnot(identical(calls[[1L]]$target, "dependency_report"),
              identical(calls[[1L]]$packages, c("cli", "magick")),
              identical(calls[[1L]]$apt_snapshot, SNAPSHOT))
    stopifnot(any(grepl("Platform linux/arm64", readLines(file.path(scratch, "discovery.log")))))
    stopifnot(identical(readLines(file.path(scratch, "apt-snapshot.txt")), SNAPSHOT))
    stopifnot(all(file.exists(file.path(scratch, c("r-packages.tsv", "ubuntu-packages.tsv",
                                                   "review.tsv", "base-r.tsv",
                                                   "base-ubuntu.tsv")))))
    stopifnot(identical(readLines(file.path(scratch, "checks.tsv"))[2:3], c(
        "R dependency resolution\tPASS\t0 件のパッケージと版条件を確認",
        "Ubuntu dependency plan\tPASS\t維持 0、追加 0、更新 0、要確認 0")))
    stopifnot(inherits(try(run_discovery(scratch, request, "linux/arm64", "bad name"),
                           silent = TRUE), "try-error"), length(calls) == 1L)
    ubuntu_status <- "1"
    failed <- file.path(scratch, "failed")
    dir.create(failed)
    stopifnot(inherits(try(run_discovery(failed, request, "linux/arm64", "cli"),
                           silent = TRUE), "try-error"),
              file.exists(file.path(failed, "checks.tsv")))
    for (case in list(list(r = "1", ubuntu = "", check = "R dependency resolution",
                           status = "FAIL"),
                      list(r = "0", ubuntu = "100",
                           check = c("R dependency resolution", "Ubuntu dependency plan"),
                           status = c("PASS", "FAIL")))) {
        r_status <- case$r
        ubuntu_status <- case$ubuntu
        unrecorded <- tempfile("unrecorded-", tmpdir = scratch)
        dir.create(unrecorded)
        stopifnot(inherits(try(run_discovery(unrecorded, request, "linux/arm64", "cli"),
                               silent = TRUE), "try-error"))
        rows <- read_checks(unrecorded)
        stopifnot(identical(rows$check, case$check), identical(rows$status, case$status),
                  grepl("の依存の調査に失敗しました", rows$detail[rows$status == "FAIL"]))
    }
    stopifnot(!dir.exists("outputs/dependency-discovery"))
})
# --- 工程の照合は checks.tsv に 1 行ずつ足し、失敗は FAIL を書いてから止め、契約に無い名前は止める（README.md「結果の共通形式」）---
local({
    scratch <- tempfile("checks-", tmpdir = file.path(normalizePath(getwd()), "_tmp"))
    dir.create(scratch)
    on.exit(unlink(scratch, recursive = TRUE))
    check <- operation_checks(scratch, "save")
    stopifnot(identical(check("Image platform", function() "sha256:x", detail = identity),
                        "sha256:x"))
    failed <- try(check("Image unchanged during save", function() stop("moved\tagain")),
                  silent = TRUE)
    stopifnot(inherits(failed, "try-error"), grepl("moved", as.character(failed)))
    stopifnot(inherits(try(check("Not a contract check"), silent = TRUE), "try-error"))
    rows <- read_checks(scratch)
    stopifnot(identical(rows$check, c("Image platform", "Image unchanged during save")),
              identical(rows$status, c("PASS", "FAIL")),
              identical(rows$detail, c("sha256:x", "moved again")))
    stopifnot(identical(worst_status(c("PASS", "REVIEW")), "REVIEW"),
              identical(worst_status(c("REVIEW", "FAIL")), "FAIL"),
              identical(worst_status(character()), "PASS"),
              identical(status_detail(c("PASS", "PASS", "FAIL")), "PASS 2、REVIEW 0、FAIL 1"))
})
# --- 1 つずつの照合（check-items.tsv・確定記録のファイル・DESCRIPTION の版条件）（README.md「結果の共通形式」）---
local({
    scratch <- tempfile("items-", tmpdir = file.path(normalizePath(getwd()), "_tmp"))
    dir.create(scratch)
    on.exit(unlink(scratch, recursive = TRUE))
    rows <- check_item_rows("Dependency lock", c("a", "b", "c"), c("1", "2", "3"),
                            c("1", NA, "9"))
    stopifnot(identical(rows$status, c("PASS", "FAIL", "FAIL")), identical(rows$found[[2L]], ""),
              identical(nrow(check_item_rows("Dependency lock", character())), 0L))
    items <- operation_check_items(scratch, "confirm")
    items("Pinned package hashes", "pkg 1.0", "", "x\ty", "FAIL")
    hashes <- hash_item_rows("Discovery unchanged", list(a = "1", gone = "2"),
                             list(a = "1", new = "3"))
    items("Discovery unchanged", hashes)
    stopifnot(inherits(try(items("Image build", "x"), silent = TRUE), "try-error"))
    written <- read_items(scratch)
    stopifnot(identical(names(written), c("check", "item", "expected", "found", "status")),
              identical(written$item, c("pkg 1.0", "a", "gone", "new")),
              identical(written$found[[1L]], "x y"),
              identical(written$status, c("FAIL", "PASS", "FAIL", "FAIL")))
    writeLines("one", file.path(scratch, "a.txt"))
    writeLines("two", file.path(scratch, "b.txt"))
    lock <- file.path(scratch, "dependency-lock.tsv")
    write_image_record(data.frame(path = c("a.txt", "b.txt"),
                                  sha256 = file_sha256(file.path(scratch, "a.txt"))), lock)
    found <- lock_item_rows("Dependency lock", lock)
    stopifnot(identical(found$item, c("a.txt", "b.txt")),
              identical(found$status, c("PASS", "FAIL")),
              identical(nrow(lock_item_rows("Dependency lock", file.path(scratch, "none"))), 0L))
    specs <- list(list(name = "R", op = ">=", version = package_version("4.1")),
                  list(name = "dplyr", op = ">=", version = package_version("1.2")),
                  list(name = "absent"))
    spec_rows <- spec_item_rows("DESCRIPTION requirements", specs, c(R = "4.5.3", dplyr = "1.1.4"))
    stopifnot(identical(spec_rows$item, c("R", "dplyr", "absent")),
              identical(spec_rows$expected, c(">= 4.1", ">= 1.2", "")),
              identical(spec_rows$found, c("4.5.3", "1.1.4", "")),
              identical(spec_rows$status, c("PASS", "FAIL", "FAIL")))
})
# --- 保存と SIF は、照合を checks.tsv に、ハッシュを archive.tsv・sif.tsv に残す（README.md「Apptainer の SIF」）---
local({
    root <- normalizePath(getwd())
    scratch <- tempfile("export-", tmpdir = file.path(root, "_tmp"))
    dir.create(file.path(scratch, "bin"), recursive = TRUE)
    path <- Sys.getenv("PATH")
    on.exit({
        Sys.setenv(PATH = path)
        setwd(root)
        unlink(scratch, recursive = TRUE)
    })
    setwd(scratch)
    id <- paste0("sha256:", strrep("d", 64L))
    dir.create("oci")
    writeLines(jsonlite::toJSON(list(schemaVersion = 2L, manifests = list(list(
        digest = paste0("sha256:", strrep("1", 64L)),
        platform = list(architecture = "arm64", os = "linux")))), auto_unbox = TRUE),
        "oci/index.json")
    withr::with_dir("oci", utils::tar(file.path(scratch, "oci_arm64.tar"), "index.json"))
    writeLines(c("#!/bin/sh",
                 'output=""; source=""; previous=""',
                 'for a in "$@"; do',
                 '  [ "$previous" = "--output" ] && output="$a"',
                 paste0('  case "$a" in *source=*) source=$(printf %s "$a" | ',
                        'sed \'s/.*"source=\\([^"]*\\)".*/\\1/\') ;; esac'),
                 '  previous="$a"',
                 "done",
                 'case "$*" in',
                 '  *"{{.Id}} {{json .RootFS.Layers}}"*) echo \'sha256:1 ["sha256:7"]\' ;;',
                 sprintf('  *"{{.Id}}"*) echo "%s" ;;', id),
                 '  *"{{.Os}}/{{.Architecture}}"*) echo linux/arm64 ;;',
                 sprintf('  *"image save"*) cp "%s" "$output" ;;',
                         file.path(scratch, "oci_arm64.tar")),
                 sprintf('  load) tar -xOf - ./index.json > "%s" ;;',
                         file.path(scratch, "loaded.json")),
                 '  *" build /work/image.sif"*) printf sif > "$source/image.sif" ;;',
                 '  *" --version"*) echo "apptainer version 1.5.5" ;;',
                 '  *" sif list "*) echo "4 |1 |NONE |36864-2 |FS (Squashfs/*System/arm64)" ;;',
                 "esac"), "bin/docker")
    Sys.chmod("bin/docker", "0755")
    Sys.setenv(PATH = paste(normalizePath("bin"), path, sep = ":"))
    archive <- save_r_image("example:test", output = "example_arm64.tar", run_id = "export")
    stopifnot(identical(archive, file.path(scratch, "outputs", "images", "export",
                                          "example_arm64.tar")))
    stored <- jsonlite::read_json("outputs/evidence/export/save/parameters.json")
    stopifnot(identical(stored$tar, "outputs/images/export/example_arm64.tar"))
    detailed <- save_r_image("example:test", run_id = "details", details = TRUE)
    stopifnot(identical(detailed$run_id, "details"), identical(detailed$status, "PASS"),
              identical(detailed$archive, detailed$paths[[1L]]),
              identical(detailed$sha256, file_sha256(detailed$archive)),
              file.exists(file.path(detailed$evidence, "archive.tsv")))
    rows <- read_checks("outputs/evidence/export/save")
    stopifnot(identical(rows$check, c("Image platform", "Image unchanged during save",
                                      "Archive written", "Archive loads back")),
              all(rows$status == "PASS"), identical(rows$detail[[1L]], id),
              grepl(" バイト、SHA-256 ", rows$detail[[3L]]),
              identical(rows$detail[[4L]], "audit-readback/example_test:arm64 sha256:1、層 1"),
              file.exists("outputs/evidence/export/save/archive.tsv"))
    saved_items <- read_items("outputs/evidence/export/save")
    stopifnot(identical(saved_items$check, c("Image unchanged during save",
                                             "Archive loads back", "Archive loads back")),
              identical(saved_items$item, c("example:test", "image ID", "layer 1")),
              identical(saved_items$expected, c(id, "sha256:1", "sha256:7")),
              identical(saved_items$found, c(id, "sha256:1", "sha256:7")),
              all(saved_items$status == "PASS"))
    loaded <- jsonlite::read_json(file.path(scratch, "loaded.json"))$manifests[[1L]]$annotations
    stopifnot(identical(loaded[["io.containerd.image.name"]],
                        "docker.io/audit-readback/example_test:arm64"),
              identical(loaded[["org.opencontainers.image.ref.name"]], "arm64"))
    saved_sif <- save_r_sif("oci_arm64.tar", output = file.path(scratch, "oci_arm64.sif"),
                            run_id = "export")
    stopifnot(identical(saved_sif, file.path(scratch, "oci_arm64.sif")))
    stored <- jsonlite::read_json("outputs/evidence/export/sif/parameters.json")
    stopifnot(identical(stored$sif, "oci_arm64.sif"))
    made <- save_r_sif("oci_arm64.tar", output = "new.sif", run_id = "details", details = TRUE)
    stopifnot(identical(made$status, "PASS"), identical(made$sif, made$paths[[1L]]),
              identical(made$sha256, file_sha256(made$sif)),
              file.exists(file.path(made$evidence, "sif.tsv")))
    bad <- try(save_r_image("example:test", output = "x.zip", run_id = "bad"), silent = TRUE)
    stopifnot(inherits(bad, "try-error"),
              identical(read_checks("outputs/evidence/bad/save")$check, "Archive written"),
              identical(read_checks("outputs/evidence/bad/save")$status, "FAIL"))
    rows <- read_checks("outputs/evidence/export/sif")
    stopifnot(identical(rows$check, c("Archive entries safe", "Archive platform image",
                                      "SIF build", "SIF file system architecture",
                                      "SIF written")),
              all(rows$status == "PASS"),
              identical(rows$detail[[2L]], paste0("sha256:", strrep("1", 64L))),
              grepl("Squashfs", rows$detail[[4L]]),
              file.exists("outputs/evidence/export/sif/sif.tsv"))
    sif_items <- read_items("outputs/evidence/export/sif")
    stopifnot(identical(sif_items$check, c("Archive entries safe", "Archive platform image")),
              identical(sif_items$item, c("index.json", paste0("sha256:", strrep("1", 64L)))),
              identical(sif_items$expected, c("safe", "linux/arm64")),
              identical(sif_items$found, c("safe", "linux/arm64")),
              all(sif_items$status == "PASS"))
    versions <- read.delim("outputs/evidence/export/sif/tool-versions.tsv",
                           colClasses = "character")
    stopifnot(identical(tail(versions, 1L)$version, "apptainer version 1.5.5"))
    again <- try(save_r_sif("oci_arm64.tar", file.path(scratch, "oci_arm64.sif"),
                            run_id = "export-again"), silent = TRUE)
    stopifnot(inherits(again, "try-error"))
})
# --- 検査用の中では、使った検査用のイメージと定義のハッシュを道具の版に残し、外では残さない（README.md「本番の手順」）---
local({
    names <- c("AUDIT_RUNNER", "AUDIT_RUNNER_IMAGE", "AUDIT_RUNNER_IMAGE_ID",
               "AUDIT_RUNNER_DEFINITION")
    saved <- Sys.getenv(names, unset = NA)
    on.exit(for (name in names) {
        if (is.na(saved[[name]])) Sys.unsetenv(name) else do.call(Sys.setenv, as.list(saved[name]))
    })
    Sys.setenv(AUDIT_RUNNER = "")
    stopifnot(is.null(runner_rows()))
    Sys.setenv(AUDIT_RUNNER = "r", AUDIT_RUNNER_IMAGE = "container-audit-runner-r:local",
               AUDIT_RUNNER_IMAGE_ID = paste0("sha256:", strrep("a", 64L)),
               AUDIT_RUNNER_DEFINITION = strrep("b", 64L))
    rows <- runner_rows()
    stopifnot(identical(rows$tool, c("runner image", "runner image id", "runner definition")),
              identical(rows$version, c("container-audit-runner-r:local",
                                        paste0("sha256:", strrep("a", 64L)), strrep("b", 64L))))
})
# --- 監査のコンテナはネットワークなし・操作側の uid:gid で動かし、入力は読み取り専用で渡す（README.md「R」）---
with_work_directory("audit-command-", function(directory) {
    scope <- new.env(parent = environment(run_r_image_audit))
    audit <- run_r_image_audit
    environment(audit) <- scope
    image_id <- paste0("sha256:", strrep("d", 64L))
    manifest <- normalizePath("projects/example-r/request/DESCRIPTION")
    inspector <- normalizePath("R/dependencies-inspect.R")
    baseline <- file.path(directory, "baseline")
    confirmed <- file.path(directory, "confirmed")
    dir.create(baseline)
    dir.create(confirmed)
    scope$local_image_id <- function(...) image_id
    waited <- FALSE
    scope$wait_for_bind_sources <- function(paths, image, platform, log) {
        stopifnot(identical(paths, c(inspector, manifest, baseline, confirmed, directory)),
                  identical(image, image_id))
        waited <<- TRUE
    }
    sent <- list()
    scope$run_command <- function(command, args, log) {
        stopifnot(identical(command, "docker"))
        sent[[length(sent) + 1L]] <<- args
    }
    missing <- try(audit("example:test", directory, baseline, manifest, "linux/arm64", confirmed),
                   silent = TRUE)
    stopifnot(inherits(missing, "try-error"), grepl("r-audit.json", missing, fixed = TRUE))
    writeLines('{"status":"PASS"}', file.path(directory, "r-audit.json"))
    withr::with_dir(directory, audit("example:test", directory, baseline, manifest, "linux/arm64",
                                    confirmed))
    stopifnot(waited, length(sent) == 2L)
    writeLines('{"status":"REVIEW"}', file.path(directory, "r-audit.json"))
    writeLines(c("check\tstatus\tdetail", "依存\tREVIEW\t"), file.path(directory, "audit-checks.tsv"))
    details <- audit("example:test", directory, baseline, manifest, "linux/arm64", confirmed,
                     details = TRUE)
    stopifnot(identical(details, list(report = file.path(directory, "r-audit.json"),
                                      log = file.path(directory, "r-audit.log"),
                                      status = "REVIEW", review = "要確認: 依存")))
    args <- sent[[1L]]
    mounts <- args[which(args == "--mount") + 1L]
    user <- paste(system2("id", "-u", stdout = TRUE), system2("id", "-g", stdout = TRUE), sep = ":")
    stopifnot(identical(args[match("--network", args) + 1L], "none"),
              identical(args[match("--user", args) + 1L], user), image_id %in% args,
              identical(mounts, c(bind_mount(inspector, "/audit/inspect.R", "readonly"),
                                  bind_mount(manifest, "/audit/DESCRIPTION", "readonly"),
                                  bind_mount(directory, "/result"),
                                  bind_mount(baseline, "/audit/baseline", "readonly"),
                                  bind_mount(confirmed, "/audit/confirmed", "readonly"))))
})
# --- 監査は確定記録の中のファイルだけで条件を作り、照合を 3 行に残す（README.md「R」）---
local({
    root <- normalizePath(getwd())
    scratch <- tempfile("audit-lock-", tmpdir = file.path(root, "_tmp"))
    lock <- example_lock(file.path(scratch, "lock"))
    bundle <- dirname(lock)
    previous <- options(audit.evidence_root = file.path(scratch, "evidence"))
    originals <- list(run_command = run_command,
                      local_image_id = local_image_id,
                      wait_for_bind_sources = wait_for_bind_sources)
    on.exit({
        options(previous)
        for (name in names(originals)) stub(name, originals[[name]])
        unlink(scratch, recursive = TRUE)
    })
    seen <- new.env()
    stub("local_image_id", function(image, platform) paste0("sha256:", strrep("c", 64L)))
    stub("wait_for_bind_sources", function(...) invisible(NULL))
    stub("run_command", function(command, args, env = character(), log = NULL) {
        mounts <- args[which(args == "--mount") + 1L]
        seen$mounts <- setNames(sub('^.*"source=([^"]*)".*$', "\\1", mounts),
                                sub("^.*destination=([^,]*).*$", "\\1", mounts))
        writeLines('{"status":"PASS"}', file.path(seen$mounts[["/result"]], "r-audit.json"))
        write.table(data.frame(check = c("R packages", "Ubuntu packages"), status = "PASS",
                               detail = ""),
                    file.path(seen$mounts[["/result"]], "audit-checks.tsv"), sep = "\t",
                    row.names = FALSE, quote = FALSE)
    })
    result <- audit_r_image("example:test", lock, run_id = "audit-lock")
    directory <- file.path(scratch, "evidence", "audit-lock", "audit")
    description <- c("Package: LockOnly", "Imports: cli (>= 3.0)")
    stopifnot(identical(readLines(file.path(directory, "DESCRIPTION")), description),
              identical(unlist(jsonlite::read_json(file.path(directory,
                                                             "r-input.json"))$description),
                        description),
              identical(seen$mounts[["/audit/DESCRIPTION"]],
                        normalizePath(file.path(directory, "DESCRIPTION"))),
              identical(seen$mounts[["/audit/confirmed"]], normalizePath(bundle)),
              identical(seen$mounts[["/audit/baseline"]],
                        normalizePath(file.path(directory, "baseline"))))
    stopifnot(identical(readLines(file.path(directory, "baseline", "base-ubuntu.tsv")),
                        c(paste("#", analysis_base_image("projects/example-r/request"),
                                "linux/arm64"),
                          "Package\tArchitecture\tVersion\tStatus",
                          "bash\tarm64\t5.2\tinstalled")))
    checks <- read_checks(directory)
    stopifnot(identical(checks$check, c("Dependency lock", "Base inventory", "Audit checks")),
              all(checks$status == "PASS"), all(result$checks$status == "PASS"))
    summary <- jsonlite::read_json(file.path(directory, "result-summary.json"))
    environment <- read_build_environment(file.path(bundle, "build-environment.json"))
    stopifnot(identical(summary$status, "PASS"),
              identical(summary$base_image, analysis_base_image("projects/example-r/request")),
              identical(summary$input_hashes, environment$inputs))
    parameters <- jsonlite::read_json(file.path(directory, "parameters.json"))
    stopifnot(setequal(names(parameters), c("image", "platform", "dependency_lock",
                                            "base_image", "input_hashes")))
    detailed <- audit_r_image("example:test", lock, run_id = "audit-details", details = TRUE)
    stopifnot(identical(detailed$status, "PASS"), identical(detailed$run_id, "audit-details"),
              file.exists(detailed$report), file.exists(detailed$log),
              identical(detailed$evidence, detailed$directory))
})
# --- 確定記録が別のプラットフォームのものなら、docker を呼ばずに FAIL を残して止める（README.md「R」）---
local({
    root <- normalizePath(getwd())
    scratch <- tempfile("build-platform-", tmpdir = file.path(root, "_tmp"))
    lock <- example_lock(file.path(scratch, "lock"))
    project <- file.path(scratch, "projects", "test-r")
    dir.create(file.path(project, "request"), recursive = TRUE)
    dir.create(file.path(project, "dockerfile"))
    stopifnot(all(file.copy(file.path("projects/example-r/request", c("DESCRIPTION", "base.txt")),
                            file.path(project, "request"))))
    writeLines("FROM scratch", file.path(project, "dockerfile", "Dockerfile"))
    previous <- options(audit.projects_root = file.path(scratch, "projects"),
                        audit.evidence_root = file.path(scratch, "evidence"))
    originals <- list(run_command = run_command,
                      local_image_id = local_image_id)
    on.exit({
        options(previous)
        for (name in names(originals)) stub(name, originals[[name]])
        unlink(scratch, recursive = TRUE)
    })
    seen <- new.env()
    seen$calls <- list()
    stub("run_command", function(command, args, env = character(), log = NULL) {
        seen$calls[[length(seen$calls) + 1L]] <- args
    })
    stub("local_image_id", function(image, platform) {
        seen$calls[[length(seen$calls) + 1L]] <- image
        paste0("sha256:", strrep("c", 64L))
    })
    failed <- try(build_r_image(NULL, lock, "test-r", run_id = "other-platform",
                                platform = "linux/amd64"), silent = TRUE)
    directory <- file.path(scratch, "evidence", "other-platform", "build")
    rows <- read_checks(directory)
    summary <- jsonlite::read_json(file.path(directory, "result-summary.json"))
    stopifnot(inherits(failed, "try-error"),
              grepl("linux/amd64 ではありません", failed, fixed = TRUE),
              !length(seen$calls),
              identical(rows$check, "Dependency lock"), identical(rows$status, "FAIL"),
              identical(summary$status, "FAIL"))
})
# --- 保存の間にタグが別のイメージに移れば止め、FAIL を残して .tar を残さない（README.md「第 3 段階の中身」）---
local({
    root <- normalizePath(getwd())
    scratch <- tempfile("save-moved-", tmpdir = file.path(root, "_tmp"))
    dir.create(scratch)
    originals <- list(run_command = run_command,
                      local_image_id = local_image_id)
    on.exit({
        setwd(root)
        for (name in names(originals)) stub(name, originals[[name]])
        unlink(scratch, recursive = TRUE)
    })
    setwd(scratch)
    ids <- paste0("sha256:", strrep(c("e", "e", "f"), 64L))
    seen <- new.env()
    seen$count <- 0L
    stub("local_image_id", function(image, platform) {
        seen$count <- seen$count + 1L
        ids[[min(seen$count, 3L)]]
    })
    stub("run_command", function(command, args, env = character(), log = NULL) {
        writeLines("x", args[[match("--output", args) + 1L]])
    })
    failed <- try(save_r_image("example:test", run_id = "tag-moved"), silent = TRUE)
    directory <- file.path("outputs", "evidence", "tag-moved", "save")
    rows <- read_checks(directory)
    items <- read_items(directory)
    stopifnot(inherits(failed, "try-error"),
              grepl("Docker タグの実体が保存の開始時と一致しません", failed, fixed = TRUE),
              seen$count == 3L,
              identical(rows$check, c("Image platform", "Image unchanged during save",
                                      "Archive written")),
              identical(rows$status, c("PASS", "FAIL", "FAIL")),
              identical(items$check, "Image unchanged during save"),
              identical(items$expected, ids[[1L]]), identical(items$found, ids[[3L]]),
              identical(items$status, "FAIL"),
              !file.exists("outputs/images/tag-moved/example_test_arm64.tar"))
    seen$count <- 0L
    ids <- ids[c(1L, 3L)]
    called <- FALSE
    stub("run_command", function(...) called <<- TRUE)
    before_save <- try(save_r_image("example:test", run_id = "before-save"), silent = TRUE)
    stopifnot(inherits(before_save, "try-error"),
              grepl("保存の開始時と一致しません", before_save, fixed = TRUE),
              seen$count == 2L, !called,
              !file.exists("outputs/images/before-save/example_test_arm64.tar"))
})
# --- 調査は DESCRIPTION の版条件を解決した版と照合し、満たさなければ FAIL を残して止める（README.md「R」）---
local({
    root <- normalizePath(getwd())
    scratch <- tempfile("resolve-", tmpdir = file.path(root, "_tmp"))
    request <- file.path(scratch, "projects", "test-r", "request")
    dir.create(request, recursive = TRUE)
    stopifnot(file.copy("projects/example-r/request/base.txt", request))
    description <- file.path(request, "DESCRIPTION")
    writeLines(c("Package: x", "Depends: R (>= 4.1)", "Imports: cli (>= 3.0), magick"),
               description)
    previous <- options(audit.projects_root = file.path(scratch, "projects"),
                        audit.evidence_root = file.path(scratch, "evidence"))
    originals <- list(run_inspect_build = run_inspect_build,
                      local_image_id = local_image_id)
    on.exit({
        options(previous)
        for (name in names(originals)) stub(name, originals[[name]])
        unlink(scratch, recursive = TRUE)
    })
    seen <- new.env()
    seen$calls <- list()
    seen$ids <- 0L
    stub("run_inspect_build", function(destination, context, platform, target, log,
                                       packages = character(), extra = character(),
                                       apt_snapshot = NULL) {
        seen$calls[[length(seen$calls) + 1L]] <- list(packages = packages, extra = extra,
                                                      apt_snapshot = apt_snapshot)
        dir.create(destination, recursive = TRUE, showWarnings = FALSE)
        for (name in c("ubuntu-packages.tsv", "review.tsv", "base-r.tsv", "base-ubuntu.tsv")) {
            writeLines("x", file.path(destination, name))
        }
        writeLines('"language_package"\t"file"\t"soname"\t"path"',
                   file.path(destination, "native-libraries.tsv"))
        write.table(data.frame(package = c("cli", "magick"), version = c("3.6.0", "2.8.5")),
                    file.path(destination, "r-packages.tsv"), sep = "\t", row.names = FALSE)
        write.table(data.frame(check = c("R dependency resolution", "Ubuntu dependency plan"),
                               status = "PASS", detail = ""),
                    file.path(destination, "checks.tsv"), sep = "\t", row.names = FALSE)
        writeLines("0", file.path(destination, "r-status"))
        writeLines("0", file.path(destination, "ubuntu-status"))
        invisible(destination)
    })
    stub("local_image_id", function(image, platform) {
        seen$ids <- seen$ids + 1L
        paste0("sha256:", strrep("b", 64L))
    })
    result <- resolve_r_dependencies("test-r", "linux/arm64", SNAPSHOT,
                                     run_id = "resolve")
    directory <- file.path(scratch, "evidence", "resolve", "discover")
    stopifnot(length(seen$calls) == 1L, seen$ids == 1L,
              identical(seen$calls[[1L]]$packages, c("cli", "magick")),
              identical(seen$calls[[1L]]$extra, character()),
              identical(seen$calls[[1L]]$apt_snapshot, SNAPSHOT),
              identical(result$report, directory))
    checks <- read_checks(directory)
    stopifnot(identical(checks$check, c("R dependency resolution", "Ubuntu dependency plan",
                                        "DESCRIPTION requirements", "Build inputs unchanged")),
              all(checks$status == "PASS"))
    items <- read_items(directory)
    cli <- items[items$check == "DESCRIPTION requirements" & items$item == "cli", ]
    stopifnot(identical(unname(unlist(cli[c("expected", "found", "status")])),
                        c(">= 3.0", "3.6.0", "PASS")),
              "base.txt" %in% items$item[items$check == "Build inputs unchanged"],
              file.exists(file.path(directory, "native-libraries.tsv")))
    environment <- read_build_environment(file.path(directory, "build-environment.json"))
    stopifnot(identical(environment$base_image, analysis_base_image(request)),
              identical(environment$apt_snapshot, SNAPSHOT),
              is.null(environment$kind))
    writeLines(c("Package: x", "Depends: R (>= 4.1)", "Imports: cli (>= 4.0), magick"),
               description)
    failed <- try(resolve_r_dependencies("test-r", "linux/arm64", SNAPSHOT,
                                         run_id = "unmet"), silent = TRUE)
    rows <- read_checks(file.path(scratch, "evidence", "unmet", "discover"))
    stopifnot(inherits(failed, "try-error"),
              grepl("DESCRIPTION の版条件を満たす候補がありません: cli", failed, fixed = TRUE),
              length(seen$calls) == 2L, seen$ids == 1L,
              "DESCRIPTION requirements" %in% rows$check[rows$status == "FAIL"])
})
# --- APT スナップショットの形が誤りなら、調査は docker を呼ばずに止める（README.md「第 1 段階の中身」）---
local({
    root <- normalizePath(getwd())
    scratch <- tempfile("snapshot-", tmpdir = file.path(root, "_tmp"))
    request <- file.path(scratch, "projects", "test-r", "request")
    dir.create(request, recursive = TRUE)
    stopifnot(file.copy("projects/example-r/request/base.txt", request))
    writeLines(c("Package: x", "Imports: cli"), file.path(request, "DESCRIPTION"))
    previous <- options(audit.projects_root = file.path(scratch, "projects"),
                        audit.evidence_root = file.path(scratch, "evidence"))
    originals <- list(run_command = run_command, run_inspect_build = run_inspect_build)
    on.exit({
        options(previous)
        for (name in names(originals)) stub(name, originals[[name]])
        unlink(scratch, recursive = TRUE)
    })
    calls <- list()
    stub("run_command", function(...) calls[[length(calls) + 1L]] <<- list(...))
    stub("run_inspect_build", function(...) calls[[length(calls) + 1L]] <<- list(...))
    failed <- try(resolve_r_dependencies("test-r", "linux/arm64", "bad", run_id = "bad"),
                  silent = TRUE)
    stopifnot(inherits(failed, "try-error"),
              grepl("YYYYMMDDTHHMMSSZ", failed, fixed = TRUE),
              identical(length(calls), 0L),
              !dir.exists(file.path(scratch, "evidence")))
})
cat("R の操作側のテストに合格しました。\n")
