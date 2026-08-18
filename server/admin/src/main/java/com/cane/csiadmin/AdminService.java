package com.cane.csiadmin;

import java.util.List;
import java.util.Map;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * 대시보드 집계와 세션 조작.
 * reports는 blob이 커서 JPA 엔티티로 다루지 않고 집계 쿼리만 JdbcTemplate로 수행한다.
 */
@Service
public class AdminService {

    private final JdbcTemplate jdbc;
    private final SessionRepository sessions;

    public AdminService(JdbcTemplate jdbc, SessionRepository sessions) {
        this.jdbc = jdbc;
        this.sessions = sessions;
    }

    /** 대시보드 통계 — 최근 60초 창은 idx_recv(recv_ts) 인덱스를 탄다. */
    public Map<String, Object> stats() {
        double now = System.currentTimeMillis() / 1000.0;
        double window = 60.0;

        Long recent = jdbc.queryForObject(
                "SELECT COUNT(*) FROM reports WHERE recv_ts > ?", Long.class, now - window);
        List<Map<String, Object>> perRx = jdbc.queryForList(
                "SELECT rx_id, COUNT(*) AS cnt, ROUND(AVG(rssi),1) AS rssi "
                + "FROM reports WHERE recv_ts > ? GROUP BY rx_id ORDER BY rx_id", now - window);
        Map<String, Object> latest = jdbc.queryForMap(
                "SELECT COALESCE(MAX(seq),0) AS max_seq, COALESCE(MAX(recv_ts),0) AS last_ts FROM reports");
        // 전체 행수는 information_schema 추정치 (정확 COUNT는 수백만 행에서 느리다)
        Map<String, Object> size = jdbc.queryForMap(
                "SELECT table_rows AS approx_rows, ROUND((data_length+index_length)/1024/1024) AS size_mb "
                + "FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name='reports'");

        SessionEntity active = sessions.findFirstByEndTsIsNullOrderBySessionIdDesc().orElse(null);
        Map<String, Object> state = jdbc.queryForMap("SELECT * FROM system_state WHERE singleton_id=1");
        double lastTs = ((Number) latest.get("last_ts")).doubleValue();

        // 주의: Map.of는 null 값을 허용하지 않는다 (activeSession이 null일 수 있음)
        var out = new java.util.HashMap<String, Object>();
        out.put("rate", Math.round(recent / window * 10.0) / 10.0);
        out.put("perRx", perRx);
        out.put("maxSeq", latest.get("max_seq"));
        out.put("lastAgeSec", lastTs > 0 ? Math.round((now - lastTs) * 10.0) / 10.0 : -1);
        out.put("approxRows", size.get("approx_rows"));
        out.put("sizeMb", size.get("size_mb"));
        out.put("mode", state.get("mode"));
        out.put("health", state.get("health"));
        out.put("modelSha", state.get("model_sha"));
        out.put("calibrationId", state.get("active_calibration_id"));
        out.put("lastError", state.get("last_error"));
        out.put("activeSession", active != null
                ? Map.of("id", active.getSessionId(), "label", active.displayLabel())
                : null);
        return out;
    }

    /** 세션별 행수 (목록 화면용). */
    public Map<Integer, Long> sessionRowCounts() {
        var counts = new java.util.HashMap<Integer, Long>();
        jdbc.query("SELECT session_id, COUNT(*) AS cnt FROM reports "
                   + "WHERE session_id IS NOT NULL GROUP BY session_id",
                   rs -> { counts.put(rs.getInt(1), rs.getLong(2)); });
        return counts;
    }

    @Transactional
    public SessionEntity startSession(String location, String behavior, String person) {
        ensureStandby();
        sessions.findFirstByEndTsIsNullOrderBySessionIdDesc().ifPresent(active -> {
            throw new IllegalStateException("이미 수집 중인 세션이 있습니다: #" + active.getSessionId());
        });
        SessionEntity s = new SessionEntity();
        s.setLocation(location == null ? "" : location.trim());
        s.setBehavior(behavior == null ? "" : behavior.trim());
        s.setPerson(person == null ? "" : person.trim());
        s.setMode("CAPTURE");
        s.setStartTs(System.currentTimeMillis() / 1000.0);
        SessionEntity saved = sessions.save(s);
        jdbc.update("UPDATE system_state SET mode='CAPTURE',active_session_id=?,health='OK',updated_ts=? WHERE singleton_id=1",
                saved.getSessionId(), System.currentTimeMillis() / 1000.0);
        return saved;
    }

    @Transactional
    public void stopSession(int sessionId) {
        SessionEntity s = sessions.findById(sessionId)
                .orElseThrow(() -> new IllegalArgumentException("세션 없음: #" + sessionId));
        if (s.isActive()) {
            s.setEndTs(System.currentTimeMillis() / 1000.0);
            sessions.save(s);
            jdbc.update("UPDATE system_state SET mode='STANDBY',active_session_id=NULL,updated_ts=? WHERE singleton_id=1",
                    System.currentTimeMillis() / 1000.0);
        }
    }

    /** 학습 데이터의 유일한 삭제 경로 — 관리자 수동 조작. 청크 삭제로 락 시간 분산. */
    @Transactional
    public long deleteSession(int sessionId) {
        SessionEntity s = sessions.findById(sessionId)
                .orElseThrow(() -> new IllegalArgumentException("세션 없음: #" + sessionId));
        if (s.isActive()) {
            throw new IllegalStateException("수집 중인 세션은 먼저 종료해야 합니다");
        }
        // 보정 세션은 calibrations가 FK로 참조한다. 활성 보정이거나 추론 기록이 참조하는
        // 보정을 만든 세션은 지울 수 없고, 그 외에는 보정 행까지 함께 지운다.
        Integer blocked = jdbc.queryForObject(
                "SELECT COUNT(*) FROM calibrations c WHERE c.session_id = ? AND ("
                + "c.calibration_id = (SELECT active_calibration_id FROM system_state WHERE singleton_id=1)"
                + " OR EXISTS (SELECT 1 FROM inference_results r WHERE r.calibration_id = c.calibration_id)"
                + " OR EXISTS (SELECT 1 FROM episodes e WHERE e.calibration_id = c.calibration_id))",
                Integer.class, sessionId);
        if (blocked != null && blocked > 0) {
            throw new IllegalStateException("세션 #" + sessionId
                    + "의 보정이 활성 상태이거나 추론 기록에서 참조 중이라 삭제할 수 없습니다");
        }
        jdbc.update("DELETE FROM calibrations WHERE session_id = ?", sessionId);
        long total = 0;
        int n;
        do {
            n = jdbc.update("DELETE FROM reports WHERE session_id = ? LIMIT 100000", sessionId);
            total += n;
        } while (n > 0);
        sessions.delete(s);
        return total;
    }

    // ---------- 수집 데이터 뷰어 ----------

    /** 세션 상세 요약: Rx별 행수·seq 범위·시간 범위. */
    public Map<String, Object> sessionSummary(int sessionId) {
        Map<String, Object> agg = jdbc.queryForMap(
                "SELECT COUNT(*) AS total, COALESCE(MIN(seq),0) AS seq_min, COALESCE(MAX(seq),0) AS seq_max, "
                + "COALESCE(MIN(recv_ts),0) AS ts_min, COALESCE(MAX(recv_ts),0) AS ts_max "
                + "FROM reports WHERE session_id = ?", sessionId);
        List<Map<String, Object>> perRx = jdbc.queryForList(
                "SELECT rx_id, COUNT(*) AS cnt, ROUND(AVG(rssi),1) AS rssi "
                + "FROM reports WHERE session_id = ? GROUP BY rx_id ORDER BY rx_id", sessionId);
        var out = new java.util.HashMap<String, Object>(agg);
        out.put("perRx", perRx);
        return out;
    }

    /** 세션의 리포트 행 페이지 (blob 제외). */
    public List<Map<String, Object>> sessionRows(int sessionId, int page, int pageSize) {
        return jdbc.queryForList(
                "SELECT id, recv_ts, seq, rx_id, rssi, ROUND(gain,3) AS gain, csi_len "
                + "FROM reports WHERE session_id = ? ORDER BY id LIMIT ? OFFSET ?",
                sessionId, pageSize, page * pageSize);
    }

    /**
     * 단일 리포트의 CSI 진폭 배열 (뷰어 차트용).
     * int8 I/Q 쌍 → sqrt(I²+Q²) × compensate_gain. gain=1.0은 미보상 표식이라 그대로 곱한다.
     */
    public Map<String, Object> reportCsi(long reportId) {
        return jdbc.queryForObject(
                "SELECT seq, rx_id, rssi, gain, csi FROM reports WHERE id = ?",
                (rs, i) -> {
                    byte[] blob = rs.getBytes("csi");
                    double gain = rs.getDouble("gain");
                    double[] amps = new double[246];
                    int out = 0;
                    for (int k = 0; k < 256; k++) {
                        if (k <= 4 || k == 128 || k >= 252) continue;
                        int re = (int) (gain * blob[2 * k]);
                        int im = (int) (gain * blob[2 * k + 1]);
                        amps[out++] = Math.round(Math.hypot(re, im) * 10.0) / 10.0;
                    }
                    return Map.of(
                            "seq", rs.getInt("seq"),
                            "rxId", rs.getInt("rx_id"),
                            "rssi", rs.getInt("rssi"),
                            "gain", Math.round(gain * 1000.0) / 1000.0,
                            "amplitudes", amps);
                },
                reportId);
    }

    /**
     * 스펙트로그램 창: 세션·Rx의 리포트를 시간순으로 offset부터 count개,
     * 각 리포트의 서브캐리어 진폭 배열(gain 보상)로 반환한다.
     * 창 크기를 고정해 세션이 아무리 커도 응답량이 일정하다.
     */
    public Map<String, Object> sessionSpectrogram(int sessionId, int rxId, int offset, int count) {
        Long total = jdbc.queryForObject(
                "SELECT COUNT(*) FROM reports WHERE session_id = ? AND rx_id = ?",
                Long.class, sessionId, rxId);

        var seqs = new java.util.ArrayList<Integer>();
        var amps = new java.util.ArrayList<double[]>();
        jdbc.query("SELECT seq, gain, csi FROM reports WHERE session_id = ? AND rx_id = ? "
                   + "ORDER BY id LIMIT ? OFFSET ?",
                rs -> {
                    byte[] blob = rs.getBytes("csi");
                    double gain = rs.getDouble("gain");
                    double[] a = new double[246];
                    int out = 0;
                    for (int k = 0; k < 256; k++) {
                        if (k <= 4 || k == 128 || k >= 252) continue;
                        int re = (int) (gain * blob[2 * k]);
                        int im = (int) (gain * blob[2 * k + 1]);
                        a[out++] = Math.round(Math.hypot(re, im) * 10.0) / 10.0;
                    }
                    seqs.add(rs.getInt("seq"));
                    amps.add(a);
                },
                sessionId, rxId, count, offset);

        return Map.of("total", total, "offset", offset,
                      "seqs", seqs, "amps", amps);
    }

    public List<Map<String, Object>> inferenceResults(int minutes) {
        double since = System.currentTimeMillis() / 1000.0 - minutes * 60L;
        return jdbc.queryForList(
                "SELECT ts, seq_start, seq_end, probability, threshold, quality, hot, motion_energy FROM inference_results "
                + "WHERE ts > ? ORDER BY ts", since);
    }

    public Map<String, Object> systemState() {
        return jdbc.queryForMap("SELECT * FROM system_state WHERE singleton_id=1");
    }

    @Transactional
    public long startCalibration(String location) {
        ensureStandby();
        double now = System.currentTimeMillis() / 1000.0;
        jdbc.update("INSERT INTO sessions(mode,location,behavior,person,start_ts,retained) VALUES('CALIBRATION',?,'empty-room','',?,TRUE)",
                location == null ? "" : location.trim(), now);
        Long id = jdbc.queryForObject("SELECT LAST_INSERT_ID()", Long.class);
        jdbc.update("UPDATE system_state SET mode='CALIBRATION',health='OK',active_session_id=?,last_error=NULL,updated_ts=? WHERE singleton_id=1", id, now);
        return id;
    }

    public void startInference() {
        ensureStandby();
        Integer count = jdbc.queryForObject("SELECT COUNT(*) FROM calibrations WHERE calibration_id=(SELECT active_calibration_id FROM system_state WHERE singleton_id=1) AND status='VALID'", Integer.class);
        if (count == null || count == 0) throw new IllegalStateException("유효한 C6 빈방 보정이 먼저 필요합니다");
        jdbc.update("UPDATE system_state SET mode='INFERENCE',health='OK',last_error=NULL,updated_ts=? WHERE singleton_id=1", System.currentTimeMillis() / 1000.0);
    }

    public void standby() {
        double now = System.currentTimeMillis() / 1000.0;
        jdbc.update("UPDATE sessions SET end_ts=? WHERE session_id=(SELECT active_session_id FROM system_state WHERE singleton_id=1) AND end_ts IS NULL", now);
        jdbc.update("UPDATE system_state SET mode='STANDBY',active_session_id=NULL,health='OK',last_error=NULL,updated_ts=? WHERE singleton_id=1", now);
    }

    private void ensureStandby() {
        String mode = jdbc.queryForObject("SELECT mode FROM system_state WHERE singleton_id=1", String.class);
        if (!"STANDBY".equals(mode)) throw new IllegalStateException("현재 모드를 먼저 중지해야 합니다: " + mode);
    }

    public List<Map<String, Object>> episodes(int limit) {
        return jdbc.queryForList("SELECT * FROM episodes ORDER BY episode_id DESC LIMIT ?", Math.min(Math.max(limit, 1), 500));
    }
}
