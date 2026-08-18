package com.cane.csiadmin;

import java.io.IOException;
import jakarta.servlet.Filter;
import jakarta.servlet.FilterChain;
import jakarta.servlet.ServletException;
import jakarta.servlet.ServletRequest;
import jakarta.servlet.ServletResponse;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletResponse;
import jakarta.servlet.http.HttpSession;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

/**
 * 단일 비밀번호 접근 제한 (ID 없음 — 사용자 결정).
 * ADMIN_PASSWORD 환경변수(admin.password)가 비어 있으면 비활성 — 로컬 개발 무마찰.
 * 클라우드 배포 시 반드시 환경변수로 설정할 것.
 */
@Component
public class AuthFilter implements Filter {

    static final String SESSION_KEY = "authed";

    @Value("${admin.password:}")
    private String password;

    @Override
    public void doFilter(ServletRequest req, ServletResponse res, FilterChain chain)
            throws IOException, ServletException {
        if (password == null || password.isBlank()) {
            chain.doFilter(req, res);           // 비밀번호 미설정 = 인증 비활성
            return;
        }
        HttpServletRequest r = (HttpServletRequest) req;
        HttpServletResponse w = (HttpServletResponse) res;
        String path = r.getRequestURI();

        if (path.equals("/login") || path.equals("/healthz") || path.startsWith("/css/")) {
            chain.doFilter(req, res);
            return;
        }
        HttpSession session = r.getSession(false);
        if (session != null && Boolean.TRUE.equals(session.getAttribute(SESSION_KEY))) {
            chain.doFilter(req, res);
            return;
        }
        if (path.startsWith("/api/")) {
            w.sendError(401);                   // API는 리다이렉트 대신 401
        } else {
            w.sendRedirect("/login");
        }
    }

    boolean matches(String attempt) {
        return password != null && !password.isBlank() && password.equals(attempt);
    }
}
