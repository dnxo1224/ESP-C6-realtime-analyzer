# -*-coding:utf-8-*-
"""csi_sync_engine 합성 데이터 검증 (하드웨어 불필요)

실행: python test_csi_sync_engine.py
9개 시나리오: 유실, 랩어라운드, 블랙아웃, flush, 파서, 이상치,
오염된 첫 패킷(워밍업 앵커), 재앵커, 바이너리 프레이머.
csi_sync_engine.py를 수정하면 반드시 이 테스트를 먼저 통과시킬 것.
"""
import os, sys, random
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from csi_sync_engine import CsiSyncEngine, parse_csi_line, build_synced_header, synced_sample_to_row

random.seed(42)

def make_pkt(tx, seq, ts):
    return {'tx_id': tx, 'seq': seq, 'rssi': -40, 'ts_us': ts % 2**32,
            'pc_time': 0.0, 'raw_cols': ['CSI_DATA'] + ['0']*24 + ['"[1,2,3]"']}

# ── 시나리오 1: 두 Tx가 33Hz + 지터로 60초 송신, 5% 유실 ──
eng = CsiSyncEngine(grid_hz=30.0, tx_ids=(1, 2))
period = 1_000_000 / 33
events = []
for tx, phase in ((1, 0.0), (2, 15000.0)):
    t = 1000.0 + phase
    seq = 0
    while t < 60_000_000:
        if random.random() > 0.05:
            events.append((t, tx, seq))
        seq += 1
        t += period + random.uniform(-2000, 2000)
events.sort()

emitted = []
for ts, tx, seq in events:
    emitted += eng.feed(make_pkt(tx, seq, int(ts)))
emitted += eng.finalize()

st = eng.stats
n = st['slots_emitted']
print(f"S1: slots={n} (기대 ~1800), full={st['slots_full']} ({100*st['slots_full']/n:.1f}%), "
      f"partial={st['slots_partial']}, empty={st['slots_empty']}, dup={st['dup_in_slot']}, late={st['late_drop']}")
assert 1750 <= n <= 1850, "슬롯 수가 60초*30Hz와 안 맞음"
slots = [s.slot for s in emitted]
assert slots == list(range(len(slots))), "슬롯 인덱스가 연속이 아님"
assert st['slots_full'] / n > 0.85, "완전 슬롯 비율이 비정상적으로 낮음"

# ── 시나리오 2: 32bit 타임스탬프 랩어라운드 걸치기 ──
eng2 = CsiSyncEngine(grid_hz=30.0, tx_ids=(1, 2))
start = 2**32 - 3_000_000   # 랩 3초 전부터 시작
emitted2 = []
for i in range(400):        # 33Hz로 약 12초 → 랩 통과
    ts = start + int(i * period)
    emitted2 += eng2.feed(make_pkt(1, i, ts))
    emitted2 += eng2.feed(make_pkt(2, i, ts + 15000))
emitted2 += eng2.finalize()
times = [s.slot_time_us for s in emitted2]
assert all(b > a for a, b in zip(times, times[1:])), "랩어라운드에서 시간이 역행함"
print(f"S2: 랩어라운드 통과 OK, slots={len(emitted2)}, 시간 단조증가 확인")

# ── 시나리오 3: 중간 2초 블랙아웃 → 빈 슬롯으로 채워지는지 ──
eng3 = CsiSyncEngine(grid_hz=30.0, tx_ids=(1, 2))
emitted3 = []
for i in range(100):
    ts = int(1000 + i * period)
    emitted3 += eng3.feed(make_pkt(1, i, ts))
    emitted3 += eng3.feed(make_pkt(2, i, ts + 15000))
# 2초 공백 후 재개
gap_start = int(1000 + 100 * period)
for i in range(100, 200):
    ts = int(gap_start + 2_000_000 + (i - 100) * period)
    emitted3 += eng3.feed(make_pkt(1, i, ts))
    emitted3 += eng3.feed(make_pkt(2, i, ts + 15000))
emitted3 += eng3.finalize()
empty = sum(1 for s in emitted3 if s.fill_count() == 0)
print(f"S3: 블랙아웃 빈 슬롯 {empty}개 (기대 ~60) — 격자 균일성 유지 OK")
assert 50 <= empty <= 70

# ── 시나리오 4: flush_stale — 패킷 끊긴 뒤 벽시계로 마감 ──
eng4 = CsiSyncEngine(grid_hz=30.0, tx_ids=(1, 2))
pend = []
for i in range(10):
    ts = int(1000 + i * period)
    pend += eng4.feed(make_pkt(1, i, ts))
before = eng4.stats['slots_emitted']
flushed = eng4.flush_stale(1.0)   # 1초간 패킷 없음
print(f"S4: flush 전 방출 {before}, flush로 추가 방출 {len(flushed)} OK")
assert len(flushed) > 0

# ── 시나리오 5: 라인 파서 ──
good = "CSI_DATA,aa:bb:cc:dd:ee:ff,123,1a:00:00:00:00:02,-45," + ",".join(["0"]*14) + ",987654," + ",".join(["0"]*5) + ',"[1,2,3]"'
p = parse_csi_line(good, 1.5)
assert p and p['tx_id'] == 2 and p['seq'] == 123 and p['ts_us'] == 987654 and p['rssi'] == -45
assert parse_csi_line("garbage", 0) is None
assert parse_csi_line(good.replace("-45", "5"), 0) is None      # rssi 비정상
merged = good + ",CSI_DATA,xx"                                   # 두 프레임 붙은 행
assert parse_csi_line(merged, 0) is None
row = synced_sample_to_row(emitted[0], (1, 2))
assert len(row) == len(build_synced_header((1, 2)))
print("S5: 파서/CSV 행 변환 OK")

# ── 시나리오 6: 깨진 타임스탬프(자릿수 오염) 방어 ──
eng6 = CsiSyncEngine(grid_hz=30.0, tx_ids=(1, 2))
em6 = []
for i in range(100):
    ts = int(1000 + i * period)
    em6 += eng6.feed(make_pkt(1, i, ts))
    em6 += eng6.feed(make_pkt(2, i, ts + 15000))
# 250초 미래로 점프한 깨진 행 → 거부되어야 함
em6 += eng6.feed(make_pkt(1, 999, int(1000 + 100 * period) + 250_000_000))
# 아주 작은 값으로 깨진 행 → 시작 직후엔 ±5초 이내라 late_drop으로,
# (uptime이 길 때는 랩 오판정으로 큰 점프가 되어 ts_outlier로) 안전하게 폐기
em6 += eng6.feed(make_pkt(2, 998, 47))
# 이후 정상 스트림이 그대로 이어져야 함
for i in range(100, 200):
    ts = int(1000 + i * period)
    em6 += eng6.feed(make_pkt(1, i, ts))
    em6 += eng6.feed(make_pkt(2, i, ts + 15000))
em6 += eng6.finalize()
st6 = eng6.stats
print(f"S6: 이상치 거부 {st6['ts_outlier']}건, slots={st6['slots_emitted']}, "
      f"full={st6['slots_full']}, empty={st6['slots_empty']}")
assert st6['ts_outlier'] == 1 and st6['late_drop'] >= 1, "깨진 행 2개가 모두 폐기되지 않음"
assert st6['slots_emitted'] < 250, "깨진 타임스탬프가 격자를 오염시킴"
assert st6['slots_empty'] < 5

# ── 시나리오 7: 첫 패킷의 타임스탬프가 오염된 경우 (워밍업 앵커로 방어) ──
eng7 = CsiSyncEngine(grid_hz=30.0, tx_ids=(1, 2))
em7 = []
em7 += eng7.feed(make_pkt(1, 0, 4_290_000_000))   # 깨진 첫 줄 (거의 2^32)
for i in range(200):
    ts = int(1000 + i * period)
    em7 += eng7.feed(make_pkt(1, i, ts))
    em7 += eng7.feed(make_pkt(2, i, ts + 15000))
em7 += eng7.finalize()
st7 = eng7.stats
n7 = st7['slots_emitted']
print(f"S7: 오염된 첫 패킷 워밍업 배제 {st7['warmup_drop']}건, slots={n7}, "
      f"full={st7['slots_full']} ({100*st7['slots_full']/n7:.1f}%), empty={st7['slots_empty']}")
assert st7['warmup_drop'] == 1, "오염된 첫 패킷이 워밍업에서 배제되지 않음"
assert n7 < 250 and st7['slots_full'] / n7 > 0.9, "격자가 오염됨"

# ── 시나리오 8: 워밍업을 통과한 오염 기준점 → 연속 이상치 재앵커 복구 ──
eng8 = CsiSyncEngine(grid_hz=30.0, tx_ids=(1, 2))
em8 = []
for i in range(5):   # 오염된 클러스터가 워밍업을 전부 차지한 극단 상황
    em8 += eng8.feed(make_pkt(1, i, 4_000_000_000 + i * 30000))
for i in range(200): # 실제 스트림은 전혀 다른 위치
    ts = int(1000 + i * period)
    em8 += eng8.feed(make_pkt(1, i, ts))
    em8 += eng8.feed(make_pkt(2, i, ts + 15000))
em8 += eng8.finalize()
st8 = eng8.stats
print(f"S8: 재앵커 {st8['reanchor']}회, 이상치 {st8['ts_outlier']}건, "
      f"slots={st8['slots_emitted']}, full={st8['slots_full']}")
assert st8['reanchor'] == 1, "재앵커가 동작하지 않음"
assert st8['ts_outlier'] <= 25, "재앵커 후에도 이상치가 계속 발생"
assert st8['slots_full'] >= 150, "재앵커 후 정상 수집이 재개되지 않음"

# ── 시나리오 9: 바이너리 프레이머 (조각 수신 + 텍스트 로그 혼입 + 깨진 프레임) ──
import struct as _st
from csi_sync_engine import CsiBinaryFramer, _BIN_META, BIN_MAGIC, BIN_VER

def make_frame(tx, seq, ts, gain=1.0, csi=None):
    csi = csi if csi is not None else list(range(-64, 64))   # 128개 int8
    meta = _BIN_META.pack(
        bytes.fromhex('90e5b1acf718'), bytes([0x1a, 0, 0, 0, 0, tx]),
        seq, ts, -40, 11, 1, 0, 1, 1, 1, 0, 0, 0, 0, -96, 0, 11, 2, 0,
        47, 0, 0, gain, len(csi))
    payload = meta + _st.pack(f'<{len(csi)}b', *csi)
    x = 0
    for b in payload:
        x ^= b
    return BIN_MAGIC + bytes([BIN_VER]) + len(payload).to_bytes(2, 'little') + payload + bytes([x])


fr = CsiBinaryFramer()
stream = b"I (1234) csi_recv: boot log line\n"          # 텍스트 로그 혼입
stream += make_frame(1, 100, 5_000_000)
stream += b"I (999) csi_recv: Stats: printed=1000\n"    # 프레임 사이 텍스트
f2 = make_frame(2, 200, 5_030_000)
stream += f2[:20]                                        # 프레임이 조각으로 나뉘어 도착
rest = f2[20:]
bad = bytearray(make_frame(3, 300, 5_060_000)); bad[30] ^= 0xFF   # 체크섬 깨진 프레임
stream2 = rest + bytes(bad) + make_frame(3, 301, 5_090_000, gain=2.5, csi=[-3, 3, -101])

pkts = fr.feed(stream, 1.0) + fr.feed(stream2, 1.1)
assert [p['tx_id'] for p in pkts] == [1, 2, 3], f"tx 순서 불일치: {[p['tx_id'] for p in pkts]}"
assert pkts[0]['seq'] == 100 and pkts[0]['ts_us'] == 5_000_000 and pkts[0]['rssi'] == -40
assert fr.stats['frames'] == 3 and fr.stats['resync'] >= 1, fr.stats
# gain 복원: int(2.5 * -3) = -7 (0방향 절삭), int(2.5*3)=7, int(2.5*-101)=-252
data_field = ','.join(pkts[2]['raw_cols'][25:]).strip('"')
assert data_field == '[-7,7,-252]', data_field
# 복원된 줄이 엔진에 그대로 들어가는지
eng9 = CsiSyncEngine(grid_hz=30.0, tx_ids=(1, 2, 3))
for p in pkts:
    eng9.feed(p)
print(f"S9: 프레이머 OK — frames={fr.stats['frames']}, resync={fr.stats['resync']}, gain 복원 OK")

# ════════════════════════════════════════════════════════════
# WIRELESS-BACKHAUL: CsiSeqSyncEngine (1Tx-4Rx, seq 정렬) 시나리오
# ════════════════════════════════════════════════════════════
from csi_sync_engine import (
    CsiSeqSyncEngine, build_backhaul_header, backhaul_sample_to_row,
)

RX_IDS = (1, 2, 5, 6)

def make_bh_pkt(rx, seq):
    return {'rx_id': rx, 'seq': seq, 'rssi': -40,
            'ts_us': (seq * 30303 + rx * 7) % 2**32,   # Rx마다 다른 로컬 클럭 흉내
            'pc_time': 0.0, 'raw_cols': ['CSI_DATA'] + ['0'] * 24 + ['"[1,2,3]"']}

# ── 시나리오 10: 33Hz 600프로브, 3% 백홀 유실 + 1% ESP-NOW 재전송 중복 ──
random.seed(7)
eng10 = CsiSeqSyncEngine(rx_ids=RX_IDS, guard_seqs=10)
em10 = []
for seq in range(600):
    for rx in RX_IDS:
        if random.random() < 0.03:
            continue
        em10 += eng10.feed(make_bh_pkt(rx, seq))
        if random.random() < 0.01:
            em10 += eng10.feed(make_bh_pkt(rx, seq))   # 중복 도달
em10 += eng10.finalize()
st10 = eng10.stats
assert [s.seq for s in em10] == list(range(600)), "seq 격자가 연속이 아님"
assert st10['dup'] > 0, "중복이 카운트되지 않음"
full10 = st10['slots_full'] / st10['slots_emitted']
print(f"S10: slots={st10['slots_emitted']}, full={100*full10:.1f}%, "
      f"partial={st10['slots_partial']}, dup={st10['dup']}")
assert full10 > 0.8, "완전 슬롯 비율이 비정상적으로 낮음"

# ── 시나리오 11: 게이트웨이 재부팅(seq 리셋 5000→0) → 연속 이상치 재앵커 ──
eng11 = CsiSeqSyncEngine(rx_ids=RX_IDS, guard_seqs=10)
em11 = []
for seq in range(5000, 5100):
    for rx in RX_IDS:
        em11 += eng11.feed(make_bh_pkt(rx, seq))
for seq in range(0, 100):
    for rx in RX_IDS:
        em11 += eng11.feed(make_bh_pkt(rx, seq))
em11 += eng11.finalize()
st11 = eng11.stats
print(f"S11: reanchor={st11['reanchor']}, outlier={st11['seq_outlier']}, "
      f"slots={st11['slots_emitted']}")
assert st11['reanchor'] == 1, "seq 리셋에서 재앵커가 동작하지 않음"
assert st11['seq_outlier'] == 5, "재앵커까지의 이상치 수가 기대와 다름 (트리거 포함 5)"
assert st11['slots_emitted'] >= 185, "재앵커 후 수집이 재개되지 않음"

# ── 시나리오 12: 프로브 전체 유실 구간(30슬롯) → 빈 슬롯으로 격자 유지 + 행 변환 ──
eng12 = CsiSeqSyncEngine(rx_ids=RX_IDS, guard_seqs=5)
em12 = []
for seq in list(range(50)) + list(range(80, 130)):
    for rx in RX_IDS:
        em12 += eng12.feed(make_bh_pkt(rx, seq))
em12 += eng12.finalize()
empty12 = sum(1 for s in em12 if s.fill_count() == 0)
hdr12 = build_backhaul_header(RX_IDS)
rows12 = [backhaul_sample_to_row(s, RX_IDS) for s in em12]
assert all(len(r) == len(hdr12) for r in rows12), "행 길이가 헤더와 안 맞음"
print(f"S12: 빈 슬롯 {empty12}개 (기대 30) — 격자 균일성 유지 OK")
assert empty12 == 30

# ── 시나리오 13: 프레이머 → recv_mac demux → seq 엔진 end-to-end ──
# 게이트웨이가 USB로 내보내는 것과 동일한 바이트 스트림(리포트 프레임 + 로그 혼입)을
# 수집기 경로 그대로: CsiBinaryFramer → rx_id 추출 → CsiSeqSyncEngine.
def make_bh_frame(rx, seq, csi=None):
    csi = csi if csi is not None else list(range(-64, 64))
    meta = _BIN_META.pack(
        bytes([0x1e, 0, 0, 0, 0, rx]),          # recv_mac = Rx 정체성
        bytes([0x1a, 0, 0, 0, 0, 0x01]),        # src_mac = 게이트웨이
        seq, (seq * 30303 + rx * 7) % 2**32,
        -40, 11, 1, 0, 1, 1, 1, 0, 0, 0, 0, -96, 0, 11, 2, 0,
        47, 0, 0, 1.0, len(csi))
    payload = meta + _st.pack(f'<{len(csi)}b', *csi)
    x = 0
    for b in payload:
        x ^= b
    return BIN_MAGIC + bytes([BIN_VER]) + len(payload).to_bytes(2, 'little') + payload + bytes([x])

framer13 = CsiBinaryFramer()
eng13 = CsiSeqSyncEngine(rx_ids=RX_IDS, guard_seqs=5)
stream = b''
for seq in range(60):
    for rx in RX_IDS:
        stream += make_bh_frame(rx, seq)
    if seq % 10 == 0:
        stream += b'I (1234) csi_gw: Stats: probes=100 reports...\r\n'   # 로그 혼입
em13 = []
for i in range(0, len(stream), 100):   # 조각 수신 흉내
    for pkt in framer13.feed(stream[i:i + 100], 0.0):
        pkt['rx_id'] = int(pkt['raw_cols'][1].split(':')[-1], 16)
        em13 += eng13.feed(pkt)
em13 += eng13.finalize()
st13 = eng13.stats
print(f"S13: 프레임 {framer13.stats['frames']}개 복원(resync={framer13.stats['resync']}), "
      f"slots={st13['slots_emitted']}, full={st13['slots_full']}")
assert framer13.stats['frames'] == 240, "프레임 복원 수가 안 맞음"
assert st13['slots_emitted'] == 60 and st13['slots_full'] == 60, "end-to-end 정렬 실패"


print("\n모든 테스트 통과")
