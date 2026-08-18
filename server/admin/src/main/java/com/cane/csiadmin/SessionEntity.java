package com.cane.csiadmin;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.GeneratedValue;
import jakarta.persistence.GenerationType;
import jakarta.persistence.Id;
import jakarta.persistence.Table;

/**
 * 수집 세션 메타 — end_ts가 NULL이면 수집 중(활성). ingest가 1초마다 폴링해 태깅한다.
 * 라벨링은 위치/행동/사람 3필드 (스키마 v3).
 */
@Entity
@Table(name = "sessions")
public class SessionEntity {

    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    @Column(name = "session_id")
    private Integer sessionId;

    @Column(nullable = false, length = 16)
    private String mode = "CAPTURE";

    @Column(nullable = false, length = 64)
    private String location;

    @Column(nullable = false, length = 64)
    private String behavior;

    @Column(nullable = false, length = 64)
    private String person;

    @Column(name = "start_ts", nullable = false)
    private double startTs;

    @Column(name = "end_ts")
    private Double endTs;

    public Integer getSessionId() { return sessionId; }
    public String getMode() { return mode; }
    public void setMode(String mode) { this.mode = mode; }
    public String getLocation() { return location; }
    public void setLocation(String location) { this.location = location; }
    public String getBehavior() { return behavior; }
    public void setBehavior(String behavior) { this.behavior = behavior; }
    public String getPerson() { return person; }
    public void setPerson(String person) { this.person = person; }
    public double getStartTs() { return startTs; }
    public void setStartTs(double startTs) { this.startTs = startTs; }
    public Double getEndTs() { return endTs; }
    public void setEndTs(Double endTs) { this.endTs = endTs; }

    public boolean isActive() { return endTs == null; }

    /** 화면 표시용 요약 라벨: "위치 · 행동 · 사람" (빈 필드는 생략) */
    public String displayLabel() {
        StringBuilder sb = new StringBuilder();
        for (String part : new String[] {location, behavior, person}) {
            if (part != null && !part.isBlank()) {
                if (sb.length() > 0) sb.append(" · ");
                sb.append(part);
            }
        }
        return sb.length() > 0 ? sb.toString() : "(라벨 없음)";
    }
}
