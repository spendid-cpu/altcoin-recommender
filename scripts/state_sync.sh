#!/usr/bin/env bash
# 실행 간 상태 파일(SQLite)을 코드(master)와 분리된 전용 브랜치에 저장한다 (2026-11-15).
# 예전에는 data/state.db(약 25MB)를 5분마다 master에 커밋해서 저장소가 461MB까지 커졌다.
# 이제 브랜치마다 '파일 하나짜리 커밋 1개'만 두고 매번 강제로 덮어쓴다 -> 이력이 쌓이지 않는다.
#   bash scripts/state_sync.sh pull <브랜치> <브랜치 안 파일명> <받을 경로>
#   bash scripts/state_sync.sh push <브랜치> <올릴 파일> <브랜치 안 파일명> "<메시지>"
# 브랜치마다 쓰는 쪽은 하나뿐이어야 한다: state = scan·track·stock(같은 동시성 그룹), state-watchdog = watchdog.
set -u
cmd="$1"; branch="$2"
git config user.name "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"

if [ "$cmd" = "pull" ]; then
  name="$3"; dest="$4"
  mkdir -p "$(dirname "$dest")"
  if git fetch -q --depth 1 origin "$branch" 2>/dev/null && git show "FETCH_HEAD:$name" > "$dest.tmp" 2>/dev/null; then
    mv "$dest.tmp" "$dest"
    echo "상태 파일 받음: $branch/$name -> $dest ($(du -h "$dest" | cut -f1))"
  else
    rm -f "$dest.tmp"
    echo "상태 브랜치 $branch 없음 - 저장소에 있는 파일(있으면)로 시작한다"
  fi
  exit 0
fi

if [ "$cmd" = "push" ]; then
  src="$3"; name="$4"; msg="${5:-state: 상태 갱신}"
  [ -f "$src" ] || { echo "올릴 상태 파일이 없다: $src"; exit 1; }
  blob=$(git hash-object -w "$src")
  tree=$(printf '100644 blob %s\t%s\n' "$blob" "$name" | git mktree)
  commit=$(git commit-tree "$tree" -m "$msg")
  for i in 1 2 3; do
    if git push -q -f origin "$commit:refs/heads/$branch"; then
      echo "상태 파일 올림: $src -> $branch/$name"
      exit 0
    fi
    echo "상태 파일 올리기 실패(시도 $i/3)"
    sleep $((i * 3))
  done
  exit 1
fi
echo "사용법: state_sync.sh pull|push ..."; exit 2
