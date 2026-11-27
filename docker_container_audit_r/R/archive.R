# --- イメージの保存・読み戻しと SIF の作成・監査を行う（Python は src/archive.py）（README.md「構成」）---

# --- 第 3 段階でイメージの中で依存を調べ直し、確定記録と照合して監査する（README.md「第 3 段階の中身」）---
audit_r_image <- function(image, lock = NULL, platform = "linux/arm64",
                          operation = "audit-r", prepare = NULL, more_parameters = list(),
                          more_inputs = character(), run_id = NULL, details = FALSE) {
    validate_platform(platform)
    lock <- required_lock(lock)
    if (!operation %in% c("audit-r", "audit-r-archive", "audit-r-sif")) {
        stop("監査の操作名は audit-r・audit-r-archive・audit-r-sif のどれかです。", call. = FALSE)
    }
    if (operation == "audit-r-sif") {
        existing <- function(path) {
            if (!is.character(path) || length(path) != 1L || is.na(path)) return(FALSE)
            file_test("-f", path) || file_test("-f", project_file(path))
        }
        if (!is.function(prepare) || !existing(more_parameters$sif) ||
            !endsWith(more_parameters$sif, ".sif") || is.null(more_parameters$sif_record) ||
            !existing(file.path(more_parameters$sif_record, "sif.tsv"))) {
            stop("SIF を監査するには、SIF のファイルと sif の工程の記録先（sif.tsv がある）を",
                 "指定してください。", call. = FALSE)
        }
    }
    saved <- tryCatch(
        read_build_environment(file.path(dirname(lock), "build-environment.json")),
        error = function(e) NULL
    )
    inputs <- c(project_file(INSPECT_SCRIPT), lock_inputs(lock), more_inputs)
    parameters <- c(list(image = image, platform = platform, dependency_lock = shown_path(lock),
                         input_hashes = saved$inputs, base_image = saved$base_image),
                    more_parameters)
    run_operation(operation, parameters, run_id = run_id, inputs = inputs,
                  action = function(directory) {
        check <- operation_checks(directory, "audit")
        items <- operation_check_items(directory, "audit")
        if (!is.null(prepare)) image <- prepare(directory, check, items)
        confirmed <- check("Dependency lock", function() {
            items("Dependency lock", lock_item_rows("Dependency lock", lock))
            verify_r_dependency_lock(lock, platform)
        }, detail = shown_path(lock))
        bundle <- confirmed$bundle
        baseline <- file.path(directory, "baseline")
        check("Base inventory", function() {
            stage_base_inventories(baseline, bundle, confirmed$environment$base_image, platform)
            base_inventory_detail(baseline)
        }, detail = identity)
        staged <- file.path(directory, "DESCRIPTION")
        if (!file.copy(file.path(bundle, "DESCRIPTION"), staged)) {
            stop("DESCRIPTION を記録できません。", call. = FALSE)
        }
        audit <- NULL
        check("Audit checks", function() {
            summary <- file.path(directory, "audit-checks.tsv")
            statuses <- function() read.delim(summary, colClasses = "character")$status
            audit <<- tryCatch(
                run_r_image_audit(image, directory, baseline, staged, platform, bundle,
                                  details = details),
                error = function(e) {
                    if (!file.exists(summary)) stop(e)
                    stop(status_detail(statuses()), "。", conditionMessage(e), call. = FALSE)
                })
            statuses()
        }, detail = status_detail, status = worst_status)
        checks <- read.delim(file.path(directory, "audit-checks.tsv"), colClasses = "character")
        open <- checks$check[checks$status == "REVIEW"]
        review <- if (length(open)) paste("要確認:", paste(open, collapse = ", "))
        value <- list(directory = directory, checks = checks)
        if (details) {
            value <- c(value, list(report = audit$report, log = audit$log,
                                   status = worst_status(c(audit$status, checks$status)),
                                   review = audit$review, run_id = run_id, evidence = directory))
        }
        list(value = value, message = review)
    })
}

# --- 第 3 段階で作った SIF の中身を、audit と同じ条件で監査する（README.md「Apptainer の SIF」）---
audit_r_sif <- function(sif, image, lock = NULL, sif_record = NULL, platform = "linux/arm64",
                        run_id = NULL, details = FALSE) {
    validate_platform(platform)
    if (!file_test("-f", sif) || !endsWith(sif, ".sif")) {
        stop("監査する SIF のファイルを指定してください: ", sif, call. = FALSE)
    }
    table <- file.path(sif_record %||% "", "sif.tsv")
    if (is.null(sif_record) || !file_test("-f", table)) {
        stop("sif の工程の記録先（sif.tsv がある）を指定してください。", call. = FALSE)
    }
    name <- sif_image_name(image, platform)
    audit_r_image(name, lock, platform = platform, operation = "audit-r-sif",
                  more_parameters = list(sif = shown_path(sif), source_image = image,
                                         sif_record = shown_path(sif_record)),
                  more_inputs = c(sif, table), run_id = run_id, details = details,
                  prepare = function(directory, check, items) {
        check("SIF unchanged", function() {
            rows <- read.delim(table, colClasses = "character", na.strings = NULL)
            expected <- rows$sha256[rows$item == "sif"]
            if (length(expected) != 1L) expected <- NA_character_
            found <- file_sha256(sif)
            items("SIF unchanged", shown_path(sif), expected, found)
            if (length(expected) != 1L || !identical(expected, found)) {
                stop("SIF の SHA-256 が sif の工程の記録（sif.tsv）と一致しません: ", sif,
                     call. = FALSE)
            }
            found
        }, detail = identity)
        import_sif_image(sif, image, name, platform, directory, check, items)
    })
}

# --- 保存する .tar の名前（<イメージ>_<アーキテクチャ>.tar。置き場所は outputs/images/<実行 ID>/）（README.md「第 3 段階の中身」）---
image_archive_name <- function(image, platform = "linux/arm64") {
    architecture <- sub("^linux/", "", validate_platform(platform))
    paste0(gsub("[^A-Za-z0-9._-]", "_", image), "_", architecture, ".tar")
}

# --- 既存の親フォルダを基準に保存先の絶対パスを求める（README.md「第 3 段階の中身」）---
archive_output_location <- function(path, run_id = NULL) {
    if (!is.character(path) || length(path) != 1L || is.na(path)) return(path)
    if (basename(path) == path) {
        if (!is.null(run_id)) path <- file.path(run_id, path)
        path <- file.path("outputs", "images", path)
    }
    parent <- dirname(path)
    missing <- character()
    while (!dir.exists(parent)) {
        missing <- c(basename(parent), missing)
        parent <- dirname(parent)
    }
    parent <- normalizePath(parent, mustWork = TRUE)
    for (part in missing) {
        parent <- switch(part, "." = parent, ".." = dirname(parent), file.path(parent, part))
    }
    file.path(parent, basename(path))
}

# --- 保存先を工程のパラメーターに書く（README.md「結果の共通形式」）---
archive_output_parameter <- function(path) {
    if (!is.character(path) || length(path) != 1L || is.na(path)) return(path)
    root <- paste0(getwd(), "/")
    if (startsWith(path, root)) substring(path, nchar(root) + 1L) else path
}

# --- 出力先のパスを確かめる（拡張子・プロジェクトの中・まだ無いこと）（README.md「第 3 段階の中身」）---
artifact_path <- function(path, extension) {
    if (!is.character(path) || length(path) != 1L || is.na(path) || !endsWith(path, extension)) {
        stop("ファイル名の末尾を ", extension, " にしてください。", call. = FALSE)
    }
    path <- archive_output_location(path)
    if (!startsWith(path, paste0(getwd(), "/"))) {
        stop("出力先はプロジェクト内を指定してください。", call. = FALSE)
    }
    if (!dir.exists(dirname(path)) && startsWith(path, file.path(getwd(), "outputs/images/"))) {
        dir.create(dirname(path), recursive = TRUE, showWarnings = FALSE)
    }
    if (!dir.exists(dirname(path))) stop("出力先のフォルダがありません: ", path, call. = FALSE)
    if (file.exists(path)) {
        stop("出力先が既にあります。別の名前を指定してください。\n", path, call. = FALSE)
    }
    path
}

# --- 作業フォルダで作り、完成したファイルだけを出力先にハードリンクで置く（README.md「第 3 段階の中身」）---
write_image_artifact <- function(output, extension, create) {
    prefix <- if (identical(extension, ".sif")) "export-sif-" else "export-image-"
    with_work_directory(prefix, function(work) {
        staging <- file.path(work, paste0("image", extension))
        create(staging, work)
        size <- file.size(staging)
        if (is.na(size) || size == 0) stop("出力ファイルが空です。", call. = FALSE)

        if (!suppressWarnings(file.link(staging, output))) {
            stop("出力先に保存できません。既存ファイルと出力先を確認してください。\n",
                 output, call. = FALSE)
        }
        message("保存しました ", output)
        invisible(output)
    })
}

# --- イメージを .tar で保存する（保存の前後でイメージ ID が変わらないか確かめる）（README.md「第 3 段階の中身」）---
write_image_tar <- function(image, tar = image_archive_name(image, platform), log = NULL,
                            expected_id = NULL, platform = "linux/arm64", check = NULL,
                            items = NULL) {
    validate_platform(platform)
    output <- artifact_path(tar, ".tar")
    unchanged <- function(record = FALSE) {
        if (is.null(expected_id)) return(invisible())
        found <- tryCatch(local_image_id(image, platform), error = identity)
        if (record && !is.null(items)) {
            items("Image unchanged during save", image, expected_id,
                  if (inherits(found, "error")) NA_character_ else found)
        }
        if (inherits(found, "error")) stop(found)
        if (!identical(found, expected_id)) {
            stop("Docker タグの実体が保存の開始時と一致しません。", call. = FALSE)
        }
    }
    write_image_artifact(output, ".tar", function(staging, work) {
        unchanged()
        run_command("docker", c(
            "image", "save", "--platform", platform, "--output", staging, "--", image
        ), log = log)
        if (is.null(check)) {
            unchanged()
        } else {
            check("Image unchanged during save", function() unchanged(record = TRUE),
                  detail = expected_id)
        }
    })
}

# --- イメージを .tar で保存し、SHA-256 を記録する（既定の置き場所は outputs/images/<実行 ID>/）（README.md「第 3 段階の中身」）---
save_r_image <- function(image, tar = NULL, platform = "linux/arm64", run_id = NULL,
                         output = NULL, details = FALSE) {
    validate_platform(platform)
    if (!is.null(tar) && !is.null(output)) {
        stop("保存先は tar と output のどちらか一方で指定してください。", call. = FALSE)
    }
    tar <- tar %||% output
    if (is.null(tar) || identical(tar, "")) tar <- image_archive_name(image, platform)
    tar <- archive_output_location(tar, run_id)
    parameters <- list(image = image, tar = archive_output_parameter(tar), platform = platform)
    run_operation("save-r", parameters, run_id = run_id,
                  action = function(directory) {
        check <- operation_checks(directory, "save")
        items <- operation_check_items(directory, "save")
        valid <- tryCatch(artifact_path(tar, ".tar"), error = identity)
        if (inherits(valid, "error")) {
            check("Archive written", detail = conditionMessage(valid), status = "FAIL")
            stop(valid)
        }
        image_id <- check("Image platform", function() local_image_id(image, platform),
                          detail = identity)
        log <- file.path(directory, "save.log")
        archive <- check("Archive written", function() {
            write_image_tar(image, valid, log = log,
                            expected_id = image_id, platform = platform, check = check,
                            items = items)
        }, detail = size_and_sha256)
        readback <- readback_image_name(image, platform)
        check("Archive loads back", function() {
            readback_archive(archive, image_id, readback, platform, log, items)
        }, detail = identity)
        relative <- substring(archive, nchar(getwd()) + 2L)
        digest <- file_sha256(archive)
        writeLines(c("path\tsha256", paste(relative, digest, sep = "\t")),
                   file.path(directory, "archive.tsv"))
        value <- if (details) {
            list(archive = archive, sha256 = digest, image_id = image_id, readback = readback,
                 log = log, paths = list(archive), status = "PASS", run_id = run_id,
                 evidence = directory)
        } else {
            archive
        }
        list(value = value, paths = archive, image_id = image_id)
    })
}

# --- 外へ書く名前（絶対パス・..）とリンクが無いかを確かめてから .tar を展開し、項目の数を返す（README.md「Apptainer の SIF」）---
safe_extract <- function(archive, directory, items = NULL) {
    names <- untar(archive, list = TRUE)
    listing <- suppressWarnings(system2("tar", c("-tvf", shQuote(archive)), stdout = TRUE))
    linked <- substr(listing, 1L, 1L) %in% c("l", "h")
    if (length(linked) != length(names)) linked <- rep(any(linked), length(names))
    found <- ifelse(linked, "link", ifelse(grepl("^/", names) |
                                               grepl("(^|/)[.][.](/|$)", names), "outside", "safe"))
    if (!is.null(items)) items("Archive entries safe", sub("/$", "", names), "safe", found)
    if (any(found != "safe")) {
        stop(".tar に安全でない中身（外への書き込みかリンク）があります: ", archive, call. = FALSE)
    }
    if (untar(archive, exdir = directory) != 0L) {
        stop(".tar を展開できません: ", archive, call. = FALSE)
    }
    length(names)
}

# --- 読み戻すときの名前（audit-readback/<イメージ>:<アーキテクチャ>。元のタグには触れない）（README.md「第 3 段階の中身」）---
readback_image_name <- function(image, platform = "linux/arm64") {
    paste0("audit-readback/", tolower(gsub("[^A-Za-z0-9._-]", "_", image)), ":",
           sub("^linux/", "", validate_platform(platform)))
}

# --- SIF から作るイメージの名前（audit-sif/<イメージ>:<アーキテクチャ>。読み戻しと同じ規則）（README.md「Apptainer の SIF」）---
sif_image_name <- function(image, platform = "linux/arm64") {
    paste0("audit-sif/", tolower(gsub("[^A-Za-z0-9._-]", "_", image)), ":",
           sub("^linux/", "", validate_platform(platform)))
}

# --- SIF を取り出す Docker のボリュームの名前（記録の実行 ID から作り、記号は - にする）（README.md「Apptainer の SIF」）---
sif_volume_name <- function(run_id) paste0("audit-sif-", gsub("[^A-Za-z0-9_.-]", "-", run_id))

# --- そのプラットフォームのイメージの設定（Config）を読む（README.md「Apptainer の SIF」）---
image_config <- function(image, platform) {
    found <- suppressWarnings(system2(find_command("docker"), shQuote(c(
        "image", "inspect", "--platform", platform, "--format", "{{json .Config}}", "--", image)),
        stdout = TRUE, stderr = FALSE))
    if (length(found) != 1L || !is.null(attr(found, "status"))) {
        stop(platform, " のイメージの設定を読めません: ", image, call. = FALSE)
    }
    jsonlite::parse_json(found)
}

# --- docker import の --change（元のイメージの Env・WorkingDir・User。空のものは付けない）（README.md「Apptainer の SIF」）---
sif_import_changes <- function(config) {
    quote <- function(value) {
        if (grepl("[[:cntrl:]]", value)) {
            return(paste0('"', gsub('(["\\\\])', "\\\\\\1", value), '"'))
        }
        as.character(jsonlite::toJSON(value, auto_unbox = TRUE))
    }
    env <- as.character(unlist(config$Env))
    values <- vapply(sub("^[^=]*=", "", env), quote, character(1), USE.NAMES = FALSE)
    changes <- c(
        if (length(env)) paste0("ENV ", sub("=.*$", "", env), "=", values),
        if (nzchar(config$WorkingDir %||% "")) paste("WORKDIR", config$WorkingDir),
        if (nzchar(config$User %||% "")) paste("USER", config$User))
    as.vector(rbind(rep("--change", length(changes)), changes))
}

# --- SIF の中のファイルシステムを取り出し、元のイメージの設定を付けて Docker のイメージにする（README.md「Apptainer の SIF」）---
import_sif_image <- function(sif, image, name, platform, directory, check, items) {
    apptainer <- contract_apptainer_image(platform)
    log <- file.path(directory, "sif-audit.log")
    volume <- sif_volume_name(basename(dirname(directory)))
    input <- normalizePath(sif, mustWork = TRUE)
    target <- file.path("/input", basename(sif))
    mount <- paste0("type=volume,source=", volume, ",destination=/work")
    wait_for_bind_sources(sif, apptainer, platform, log)
    run_command("docker", c("volume", "create", volume), log = log)
    on.exit(try(run_command("docker", c("volume", "rm", "-f", volume), log = log),
                silent = TRUE), add = TRUE)
    check("SIF file system extracted", function() {
        run_command("docker", c(
            "run", "--rm", "--platform", platform, "--network", "none", "--mount", mount,
            "--mount", bind_mount(input, target, "readonly"),
            "--entrypoint", "sh", apptainer,
            "-c", contract_sif_extract_script(), "sh", target,
            sub("^linux/", "", platform)
        ), log = log)
    }, detail = apptainer)
    check("SIF image imported", function() {
        changes <- sif_import_changes(image_config(image, platform))
        script <- paste(
            "docker=$1; platform=$2; mount=$3; apptainer=$4; name=$5; log=$6; shift 6;",
            '"$docker" run --rm --platform "$platform" --network none --mount "$mount"',
            '--entrypoint tar "$apptainer" -C /work/rootfs -cf - . 2>>"$log" |',
            '"$docker" import --platform "$platform" "$@" - "$name" >>"$log" 2>&1')
        run_command("bash", c("-o", "pipefail", "-c", script, "bash", find_command("docker"),
                              platform, paste0(mount, ",readonly"), apptainer, name, log, changes))
        local_image_id(name, platform)
    }, detail = identity)
    name
}

# --- そのプラットフォームの分のイメージ ID と層（RootFS）を返す（README.md「第 3 段階の中身」）---
platform_image_layers <- function(image, platform) {
    found <- suppressWarnings(system2(find_command("docker"), shQuote(c(
        "image", "inspect", "--platform", platform, "--format", "{{.Id}} {{json .RootFS.Layers}}",
        "--", image)), stdout = TRUE, stderr = FALSE))
    if (length(found) != 1L || !is.null(attr(found, "status"))) {
        stop(platform, " のイメージを確認できません: ", image, call. = FALSE)
    }
    list(id = sub(" .*$", "", found), layers = unlist(jsonlite::parse_json(sub("^[^ ]+ ", "",
                                                                                  found))))
}

# --- 保存した .tar を別の名前で読み戻し、元のイメージと ID と層が同じかを確かめる（README.md「第 3 段階の中身」）---
readback_archive <- function(archive, image_id, name, platform, log, items = NULL) {
    with_work_directory("readback-", function(work) {
        layout <- file.path(work, "layout")
        dir.create(layout)
        safe_extract(archive, layout)
        index_path <- file.path(layout, "index.json")
        if (!file_test("-f", index_path)) {
            stop("OCI の形の .tar（index.json がある）を指定してください: ", archive, call. = FALSE)
        }
        index <- jsonlite::read_json(index_path)
        tag <- sub("^.*:", "", name)
        index$manifests <- lapply(index$manifests, function(item) {
            item$annotations[["io.containerd.image.name"]] <- paste0("docker.io/", name)
            item$annotations[["org.opencontainers.image.ref.name"]] <- tag
            item
        })
        writeLines(jsonlite::toJSON(index, auto_unbox = TRUE, digits = NA), index_path)
        manifest_path <- file.path(layout, "manifest.json")
        if (file_test("-f", manifest_path)) {
            manifest <- jsonlite::read_json(manifest_path)
            manifest <- lapply(manifest, function(item) {
                item$RepoTags <- list(name)
                item
            })
            writeLines(jsonlite::toJSON(manifest, auto_unbox = TRUE, digits = NA), manifest_path)
        }
        packed <- file.path(work, "readback.tar")
        script <- '
exec >>"$5" 2>&1
"$3" -C "$1" -cf "$2" .
status=$?
printf "tar の終了コード %s\n" "$status"
[ "$status" -eq 0 ] || exit "$status"
"$4" load <"$2"
status=$?
printf "docker load の終了コード %s\n" "$status"
exit "$status"'
        status <- system2(find_command("sh"), shQuote(c(
            "-c", script, "sh", layout, packed, find_command("tar"), find_command("docker"), log
        )))
        if (status != 0L) stop("読み戻しに失敗しました。ログ ", log, call. = FALSE)
    })
    original <- platform_image_layers(image_id, platform)
    loaded <- platform_image_layers(name, platform)
    if (!is.null(items)) items("Archive loads back", readback_item_rows(original, loaded))
    if (!identical(original, loaded)) {
        stop("読み戻したイメージが元のイメージの ", platform, " の分と一致しません: ", name,
             call. = FALSE)
    }
    paste0(name, " ", loaded$id, "、層 ", length(loaded$layers))
}

# --- 元のイメージと読み戻したイメージの ID と層を 1 つずつ照合した行にする（片方だけの層も行にする）（README.md「第 3 段階の中身」）---
readback_item_rows <- function(original, loaded) {
    count <- max(length(original$layers), length(loaded$layers))
    layer <- function(value) if (count) as.character(value$layers)[seq_len(count)] else character()
    check_item_rows("Archive loads back", c("image ID", if (count) paste("layer", seq_len(count))),
                    c(original$id, layer(original)), c(loaded$id, layer(loaded)))
}

# --- 固定した Apptainer のイメージの apptainer の版を返す（読めなければ unavailable）（README.md「Apptainer の SIF」）---
apptainer_version <- function(apptainer, platform) {
    found <- suppressWarnings(system2(find_command("docker"), shQuote(c(
        "run", "--rm", "--platform", platform, "--network", "none", "--entrypoint", "apptainer",
        apptainer, "--version")), stdout = TRUE, stderr = FALSE))
    if (!is.null(attr(found, "status")) || !length(found) || !nzchar(trimws(found[[1L]]))) {
        return("unavailable")
    }
    trimws(found[[1L]])
}

# --- .tar を展開し、目次（index.json）を対象のプラットフォームのイメージ 1 つに絞る（README.md「Apptainer の SIF」）---
sif_layout <- function(archive, work, platform, check = NULL, items = NULL) {
    if (is.null(check)) check <- function(name, run, ...) run()
    layout <- file.path(work, "layout")
    if (!dir.create(layout, showWarnings = FALSE)) {
        stop("新しい展開先を作成できません: ", layout, call. = FALSE)
    }
    check("Archive entries safe", function() safe_extract(archive, layout, items),
          detail = function(count) paste(count, "項目"))
    check("Archive platform image", function() {
        index_path <- file.path(layout, "index.json")
        if (!file_test("-f", index_path)) {
            stop("OCI の形の .tar（index.json がある）を指定してください: ", archive, call. = FALSE)
        }
        index <- jsonlite::read_json(index_path)
        architecture <- sub("^linux/", "", validate_platform(platform))
        blob <- function(digest) {
            path <- file.path(layout, "blobs", sub(":", "/", digest, fixed = TRUE))
            if (file_test("-f", path)) jsonlite::read_json(path)
        }
        item_platform <- function(item) {
            if (!is.null(item$platform)) return(item$platform)
            manifest <- blob(item$digest)
            config <- if (!is.null(manifest$config$digest)) blob(manifest$config$digest)
            list(architecture = config$architecture, os = config$os)
        }
        keep <- Filter(function(item) {
            attached <- !is.null(item$annotations[["io.containerd.manifest.subject"]])
            found <- item_platform(item)
            identical(found$architecture, architecture) && identical(found$os, "linux") &&
                !attached
        }, index$manifests)
        if (!is.null(items) && length(keep)) {
            items("Archive platform image", vapply(keep, function(item) item$digest, character(1)),
                  platform, platform, if (length(keep) == 1L) "PASS" else "FAIL")
        }
        if (length(keep) != 1L) {
            stop(platform, " のイメージが .tar に 1 つだけ入っていません: ", archive, call. = FALSE)
        }
        index$manifests <- keep
        writeLines(jsonlite::toJSON(index, auto_unbox = TRUE, digits = NA), index_path)
        keep[[1L]]$digest
    }, detail = identity)
}

# --- 保存した .tar から固定した Apptainer で SIF を作り、アーキテクチャと SHA-256 を記録する（README.md「Apptainer の SIF」）---
save_r_sif <- function(archive, sif = NULL, platform = "linux/arm64", run_id = NULL,
                       output = NULL, details = FALSE) {
    validate_platform(platform)
    if (!file_test("-f", archive) || !endsWith(archive, ".tar")) {
        stop("保存した .tar のファイルを指定してください: ", archive, call. = FALSE)
    }
    if (!is.null(sif) && !is.null(output)) {
        stop("保存先は sif と output のどちらか一方で指定してください。", call. = FALSE)
    }
    sif <- sif %||% output
    if (is.null(sif) || identical(sif, "")) {
        sif <- sub("[.]tar$", ".sif", basename(archive))
    }
    apptainer <- contract_apptainer_image(platform)
    path <- archive_output_location(sif, run_id)
    parameters <- list(image = archive, sif = archive_output_parameter(path),
                       platform = platform, apptainer = apptainer)
    run_operation("save-r-sif", parameters, run_id = run_id, inputs = archive,
                  action = function(directory) {
        if (!startsWith(normalizePath(archive), paste0(getwd(), "/"))) {
            stop("保存した .tar は、プロジェクトの中のものを指定してください: ", archive, call. = FALSE)
        }
        output <- artifact_path(path, ".sif")
        log <- file.path(directory, "sif.log")
        found <- new.env()
        check <- operation_checks(directory, "sif")
        items <- operation_check_items(directory, "sif")
        check("SIF written", function() {
            write_image_artifact(output, ".sif", function(staging, work) {
                found$digest <- sif_layout(archive, work, platform, check, items)
                writeLines(paste("Manifest", found$digest), log)
                wait_for_bind_sources(work, apptainer, platform, log)
                apptainer_run <- function(...) {
                    run_command("docker", c(
                        "run", "--rm", "--platform", platform, "--network", "none",
                        "--mount", bind_mount(normalizePath(work), "/work"),
                        "--entrypoint", "apptainer", apptainer, ...
                    ), log = log)
                }
                check("SIF build", function() {
                    apptainer_run("build", "/work/image.sif", "oci:/work/layout")
                }, detail = apptainer)
                write_tool_versions(directory, data.frame(
                    tool = "apptainer", version = apptainer_version(apptainer, platform)))
                check("SIF file system architecture", function() {
                    apptainer_run("sif", "list", "/work/image.sif")
                    architecture <- sub("^linux/", "", platform)
                    listed <- grep(paste0("Squashfs/.*", architecture, "\\)"),
                                   readLines(log, warn = FALSE), value = TRUE)
                    if (!length(listed)) {
                        stop("SIF の中身が ", platform, " ではありません。ログ ", log, call. = FALSE)
                    }
                    trimws(listed[[1L]])
                }, detail = identity)
            })
        }, detail = function(path) size_and_sha256(path))
        relative <- function(path) substring(normalizePath(path), nchar(getwd()) + 2L)
        digest <- file_sha256(output)
        writeLines(c("item\tpath\tsha256",
                     paste("archive", relative(archive), file_sha256(archive), sep = "\t"),
                     paste("sif", relative(output), digest, sep = "\t"),
                     paste("manifest", "", sub("^sha256:", "", found$digest), sep = "\t"),
                     paste("apptainer", apptainer, "", sep = "\t")),
                   file.path(directory, "sif.tsv"))
        value <- if (details) {
            list(sif = output, sha256 = digest, log = log, paths = list(output),
                 status = "PASS", run_id = run_id, evidence = directory)
        } else {
            output
        }
        list(value = value, paths = output)
    })
}

# --- 記録の TSV を書く（REPRODUCIBILITY.md 2 節）---
write_image_record <- function(data, path) {
    write.table(data, path, sep = "\t", row.names = FALSE, na = "", quote = FALSE)
}
