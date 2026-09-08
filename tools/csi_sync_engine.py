#!/usr/bin/env python3
# -*-coding:utf-8-*-
"""
csi_sync_engine.py — 2Tx-1Rx CSI 스트림 동기화 엔진

핵심 원칙:
  - 동기화 기준 시각은 오직 하나: Rx ESP32의 Wi-Fi 하드웨어 수신 타임스탬프
    (CSV의 local_timestamp 컬럼, 패킷이 Rx 안테나에 도달한 순간 찍히는 us 카운터).
    두 Tx 스트림 모두 같은 Rx 클럭으로 찍히므로 기기 간 클럭 보정이 필요 없다.
  - 이 엔진은 스트리밍(인과적)으로 동작한다. 수집기가 CSV를 저장할 때도,
    나중에 real-time 추론을 붙일 때도 "같은 클래스"를 그대로 쓴다.
    → 오프라인 데이터와 실시간 입력이 구조적으로 동일해진다.

동작 규칙:
  1. 첫 패킷의 타임스탬프를 t0로 잡고 주기 T(기본 1/30초)의 슬롯 격자를 만든다.
  2. 패킷은 자기 타임스탬프로 슬롯에 배정된다. 같은 슬롯에 같은 Tx 패킷이
     여러 개면 슬롯 중심에 가까운 것을 남긴다.
  3. 슬롯 종료 + guard(반주기)를 넘는 타임스탬프가 관측되면 슬롯을 확정(방출)한다.
     비어 있는 슬롯도 빈 채로 방출해 출력이 항상 균일한 30Hz 격자가 되게 한다.
  4. Rx 타임스탬프는 32비트 us라 약 71분마다 랩어라운드 → 자동 unwrap.
"""

import struct
from dataclasses import dataclass, field
from typing import Optional


# ──────────────────────────────────────────────
# 시리얼 한 줄 → 패킷 dict 파싱 (수집기/오프라인 공용)
# 컬럼: 0=type, 2=seq, 3=mac, 4=rssi, 19=local_timestamp, 25=data("[...]")
# ──────────────────────────────────────────────
def parse_csi_line(line: str, pc_time: float) -> Optional[dict]:
    """CSI_DATA 줄 하나를 파싱. 깨진 줄이면 None."""
    if not line.startswith('CSI_DATA'):
        return None

    cols = line.split(',')
    if len(cols) < 26:
        return None

    # 한 줄에 두 프레임이 붙은 깨진 행 필터 (이전 프로젝트에서 관측된 패턴)
    if any('CSI_DATA' in c for c in cols[1:]):
        return None

    try:
        seq = int(cols[2])
        mac = cols[3]
        rssi = int(cols[4])
        ts_us = int(cols[19])
    except (ValueError, IndexError):
        return None

    if rssi > 0 or rssi < -100:
        return None

    # 타임스탬프는 32bit us 카운터 — 자릿수가 깨진(합쳐진) 행 방어
    if not (0 <= ts_us < 2**32):
        return None

    # Tx 번호 = 송신 MAC 마지막 바이트 (1a:00:00:00:00:XX)
    try:
        tx_id = int(mac.strip().split(':')[-1], 16)
    except ValueError:
        return None

    return {
        'tx_id': tx_id,
        'seq': seq,
        'rssi': rssi,
        'ts_us': ts_us,          # Rx 하드웨어 타임스탬프 (32bit raw)
        'pc_time': pc_time,      # 라벨링/디버깅용 (동기화에는 쓰지 않음)
        'raw_cols': cols,        # 원본 행 전체 (raw CSV 저장용)
    }


# ──────────────────────────────────────────────
# 바이너리 프레임 디코더
#
# Rx 펌웨어(csi_recv)는 텍스트 CSV 대신 바이너리 프레임을 보낸다
# (USB-Serial/JTAG 실질 처리량 ~100KB/s에 3Tx x 33Hz 텍스트가 안 들어가서).
#   프레임: [0xA5][0x5A][ver=0x01][payload_len u16 LE][meta 46B + csi int8들][xor u8]
# 디코더는 프레임을 "기존 텍스트 CSV 줄과 100% 동일한 문자열"로 복원한 뒤
# parse_csi_line()을 그대로 통과시킨다 → raw CSV 포맷, 동기화 엔진, 기존
# 진단도구가 전부 무변경으로 동작한다.
# 펌웨어의 csi_bin_meta_t 구조 변경 시 _BIN_META를 반드시 함께 수정할 것.
# ──────────────────────────────────────────────
BIN_MAGIC = b'\xa5\x5a'
BIN_VER = 0x01
_BIN_META = struct.Struct('<6s6sIIbBBBBBBBBBBbBBBBHBBfH')
_BIN_META_SIZE = _BIN_META.size   # 46
_BIN_MAX_CSI = 512

# 펌웨어가 텍스트로 출력하던 시절의 헤더 (raw CSV 첫 줄용)
RAW_CSV_HEADER = ("type,recv_mac,id,mac,rssi,rate,sig_mode,mcs,bandwidth,smoothing,"
                  "not_sounding,aggregation,stbc,fec_coding,sgi,noise_floor,ampdu_cnt,"
                  "channel,secondary_channel,local_timestamp,ant,sig_len,rx_state,"
                  "len,first_word,data").split(',')


def _mac_str(b: bytes) -> str:
    return ':'.join(f'{x:02x}' for x in b)


class CsiBinaryFramer:
    """바이트 스트림 → 패킷 dict (parse_csi_line 결과와 동일한 형태).

    텍스트 줄(부팅 로그, Stats 등)이 스트림에 섞여 있어도 매직 스캔으로
    조용히 건너뛴다. 깨진 프레임은 체크섬으로 걸러지고 resync 카운트만 올라간다.
    """

    def __init__(self):
        self._buf = bytearray()
        self.stats = {'frames': 0, 'resync': 0}

    def feed(self, data: bytes, pc_time: float) -> list:
        if data:
            self._buf += data
        out = []
        while True:
            i = self._buf.find(BIN_MAGIC)
            if i < 0:
                # 매직 없음 — 마지막 1바이트만 남기고 버림 (경계에 걸친 매직 대비)
                if len(self._buf) > 1:
                    del self._buf[:-1]
                break
            if i > 0:
                del self._buf[:i]
            if len(self._buf) < 5:
                break
            ver = self._buf[2]
            plen = self._buf[3] | (self._buf[4] << 8)
            if ver != BIN_VER or plen < _BIN_META_SIZE or plen > _BIN_META_SIZE + _BIN_MAX_CSI:
                self.stats['resync'] += 1
                del self._buf[:2]        # 가짜 매직 — 2바이트 버리고 재탐색
                continue
            if len(self._buf) < 5 + plen + 1:
                break                     # 프레임 미완 — 다음 feed 대기
            payload = bytes(self._buf[5:5 + plen])
            crc = self._buf[5 + plen]
            x = 0
            for b in payload:
                x ^= b
            if x != crc:
                self.stats['resync'] += 1
                del self._buf[:2]
                continue
            del self._buf[:5 + plen + 1]
            pkt = self._decode(payload, pc_time)
            if pkt is not None:
                self.stats['frames'] += 1
                out.append(pkt)
        return out

    def _decode(self, payload: bytes, pc_time: float):
        (recv_mac, src_mac, seq, timestamp, rssi, rate, sig_mode, mcs, cwb,
         smoothing, not_sounding, aggregation, stbc, fec_coding, sgi,
         noise_floor, ampdu_cnt, channel, secondary_channel, ant,
         sig_len, rx_state, first_word, gain, csi_len) = _BIN_META.unpack_from(payload)

        csi_bytes = payload[_BIN_META_SIZE:_BIN_META_SIZE + csi_len]
        if len(csi_bytes) != csi_len:
            return None
        vals = struct.unpack(f'<{csi_len}b', csi_bytes)
        if gain != 1.0:
            # 펌웨어 텍스트 시절의 (int16_t)(gain * v) 캐스팅(0방향 절삭)과 동일하게
            vals = [int(gain * v) for v in vals]

        # 기존 텍스트 CSV 줄과 동일한 문자열로 복원 → 기존 파서/포맷 재사용
        line = (f"CSI_DATA,{_mac_str(recv_mac)},{seq},{_mac_str(src_mac)},"
                f"{rssi},{rate},{sig_mode},{mcs},{cwb},{smoothing},{not_sounding},"
                f"{aggregation},{stbc},{fec_coding},{sgi},{noise_floor},{ampdu_cnt},"
                f"{channel},{secondary_channel},{timestamp},{ant},{sig_len},{rx_state},"
                f"{csi_len},{first_word},\"[" + ",".join(map(str, vals)) + "]\"")
        return parse_csi_line(line, pc_time)


# ──────────────────────────────────────────────
# 방출되는 동기화 샘플 1개 = 격자 슬롯 1칸
# ──────────────────────────────────────────────
@dataclass
class SyncedSample:
    slot: int                    # 슬롯 인덱스 (t0부터 0, 1, 2, ...)
    slot_time_us: int            # 슬롯 시작 시각 (unwrap된 Rx 클럭, us)
    pkts: dict = field(default_factory=dict)   # {tx_id: pkt or None}

    def fill_count(self):
        return sum(1 for p in self.pkts.values() if p is not None)


class CsiSyncEngine:
    # UART 노이즈로 숫자가 깨진 타임스탬프 1개가 격자를 오염시키는 것을 막는 방어선.
    # 직전 watermark에서 이보다 크게 벗어난 패킷은 깨진 행으로 보고 폐기한다.
    MAX_TS_JUMP_US = 5_000_000
    # 격자 기준점은 첫 패킷 하나가 아니라, 처음 몇 개의 다수 일치 클러스터로 잡는다
    # (깨진 줄이 첫 패킷으로 들어와 기준점을 오염시키는 것 방지)
    WARMUP_PKTS = 5
    WARMUP_SPAN_US = 1_000_000
    # 연속 이상치가 이만큼 쌓이면 "기준점이 틀렸다"고 보고 실제 스트림 위치로 재앵커
    MAX_CONSEC_OUTLIERS = 20

    def __init__(self, grid_hz: float = 30.0, tx_ids=(1, 2), guard_ratio: float = 0.5):
        self.period_us = 1_000_000.0 / grid_hz
        self.tx_ids = tuple(tx_ids)
        self.guard_us = self.period_us * guard_ratio

        # 타임스탬프 unwrap 상태
        self._last_raw_ts = None
        self._ts_offset = 0          # 랩어라운드 누적 보정치 (2^32 단위)

        self._t0 = None              # 격자 원점 (워밍업 클러스터의 최소 타임스탬프)
        self._watermark = None       # 지금까지 관측된 최대 unwrap 타임스탬프
        self._pending = {}           # {slot_idx: {tx_id: pkt}}
        self._next_emit = 0          # 다음에 방출할 슬롯 인덱스
        self._warmup = []            # 앵커 확정 전 패킷 버퍼
        self._consec_outliers = 0    # 연속 이상치 카운터 (재앵커 판단용)

        # 통계
        self.stats = {
            'pkts': {t: 0 for t in self.tx_ids},
            'dup_in_slot': 0,
            'ts_outlier': 0,
            'warmup_drop': 0,
            'reanchor': 0,
            'late_drop': 0,
            'slots_emitted': 0,
            'slots_full': 0,
            'slots_partial': 0,
            'slots_empty': 0,
        }

    # ── 내부: 32bit us 랩어라운드 풀기 (상태 변경 없이 후보값만 계산) ──
    def _unwrap_candidate(self, raw_ts: int):
        wrapped = (self._last_raw_ts is not None
                   and raw_ts < self._last_raw_ts - 2**31)
        offset = self._ts_offset + (2**32 if wrapped else 0)
        return raw_ts + offset, wrapped

    # ── 워밍업 버퍼에서 다수 일치 클러스터로 격자 원점을 확정 ──
    def _anchor_from_warmup(self) -> list:
        pkts = self._warmup
        self._warmup = []
        ts_sorted = sorted(p['ts_us'] for p in pkts)
        median = ts_sorted[len(ts_sorted) // 2]
        cluster = [p for p in pkts if abs(p['ts_us'] - median) <= self.WARMUP_SPAN_US]
        self.stats['warmup_drop'] += len(pkts) - len(cluster)

        self._t0 = min(p['ts_us'] for p in cluster)
        self._watermark = self._t0
        self._last_raw_ts = self._t0

        out = []
        for p in sorted(cluster, key=lambda x: x['ts_us']):
            out += self._feed_anchored(p)
        return out

    # ── 패킷 1개 투입 → 확정된 샘플 리스트 반환 ──
    def feed(self, pkt: dict) -> list:
        if pkt['tx_id'] not in self.stats['pkts']:
            return []   # 알 수 없는 Tx는 무시

        if self._t0 is None:
            self._warmup.append(dict(pkt))
            if len(self._warmup) < self.WARMUP_PKTS:
                return []
            return self._anchor_from_warmup()

        return self._feed_anchored(pkt)

    def _feed_anchored(self, pkt: dict) -> list:
        ts, wrapped = self._unwrap_candidate(pkt['ts_us'])

        # 이상치 방어: watermark에서 ±MAX_TS_JUMP_US를 벗어나면 깨진 행으로 폐기.
        # unwrap 상태(_last_raw_ts/_ts_offset)를 갱신하기 전에 검사해야
        # 깨진 값 하나가 이후 랩어라운드 판정까지 오염시키지 않는다.
        if abs(ts - self._watermark) > self.MAX_TS_JUMP_US:
            self.stats['ts_outlier'] += 1
            self._consec_outliers += 1
            # 연속 이상치가 임계를 넘으면 기준점 쪽이 오염된 것 → 현 스트림 위치로 재앵커.
            # 슬롯 번호 연속성을 위해 t0를 "다음 방출 슬롯 = 현재 위치"가 되도록 재설정한다.
            if self._consec_outliers >= self.MAX_CONSEC_OUTLIERS:
                self.stats['reanchor'] += 1
                self._consec_outliers = 0
                self._pending.clear()          # 오염된 기준의 미확정 슬롯 폐기
                self._ts_offset = 0
                self._last_raw_ts = pkt['ts_us']
                self._t0 = pkt['ts_us'] - self._next_emit * self.period_us
                self._watermark = pkt['ts_us']
                ts, wrapped = pkt['ts_us'], False
            else:
                return []

        self._consec_outliers = 0
        if wrapped:
            self._ts_offset += 2**32
        self._last_raw_ts = pkt['ts_us']

        pkt = dict(pkt)
        pkt['ts_unwrapped'] = ts

        self.stats['pkts'][pkt['tx_id']] += 1

        slot = int((ts - self._t0) // self.period_us)
        if slot < self._next_emit:
            # 이미 확정되어 방출된 슬롯에 뒤늦게 도착 → 폐기 (스트림이 단조라 거의 없음)
            self.stats['late_drop'] += 1
            return []

        bucket = self._pending.setdefault(slot, {})
        prev = bucket.get(pkt['tx_id'])
        if prev is None:
            bucket[pkt['tx_id']] = pkt
        else:
            # 같은 슬롯·같은 Tx 중복 → 슬롯 중심에 가까운 쪽 유지
            self.stats['dup_in_slot'] += 1
            center = self._t0 + (slot + 0.5) * self.period_us
            if abs(ts - center) < abs(prev['ts_unwrapped'] - center):
                bucket[pkt['tx_id']] = pkt

        if ts > self._watermark:
            self._watermark = ts

        return self._emit_ready()

    # ── watermark 기준으로 확정 가능한 슬롯을 순서대로 방출 ──
    def _emit_ready(self) -> list:
        out = []
        while True:
            slot_end = self._t0 + (self._next_emit + 1) * self.period_us
            if self._watermark < slot_end + self.guard_us:
                break
            out.append(self._make_sample(self._next_emit))
            self._next_emit += 1
        return out

    def _make_sample(self, slot: int) -> SyncedSample:
        bucket = self._pending.pop(slot, {})
        s = SyncedSample(
            slot=slot,
            slot_time_us=int(self._t0 + slot * self.period_us),
            pkts={t: bucket.get(t) for t in self.tx_ids},
        )
        n = s.fill_count()
        self.stats['slots_emitted'] += 1
        if n == len(self.tx_ids):
            self.stats['slots_full'] += 1
        elif n == 0:
            self.stats['slots_empty'] += 1
        else:
            self.stats['slots_partial'] += 1
        return s

    # ── 패킷이 끊겼을 때 벽시계로 방출 (실시간 지연 상한 보장) ──
    # gap_wall_s: 마지막 패킷 이후 흐른 벽시계 초.
    # 슬롯 하나(33ms)보다 훨씬 큰 gap이면 이후 패킷은 반드시 더 뒤 슬롯이므로 안전.
    def flush_stale(self, gap_wall_s: float) -> list:
        if self._t0 is None or gap_wall_s < 0.3:
            return []
        est_now = self._watermark + gap_wall_s * 1_000_000.0
        out = []
        while True:
            slot_end = self._t0 + (self._next_emit + 1) * self.period_us
            if est_now < slot_end + self.guard_us:
                break
            out.append(self._make_sample(self._next_emit))
            self._next_emit += 1
        return out

    # ── 수집 종료 시 남은 슬롯 전부 방출 ──
    def finalize(self) -> list:
        out = []
        if self._t0 is None:
            if not self._warmup:
                return []
            out += self._anchor_from_warmup()   # 워밍업 미완이면 있는 것으로 앵커
        last = max(self._pending.keys(), default=self._next_emit - 1)
        while self._next_emit <= last:
            out.append(self._make_sample(self._next_emit))
            self._next_emit += 1
        return out


# ──────────────────────────────────────────────
# 동기화 샘플 → CSV 행 (수집기와 오프라인 재처리가 같은 포맷을 쓴다)
# ──────────────────────────────────────────────
SYNCED_CSV_HEADER = None  # build_synced_header()로 생성


def build_synced_header(tx_ids=(1, 2)) -> list:
    header = ['slot', 'slot_time_us']
    for t in tx_ids:
        header += [f'tx{t}', f'tx{t}_seq', f'tx{t}_rssi', f'tx{t}_ts_us', f'tx{t}_pc_time', f'tx{t}_data']
    return header


def synced_sample_to_row(sample: SyncedSample, tx_ids=(1, 2)) -> list:
    row = [sample.slot, sample.slot_time_us]
    for t in tx_ids:
        p = sample.pkts.get(t)
        if p is None:
            row += [0, '', '', '', '', '']
        else:
            # data 필드("[q,i,q,i,...]")는 따옴표 안에 콤마가 있어 split 시
            # 여러 컬럼으로 쪼개져 있음 → 다시 합쳐 하나의 필드로 저장
            data = ','.join(p['raw_cols'][25:]).strip().strip('"')
            row += [1, p['seq'], p['rssi'], p['ts_unwrapped'],
                    f"{p['pc_time']:.6f}", data]
    return row


# ──────────────────────────────────────────────
# WIRELESS-BACKHAUL (1Tx-NRx convergecast) 동기화
#
# 구조가 CsiSyncEngine과 반대다: Tx가 여러 개가 아니라 Rx가 여러 개이고,
# 각 Rx의 로컬 클럭(meta.timestamp)이 서로 달라 타임스탬프 격자를 쓸 수 없다.
# 대신 모든 Rx가 "같은" 프로브 프레임에서 CSI를 캡처하므로, 게이트웨이가
# 프로브에 실어 보내는 카운터(seq)가 완벽한 정렬 키다. 게이트웨이가 seq를
# 고정 주기(33Hz)로 증가시키므로 seq 격자가 곧 시간 격자다.
#
# 수집기는 CsiBinaryFramer가 복원한 패킷의 recv_mac 마지막 바이트로 rx_id를
# 구해 pkt['rx_id']에 넣은 뒤 feed()에 투입한다.
# ──────────────────────────────────────────────
@dataclass
class SeqSyncedSample:
    seq: int                     # 프로브 카운터 = 슬롯 인덱스
    pkts: dict = field(default_factory=dict)   # {rx_id: pkt or None}

    def fill_count(self):
        return sum(1 for p in self.pkts.values() if p is not None)


class CsiSeqSyncEngine:
    # watermark에서 이보다 먼 seq는 깨진 프레임으로 의심하고 보류 (33Hz 기준 ~30초)
    MAX_SEQ_JUMP = 1000
    # 연속 이상치가 이만큼 쌓이면 실제 스트림이 점프한 것(게이트웨이 재부팅/장시간 단절)
    # 으로 보고 현재 위치로 재앵커
    MAX_CONSEC_OUTLIERS = 5

    def __init__(self, rx_ids=(1, 2, 5, 6), guard_seqs=10):
        self.rx_ids = tuple(rx_ids)
        self.guard = guard_seqs      # watermark - guard 이하의 seq를 확정 (지각 리포트 흡수)

        self._pending = {}           # {seq: {rx_id: pkt}}
        self._next_emit = None       # 다음에 방출할 seq (첫 패킷으로 앵커)
        self._watermark = None       # 지금까지 관측된 최대 seq
        self._consec_outliers = 0

        self.stats = {
            'pkts': {r: 0 for r in self.rx_ids},
            'dup': 0,                # ESP-NOW 재전송으로 같은 리포트가 두 번 도달
            'late_drop': 0,
            'seq_outlier': 0,
            'reanchor': 0,
            'slots_emitted': 0,
            'slots_full': 0,
            'slots_partial': 0,
            'slots_empty': 0,
        }

    def feed(self, pkt: dict) -> list:
        rx = pkt.get('rx_id')
        if rx not in self.stats['pkts']:
            return []
        seq = pkt['seq']

        if self._next_emit is None:
            self._next_emit = seq
            self._watermark = seq

        # 이상치 방어: 단발성 깨진 seq는 버리고, 연속되면 진짜 점프로 보고 재앵커
        if abs(seq - self._watermark) > self.MAX_SEQ_JUMP:
            self.stats['seq_outlier'] += 1
            self._consec_outliers += 1
            if self._consec_outliers >= self.MAX_CONSEC_OUTLIERS:
                self.stats['reanchor'] += 1
                self._consec_outliers = 0
                self._pending.clear()
                self._next_emit = seq
                self._watermark = seq
            else:
                return []
        self._consec_outliers = 0

        if seq < self._next_emit:
            self.stats['late_drop'] += 1
            return []

        self.stats['pkts'][rx] += 1
        bucket = self._pending.setdefault(seq, {})
        if rx in bucket:
            self.stats['dup'] += 1
        else:
            bucket[rx] = dict(pkt)

        if seq > self._watermark:
            self._watermark = seq
        return self._emit_ready()

    def _emit_ready(self) -> list:
        out = []
        while self._next_emit <= self._watermark - self.guard:
            out.append(self._make_sample(self._next_emit))
            self._next_emit += 1
        return out

    def _make_sample(self, seq: int) -> SeqSyncedSample:
        bucket = self._pending.pop(seq, {})
        s = SeqSyncedSample(seq=seq, pkts={r: bucket.get(r) for r in self.rx_ids})
        n = s.fill_count()
        self.stats['slots_emitted'] += 1
        if n == len(self.rx_ids):
            self.stats['slots_full'] += 1
        elif n == 0:
            self.stats['slots_empty'] += 1
        else:
            self.stats['slots_partial'] += 1
        return s

    # ── 패킷이 끊겼을 때 벽시계로 마감 (실시간 지연 상한 보장) ──
    def flush_stale(self, gap_wall_s: float) -> list:
        if self._next_emit is None or gap_wall_s < 0.5:
            return []
        out = []
        while self._next_emit <= self._watermark:
            out.append(self._make_sample(self._next_emit))
            self._next_emit += 1
        return out

    # ── 수집 종료 시 남은 슬롯 전부 방출 ──
    def finalize(self) -> list:
        if self._next_emit is None:
            return []
        out = []
        last = max(self._pending.keys(), default=self._watermark)
        last = max(last, self._watermark)
        while self._next_emit <= last:
            out.append(self._make_sample(self._next_emit))
            self._next_emit += 1
        return out


def build_backhaul_header(rx_ids=(1, 2, 5, 6)) -> list:
    header = ['seq']
    for r in rx_ids:
        header += [f'rx{r}', f'rx{r}_rssi', f'rx{r}_ts_us', f'rx{r}_pc_time', f'rx{r}_data']
    return header


def backhaul_sample_to_row(sample: SeqSyncedSample, rx_ids=(1, 2, 5, 6)) -> list:
    # ts_us는 각 Rx의 로컬 클럭(32bit raw) — 스트림 간 비교용이 아니라 참고용.
    # 정렬은 seq가 보장한다.
    row = [sample.seq]
    for r in rx_ids:
        p = sample.pkts.get(r)
        if p is None:
            row += [0, '', '', '', '']
        else:
            data = ','.join(p['raw_cols'][25:]).strip().strip('"')
            row += [1, p['rssi'], p['ts_us'], f"{p['pc_time']:.6f}", data]
    return row
