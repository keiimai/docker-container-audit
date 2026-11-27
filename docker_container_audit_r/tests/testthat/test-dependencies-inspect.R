# --- コンテナの中の依存の監査と、共有ライブラリの調べ方のテスト（Python は tests/test_dependencies_inspect.py）（CHECKS.md 1 節）---
# --- このファイルを走らせる間だけ子の根に移る（README.md「テスト」）---
withr::local_dir(testthat::test_path("..", ".."))

# --- 依存の関係と版条件を割り出し、版条件を満たさなければ止める（README.md「R」）---
testthat::test_that("依存の関係と版条件を調べる", {
    inspector <- new.env()
    sys.source("R/dependencies-inspect.R", envir = inspector)

    base <- rbind(BasePkg = c("BasePkg", "1.0.0", NA, NA, NA, NA))
    snapshot <- rbind(
        MainPkg = c("MainPkg", "3.0.0", NA, "MidPkg (>= 2.0.0)", NA, NA),
        MidPkg = c("MidPkg", "2.0.0", NA, "BasePkg (>= 1.0.0)", NA, NA)
    )
    colnames(base) <- colnames(snapshot) <- c(
        "Package", "Version", "Depends", "Imports", "LinkingTo", "SystemRequirements"
    )
    inspector$installed.packages <- function(...) base
    inspector$available.packages <- function(...) snapshot

    plan <- inspector$get_r_plan("MainPkg", "fixture")$plan
    stopifnot(identical(plan$package, c("BasePkg", "MainPkg", "MidPkg")),
              identical(plan$relation, c("indirect", "selected", "direct")),
              identical(plan$base_version, c("1.0.0", NA_character_, NA_character_)))
    snapshot["MidPkg", "Version"] <- "1.0.0"
    error <- tryCatch(inspector$get_r_plan("MainPkg", "fixture"), error = identity)
    testthat::expect_no_error(stopifnot(
        inherits(error, "error"),
        grepl("MidPkg (>= 2.0.0)", conditionMessage(error), fixed = TRUE)
    ))
})

# --- 同じ名前のパッケージが 2 つ入っていれば、確定記録の版の照合を合格にしない（Python と同じ）（README.md「R」）---
testthat::test_that("同名のパッケージが重複すれば不合格にする", {
    inspector <- new.env()
    sys.source("R/dependencies-inspect.R", envir = inspector)
    pins <- data.frame(package = c("alpha", "beta"), version = c("1.0", "2.0"),
                       sha256 = strrep("a", 64L))
    actual <- c(alpha = "1.0", beta = "2.0")
    single <- inspector$audit_confirmed_r(actual, character(), pins)
    twice <- inspector$audit_confirmed_r(actual, character(), pins,
                                         list(alpha = c("1.0", "0.9"), beta = "2.0"))
    rows <- twice$items[twice$items$check == "Confirmed R versions", ]
    testthat::expect_no_error(stopifnot(
        !length(single$versions), length(twice$versions) == 1L,
        identical(rows$status, c("FAIL", "PASS")),
        identical(rows$found[[1L]], "1.0, 0.9")
    ))
})

# --- r-audit.json の文は、base R だけで正しい JSON になる（jsonlite で読み戻せる）（README.md「R」）---
testthat::test_that("監査の JSON を読み戻せる", {
    inspector <- new.env()
    sys.source("R/dependencies-inspect.R", envir = inspector)
    value <- list(status = "PASS", text = "a\"b\\c\nd\t\r\b\f\001", missing = character(),
                  count = 3L, ok = TRUE, none = NULL,
                  rows = data.frame(name = c("x", "y"), version = c("1", "2")))
    back <- jsonlite::fromJSON(inspector$json_text(value), simplifyVector = FALSE)
    testthat::expect_no_error(stopifnot(
        identical(back$text, value$text), identical(back$missing, list()),
        identical(back$count, 3L), isTRUE(back$ok), is.null(back$none),
        identical(back$rows[[2L]]$version, "2")
    ))
})

# --- 監査の照合: 確定記録の版との違いと、固定ベースとの差分（確定した更新は除く）（README.md「R」）---
testthat::test_that("確定版と固定ベースとの差分を見つける", {
    inspector <- new.env()
    sys.source("R/dependencies-inspect.R", envir = inspector)
    pins <- data.frame(package = c("a", "b", "c"), version = c("1", "2", "3"))
    stopifnot(identical(inspector$audit_pin_errors(pins, c(a = "1", b = "9")),
                        c("b expected 2, found 9", "c expected 3, found ")))
    baseline <- data.frame(Package = c("bash", "libc6", "gone"),
                           Architecture = c("arm64", "arm64", "all"),
                           Version = c("5.2", "2.39", "1"))
    current <- data.frame(Package = c("bash", "libc6"), Architecture = c("arm64", "arm64"),
                          Version = c("5.2", "2.40"))
    testthat::expect_no_error(stopifnot(
        identical(inspector$audit_base_changes(baseline, current),
                  c("libc6:arm64", "gone:all")),
        identical(inspector$audit_base_changes(baseline, current, "libc6:arm64"),
                  "gone:all"),
        identical(inspector$audit_base_changes(baseline, current, "libc6:amd64"),
                  c("libc6:arm64", "gone:all")),
        identical(inspector$audit_base_changes(baseline, current,
                                               c("libc6:arm64", "gone:all")),
                  "gone:all")
    ))
})

# --- 確定記録の Ubuntu と R の版・導入記録の SHA-256・項目の行・固定ベース・OS・Quarto の照合と、コマンドの失敗の残し方（README.md「R」）---
testthat::test_that("版・導入記録・OS・Quarto を照合する", {
    inspector <- new.env()
    sys.source("R/dependencies-inspect.R", envir = inspector)
    current <- data.frame(Package = c("libgomp1", "extra"), Architecture = c("amd64", "all"),
                          Version = c("2", "1"))
    plan <- data.frame(package = c("libgomp1", "libfoo1"), architecture = c("amd64", "amd64"),
                       version = c("1", "3"), base_version = c(NA, NA),
                       action = c("install", "install"))
    found <- inspector$audit_confirmed_ubuntu(current, plan)
    stopifnot(identical(found$versions, c("libfoo1:amd64 expected 3, found ",
                                          "libgomp1:amd64 expected 1, found 2")),
              identical(found$unlisted, "extra:all"),
              identical(found$missing, "libfoo1:amd64"))
    plan <- data.frame(package = c("libgomp1", "extra"), architecture = c("amd64", "all"),
                       version = c("2", "1"), base_version = c(NA, "1"),
                       action = c("install", "keep"))
    found <- inspector$audit_confirmed_ubuntu(current, plan)
    stopifnot(!length(found$versions), !length(found$unlisted), !length(found$missing))
    plan$architecture[[1L]] <- "arm64"
    found <- inspector$audit_confirmed_ubuntu(current, plan)
    stopifnot(identical(found$unlisted, "libgomp1:amd64"),
              identical(found$missing, "libgomp1:arm64"))
    stopifnot(identical(found$items$check, c("Confirmed Ubuntu versions",
                                             "Unlisted Ubuntu additions",
                                             "Confirmed Ubuntu additions missing or ambiguous")),
              identical(found$items$item, c("libgomp1:arm64", "libgomp1:amd64", "libgomp1:arm64")),
              identical(found$items$expected, c("2", "", "2")),
              identical(found$items$found, c("", "2", "")),
              identical(found$items$status, c("FAIL", "REVIEW", "FAIL")))

    pins <- data.frame(package = c("cli", "glue"), version = c("3.6.0", "1.8.0"),
                       sha256 = c(strrep("a", 64L), strrep("b", 64L)))
    actual <- c(base = "4.5.3", cli = "3.6.0", glue = "1.7.0", extra = "0.1")
    confirmed <- inspector$audit_confirmed_r(actual, "base", pins)
    stopifnot(identical(confirmed$versions, "glue expected 1.8.0, found 1.7.0"),
              identical(confirmed$unlisted, "extra"),
              identical(confirmed$items$check, c("Confirmed R versions", "Confirmed R versions",
                                                 "Unlisted R additions")),
              identical(confirmed$items$status, c("PASS", "FAIL", "REVIEW")))
    confirmed <- inspector$audit_confirmed_r(actual[c("base", "cli")], "base", pins)
    stopifnot(identical(confirmed$versions, "glue expected 1.8.0, found "),
              !length(confirmed$unlisted),
              identical(confirmed$items$status, c("PASS", "FAIL")))

    missing <- inspector$audit_r_install_report(pins, NULL)
    stopifnot(identical(missing$status, "REVIEW"),
              identical(missing$errors, "ビルドの導入記録が無いので、SHA-256 は照合していない"),
              identical(missing$items$status, c("REVIEW", "REVIEW")),
              identical(missing$items$expected, paste(pins$version, pins$sha256)))
    stopifnot(identical(inspector$audit_r_install_report(pins, pins)$status, "PASS"),
              all(inspector$audit_r_install_report(pins, pins)$items$status == "PASS"))
    changed <- pins
    changed$sha256[[2L]] <- strrep("c", 64L)
    changed <- rbind(changed, data.frame(package = "extra", version = "0.1",
                                         sha256 = strrep("d", 64L)))
    differs <- inspector$audit_r_install_report(pins, changed)
    stopifnot(identical(differs$status, "FAIL"), length(differs$errors) == 3L,
              identical(differs$items$item, c("cli", "glue", "extra")),
              identical(differs$items$status, c("PASS", "FAIL", "FAIL")))

    rows <- inspector$audit_items("Confirmed R versions", c("a", "b"), c("1", "2"), c("1", NA))
    stopifnot(identical(rows$status, c("PASS", "FAIL")), identical(rows$found, c("1", "")),
              identical(names(rows), c("check", "item", "expected", "found", "status")))
    baseline <- data.frame(Package = c("bash", "libc6", "gone"),
                           Architecture = c("arm64", "arm64", "all"),
                           Version = c("5.2", "2.39", "1"))
    current <- data.frame(Package = c("bash", "libc6"), Architecture = c("arm64", "arm64"),
                          Version = c("5.2", "2.40"))
    base_rows <- inspector$audit_base_items(baseline, current, "libc6:arm64")
    stopifnot(identical(base_rows$item, c("bash:arm64", "libc6:arm64", "gone:all")),
              identical(base_rows$found, c("5.2", "2.40", "")),
              identical(base_rows$status, c("PASS", "PASS", "FAIL")),
              identical(inspector$audit_base_items(baseline, current)$status,
                        c("PASS", "FAIL", "FAIL")))

    debian <- inspector$audit_os_release(c("ID=debian", 'PRETTY_NAME="Debian 12"'))
    quoted <- inspector$audit_os_release(c("ID='ubuntu'", "PRETTY_NAME='Ubuntu Linux'"))
    stopifnot(identical(quoted, list(name = "Ubuntu Linux", failure = character())))
    escaped <- inspector$audit_os_release(c("ID=alpine", "ID=ubuntu",
                                            'PRETTY_NAME="My \\"OS\\""'))
    stopifnot(identical(escaped, list(name = 'My "OS"', failure = character())))
    alpine <- inspector$audit_os_release(c("ID=alpine", 'PRETTY_NAME="Alpine Linux"'))
    stopifnot(identical(debian, list(name = "Debian 12", failure = character())),
              identical(alpine$failure, "Not Debian/Ubuntu: Alpine Linux"))

    stopifnot(identical(inspector$audit_quarto("")$status, "REVIEW"))
    fake <- tempfile("quarto-", tmpdir = file.path(normalizePath(getwd()), "_tmp"))
    writeLines("#!/bin/sh", fake)
    Sys.chmod(fake, "0755")
    empty <- inspector$audit_quarto(fake)
    stopifnot(identical(empty$found, ""), identical(empty$status, "PASS"))
    writeLines(c("#!/bin/sh", "echo 1.9.36"), fake)
    Sys.chmod(fake, "0755")
    found <- inspector$audit_quarto(fake)
    writeLines(c("#!/bin/sh", "echo boom", "exit 1"), fake)
    broken <- inspector$audit_quarto(fake)
    unlink(fake)
    stopifnot(identical(found$status, "PASS"), identical(found$found, "1.9.36"),
              identical(found$detail, paste0("quarto 1.9.36 (", fake, ")")),
              identical(broken$status, "FAIL"), identical(broken$detail, "boom"))

    failed <- structure(c("Reading package lists...", "E: broken"), status = 100L)
    testthat::expect_no_error(stopifnot(
        identical(inspector$command_failure(failed), "Reading package lists...; E: broken"),
        identical(inspector$command_failure("Reading package lists..."), character()),
        identical(inspector$command_failure(structure(character(), status = 1L)),
                  "command failed")
    ))
})

# --- ELF の機械の種類と共有ライブラリの検査（ELF でないか短すぎる見出しは NA、違う機械用は調べず、ldd が無ければ REVIEW）（README.md「R」）---
testthat::test_that("共有ライブラリの欠けと CPU の違いを調べる", {
    inspector <- new.env()
    sys.source("R/dependencies-inspect.R", envir = inspector)
    scratch <- tempfile("elf-", tmpdir = file.path(normalizePath(getwd()), "_tmp"))
    dir.create(scratch, recursive = TRUE)
    withr::defer(unlink(scratch, recursive = TRUE))
    elf_header <- function(machine) {
        c(as.raw(c(0x7f, 0x45, 0x4c, 0x46, 2, 1, 1)), as.raw(rep(0L, 11L)),
          as.raw(c(machine %% 256L, machine %/% 256L)))
    }
    writeBin(elf_header(62L), file.path(scratch, "amd64.so"))
    writeBin(elf_header(183L), file.path(scratch, "arm64.so"))
    writeBin(charToRaw("not elf"), file.path(scratch, "text.so"))
    writeBin(as.raw(c(0x7f, 0x45, 0x4c, 0x46, 2, 1, 1)), file.path(scratch, "short.so"))
    stopifnot(identical(inspector$elf_machine(file.path(scratch, "amd64.so")), 62L),
              identical(inspector$elf_machine(file.path(scratch, "arm64.so")), 183L),
              is.na(inspector$elf_machine(file.path(scratch, "text.so"))),
              is.na(inspector$elf_machine(file.path(scratch, "short.so"))),
              identical(unname(inspector$ELF_MACHINES["aarch64"]), 183L))

    package <- file.path(scratch, "lib", "examplepkg")
    dir.create(file.path(package, "libs"), recursive = TRUE)
    file.copy(file.path(scratch, c("arm64.so", "amd64.so", "text.so")), file.path(package, "libs"))
    bin <- file.path(scratch, "bin")
    dir.create(bin)
    writeLines(c("#!/bin/sh", "echo '  libfound.so.1 => /usr/lib/libfound.so.1 (0x1)'",
                 "echo '  libmissing.so.2 => not found'", "exit 1"), file.path(bin, "ldd"))
    writeLines(c("#!/bin/sh", 'echo "libfound1:arm64: $2"'), file.path(bin, "dpkg-query"))
    Sys.chmod(file.path(bin, c("ldd", "dpkg-query")), "0755")
    path <- Sys.getenv("PATH")
    Sys.setenv(PATH = paste(bin, path, sep = ":"))
    native <- inspector$inspect_shared_libraries(c(examplepkg = package), machine = "aarch64")
    Sys.setenv(PATH = path)
    arm64 <- file.path(package, "libs", "arm64.so")
    stopifnot(identical(native$status, "FAIL"), identical(native$scanned, 1L),
              identical(native$missing, paste0("libmissing.so.2: ", arm64)),
              identical(native$foreign, file.path(package, "libs", "amd64.so")),
              identical(native$libraries$soname, c("libfound.so.1", "libmissing.so.2")),
              identical(native$libraries$owners, c("libfound1:arm64", "")),
              identical(native$libraries$language_package, rep("examplepkg", 2L)))
    Sys.setenv(PATH = file.path(scratch, "none"))
    unavailable <- inspector$inspect_shared_libraries(c(examplepkg = package))
    Sys.setenv(PATH = path)
    testthat::expect_no_error(stopifnot(
        identical(unavailable$status, "REVIEW")
    ))
})

# --- 監査の入口を、作業用のフォルダの偽のファイルと偽のコマンドで走らせ、結果を読み戻す（README.md「R」）---
run_fake_audit <- function(requirement, dependencies, broken = character(), install_report = TRUE,
                           configure = NULL) {
    inspector <- new.env()
    sys.source("R/dependencies-inspect.R", envir = inspector)
    scratch <- tempfile("audit-", tmpdir = file.path(normalizePath(getwd()), "_tmp"))
    on.exit(unlink(scratch, recursive = TRUE))
    for (folder in c("audit/confirmed", "result", "baseline")) {
        dir.create(file.path(scratch, folder), recursive = TRUE)
    }
    redirect <- function(path) {
        if (identical(path, file.path(R.home(), "r-install-report.tsv"))) {
            file.path(scratch, "r-install-report.tsv")
        } else if (is.character(path) && length(path) == 1L && grepl("^/(audit|result)/", path)) {
            file.path(scratch, sub("^/", "", path))
        } else {
            path
        }
    }
    writeLines(c("Package: fixture", paste("Imports:", requirement)),
               file.path(scratch, "audit", "DESCRIPTION"))
    writeLines(c("package\tversion\tsha256", paste0("alphapkg\t1.2\t", strrep("a", 64L)),
                 paste0("betapkg\t1.0\t", strrep("b", 64L))),
               file.path(scratch, "audit", "confirmed", "r-pins.tsv"))
    if (install_report) {
        file.copy(file.path(scratch, "audit", "confirmed", "r-pins.tsv"),
                  file.path(scratch, "r-install-report.tsv"))
    }
    writeLines(c("package\tarchitecture\tversion\tbase_version\taction",
                 "bash\tarm64\t5.2\t5.2\tkeep"),
               file.path(scratch, "audit", "confirmed", "ubuntu-packages.tsv"))
    writeLines(c(sprintf("# rocker/rstudio:%s@sha256:%s linux/arm64", getRversion(),
                         strrep("0", 64L)), "Package\tVersion", "basepkg\t1.0"),
               file.path(scratch, "baseline", "base-r.tsv"))
    ubuntu <- c("Package\tArchitecture\tVersion\tStatus", "bash\tarm64\t5.2\tinstalled")
    writeLines(c("# fixture", ubuntu), file.path(scratch, "baseline", "base-ubuntu.tsv"))
    installed <- rbind(
        c("basepkg", "1.0", NA, NA, NA, "base", "/fixture/lib"),
        c("alphapkg", "1.2", NA, dependencies, NA, NA, "/fixture/lib"),
        c("betapkg", "1.0", NA, NA, NA, NA, "/fixture/lib")
    )
    colnames(installed) <- c("Package", "Version", "Depends", "Imports", "LinkingTo",
                             "Priority", "LibPath")
    inspector$installed.packages <- function(...) installed
    inspector$ubuntu_package_inventory <- function() ubuntu
    inspector$dir.exists <- function(paths) base::dir.exists(redirect(paths))
    inspector$file.exists <- function(path) base::file.exists(redirect(path))
    inspector$read.dcf <- function(file, ...) base::read.dcf(redirect(file), ...)
    inspector$read.delim <- function(file, ...) {
        if (missing(file)) utils::read.delim(...) else utils::read.delim(redirect(file), ...)
    }
    inspector$write.table <- function(x, file, ...) utils::write.table(x, redirect(file), ...)
    inspector$writeLines <- function(text, con = stdout(), ...) {
        base::writeLines(text, redirect(con), ...)
    }
    inspector$system2 <- function(command, args = character(), ...) {
        if (basename(command) == "Rscript" && utils::tail(args, 1L) %in% broken) {
            structure("Error: fixture load failure", status = 1L)
        } else {
            character()
        }
    }
    inspector$Sys.setenv <- function(...) invisible(TRUE)
    inspector$inspect_shared_libraries <- function(libraries, ...) {
        list(status = "PASS", review = character(), missing = character(),
             foreign = character(), scanned = 0L,
             libraries = data.frame(language_package = character(), file = character(),
                                    soname = character(), path = character(),
                                    owners = character()))
    }
    inspector$audit_quarto <- function(executable) {
        list(status = "PASS", detail = "quarto 1.9.36 (/usr/local/bin/quarto)", found = "1.9.36")
    }
    if (!is.null(configure)) configure(inspector)
    output <- character()
    error <- tryCatch({
        baseline <- file.path(scratch, "baseline")
        output <- utils::capture.output(inspector$inspect_audit_main(baseline))
        NULL
    }, error = identity)
    result <- function(name) file.path(scratch, "result", name)
    list(error = error, output = output,
         checks = utils::read.delim(result("audit-checks.tsv"), colClasses = "character"),
         items = utils::read.delim(result("audit-items.tsv"), colClasses = "character",
                                   na.strings = character()),
         installed = utils::read.delim(result("installed-packages.tsv"),
                                       colClasses = "character", na.strings = character()),
         native = utils::read.delim(result("native-libraries.tsv"), colClasses = "character"),
         report = jsonlite::fromJSON(result("r-audit.json"), simplifyVector = FALSE))
}

# --- 要件を満たさない・名前空間の読み込みの失敗・依存の関係の食い違いは FAIL にする（README.md「R」）---
testthat::test_that("要件・読み込み・依存の失敗を記録する", {
    audit <- run_fake_audit("alphapkg (>= 9.0)", "betapkg (>= 2.0), gonepkg", "alphapkg")
    stopifnot(inherits(audit$error, "error"),
              startsWith(conditionMessage(audit$error), "Dependency audit failed: "),
              identical(audit$report$status, "FAIL"))
    status <- setNames(audit$checks$status, audit$checks$check)
    failed <- c("DESCRIPTION requirements", "Missing R dependencies",
                "R dependency version constraints", "R namespace loading")
    stopifnot(all(status[failed] == "FAIL"),
              all(vapply(failed, grepl, logical(1), conditionMessage(audit$error),
                         fixed = TRUE)))
    item_rows <- function(check) {
        found <- audit$items[audit$items$check == check, ]
        paste(found$item, found$expected, found$found, found$status, sep = "|")
    }
    testthat::expect_no_error(stopifnot(
        identical(item_rows("DESCRIPTION requirements"), "alphapkg|>= 9.0|1.2|FAIL"),
        identical(item_rows("Missing R dependencies"),
                  c("alphapkg||1.2|PASS", "betapkg||1.0|PASS", "gonepkg|||FAIL")),
        identical(item_rows("R dependency version constraints"),
                  c("alphapkg Imports betapkg|>= 2.0|1.0|FAIL",
                    "alphapkg Imports gonepkg|||FAIL")),
        identical(item_rows("R namespace loading"),
                  c("alphapkg||Error: fixture load failure|FAIL", "betapkg||loaded|PASS"))
    ))
})

# --- 監査の照合の中に「Template R version」は無く、導入済みの一覧・共有ライブラリの表・Quarto の行を書く（README.md「R」）---
testthat::test_that("合格時に導入一覧と照合の明細を書く", {
    audit <- run_fake_audit("alphapkg (>= 1.0)", "betapkg (>= 1.0)")
    status <- setNames(audit$checks$status, audit$checks$check)
    stopifnot(is.null(audit$error), identical(audit$report$status, "PASS"),
              all(status == "PASS"), !"Template R version" %in% audit$checks$check,
              "Confirmed R runtime version" %in% audit$checks$check,
              all(status[c("DESCRIPTION requirements", "Missing R dependencies",
                           "R dependency version constraints", "R namespace loading")] == "PASS"))
    quarto <- audit$items[audit$items$check == "Quarto CLI", ]
    testthat::expect_no_error(stopifnot(
        identical(audit$installed$ecosystem, c("r", "r", "r", "ubuntu")),
        identical(audit$installed$package[[4L]], "bash"),
        identical(names(audit$native),
                  c("language_package", "file", "soname", "path", "owners")),
        identical(paste(quarto$item, quarto$expected, quarto$found, quarto$status,
                        sep = "|"), "quarto||1.9.36|PASS")
    ))
})

# --- 導入記録だけが無ければ、監査を終えて全体とハッシュ照合を要確認にする（README.md「第 3 段階の中身」）---
testthat::test_that("導入記録が無ければ要確認にする", {
    audit <- run_fake_audit("alphapkg (>= 1.0)", "betapkg (>= 1.0)", install_report = FALSE)
    testthat::expect_no_error(stopifnot(
        is.null(audit$error), identical(audit$report$status, "REVIEW"),
        identical(audit$checks$check[audit$checks$status == "REVIEW"],
                  "Confirmed R package hashes")
    ))
})

# --- 未登録のパッケージと版を、画面と表に同じ並びで残す（README.md「R」）---
testthat::test_that("未登録パッケージの画面と表の並びが一致する", {
    audit <- run_fake_audit("alphapkg", "betapkg", configure = function(inspector) {
        installed <- inspector$installed.packages()
        extra <- installed[c(1L, 1L), , drop = FALSE]
        extra[, "Package"] <- c("extratwo", "extraone")
        extra[, "Version"] <- c("5.0", "4.0")
        inspector$installed.packages <- function(...) rbind(installed, extra)
    })
    detail <- "extraone 4.0; extratwo 5.0"
    row <- audit$checks[audit$checks$check == "Unlisted R additions", ]
    testthat::expect_no_error(stopifnot(
        identical(row$status, "REVIEW"), identical(row$detail, detail),
        paste0("   ", detail) %in% audit$output
    ))
})

# --- 第1段階で対象 CPU だけを調べ、ldd の失敗理由と未解決の名前を区別する（README.md「R」）---
testthat::test_that("第1段階で ldd の失敗と未解決名を分ける", {
    inspector <- new.env()
    sys.source("R/dependencies-inspect.R", envir = inspector)
    inspector$Sys.info <- function() c(machine = "aarch64")
    inspector$list.files <- function(...) c("foreign.so", "failure.so", "missing.so")
    inspector$elf_machine <- function(file) if (file == "foreign.so") 62L else 183L
    inspector$system2 <- function(command, args, ...) {
        stopifnot(identical(command, "ldd"), !grepl("foreign", args))
        text <- if (grepl("missing", args)) "libabsent.so => not found" else "read error"
        structure(text, status = 1L)
    }
    plan <- data.frame(package = "fixture", base_version = "1")
    result <- inspector$scan_r_libraries(plan, NULL, "")
    testthat::expect_no_error(stopifnot(
        identical(result$libraries$soname, "libabsent.so"),
        identical(result$review$item, "failure.so"),
        grepl("read error", result$review$detail, fixed = TRUE)
    ))
})

# --- 同じファイルの所有元が複数あるときは全パッケージに利用元を付ける（README.md「R」）---
testthat::test_that("第1段階で複数の所有元に利用元を付ける", {
    inspector <- new.env()
    sys.source("R/dependencies-inspect.R", envir = inspector)
    inventory <- data.frame(Package = c("libone", "libtwo"), Architecture = "arm64",
                            Version = "1", Status = "installed")
    inspector$read.delim <- function(...) inventory
    inspector$ubuntu_package_inventory <- function() character()
    inspector$writeLines <- function(...) NULL
    inspector$system2 <- function(command, ...) {
        stopifnot(identical(command, "dpkg-query"))
        "libone:arm64, libtwo:arm64: /usr/lib/x.so"
    }
    libraries <- data.frame(r_package = "fixture", soname = "x.so", path = "/lib/x.so")
    result <- inspector$get_ubuntu_plan(libraries, character())
    testthat::expect_no_error(stopifnot(
        identical(result$plan$package, c("libone", "libtwo")),
        identical(unname(result$plan$language_packages), rep("fixture", 2L))
    ))
})

# --- タブ区切りの Contents から提供元を読み、列挙後に索引が失われた場合は途中結果を採用しない（README.md「検査対象のフォルダ」）---
testthat::test_that("展開できなかった Contents の途中結果を使わない", {
    inspector <- new.env()
    sys.source("R/dependencies-inspect.R", envir = inspector)
    scratch <- tempfile("contents-", tmpdir = file.path(getwd(), "_tmp"))
    dir.create(scratch)
    withr::defer(unlink(scratch, recursive = TRUE))
    inspector$tempdir <- function() scratch
    index <- file.path(scratch, "Contents")
    next_index <- file.path(scratch, "Contents-next")
    writeLines("  usr/lib/libtest.so\tlibs/pkg", index)
    file.create(next_index)
    indices <- index
    inspector$system2 <- function(command, args, ...) {
        if (command == "apt-get") {
            if (shQuote("indextargets") %in% args) return(indices)
            stopifnot(shQuote("Acquire::https::Timeout=20") %in% args)
            return(0L)
        }
        if (length(indices) == 2L) unlink(next_index)
        base::system2(command, args, ...)
    }
    stopifnot(identical(inspector$contents_providers("libtest.so"), list(libtest.so = "pkg")))
    indices <- c(index, next_index)
    error <- tryCatch(inspector$contents_providers("libtest.so"), error = identity)
    testthat::expect_no_error(stopifnot(
        inherits(error, "error"),
        startsWith(conditionMessage(error), "Ubuntu の Contents 索引を展開できません: "),
        grepl("bash の実行に失敗", conditionMessage(error), fixed = TRUE)
    ))
})

# --- Ubuntu の一覧を取得できなくても、言語の確定版は照合する（README.md「R」）---
testthat::test_that("Ubuntu 一覧が無くても確定版を照合する", {
    unavailable <- run_fake_audit("alphapkg", "betapkg", configure = function(inspector) {
        inspector$ubuntu_package_inventory <- function() stop("一覧を取得できません")
    })
    status <- setNames(unavailable$checks$status, unavailable$checks$check)
    testthat::expect_no_error(stopifnot(
        identical(unavailable$report$status, "FAIL"),
        identical(unname(status["Ubuntu package inventory"]), "FAIL"),
        identical(unname(status["Confirmed R versions"]), "PASS"),
        !"Base OS packages removed or changed" %in% names(status),
        !"Confirmed Ubuntu versions" %in% names(status)
    ))
})

# --- 監査の途中の例外でも、それまでの照合・一覧を保存して元の例外を返す（README.md「R」）---
testthat::test_that("監査中の例外で証跡と元の例外を残す", {
    error <- simpleError("監査が中断されました")
    partial <- run_fake_audit("alphapkg (>= 1.0)", "betapkg", configure = function(inspector) {
        inspector$audit_quarto <- function(...) stop(error)
    })
    summary <- partial$checks[partial$checks$check == "DESCRIPTION requirements", ]
    item <- partial$items[partial$items$check == "DESCRIPTION requirements", ]
    installed <- partial$installed[partial$installed$package == "alphapkg", ]
    testthat::expect_no_error(stopifnot(
        identical(partial$report$status, "FAIL"), identical(partial$error, error),
        identical(summary$status, "PASS"), identical(item$status, "PASS"),
        identical(item$item, "alphapkg"), identical(item$expected, ">= 1.0"),
        identical(item$found, "1.2"), identical(installed$version, "1.2")
    ))
})

# --- 同名の R パッケージの各版を TSV と JSON の両方に残す（README.md「R」）---
testthat::test_that("同名のパッケージの各版を一覧に残す", {
    audit <- run_fake_audit("alphapkg", "betapkg", configure = function(inspector) {
        installed <- inspector$installed.packages()
        extra <- installed[installed[, "Package"] == "alphapkg", , drop = FALSE]
        extra[, "Version"] <- "0.5"
        inspector$installed.packages <- function(...) rbind(installed, extra)
    })
    rows <- audit$installed[audit$installed$package == "alphapkg", ]
    versions <- vapply(Filter(function(row) row$name == "alphapkg", audit$report$installed),
                       function(row) row$version, character(1))
    testthat::expect_no_error(stopifnot(
        identical(rows$version, c("1.2", "0.5")), identical(versions, rows$version)
    ))
})

# --- 調査の環境の APT スナップショットへの書き換えは、取得元（stanza）の区切りの空行を残す（Python と同じ）（README.md「第 1 段階の中身」）---
testthat::test_that("APT の取得元を書き換えても区切りの空行を残す", {
    recipe <- readLines("Dockerfile.inspect", warn = FALSE)
    line <- grep('sed "s|^URIs:', recipe, fixed = TRUE, value = TRUE)
    stopifnot(length(line) == 1L)
    script <- sub('^[^"]*"([^"]*)".*$', "\\1", line)
    script <- gsub("$APT_SNAPSHOT", "20261101T000000Z", script, fixed = TRUE)
    scratch <- tempfile("apt-sources-", tmpdir = file.path(getwd(), "_tmp"))
    dir.create(scratch, recursive = TRUE)
    withr::defer(unlink(scratch, recursive = TRUE))
    sources <- file.path(scratch, "ubuntu.sources")
    ports <- "URIs: http://ports.ubuntu.com/ubuntu-ports/"
    writeLines(c("Types: deb", ports, "Suites: noble noble-updates",
                 "", "Types: deb", ports, "Suites: noble-security"), sources)
    uri <- "URIs: https://snapshot.ubuntu.com/ubuntu/20261101T000000Z/"
    testthat::expect_no_error(stopifnot(
        identical(system2("sed", c(shQuote(script), shQuote(sources)), stdout = TRUE), c(
        "Types: deb", uri, "Check-Valid-Until: no", "Suites: noble noble-updates", "",
        "Types: deb", uri, "Check-Valid-Until: no", "Suites: noble-security"))
    ))
})
