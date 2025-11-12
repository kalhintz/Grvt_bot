# GRVT 트레이딩 봇

GRVT 거래소 전용 자동 트레이딩 봇

## 주요 기능

- WebSocket 기반 실시간 마켓 데이터 수신
- 자동 포지션 관리 (진입/청산)
- 자동 재연결 로직
- 에러 처리 및 로깅

## Requirements

```txt
aiohttp>=3.9.0
requests>=2.31.0
websockets>=12.0
python-dotenv>=1.0.0
eth-account>=0.11.0
```

## 설치

### 1. 저장소 클론
```bash
git clone https://github.com/kalhintz/Grvt_bot.git
cd [your-repo-name]
```

### 2. Python 환경 설정 (Python 3.10+ 권장)
```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
```

### 3. 의존성 설치
```bash
pip install -r requirements.txt
```

### 4. GRVT SDK 설치
```bash
# GRVT SDK 설치 (공식 문서 참고)
pip install grvt-pysdk  # 또는 GRVT에서 제공하는 설치 방법
```

### 5. 환경변수 설정
```bash
cp .env.example .env
# .env 파일을 열어서 본인의 API 키와 설정값으로 수정
```

**중요 환경변수:**
- `GRVT_API_KEY`: GRVT API 키
- `GRVT_PRIVATE_KEY`: GRVT 프라이빗 키 (0x 접두사 포함)
- `GRVT_TRADING_ACCOUNT_ID`: GRVT 트레이딩 계정 ID
- `GRVT_INSTRUMENT`: 거래 상품 (예: BTC_USDT_Perp)
- `NOTIONAL_USD`: 거래 금액 (USD)

### 6. 실행
```bash
python grvt_only.py
```

## 전략 파라미터

`.env` 파일에서 아래 파라미터를 조정할 수 있습니다:

- `NOTIONAL_USD`: 거래 금액 (기본값: 32000)
- `POSITION_HOLD_MIN_SEC`: 포지션 최소 홀드 시간 (기본값: 300초)
- `POSITION_HOLD_MAX_SEC`: 포지션 최대 홀드 시간 (기본값: 600초)
- `LOG_LEVEL`: 로그 레벨 (INFO, DEBUG, WARNING, ERROR)

## 주의사항

⚠️ **실제 자금을 사용하기 전에 반드시 테스트넷에서 충분히 테스트하세요**

- 현재 코드는 랜덤 방향 거래 예시입니다
- 실제 전략 로직을 구현해야 합니다
- 리스크 관리를 위한 추가 로직이 필요합니다

## 라이선스

MIT
