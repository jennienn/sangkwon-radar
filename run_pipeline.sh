#!/bin/bash
# =========================================================
# 상권레이더 - 파이프라인 전체를 순서 보장하며 실행
#
# 기존에는 cron에 collect(2:00)/process(2:05)/backup(2:10)을 각각
# 고정 시각으로 등록해뒀는데, collect.py가 25개 구를 도느라 5분을
# 넘기면 process.py가 그날의 새 데이터가 아니라 어제 데이터를 그대로
# 처리해버리는 경쟁 상태(race condition) 문제가 실제로 발생했다.
# (2026-07-10 새벽, process가 02:05에 어제 2075건을 처리하고,
#  collect는 02:07에야 끝나며 1916건을 새로 수집한 사례로 확인됨)
#
# 그래서 세 단계를 &&로 묶어 "앞 단계가 성공해야 다음 단계 실행"되도록
# 순서를 강제한다. 이러면 collect.py가 몇 분이 걸리든 상관없이 항상
# 최신 데이터로 process.py가 실행된다.
# =========================================================

cd /home/opc/sangkwon-radar || exit 1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] ===== 파이프라인 시작 ====="

python3 scripts/collect.py >> logs/collect.log 2>&1
if [ $? -ne 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] collect.py 실패 - 파이프라인 중단"
    exit 1
fi

python3 scripts/process.py >> logs/process.log 2>&1
if [ $? -ne 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] process.py 실패 - 백업은 생략"
    exit 1
fi

bash scripts/backup_to_object_storage.sh >> logs/backup.log 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] ===== 파이프라인 완료 ====="
