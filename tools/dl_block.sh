#!/bin/bash
# 分块并行下载（curl 分块 + 逐块内容校验，xargs 并发 + 失败重试）
# 用法: bash dl_block.sh <url> <output_file> [block_size_mb] [block_dir]
#   block_dir 缺省 = 输出同目录；driver 调用时传输出目录下的隐藏子目录
#   （块文件与输出分开落点，下完即删：中间产物不占输出侧的卷）
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"   # 本脚本所在（工具目录）；
                                        # 与下面的 DIR（= 输出目录）不是一回事，别混
set -u
URL="$1"
OUT="$2"
BLOCK_MB="${3:-100}"
BLOCK=$((BLOCK_MB * 1024 * 1024))
DIR=$(dirname "$OUT")
mkdir -p "$DIR"
BASE=$(basename "$OUT")
# 分块落点：默认与输出同目录；给了第 4 参就放那里（driver 传 <outdir>/.blocks_<名>/）
BLOCK_DIR="${4:-$DIR}"
mkdir -p "$BLOCK_DIR"

# 获取文件总大小
TOTAL=$(curl -sI --connect-timeout 10 "$URL" | grep -i content-length | awk '{print $2}' | tr -d '\r')
if [ -z "$TOTAL" ] || [ "$TOTAL" -eq 0 ]; then
  echo "ERROR: cannot get file size"
  exit 1
fi
NBLOCKS=$(( (TOTAL + BLOCK - 1) / BLOCK ))
echo "Total: $((TOTAL/1073741824))GB, Blocks: $NBLOCKS x ${BLOCK_MB}MB"

# 单块下载函数（带重试）
dl_one() {
  local i=$1
  local part="$BLOCK_DIR/${BASE}.part_$i"
  local start=$((i * BLOCK))
  local end=$((start + BLOCK - 1))
  [ $end -ge $TOTAL ] && end=$((TOTAL - 1))
  for try in 1 2 3; do
    curl -sL -r $start-$end --connect-timeout 10 --max-time 900 "$URL" -o "$part"
    local size=$(stat -c%s "$part" 2>/dev/null || echo 0)
    local expect=$((end - start + 1))
    if [ "$size" -eq "$expect" ]; then
      # 内容验证（逐块解压检查）
      if python3 "$HERE/verify_block.py" "$part"; then
        return 0
      fi
    fi
    rm -f "$part"
    sleep 3
  done
  return 1
}
export -f dl_one
export URL OUT DIR BASE BLOCK TOTAL BLOCK_DIR HERE

# 并行下载（8 路）
seq 0 $((NBLOCKS - 1)) | xargs -P 8 -I {} bash -c 'dl_one {}'

# 检查完整性
FAIL=0
for ((i=0; i<NBLOCKS; i++)); do
  part="$BLOCK_DIR/${BASE}.part_$i"
  size=$(stat -c%s "$part" 2>/dev/null || echo 0)
  start=$((i * BLOCK))
  end=$((start + BLOCK - 1))
  [ $end -ge $TOTAL ] && end=$((TOTAL - 1))
  expect=$((end - start + 1))
  if [ "$size" -ne "$expect" ]; then
    echo "BLOCK $i FAILED: $size/$expect"
    FAIL=1
  fi
done
if [ "$FAIL" -eq 1 ]; then
  echo "FAIL: incomplete blocks, retry failed blocks once"
  # 重试失败的块（单块重下）
  for ((i=0; i<NBLOCKS; i++)); do
    part="$BLOCK_DIR/${BASE}.part_$i"
    size=$(stat -c%s "$part" 2>/dev/null || echo 0)
    start=$((i * BLOCK))
    end=$((start + BLOCK - 1))
    [ $end -ge $TOTAL ] && end=$((TOTAL - 1))
    expect=$((end - start + 1))
    if [ "$size" -ne "$expect" ]; then
      echo "Retrying block $i..."
      rm -f "$part"
      dl_one $i || { echo "BLOCK $i STILL FAILED"; exit 1; }
    fi
  done
fi

# 拼接
echo "Concatenating..."
: > "$OUT"
for ((i=0; i<NBLOCKS; i++)); do
  cat "$BLOCK_DIR/${BASE}.part_$i" >> "$OUT"
  rm -f "$BLOCK_DIR/${BASE}.part_$i"
done
rm -f "$BLOCK_DIR/${BASE}.part_"* 2>/dev/null
rmdir "$BLOCK_DIR" 2>/dev/null || true
echo "DONE: $OUT ($(stat -c%s "$OUT") bytes)"