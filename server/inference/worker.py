"""C6 calibration and Random-Forest inference worker."""
from __future__ import annotations

import logging
import os
import time
from collections import OrderedDict

import numpy as np
import pymysql

from c6_pipeline import (
    C6Model, CalibrationProfile, EpisodeTracker, amplitude_from_storage,
    interpolate_grid, motion_energy,
)

LOG = logging.getLogger("c6-worker")
MODEL_PATH = os.getenv("MODEL_PATH", "/models/final_bundle.joblib")
CALIBRATION_SLOTS = int(os.getenv("CALIBRATION_SLOTS", "19800"))
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "0.3"))


def db_connect():
    while True:
        try:
            return pymysql.connect(
                host=os.getenv("DB_HOST", "db"), user=os.getenv("DB_USER", "csi_c6"),
                password=os.getenv("DB_PASSWORD", "csi_c6_dev"), database=os.getenv("DB_NAME", "csi_c6"),
                autocommit=True, cursorclass=pymysql.cursors.DictCursor,
            )
        except Exception as exc:
            LOG.warning("DB wait: %s", exc)
            time.sleep(2)


def build_grid(rows: list[dict]) -> tuple[np.ndarray, np.ndarray, list[int]]:
    slots: OrderedDict[int, dict[int, np.ndarray]] = OrderedDict()
    for row in rows:
        seq, rx = int(row["seq"]), int(row["rx_id"])
        slots.setdefault(seq, {})[rx] = np.asarray(
            amplitude_from_storage(row["csi"], float(row["gain"])), dtype=np.float32
        )
    sequences = list(slots)
    amplitude = np.zeros((len(sequences), 4, 246), dtype=np.float32)
    mask = np.zeros((len(sequences), 4), dtype=bool)
    for index, seq in enumerate(sequences):
        for rx, values in slots[seq].items():
            amplitude[index, rx - 1] = values
            mask[index, rx - 1] = True
    return amplitude, mask, sequences


def load_rows(db, *, session_id=None, limit=None):
    with db.cursor() as cur:
        if session_id is not None:
            cur.execute("SELECT seq,rx_id,gain,csi FROM reports WHERE session_id=%s ORDER BY id", (session_id,))
        else:
            cur.execute("SELECT seq,rx_id,gain,csi FROM reports WHERE mode='INFERENCE' ORDER BY id DESC LIMIT %s", (limit or 1200,))
        rows = cur.fetchall()
    return rows if session_id is not None else list(reversed(rows))


def set_health(db, health, error=None, model_sha=None):
    with db.cursor() as cur:
        cur.execute("UPDATE system_state SET health=%s,last_error=%s,model_sha=COALESCE(%s,model_sha),updated_ts=%s WHERE singleton_id=1",
                    (health, error, model_sha, time.time()))


def fail_calibration(db, model: C6Model, session_id, message: str):
    """게이트 탈락은 재시도해도 같은 데이터로 같은 결과다 — FAILED로 기록하고
    STANDBY로 복귀한다. 기존 VALID 보정(active_calibration_id)은 그대로 유지."""
    now = time.time()
    with db.cursor() as cur:
        cur.execute("""INSERT INTO calibrations
          (session_id,status,started_ts,completed_ts,model_sha,error_message)
          VALUES (%s,'FAILED',(SELECT start_ts FROM sessions WHERE session_id=%s),%s,%s,%s)""",
          (session_id, session_id, now, model.sha256, message[:1000]))
        cur.execute("UPDATE sessions SET end_ts=%s WHERE session_id=%s", (now, session_id))
        cur.execute("""UPDATE system_state SET mode='STANDBY',health='WARNING',active_session_id=NULL,
          last_error=%s,updated_ts=%s WHERE singleton_id=1""", (message[:1000], now))
    LOG.warning("calibration failed session=%s: %s", session_id, message)


def finish_calibration(db, model: C6Model, state: dict):
    session_id = state["active_session_id"]
    if not session_id:
        raise ValueError("CALIBRATION mode has no active session")
    # 슬롯이 찰 때까지는 값싼 COUNT만 — 전체 로드(수십 MB)는 완료 시점에 한 번
    with db.cursor() as cur:
        cur.execute("SELECT COUNT(DISTINCT seq) AS n FROM reports WHERE session_id=%s", (session_id,))
        if int(cur.fetchone()["n"]) < CALIBRATION_SLOTS:
            return
    rows = load_rows(db, session_id=session_id)
    try:
        amplitude, mask, sequences = build_grid(rows)
        distinct_slots = len(sequences)
        per_rx_rate = mask.sum(axis=0) / max(distinct_slots, 1) * 33.0
        if np.any(per_rx_rate < 25.0):
            raise ValueError(f"calibration requires every RX at >=25 Hz, got {per_rx_rate.tolist()}")
        result = interpolate_grid(amplitude, mask)
        if result.quality == "DEGRADED":
            raise ValueError("calibration data is degraded")
        profile = model.calibrate(result.amplitude)
        energies = [motion_energy(result.amplitude[i:i + 200]) for i in range(0, len(amplitude) - 199, 33)]
        empty_q95 = float(np.quantile(energies, 0.95))
    except ValueError as exc:
        fail_calibration(db, model, session_id, str(exc))
        return
    now = time.time()
    with db.cursor() as cur:
        cur.execute("""INSERT INTO calibrations
          (session_id,status,started_ts,completed_ts,model_sha,window_count,threshold,
           score_median,score_quantile,empty_motion_q95,dsd)
          VALUES (%s,'VALID',(SELECT start_ts FROM sessions WHERE session_id=%s),%s,%s,%s,%s,%s,%s,%s,%s)""",
          (session_id, session_id, now, model.sha256, profile.window_count, profile.threshold,
           profile.median_score, profile.quantile_score, empty_q95, profile.dsd.astype("<f4").tobytes()))
        calibration_id = cur.lastrowid
        cur.execute("UPDATE sessions SET end_ts=%s WHERE session_id=%s", (now, session_id))
        cur.execute("""UPDATE system_state SET mode='STANDBY',health='OK',active_session_id=NULL,
          active_calibration_id=%s,model_sha=%s,last_error=NULL,updated_ts=%s WHERE singleton_id=1""",
          (calibration_id, model.sha256, now))
    LOG.info("calibration complete id=%s windows=%d threshold=%.6f", calibration_id, profile.window_count, profile.threshold)


def load_profile(db, calibration_id):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM calibrations WHERE calibration_id=%s AND status='VALID'", (calibration_id,))
        row = cur.fetchone()
    if not row:
        raise ValueError("a valid calibration is required")
    dsd = np.frombuffer(row["dsd"], dtype="<f4").reshape(4, 246).copy()
    profile = CalibrationProfile(dsd, float(row["threshold"]), float(row["score_median"]),
                                 float(row["score_quantile"]), int(row["window_count"]))
    return profile, row


def run_inference(db, model, state, tracker, last_seq):
    calibration_id = state["active_calibration_id"]
    profile, calibration = load_profile(db, calibration_id)
    amplitude, mask, sequences = build_grid(load_rows(db, limit=1600))
    if len(sequences) < 200 or sequences[-1] == last_seq:
        return last_seq
    amplitude, mask, sequences = amplitude[-200:], mask[-200:], sequences[-200:]
    result = interpolate_grid(amplitude, mask)
    if result.quality == "DEGRADED":
        set_health(db, "DEGRADED", f"missing gap exceeds 15 slots (max={result.max_gap})")
        return sequences[-1]
    probability = model.score(result.amplitude, profile)
    energy = motion_energy(result.amplitude)
    now = time.time()
    hot = probability >= profile.threshold
    _, completed = tracker.update(ts=now, probability=probability, threshold=profile.threshold,
                                  motion_energy=energy, empty_motion_q95=float(calibration["empty_motion_q95"]))
    with db.cursor() as cur:
        cur.execute("""INSERT IGNORE INTO inference_results
          (ts,seq_start,seq_end,probability,threshold,quality,hot,motion_energy,calibration_id,model_sha)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
          (now, sequences[0], sequences[-1], probability, profile.threshold, result.quality, hot,
           energy, calibration_id, model.sha256))
        if completed:
            cur.execute("""INSERT INTO episodes
              (start_ts,end_ts,peak_probability,status,reason,calibration_id,model_sha)
              VALUES (%s,%s,%s,%s,%s,%s,%s)""",
              (completed.start_ts, now, completed.peak_probability, completed.status,
               completed.reason, calibration_id, model.sha256))
    set_health(db, result.quality, model_sha=model.sha256)
    return sequences[-1]


def main():
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    model = C6Model.load(MODEL_PATH)
    db, tracker, last_seq = db_connect(), EpisodeTracker(), None
    set_health(db, "OK", model_sha=model.sha256)
    while True:
        try:
            db.ping(reconnect=True)
            with db.cursor() as cur:
                cur.execute("SELECT * FROM system_state WHERE singleton_id=1")
                state = cur.fetchone()
            if state["mode"] == "CALIBRATION":
                finish_calibration(db, model, state)
            elif state["mode"] == "INFERENCE":
                last_seq = run_inference(db, model, state, tracker, last_seq)
        except Exception as exc:
            LOG.exception("worker cycle failed")
            set_health(db, "ERROR", str(exc)[:1000], model.sha256)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
