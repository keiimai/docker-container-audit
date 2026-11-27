# --- プロジェクトの場所と設定を読み、報告書の入力を集める（Python は src/project.py）（README.md「検査対象のフォルダ」）---

# --- プロジェクトの config.yaml を記録層で読む（名前違いと、もう一方の言語のプロジェクトは止める）（platform と節も見る）（README.md「検査対象のフォルダ」）---
load_project_config <- function(project) {
    paths <- project_paths(project)
    path <- file.path(paths$root, "config.yaml")
    if (!file_test("-f", path)) stop("設定ファイルがありません: ", path, call. = FALSE)
    cfg <- repro_config_load(path)
    if (!identical(cfg$project, project)) {
        stop("設定ファイルの project（", paste(cfg$project, collapse = " "), "）が、",
             "プロジェクトのフォルダの名前（", project, "）と違います: ", path, call. = FALSE)
    }
    validate_platform(cfg$platform)
    for (section in c("discovery", "image")) {
        if (!is.list(cfg[[section]]) || is.null(names(cfg[[section]]))) {
            stop("設定ファイルに ", section, ": の節がありません: ", path, call. = FALSE)
        }
    }
    cfg
}

# --- 設定の文字の値（無ければ空。文字でなければ止める）（README.md「検査対象のフォルダ」）---
config_text <- function(value, name) {
    if (is.null(value)) return("")
    if (!is.character(value) || length(value) != 1L || is.na(value)) {
        stop("設定ファイルの ", name, " は文字にしてください。", call. = FALSE)
    }
    value
}

# --- 設定の真偽の値（true か false。ほかの値なら止める）（README.md「検査対象のフォルダ」）---
config_flag <- function(value, name) {
    if (!is.logical(value) || length(value) != 1L || is.na(value)) {
        stop("設定ファイルの ", name, " は true か false にしてください。", call. = FALSE)
    }
    value
}

# --- base.txt を読む（1 行に KEY=VALUE。# から行末は注釈、空行は無視）（README.md「検査対象のフォルダ」）---
read_base_file <- function(path, required, optional = character()) {
    if (!file_test("-f", path)) stop("base.txt がありません: ", path, call. = FALSE)
    lines <- trimws(sub("#.*$", "", readLines(path, warn = FALSE, encoding = "UTF-8")))
    lines <- lines[nzchar(lines)]
    pattern <- "^([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=[[:space:]]*(.*)$"
    if (any(!grepl(pattern, lines))) {
        stop("base.txt は 1 行に KEY=VALUE の形で書いてください: ", path, call. = FALSE)
    }
    keys <- sub(pattern, "\\1", lines)
    values <- trimws(sub(pattern, "\\2", lines))
    unknown <- setdiff(keys, c(required, optional))
    if (length(unknown)) {
        stop("base.txt に知らないキーがあります: ", paste(unknown, collapse = ", "), "（", path,
             "）", call. = FALSE)
    }
    repeated <- unique(keys[duplicated(keys)])
    if (length(repeated)) {
        stop("base.txt でキーが重なっています: ", paste(repeated, collapse = ", "), "（", path,
             "）", call. = FALSE)
    }
    missing <- setdiff(required, keys[nzchar(values)])
    if (length(missing)) {
        stop("base.txt に ", paste(missing, collapse = ", "), " がありません: ", path,
             call. = FALSE)
    }
    setNames(values, keys)
}

# --- R の見本のプロジェクト（projects/example-r）（README.md「検査対象のフォルダ」）---
DEFAULT_PROJECT <- "example-r"

# --- プロジェクトのフォルダの根（既定は projects。テストでは options(audit.projects_root) で差し替える）（README.md「検査対象のフォルダ」）---
projects_root <- function() {
    configured <- getOption("audit.projects_root")
    if (!is.null(configured)) return(configured)
    root <- audit_root()
    if (identical(root, normalizePath(getwd()))) "projects" else file.path(root, "projects")
}

# --- プロジェクト名の形を確かめる（Docker のイメージ名にも使えるように小文字）（README.md「検査対象のフォルダ」）---
project_name <- function(project) {
    if (!is.character(project) || length(project) != 1L || is.na(project) ||
        !grepl("^[a-z0-9][a-z0-9_-]*$", project)) {
        stop("プロジェクト名は小文字の英数字・ハイフン・下線（^[a-z0-9][a-z0-9_-]*$）にしてください: ",
             paste(project, collapse = " "), call. = FALSE)
    }
    project
}

# --- request/ の中身から言語を決める（DESCRIPTION なら r、requirements.txt なら python）（README.md「検査対象のフォルダ」）---
project_language <- function(request = NULL, project = NULL) {
    if (!is.null(project)) {
        if (!is.null(request)) stop("プロジェクト名か request/ のどちらかを指定してください。",
                                    call. = FALSE)
        request <- file.path(projects_root(), project_name(project), "request")
    }
    found <- c(r = file_test("-f", file.path(request, "DESCRIPTION")),
               python = file_test("-f", file.path(request, "requirements.txt")))
    if (sum(found) != 1L) {
        stop("request/ には DESCRIPTION（R）か requirements.txt（Python）のどちらか 1 つを置いて",
             "ください: ", request, call. = FALSE)
    }
    names(found)[found]
}

# --- プロジェクトのフォルダ（根・request・dockerfile）を返す（R のプロジェクトだけ）（README.md「検査対象のフォルダ」）---
project_paths <- function(project = DEFAULT_PROJECT) {
    root <- file.path(projects_root(), project_name(project))
    request <- file.path(root, "request")
    if (!dir.exists(request)) {
        stop("プロジェクトの request/ がありません: ", request, call. = FALSE)
    }
    if (project_language(request) != "r") {
        stop("プロジェクト ", project, " は Python のプロジェクトです（request/requirements.txt）。",
             "Python の子（../docker_container_audit_python）の報告書を使ってください。",
             call. = FALSE)
    }
    list(project = project, root = root, request = request,
         dockerfile = file.path(root, "dockerfile"))
}

# --- 第 1 段階の入力のフォルダ（projects/<名前>/request）（README.md「検査対象のフォルダ」）---
project_request <- function(project = DEFAULT_PROJECT) project_paths(project)$request

# --- 第 3 段階のイメージの既定の名前（<プロジェクト名>:local）（README.md「検査対象のフォルダ」）---
default_image <- function(project = DEFAULT_PROJECT) paste0(project_name(project), ":local")

# --- request/base.txt の固定ベースと R パッケージの取得元（README.md「検査対象のフォルダ」）---
target_config <- function(context = project_request()) {
    values <- read_base_file(file.path(context, "base.txt"), c("BASE_IMAGE", "R_REPOSITORY"))
    base <- values[["BASE_IMAGE"]]
    pattern <- "^rocker/rstudio:([0-9]+[.][0-9]+[.][0-9]+)@sha256:([a-f0-9]{64})$"
    parts <- regmatches(base, regexec(pattern, base))[[1L]]
    if (length(parts) != 3L) {
        stop("BASE_IMAGE は rocker/rstudio:<版>@sha256:<64 桁のダイジェスト> にしてください: ",
             base, call. = FALSE)
    }
    repository <- values[["R_REPOSITORY"]]
    if (!grepl("^https://[^[:space:]]+/[0-9]{4}-[0-9]{2}-[0-9]{2}$", repository)) {
        stop("R_REPOSITORY は P3M の日付付きの URL（https://…/YYYY-MM-DD）にしてください: ",
             repository, call. = FALSE)
    }
    c(BASE_IMAGE = base, RSTUDIO_TAG = parts[[2L]], RSTUDIO_SHA256 = parts[[3L]],
      R_REPOSITORY = repository)
}

# --- 第 1 段階の入力（DESCRIPTION・base.txt と、あれば ubuntu-packages.txt）（README.md「検査対象のフォルダ」）---
target_inputs <- function(context = NULL, project = NULL) {
    if (!is.null(project)) {
        if (!is.null(context)) stop("プロジェクト名か request/ のどちらかを指定してください。",
                                    call. = FALSE)
        context <- project_request(project)
    }
    if (is.null(context)) stop("対象のフォルダを指定してください。", call. = FALSE)
    if (!dir.exists(context)) stop("対象のフォルダがありません: ", context, call. = FALSE)
    files <- c("DESCRIPTION", "base.txt", "ubuntu-packages.txt")
    paths <- setNames(file.path(context, files), files)
    missing <- setdiff(names(paths)[!file_test("-f", paths)], "ubuntu-packages.txt")
    if (length(missing)) {
        stop("対象のフォルダに ", paste(missing, collapse = "、"), " がありません: ", context,
             call. = FALSE)
    }
    paths[file_test("-f", paths)]
}

# --- 確定記録と、その中のファイル（記録の入力にする）（REPRODUCIBILITY.md 2 節）---
lock_inputs <- function(lock) {
    if (is.null(lock)) return(character())
    files <- file.path(dirname(lock), contract_lock_files("r"))
    c(lock, files[file_test("-f", files)])
}

# --- 第 1・第 3 段階の報告書の記録（repro_run の inputs）に渡すファイル（README.md「記録」）---
report_inputs <- function(project, stage, lock = NULL) {
    paths <- project_paths(project)
    config <- file.path(paths$root, "config.yaml")
    if (identical(stage, "discovery")) {
        files <- c(unname(target_inputs(paths$request)), config, INSPECT_DOCKERFILE)
    } else if (identical(stage, "image")) {
        files <- c(dockerfile_files(paths$dockerfile), config, lock_inputs(required_lock(lock)))
    } else {
        stop("段階は discovery か image です。", call. = FALSE)
    }
    specs <- file.path("..", "contracts", basename(contract_files()))
    unique(c(vapply(files, shown_path, character(1), USE.NAMES = FALSE), specs))
}

# --- 入力のハッシュ（第 1 段階の入力と、調査の環境の定義 Dockerfile.inspect）（REPRODUCIBILITY.md 2 節）---
target_input_hashes <- function(context) {
    c(as.list(vapply(target_inputs(context), file_sha256, character(1))),
      setNames(list(file_sha256(project_file(INSPECT_DOCKERFILE))), INSPECT_DOCKERFILE))
}

# --- 固定ベースの名前（rocker/rstudio:<版>@sha256:<ダイジェスト>。base.txt の BASE_IMAGE）（README.md「結果の共通形式」）---
analysis_base_image <- function(context = project_request()) {
    target_config(context)[["BASE_IMAGE"]]
}

# --- Ubuntu パッケージの一覧を読む（1 行に 1 つ。# から後は注釈）（README.md「検査対象のフォルダ」）---
read_package_list <- function(path) {
    if (!file_test("-f", path)) return(character())
    items <- trimws(sub("#.*$", "", readLines(path, warn = FALSE)))
    items <- items[nzchar(items)]
    if (any(!grepl("^[a-z0-9][a-z0-9+.-]+$", items))) {
        stop("Ubuntu パッケージは APT のパッケージ名を 1 行に 1 つ書いてください: ", path, call. = FALSE)
    }
    unique(items)
}

# --- 自動では分からない Ubuntu パッケージ（ubuntu-packages.txt。無ければ空）（README.md「検査対象のフォルダ」）---
target_ubuntu_packages <- function(context) {
    read_package_list(file.path(context, "ubuntu-packages.txt"))
}

# --- R の調査の環境の定義（README.md「構成」）---
INSPECT_DOCKERFILE <- "Dockerfile.inspect"

# --- 対象のコンテナの中で依存を調べるスクリプト（README.md「構成」）---
INSPECT_SCRIPT <- "R/dependencies-inspect.R"

# --- R の子の根（R/project.R のあるフォルダ。作業フォルダから上へたどって探す）（README.md「構成」）---
audit_root <- function() {
    directory <- normalizePath(getwd(), mustWork = TRUE)
    repeat {
        if (file.exists(file.path(directory, "R", "project.R")) &&
            file.exists(file.path(directory, INSPECT_DOCKERFILE))) {
            return(directory)
        }
        parent <- dirname(directory)
        if (identical(parent, directory)) stop("R の子の根が見つかりません。", call. = FALSE)
        directory <- parent
    }
}

# --- R の子の根からのパス（作業フォルダによらない）（README.md「構成」）---
project_file <- function(path) file.path(audit_root(), path)

# --- 親の contracts/ の直下のファイル（報告書の記録の inputs に渡す。子の根からの相対パス）（README.md「構成」）---
contract_files <- function() {
    directory <- contract_dir()
    files <- sort(list.files(directory, all.files = TRUE, no.. = TRUE), method = "radix")
    files <- files[file_test("-f", file.path(directory, files))]
    base <- if (identical(normalizePath(file.path("..", "contracts"), mustWork = FALSE),
                          normalizePath(directory))) file.path("..", "contracts") else directory
    file.path(base, files)
}

# --- 第 1 段階の確定記録の指定を確かめる（空なら、何をすればよいかを書いて止める）（README.md「第 2 段階の中身」）---
required_lock <- function(lock) {
    if (is.null(lock) || identical(lock, "")) {
        stop("第 1 段階の確定記録（dependency-lock.tsv）を、設定ファイル projects/<名前>/config.yaml の ",
             "image.dependency_lock に書いてください。第 1 段階の報告書 ",
             "notebooks/r-dependency-discovery.qmd で確定すると、表 dockerfile-inputs の ",
             "dependency_lock にパスが出ます。", call. = FALSE)
    }
    path <- if (startsWith(lock, "/")) lock else project_file(lock)
    normalizePath(path, mustWork = FALSE)
}

# --- ビルドに使う dockerfile/のファイル（隠しファイルを含み、.git は除く）（README.md「検査対象のフォルダ」）---
dockerfile_files <- function(dockerfile) {
    if (!file_test("-f", file.path(dockerfile, "Dockerfile"))) {
        stop("Dockerfile のフォルダに Dockerfile がありません: ", dockerfile, call. = FALSE)
    }
    walk <- function(directory) {
        names <- setdiff(list.files(directory, all.files = TRUE, no.. = TRUE), ".git")
        paths <- file.path(directory, names)
        targets <- normalizePath(paths, mustWork = FALSE)
        files <- paths[fs::is_file(targets, follow = FALSE)]
        children <- paths[fs::is_dir(paths, follow = FALSE)]
        for (child in children) files <- c(files, walk(child))
        files
    }
    sort(walk(dockerfile), method = "radix")
}
