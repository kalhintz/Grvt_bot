#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GRVT 트레이딩 봇
- WebSocket 재연결 로직 강화
- 에러 처리 개선
- 연결 상태 모니터링
"""

import os
import time
import json
import uuid
import random
import asyncio
import logging
from decimal import Decimal, ROUND_DOWN
from typing import Optional, Dict, Any
from datetime import datetime

import requests
import websockets
from websockets.exceptions import ConnectionClosed
from dotenv import load_dotenv

# ---- GRVT signing / SDK ----
from eth_account import Account
from pysdk.grvt_raw_signing import sign_order
from pysdk.grvt_raw_types import (
    Order, OrderLeg, Signature, OrderMetadata,
    TimeInForce, Instrument, Kind, InstrumentSettlementPeriod
)
from pysdk.grvt_raw_env import GrvtEnv
from pysdk.grvt_raw_base import GrvtApiConfig

# =========================================================
# 설정 및 유틸
# =========================================================

load_dotenv()

class Config:
    """중앙화된 설정 관리"""
    # 거래 설정
    NOTIONAL_USD = float(os.getenv("NOTIONAL_USD", "50000"))

    # 포지션 홀드
    POSITION_HOLD_MIN = int(os.getenv("POSITION_HOLD_MIN_SEC", "300"))
    POSITION_HOLD_MAX = int(os.getenv("POSITION_HOLD_MAX_SEC", "600"))

    # 주문 교체 타이밍
    ORDER_REPLACE_MIN = float(os.getenv("ORDER_REPLACE_MIN_SEC", "18"))
    ORDER_REPLACE_MAX = float(os.getenv("ORDER_REPLACE_MAX_SEC", "35"))

    # 시장 파라미터 (고정)
    GRVT_TICK = 0.1
    GRVT_MIN_SIZE = 0.001

    # 타임아웃
    ACK_TIMEOUT = 5.0
    STUCK_TIMEOUT = 15.0
    MAX_STALE_SEC = 60.0

    # 재연결 설정
    RECONNECT_DELAY = 5.0
    MAX_RECONNECT_ATTEMPTS = 10

def setup_logger(name: str) -> logging.Logger:
    lvl = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    log = logging.getLogger(name)
    if not log.handlers:
        log.setLevel(lvl)
        fmt = logging.Formatter('%(asctime)s [%(name)s] %(levelname)s: %(message)s', datefmt='%H:%M:%S')
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        log.addHandler(ch)
    return log

def quantize(value: float, tick: float) -> float:
    """가격/수량 정량화"""
    if tick <= 0:
        return value
    q = Decimal(str(value)) / Decimal(str(tick))
    return float(q.to_integral_value(rounding=ROUND_DOWN) * Decimal(str(tick)))

# =========================================================
# GRVT
# =========================================================

class GRVT:
    def __init__(self, log: logging.Logger):
        self.log = log
        self.config = Config()

        # 환경변수
        self.api_key = os.getenv("GRVT_API_KEY", "").strip()
        self.private_key = os.getenv("GRVT_PRIVATE_KEY", "").strip()
        self.sub = os.getenv("GRVT_TRADING_ACCOUNT_ID", "").strip()
        self.instrument = os.getenv("GRVT_INSTRUMENT", "BTC_USDT_Perp").strip()

        if not all([self.api_key, self.private_key, self.sub]):
            raise RuntimeError("GRVT 환경변수 누락")

        # 상태
        self.cookie = ""
        self.instrument_obj = None
        self.position = 0.0
        self.best_bid = None
        self.best_ask = None
        self.market_ready = asyncio.Event()
        self.last_order_error_time = 0

        # SDK 설정
        pk_hex = self.private_key if self.private_key.startswith("0x") else "0x" + self.private_key
        self.acct = Account.from_key(pk_hex)
        self.sdk_cfg = GrvtApiConfig(
            env=GrvtEnv.PROD,
            private_key=pk_hex,
            trading_account_id=self.sub,
            api_key=self.api_key,
            logger=None
        )

    def login(self):
        """GRVT 로그인 (재시도 로직 포함)"""
        for attempt in range(3):
            try:
                r = requests.post(
                    "https://edge.grvt.io/auth/api_key/login",
                    json={"api_key": self.api_key},
                    timeout=10
                )
                r.raise_for_status()
                ck = r.headers.get("Set-Cookie", "")
                if "gravity=" in ck:
                    self.cookie = "gravity=" + ck.split("gravity=")[1].split(";")[0]
                    self.log.info("✅ GRVT 로그인 완료")
                    return
            except Exception as e:
                self.log.error(f"GRVT 로그인 실패 (시도 {attempt+1}/3): {e}")
                if attempt < 2:
                    time.sleep(2)

        raise RuntimeError("GRVT 로그인 최종 실패")

    def fetch_instrument(self):
        """시장 정보 조회"""
        r = requests.post(
            "https://market-data.grvt.io/full/v1/instrument",
            json={"instrument": self.instrument},
            timeout=8
        )
        r.raise_for_status()
        data = r.json()["result"]

        self.instrument_obj = Instrument(
            instrument=data["instrument"],
            instrument_hash=data["instrument_hash"],
            base=data["base"], quote=data["quote"],
            kind=Kind.PERPETUAL, venues=[],
            settlement_period=InstrumentSettlementPeriod.DAILY,
            tick_size=data["tick_size"],
            min_size=data["min_size"],
            create_time=data["create_time"],
            base_decimals=data["base_decimals"],
            quote_decimals=data["quote_decimals"],
            max_position_size=data.get("max_position_size", "0")
        )
        self.log.info(f"시장 정보: tick={self.config.GRVT_TICK} min_size={self.config.GRVT_MIN_SIZE}")

    async def start_market_data(self):
        """마켓 데이터 구독 (자동 재연결)"""
        url = "wss://market-data.grvt.io/ws/full"
        sub_msg = {
            "jsonrpc": "2.0",
            "method": "subscribe",
            "params": {"stream": "v1.book.s", "selectors": [f"{self.instrument}@500-10"]},
            "id": 1
        }

        reconnect_count = 0
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                    await ws.send(json.dumps(sub_msg))
                    self.log.info("✅ GRVT 마켓 데이터 구독")
                    reconnect_count = 0  # 성공 시 카운터 리셋

                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("stream") == "v1.book.s":
                            self._update_orderbook(msg.get("feed", {}))

            except ConnectionClosed as e:
                self.log.warning(f"GRVT 마켓 데이터 연결 끊김: {e}")
                reconnect_count += 1
            except Exception as e:
                self.log.error(f"GRVT 마켓 데이터 오류: {e}")
                reconnect_count += 1

            # 재연결 대기 (지수 백오프)
            wait_time = min(60, 2 ** min(reconnect_count, 6))
            self.log.info(f"GRVT 마켓 데이터 재연결 대기 {wait_time}초...")
            await asyncio.sleep(wait_time)

    async def start_private_data(self):
        """포지션/체결 구독 (자동 재연결)"""
        url = "wss://trades.grvt.io/ws/full"
        headers = [("Cookie", self.cookie), ("X-Grvt-Account-Id", self.api_key)]
        selector = f"{self.sub}-{self.instrument}"

        subs = [
            {"jsonrpc": "2.0", "method": "subscribe",
             "params": {"stream": "v1.position", "selectors": [selector]}, "id": 101},
            {"jsonrpc": "2.0", "method": "subscribe",
             "params": {"stream": "v1.fill", "selectors": [selector]}, "id": 102}
        ]

        reconnect_count = 0
        while True:
            try:
                # 쿠키 갱신이 필요할 수 있음
                if reconnect_count > 0 and reconnect_count % 3 == 0:
                    self.log.info("GRVT 재로그인 시도...")
                    try:
                        self.login()
                        headers = [("Cookie", self.cookie), ("X-Grvt-Account-Id", self.api_key)]
                    except Exception as e:
                        self.log.error(f"재로그인 실패: {e}")

                async with websockets.connect(url, extra_headers=headers,
                                             ping_interval=20, ping_timeout=10) as ws:
                    for sub in subs:
                        await ws.send(json.dumps(sub))

                    self.log.info("✅ GRVT 프라이빗 데이터 구독")
                    reconnect_count = 0

                    async for raw in ws:
                        msg = json.loads(raw)
                        stream = msg.get("stream")

                        if stream == "v1.position":
                            self._update_position(msg.get("feed", {}))
                        elif stream == "v1.fill":
                            self._handle_fill(msg.get("feed", {}))

            except ConnectionClosed as e:
                self.log.warning(f"GRVT 프라이빗 데이터 연결 끊김: {e}")
                reconnect_count += 1
            except Exception as e:
                self.log.error(f"GRVT 프라이빗 데이터 오류: {e}")
                reconnect_count += 1

            wait_time = min(60, 2 ** min(reconnect_count, 6))
            self.log.info(f"GRVT 프라이빗 데이터 재연결 대기 {wait_time}초...")
            await asyncio.sleep(wait_time)

    def _update_orderbook(self, feed: Dict):
        """오더북 업데이트"""
        bids = feed.get("bids", [])
        asks = feed.get("asks", [])

        if bids:
            self.best_bid = float(bids[0][0])
        if asks:
            self.best_ask = float(asks[0][0])

        if self.best_bid and self.best_ask and not self.market_ready.is_set():
            self.market_ready.set()

    def _update_position(self, feed: Dict):
        """포지션 업데이트"""
        size = float(feed.get("size", 0))
        self.position = size
        self.log.debug(f"포지션 업데이트: {size:.6f}")

    def _handle_fill(self, feed: Dict):
        """체결 처리"""
        side = feed.get("side", "")
        size = float(feed.get("size", 0))
        price = float(feed.get("price", 0))
        self.log.info(f"✅ GRVT 체결: {side} {size:.6f} @ {price:.2f}")

    async def place_order(self, side: str, price: float, qty: float) -> Optional[str]:
        """주문 전송"""
        try:
            # 가격/수량 정량화
            price = quantize(price, self.config.GRVT_TICK)
            qty = quantize(qty, self.config.GRVT_MIN_SIZE)

            if qty < self.config.GRVT_MIN_SIZE:
                self.log.warning(f"수량 부족: {qty} < {self.config.GRVT_MIN_SIZE}")
                return None

            # 주문 생성
            leg = OrderLeg(
                instrument=self.instrument_obj.instrument,
                size=str(qty),
                limit_price=str(price),
                is_buying_asset=(side == "buy")
            )

            order_id = str(uuid.uuid4())
            order = Order(
                order_id=order_id,
                sub_account_id=self.sub,
                is_market=False,
                time_in_force=TimeInForce.GOOD_TILL_TIME,
                legs=[leg],
                metadata=OrderMetadata(
                    client_order_id=order_id,
                    create_time=str(int(time.time() * 1e6))
                ),
                post_only=False,
                reduce_only=False
            )

            # 서명
            signature = sign_order(
                order=order,
                private_key=self.sdk_cfg.private_key,
                is_market=False
            )

            # API 요청
            payload = {
                "order": order.model_dump(),
                "signature": signature.model_dump()
            }

            r = requests.post(
                "https://trades.grvt.io/full/v1/create_order",
                json=payload,
                headers={"Cookie": self.cookie, "X-Grvt-Account-Id": self.api_key},
                timeout=10
            )

            if r.status_code == 200:
                result = r.json().get("result", {})
                self.log.info(f"📝 GRVT 주문 전송: {side} {qty:.6f} @ {price:.2f}")
                return result.get("order_id")
            else:
                self.log.error(f"주문 실패: {r.status_code} {r.text}")
                return None

        except Exception as e:
            self.log.error(f"주문 오류: {e}")
            return None

    async def cancel_all_orders(self):
        """모든 주문 취소"""
        try:
            r = requests.post(
                "https://trades.grvt.io/full/v1/cancel_all_orders",
                json={"sub_account_id": self.sub, "instrument": self.instrument},
                headers={"Cookie": self.cookie, "X-Grvt-Account-Id": self.api_key},
                timeout=10
            )

            if r.status_code == 200:
                self.log.info("🗑️ 모든 주문 취소 완료")
            else:
                self.log.warning(f"주문 취소 실패: {r.status_code}")

        except Exception as e:
            self.log.error(f"주문 취소 오류: {e}")

    async def manage_position(self, side: str, qty: float) -> bool:
        """포지션 관리 (진입)"""
        try:
            # 기존 주문 취소
            await self.cancel_all_orders()
            await asyncio.sleep(1)

            # 가격 결정
            if side == "buy":
                price = self.best_ask if self.best_ask else None
            else:
                price = self.best_bid if self.best_bid else None

            if not price:
                self.log.warning("호가 정보 없음")
                return False

            # 공격적 가격 (즉시 체결 유도)
            if side == "buy":
                price = price * 1.001
            else:
                price = price * 0.999

            # 주문 전송
            order_id = await self.place_order(side, price, qty)
            if not order_id:
                return False

            # 체결 대기 (최대 30초)
            target_pos = qty if side == "buy" else -qty
            for _ in range(30):
                await asyncio.sleep(1)
                if abs(abs(self.position) - abs(target_pos)) < self.config.GRVT_MIN_SIZE:
                    self.log.info(f"✅ GRVT 진입 완료: {self.position:.6f}")
                    return True

            self.log.warning("⚠️ GRVT 진입 타임아웃")
            await self.cancel_all_orders()
            return False

        except Exception as e:
            self.log.error(f"포지션 관리 오류: {e}")
            return False

    async def close_position(self) -> bool:
        """포지션 청산"""
        try:
            if abs(self.position) < self.config.GRVT_MIN_SIZE:
                self.log.info("청산할 포지션 없음")
                return True

            # 기존 주문 취소
            await self.cancel_all_orders()
            await asyncio.sleep(1)

            # 청산 방향/수량
            side = "sell" if self.position > 0 else "buy"
            qty = abs(self.position)

            # 가격 결정
            if side == "buy":
                price = self.best_ask if self.best_ask else None
            else:
                price = self.best_bid if self.best_bid else None

            if not price:
                self.log.warning("호가 정보 없음")
                return False

            # 공격적 가격
            if side == "buy":
                price = price * 1.001
            else:
                price = price * 0.999

            # 청산 주문
            order_id = await self.place_order(side, price, qty)
            if not order_id:
                return False

            # 체결 대기
            for _ in range(30):
                await asyncio.sleep(1)
                if abs(self.position) < self.config.GRVT_MIN_SIZE:
                    self.log.info("✅ GRVT 청산 완료")
                    return True

            self.log.warning("⚠️ GRVT 청산 타임아웃")
            await self.cancel_all_orders()
            return False

        except Exception as e:
            self.log.error(f"청산 오류: {e}")
            return False

# =========================================================
# 트레이딩 엔진
# =========================================================

class TradingEngine:
    def __init__(self, grvt: GRVT, log: logging.Logger):
        self.grvt = grvt
        self.log = log
        self.config = Config()
        self.active = False
        self.cycle_count = 0
        self.start_time = time.time()

    async def run(self):
        """메인 루프"""
        await self.grvt.market_ready.wait()
        self.log.info("✅ 시장 준비 완료")

        # 초기 정리
        self.log.info("🧹 포지션 정리 중...")
        await self.grvt.close_position()
        await asyncio.sleep(3)

        while True:
            try:
                if self.active:
                    await asyncio.sleep(1)
                    continue

                # 주기적 상태 로그
                if int(time.time()) % 30 == 0:
                    runtime = int(time.time() - self.start_time)
                    hours = runtime // 3600
                    minutes = (runtime % 3600) // 60
                    self.log.info(f"📊 상태: 사이클={self.cycle_count} 런타임={hours}h{minutes}m")

                # 거래 로직 (예시: 단순히 포지션 열고 닫기)
                if not self.grvt.best_bid or not self.grvt.best_ask:
                    await asyncio.sleep(1)
                    continue

                # 거래 수량 계산
                ref_price = self.grvt.best_ask
                qty = quantize(self.config.NOTIONAL_USD / ref_price, self.config.GRVT_MIN_SIZE)

                # 랜덤 방향 선택 (실제 전략으로 교체 필요)
                side = random.choice(["buy", "sell"])

                self.active = True
                self.cycle_count += 1

                self.log.info("=" * 70)
                self.log.info(f"🎬 사이클 #{self.cycle_count}: {side.upper()}")
                self.log.info(f"   수량: {qty:.6f} BTC @ {ref_price:.2f}")
                self.log.info("=" * 70)

                # 진입
                ok = await self.grvt.manage_position(side, qty)
                if not ok:
                    self.log.error("진입 실패")
                    self.active = False
                    continue

                # 홀드
                hold_time = random.randint(self.config.POSITION_HOLD_MIN, self.config.POSITION_HOLD_MAX)
                self.log.info(f"⏳ {hold_time}초 홀드")
                await asyncio.sleep(hold_time)

                # 청산
                self.log.info("🔚 포지션 청산 시작")
                await self.grvt.close_position()

                self.log.info(f"✅ 사이클 #{self.cycle_count} 완료")
                self.active = False

            except Exception as e:
                self.log.error(f"사이클 오류: {e}", exc_info=True)
                self.active = False
                await asyncio.sleep(5)

# =========================================================
# 메인
# =========================================================

async def main():
    log = setup_logger("MAIN")
    config = Config()

    log.info("=" * 70)
    log.info("🚀 GRVT 트레이딩 봇")
    log.info("=" * 70)
    log.info(f"📍 NOTIONAL: ${config.NOTIONAL_USD:,.0f}")
    log.info(f"📍 홀드 시간: {config.POSITION_HOLD_MIN}~{config.POSITION_HOLD_MAX}초")
    log.info(f"📍 시작 시간: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("=" * 70)

    # GRVT 초기화
    grvt = GRVT(setup_logger("GRVT"))
    grvt.login()
    grvt.fetch_instrument()

    # 비동기 태스크 시작
    asyncio.create_task(grvt.start_market_data())
    asyncio.create_task(grvt.start_private_data())

    # 트레이딩 엔진 실행
    engine = TradingEngine(grvt, setup_logger("ENGINE"))
    await engine.run()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n✋ 사용자 중단")
    except Exception as e:
        print(f"❌ 치명적 오류: {e}")
