#!/bin/bash
# =========================================================
# VM(Oracle Linux 8)에서 최초 1회 실행하는 환경 설치 스크립트
# 사용법: bash setup.sh
# =========================================================
set -e

echo "[1/4] Python3 / pip 확인 및 설치"
if ! command -v python3 &> /dev/null; then
  sudo dnf install -y python3 python3-pip
fi
python3 --version

echo "[2/4] 방화벽 포트 확인 - 3000번은 Security List에 이미 열려있음 (추가 요청 불필요)"
sudo firewall-cmd --permanent --add-port=3000/tcp || true
sudo firewall-cmd --reload || true

echo "[3/4] Python 패키지 설치"
pip3 install --user -r requirements.txt

echo "[4/4] raw_data / logs 디렉토리 생성"
mkdir -p raw_data logs

echo ""
echo "설치 완료. 다음 순서로 진행하세요:"
echo "  0) Oracle XE 21c가 아직 설치 안 됐다면 README의 '설치 및 실행 방법' 참고"
echo "     (NLS_LANG=AMERICAN_AMERICA.AL32UTF8 환경변수 설정 필수 - 안 하면 한글 데이터가 깨짐)"
echo "  1) cp .env.example .env  # 값 채우기"
echo "     (.env는 python-dotenv가 각 스크립트 실행 시 자동으로 읽으므로 별도 source/export 불필요)"
echo "  2) sqlplus sangkwon/<비번>@localhost:1521/XEPDB1 @sql/schema.sql"
echo "  3) python3 scripts/collect.py --sample   # 키 없이 먼저 파이프라인 테스트"
echo "  4) python3 scripts/process.py"
echo "  5) python3 app/app.py   # http://<공인IP>:3000 접속"
echo "  6) crontab -e 로 run_pipeline.sh 등록하면 매일 자동 수집-가공-백업"
