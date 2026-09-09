#!/usr/bin/env python3
# -*-coding:utf-8-*-
"""
collect_session.py — 낙상 데이터 수집 큐 스크립트 (COLLECTOR_HANDOFF.md 구현)

CSI 수집기(csi_session.py) 옆에서 돌면서 정해진 60초 격자로 음성·화면 지시를 내리고,
모든 큐를 수집기와 **같은 PC 시계**(time.time)로 cues.csv에 기록한다.
이 파일과 수집기의 pc_time만 있으면 모델 세션이 라벨을 자동 계산할 수 있다.

  python collect_session.py --mode event --session-no 1 --subject A --config o1_f01 --out D:\\csi_collect
  python collect_session.py --mode life  --session-no 1 --subject A --config o1_f01 --out D:\\csi_collect
  python collect_session.py --mode empty --minutes 15 --config o1_f01 --out D:\\csi_collect
  python collect_session.py --dry-run --mode event --session-no 3      # 수집기 없이 리허설

--collector-cmd 를 주면 수집기를 서브프로세스로 띄우고 첫 프레임을 확인한 뒤 시작한다.
없으면 수집기가 이미 돌고 있다고 가정한다(어느 쪽이든 t0가 기록되므로 정렬은 pc_time으로 한다).

키: X=현재 사이클 무효 / N=메모 / P=일시정지·재개 / Q=중단
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

try:
    import winsound
except ImportError:                                   # 비-Windows 리허설용
    winsound = None

import tkinter as tk

# ──────────────────────────────────────────────
# 세션 구성 (문서 1·2·3절 — 모든 시각 고정)
# ──────────────────────────────────────────────
ABSENCE_S = 120.0          # 0:00~2:00 부재
ENTER_S = 120.0            # 2:00 "입장하세요" — 입장·준비 10초
CYCLE0_S = 130.0           # 2:10 첫 사이클 시작
CYCLE_LEN = 60.0
N_CYCLES = 8
SESSION_S = CYCLE0_S + N_CYCLES * CYCLE_LEN            # 610초 = 10:10

ACTS = [
    ("stand_still", "가만히 서 계세요"),
    ("stretch",     "가만히 서서 기지개를 펴세요"),
    ("pick_stand",  "가만히 서서 물건을 주우세요"),
    ("walk",        "걸어다니세요"),
    ("walk_pick",   "걸어다니며 물건을 주우세요"),
    ("chair_sit",   "의자로 가서 앉아 기다리세요"),
]
RESTS = {
    "lie_rest": "매트에 누워 가만히 계세요",
    "sit_rest": "의자에 앉아 가만히 계세요",
}
LIFE_BLOCKS = [
    ("walk",        "걸어다니세요"),
    ("sit_rest",    "의자에 앉아 가만히 계세요"),
    ("lie_rest",    "매트에 누워 가만히 계세요"),
    ("walk_pick",   "걸어다니며 물건을 주우세요"),
    ("stretch_arm", "서서 기지개를 펴고 팔을 흔드세요"),
    ("chair_door",  "의자를 옮기고 문을 여닫으세요"),
    ("desk_free",   "책상 앞에서 자유롭게 하세요"),
    ("mat_sit",     "매트에 앉아 가만히 계세요"),
]
TEXT_ENTER, TEXT_FALL = "입장하세요", "낙상"
TEXT_WIGGLE, TEXT_RISE = "조금씩 움직이세요", "일어나세요"
TEXT_END, TEXT_PAUSE, TEXT_RESUME = "수집 종료", "일시정지", "재개합니다"

ALL_TEXTS = ([TEXT_ENTER, TEXT_FALL, TEXT_WIGGLE, TEXT_RISE, TEXT_END, TEXT_PAUSE, TEXT_RESUME]
             + [t for _, t in ACTS] + list(RESTS.values()) + [t for _, t in LIFE_BLOCKS])


def cycle_order(session_no: int):
    """세션 번호로 사이클 순서를 정한다 (무작위 없음, 문서 2절)."""
    off = (session_no - 1) % len(ACTS)
    acts = ACTS[off:] + ACTS[:off]
    rests = ["lie_rest", "sit_rest"] if session_no % 2 == 1 else ["sit_rest", "lie_rest"]
    return acts[:3] + [(rests[0], RESTS[rests[0]])] + acts[3:] + [(rests[1], RESTS[rests[1]])]


def build_cues(mode: str, session_no: int, minutes: float):
    """(t_session_s, phase, label, text, cycle, hold_s) 목록을 시간순으로 만든다.

    hold_s는 '이 지시를 유지하는 시간'으로, 화면에 남은 초를 띄우는 용도다."""
    cues = [(0.0, "start", "empty", "부재 — 문 밖에서 대기", 0, ABSENCE_S)]
    if mode == "empty":
        total = minutes * 60.0
        cues[0] = (0.0, "start", "empty", "부재 수집 중", 0, total)
        cues.append((total, "end", "empty", TEXT_END, 0, 0.0))
        return cues, total, []

    cues.append((ENTER_S, "enter", "enter", TEXT_ENTER, 0, 10.0))

    if mode == "life":
        for k, (label, text) in enumerate(LIFE_BLOCKS):
            cues.append((CYCLE0_S + k * CYCLE_LEN, "act", label, text, k + 1, CYCLE_LEN))
        cues.append((SESSION_S, "end", "end", TEXT_END, 0, 0.0))
        return cues, SESSION_S, [lb for lb, _ in LIFE_BLOCKS]

    order = cycle_order(session_no)
    for k, (label, text) in enumerate(order):
        base = CYCLE0_S + k * CYCLE_LEN
        cyc = k + 1
        if label in RESTS:                       # 휴식 사이클: 낙상 없음
            cues.append((base, "rest", label, text, cyc, 50.0))
            cues.append((base + 50.0, "rise", label, TEXT_RISE, cyc, 10.0))
        else:                                    # 낙상 사이클
            cues.append((base, "act", label, text, cyc, 20.0))
            cues.append((base + 20.0, "fall", label, TEXT_FALL, cyc, 30.0))
            if cyc == 1:                         # 꿈틀 큐는 항상 c1 (문서 2절)
                cues.append((base + 23.0, "wiggle", label, TEXT_WIGGLE, cyc, 27.0))
            cues.append((base + 50.0, "rise", label, TEXT_RISE, cyc, 10.0))
    cues.append((SESSION_S, "end", "end", TEXT_END, 0, 0.0))
    cues.sort(key=lambda c: c[0])
    return cues, SESSION_S, [lb for lb, _ in order]


# ──────────────────────────────────────────────
# 음성 — 시작 시 한 번 WAV로 합성해 캐시하고 재생은 winsound
# ──────────────────────────────────────────────
class Voice:
    def __init__(self, cache_dir: str, mute: bool = False):
        self.dir = cache_dir
        os.makedirs(self.dir, exist_ok=True)
        self.backend = "none"
        self.mute = mute            # 리허설용 무음 (합성·기록은 그대로 한다)
        self.paths: dict[str, str] = {}

    def _path(self, text: str) -> str:
        return os.path.join(self.dir, hashlib.sha1(text.encode("utf-8")).hexdigest()[:16] + ".wav")

    def prepare(self, texts, log=print):
        texts = list(dict.fromkeys(texts))          # 문구 중복 제거 (life 모드와 겹친다)
        missing = [t for t in texts if not os.path.exists(self._path(t))]
        if not missing:
            self.backend = "cache"
        elif self._synth_sapi(missing, log):
            self.backend = "sapi"
        elif self._synth_edge(missing, log):
            self.backend = "edge-tts"
        else:
            self.backend = "none"
            log("[!] 음성 합성 실패 — 삐 소리와 화면 문구만으로 진행합니다")
        for t in texts:
            p = self._path(t)
            if os.path.exists(p):
                self.paths[t] = p
        if self.backend != "none" and len(self.paths) < len(texts):
            log(f"[!] 일부 문구 음성 없음 ({len(self.paths)}/{len(texts)})")
        return self.backend

    def _synth_sapi(self, texts, log) -> bool:
        """Windows 내장 한국어 음성(Heami 등)으로 WAV 생성."""
        if os.name != "nt":
            return False
        # 목록은 임시 JSON 파일로 넘긴다 — powershell -Command 에 stdin을 물리면
        # $input이 비어 조용히 0개를 합성하고 끝난다(성공처럼 보여 더 위험하다).
        items = [{"t": t, "p": self._path(t)} for t in texts]
        list_path = os.path.join(self.dir, "_synth.json")
        with open(list_path, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$ko = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -eq 'ko-KR' } | "
            "  Select-Object -First 1; "
            "if ($null -eq $ko) { exit 2 }; "
            "$s.SelectVoice($ko.VoiceInfo.Name); "
            f"$items = Get-Content -Raw -Encoding UTF8 '{list_path}' | ConvertFrom-Json; "
            "foreach ($i in $items) { $s.SetOutputToWaveFile($i.p); $s.Speak($i.t) }; "
            "$s.Dispose(); exit 0"
        )
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                               capture_output=True, timeout=300)
            made = sum(1 for i in items if os.path.exists(i["p"]))
            if r.returncode == 0 and made == len(items):
                log(f"[*] 음성 합성 완료 (Windows 내장 한국어 음성), {made}개")
                return True
            log(f"[!] SAPI 합성 미완 ({made}/{len(items)}) — edge-tts로 시도합니다")
        except Exception as exc:
            log(f"[!] SAPI 합성 실패: {exc}")
        finally:
            try:
                os.remove(list_path)
            except OSError:
                pass
        return False

    def _synth_edge(self, texts, log) -> bool:
        """edge-tts(인터넷 필요) → mp3 → wav. ffmpeg가 없으면 실패로 본다."""
        try:
            for t in texts:
                mp3 = self._path(t) + ".mp3"
                subprocess.run(["edge-tts", "--voice", "ko-KR-SunHiNeural",
                                "--text", t, "--write-media", mp3],
                               capture_output=True, timeout=60, check=True)
                subprocess.run(["ffmpeg", "-y", "-i", mp3, self._path(t)],
                               capture_output=True, timeout=60, check=True)
                os.remove(mp3)
            log(f"[*] 음성 합성 완료 (edge-tts), {len(texts)}개")
            return True
        except Exception as exc:
            log(f"[!] edge-tts 합성 실패: {exc}")
            return False

    def beep(self, times: int = 1):
        if self.mute:
            return

        def run():
            for i in range(times):
                if winsound:
                    winsound.Beep(1000, 300)
                if i + 1 < times:
                    time.sleep(0.12)
        threading.Thread(target=run, daemon=True).start()

    def say(self, text: str):
        if self.mute:
            return
        path = self.paths.get(text)
        if path and winsound:
            try:
                winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            except Exception:
                pass


# ──────────────────────────────────────────────
# 세션 실행기 (tkinter 전체화면)
# ──────────────────────────────────────────────
class SessionRunner:
    def __init__(self, args, cues, total_s, cycle_labels, out_dir):
        self.args, self.cues, self.total_s = args, cues, total_s
        self.cycle_labels, self.out_dir = cycle_labels, out_dir
        self.voice = Voice(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "assets", "voice"), mute=args.mute)
        self.rows: list[dict] = []
        self.notes: list[dict] = []
        self.invalid: set[int] = set()
        self.restarted: set[int] = set()
        self.idx = 0
        self.t0 = None                 # 0:00 삐를 낸 순간의 time.time()
        self.perf0 = None              # 같은 순간의 perf_counter (드리프트 없는 스케줄)
        self.paused_at = None
        self.pause_shift = 0.0         # 일시정지로 밀린 총 시간
        self.current = None            # (phase, label, text, cycle, hold_s, start_session_s)
        self.max_error_ms = 0.0
        self.collector = None
        self.finished = False
        self._build_ui()

    # ── UI ──
    def _build_ui(self):
        self.root = tk.Tk()
        self.root.title("CSI 수집 세션")
        self.root.configure(bg="black")
        if self.args.windowed:
            # 리허설 창이 포커스를 가져가면 조작자가 다른 곳에 입력한 키가
            # 이 창의 단축키(P/Q/X)로 먹혀 세션이 끊긴다. 뒤로 내려둔다.
            self.root.geometry("900x600")
            self.root.lower()
        else:
            self.root.attributes("-fullscreen", True)
        self.root.bind("<Key>", self._on_key)

        self.top = tk.Label(self.root, text="", fg="#9ca3af", bg="black",
                            font=("Malgun Gothic", 20))
        self.top.pack(pady=(18, 6))
        self.bar = tk.Canvas(self.root, height=14, bg="#1f2937", highlightthickness=0)
        self.bar.pack(fill="x", padx=40)
        self.center = tk.Label(self.root, text="준비 중…", fg="white", bg="black",
                               font=("Malgun Gothic", 84, "bold"), wraplength=1600)
        self.center.pack(expand=True)
        self.hold = tk.Label(self.root, text="", fg="#60a5fa", bg="black",
                             font=("Malgun Gothic", 44, "bold"))
        self.hold.pack()
        self.recent = tk.Label(self.root, text="", fg="#6b7280", bg="black",
                               font=("Malgun Gothic", 16), justify="left")
        self.recent.pack(pady=(6, 2))
        self.foot = tk.Label(self.root, text="X 사이클 무효 · N 메모 · P 일시정지 · Q 중단",
                             fg="#4b5563", bg="black", font=("Malgun Gothic", 14))
        self.foot.pack(pady=(0, 14))

    def log(self, msg):
        print(msg, flush=True)

    # ── 시작 ──
    def start(self):
        self.center.config(text="음성 준비 중…", font=("Malgun Gothic", 48))
        self.root.update()
        backend = self.voice.prepare(ALL_TEXTS, self.log)
        self.voice_backend = backend

        if self.args.collector_cmd and not self.args.dry_run:
            self._start_collector()

        self.center.config(text="곧 시작합니다", font=("Malgun Gothic", 72, "bold"))
        self.root.update()
        time.sleep(3.0)

        self.t0 = time.time()
        self.perf0 = time.perf_counter()
        self.voice.beep(1)
        self._record(0.0, "start", self.cues[0][2], self.cues[0][3], 0)
        self.current = (self.cues[0][1], self.cues[0][2], self.cues[0][3],
                        0, self.cues[0][5], 0.0)
        self.idx = 1
        self.root.after(20, self._tick)
        self.root.mainloop()

    def _start_collector(self):
        # 세션 폴더는 실행 시각으로 정해지므로 사용자가 미리 알 수 없다.
        # {session_dir} 자리표시자를 실제 경로로 바꿔 넘긴다.
        cmd = self.args.collector_cmd.replace("{session_dir}", self.out_dir)
        self.log(f"[*] 수집기 실행: {cmd}")
        self.collector = subprocess.Popen(cmd, shell=True)
        # 수집기가 포트를 열고 첫 프레임을 받을 시간을 준다 (문서 4.1)
        time.sleep(float(self.args.collector_warmup))

    # ── 시계 ──
    def _elapsed(self) -> float:
        return (time.perf_counter() - self.perf0) - self.pause_shift

    # ── 메인 루프 ──
    def _tick(self):
        if self.finished:
            return
        if self.paused_at is not None:
            self._paint()
            self.root.after(50, self._tick)
            return

        now_s = self._elapsed()
        while self.idx < len(self.cues) and now_s >= self.cues[self.idx][0]:
            t_plan, phase, label, text, cycle, hold = self.cues[self.idx]
            actual = self._elapsed()
            err = abs(actual - t_plan) * 1000.0
            self.max_error_ms = max(self.max_error_ms, err)
            if phase == "end":
                self.voice.beep(2)
            else:
                self.voice.beep(1)
            self.voice.say(text)
            self._record(t_plan, phase, label, text, cycle, actual)
            self.current = (phase, label, text, cycle, hold, t_plan)
            self.idx += 1
            if phase == "end":
                self._finish("완료")
                return

        self._paint()
        self.root.after(20, self._tick)

    def _paint(self):
        el = self._elapsed()
        remain = max(self.total_s - el, 0.0)
        phase, label, text, cycle, hold, start_s = self.current
        cyc_txt = f"c{cycle}" if cycle else "—"
        self.top.config(text=(f"{self.args.config} · {self.args.subject} · {self.args.mode}"
                              f"   경과 {self._mmss(el)} / 남은 {self._mmss(remain)}   {cyc_txt}"
                              + ("   [일시정지]" if self.paused_at is not None else "")))
        w = self.bar.winfo_width() or 1
        self.bar.delete("all")
        self.bar.create_rectangle(0, 0, w * min(el / self.total_s, 1.0), 14,
                                  fill="#4f46e5", width=0)
        self.center.config(text=text, font=("Malgun Gothic", 84, "bold"))
        left = start_s + hold - el
        self.hold.config(text=f"{int(left)}초" if hold and left > 0 else "")
        self.recent.config(text="\n".join(
            f"{r['t_session_s']:>6.1f}s  {r['phase']:<6} {r['text']}" for r in self.rows[-3:]))

    @staticmethod
    def _mmss(s):
        s = max(int(s), 0)
        return f"{s // 60:02d}:{s % 60:02d}"

    # ── 기록 ──
    def _record(self, t_plan, phase, label, text, cycle, t_actual=None):
        now = time.time()
        self.rows.append({
            "t_pc_unix": f"{now:.6f}",
            "t_iso": datetime.fromtimestamp(now, timezone.utc).astimezone().isoformat(),
            "t_session_s": round(t_plan, 3),
            "t_actual_s": round(t_actual if t_actual is not None else t_plan, 3),
            "cycle": cycle,
            "label": label,
            "phase": phase,
            "text": text,
            "valid": 1,
        })

    # ── 키 ──
    def _on_key(self, ev):
        k = (ev.char or "").lower()
        if k == "x":
            cyc = self.current[3]
            if cyc:
                self.invalid.add(cyc)
                for r in self.rows:
                    if r["cycle"] == cyc:
                        r["valid"] = 0
                self.foot.config(text=f"c{cyc} 무효 처리됨 · X 무효 · N 메모 · P 일시정지 · Q 중단")
        elif k == "n":
            self._prompt_note()
        elif k == "p":
            self._toggle_pause()
        elif k == "q":
            # 10분짜리 세션을 오타 한 번으로 날리지 않도록 한 번 더 묻는다.
            # (한글 입력 상태에서는 ㅂ이 q, ㅔ가 p라 실수로 눌리기 쉽다)
            self._confirm_quit()

    def _confirm_quit(self):
        win = tk.Toplevel(self.root)
        win.title("중단 확인")
        win.attributes("-topmost", True)
        tk.Label(win, text="세션을 중단하고 저장할까요?",
                 font=("Malgun Gothic", 16)).pack(padx=24, pady=(18, 8))
        tk.Label(win, text="Enter = 중단 / Esc = 계속 진행",
                 fg="#6b7280", font=("Malgun Gothic", 12)).pack(padx=24, pady=(0, 12))
        win.bind("<Return>", lambda _=None: (win.destroy(), self._finish("사용자 중단")))
        win.bind("<Escape>", lambda _=None: win.destroy())
        win.focus_force()

    def _prompt_note(self):
        win = tk.Toplevel(self.root)
        win.title("메모")
        win.attributes("-topmost", True)
        tk.Label(win, text="메모 한 줄:", font=("Malgun Gothic", 14)).pack(padx=12, pady=(12, 4))
        entry = tk.Entry(win, width=50, font=("Malgun Gothic", 14))
        entry.pack(padx=12, pady=4)
        entry.focus_set()

        def submit(_=None):
            txt = entry.get().strip()
            if txt:
                now = time.time()
                self.notes.append({"t_pc_unix": f"{now:.6f}",
                                   "t_session_s": round(self._elapsed(), 3), "text": txt})
                self._record(round(self._elapsed(), 3), "note", "note", txt, self.current[3])
            win.destroy()
        entry.bind("<Return>", submit)
        tk.Button(win, text="저장", command=submit).pack(pady=(4, 12))

    def _toggle_pause(self):
        if self.paused_at is None:
            self.paused_at = time.perf_counter()
            self.voice.say(TEXT_PAUSE)
            self._record(round(self._elapsed(), 3), "pause", "pause", TEXT_PAUSE, self.current[3])
        else:
            self.pause_shift += time.perf_counter() - self.paused_at
            self.paused_at = None
            self.voice.say(TEXT_RESUME)
            cyc = self.current[3]
            # 재개 시 현재 사이클을 처음부터 다시 (문서 4.4)
            if cyc:
                self.restarted.add(cyc)
                first = next((i for i, c in enumerate(self.cues) if c[4] == cyc), None)
                if first is not None:
                    self.pause_shift += self._elapsed() - self.cues[first][0]
                    self.idx = first
            self._record(round(self._elapsed(), 3), "resume", "resume", TEXT_RESUME, cyc)

    # ── 종료 ──
    def _finish(self, why):
        if self.finished:
            return
        self.finished = True
        t_end = time.time()
        os.makedirs(self.out_dir, exist_ok=True)

        cues_path = os.path.join(self.out_dir, "cues.csv")
        with open(cues_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["t_pc_unix", "t_iso", "t_session_s", "t_actual_s",
                                              "cycle", "label", "phase", "text", "valid"])
            w.writeheader()
            w.writerows(self.rows)

        # 점검은 수집기가 파일을 닫은 뒤에 해야 한다. 강제 종료하면 버퍼가 남고
        # meta.json이 안 써지므로, 스스로 끝나기를 기다린 뒤 마지막에만 끊는다.
        if self.collector and self.collector.poll() is None:
            self.log("[*] 수집기가 스스로 끝나기를 기다리는 중 (최대 90초)…")
            try:
                self.collector.wait(timeout=90)
            except subprocess.TimeoutExpired:
                self.log("[!] 수집기가 끝나지 않아 종료합니다 — 파일이 잘렸을 수 있습니다")
                self.collector.terminate()

        meta = {
            "session_id": os.path.basename(self.out_dir),
            "mode": self.args.mode,
            "session_no": self.args.session_no,
            "subject": self.args.subject,
            "config_id": self.args.config,
            "orientation": self.args.orientation,
            "furniture_note": self.args.furniture_note,
            "mat_position": self.args.mat_position,
            "chair_position": self.args.chair_position,
            "spotter_inside": bool(self.args.spotter_inside),
            "t0_pc_unix": self.t0,
            "t_end_pc_unix": t_end,
            "invalid_cycles": sorted(self.invalid),
            "restarted_cycles": sorted(self.restarted),
            "notes": self.notes,
            "voice_backend": getattr(self, "voice_backend", "none"),
            "max_cue_error_ms": round(self.max_error_ms, 1),
            "finish_reason": why,
            "cycles": [{"cycle": i + 1, "label": lb} for i, lb in enumerate(self.cycle_labels)],
        }
        if not self.args.dry_run:
            meta["checks"] = run_checks(self.out_dir, self.t0, t_end, self.rows)
        with open(os.path.join(self.out_dir, "session_meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        self.log(f"\n[*] {why} — cues {len(self.rows)}개, 큐 오차 최대 {self.max_error_ms:.1f}ms")
        self.log(f"    저장: {self.out_dir}")
        for line in format_checks(meta.get("checks")):
            self.log("    " + line)
        try:
            self.root.destroy()
        except Exception:
            pass


# ──────────────────────────────────────────────
# 종료 시 자동 점검 (문서 4.6) — 수집기 출력을 읽어 확인
# ──────────────────────────────────────────────
def run_checks(session_dir: str, t0: float, t_end: float, cue_rows) -> dict:
    out = {"available": False}
    synced = os.path.join(session_dir, "synced.csv")
    if not os.path.exists(synced):
        out["note"] = "수집기 출력(synced.csv)이 세션 폴더에 없어 점검을 건너뜀"
        return out

    duration = max(t_end - t0, 1e-6)
    per_rx, pc_times, seqs = {}, [], []
    len_bad = 0
    gains = []
    with open(synced, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                seqs.append(int(row["seq"]))
            except (KeyError, ValueError):
                pass
            for rx in (1, 2, 3, 4):
                if row.get(f"rx{rx}") not in (None, "", "0"):
                    per_rx[rx] = per_rx.get(rx, 0) + 1
                    t = row.get(f"rx{rx}_pc_time")
                    if t:
                        pc_times.append(float(t))

    rates = {f"rx{rx}": round(per_rx.get(rx, 0) / duration, 1) for rx in (1, 2, 3, 4)}
    full_rows = 0
    with open(synced, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if all(row.get(f"rx{rx}") not in (None, "", "0") for rx in (1, 2, 3, 4)):
                full_rows += 1
    total_rows = max(len(seqs), 1)

    pc_times.sort()
    warns = []
    for name, hz in rates.items():
        if hz < 30.0:
            warns.append(f"{name} 수신률 {hz}Hz < 30Hz")
    if full_rows * 100.0 / total_rows < 95.0:
        warns.append(f"4노드 완전행 {full_rows*100.0/total_rows:.1f}% < 95%")
    if seqs != sorted(seqs):
        warns.append("seq가 단조 증가하지 않음")
    if not pc_times or pc_times[0] > t0 - 10 or pc_times[-1] < t_end + 10:
        warns.append("큐 t0 앞뒤로 10초 이상의 프레임 여유가 없음")
    max_err = max((abs(r["t_actual_s"] - r["t_session_s"]) for r in cue_rows), default=0.0)
    if max_err * 1000.0 >= 50.0:
        warns.append(f"큐 시각 오차 {max_err*1000:.0f}ms >= 50ms")

    out.update({
        "available": True,
        "duration_s": round(duration, 1),
        "rates_hz": rates,
        "full_row_pct": round(full_rows * 100.0 / total_rows, 1),
        "seq_monotonic": seqs == sorted(seqs),
        "csi_len_violations": len_bad,
        "frames_before_t0": bool(pc_times and pc_times[0] < t0),
        "frames_after_end": bool(pc_times and pc_times[-1] > t_end),
        "max_cue_error_ms": round(max_err * 1000.0, 1),
        "warnings": warns,
    })
    return out


def format_checks(checks) -> list[str]:
    if not checks:
        return []
    if not checks.get("available"):
        return [checks.get("note", "점검 없음")]
    lines = [f"수신률 {checks['rates_hz']} · 완전행 {checks['full_row_pct']}% "
             f"· 큐 오차 최대 {checks['max_cue_error_ms']}ms"]
    lines += [f"[!] {w}" for w in checks["warnings"]] or ["점검 전부 통과"]
    return lines


# ──────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="낙상 데이터 수집 큐 스크립트")
    ap.add_argument("--mode", choices=["event", "life", "empty"], default="event")
    ap.add_argument("--session-no", type=int, default=1)
    ap.add_argument("--subject", default="A")
    ap.add_argument("--config", default="o1_f01", help="배치/가구 구성 id (예: o1_f03)")
    ap.add_argument("--minutes", type=float, default=15.0, help="empty 모드 길이")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                  "data", "collect"))
    ap.add_argument("--collector-cmd", default=None,
                    help="수집기를 서브프로세스로 띄울 명령. {session_dir}는 실제 세션 폴더로 치환된다 "
                         "(없으면 수집기가 이미 돌고 있다고 가정)")
    ap.add_argument("--collector-warmup", type=float, default=10.0,
                    help="수집기 기동 후 첫 삐까지 기다릴 시간(초). 점검이 t0 앞 10초 여유를 "
                         "요구하므로 이보다 줄이지 말 것 (실제 여유 = 이 값 + 3초)")
    ap.add_argument("--orientation", type=int, default=1)
    ap.add_argument("--furniture-note", default="")
    ap.add_argument("--mat-position", default="")
    ap.add_argument("--chair-position", default="")
    ap.add_argument("--spotter-inside", action="store_true")
    ap.add_argument("--windowed", action="store_true", help="전체화면 대신 창 모드 (개발용)")
    ap.add_argument("--mute", action="store_true",
                    help="소리 없이 리허설 (합성·기록·화면은 그대로)")
    ap.add_argument("--dry-run", action="store_true", help="수집기 없이 타이머·음성·화면만")
    args = ap.parse_args()

    cues, total_s, cycle_labels = build_cues(args.mode, args.session_no, args.minutes)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_id = f"{args.mode}_s{args.session_no:02d}_{args.subject}_{stamp}"
    out_dir = os.path.join(args.out, args.config, session_id)
    os.makedirs(out_dir, exist_ok=True)

    print(f"[*] 세션 {session_id}  ({total_s/60:.1f}분, 큐 {len(cues)}개)")
    print(f"    저장: {out_dir}")
    if args.mode == "event":
        print("    사이클: " + ", ".join(f"c{i+1}={lb}" for i, lb in enumerate(cycle_labels)))
    print("    현장 규칙: 방 안에는 피험자 1명 · 낙상은 항상 매트 위 · 실패한 사이클은 즉시 X")

    SessionRunner(args, cues, total_s, cycle_labels, out_dir).start()


if __name__ == "__main__":
    main()
