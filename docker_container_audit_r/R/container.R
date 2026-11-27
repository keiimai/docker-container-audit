# --- Docker を動かし、イメージ ID・プラットフォーム・ファイルのハッシュを調べる（Python は src/container.py）（README.md「構成」）---

# --- コマンドを探す（README.md「操作側と検査対象」）---
find_command <- function(command) {
    executable <- Sys.which(command)[[1L]]
    if (!nzchar(executable)) {
        stop(command, " が見つかりません。", call. = FALSE)
    }
    executable
}

# --- コマンドを走らせ、失敗なら止める（ログがあれば標準出力とエラーを追記）（REPRODUCIBILITY.md 2 節）---
run_command <- function(command, args, env = character(), log = NULL) {
    executable <- find_command(command)
    if (is.null(log)) {
        status <- system2(executable, shQuote(args), env = env)
    } else {
        status <- system2("/bin/sh", shQuote(c(
            "-c", 'log=$1; shift; exec "$@" >>"$log" 2>&1',
            "sh", log, executable, args
        )), env = env)
    }
    if (status != 0L) {
        stop(command, " の実行に失敗しました（終了コード ", status, "）。",
             if (!is.null(log)) paste0("\nログ ", log), call. = FALSE)
    }
}

# --- _tmp/ に、名前の頭が prefix の作業フォルダを作り、終われば消す（README.md「検査とテスト」）---
with_work_directory <- function(prefix, action) {
    scratch <- project_file("_tmp")
    dir.create(scratch, showWarnings = FALSE)
    work <- tempfile(prefix, tmpdir = scratch)
    if (!dir.create(work, mode = "0700")) {
        stop("作業フォルダを作成できません。", call. = FALSE)
    }
    on.exit(unlink(work, recursive = TRUE), add = TRUE)
    action(work)
}

# --- docker の --mount に渡す bind の指定を作る（CSV の形なので、パスの引用符は二重にする）（README.md「構成」）---
bind_mount <- function(source, destination, option = character()) {
    source <- gsub('"', '""', normalizePath(source, mustWork = FALSE), fixed = TRUE)
    paste(c("type=bind", paste0('"source=', source, '"'),
            paste0("destination=", destination), option), collapse = ",")
}

# --- 作った直後のパスが Docker から見えるまで待つ（Dropbox の同期の間。プロジェクトの外は止める）（README.md「構成」）---
wait_for_bind_sources <- function(paths, image, platform = NULL, log = NULL) {
    root <- audit_root()
    paths <- ifelse(startsWith(paths, "/"), paths, file.path(root, paths))
    paths <- unique(normalizePath(paths, mustWork = TRUE))
    prefix <- paste0(root, "/")
    if (!all(startsWith(paths, prefix))) {
        stop("Docker の共有確認にはプロジェクト内のパスを指定してください。", call. = FALSE)
    }

    probe <- '
visible() {
    remaining=$1
    current=/project
    while :; do
        ls -a -- "$current" >/dev/null 2>&1 || return 1
        component=${remaining%%/*}
        current="$current/$component"
        if [ "$component" = "$remaining" ]; then break; fi
        remaining=${remaining#*/}
    done
    test -e "$current"
}
for attempt in $(seq 1 60); do
    missing=
    for path do
        if ! visible "$path"; then missing=$path; break; fi
    done
    if [ -z "$missing" ]; then
        printf "Docker bind sources visible (attempt %s)\n" "$attempt"
        exit 0
    fi
    sleep 0.25
done
printf "Docker VM cannot see the bind source: %s\n" "$missing" >&2
exit 1'
    run_command("docker", c(
        "run", "--rm", "--network", "none", if (!is.null(platform)) c("--platform", platform),
        "--mount", bind_mount(root, "/project", "readonly"),
        "--entrypoint", "/bin/sh", image, "-ec", probe, "sh", substring(paths, nchar(prefix) + 1L)
    ), log = log)
    invisible(NULL)
}

# --- ファイルの SHA-256 を返す（REPRODUCIBILITY.md 3 節）---
file_sha256 <- function(path) {
    digest::digest(normalizePath(path, mustWork = TRUE), algo = "sha256", file = TRUE)
}

# --- バイト数と SHA-256 の説明（README.md「結果の共通形式」）---
size_and_sha256 <- function(path) {
    sprintf("%.0f バイト、SHA-256 %s", file.size(path), file_sha256(path))
}

# --- プラットフォームが linux/amd64 か linux/arm64 かを確かめる（README.md「検査対象のフォルダ」）---
validate_platform <- function(platform) {
    if (!is.character(platform) || length(platform) != 1L || is.na(platform) ||
        !platform %in% c("linux/amd64", "linux/arm64"))
        stop("platform は linux/amd64 または linux/arm64 を指定してください。", call. = FALSE)
    platform
}

# --- ローカルのイメージの ID を返す（タグを ID にしてから、そのプラットフォームがあるかを確かめる）（README.md「結果の共通形式」）---
local_image_id <- function(image, platform) {
    validate_platform(platform)
    docker <- find_command("docker")
    inspect <- function(args) {
        with_work_directory("inspect-id-", function(work) {
            errors <- file.path(work, "stderr")
            output <- suppressWarnings(system2(docker, shQuote(args), stdout = TRUE,
                                               stderr = errors))
            reason <- paste(readLines(errors, warn = FALSE), collapse = "\n")
            list(output = output, reason = if (nzchar(reason)) paste0(": ", reason) else "")
        })
    }
    id <- inspect(c("image", "inspect", "--format", "{{.Id}}", "--", image))
    if (length(id$output) != 1L || !is.null(attr(id$output, "status")) ||
        !grepl("^sha256:[a-f0-9]{64}$", id$output)) {
        stop("イメージ ID を確認できません: ", image, id$reason, call. = FALSE)
    }
    selected <- inspect(c("image", "inspect", "--platform", platform,
                          "--format", "{{.Os}}/{{.Architecture}}", "--", id$output))
    if (!identical(selected$output, platform)) {
        stop(platform, " のイメージがローカルにありません: ", image, selected$reason, call. = FALSE)
    }
    id$output
}

# --- 記録に書くパス（プロジェクトの中ならプロジェクトからの相対パス）（README.md「結果の共通形式」）---
shown_path <- function(path) {
    if (is.null(path)) return(NULL)
    root <- audit_root()
    source <- if (startsWith(path, "/")) path else file.path(root, path)
    full <- normalizePath(source, mustWork = FALSE)
    if (identical(full, root)) return(".")
    if (startsWith(full, paste0(root, "/"))) substring(full, nchar(root) + 2L) else path
}
