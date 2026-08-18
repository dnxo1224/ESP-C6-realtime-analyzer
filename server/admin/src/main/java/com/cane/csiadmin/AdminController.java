package com.cane.csiadmin;

import java.time.Instant;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.util.List;
import java.util.Map;
import org.springframework.stereotype.Controller;
import org.springframework.ui.Model;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.ResponseBody;
import org.springframework.web.servlet.mvc.support.RedirectAttributes;

@Controller
public class AdminController {

    private static final DateTimeFormatter TS_FMT =
            DateTimeFormatter.ofPattern("MM-dd HH:mm:ss").withZone(ZoneId.systemDefault());
    private static final int PAGE_SIZE = 200;

    private final AdminService service;
    private final SessionRepository sessions;

    public AdminController(AdminService service, SessionRepository sessions) {
        this.service = service;
        this.sessions = sessions;
    }

    public record SessionRow(SessionEntity s, String start, String end, long rows) {}

    private static String fmt(double epochSec) {
        return epochSec > 0 ? TS_FMT.format(Instant.ofEpochMilli((long) (epochSec * 1000))) : "—";
    }

    // ---------- 페이지 ----------

    @GetMapping("/")
    public String dashboard() {
        return "dashboard";
    }

    @GetMapping("/healthz")
    @ResponseBody
    public String healthz() { return "ok"; }

    @GetMapping("/sessions")
    public String sessions(Model model) {
        Map<Integer, Long> counts = service.sessionRowCounts();
        List<SessionRow> rows = sessions.findAllByOrderBySessionIdDesc().stream()
                .map(s -> new SessionRow(
                        s, fmt(s.getStartTs()),
                        s.getEndTs() != null ? fmt(s.getEndTs()) : null,
                        counts.getOrDefault(s.getSessionId(), 0L)))
                .toList();
        model.addAttribute("rows", rows);
        return "sessions";
    }

    /** 수집 데이터 뷰어 — 세션 요약 + 리포트 행 페이지. */
    @GetMapping("/sessions/{id}")
    public String sessionDetail(@PathVariable int id,
                                @RequestParam(defaultValue = "0") int page, Model model) {
        SessionEntity s = sessions.findById(id)
                .orElseThrow(() -> new IllegalArgumentException("세션 없음: #" + id));
        Map<String, Object> summary = service.sessionSummary(id);
        List<Map<String, Object>> rows = service.sessionRows(id, page, PAGE_SIZE);

        long total = ((Number) summary.get("total")).longValue();
        double tsMin = ((Number) summary.get("ts_min")).doubleValue();
        double tsMax = ((Number) summary.get("ts_max")).doubleValue();

        model.addAttribute("s", s);
        model.addAttribute("summary", summary);
        model.addAttribute("rows", rows);
        model.addAttribute("tsRange", fmt(tsMin) + " ~ " + fmt(tsMax));
        model.addAttribute("durationSec", tsMax > tsMin ? Math.round(tsMax - tsMin) : 0);
        model.addAttribute("page", page);
        model.addAttribute("pageSize", PAGE_SIZE);
        model.addAttribute("lastPage", total == 0 ? 0 : (total - 1) / PAGE_SIZE);
        return "session_detail";
    }

    @GetMapping("/inference")
    public String inference() {
        return "inference";
    }

    /** 보정 관리 — 이력·임계값·재활성화. */
    @GetMapping("/calibrations")
    public String calibrations(Model model) {
        Map<String, Object> state = service.systemState();
        List<Map<String, Object>> rows = service.calibrationList().stream().map(c -> {
            var m = new java.util.HashMap<String, Object>(c);
            m.put("startedFmt", fmt(((Number) c.get("started_ts")).doubleValue()));
            Object done = c.get("completed_ts");
            m.put("completedFmt", done != null ? fmt(((Number) done).doubleValue()) : "—");
            return (Map<String, Object>) m;
        }).toList();
        model.addAttribute("rows", rows);
        model.addAttribute("activeId", state.get("active_calibration_id"));
        model.addAttribute("systemModelSha", state.get("model_sha"));
        model.addAttribute("mode", state.get("mode"));
        return "calibrations";
    }

    @PostMapping("/calibrations/{id}/activate")
    public String activateCalibration(@PathVariable long id, RedirectAttributes ra) {
        try {
            service.activateCalibration(id);
            ra.addFlashAttribute("msg", "보정 #" + id + " 활성화 — 다음 추론 시작부터 적용됩니다");
        } catch (IllegalStateException e) {
            ra.addFlashAttribute("err", e.getMessage());
        }
        return "redirect:/calibrations";
    }

    // ---------- 세션 조작 ----------

    @PostMapping("/sessions/start")
    public String start(@RequestParam String location, @RequestParam String behavior,
                        @RequestParam String person, RedirectAttributes ra) {
        try {
            SessionEntity s = service.startSession(location, behavior, person);
            ra.addFlashAttribute("msg", "수집 시작: #" + s.getSessionId() + " [" + s.displayLabel() + "]");
        } catch (IllegalStateException e) {
            ra.addFlashAttribute("err", e.getMessage());
        }
        return "redirect:/sessions";
    }

    @PostMapping("/sessions/{id}/stop")
    public String stop(@PathVariable int id, RedirectAttributes ra) {
        service.stopSession(id);
        ra.addFlashAttribute("msg", "수집 종료: #" + id);
        return "redirect:/sessions";
    }

    @PostMapping("/sessions/{id}/delete")
    public String delete(@PathVariable int id, RedirectAttributes ra) {
        try {
            long n = service.deleteSession(id);
            ra.addFlashAttribute("msg", "세션 #" + id + " 삭제 완료 (" + n + "행)");
        } catch (IllegalStateException e) {
            ra.addFlashAttribute("err", e.getMessage());
        }
        return "redirect:/sessions";
    }

    // ---------- JSON API ----------

    @GetMapping("/api/stats")
    @ResponseBody
    public Map<String, Object> stats() {
        return service.stats();
    }

    @GetMapping("/api/sessions/{id}/spectrogram")
    @ResponseBody
    public Map<String, Object> spectrogram(@PathVariable int id,
                                           @RequestParam(defaultValue = "1") int rx,
                                           @RequestParam(defaultValue = "0") int offset,
                                           @RequestParam(defaultValue = "200") int count) {
        return service.sessionSpectrogram(id, rx, Math.max(0, offset), Math.min(count, 400));
    }

    @GetMapping("/api/reports/{id}/csi")
    @ResponseBody
    public Map<String, Object> reportCsi(@PathVariable long id) {
        return service.reportCsi(id);
    }

    @GetMapping("/api/inference")
    @ResponseBody
    public List<Map<String, Object>> inferenceApi(@RequestParam(defaultValue = "60") int minutes) {
        return service.inferenceResults(minutes);
    }

    @GetMapping("/api/system")
    @ResponseBody
    public Map<String, Object> system() { return service.systemState(); }

    /** 라이브 진폭 스트림 — afterId 커서 증분 폴링. */
    @GetMapping("/api/live")
    @ResponseBody
    public Map<String, Object> live(@RequestParam(defaultValue = "1") int rx,
                                    @RequestParam(defaultValue = "0") long afterId,
                                    @RequestParam(defaultValue = "66") int limit) {
        return service.liveWindow(Math.min(Math.max(rx, 1), 4), afterId, Math.min(Math.max(limit, 1), 200));
    }

    @PostMapping("/api/control/calibration/start")
    @ResponseBody
    public Map<String, Object> calibrationStart(@RequestParam(defaultValue = "") String location) {
        return Map.of("sessionId", service.startCalibration(location), "mode", "CALIBRATION");
    }

    @PostMapping("/api/control/inference/start")
    @ResponseBody
    public Map<String, Object> inferenceStart() {
        service.startInference();
        return Map.of("mode", "INFERENCE");
    }

    @PostMapping("/api/control/standby")
    @ResponseBody
    public Map<String, Object> standby() {
        service.standby();
        return Map.of("mode", "STANDBY");
    }

    @GetMapping("/api/episodes")
    @ResponseBody
    public List<Map<String, Object>> episodes(@RequestParam(defaultValue = "100") int limit) {
        return service.episodes(limit);
    }
}
