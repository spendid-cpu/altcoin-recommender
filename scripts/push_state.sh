#!/usr/bin/env bash
# 워크플로(scan/track/stock)가 갱신한 data/state.db를 master에 올린다. 사용법: bash scripts/push_state.sh "커밋 메시지"
# 평소에는 pull --rebase + push로 끝난다. 다른 작업이 그 사이에 state.db를 올려서 이진 파일 충돌이 나면(2026-10-08 스캔 1회 실패)
# 내 DB를 따로 보관해 두고 원격 최신 위에 다시 얹어 올린다(그 다른 작업의 DB 변경은 이 DB가 덮어쓴다. 코드 등 다른 파일은 원격 것을 그대로 둔다).
set -u
msg="${1:-state: 상태 갱신 [skip ci]}"
git config user.name "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"
git add data/state.db
if git diff --staged --quiet; then
  echo "state.db 변경 없음"
  exit 0
fi
git commit -q -m "$msg"
for i in 1 2 3; do
  if git pull -q --rebase origin master && git push -q origin HEAD:master; then
    exit 0
  fi
  echo "상태 파일 올리기 실패(시도 $i/3) - 내 DB를 보관하고 원격 최신 위에 다시 얹는다"
  git rebase --abort 2>/dev/null || true
  keep="${RUNNER_TEMP:-/tmp}/state_ours_$$.db"
  cp data/state.db "$keep"
  git fetch -q origin master
  git reset -q --hard origin/master
  cp "$keep" data/state.db
  git add data/state.db
  git commit -q -m "$msg (충돌 후 다시 얹음)" || true
  if git push -q origin HEAD:master; then
    exit 0
  fi
  sleep $((i * 3))
done
echo "상태 파일 올리기를 3번 시도했지만 실패했다"
exit 1
