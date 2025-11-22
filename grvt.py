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

        # selector 형식: instrument@rate-depth (depth: 10, 50, 100, 500)
        selector = f"{self.instrument}@500-50"
        sub_msg = {
            "jsonrpc": "2.0",
            "method": "subscribe",
            "params": {"stream": "v1.book.s", "selectors": [selector]},
            "id": 1
        }

        reconnect_count = 0
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                    await ws.send(json.dumps(sub_msg))
                    self.log.info(f"✅ GRVT 마켓 데이터 구독: {selector}")
                    reconnect_count = 0

                    async for raw in ws:
                        try:
                            msg = json.loads(raw)

                            # 에러 체크
                            if "error" in msg:
                                self.log.error(f"GRVT WS 에러: {msg['error']}")
                                continue

                            # 구독 응답 확인
                            if "result" in msg and msg.get("method") == "subscribe":
                                self.log.info(f"구독 확인: {msg['result'].get('subs', [])}")
                                continue

                            # 오더북 데이터
                            if msg.get("stream") == "v1.book.s":
                                feed = msg.get("feed", {})
                                if feed:
                                    self._update_orderbook(feed)

                        except json.JSONDecodeError as e:
                            self.log.warning(f"JSON 파싱 실패: {e}")

            except ConnectionClosed as e:
                self.log.warning(f"GRVT 마켓 데이터 연결 끊김: code={e.code} reason={e.reason}")
                reconnect_count += 1
            except Exception as e:
                self.log.error(f"GRVT 마켓 데이터 오류: {type(e).__name__}: {e}")
                reconnect_count += 1

            wait_time = min(60, 2 ** min(reconnect_count, 6))
            self.log.info(f"GRVT 마켓 데이터 재연결 대기 {wait_time}초...")
            await asyncio.sleep(wait_time)

    async def start_private_data(self):
        """포지션/체결 구독 (자동 재연결)"""
        url = "wss://trades.grvt.io/ws/full"
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
                # 쿠키 갱신
                if reconnect_count > 0 and reconnect_count % 3 == 0:
                    self.log.info("GRVT 재로그인 시도...")
                    try:
                        self.login()
                    except Exception as e:
                        self.log.error(f"재로그인 실패: {e}")

                # websockets 버전 호환성 처리
                headers = {"Cookie": self.cookie, "X-Grvt-Account-Id": self.api_key}

                async with websockets.connect(
                    url,
                    additional_headers=headers,  # websockets >= 10.0
                    ping_interval=20,
                    ping_timeout=10
                ) as ws:
                    for sub in subs:
                        await ws.send(json.dumps(sub))

                    self.log.info("✅ GRVT 프라이빗 데이터 구독")
                    reconnect_count = 0

                    async for raw in ws:
                        try:
                            msg = json.loads(raw)

                            # 에러 체크
                            if "error" in msg:
                                self.log.error(f"GRVT Private WS 에러: {msg['error']}")
                                continue

                            stream = msg.get("stream")

                            if stream == "v1.position":
                                self._update_position(msg.get("feed", {}))
                            elif stream == "v1.fill":
                                self._handle_fill(msg.get("feed", {}))

                        except json.JSONDecodeError as e:
                            self.log.warning(f"JSON 파싱 실패: {e}")

            except ConnectionClosed as e:
                self.log.warning(f"GRVT 프라이빗 데이터 연결 끊김: code={e.code} reason={e.reason}")
                reconnect_count += 1
            except TypeError as e:
                # websockets 버전 호환성 - extra_headers 시도
                if "additional_headers" in str(e) or "extra_headers" in str(e):
                    self.log.warning("websockets 버전 호환성 문제 감지, 대체 방식 시도")
                    try:
                        await self._start_private_data_legacy()
                        return
                    except Exception as e2:
                        self.log.error(f"레거시 방식도 실패: {e2}")
                else:
                    self.log.error(f"GRVT 프라이빗 데이터 오류: {type(e).__name__}: {e}")
                reconnect_count += 1
            except Exception as e:
                self.log.error(f"GRVT 프라이빗 데이터 오류: {type(e).__name__}: {e}")
                reconnect_count += 1

            wait_time = min(60, 2 ** min(reconnect_count, 6))
            self.log.info(f"GRVT 프라이빗 데이터 재연결 대기 {wait_time}초...")
            await asyncio.sleep(wait_time)

    async def _start_private_data_legacy(self):
        """레거시 websockets 호환 (extra_headers 사용)"""
        url = "wss://trades.grvt.io/ws/full"
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
                if reconnect_count > 0 and reconnect_count % 3 == 0:
                    try:
                        self.login()
                    except:
                        pass

                headers = [("Cookie", self.cookie), ("X-Grvt-Account-Id", self.api_key)]

                async with websockets.connect(
                    url,
                    extra_headers=headers,  # websockets < 10.0
                    ping_interval=20,
                    ping_timeout=10
                ) as ws:
                    for sub in subs:
                        await ws.send(json.dumps(sub))

                    self.log.info("✅ GRVT 프라이빗 데이터 구독 (레거시)")
                    reconnect_count = 0

                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                            stream = msg.get("stream")

                            if stream == "v1.position":
                                self._update_position(msg.get("feed", {}))
                            elif stream == "v1.fill":
                                self._handle_fill(msg.get("feed", {}))
                        except:
                            pass

            except Exception as e:
                self.log.error(f"레거시 프라이빗 오류: {e}")
                reconnect_count += 1

            await asyncio.sleep(min(60, 2 ** min(reconnect_count, 6)))

    def _update_orderbook(self, feed: Dict):
        """오더북 업데이트"""
        try:
            bids = feed.get("bids", [])
            asks = feed.get("asks", [])

            # 디버그: 실제 데이터 구조 확인
            if bids and not self.market_ready.is_set():
                self.log.info(f"🔍 오더북 구조: bids[0]={bids[0] if bids else 'empty'}")

            if bids:
                # 구조에 따라 파싱
                first_bid = bids[0]
                if isinstance(first_bid, dict):
                    # {"price": "123", "size": "456"} 형태
                    self.best_bid = float(first_bid.get("price") or first_bid.get("p", 0))
                elif isinstance(first_bid, (list, tuple)):
                    # [price, size] 형태
                    self.best_bid = float(first_bid[0])
                else:
                    # 단일 값?
                    self.best_bid = float(first_bid)

            if asks:
                first_ask = asks[0]
                if isinstance(first_ask, dict):
                    self.best_ask = float(first_ask.get("price") or first_ask.get("p", 0))
                elif isinstance(first_ask, (list, tuple)):
                    self.best_ask = float(first_ask[0])
                else:
                    self.best_ask = float(first_ask)

            if self.best_bid and self.best_ask:
                if not self.market_ready.is_set():
                    self.log.info(f"📊 호가: bid={self.best_bid:.2f} ask={self.best_ask:.2f}")
                    self.market_ready.set()

        except Exception as e:
            self.log.error(f"오더북 파싱 오류: {e}, feed={json.dumps(feed)[:500]}")

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
            from dataclasses import asdict
            from enum import Enum

            price = quantize(price, self.config.GRVT_TICK)
            qty = quantize(qty, self.config.GRVT_MIN_SIZE)

            if qty < self.config.GRVT_MIN_SIZE:
                self.log.warning(f"수량 부족: {qty} < {self.config.GRVT_MIN_SIZE}")
                return None

            # 간단한 숫자 ID 사용 (API 예제처럼)
            client_order_id = str(int(time.time() * 1000))
            now_ns = int(time.time() * 1e9)
            expiration_int = int((time.time() + 3600) * 1e9)

            leg = OrderLeg(
                instrument=self.instrument,
                size=str(qty),
                limit_price=str(price),
                is_buying_asset=(side == "buy")
            )

            dummy_sig = Signature(
                signer="",
                r="",
                s="",
                v=0,
                expiration=expiration_int,
                nonce=random.randint(1, 2**31 - 1)
            )

            order = Order(
                order_id=client_order_id,
                sub_account_id=self.sub,
                is_market=False,
                time_in_force=TimeInForce.GOOD_TILL_TIME,
                legs=[leg],
                metadata=OrderMetadata(
                    client_order_id=client_order_id,
                    create_time=str(now_ns)
                ),
                signature=dummy_sig,
                post_only=False,
                reduce_only=False
            )

            instruments_dict = {self.instrument: self.instrument_obj}

            signed_order = sign_order(
                order=order,
                config=self.sdk_cfg,
                account=self.acct,
                instruments=instruments_dict
            )

            # dict 변환
            try:
                order_dict = signed_order.model_dump()
            except AttributeError:
                order_dict = asdict(signed_order)

            # Enum을 문자열로 변환하고 None 제거
            def convert_and_clean(obj):
                if isinstance(obj, dict):
                    return {k: convert_and_clean(v) for k, v in obj.items() if v is not None}
                elif isinstance(obj, list):
                    return [convert_and_clean(item) for item in obj]
                elif isinstance(obj, Enum):
                    return obj.name
                else:
                    return obj

            order_dict = convert_and_clean(order_dict)

            # order_id 제거
            order_dict.pop("order_id", None)

            # signature 안의 expiration을 문자열로 변환
            if "signature" in order_dict:
                sig = order_dict["signature"]
                if "expiration" in sig and not isinstance(sig["expiration"], str):
                    sig["expiration"] = str(sig["expiration"])

            payload = {
                "order": order_dict
            }

            # 디버그
            self.log.info(f"🔍 payload: {json.dumps(payload)[:800]}")

            r = requests.post(
                "https://trades.grvt.io/full/v1/create_order",
                json=payload,
                headers={"Cookie": self.cookie, "X-Grvt-Account-Id": self.api_key},
                timeout=10
            )

            if r.status_code == 200:
                result = r.json()
                if "error" in result:
                    self.log.error(f"주문 API 에러: {result['error']}")
                    return None
                self.log.info(f"📝 GRVT 주문 전송: {side} {qty:.6f} @ {price:.2f}")
                return result.get("result", {}).get("order_id")
            else:
                self.log.error(f"주문 실패: {r.status_code} {r.text[:300]}")
                return None

        except Exception as e:
            self.log.error(f"주문 오류: {type(e).__name__}: {e}")
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
            await self.cancel_all_orders()
            await asyncio.sleep(1)

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

            order_id = await self.place_order(side, price, qty)
            if not order_id:
                return False

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

            await self.cancel_all_orders()
            await asyncio.sleep(1)

            side = "sell" if self.position > 0 else "buy"
            qty = abs(self.position)

            if side == "buy":
                price = self.best_ask if self.best_ask else None
            else:
                price = self.best_bid if self.best_bid else None

            if not price:
                self.log.warning("호가 정보 없음")
                return False

            if side == "buy":
                price = price * 1.001
            else:
                price = price * 0.999

            order_id = await self.place_order(side, price, qty)
            if not order_id:
                return False

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

        self.log.info("🧹 포지션 정리 중...")
        await self.grvt.close_position()
        await asyncio.sleep(3)

        while True:
            try:
                if self.active:
                    await asyncio.sleep(1)
                    continue

                if int(time.time()) % 30 == 0:
                    runtime = int(time.time() - self.start_time)
                    hours = runtime // 3600
                    minutes = (runtime % 3600) // 60
                    self.log.info(f"📊 상태: 사이클={self.cycle_count} 런타임={hours}h{minutes}m")

                if not self.grvt.best_bid or not self.grvt.best_ask:
                    await asyncio.sleep(1)
                    continue

                ref_price = self.grvt.best_ask
                qty = quantize(self.config.NOTIONAL_USD / ref_price, self.config.GRVT_MIN_SIZE)

                side = random.choice(["buy", "sell"])

                self.active = True
                self.cycle_count += 1

                self.log.info("=" * 70)
                self.log.info(f"🎬 사이클 #{self.cycle_count}: {side.upper()}")
                self.log.info(f"   수량: {qty:.6f} BTC @ {ref_price:.2f}")
                self.log.info("=" * 70)

                ok = await self.grvt.manage_position(side, qty)
                if not ok:
                    self.log.error("진입 실패")
                    self.active = False
                    continue

                hold_time = random.randint(self.config.POSITION_HOLD_MIN, self.config.POSITION_HOLD_MAX)
                self.log.info(f"⏳ {hold_time}초 홀드")
                await asyncio.sleep(hold_time)

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

    grvt = GRVT(setup_logger("GRVT"))
    grvt.login()
    grvt.fetch_instrument()

    asyncio.create_task(grvt.start_market_data())
    asyncio.create_task(grvt.start_private_data())

    engine = TradingEngine(grvt, setup_logger("ENGINE"))
    await engine.run()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n✋ 사용자 중단")
    except Exception as e:
        print(f"❌ 치명적 오류: {e}")
