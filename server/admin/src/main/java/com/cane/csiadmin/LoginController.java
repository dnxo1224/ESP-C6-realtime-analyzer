package com.cane.csiadmin;

import jakarta.servlet.http.HttpServletRequest;
import org.springframework.stereotype.Controller;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.servlet.mvc.support.RedirectAttributes;

@Controller
public class LoginController {

    private final AuthFilter auth;

    public LoginController(AuthFilter auth) {
        this.auth = auth;
    }

    @GetMapping("/login")
    public String loginPage() {
        return "login";
    }

    @PostMapping("/login")
    public String login(@RequestParam String password, HttpServletRequest req, RedirectAttributes ra) {
        if (auth.matches(password)) {
            req.getSession(true).setAttribute(AuthFilter.SESSION_KEY, Boolean.TRUE);
            return "redirect:/";
        }
        ra.addFlashAttribute("err", "비밀번호가 틀렸습니다");
        return "redirect:/login";
    }
}
