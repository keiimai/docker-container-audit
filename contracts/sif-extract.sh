# --- SIF の中のファイルシステムを、Docker の名前付きボリューム（/work）に取り出す（R と Python の共通。README.md「Apptainer の SIF」）---
# 使い方（R と Python は、このファイルの中身を読んで、Apptainer のイメージ（apptainer-images.tsv）の中で動かす）:
#   docker run --rm --network none --mount <SIF を /input に読み取り専用> --mount <ボリュームを /work> \
#     --entrypoint sh <Apptainer のイメージ> -c "<このファイルの中身>" sh /input/<SIF> <アーキテクチャ（arm64・amd64）>
# apptainer build --sandbox は使わない。Apptainer は SIF を展開するとき利用者の名前空間を作るが、Docker の初期の
# seccomp では特別な権限の無いコンテナに作らせないため、Docker の中では止まる（2026-11-25 に確認）。
# 代わりに、apptainer sif list でファイルシステムの区画（FS (Squashfs/*System/<アーキテクチャ>)）を 1 つだけ選び、
# apptainer sif dump でその区画を取り出し、unsquashfs で /work/rootfs に展開する（中身は Apptainer が展開するものと同じ区画）。
set -eu
sif=$1
architecture=$2
case "$architecture" in
    arm64 | amd64) ;;
    *)
        echo "アーキテクチャは arm64 か amd64 です: $architecture" >&2
        exit 1
        ;;
esac
if ! command -v unsquashfs >/dev/null 2>&1; then
    echo "Apptainer のイメージに unsquashfs がありません。" >&2
    exit 1
fi
apptainer sif list "$sif"
# 区画の表の 5 列目（TYPE）が、そのアーキテクチャのファイルシステムである行の ID（1 列目）。
found=$(apptainer sif list "$sif" | awk -F'|' -v type="FS (Squashfs/*System/$architecture)" '
    { kind = $5; gsub(/^[ \t]+|[ \t]+$/, "", kind)
      if (kind == type) { id = $1; gsub(/[ \t]/, "", id); print id } }')
if [ "$(printf '%s\n' "$found" | grep -c .)" != 1 ]; then
    echo "SIF に $architecture のファイルシステムの区画が 1 つだけではありません（見つかった ID: ${found:-なし}）。" >&2
    exit 1
fi
apptainer sif dump "$found" "$sif" > /work/rootfs.squashfs
unsquashfs -d /work/rootfs /work/rootfs.squashfs
rm -f /work/rootfs.squashfs
