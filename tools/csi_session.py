#!/usr/bin/env python3
# -*-coding:utf-8-*-
"""
csi_session.py — 실험 세션 수집기 (WIRELESS-BACKHAUL 1Tx-4Rx, ESP32-C6/HE20)

사용법:
  1) 아래 [세션 설정] 블록의 PLACE / SUBJECT / ACTION / DURATION 을 수정하고
  2) py -3 csi_session.py 실행.
     (또는 블록을 안 고치고 CLI로: py -3 csi_session.py --action walk --duration 120)

폴더 구조 (같은 클래스 조합을 다시 돌리면 trial 번호가 자동 증가 — 덮어쓰기 없음):

  data/sessions/<PLACE>/<SUBJECT>/<ACTION>/trial_01/
      rx1.csv ~ rx4.csv    ← 노드별 raw CSV (기존 진단 도구와 같은 포맷 + pc_time)
      synced.csv           ← 프로브 seq당 1행, 4노드 정렬. 결측 노드는 빈칸
      meta.json            ← 라벨·시각·노드별 카운트·동기화 품질 (데이터 선별용)

데이터 경로: 게이트웨이(COM13) USB 하나만 읽는다. Rx들은 전원만 있으면 된다
(보조배터리 배치 OK — 반드시 "최종 위치에서" 전원을 넣을 것: 게인 baseline이
부팅 후 첫 100프레임에서 잡히기 때문).

포맷 계약 (펌웨어와 짝): HE20 SU 512B = 256서브캐리어, null 0-4·128·252-255
→ 유효 진폭 246차원. csi_len이 512가 아닌 프레임은 계약 위반으로 집계된다.
"""

import argparse
import csv
import json
import os
import time
from collections import deque
from datetime import datetime

import serial
import winsound

from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box

from csi_sync_engine import (
    CsiSeqSyncEngine, CsiBinaryFramer, RAW_CSV_HEADER,
    build_backhaul_header, backhaul_sample_to_row,
)

console = Console()

# ──────────────────────────────────────────────
# [세션 설정] — 실험마다 이 블록만 고친다
# ──────────────────────────────────────────────
PLACE     = 'lab'        # 수집 장소 라벨 (예: lab, home_A)
SUBJECT   = 'A'          # 피험자 라벨 (예: A, B, empty ← 부재 수집이면 empty)
ACTION    = 'empty'      # 행동 (예: empty, walk, sitdown, liedown, handsup)
DURATION  = 120          # 수집 시간 (초). empty 기준선은 길게 (예: 300~1920)
PREP_TIME = 5            # 시작 전 카운트다운 (초) — 측정 위치로 이동할 시간

PORT      = 'COM4'       # 게이트웨이(Tx)의 USB 포트. Rx들은 전원만 있으면 된다
RX_IDS    = (1, 2, 3, 4) # recv_mac 마지막 바이트

DATA_COL      = RAW_CSV_HEADER.index('data')   # 이 열부터 끝까지가 CSI 배열 하나
GATEWAY_MAC   = '1a:00:00:00:00:01'
GUARD_SEQS    = 10       # watermark보다 이만큼 뒤처진 seq를 확정 (~0.3s 지각 흡수)
NOMINAL_HZ    = 33.0     # 프로브 주기 (UI 게이지 스케일)
CSI_LEN_EXPECT = 512     # 포맷 계약 — 아니면 위반 카운트
FULL_ROW_GOAL = 95.0     # 세션 합격선: 4/4 완전행 % (미달 시 경고)

BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'sessions')

UPDATE_SEC       = 0.5
RECENT_HZ_WINDOW = 1.0


# ──────────────────────────────────────────────
# 세션 폴더 준비 (trial 자동 증가)
# ──────────────────────────────────────────────
def make_trial_dir(place, subject, action, base=None):
    class_dir = os.path.join(base or BASE_DIR, place, subject, action)
    os.makedirs(class_dir, exist_ok=True)
    n = 1
    while True:
        trial_dir = os.path.join(class_dir, f"trial_{n:02d}")
        if not os.path.exists(trial_dir):
            os.makedirs(trial_dir)
            return trial_dir, n
        n += 1


# ──────────────────────────────────────────────
# Rich UI
# ──────────────────────────────────────────────
def build_ui(label, counts, hz_hist, engine, collect_start, duration, len_bad):
    MAX_HZ, BAR_CHARS = NOMINAL_HZ + 2, 24
    now = time.time()
    elapsed = max(now - collect_start, 0.001)
    remain = max(duration - elapsed, 0)
    pct = min(elapsed / duration, 1.0)

    bar = "█" * int(pct * 40) + "░" * (40 - int(pct * 40))
    prog = Text()
    prog.append(" 진행  ", style="bold cyan")
    prog.append(bar, style="cyan")
    prog.append(f"  {time.strftime('%M:%S', time.gmtime(elapsed))}"
                f" / {time.strftime('%M:%S', time.gmtime(duration))}"
                f"  (남은 시간 {time.strftime('%M:%S', time.gmtime(remain))})", style="white")

    table = Table(box=box.ROUNDED, show_header=True,
                  header_style="bold white on grey23", expand=True, padding=(0, 1))
    table.add_column("수신기", style="bold", width=10, justify="center")
    table.add_column("수신율", width=30, justify="left")
    table.add_column("Hz", width=8, justify="right")
    table.add_column("누적", width=10, justify="right")

    for rx in RX_IDS:
        count = counts[rx]
        hist = hz_hist[rx]
        hist.append((now, count))
        while len(hist) >= 2 and (now - hist[1][0]) >= RECENT_HZ_WINDOW:
            hist.popleft()
        t0, c0 = hist[0]
        dt = now - t0
        hz = (count - c0) / dt if dt >= 0.3 else count / elapsed

        filled = int(min(hz / MAX_HZ, 1.0) * BAR_CHARS)
        bar_t = Text()
        style = "green" if hz >= NOMINAL_HZ * 0.9 else ("yellow" if hz >= NOMINAL_HZ * 0.5 else "red")
        bar_t.append("█" * filled, style=style)
        bar_t.append("░" * (BAR_CHARS - filled), style="grey50")
        table.add_row(f"Rx{rx}", bar_t, Text(f"{hz:.1f}", style=style), f"{count}")

    st = engine.stats
    emitted = st['slots_emitted']
    if emitted > 0:
        fill_line = (f" 동기화 슬롯 {emitted}개: "
                     f"완전 {st['slots_full']} ({100*st['slots_full']/emitted:.1f}%) | "
                     f"부분 {st['slots_partial']} | 빈칸 {st['slots_empty']} | "
                     f"중복 {st['dup']}")
    else:
        fill_line = " 동기화 슬롯 대기 중..."
    if len_bad:
        fill_line += f"  [bold red]| 계약위반(csi_len≠{CSI_LEN_EXPECT}): {len_bad}[/bold red]"

    return Group(Panel(
        Group(prog, table, Text(fill_line, style="cyan")),
        title=f"[bold cyan]CSI 세션 수집 — {label}[/bold cyan]",
        border_style="cyan",
    ))


# ──────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="1Tx-4Rx 백홀 CSI 세션 수집기")
    ap.add_argument('--place',    default=PLACE)
    ap.add_argument('--subject',  default=SUBJECT)
    ap.add_argument('--action',   default=ACTION)
    ap.add_argument('--duration', type=float, default=DURATION)
    ap.add_argument('--prep',     type=float, default=PREP_TIME)
    ap.add_argument('--port',     default=PORT)
    ap.add_argument('--out',      default=None,
                    help="저장 루트 (기본: tools/data/sessions)")
    ap.add_argument('--session-dir', default=None,
                    help="저장 폴더를 직접 지정 (큐 스크립트가 세션 폴더를 잡아줄 때 사용). "
                         "지정하면 place/subject/action 폴더 구조와 trial 번호를 쓰지 않는다")
    args = ap.parse_args()

    label = f"{args.place} / {args.subject} / {args.action}"
    if args.session_dir:
        trial_dir, trial_no = os.path.abspath(args.session_dir), 0
        os.makedirs(trial_dir, exist_ok=True)
    else:
        trial_dir, trial_no = make_trial_dir(args.place, args.subject, args.action, args.out)
    console.print(f"\n[bold][*] 세션: {label}  →  trial_{trial_no:02d}[/bold]")
    console.print(f"    저장 위치: {trial_dir}")

    raw_paths = {rx: os.path.join(trial_dir, f"rx{rx}.csv") for rx in RX_IDS}
    synced_path = os.path.join(trial_dir, "synced.csv")
    meta_path = os.path.join(trial_dir, "meta.json")

    console.print(f"\n[*] 게이트웨이 포트 {args.port} 연결 중...")
    ser = serial.Serial(port=args.port, baudrate=2000000, bytesize=8,
                        parity='N', stopbits=1, timeout=0.05)
    try:
        ser.set_buffer_size(rx_size=262144, tx_size=65536)
    except Exception:
        pass
    console.print(f"[*] {args.port} 연결 완료.")

    # 카운트다운 (삑삑삑 → 삐-)
    if args.prep > 0:
        console.print(f"\n[bold cyan][*] {args.prep:.0f}초 뒤 수집 시작. 측정 위치로 이동하세요![/bold cyan]")
        for i in range(int(args.prep), 0, -1):
            console.print(f" - {i}초 전...")
            winsound.Beep(1500, 150)
            time.sleep(1)

    # 장치측 backlog 드레인: 게이트웨이 32KB 링버퍼에 쌓인 옛 리포트를 폐기해
    # 수집 시작점을 비프 시점과 정렬한다 (안 하면 첫 구간 레이트가 부풀어 보임)
    ser.reset_input_buffer()
    drained = 0
    drain_start = time.time()
    while time.time() - drain_start < 1.0:
        drained += len(ser.read(4096))
    ser.reset_input_buffer()
    console.print(f"[*] 시작 전 잔여 데이터 {drained:,} bytes 폐기 (backlog 드레인)")

    console.print("\n[bold green][>> 수집 시작 <<][/bold green]")
    winsound.Beep(1000, 800)

    engine = CsiSeqSyncEngine(rx_ids=RX_IDS, guard_seqs=GUARD_SEQS)
    framer = CsiBinaryFramer()

    raw_files, raw_writers = {}, {}
    for rx in RX_IDS:
        f = open(raw_paths[rx], 'w', newline='', buffering=65536)
        raw_files[rx] = f
        raw_writers[rx] = csv.writer(f)
        raw_writers[rx].writerow(RAW_CSV_HEADER + ['pc_time'])
    synced_file = open(synced_path, 'w', newline='', buffering=65536)
    synced_writer = csv.writer(synced_file)
    synced_writer.writerow(build_backhaul_header(RX_IDS))

    counts = {rx: 0 for rx in RX_IDS}
    hz_hist = {rx: deque() for rx in RX_IDS}
    unknown_rx = 0
    len_bad = 0
    synced_rows = 0

    start_iso = datetime.now().isoformat(timespec='seconds')
    collect_start = time.time()
    last_pkt_wall = collect_start
    last_ui = 0.0

    def write_samples(samples):
        nonlocal synced_rows
        for s in samples:
            synced_writer.writerow(backhaul_sample_to_row(s, RX_IDS))
            synced_rows += 1

    interrupted = False
    with Live(build_ui(label, counts, hz_hist, engine, collect_start, args.duration, len_bad),
              console=console, refresh_per_second=1 / UPDATE_SEC,
              transient=False) as live:
        try:
            while (time.time() - collect_start) < args.duration:
                data = ser.read(4096)
                now = time.time()

                if data:
                    for pkt in framer.feed(data, now):
                        if pkt is None:
                            continue
                        # src는 반드시 게이트웨이 — 다른 펌웨어 기기 오염 차단
                        if pkt['raw_cols'][3] != GATEWAY_MAC:
                            unknown_rx += 1
                            continue
                        # demux: recv_mac(1e:...:XX) 마지막 바이트 = Rx 번호
                        recv_mac = pkt['raw_cols'][1]
                        if not recv_mac.startswith('1e:'):
                            unknown_rx += 1
                            continue
                        try:
                            rx_id = int(recv_mac.split(':')[-1], 16)
                        except ValueError:
                            unknown_rx += 1
                            continue
                        if rx_id not in counts:
                            unknown_rx += 1
                            continue

                        # 포맷 계약 감시: csi_len != 512는 위반으로 집계 (저장은 함)
                        try:
                            if int(pkt['raw_cols'][23]) != CSI_LEN_EXPECT:
                                len_bad += 1
                        except (ValueError, IndexError):
                            len_bad += 1

                        pkt['rx_id'] = rx_id
                        counts[rx_id] += 1
                        last_pkt_wall = now
                        # raw_cols는 CSI_DATA 줄을 쉼표로 자른 결과라 마지막 'data'
                        # 배열이 수백 개 열로 흩어져 있다. 그대로 쓰면 헤더 26열과
                        # 행 538열이 어긋나 표준 CSV 파서가 엉뚱한 값을 읽는다.
                        # data 인덱스부터 다시 이어 붙여 한 필드로 되돌린다
                        # (쉼표를 포함하므로 csv.writer가 따옴표로 감싼다).
                        cols = pkt['raw_cols']
                        row = cols[:DATA_COL] + [','.join(cols[DATA_COL:])]
                        raw_writers[rx_id].writerow(row + [f"{now:.6f}"])
                        write_samples(engine.feed(pkt))
                else:
                    # 스트림이 끊겼으면 벽시계 기준으로 미확정 슬롯 마감
                    write_samples(engine.flush_stale(now - last_pkt_wall))

                if now - last_ui >= UPDATE_SEC:
                    live.update(build_ui(label, counts, hz_hist, engine,
                                         collect_start, args.duration, len_bad))
                    last_ui = now

        except KeyboardInterrupt:
            interrupted = True
            console.print("\n[yellow][!] 사용자 중단 — 지금까지 데이터는 저장됩니다[/yellow]")
        finally:
            write_samples(engine.finalize())
            for f in raw_files.values():
                f.flush(); f.close()
            synced_file.flush(); synced_file.close()
            if ser.is_open:
                ser.close()

    elapsed = time.time() - collect_start
    console.print("\n[bold green][<< 수집 종료 >>][/bold green]")
    winsound.Beep(1000, 300); time.sleep(0.1); winsound.Beep(1000, 300)

    # ── meta.json — 나중에 데이터 선별할 때의 근거 ──
    st = engine.stats
    emitted = st['slots_emitted']
    full_pct = 100 * st['slots_full'] / emitted if emitted else 0.0
    meta = {
        'place': args.place, 'subject': args.subject, 'action': args.action,
        'trial': trial_no,
        'start_time': start_iso,
        'end_time': datetime.now().isoformat(timespec='seconds'),
        'duration_sec': round(elapsed, 1),
        'duration_planned_sec': args.duration,
        'interrupted': interrupted,
        'port': args.port,
        'rx_ids': list(RX_IDS),
        'reports_per_node': {f"rx{rx}": counts[rx] for rx in RX_IDS},
        'hz_per_node': {f"rx{rx}": round(counts[rx] / elapsed, 2) for rx in RX_IDS},
        'sync': {
            'slots': emitted, 'rows_written': synced_rows,
            'full': st['slots_full'], 'full_pct': round(full_pct, 2),
            'partial': st['slots_partial'], 'empty': st['slots_empty'],
            'dup': st['dup'], 'seq_outlier': st['seq_outlier'],
            'late_drop': st['late_drop'], 'reanchor': st['reanchor'],
        },
        'framer': dict(framer.stats),
        'unknown_rx': unknown_rx,
        'contract_violation_len': len_bad,
        'format': {
            'phy': 'HE20 (802.11ax SU) MCS0 LGI ch11',
            'csi_len_bytes': CSI_LEN_EXPECT,
            'subcarriers': CSI_LEN_EXPECT // 2,
            'null_subcarriers': list(range(0, 5)) + [128] + list(range(252, 256)),
            'amplitude_dims': CSI_LEN_EXPECT // 2 - 10,
        },
    }
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # ── 요약 ──
    console.print(f"\n[bold][수집 요약 — {label} trial_{trial_no:02d}][/bold]")
    for rx in RX_IDS:
        rate = counts[rx] / elapsed if elapsed > 0 else 0
        mark = "[green]OK[/green]" if rate >= NOMINAL_HZ * 0.9 else "[red]낮음[/red]"
        console.print(f"  Rx{rx}: {counts[rx]} 프레임 | {rate:.1f} Hz {mark}")
    if emitted:
        console.print(f"  동기화: {synced_rows}행 → synced.csv")
        console.print(f"    완전(4/4): {st['slots_full']} ({full_pct:.1f}%) | "
                      f"부분: {st['slots_partial']} | 빈칸: {st['slots_empty']} | "
                      f"중복: {st['dup']} | 이상치: {st['seq_outlier']} | 재앵커: {st['reanchor']}")
    console.print(f"  프레임: {framer.stats['frames']} | resync: {framer.stats['resync']} | "
                  f"미지정: {unknown_rx} | 계약위반(len): {len_bad}")
    console.print(f"  메타: {meta_path}")

    if len_bad:
        console.print(f"\n[bold red][!] csi_len≠{CSI_LEN_EXPECT} 프레임 {len_bad}개 — "
                      f"HE20 협상 실패 의심. 이 trial은 학습에 쓰지 말 것.[/bold red]")
    if full_pct < FULL_ROW_GOAL:
        console.print(f"\n[bold yellow][!] 완전행 {full_pct:.1f}% < 목표 {FULL_ROW_GOAL}% — "
                      f"배치/RSSI(-40~-70)/전원을 점검하고 재수집을 권장합니다.[/bold yellow]")
    else:
        console.print(f"\n[bold green][✓] 완전행 {full_pct:.1f}% — 세션 합격[/bold green]")


if __name__ == '__main__':
    main()
