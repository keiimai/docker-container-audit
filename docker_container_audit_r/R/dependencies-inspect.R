# --- 調査の環境で依存を調べ、イメージの中で照合する（base R だけで動く）（Python は src/dependencies_inspect.py）（README.md「構成」）---

# --- コマンドを走らせて出力を返す（失敗なら止める）（README.md「構成」）---
command_output <- function(command, args) {
    output <- suppressWarnings(system2(command, shQuote(args), stdout = TRUE, stderr = TRUE))
    if (!is.null(attr(output, "status"))) {
        stop(command, " の実行に失敗しました。\n", paste(args, collapse = " "),
             "\n", paste(output, collapse = "\n"), call. = FALSE)
    }
    output
}

# --- 機械の名前（Sys.info() の machine）と、ELF の見出しの機械の種類（e_machine）の対応（README.md「R」）---
ELF_MACHINES <- c(x86_64 = 62L, amd64 = 62L, aarch64 = 183L, arm64 = 183L)

# --- ELF の見出しから機械の種類（e_machine）を返す（ELF でなければ NA）（README.md「R」）---
elf_machine <- function(path) {
    header <- readBin(path, "raw", n = 20L)
    if (length(header) < 20L || !identical(header[1:4], as.raw(c(0x7f, 0x45, 0x4c, 0x46)))) {
        return(NA_integer_)
    }
    bytes <- as.integer(header[19:20])
    if (identical(header[[6L]], as.raw(2L))) bytes[[1L]] * 256L + bytes[[2L]] else
        bytes[[2L]] * 256L + bytes[[1L]]
}

# --- 確定記録の版と違うものを返す（見つからない版は明細と同じ空欄）（README.md「R」）---
audit_pin_errors <- function(pins, actual) {
    found <- unname(actual[pins$package])
    different <- is.na(found) | found != pins$version
    found[is.na(found)] <- ""
    sprintf("%s expected %s, found %s", pins$package[different], pins$version[different],
            found[different])
}

# --- Quarto の CLI を確かめ、判定・説明・版を返す（無ければ REVIEW、動かなければ FAIL）（README.md「R」）---
audit_quarto <- function(executable) {
    if (!nzchar(executable)) {
        return(list(status = "REVIEW", detail = "quarto is unavailable", found = ""))
    }
    output <- suppressWarnings(system2(executable, "--version", stdout = TRUE, stderr = TRUE))
    if (!is.null(attr(output, "status"))) {
        detail <- command_failure(output)
        return(list(status = "FAIL", detail = detail, found = detail))
    }
    versions <- trimws(output[nzchar(trimws(output))])
    version <- if (length(versions)) versions[[1L]] else ""
    list(status = "PASS", detail = paste0("quarto ", version, " (", executable, ")"),
         found = version)
}

# --- 照合した項目を audit-items.tsv の行にする（status を省けば expected と found で決める）（何行もまとめて）（README.md「R」）---
audit_items <- function(check, item, expected = "", found = "", status = NULL) {
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

# --- 固定ベースの Ubuntu パッケージを 1 つずつ照合した行にする（確定した更新は除く）（README.md「R」）---
audit_base_items <- function(baseline, current, approved = character()) {
    keys <- function(x) paste(x$Package, x$Architecture, sep = ":")
    versions <- setNames(current$Version, keys(current))
    found <- unname(versions[keys(baseline)])
    same <- !is.na(found) & found == baseline$Version
    kept <- keys(baseline) %in% approved & !is.na(found)
    audit_items("Base OS packages removed or changed", keys(baseline), baseline$Version, found,
                ifelse(same | kept, "PASS", "FAIL"))
}

# --- 固定ベースの Ubuntu パッケージのうち、消えたか版が変わったものを返す（確定した更新は除く）（README.md「R」）---
audit_base_changes <- function(baseline, current, approved = character()) {
    keys <- function(x) paste(x$Package, x$Architecture, sep = ":")
    versions <- setNames(current$Version, keys(current))
    changed <- keys(baseline)[is.na(versions[keys(baseline)]) |
                                  versions[keys(baseline)] != baseline$Version]
    changed[!(changed %in% approved & !is.na(versions[changed]))]
}

# --- os-release の項目を読み、対になった引用符を外す（README.md「R」）---
.inspect_os_fields <- function(lines) {
    entries <- grep("^[A-Za-z_][A-Za-z_0-9]*=", lines, value = TRUE)
    keys <- sub("=.*$", "", entries)
    values <- sub("^[^=]*=", "", entries)
    values <- sub("^(['\"])(.*)\\1$", "\\2", values)
    values <- gsub("\\\\([\\\\$\"'\x60])", "\\1", values, perl = TRUE)
    setNames(values, keys)[!duplicated(keys, fromLast = TRUE)]
}

# --- OS が Debian・Ubuntu かを確かめ、名前と不合格の説明を返す（README.md「R」）---
audit_os_release <- function(os_release) {
    fields <- .inspect_os_fields(os_release)
    name <- unname(fields["PRETTY_NAME"])
    name <- if (is.na(name)) "" else name
    list(name = name, failure = if (!fields["ID"] %in% c("ubuntu", "debian")) {
        paste("Not Debian/Ubuntu:", name)
    } else {
        character()
    })
}

# --- コマンドが失敗したときだけ、出力を 1 行の説明にして返す（README.md「R」）---
command_failure <- function(output, empty = character()) {
    if (is.null(attr(output, "status"))) return(empty)
    lines <- trimws(output[nzchar(trimws(output))])
    if (length(lines)) paste(lines, collapse = "; ") else "command failed"
}

# --- 確定記録の R パッケージの版と、入っている R パッケージを照合する（同じ名前が 2 つあれば合格にしない）（README.md「第 3 段階の中身」）---
audit_confirmed_r <- function(actual, base_packages, pins, copies = list()) {
    found <- actual
    multiple <- names(copies)[lengths(copies) > 1L]
    found[multiple] <- vapply(copies[multiple], paste, character(1), collapse = ", ")
    unlisted <- sort(setdiff(names(actual), c(base_packages, pins$package)), method = "radix")
    list(versions = audit_pin_errors(pins, found), unlisted = unlisted,
         items = rbind(
             audit_items("Confirmed R versions", pins$package, pins$version,
                         found[pins$package]),
             audit_items("Unlisted R additions", unlisted, "", actual[unlisted], "REVIEW")))
}

# --- ビルドの導入記録（r-install-report.tsv）が無いときの説明（README.md「第 3 段階の中身」）---
INSTALL_REPORT_MISSING <- "ビルドの導入記録が無いので、SHA-256 は照合していない"

# --- ビルドの導入記録を、確定記録の版と SHA-256 と照合する（無ければ REVIEW）（README.md「第 3 段階の中身」）---
audit_r_install_report <- function(pins, report) {
    pairs <- function(x) setNames(paste(x$version, x$sha256), x$package)
    if (is.null(report)) {
        return(list(status = "REVIEW", errors = INSTALL_REPORT_MISSING,
                    items = audit_items("Confirmed R package hashes", pins$package, pairs(pins),
                                        "", "REVIEW")))
    }
    key <- function(x) paste(x$package, x$version, x$sha256)
    errors <- sort(c(setdiff(key(pins), key(report)), setdiff(key(report), key(pins))))
    built <- pairs(report)
    hashed <- union(pins$package, names(built))
    list(status = if (length(errors)) "FAIL" else "PASS", errors = errors,
         items = audit_items("Confirmed R package hashes", hashed, pairs(pins)[hashed],
                             built[hashed]))
}

# --- 確定記録の Ubuntu の計画と、入っているパッケージを照合する（README.md「R」）---
audit_confirmed_ubuntu <- function(current, plan) {
    keys <- function(x) paste(x$Package, x$Architecture, sep = ":")
    sorted <- order(current$Package, current$Architecture, method = "radix")
    current <- current[sorted, , drop = FALSE]
    versions <- setNames(current$Version, keys(current))
    plan <- plan[order(plan$package, plan$architecture, method = "radix"), , drop = FALSE]
    plan_keys <- paste(plan$package, plan$architecture, sep = ":")
    installs <- plan$action %in% c("install", "update")
    found <- unname(versions[plan_keys[installs]])
    different <- is.na(found) | found != plan$version[installs]
    based <- plan_keys[!is.na(plan$base_version) & nzchar(plan$base_version)]
    additions <- current$Package[!keys(current) %in% based]
    unlisted <- setdiff(keys(current), plan_keys)
    absent <- setdiff(plan_keys[plan$action == "install"], keys(current))
    ambiguous <- sort(unique(additions[duplicated(additions)]), method = "radix")
    added <- keys(current)[current$Package %in% ambiguous & !keys(current) %in% based]
    items <- rbind(
        audit_items("Confirmed Ubuntu versions", plan_keys[installs], plan$version[installs],
                    found),
        audit_items("Unlisted Ubuntu additions", unlisted, "", versions[unlisted], "REVIEW"),
        audit_items("Confirmed Ubuntu additions missing or ambiguous", absent,
                    plan$version[match(absent, plan_keys)], "", "FAIL"),
        audit_items("Confirmed Ubuntu additions missing or ambiguous", ambiguous, "",
                    vapply(ambiguous, function(name) {
                        paste(sort(added[sub(":.*$", "", added) == name], method = "radix"),
                              collapse = ", ")
                    }, character(1)), "FAIL"))
    list(
        versions = sprintf("%s expected %s, found %s", plan_keys[installs][different],
                           plan$version[installs][different],
                           ifelse(is.na(found[different]), "", found[different])),
        unlisted = unlisted,
        missing = c(absent, ambiguous),
        items = items
    )
}

# --- 共有ライブラリの持ち主の Ubuntu パッケージ（同じ場所は 1 度だけ聞く）（README.md「R」）---
library_owners <- local({
    known <- list()
    function(path) {
        for (candidate in unique(c(path, normalizePath(path, mustWork = FALSE)))) {
            if (!is.null(known[[candidate]])) return(known[[candidate]])
            output <- suppressWarnings(system2("dpkg-query", c("-S", shQuote(candidate)),
                                               stdout = TRUE, stderr = FALSE))
            if (is.null(attr(output, "status")) && length(output)) {
                known[[candidate]] <<- sub(": /.*$", "", output)
                return(known[[candidate]])
            }
        }
        character()
    }
})

# --- R パッケージの共有ライブラリを ldd で調べ、解決したパスを dpkg の持ち主に対応させる（README.md「R」）---
inspect_shared_libraries <- function(libraries, machine = Sys.info()[["machine"]]) {
    result <- list(status = "PASS", scanned = 0L, review = character(), missing = character(),
                   foreign = character(),
                   libraries = data.frame(language_package = character(), file = character(),
                                          soname = character(), path = character(),
                                          owners = character()))
    if (!nzchar(Sys.which("ldd"))) {
        result$status <- "REVIEW"
        result$review <- "ldd is unavailable; shared libraries were not inspected"
        return(result)
    }
    dpkg <- nzchar(Sys.which("dpkg-query"))
    own <- unname(ELF_MACHINES[machine])
    for (name in names(libraries)) {
        for (file in list.files(file.path(libraries[[name]], "libs"),
                                pattern = "[.]so([.][0-9]+)*$", full.names = TRUE,
                                recursive = TRUE)) {
            found <- tryCatch(elf_machine(file), error = identity)
            if (inherits(found, "error")) {
                result$review <- c(result$review, paste0(file, ": ", conditionMessage(found)))
                next
            }
            if (is.na(found)) next
            if (!is.na(own) && found != own) {
                result$foreign <- c(result$foreign, file)
                next
            }
            result$scanned <- result$scanned + 1L
            linked <- suppressWarnings(system2("ldd", shQuote(file), stdout = TRUE, stderr = TRUE))
            matches <- regmatches(linked, regexec(
                "^[[:space:]]*([^[:space:]]+)[[:space:]]+=>[[:space:]]+(not found|/[^[:space:]]+)",
                linked))
            matches <- matches[lengths(matches) == 3L]
            for (match in matches) {
                owners <- if (match[[3L]] != "not found" && dpkg) library_owners(match[[3L]])
                result$libraries[nrow(result$libraries) + 1L, ] <- list(
                    name, file, match[[2L]], match[[3L]], paste(owners, collapse = "; "))
                if (match[[3L]] == "not found") {
                    result$missing <- c(result$missing, paste0(match[[2L]], ": ", file))
                }
            }
            not_found <- any(vapply(matches, function(x) x[[3L]] == "not found", logical(1)))
            if (!is.null(attr(linked, "status")) && !not_found) {
                result$review <- c(result$review, paste0("ldd failed for ", file, ": ",
                                                         paste(trimws(linked), collapse = " ")))
            }
        }
    }
    if (!dpkg) {
        result$review <- c(result$review,
                           "dpkg-query is unavailable; OS package ownership was not mapped")
    }
    result$status <- if (length(result$missing)) "FAIL" else if (length(result$review)) "REVIEW"
        else "PASS"
    result
}

# --- 文字を JSON の文字列にする（README.md「R」）---
json_string <- function(x) {
    x <- gsub("\\", "\\\\", x, fixed = TRUE)
    x <- gsub('"', '\\"', x, fixed = TRUE)
    x <- gsub("\n", "\\n", x, fixed = TRUE)
    x <- gsub("\t", "\\t", x, fixed = TRUE)
    if (any(grepl("[\001-\037]", x))) {
        for (code in 1:31) {
            x <- gsub(intToUtf8(code), sprintf("\\u%04x", code), x, fixed = TRUE)
        }
    }
    paste0('"', x, '"')
}

# --- R の値を JSON の文にする（コンテナの R に jsonlite があるとは限らない）（README.md「R」）---
json_text <- function(x) {
    if (is.null(x)) return("null")
    if (is.data.frame(x)) {
        return(json_text(lapply(seq_len(nrow(x)), function(i) as.list(x[i, , drop = FALSE]))))
    }
    if (is.list(x) || length(x) != 1L) {
        x <- as.list(x)
        items <- vapply(x, json_text, character(1))
        if (length(x) && !is.null(names(x))) {
            return(paste0("{", paste0(json_string(names(x)), ":", items, collapse = ","), "}"))
        }
        return(paste0("[", paste(items, collapse = ","), "]"))
    }
    if (is.na(x)) return("null")
    if (is.logical(x)) return(if (x) "true" else "false")
    if (is.numeric(x)) return(format(x, scientific = FALSE))
    json_string(as.character(x))
}

# --- 調査の表を /report に TSV で書く（README.md「結果の共通形式」）---
write_report <- function(name, data) {
    write.table(data, file.path("/report", name), sep = "\t", row.names = FALSE, na = "")
}

# --- R パッケージの取得元（request/base.txt の R_REPOSITORY）（README.md「検査対象のフォルダ」）---
analysis_repository <- function() {
    repo <- Sys.getenv("R_REPOSITORY")
    if (!nzchar(repo)) stop("R パッケージの取得元（R_REPOSITORY）がありません。", call. = FALSE)
    repo
}

# --- dpkg のパッケージ一覧（名前・アーキテクチャ・版・状態）（README.md「R」）---
ubuntu_package_inventory <- function() {
    c("Package\tArchitecture\tVersion\tStatus", command_output("dpkg-query", c(
        "-W", "-f", "${Package}\\t${Architecture}\\t${Version}\\t${db:Status-Status}\\n"
    )))
}

# --- 固定ベースの R と Ubuntu のパッケージ一覧を書く（1 行目は固定ベースとプラットフォーム）（README.md「R」）---
write_base_inventories <- function() {
    image <- sprintf("rocker/rstudio:%s@sha256:%s",
                     Sys.getenv("RSTUDIO_TAG"), Sys.getenv("RSTUDIO_SHA256"))
    header <- paste("#", image, paste0("linux/", command_output("dpkg", "--print-architecture")))
    installed <- installed.packages(noCache = TRUE)
    installed <- installed[!duplicated(installed[, "Package"]), , drop = FALSE]
    writeLines(c(header, "Package\tVersion",
                 paste(installed[, "Package"], installed[, "Version"], sep = "\t")),
               "/report/base-r.tsv")
    writeLines(c(header, ubuntu_package_inventory()), "/report/base-ubuntu.tsv")
}

# --- 固定ベースと P3M の索引から R の依存を割り出し、版条件を照合する（README.md「R」）---
get_r_plan <- function(packages, repo) {
    fields <- c("Depends", "Imports", "LinkingTo", "SystemRequirements")
    columns <- c("Package", "Version", fields)
    base <- installed.packages(fields = fields, noCache = TRUE)
    base <- base[!duplicated(base[, "Package"]), columns, drop = FALSE]
    rownames(base) <- base[, "Package"]
    snapshot <- available.packages(
        repos = repo, type = "source", fields = c("Path", "SystemRequirements"),
        filters = c("R_version", "OS_type", "subarch")
    )
    if (!nrow(snapshot)) stop("Posit スナップショットの索引が空です。", call. = FALSE)
    catalog <- rbind(base, snapshot[setdiff(rownames(snapshot), rownames(base)), columns,
                                    drop = FALSE])
    absent <- setdiff(packages, rownames(catalog))
    if (length(absent)) {
        stop("ベースまたはスナップショットにない R パッケージ ", paste(absent, collapse = ", "), call. = FALSE)
    }
    hard <- fields[1:3]
    deps <- function(recursive) {
        unlist(tools::package_dependencies(packages, db = catalog, which = hard,
                                           recursive = recursive), use.names = FALSE)
    }
    direct <- deps(FALSE)
    required <- sort(union(packages, deps(TRUE)))
    absent <- setdiff(required, rownames(catalog))
    if (length(absent)) {
        stop("取得できない R 依存パッケージ ", paste(absent, collapse = ", "), call. = FALSE)
    }

    versions <- c(R = as.character(getRversion()),
                  setNames(catalog[, "Version"], rownames(catalog)))
    for (package in required) {
        specs <- trimws(unlist(strsplit(na.omit(catalog[package, hard]), ",", fixed = TRUE)))
        for (spec in specs) {
            match <- regmatches(spec, regexec(
                "^([A-Za-z0-9.]+)[[:space:]]*\\((>=|<=|==|>|<)[[:space:]]*([^)]*)\\)$", spec
            ))[[1L]]
            dependency <- if (length(match)) match[[2L]] else spec
            actual <- versions[dependency]
            if (is.na(actual) || (length(match) && !do.call(match[[3L]], list(
                package_version(actual), package_version(trimws(match[[4L]]))
            )))) {
                stop(package, " の版条件を満たせません。 ", spec,
                     "（候補 ", actual, "）", call. = FALSE)
            }
        }
    }
    plan <- data.frame(
        package = required, version = catalog[required, "Version"],
        relation = ifelse(required %in% packages, "selected",
                          ifelse(required %in% direct, "direct", "indirect")),
        base_version = base[match(required, rownames(base)), "Version"],
        package_format = ifelse(required %in% rownames(base), "base", NA_character_),
        system_requirements = catalog[required, "SystemRequirements"],
        needs_compilation = NA_character_, sha256 = NA_character_
    )
    list(plan = plan, snapshot = snapshot)
}

# --- R パッケージの共有ライブラリを ldd で調べる（ベースに無いものは取得して展開する）（README.md「検査対象のフォルダ」）---
scan_r_libraries <- function(plan, snapshot, repo) {
    from_repo <- is.na(plan$base_version)
    packages <- plan$package[from_repo]
    archives <- matrix(character(), ncol = 2L)
    for (attempt in 1:3) {
        remaining <- setdiff(packages, archives[, 1L])
        if (!length(remaining)) break
        if (attempt > 1L) message("取り直す R パッケージ（", attempt, " 回目）: ", length(remaining), " 件")
        archives <- rbind(archives, download.packages(
            remaining, destdir = tempdir(), available = snapshot, repos = repo, type = "source"
        ))
    }
    if (!setequal(packages, archives[, 1L])) {
        stop("取得できない R パッケージ ", paste(setdiff(packages, archives[, 1L]), collapse = ", "),
             call. = FALSE)
    }
    if (length(packages)) {
        digests <- sub("[[:space:]].*$", "", command_output("sha256sum", archives[, 2L]))
        plan$sha256[from_repo] <- digests[match(packages, archives[, 1L])]
    }
    review <- data.frame(item = character(), detail = character())
    own <- unname(ELF_MACHINES[Sys.info()[["machine"]]])
    libraries <- data.frame(r_package = character(), file = character(), soname = character(),
                            path = character())

    for (i in seq_len(nrow(plan))) {
        package <- plan$package[[i]]
        if (from_repo[[i]]) {
            archive <- archives[match(package, archives[, 1L]), 2L]
            members <- untar(archive, list = TRUE)
            description <- paste0(package, "/DESCRIPTION")
            shared <- grep("[.]so([.][0-9]+)*$", members, value = TRUE)
            unpack <- tempfile("package-", tmpdir = tempdir())
            if (!dir.create(unpack) ||
                untar(archive, files = c(description, shared), exdir = unpack) != 0L) {
                stop("R パッケージを調べられません。 ", package, call. = FALSE)
            }
            metadata <- read.dcf(file.path(unpack, description), fields = c(
                "Package", "Version", "Built", "SystemRequirements", "NeedsCompilation"
            ))
            if (!identical(unname(metadata[1L, c("Package", "Version")]),
                           c(package, plan$version[[i]]))) {
                stop("取得した R パッケージの版が索引と一致しません。 ", package, call. = FALSE)
            }
            plan$package_format[[i]] <- if (is.na(metadata[1L, "Built"])) "source" else "binary"
            plan$system_requirements[[i]] <- metadata[1L, "SystemRequirements"]
            plan$needs_compilation[[i]] <- metadata[1L, "NeedsCompilation"]
            files <- file.path(unpack, shared)
        } else {
            files <- list.files(system.file("libs", package = package),
                                pattern = "[.]so([.][0-9]+)*$", full.names = TRUE, recursive = TRUE)
        }
        for (file in files) {
            shown <- sub(paste0("^.*/(", package, "/libs/)"), "\\1", file)
            found <- tryCatch(elf_machine(file), error = identity)
            if (inherits(found, "error")) {
                review[nrow(review) + 1L, ] <- list(shown, conditionMessage(found))
                next
            }
            if (is.na(found) || (!is.na(own) && found != own)) next
            output <- tryCatch(
                suppressWarnings(system2("ldd", shQuote(file), stdout = TRUE, stderr = TRUE)),
                error = identity
            )
            if (inherits(output, "error")) {
                review[nrow(review) + 1L, ] <- list(shown, conditionMessage(output))
                next
            }
            lines <- trimws(output)
            matches <- regmatches(lines, regexec(
                "^([^[:space:]]+) => (not found|/[^[:space:]]+)([[:space:]]|$)", lines
            ))
            matches <- matches[lengths(matches) > 0L]
            for (match in matches) {
                libraries[nrow(libraries) + 1L, ] <- c(package, shown, match[[2L]], match[[3L]])
            }
            if (!is.null(attr(output, "status")) &&
                !any(vapply(matches, function(m) m[[3L]] == "not found", logical(1)))) {
                review[nrow(review) + 1L, ] <- list(shown, paste("ldd で調べられません。 ",
                                                                paste(lines, collapse = " ")))
            }
        }
        if (from_repo[[i]]) unlink(unpack, recursive = TRUE)
    }
    list(plan = plan, libraries = unique(libraries), review = unique(review))
}

# --- R の調査の入口（依存と共有ライブラリを調べ、/report に記録する）（README.md「R」）---
inspect_r_main <- function(packages) {
    repo <- analysis_repository()
    platform <- paste(R.version$platform, R.version$arch, R.version$os)
    options(
        HTTPUserAgent = sprintf("R/%s R (%s %s)", getRversion(), getRversion(), platform),
        download.file.method = "libcurl", timeout = 120
    )
    Sys.setenv(LD_LIBRARY_PATH = paste(c(R.home("lib"), Sys.getenv("LD_LIBRARY_PATH")),
                                       collapse = ":"))
    message("R ", getRversion(), " / ", platform, "\nPosit ", repo)
    local({
        write_base_inventories()
        r <- get_r_plan(packages, repo)
        write_report("r-packages.tsv", r$plan)
        inspected <- scan_r_libraries(r$plan, r$snapshot, repo)
        write_report("r-packages.tsv", inspected$plan)
        libraries <- inspected$libraries
        write_report("native-libraries.tsv", data.frame(
            language_package = libraries$r_package, file = libraries$file,
            soname = libraries$soname, path = libraries$path))
        inspected$base <- readLines("/report/base-r.tsv", n = 1L)
        inspected$repo <- repo
        saveRDS(inspected, "/report/r-discovery.rds")
    })
}

# --- 署名付きの APT 索引（Contents）から、共有ライブラリの提供元を調べる（README.md「検査対象のフォルダ」）---
contents_providers <- function(sonames) {
    conffile <- file.path(tempdir(), "contents.conf")
    writeLines(c(
        "Acquire::IndexTargets::deb::Contents-deb {",
        '    MetaKey "$(COMPONENT)/Contents-$(ARCHITECTURE)";',
        '    ShortDescription "Contents-$(ARCHITECTURE)";',
        '    Description "$(RELEASE)/$(COMPONENT) $(ARCHITECTURE) Contents";',
        '    KeepCompressed "true";',
        '    Optional "true";',
        "};",
        "Acquire::IndexTargets::deb::Contents-deb-legacy {",
        '    MetaKey "Contents-$(ARCHITECTURE)";',
        '    ShortDescription "Contents-$(ARCHITECTURE)";',
        '    Description "$(RELEASE) $(ARCHITECTURE) Contents";',
        '    KeepCompressed "true";',
        '    Optional "true";',
        '    Fallback-Of "Contents-deb";',
        '    Identifier "Contents-deb";',
        "};"
    ), conffile)
    update <- system2("apt-get", shQuote(c(
        "-c", conffile, "-o", "Acquire::Retries=2", "-o", "Acquire::http::Timeout=20",
        "-o", "Acquire::https::Timeout=20", "-o", "APT::Update::Error-Mode=any", "update"
    )))
    if (update != 0L) stop("Ubuntu の APT 索引の更新に失敗しました。", call. = FALSE)
    if (!length(sonames)) return(list())
    indices <- command_output("apt-get", c(
        "-c", conffile, "indextargets", "--format", "$(FILENAME)", "Identifier: Contents-deb"
    ))
    indices <- indices[file.exists(indices)]
    if (!length(indices)) stop("Ubuntu の Contents 索引を取得できません。", call. = FALSE)
    match_files <- r"--{
        /usr/lib/apt/apt-helper cat-file "${@:2}" | awk -v names="$1" '
            BEGIN { n = split(names, items, " "); for (i=1; i<=n; i++) wanted[items[i]] = 1 }
            $1 ~ /^(usr\/)?lib(\/[a-z0-9_]+-linux-gnu|64)?\/[^\/]+$/ {
                name = $1
                sub(/^.*\//, "", name)
                if (name in wanted) print name "\t" $2
            }
        '
    }--"
    matches <- tryCatch(
        command_output("bash", c(
            "-o", "pipefail", "-c", match_files, "bash", paste(sonames, collapse = " "), indices
        )),
        error = function(error) {
            stop("Ubuntu の Contents 索引を展開できません: ", conditionMessage(error),
                 call. = FALSE)
        }
    )
    pieces <- strsplit(matches, "\t", fixed = TRUE)
    providers <- split(lapply(pieces, function(row) {
        sub("^.*/", "", strsplit(row[[2L]], ",", fixed = TRUE)[[1L]])
    }), vapply(pieces, "[[", character(1), 1L))
    lapply(providers, function(x) sort(unique(unlist(x, use.names = FALSE))))
}

# --- 共有ライブラリごとに提供元を選ぶ（候補が 1 つか、ubuntu-packages.txt に書いた候補が 1 つならそれ。ほかは要確認）（README.md「検査対象のフォルダ」）---
choose_providers <- function(sonames, providers, listed) {
    chosen <- character()
    unresolved <- data.frame(item = character(), detail = character())
    for (soname in sonames) {
        options <- providers[[soname]]
        explicit <- intersect(options, listed)
        if (length(explicit) == 1L) options <- explicit
        if (length(options) == 1L) {
            chosen[[soname]] <- options
        } else {
            unresolved[nrow(unresolved) + 1L, ] <- c(
                soname, if (length(options)) paste("提供パッケージの候補", paste(options, collapse = ", "))
                else "Ubuntu の索引に提供パッケージが見つかりません。"
            )
        }
    }
    list(chosen = chosen, unresolved = unresolved)
}

# --- 入れる前と後の一覧から Ubuntu の計画を作る（ベースのパッケージが消えたら止める）（README.md「R」）---
ubuntu_plan_rows <- function(inventory, after, requested, libraries) {
    installed <- inventory[inventory$Status == "installed", ]
    base_versions <- setNames(installed$Version, installed$Package)
    after <- after[after$Status == "installed" & !duplicated(after$Package), ]
    removed <- setdiff(names(base_versions), after$Package)
    if (length(removed)) {
        stop("APT がベースのパッケージを削除しました: ", paste(removed, collapse = ", "),
             call. = FALSE)
    }
    after <- after[order(after$Package), ]
    packages <- after$Package
    base_version <- unname(base_versions[packages])
    data.frame(
        package = packages, architecture = after$Architecture, version = after$Version,
        relation = ifelse(packages %in% requested, "direct",
                          ifelse(is.na(base_version), "indirect", "base")),
        base_version = base_version,
        action = ifelse(is.na(base_version), "install",
                        ifelse(base_version == after$Version, "keep", "update")),
        language_packages = vapply(packages, function(name) {
            paste(sort(unique(libraries$r_package[which(libraries$ubuntu == name)])),
                  collapse = ", ")
        }, character(1))
    )
}

# --- R の共有ライブラリを Ubuntu のパッケージに対応づけ、APT で入れて、入れる前と後の差で計画を作る（README.md「R」）---
get_ubuntu_plan <- function(libraries, extra) {
    inventory <- read.delim("/report/base-ubuntu.tsv", skip = 1L, colClasses = "character")
    paths <- unique(libraries$path[libraries$path != "not found"])
    aliases <- sub("^/lib", "/usr/lib", paths)
    aliases[paths == aliases] <- sub("^/usr/lib", "/lib", paths[paths == aliases])
    owners <- list()
    if (length(paths)) {
        lines <- suppressWarnings(system2("dpkg-query",
            shQuote(c("-S", unique(c(paths, aliases)))), stdout = TRUE, stderr = FALSE))
        lines <- grep(": /", lines, value = TRUE)
        packages <- strsplit(sub(": /.*$", "", lines), ",", fixed = TRUE)
        owners <- split(lapply(packages, function(names) sub(":.*$", "", trimws(names))),
                        sub("^.*: ", "", lines))
    }
    providers_by_file <- lapply(libraries$path, function(path) {
        names <- unlist(owners[[path]], use.names = FALSE)
        if (!length(names) && path %in% paths) {
            names <- unlist(owners[[aliases[match(path, paths)]]], use.names = FALSE)
        }
        if (length(names)) unique(names) else NA_character_
    })
    rows <- rep(seq_len(nrow(libraries)), lengths(providers_by_file))
    libraries <- libraries[rows, , drop = FALSE]
    libraries$ubuntu <- as.character(unlist(providers_by_file, use.names = FALSE))

    missing <- libraries$path == "not found"
    sonames <- sort(unique(libraries$soname[missing]))
    providers <- if (length(sonames) || length(extra)) contents_providers(sonames) else list()
    choice <- choose_providers(sonames, providers, extra)
    for (soname in names(choice$chosen)) {
        libraries$ubuntu[missing & libraries$soname == soname] <- choice$chosen[[soname]]
    }
    unresolved <- choice$unresolved

    providers <- sort(unique(na.omit(libraries$ubuntu[missing])))
    requested <- sort(unique(c(extra, providers)))
    simulated <- character()
    if (length(requested)) {
        simulated <- command_output("apt-get", c(
            "-s", "--no-install-recommends", "install", "--", requested
        ))
    }
    writeLines(simulated, "/report/apt-plan.log")
    if (any(startsWith(simulated, "Remv "))) {
        stop("APT の試算でベースのパッケージが削除対象になりました。", call. = FALSE)
    }
    if (length(requested)) {
        status <- system2("apt-get", shQuote(c(
            "-o", "Acquire::Retries=5", "install", "-y", "--no-install-recommends", "--", requested
        )))
        if (status != 0L) stop("Ubuntu パッケージを入れられません。", call. = FALSE)
    }

    after <- ubuntu_package_inventory()
    writeLines(after[-1L], "/report/installed-ubuntu.tsv")
    after <- read.delim(text = after, colClasses = "character")
    plan <- ubuntu_plan_rows(inventory, after, requested, libraries)
    list(plan = plan, unresolved = unresolved)
}

# --- Ubuntu の調査の入口（R の調査と同じベースと取得元か確かめる）（README.md「R」）---
inspect_ubuntu_main <- function(extra) {
    local({
        inspected <- readRDS("/report/r-discovery.rds")
        write_base_inventories()
        if (!identical(inspected$base, readLines("/report/base-r.tsv", n = 1L)) ||
            !identical(inspected$repo, analysis_repository())) {
            stop("Step 1 のベースイメージまたは Posit の版が現在の設定と一致しません。", call. = FALSE)
        }
        ubuntu <- get_ubuntu_plan(inspected$libraries, extra)
        write_report("ubuntu-packages.tsv", ubuntu$plan)
        native_source <- inspected$plan$package[which(
            inspected$plan$package_format == "source" & inspected$plan$needs_compilation == "yes"
        )]
        source_review <- data.frame(
            item = native_source,
            detail = rep("DESCRIPTION のシステム要件を確認してください。", length(native_source))
        )
        review <- rbind(ubuntu$unresolved, source_review, inspected$review)
        write_report("review.tsv", review)
    })
}

# --- 環境を監査し、r-audit.json と、installed-packages など 4 つの TSV を書く（README.md「R」）---
inspect_audit_main <- function(args) {
if (length(args) != 1L || !dir.exists(args[[1L]]) || !dir.exists("/audit/confirmed")) {
    stop("Expected a baseline directory and the dependency lock at /audit/confirmed")
}
release <- character()
installed_rows <- data.frame(name = character(), version = character())
state <- new.env()
state$issues <- character()
state$checks <- data.frame(check = character(), status = character(), detail = character())
state$items <- audit_items("", character())
state$facts <- list()
complete <- FALSE
on.exit({
    write.table(state$checks, "/result/audit-checks.tsv", sep = "\t", row.names = FALSE)
    write.table(state$items, "/result/audit-items.tsv", sep = "\t", row.names = FALSE)
    writeLines(json_text(c(list(
        status = if (!complete || length(state$issues)) {
            "FAIL"
        } else if (any(state$checks$status == "REVIEW")) {
            "REVIEW"
        } else {
            "PASS"
        },
        r = R.version.string, machine = R.version$arch,
        os_release = as.list(release),
        executable = file.path(R.home("bin"), "R"),
        installed = installed_rows
    ), state$facts, list(
        checks = state$checks,
        scope = paste("Installed R packages, version constraints, namespace loading,",
                      "shared libraries and Ubuntu packages; not application behavior.")
    ))), "/result/r-audit.json")
})
os_lines <- if (file.exists("/etc/os-release")) readLines("/etc/os-release", warn = FALSE)
release <- .inspect_os_fields(os_lines)
for (field in intersect(c("ID", "VERSION_ID"), names(release))) {
    cat("AUDIT_OS_", field, "=", release[[field]], "\n", sep = "")
}
desc <- read.dcf("/audit/DESCRIPTION", all = TRUE)[1L, ]
fields <- intersect(c("Depends", "Imports", "LinkingTo"), names(desc))
text <- paste(na.omit(desc[fields]), collapse = ",")
specs <- if (nzchar(trimws(text))) {
    tools:::.split_dependencies(strsplit(text, ",", fixed = TRUE)[[1L]])
} else {
    list()
}
installed <- installed.packages(fields = c("Depends", "Imports", "LinkingTo"), noCache = TRUE)
installed_rows <- data.frame(name = installed[, "Package"], version = installed[, "Version"],
                             row.names = NULL)
copies <- split(installed_rows$version, installed_rows$name)
installed <- installed[!duplicated(installed[, "Package"]), , drop = FALSE]
rownames(installed) <- installed[, "Package"]
current <- tryCatch(read.delim(text = ubuntu_package_inventory(), colClasses = "character",
                               quote = ""),
                    error = identity)
inventory <- data.frame(ecosystem = rep("r", nrow(installed_rows)), package = installed_rows$name,
                        architecture = rep("", nrow(installed_rows)),
                        version = installed_rows$version, row.names = NULL)
if (!inherits(current, "error")) {
    current <- current[current$Status == "installed", ]
    if (nrow(current)) inventory <- rbind(inventory, data.frame(
        ecosystem = "ubuntu", package = current$Package, architecture = current$Architecture,
        version = current$Version))
}
write.table(inventory, "/result/installed-packages.tsv", sep = "\t", row.names = FALSE)
record_review <- function(label, detail) {
    summary <- paste(detail, collapse = "; ")
    state$checks[nrow(state$checks) + 1L, ] <- list(label, "REVIEW", summary)
    cat(label, ": REVIEW\n   ", summary, "\n", sep = "")
}
record <- function(label, detail = character(), note = "") {
    state$checks[nrow(state$checks) + 1L, ] <- list(label, if (length(detail)) "FAIL" else "PASS",
                                                    if (length(detail))
                                                        paste(detail, collapse = "; ") else note)
    cat(label, ": ", if (length(detail)) "FAIL" else "PASS", "\n", sep = "")
    if (length(detail)) {
        cat(paste("  ", detail), sep = "\n")
        state$issues <- c(state$issues, label)
    }
}
add_items <- function(rows) state$items <- rbind(state$items, rows)
actual_version <- function(name) {
    if (name == "R") {
        as.character(getRversion())
    } else if (name %in% rownames(installed)) {
        installed[name, "Version"]
    } else {
        NA_character_
    }
}
check_spec <- function(spec) {
    name <- spec$name
    actual <- actual_version(name)
    if (is.na(actual)) return(paste(name, "missing"))
    if (!is.null(spec$op) &&
        !isTRUE(do.call(spec$op, list(package_version(actual), spec$version))))
        return(paste(name, "expected", spec$op, as.character(spec$version), "found", actual))
    character()
}
spec_items <- function(check, specs, items = vapply(specs, function(x) x$name, character(1))) {
    audit_items(check, items,
                vapply(specs, function(x) {
                    if (is.null(x$op)) "" else paste(x$op, as.character(x$version))
                }, character(1)),
                vapply(specs, function(x) actual_version(x$name), character(1)),
                vapply(specs, function(x) if (length(check_spec(x))) "FAIL" else "PASS",
                       character(1)))
}
record("DESCRIPTION requirements", unlist(lapply(specs, check_spec), use.names = FALSE),
       note = sprintf("%d 件", length(specs)))
add_items(spec_items("DESCRIPTION requirements", specs))
roots <- setdiff(names(specs), "R")
closure <- sort(union(roots, unlist(tools::package_dependencies(roots, db = installed,
    which = c("Depends", "Imports", "LinkingTo"), recursive = TRUE), use.names = FALSE)))
record("Missing R dependencies", setdiff(closure, rownames(installed)),
       note = sprintf("%d 件", length(closure)))
add_items(audit_items("Missing R dependencies", closure, "",
                      vapply(closure, actual_version, character(1)),
                      ifelse(closure %in% rownames(installed), "PASS", "FAIL")))
violations <- character()
for (name in intersect(closure, rownames(installed))) {
    for (field in c("Depends", "Imports", "LinkingTo")) {
        value <- installed[name, field]
        if (!is.na(value) && nzchar(value)) {
            parsed <- tools:::.split_dependencies(strsplit(value, ",", fixed = TRUE)[[1L]])
            violations <- c(violations, unlist(lapply(parsed, check_spec), use.names = FALSE))
            add_items(spec_items("R dependency version constraints", parsed,
                                 paste(name, field, vapply(parsed, function(x) x$name,
                                                           character(1)))))
        }
    }
}
record("R dependency version constraints", violations,
       note = sprintf("%d 件", length(intersect(closure, rownames(installed)))))
loadable <- intersect(closure, rownames(installed))
load_errors <- vapply(loadable, function(name) {
    output <- suppressWarnings(system2(file.path(R.home("bin"), "Rscript"), c(
        "--vanilla", "-e", shQuote("invisible(loadNamespace(commandArgs(TRUE)[[1L]]))"), name
    ), stdout = TRUE, stderr = TRUE, timeout = 600))
    status <- attr(output, "status")
    if (is.null(status)) return("")
    last <- tail(output[nzchar(trimws(output))], 1L)
    if (identical(status, 124L)) "timed out after 600 s" else
        if (length(last)) trimws(last) else paste("exit status", status)
}, character(1))
loads <- paste0(loadable, ": ", load_errors)[nzchar(load_errors)]
record("R namespace loading", loads, note = sprintf("%d 件", length(closure)))
add_items(audit_items("R namespace loading", loadable, "",
                      ifelse(nzchar(load_errors), load_errors, "loaded"),
                      ifelse(nzchar(load_errors), "FAIL", "PASS")))
state$facts$namespaces <- list(checked = closure, failed = loads)
run <- function(cmd, args) suppressWarnings(system2(cmd, args, stdout = TRUE, stderr = TRUE))
targets <- rownames(installed)[is.na(installed[, "Priority"]) | installed[, "Priority"] != "base"]
Sys.setenv(LD_LIBRARY_PATH = paste(c(R.home("lib"), Sys.getenv("LD_LIBRARY_PATH")), collapse = ":"))
native <- inspect_shared_libraries(setNames(file.path(installed[targets, "LibPath"], targets),
                                            targets))
details <- c(native$review, native$missing,
             if (length(native$foreign))
                 paste("他の機械用のため調べない:", paste(native$foreign, collapse = ", ")))
if (native$status == "REVIEW") {
    record_review("Shared libraries", details)
} else {
    record("Shared libraries", if (native$status == "FAIL") details,
           note = paste(details, collapse = "; "))
}
write.table(native$libraries, "/result/native-libraries.tsv", sep = "\t", row.names = FALSE)
state$facts$shared_libraries <- list(status = native$status, files = native$scanned,
                                     missing = native$missing, review = native$review,
                                     foreign = native$foreign)
quarto <- audit_quarto(Sys.which("quarto"))
if (quarto$status == "REVIEW") {
    record_review("Quarto CLI", quarto$detail)
} else {
    record("Quarto CLI", if (quarto$status == "FAIL") quarto$detail, note = quarto$detail)
}
add_items(audit_items("Quarto CLI", "quarto", "", quarto$found, quarto$status))
state$facts$quarto <- quarto
os <- audit_os_release(os_lines)
record("OS (Debian/Ubuntu)", os$failure, note = os$name)
apt <- run("apt-get", c("-o", "Debug::NoLocking=1", "-o", "Dir::Cache::pkgcache=",
                       "-o", "Dir::Cache::srcpkgcache=", "check"))
record("APT consistency", command_failure(apt))
dpkg <- run("dpkg", "--audit")
record("dpkg audit", c(command_failure(dpkg, character()),
                       if (is.null(attr(dpkg, "status"))) trimws(dpkg[nzchar(trimws(dpkg))])))
record("Ubuntu package inventory", if (inherits(current, "error")) conditionMessage(current)
    else if (!nrow(current)) "No installed OS packages")
path <- args[[1L]]
base <- read.delim(file.path(path, "base-r.tsv"), skip = 1L, colClasses = "character")
actual <- setNames(installed[, "Version"], installed[, "Package"])
changed <- base$Package[is.na(actual[base$Package]) | actual[base$Package] != base$Version]
record("Base R packages removed or changed", changed)
add_items(audit_items("Base R packages removed or changed", base$Package, base$Version,
                      actual[base$Package]))
inventory_available <- !inherits(current, "error")
keys <- function(x) paste(x$Package, x$Architecture, sep = ":")
if (inventory_available) {
    baseline <- read.delim(file.path(path, "base-ubuntu.tsv"), skip = 1L, colClasses = "character")
    baseline <- baseline[baseline$Status == "installed", ]
    plan <- read.delim("/audit/confirmed/ubuntu-packages.tsv", colClasses = "character",
                       na.strings = "")
    plan_keys <- paste(plan$package, plan$architecture, sep = ":")
    record("Base OS packages removed or changed",
           audit_base_changes(baseline, current, plan_keys[plan$action == "update"]))
    add_items(audit_base_items(baseline, current, plan_keys[plan$action == "update"]))
}
record_unlisted <- function(label, unlisted) {
    if (length(unlisted)) record_review(label, unlisted) else record(label)
}
r_pins <- read.delim("/audit/confirmed/r-pins.tsv", colClasses = "character")
confirmed_r <- audit_confirmed_r(actual, base$Package, r_pins, copies)
r_items <- function(check) confirmed_r$items[confirmed_r$items$check == check, ]
record("Confirmed R versions", confirmed_r$versions)
add_items(r_items("Confirmed R versions"))
if (inventory_available) {
    ubuntu <- audit_confirmed_ubuntu(current, plan)
    ubuntu_items <- function(check) ubuntu$items[ubuntu$items$check == check, ]
    record("Confirmed Ubuntu versions", ubuntu$versions)
    add_items(ubuntu_items("Confirmed Ubuntu versions"))
}
unlisted <- confirmed_r$unlisted
versions <- vapply(copies[unlisted], paste, character(1), collapse = ", ")
record_unlisted("Unlisted R additions", if (length(unlisted)) paste(unlisted, versions))
add_items(r_items("Unlisted R additions"))
if (inventory_available) {
    record_unlisted("Unlisted Ubuntu additions", ubuntu$unlisted)
    add_items(ubuntu_items("Unlisted Ubuntu additions"))
    record("Confirmed Ubuntu additions missing or ambiguous", ubuntu$missing)
    add_items(ubuntu_items("Confirmed Ubuntu additions missing or ambiguous"))
}
report <- file.path(R.home(), "r-install-report.tsv")
hashes <- audit_r_install_report(r_pins, if (file.exists(report)) {
    read.delim(report, colClasses = "character")
})
if (hashes$status == "REVIEW") {
    record_review("Confirmed R package hashes", hashes$errors)
} else {
    record("Confirmed R package hashes", hashes$errors, note = sprintf("%d 件", nrow(r_pins)))
}
add_items(hashes$items)
expected_r <- sub("^# rocker/rstudio:([^@]+)@.*$", "\\1",
                  readLines(file.path(path, "base-r.tsv"), n = 1L))
runtime <- paste0("Expected ", expected_r, ", found ", getRversion())
record("Confirmed R runtime version",
       if (as.character(getRversion()) != expected_r) runtime, note = runtime)
add_items(audit_items("Confirmed R runtime version", "R", expected_r,
                      as.character(getRversion())))
cat("Additional R packages: ",
    paste(setdiff(rownames(installed), base$Package), collapse = ", "), "\n", sep = "")
if (inventory_available) {
    cat("Additional OS packages: ", paste(setdiff(keys(current), keys(baseline)), collapse = ", "),
        "\n", sep = "")
}
complete <- TRUE
if (length(state$issues)) stop("Dependency audit failed: ", paste(state$issues, collapse = ", "))
cat("DEPENDENCY AUDIT PASSED\n")

}

# --- コマンドラインの入口（r・ubuntu・audit）（README.md「構成」）---
if (sys.nframe() == 0L) {
    args <- commandArgs(trailingOnly = TRUE)
    usage <- "使い方: dependencies-inspect.R r PACKAGE... | ubuntu PACKAGE... | audit BASELINE_DIR"
    if (!length(args)) stop(usage)
    mode <- args[[1L]]
    if (identical(mode, "r")) inspect_r_main(args[-1L])
    else if (identical(mode, "ubuntu")) inspect_ubuntu_main(args[-1L])
    else if (identical(mode, "audit")) inspect_audit_main(args[-1L])
    else stop("知らないモードです: ", mode, "。", usage)
}
