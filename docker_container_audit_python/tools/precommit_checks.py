# --- コミット前の検査のうち、pre-commit の既製のフックに無いもの（CHECKS.md 1 節）---
from __future__ import annotations

import ast
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

#: よく知られたトークンの形
TOKEN_PATTERNS: dict[str, re.Pattern[str]] = {
    name: re.compile(pattern)
    for name, pattern in {
        "AWS のアクセスキー": r"(?:A3T[A-Z0-9]|ABIA|ACCA|AKIA|ASIA)[0-9A-Z]{16}",
        "AWS のシークレットキー": (
            r"(?i)aws.{0,20}?(?:key|pwd|pw|password|pass|token).{0,20}?['\"][0-9a-zA-Z/+]{40}['\"]"
        ),
        "Artifactory のトークン": r"(?m)(?:\s|=|:|\"|^)AKC[a-zA-Z0-9]{10,}(?:\s|\"|$)",
        "Artifactory のパスワード": r"(?m)(?:\s|=|:|\"|^)AP[0-9A-F][a-zA-Z0-9]{8,}(?:\s|\"|$)",
        "Azure のストレージキー": r"AccountKey=[a-zA-Z0-9+/=]{88}",
        "URL に埋め込んだパスワード": r"://[^:/?#\[\]@!$&'()*+,;=\s]+:[^:/?#\[\]@!$&'()*+,;=\s]+@",
        "Discord のトークン": r"[MNO][a-zA-Z0-9_-]{23,25}\.[a-zA-Z0-9_-]{6}\.[a-zA-Z0-9_-]{27}",
        "GitHub のトークン": r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{36}|github_pat_[A-Za-z0-9_]{82}",
        "GitLab のトークン": (
            r"(?:(?:glpat|gldt|glft|glsoat|glrt)-[A-Za-z0-9_-]{20,50}|GR1348941[A-Za-z0-9_-]{20,50}|"
            r"glcbt-(?:[0-9a-fA-F]{2}_)?[A-Za-z0-9_-]{20,50}|glimt-[A-Za-z0-9_-]{25}|"
            r"glptt-[A-Za-z0-9_-]{40}|glagent-[A-Za-z0-9_-]{50,1024}|gloas-[A-Za-z0-9_-]{64})(?!\w)"
        ),
        "JWT": r"eyJ[A-Za-z0-9_=-]+\.eyJ[A-Za-z0-9_=-]+",
        "Mailchimp のキー": r"[0-9a-z]{32}-us[0-9]{1,2}",
        "npm のトークン": r"//.+/:_authToken=\s*(?:npm_.+|[A-Fa-f0-9-]{36})",
        "OpenAI のキー": r"sk-[A-Za-z0-9_-]*[A-Za-z0-9]{20}T3BlbkFJ[A-Za-z0-9]{20}",
        "PyPI のトークン": r"pypi-(?:AgEIcHlwaS5vcmc|AgENdGVzdC5weXBpLm9yZw)[A-Za-z0-9_-]{70,}",
        "SendGrid のキー": r"SG\.[a-zA-Z0-9_-]{22}\.[a-zA-Z0-9_-]{43}",
        "Slack のトークン": r"(?i)xox(?:a|b|p|o|s|r)-(?:[0-9]+-)+[a-z0-9]+",
        "Slack の Webhook": r"https://hooks\.slack\.com/services/T[a-zA-Z0-9_]+/B[a-zA-Z0-9_]+/[a-zA-Z0-9_]+",
        "Square のキー": r"sq0csp-[0-9A-Za-z\\_-]{43}",
        "Stripe のキー": r"(?:r|s)k_live_[0-9a-zA-Z]{24}",
        "Telegram のトークン": r"(?m)^[0-9]{8,10}:[0-9A-Za-z_-]{35}$",
        "Twilio のキー": r"(?:AC|SK)[a-z0-9]{32}",
    }.items()
}

#: 秘密情報らしい名前に書いた値（代入・引数・辞書・YAML の形。名前と値は英数字だけで見る）
SECRET_ASSIGNMENT = re.compile(
    r"(?ai)(?:^|[^\w.])[\w.]*(?:access|api|auth|key|credential|creds|passw(?:or)?d|secret|token)"
    r"[\w.]*[\"']?\]?\s*[=:]\s*[\"']?([\w.=+/-]{10,150})"
)

#: 同じ形で、値が引用符の中にあるもの
SECRET_QUOTED = re.compile(SECRET_ASSIGNMENT.pattern.replace("?([", "([", 1))

#: 値を検査する、環境変数のファイル（Git で追跡する雛形）
ENV_TEMPLATES = {".env.example"}

#: 秘密情報らしい名前の値を探す、コード・ノートブック・YAML（ノートブックはセルの中身を読む）
SECRET_SUFFIXES = {".py", ".qmd", ".ipynb", ".yaml", ".yml", ".cff"}

#: 記録の流れを握る関数
FLOW_FUNCTIONS = {"run", "finish", "fail"}

#: JSON のコメント（文字列の中は除く）。コメントを書ける JSON（VS Code の設定など）も読むため
JSON_COMMENT = re.compile(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/', re.DOTALL)

#: コメントを空白に置き換える文字（改行は残し、指摘の行と桁をずらさない）
BLANK = re.compile(r"[^\n]")


# --- 文字の散らばり（シャノンのエントロピー。1 文字あたりのビット数）（CHECKS.md 1 節）---
def entropy(value: str) -> float:
    counts = Counter(value)
    total = len(value)
    return -sum(n / total * math.log2(n / total) for n in counts.values())


# --- 秘密情報らしい名前に書いた値を探す（CHECKS.md 1 節）---
def secret_values(text: str, quoted: bool) -> list[str]:
    values = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        for found in (SECRET_QUOTED if quoted else SECRET_ASSIGNMENT).finditer(line):
            value = found.group(1)
            if not re.fullmatch(r"[A-Za-z_.-]+", value) and entropy(value) >= 3.5:
                values.append(value)
    return values


# --- ノートブックのセルの中身をつなげて返す（読めなければ空）（CHECKS.md 1 節）---
def notebook_source(text: str) -> str:
    try:
        cells = json.loads(text).get("cells", [])
    except (ValueError, AttributeError):
        return ""
    return "\n".join(
        "".join(source) if isinstance(source, list) else str(source)
        for source in (cell.get("source", "") for cell in cells if isinstance(cell, dict))
    )


# --- src/ のコードが呼んでいる、記録の流れの関数の名前を返す（REPRODUCIBILITY.md 2 節）---
def flow_calls(text: str) -> list[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    modules = {"repro"} | {
        alias.asname or "repro"
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "repro"
    }
    imported = {
        alias.asname or alias.name: alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "repro"
        for alias in node.names
        if alias.name in FLOW_FUNCTIONS
    }
    called = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if (
            isinstance(function, ast.Attribute)
            and function.attr in FLOW_FUNCTIONS
            and isinstance(function.value, ast.Name)
            and function.value.id in modules
        ):
            called.add(function.attr)
        elif isinstance(function, ast.Name) and function.id in imported:
            called.add(imported[function.id])
    return sorted(called)


# --- ファイルごとに検査し、指摘を返す（CHECKS.md 1 節）---
def check(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    problems = []
    token = next((name for name, pattern in TOKEN_PATTERNS.items() if pattern.search(text)), None)
    if token is not None:
        problems.append(
            f"{path}: {token}らしき文字列があります。値を消し、そのトークンを無効にしてください"
            "（push 済みなら漏れたものとして扱う）"
        )
    if path.name in ENV_TEMPLATES and secret_values(text, quoted=False):
        problems.append(
            f"{path}: 秘密情報らしい名前に値が書かれています。"
            "値は .env（Git で追跡しない）に書いてください（.env.example には名前だけを残す）"
        )
    elif path.suffix in SECRET_SUFFIXES and secret_values(
        notebook_source(text) if path.suffix == ".ipynb" else text,
        quoted=path.suffix in {".py", ".qmd", ".ipynb"},
    ):
        problems.append(
            f"{path}: 秘密情報らしい名前に値が書かれています。"
            "値は .env（Git で追跡しない）に書き、コードではそのファイルを読んでください（環境変数としては届かない）"
        )
    if path.parts[0] == "src" and path.suffix == ".py" and path.as_posix() != "src/repro.py":
        called = flow_calls(text)
        if called:
            problems.append(
                f"{path}: {'・'.join(f'repro.{name}()' for name in called)} を呼んでいます。"
                "記録の流れ（repro.run()・finish()・fail()）は入口のノートブックに直接書き、"
                "src/ の関数からは呼ばないでください"
                "（→ docs/REPRODUCIBILITY.md 2 節）"
            )
    if path.suffix == ".json" and text:
        try:
            json.loads(
                JSON_COMMENT.sub(
                    lambda found: found.group(1) or BLANK.sub(" ", found.group(0)), text
                )
            )
        except json.JSONDecodeError as error:
            problems.append(f"{path}: JSON として読めません: {error}")
    return problems


# --- 渡されたファイルをすべて検査し、指摘があれば止める（CHECKS.md 1 節）---
def main(argv: list[str]) -> int:
    problems = [problem for name in argv for problem in check(Path(name))]
    if problems:
        print("[pre-commit] 直す必要があります:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    return 0


# --- スクリプトとして走らせたときの入口（pre-commit が呼ぶ）（CHECKS.md 1 節）---
if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
