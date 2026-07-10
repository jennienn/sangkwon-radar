#!/bin/bash
# =========================================================
# raw_data 폴더의 원본 백업 파일(JSON+XML)을 OCI Object Storage 버킷으로 업로드
# 사전 준비:
#   1) OCI CLI 설치 및 `oci setup config` 로 인증 설정 완료
#   2) 버킷 생성: oci os bucket create --name sangkwon-radar-raw --compartment-id <컴파트먼트OCID>
#
# cron 등록 예시 (매일 새벽 3시, 수집/가공 이후 실행):
#   0 3 * * * /home/opc/sangkwon-radar/scripts/backup_to_object_storage.sh >> /home/opc/sangkwon-radar/logs/backup.log 2>&1
# =========================================================

set -e

BUCKET_NAME="sangkwon-radar-raw"
RAW_DIR="$(cd "$(dirname "$0")/.." && pwd)/raw_data"
UPLOADED_LOG="$RAW_DIR/.uploaded_to_object_storage"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Object Storage 백업 시작: $RAW_DIR -> $BUCKET_NAME"

if [ ! -d "$RAW_DIR" ]; then
  echo "raw_data 디렉토리가 없습니다: $RAW_DIR"
  exit 1
fi

touch "$UPLOADED_LOG"
count=0

for f in "$RAW_DIR"/*.json "$RAW_DIR"/*.xml; do
  [ -e "$f" ] || continue
  fname=$(basename "$f")

  # 이미 업로드한 파일은 다시 올리지 않음 (raw_data는 append-only로 계속 쌓이는 구조라
  # 매번 전체를 다시 올리면 시간이 오래 걸리고 API 호출 낭비이므로, 업로드 이력을 남긴다)
  if grep -qxF "$fname" "$UPLOADED_LOG" 2>/dev/null; then
    continue
  fi

  oci os object put --bucket-name "$BUCKET_NAME" --file "$f" --name "raw/$fname" --force
  echo "  업로드 완료: $fname"
  echo "$fname" >> "$UPLOADED_LOG"
  count=$((count+1))
done

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 백업 완료 (신규 업로드 $count건)"
