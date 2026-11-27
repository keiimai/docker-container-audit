# --- プロジェクト・入力・確定記録・SIF・.env のテスト（Docker なし。Python は tests/test_targets.py）（README.md「検査とテスト」）---
# --- このファイルを走らせる間だけ子の根に移る（README.md「テスト」）---
withr::local_dir(testthat::test_path("..", ".."))
# --- R/ の関数を全部読み込み、.env と記録層の一時ファイルの置き場所を整える（報告書の setup と同じ）---
for (path in list.files("R", pattern = "[.][Rr]$", full.names = TRUE)) source(path)
audit_setup()

# --- 式が止まるか（CHECKS.md 1 節）---
fails <- function(expr) inherits(try(expr, silent = TRUE), "try-error")
# --- 作業用の _tmp/ と、見本のプロジェクトの request/（README.md「検査とテスト」）---
dir.create("_tmp", showWarnings = FALSE)
example <- "projects/example-r/request"

# --- 見本のプロジェクトは request/ と dockerfile/ を持ち、targets/・dockerfiles/ は無い（README.md「検査対象のフォルダ」）---
stopifnot(identical(names(target_inputs(example)),
                    c("DESCRIPTION", "base.txt", "ubuntu-packages.txt")),
          identical(project_paths()$request, example),
          identical(project_paths("example-r")$dockerfile, "projects/example-r/dockerfile"),
          !dir.exists("targets/r"), !dir.exists("dockerfiles/r"),
          !file.exists(file.path(example, "Dockerfile")),
          file.exists("projects/example-r/dockerfile/Dockerfile"))
# --- 子の下の別のフォルダからも同じ見本の報告書入力を列挙する（README.md「構成」）---
local({
    discovery <- report_inputs("example-r", "discovery")
    lock <- "outputs/evidence/x/confirm/dependency-lock.tsv"
    image <- report_inputs("example-r", "image", lock)
    withr::with_dir("notebooks", {
        stopifnot(identical(report_inputs("example-r", "discovery"), discovery),
                  identical(report_inputs("example-r", "image", lock), image))
    })
})

# --- Dockerfile の入力には通常のファイルへのリンクを含め、ディレクトリのリンクはたどらない（README.md「第 3 段階の中身」）---
local({
    folder <- tempfile("dockerfile-links-", tmpdir = "_tmp")
    dir.create(file.path(folder, "assets"), recursive = TRUE)
    on.exit(unlink(folder, recursive = TRUE))
    writeLines("FROM example", file.path(folder, "Dockerfile"))
    writeLines("asset", file.path(folder, "assets", "a.txt"))
    stopifnot(file.symlink("assets", file.path(folder, "copy")),
              file.symlink("assets/a.txt", file.path(folder, "single")),
              file.symlink("single", file.path(folder, "alias")),
              identical(dockerfile_files(folder),
                        file.path(folder, c("Dockerfile", "alias", "assets/a.txt", "single"))))
})
# --- 見本の Dockerfile に取得元の時点が入り、導入時に確定記録の SHA-256 と照合する（README.md「第 3 段階の中身」）---
local({
    folder <- "projects/example-r/dockerfile"
    recipe <- readLines(file.path(folder, "Dockerfile"), warn = FALSE)
    installer <- readLines(file.path(folder, "install-dependencies.R"), warn = FALSE)
    install <- "Rscript --vanilla /opt/build/install-dependencies.R /opt/build/r-pins.tsv"
    stopifnot(!any(grepl("audit_lock", recipe, fixed = TRUE)),
              length(grep("^ARG APT_SNAPSHOT=[0-9]{8}T[0-9]{6}Z$", recipe)) == 1L,
              "for name in r-pins.tsv ubuntu-pins.txt; do" %in% recipe,
              any(grepl("< /opt/build/ubuntu-pins.txt", recipe, fixed = TRUE)),
              install %in% recipe,
              !any(grepl("install.packages(", recipe, fixed = TRUE)),
              any(grepl("sha256sum", installer, fixed = TRUE)),
              any(grepl("if (!identical(actual, pins$sha256))", installer, fixed = TRUE)))
})
# --- プロジェクト名の形、request/ の中身による言語の判定、根の差し替え（README.md「検査対象のフォルダ」）---
local({
    scratch <- tempfile("projects-", tmpdir = file.path(normalizePath(getwd()), "_tmp"))
    previous <- options(audit.projects_root = scratch)
    on.exit({
        options(previous)
        unlink(scratch, recursive = TRUE)
    })
    request <- function(name, files) {
        path <- file.path(scratch, name, "request")
        dir.create(path, recursive = TRUE)
        for (file in files) writeLines("x", file.path(path, file))
        path
    }
    request("my-r_2", c("DESCRIPTION", "base.txt"))
    request("py", "requirements.txt")
    request("both", c("DESCRIPTION", "requirements.txt"))
    request("none", "base.txt")
    stopifnot(identical(project_paths("my-r_2")$request, file.path(scratch, "my-r_2", "request")),
              identical(project_language(file.path(scratch, "py", "request")), "python"),
              identical(project_language(project = "py"), "python"),
              identical(target_inputs(project = "my-r_2"),
                        target_inputs(project_request("my-r_2"))),
              identical(project_name("a1-b_c"), "a1-b_c"))
    py <- tryCatch(project_paths("py"), error = conditionMessage)
    stopifnot(fails(project_name("Example")), fails(project_name("-x")), fails(project_name("a/b")),
              fails(project_name(c("a", "b"))), fails(project_name(NA_character_)),
              grepl("Python のプロジェクト", py, fixed = TRUE),
              fails(project_paths("both")), fails(project_paths("none")),
              fails(project_paths("missing")))
})
# --- request/ は DESCRIPTION と base.txt が必須で、ubuntu-packages.txt は任意（README.md「検査対象のフォルダ」）---
local({
    scratch <- tempfile("inputs-", tmpdir = file.path(normalizePath(getwd()), "_tmp"))
    dir.create(scratch)
    on.exit(unlink(scratch, recursive = TRUE))
    stopifnot(all(file.copy(file.path(example, c("DESCRIPTION", "base.txt")), scratch)))
    stopifnot(identical(names(target_inputs(scratch)), c("DESCRIPTION", "base.txt")),
              identical(target_ubuntu_packages(scratch), character()))
    unlink(file.path(scratch, "base.txt"))
    stopifnot(fails(target_inputs(scratch)), fails(target_inputs(file.path(scratch, "none"))))
})
# --- 設定ファイル projects/<名前>/config.yaml（project がフォルダの名前と違えば止める）（README.md「検査対象のフォルダ」）---
local({
    scratch <- tempfile("config-", tmpdir = file.path(normalizePath(getwd()), "_tmp"))
    previous <- options(audit.projects_root = scratch)
    on.exit({
        options(previous)
        unlink(scratch, recursive = TRUE)
    })
    dir.create(file.path(scratch, "test-r", "request"), recursive = TRUE)
    stopifnot(file.copy(file.path(example, "DESCRIPTION"), file.path(scratch, "test-r", "request")))
    config <- readLines("projects/example-r/config.yaml", encoding = "UTF-8")
    path <- file.path(scratch, "test-r", "config.yaml")
    renamed <- sub("^(experiment_name|project): example-r", "\\1: test-r", config)
    writeLines(renamed, path)
    cfg <- load_project_config("test-r")
    stopifnot(identical(cfg$project, "test-r"), identical(cfg$experiment_name, "test-r"),
              identical(cfg$platform, "linux/arm64"), identical(cfg$seed, 1L),
              identical(config_text(cfg$image$image, "image.image"), ""),
              identical(config_text(cfg$discovery$review_notes, "discovery.review_notes"), ""),
              isTRUE(config_flag(cfg$image$sif, "image.sif")))
    writeLines(sub("^experiment_name: example-r", "experiment_name: test-r", config), path)
    stopifnot(fails(load_project_config("test-r")))
    writeLines(sub("^platform: linux/arm64", "platform: linux/s390x", renamed), path)
    stopifnot(fails(load_project_config("test-r")))
    header <- c("project: test-r", "platform: linux/arm64")
    writeLines(c(header, "discovery: {}", "image: {}"), path)
    stopifnot(identical(names(load_project_config("test-r")$image), character()))
    for (sequence in c("[]", "[{}]")) {
        for (section in c("discovery", "image")) {
            values <- c(discovery = "{}", image = "{}")
            values[section] <- sequence
            writeLines(c(header, paste0(names(values), ": ", values)), path)
            error <- tryCatch(load_project_config("test-r"), error = identity)
            stopifnot(inherits(error, "error"),
                      grepl(paste0(section, ": の節"), conditionMessage(error), fixed = TRUE))
        }
    }
    unlink(path)
    stopifnot(fails(load_project_config("test-r")))
    stopifnot(fails(config_flag("yes", "x")), fails(config_flag(NA, "x")),
              fails(config_text(1, "x")), identical(config_text(NULL, "x"), ""))
    options(previous)
    stopifnot(identical(load_project_config("example-r")$project, "example-r"))
})
# --- 自動では分からない Ubuntu パッケージは、注釈と重複を除き、名前でない行で止める（README.md「検査対象のフォルダ」）---
local({
    listed <- tempfile("packages-", tmpdir = file.path(normalizePath(getwd()), "_tmp"),
                       fileext = ".txt")
    on.exit(unlink(listed))
    writeLines(c("# 注釈", "libmagick++-dev   # magick", "", "libmagick++-dev"), listed)
    stopifnot(identical(read_package_list(listed), "libmagick++-dev"))
    writeLines("Bad Name", listed)
    stopifnot(fails(read_package_list(listed)))
    stopifnot(identical(target_ubuntu_packages(example), character()))
})

# --- base.txt の読み方（KEY=VALUE。知らないキー・重なり・欠け・ダイジェスト無しで止める）（README.md「検査対象のフォルダ」）---
local({
    scratch <- tempfile("base-", tmpdir = file.path(normalizePath(getwd()), "_tmp"))
    dir.create(scratch)
    on.exit(unlink(scratch, recursive = TRUE))
    path <- file.path(scratch, "base.txt")
    digest <- strrep("d", 64L)
    image <- paste0("rocker/rstudio:4.5.3@sha256:", digest)
    repository <- "https://p3m.dev/cran/__linux__/noble/2026-04-23"
    base_line <- paste0("BASE_IMAGE=", image)
    repo_line <- paste0("R_REPOSITORY=", repository)
    read <- function(lines) {
        writeLines(lines, path)
        read_base_file(path, c("BASE_IMAGE", "R_REPOSITORY"))
    }
    stopifnot(identical(read(c("# 注釈", "", paste(base_line, "  # 固定ベース"), repo_line)),
                        c(BASE_IMAGE = image, R_REPOSITORY = repository)))
    stopifnot(fails(read(c(base_line, repo_line, "EXTRA=1"))),
              fails(read(c(base_line, base_line, repo_line))),
              fails(read(base_line)),
              fails(read(c("BASE_IMAGE=", repo_line))),
              fails(read(c(image, repo_line))),
              fails(read_base_file(file.path(scratch, "none.txt"), "BASE_IMAGE")))
    config <- function(lines) {
        writeLines(lines, path)
        target_config(scratch)
    }
    found <- config(c(base_line, repo_line))
    stopifnot(identical(found[["RSTUDIO_TAG"]], "4.5.3"),
              identical(found[["RSTUDIO_SHA256"]], digest),
              identical(found[["R_REPOSITORY"]], repository),
              identical(analysis_base_image(scratch), image))
    stopifnot(fails(config(c("BASE_IMAGE=rocker/rstudio:4.5.3", repo_line))),
              fails(config(c(paste0("BASE_IMAGE=ubuntu:24.04@sha256:", digest), repo_line))),
              fails(config(c(base_line, "R_REPOSITORY=https://p3m.dev/cran/latest"))))
    stopifnot(grepl("^rocker/rstudio:4[.]5[.]3@sha256:[a-f0-9]{64}$", analysis_base_image()))
})

# --- 確定の工程（調査記録の見本で通し、要確認・ハッシュの欠け・記録の変化では確定しない）（README.md「第 1 段階の中身」）---
local({
    root <- normalizePath(getwd())
    scratch <- tempfile("targets-", tmpdir = file.path(root, "_tmp"))
    dir.create(scratch)
    previous <- options(audit.projects_root = file.path(scratch, "projects"))
    on.exit({
        options(previous)
        setwd(root)
        unlink(scratch, recursive = TRUE)
    })
    project <- "test-r"
    context <- file.path(scratch, "projects", project, "request")
    dir.create(context, recursive = TRUE)
    stopifnot(all(file.copy(file.path(example, c("DESCRIPTION", "base.txt",
                                                 "ubuntu-packages.txt")), context)))
    stopifnot(identical(project_request(project), context))
    discovery <- function(name, base, r_rows, os_rows, platform = "linux/amd64",
                          review = character()) {
        d <- file.path(scratch, name)
        dir.create(file.path(d, "resolution"), recursive = TRUE)
        header <- paste("#", base, platform)
        writeLines(c(header, "Package\tVersion", "base\t4.5.3"), file.path(d, "base-r.tsv"))
        writeLines(c(header, "Package\tArchitecture\tVersion\tStatus",
                     "bash\tamd64\t5.2\tinstalled"),
                   file.path(d, "base-ubuntu.tsv"))
        write_image_record(r_rows, file.path(d, "r-packages.tsv"))
        write_image_record(os_rows, file.path(d, "ubuntu-packages.tsv"))
        write_image_record(data.frame(item = review,
                                      detail = rep("提供パッケージの候補 a, b", length(review))),
                           file.path(d, "review.tsv"))
        write_image_record(data.frame(
            check = c("R dependency resolution", "Ubuntu dependency plan"),
            status = c("PASS", if (length(review)) "REVIEW" else "PASS"), detail = ""),
            file.path(d, "checks.tsv"))
        saveRDS(list(), file.path(d, "resolution", "r-discovery.rds"))
        writeLines("20261101T000000Z", file.path(d, "apt-snapshot.txt"))
        write_discovery_environment(d, context, platform, "20261101T000000Z")
        list(report = d)
    }
    selected <- c("tidyverse", "grf", "policytree", "odbc", "testthat",
                  "DiagrammeR", "DiagrammeRsvg", "rsvg")
    r_rows <- data.frame(package = c(selected, "magick"),
                         version = c("2.0.0", "2.4.0", "1.2.3", "1.6.0", "3.2.0",
                                     "1.0.11", "0.1", "2.6.2", "2.9.0"),
                         relation = c(rep("selected", length(selected)), "direct"),
                         base_version = "", package_format = "binary", system_requirements = "",
                         needs_compilation = "no", sha256 = strrep("a", 64L))
    os_rows <- data.frame(package = c("libmagick++-6.q16-9t64", "libcurl4t64", "bash"),
                          architecture = "amd64",
                          version = c("8:6.9.12", "8.5.0", "5.2"),
                          relation = c("direct", "direct", "base"),
                          base_version = c("", "", "5.2"), action = c("install", "install", "keep"),
                          language_packages = c("magick", "curl", ""))
    base <- analysis_base_image(context)

    found <- discovery("confirmed", base, r_rows, os_rows)
    setwd(scratch)
    stopifnot(fails(confirm_r_dependencies(found, project, run_id = "wrong-platform",
                                           platform = "linux/arm64")))
    for (action in c("empty", "keep")) {
        rows <- if (action == "empty") os_rows[FALSE, ] else os_rows[os_rows$action == "keep", ]
        same <- discovery(action, base, r_rows, rows)
        no_change <- confirm_r_dependencies(same, project, run_id = action,
                                            platform = "linux/amd64")
        pins <- file.path(dirname(no_change$lock), "ubuntu-pins.txt")
        entries <- read.delim(no_change$lock, colClasses = "character")
        stopifnot(file.size(pins) == 0,
                  identical(entries$sha256[entries$path == "ubuntu-pins.txt"], file_sha256(pins)))
    }
    result <- confirm_r_dependencies(found, project, run_id = "confirmed")
    stopifnot(identical(result$checks$language_packages, nrow(r_rows)))
    stopifnot(identical(result$checks$status, "PASS"), !is.null(result$lock),
              identical(names(result$checks),
                        contract_table_columns("dependency-confirmation", "r")))
    lock <- result$lock
    bundle <- dirname(lock)
    stopifnot(identical(lock, "outputs/evidence/confirmed/confirm/dependency-lock.tsv"))
    stopifnot(setequal(read.delim(lock)$path, contract_lock_files("r")),
              "DESCRIPTION" %in% read.delim(lock)$path,
              identical(readLines(file.path(bundle, "DESCRIPTION")),
                        readLines(file.path(context, "DESCRIPTION"))))
    stopifnot(identical(readLines(file.path(bundle, "ubuntu-pins.txt")),
                        c("libmagick++-6.q16-9t64=8:6.9.12", "libcurl4t64=8.5.0")))
    environment <- read_build_environment(file.path(bundle, "build-environment.json"))
    stopifnot(identical(environment$apt_snapshot, "20261101T000000Z"),
              is.null(environment$kind),
              identical(environment$language, "r"), identical(environment$platform, "linux/amd64"),
              identical(environment$base_image, base),
              identical(environment$r_repository, target_config(context)[["R_REPOSITORY"]]),
              identical(environment$inputs[["DESCRIPTION"]],
                        file_sha256(file.path(bundle, "DESCRIPTION"))))
    confirmed <- verify_r_dependency_lock(lock, "linux/amd64")
    stopifnot(identical(confirmed$environment$base_image, base),
              identical(confirmed$bundle, normalizePath(bundle)))
    stopifnot(fails(verify_r_dependency_lock(lock, "linux/arm64")))
    items <- read.delim(file.path(bundle, "check-items.tsv"), colClasses = "character",
                        na.strings = NULL)
    stopifnot(all(items$status == "PASS"),
              identical(unique(items$check), c("Discovery unchanged", "DESCRIPTION requirements",
                                               "Pinned package hashes")),
              all(c("base_image", "input:DESCRIPTION", "input:base.txt",
                    "evidence:r-packages.tsv") %in%
                      items$item[items$check == "Discovery unchanged"]),
              !"kind" %in% items$item,
              identical(sum(items$check == "Pinned package hashes"), nrow(r_rows)),
              "magick 2.9.0" %in% items$item)

    inputs <- dockerfile_input_rows(lock, project)
    stopifnot(identical(names(inputs), contract_table_columns("dockerfile-inputs", "r")),
              identical(inputs$item, c("project", "base_image", "platform", "apt_snapshot",
                                       "dependency_lock", "ubuntu_pins", "language_pins",
                                       "r_repository")),
              identical(inputs$value, c(project, base, "linux/amd64", "20261101T000000Z",
                                        shown_path(lock),
                                        shown_path(file.path(bundle, "ubuntu-pins.txt")),
                                        shown_path(file.path(bundle, "r-pins.tsv")),
                                        environment$r_repository)),
              identical(nrow(dockerfile_input_rows(NULL, project)), 0L),
              identical(names(dockerfile_input_rows(NULL, project)),
                        contract_table_columns("dockerfile-inputs", "r")))

    cat("Note: changed after confirm\n", file = file.path(context, "DESCRIPTION"), append = TRUE)
    cat("# changed after confirm\n", file = file.path(context, "base.txt"), append = TRUE)
    stopifnot(identical(verify_r_dependency_lock(lock, "linux/amd64")$bundle,
                        normalizePath(bundle)))
    rehash <- function() {
        names <- contract_lock_files("r")
        write_image_record(data.frame(path = names, sha256 = vapply(
            file.path(bundle, names), file_sha256, character(1))), lock)
    }
    check_error <- function(path, message) {
        error <- tryCatch({
            verify_r_dependency_lock(path, "linux/amd64")
            NULL
        }, error = conditionMessage)
        stopifnot(!is.null(error), grepl(message, error, fixed = TRUE))
    }
    check_error(file.path(bundle, "other.tsv"), "dependency-lock.tsv を指定")
    entries <- read.delim(lock, colClasses = "character")
    entries$sha256[[1L]] <- "short"
    write_image_record(entries, lock)
    check_error(lock, "形式が不正")
    env_path <- file.path(bundle, "build-environment.json")
    original_env <- readLines(env_path)
    for (invalid in list(list(apt_snapshot = NULL), list(base_image = "rocker/rstudio:latest"))) {
        changed <- environment
        changed[names(invalid)] <- invalid
        write_build_environment(changed, env_path)
        rehash()
        check_error(lock, "固定ベース・APT スナップショット")
    }
    writeLines(original_env, env_path)
    baseline <- file.path(bundle, "base-ubuntu.tsv")
    original_base <- readLines(baseline)
    wrong_base <- original_base
    wrong_base[[1L]] <- sub("linux/amd64", "linux/arm64", wrong_base[[1L]], fixed = TRUE)
    writeLines(wrong_base, baseline)
    rehash()
    check_error(lock, "一覧の形式")
    writeLines(c(original_base, "other\tamd64\t1"), baseline)
    rehash()
    check_error(lock, "4列")
    writeLines(original_base, baseline)
    original <- readLines(file.path(bundle, "DESCRIPTION"))
    writeLines(c(original, "Imports: other"), file.path(bundle, "DESCRIPTION"))
    rehash()
    stopifnot(fails(verify_r_dependency_lock(lock)))
    writeLines(original, file.path(bundle, "DESCRIPTION"))
    rehash()
    stopifnot(identical(verify_r_dependency_lock(lock)$environment$platform, "linux/amd64"))
    cat("tampered\n", file = file.path(bundle, "ubuntu-pins.txt"), append = TRUE)
    stopifnot(fails(verify_r_dependency_lock(lock)))

    setwd(root)
    updated <- os_rows
    updated$version[3] <- "5.2-1"
    updated$action[3] <- "update"
    found <- discovery("update", base, r_rows, updated)
    setwd(scratch)
    result <- confirm_r_dependencies(found, project, run_id = "update")
    stopifnot(identical(result$checks$status, "REVIEW"), is.null(result$lock),
              identical(result$findings$item, "bash"),
              identical(nrow(dockerfile_input_rows(result$lock)), 0L))
    notes <- file.path(scratch, "review-notes.tsv")
    writeLines(c("item\tdetail\treason",
                 "bash\t固定ベースのパッケージを更新する候補\tセキュリティ更新を入れる"), notes)
    result <- confirm_r_dependencies(found, project, notes, run_id = "update-notes")
    stopifnot(identical(result$checks$status, "PASS"),
              "bash=5.2-1" %in% readLines(file.path(dirname(result$lock), "ubuntu-pins.txt")))
    mismatched <- file.path(scratch, "review-notes-mismatched.tsv")
    writeLines(c("item\tdetail\treason", "libbar.so.2\tx\ty"), mismatched)
    failed <- try(confirm_r_dependencies(found, project, mismatched,
                                         run_id = "update-mismatched"), silent = TRUE)
    stopifnot(inherits(failed, "try-error"),
              grepl("確認根拠の TSV が調査の要確認項目と一致しません", failed, fixed = TRUE))

    setwd(root)
    found <- discovery("review", base, r_rows, os_rows, review = "libfoo.so.1")
    setwd(scratch)
    result <- confirm_r_dependencies(found, project, run_id = "review")
    stopifnot(identical(result$checks$status, "REVIEW"), is.null(result$lock),
              identical(result$findings$item, "libfoo.so.1"))

    # 項目と詳細の組の中にタブがあっても、その組に書いた理由だけを採用する。
    findings <- data.frame(item = c("a\tb", "a"), detail = c("c", "b\tc"))
    write.table(findings, file.path(found$report, "review.tsv"), sep = "\t", row.names = FALSE,
                qmethod = "double")
    write_discovery_environment(found$report, context, "linux/amd64", "20261101T000000Z")
    notes_rows <- transform(findings, reason = c('  checked\t"both"  ', "NA"))
    write.table(notes_rows[1L, ], notes, sep = "\t", row.names = FALSE, qmethod = "double")
    partial <- confirm_r_dependencies(found, project, notes, run_id = "notes-partial")
    stopifnot(is.null(partial$lock), identical(partial$checks$unresolved, 1L))
    write.table(notes_rows, notes, sep = "\t", row.names = FALSE, qmethod = "double")
    recorded <- confirm_r_dependencies(found, project, notes, run_id = "notes-complete")
    decisions <- read.delim(file.path(dirname(recorded$lock), "dependency-review.tsv"),
                            colClasses = "character", na.strings = NULL)
    stopifnot(identical(decisions$decision, notes_rows$reason),
              identical(decisions[c("item", "detail")], findings))

    setwd(root)
    unhashed <- r_rows
    unhashed$sha256 <- ""
    found <- discovery("unhashed", base, unhashed, os_rows)
    setwd(scratch)
    stopifnot(fails(confirm_r_dependencies(found, project, run_id = "unhashed")))

    setwd(root)
    found <- discovery("tampered", base, r_rows, os_rows)
    setwd(scratch)
    cat("tampered\n", file = file.path(found$report, "review.tsv"), append = TRUE)
    stopifnot(fails(confirm_r_dependencies(found, project, run_id = "targets-tampered")))
    items <- read.delim("outputs/evidence/targets-tampered/confirm/check-items.tsv",
                        colClasses = "character", na.strings = NULL)
    stopifnot(identical(items$item[items$status == "FAIL"], "evidence:review.tsv"))

    setwd(root)
    broken <- rbind(r_rows, r_rows[1L, ])
    broken$sha256[2L] <- "short"
    found <- discovery("broken-pins", base, broken, os_rows)
    setwd(scratch)
    stopifnot(fails(confirm_r_dependencies(found, project, run_id = "targets-pins")))
    items <- read.delim("outputs/evidence/targets-pins/confirm/check-items.tsv",
                        colClasses = "character", na.strings = NULL)
    pins <- items[items$check == "Pinned package hashes", ]
    stopifnot(identical(nrow(pins), nrow(broken)),
              identical(pins$item, paste(broken$package, broken$version)),
              identical(pins$found, broken$sha256),
              identical(pins$status, c("PASS", "FAIL", rep("PASS", nrow(r_rows) - 2L), "FAIL")),
              !file.exists("outputs/evidence/targets-pins/confirm/dependency-lock.tsv"))
})
# --- 共有ライブラリの提供元は、1 つならそれ、ubuntu-packages.txt に書いた候補を優先し、決まらなければ要確認にする（README.md「第 1 段階の中身」）---
local({
    inspector <- new.env()
    sys.source("R/dependencies-inspect.R", envir = inspector)
    providers <- list("libgomp.so.1" = "libgomp1", "libfoo.so.1" = c("libfoo1", "libfoo1t64"),
                      "libbar.so.2" = c("libbar2", "libbar2-alt"))
    choice <- inspector$choose_providers(
        c("libbar.so.2", "libfoo.so.1", "libgomp.so.1"), providers, "libbar2")
    stopifnot(identical(choice$chosen, c("libbar.so.2" = "libbar2", "libgomp.so.1" = "libgomp1")),
              identical(choice$unresolved$item, "libfoo.so.1"),
              identical(choice$unresolved$detail, "提供パッケージの候補 libfoo1, libfoo1t64"))
    none <- inspector$choose_providers("libnone.so", list(), character())
    stopifnot(identical(none$chosen, character()), identical(none$unresolved$item, "libnone.so"),
              grepl("見つかりません", none$unresolved$detail, fixed = TRUE))
})
# --- 入れる前と後の一覧から計画を作り、提供元を direct にして、それを使う R パッケージを書く（README.md「第 1 段階の中身」）---
local({
    inspector <- new.env()
    sys.source("R/dependencies-inspect.R", envir = inspector)
    inventory <- function(package, version) {
        data.frame(Package = package, Architecture = "arm64", Version = version,
                   Status = "installed")
    }
    base <- inventory(c("bash", "libc6"), c("5.2", "2.39"))
    after <- inventory(c("bash", "libc6", "libxml2-dev", "libgomp1", "gcc-14-base"),
                       c("5.2", "2.40", "2.9", "14", "14"))
    libraries <- data.frame(r_package = c("xgboost", "lightgbm", "data.table", "xgboost"),
                            file = "libs/x.so",
                            soname = c("libgomp.so.1", "libgomp.so.1", "libc.so.6", "libc.so.6"),
                            path = c("not found", "not found", "/lib/libc.so.6", "/lib/libc.so.6"),
                            ubuntu = c("libgomp1", "libgomp1", "libc6", "libc6"))
    plan <- inspector$ubuntu_plan_rows(base, after, c("libgomp1", "libxml2-dev"), libraries)
    row <- function(name) as.list(plan[plan$package == name, ])
    stopifnot(identical(plan$package,
                        c("bash", "gcc-14-base", "libc6", "libgomp1", "libxml2-dev")),
              identical(c(row("libgomp1")$relation, row("libgomp1")$action),
                        c("direct", "install")),
              identical(row("libgomp1")$language_packages, "lightgbm, xgboost"),
              identical(c(row("gcc-14-base")$relation, row("gcc-14-base")$action),
                        c("indirect", "install")),
              identical(c(row("libc6")$action, row("libc6")$base_version), c("update", "2.39")),
              identical(c(row("bash")$relation, row("bash")$action), c("base", "keep")),
              identical(row("libc6")$language_packages, "data.table, xgboost"),
              identical(row("bash")$language_packages, ""),
              identical(row("libxml2-dev")$relation, "direct"))
    removed <- tryCatch(inspector$ubuntu_plan_rows(base, after[1L, ], character(), libraries),
                        error = conditionMessage)
    stopifnot(grepl("削除しました: libc6", removed, fixed = TRUE))
})
# --- 調査の環境は Python と同じ段の名前（Python は uv も）を持ち、環境テンプレートの扱い（MODE）は持たない（README.md「第 1 段階の中身」）---
local({
    recipe <- readLines("Dockerfile.inspect", warn = FALSE)
    stages <- sub("^FROM \\S+ AS (\\S+)$", "\\1", grep("^FROM \\S+ AS \\S+$", recipe, value = TRUE))
    stopifnot(identical(stages, c("audit_tools", "discovery", "dependency_report")),
              !any(grepl("MODE", recipe, fixed = TRUE)),
              !any(grepl("template", recipe, fixed = TRUE)))
})
# --- R の検査用と見本の Dockerfile は base.txt と同じ固定ベース（ダイジェスト。見本は取得元も）、道具は Python の子と同じ版（README.md「構成」）---
local({
    dockerfile_args <- function(path) {
        lines <- grep("^ARG [A-Z_]+=", readLines(path, warn = FALSE), value = TRUE)
        setNames(sub("^ARG [A-Z_]+=", "", lines), sub("^ARG ([A-Z_]+)=.*$", "\\1", lines))
    }
    ignored <- function(path) {
        lines <- readLines(path, warn = FALSE)
        lines[nzchar(lines) & !startsWith(lines, "#")]
    }
    r_runner <- dockerfile_args("Dockerfile.runner")
    r_base <- read_base_file(file.path(example, "base.txt"), c("BASE_IMAGE", "R_REPOSITORY"))
    stopifnot(identical(r_runner[["BASE_IMAGE"]], r_base[["BASE_IMAGE"]]),
              startsWith(r_runner[["BASE_IMAGE"]], "rocker/rstudio:4.5.3@"),
              all(grepl("@sha256:[a-f0-9]{64}$", r_runner[grepl("_IMAGE$", names(r_runner))])),
              !any(grepl("uv sync", readLines("Dockerfile.runner", warn = FALSE))))
    stopifnot(identical(ignored("Dockerfile.runner.dockerignore"),
                        c("*", "!renv-pins.tsv", "!tools/install-r-packages.R")))
    runner <- readLines("tools/runner.sh", warn = FALSE)
    stopifnot(paste("definition_files=(Dockerfile.runner renv.lock renv-pins.tsv",
                    "tools/install-r-packages.R)") %in% runner,
              any(grepl("container-audit-runner}-r:local", runner, fixed = TRUE)),
              !any(grepl("r|python", runner, fixed = TRUE)))
    sample <- dockerfile_args("projects/example-r/dockerfile/Dockerfile")
    stopifnot(identical(sample[["BASE_IMAGE"]], r_base[["BASE_IMAGE"]]),
              identical(sample[["R_REPOSITORY"]], r_base[["R_REPOSITORY"]]))

    python <- file.path("..", "docker_container_audit_python")
    if (!dir.exists(python)) {
        cat("SKIP: ../docker_container_audit_python が無いので、Python の子との比較は飛ばします\n")
        return(invisible(NULL))
    }
    stopifnot(identical(ignored(file.path(python, "Dockerfile.runner.dockerignore")),
                        c("*", "!pyproject.toml", "!uv.lock", "!.python-version")))
    py_runner <- dockerfile_args(file.path(python, "Dockerfile.runner"))
    py_base <- read_base_file(file.path(python, "projects", "example-python", "request",
                                        "base.txt"), "BASE_IMAGE")
    stopifnot(identical(py_runner[["BASE_IMAGE"]], py_base[["BASE_IMAGE"]]),
              grepl("^ubuntu:[0-9.]+@sha256:[a-f0-9]{64}$", py_base[["BASE_IMAGE"]]))
    for (name in c("DOCKER_CLI_IMAGE", "BUILDX_IMAGE", "APT_SNAPSHOT")) {
        stopifnot(identical(r_runner[[name]], py_runner[[name]]))
    }
    stopifnot(all(grepl("@sha256:[a-f0-9]{64}$", py_runner[grepl("_IMAGE$", names(py_runner))])),
              !any(grepl("renv", readLines(file.path(python, "Dockerfile.runner"),
                                           warn = FALSE))),
              identical(trimws(readLines(file.path(python, ".python-version"),
                                         warn = FALSE)[[1L]]), "3.12"))
})

# --- SIF の .tar（目次をイメージ 1 つに絞る・リンクで止める・読み戻しの照合・保存先）（README.md「Apptainer の SIF」）---
local({
    scratch <- tempfile("sif-", tmpdir = file.path(normalizePath(getwd()), "_tmp"))
    dir.create(file.path(scratch, "src"), recursive = TRUE)
    on.exit(unlink(scratch, recursive = TRUE))
    image <- list(digest = paste0("sha256:", strrep("1", 64L)),
                  platform = list(architecture = "arm64", os = "linux"))
    amd64 <- list(digest = paste0("sha256:", strrep("3", 64L)),
                  platform = list(architecture = "amd64", os = "linux"))
    attestation <- list(digest = paste0("sha256:", strrep("2", 64L)),
                        annotations = list("io.containerd.manifest.subject" = image$digest))
    oci_archive <- function(name, manifests) {
        index <- list(schemaVersion = 2L, manifests = manifests)
        writeLines(jsonlite::toJSON(index, auto_unbox = TRUE),
                   file.path(scratch, "src", "index.json"))
        path <- file.path(scratch, name)
        withr::with_dir(file.path(scratch, "src"), utils::tar(path, "index.json"))
        path
    }
    work <- file.path(scratch, "work")
    dir.create(work)
    one <- oci_archive("one.tar", list(image, attestation, amd64))
    stopifnot(identical(sif_layout(one, work, "linux/arm64"), image$digest))
    stopifnot(fails(sif_layout(one, work, "linux/arm64")))
    kept <- jsonlite::read_json(file.path(work, "layout", "index.json"))$manifests
    stopifnot(length(kept) == 1L, identical(kept[[1L]]$digest, image$digest))
    other <- file.path(scratch, "other")
    dir.create(other)
    stopifnot(fails(sif_layout(oci_archive("none.tar", list(attestation)), other,
                               "linux/arm64")))
    plain <- list(digest = paste0("sha256:", strrep("5", 64L)))
    config <- paste0("sha256:", strrep("6", 64L))
    dir.create(file.path(scratch, "src", "blobs", "sha256"), recursive = TRUE)
    writeLines(jsonlite::toJSON(list(config = list(digest = config)), auto_unbox = TRUE),
               file.path(scratch, "src", "blobs", "sha256", strrep("5", 64L)))
    writeLines(jsonlite::toJSON(list(architecture = "arm64", os = "linux"), auto_unbox = TRUE),
               file.path(scratch, "src", "blobs", "sha256", strrep("6", 64L)))
    index <- list(schemaVersion = 2L, manifests = list(plain))
    writeLines(jsonlite::toJSON(index, auto_unbox = TRUE), file.path(scratch, "src", "index.json"))
    plain_tar <- file.path(scratch, "plain.tar")
    withr::with_dir(file.path(scratch, "src"), utils::tar(plain_tar, c("index.json", "blobs")))
    index$manifests <- list(c(plain, list(platform = NULL)))
    jsonlite::write_json(index, file.path(scratch, "src", "index.json"),
                         auto_unbox = TRUE, null = "null")
    null_tar <- file.path(scratch, "null.tar")
    withr::with_dir(file.path(scratch, "src"), utils::tar(null_tar, c("index.json", "blobs")))
    null_work <- file.path(scratch, "null")
    dir.create(null_work)
    stopifnot(identical(sif_layout(null_tar, null_work, "linux/arm64"), plain$digest))
    unlink(file.path(scratch, "src", "blobs"), recursive = TRUE)
    plain_work <- file.path(scratch, "plain")
    dir.create(plain_work)
    stopifnot(identical(sif_layout(plain_tar, plain_work, "linux/arm64"), plain$digest))
    plain_amd64 <- file.path(scratch, "plain-amd64")
    dir.create(plain_amd64)
    stopifnot(fails(sif_layout(plain_tar, plain_amd64, "linux/amd64")))
    file.symlink("index.json", file.path(scratch, "src", "link.json"))
    linked <- file.path(scratch, "linked.tar")
    withr::with_dir(file.path(scratch, "src"),
                    utils::tar(linked, c("index.json", "link.json"), tar = "tar"))
    unsafe <- file.path(scratch, "unsafe")
    dir.create(unsafe)
    seen <- new.env()
    seen$rows <- NULL
    capture <- function(check, item, expected = "", found = "", status = NULL) {
        seen$rows <- rbind(seen$rows, check_item_rows(check, item, expected, found, status))
    }
    stopifnot(fails(sif_layout(linked, unsafe, "linux/arm64", items = capture)),
              !file.exists(file.path(unsafe, "layout", "index.json")))
    stopifnot(identical(seen$rows$item, c("index.json", "link.json")),
              identical(seen$rows$found, c("safe", "link")),
              identical(seen$rows$status, c("PASS", "FAIL")))
    seen$rows <- NULL
    two <- file.path(scratch, "two")
    dir.create(two)
    second <- modifyList(image, list(digest = paste0("sha256:", strrep("4", 64L))))
    stopifnot(fails(sif_layout(oci_archive("two.tar", list(image, second)), two,
                               "linux/arm64", items = capture)))
    platform_rows <- seen$rows[seen$rows$check == "Archive platform image", ]
    stopifnot(identical(platform_rows$item, c(image$digest, second$digest)),
              all(platform_rows$status == "FAIL"))
    readback_rows <- readback_item_rows(list(id = "sha256:1", layers = c("sha256:7", "sha256:8")),
                                        list(id = "sha256:1", layers = "sha256:7"))
    stopifnot(identical(readback_rows$item, c("image ID", "layer 1", "layer 2")),
              identical(readback_rows$found, c("sha256:1", "sha256:7", "")),
              identical(readback_rows$status, c("PASS", "PASS", "FAIL")))
    withr::with_dir(scratch, stopifnot(
        endsWith(artifact_path("x_arm64.tar", ".tar"), "/images/x_arm64.tar"),
        fails(artifact_path("x.zip", ".tar")),
        fails(artifact_path(file.path(dirname(getwd()), "outside.tar"), ".tar")),
        fails(artifact_path(one, ".tar"))))
    stopifnot(identical(contract_apptainer_image("linux/arm64"),
                        paste0("ghcr.io/apptainer/apptainer@sha256:",
                               "167bf566808fd902a7722fe7c98c37f5f0f71f5e4f35dbac64a50f13f4352ef3")))
})
# --- 入口は .env の値を環境変数に読み込み、既にある変数は上書きしない（REPRODUCIBILITY.md 4 節）---
local({
    env_file <- tempfile("env-", tmpdir = file.path(normalizePath(getwd()), "_tmp"))
    on.exit(unlink(env_file))
    writeLines(c("# 注釈", "AUDIT_TEST_A=1   # 行末の注釈", "AUDIT_TEST_B=\"x\"", "AUDIT_TEST_C=new",
                 "AUDIT_TEST_A=2"), env_file)
    withr::with_envvar(c(AUDIT_TEST_A = NA, AUDIT_TEST_B = NA, AUDIT_TEST_C = "old"), {
        loaded <- load_env_file(env_file)
        stopifnot(setequal(loaded, c("AUDIT_TEST_A", "AUDIT_TEST_B")),
                  identical(Sys.getenv(c("AUDIT_TEST_A", "AUDIT_TEST_B", "AUDIT_TEST_C"),
                                       names = FALSE), c("2", "x", "old")))
    })
    stopifnot(identical(load_env_file(file.path(dirname(env_file), "missing.env")), character()))
})
cat("R のプロジェクトと入力のテストに合格しました。\n")
