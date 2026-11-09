"""Record the PactPay demo from the real running app (Playwright), scene by scene, for scripts/video/assemble.py.

    python scripts/video/record_demo.py --base http://127.0.0.1:18480 --out build/video [--live]

Each scene in narration.json has a narration (already rendered to audio by tts.py, so its duration is known).
The director performs the scene's real UI actions, then holds the frame until the narration has been spoken.
Waits that are only real computation (the AI drafting, the sandboxed tests plus the AI review, PayPal settling)
are recorded too, but marked as cuts. The assembled video removes them and says on screen how long the
real wait was. Nothing on screen is mocked: it is the app, its API and (with --live) PayPal Sandbox.

--live: the app runs with Sandbox credentials. The buyer approves in PayPal Checkout, which is recorded, with
PAYPAL_SANDBOX_BUYER_EMAIL / PAYPAL_SANDBOX_BUYER_PASSWORD from the environment.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

HERE = Path(__file__).resolve().parent
CURSOR_JS = """
(() => {
  const add = () => {
    if (document.getElementById('__cursor')) return;
    const c = document.createElement('div'); c.id = '__cursor';
    c.style.cssText = 'position:fixed;z-index:2147483647;width:22px;height:22px;margin:-11px 0 0 -11px;border-radius:50%;' +
      'background:rgba(255,196,57,.55);border:2px solid #b07a00;pointer-events:none;transition:transform .12s;left:-50px;top:-50px';
    document.documentElement.appendChild(c);
    addEventListener('mousemove', e => { c.style.left = e.clientX + 'px'; c.style.top = e.clientY + 'px'; }, true);
    addEventListener('mousedown', () => { c.style.transform = 'scale(.7)'; }, true);
    addEventListener('mouseup', () => { c.style.transform = 'scale(1)'; }, true);
  };
  if (document.readyState === 'loading') addEventListener('DOMContentLoaded', add); else add();
})();
"""


class Director:
    def __init__(self, page, durations: dict[str, float], save=None):
        self.page, self.durations, self.save = page, durations, save
        self.t0 = time.monotonic()
        self.timeline: list[dict] = []
        self.mouse = (960, 540)

    def now(self) -> float:
        return time.monotonic() - self.t0

    def idle(self, timeout=600_000):
        self.page.wait_for_function("() => !document.body.dataset.busy", timeout=timeout)

    def move_to(self, locator):
        box = locator.bounding_box()
        if not box:
            return
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        steps = 18
        for i in range(1, steps + 1):
            self.page.mouse.move(self.mouse[0] + (x - self.mouse[0]) * i / steps,
                                 self.mouse[1] + (y - self.mouse[1]) * i / steps)
            self.page.wait_for_timeout(14)
        self.mouse = (x, y)

    def click(self, selector: str, *, pause: int = 350):
        self.idle()
        loc = self.page.locator(selector).first
        loc.scroll_into_view_if_needed()
        self.move_to(loc)
        self.page.wait_for_timeout(pause)
        loc.click()
        self.page.wait_for_timeout(150)

    def scroll(self, y: int, ms: int = 900):
        self.page.evaluate("([y, ms]) => window.scrollTo({top: y, behavior: 'smooth'})", [y, ms])
        self.page.wait_for_timeout(ms)

    def scroll_to(self, selector: str, block: str = "start"):
        self.page.locator(selector).first.evaluate(f"e => e.scrollIntoView({{behavior: 'smooth', block: '{block}'}})")
        self.page.wait_for_timeout(900)

    def scene(self, sid: str):
        return _Scene(self, sid)

    def cut(self, label: str):
        return _Cut(self, label)


class _Scene:
    def __init__(self, d: Director, sid: str):
        self.d, self.sid = d, sid

    def __enter__(self):
        self.start = self.d.now()
        return self

    def __exit__(self, *exc):
        if exc[0]:
            return False
        need = self.d.durations.get(self.sid, 3.0) + 0.6
        cuts = sum(c["end"] - c["start"] for c in self.d.timeline if c.get("cut") and c["start"] >= self.start)
        shown = self.d.now() - self.start - cuts
        if shown < need:
            self.d.page.wait_for_timeout(int((need - shown) * 1000))
        self.d.timeline.append({"scene": self.sid, "start": self.start, "end": self.d.now()})
        if self.d.save:
            self.d.save()
        return False


class _Cut:
    def __init__(self, d: Director, label: str):
        self.d, self.label = d, label

    def __enter__(self):
        self.start = self.d.now()
        return self

    def __exit__(self, *exc):
        if exc[0]:
            return False
        self.d.timeline.append({"cut": True, "label": self.label, "start": self.start + 1.2, "end": self.d.now() - 0.4,
                                "real_seconds": round(self.d.now() - self.start, 1)})
        return False


def run(base: str, out: Path, live: bool) -> None:
    narr = json.loads((out / "narration_durations.json").read_text())
    durations = {k: v["seconds"] for k, v in narr.items()}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1920, "height": 1080}, record_video_dir=str(out / "raw"),
                                  record_video_size={"width": 1920, "height": 1080}, color_scheme="light")
        ctx.add_init_script(CURSOR_JS)
        ctx.add_init_script("document.addEventListener('DOMContentLoaded', () => { document.documentElement.style.zoom = '1.32'; });")
        page = ctx.new_page()
        t_page = time.monotonic()  # Playwright's video starts with the page
        d = Director(page, durations)
        live_flag = live

        def save(final: bool = False) -> None:
            (out / "timeline.json").write_text(json.dumps({"video": str(page.video.path()), "offset": d.t0 - t_page,
                                                            "live": live_flag, "complete": final,
                                                            "events": d.timeline}, indent=1))
        d.save = save
        page.goto(base + "/", wait_until="networkidle")
        d.t0 = time.monotonic()
        offset = d.t0 - t_page  # add to every timeline time to get the video's time

        # 1. hook: the start screen
        with d.scene("s01_hook"):
            page.wait_for_timeout(800)
            d.scroll(260, 1500)
            d.scroll(0, 1200)

        # 2. natural-language agreement -> AI-drafted policy (grounded in the text)
        with d.scene("s02_agreement"):
            box = page.locator("#custom-chat")
            box.scroll_into_view_if_needed()
            d.move_to(box)
            box.click()
            box.fill((HERE / "custom_agreement.txt").read_text().strip())
            page.wait_for_timeout(600)
            d.click('[data-action="start-custom"]')
        with d.cut("AI drafting the payment policy"):
            d.idle()
            expect(page.locator(".pact-title__meta")).to_contain_text("Draft terms", timeout=600_000)
        with d.scene("s03_drafted"):
            page.wait_for_timeout(500)
            if page.locator(".callout--warn").count():
                d.scroll_to(".callout--warn", "center")
                page.wait_for_timeout(2200)
            d.scroll_to(".criteria")
            page.wait_for_timeout(1500)
            d.scroll_to(".chat", "center")
            page.wait_for_timeout(1200)
            d.scroll(0)

        # 3. the reference pact for the full journey
        page.goto(base + "/", wait_until="networkidle")
        with d.scene("s04_pact"):
            d.click('[data-action="start-preset"][data-id="paypal_webhook_sprint"]')
            d.idle()
            page.wait_for_timeout(900)
            d.scroll_to(".criterion details")
            page.locator(".criterion details summary").first.click()
            page.wait_for_timeout(1800)
            d.scroll_to(".payout-table", "center")
            page.wait_for_timeout(1000)
            d.scroll(0)
        if True:  # same path live and offline: one payee becomes PayPal's Sandbox test receiver (visible edit)
            d.click('[data-action="edit"]')
            page.fill('#edit-form [data-payee="0"] [name="paypal_email"]', "payouts-simulator-receiver@paypal.com")
            page.wait_for_timeout(600)
            d.click('[data-action="save-terms"]')
            d.idle()
        with d.scene("s05_lock"):
            d.click('[data-action="approve"]')
            d.idle()
            d.click('.persona[data-id="freelancer"]')
            d.idle()
            d.click('[data-action="approve"]')
            d.idle()
            expect(page.locator(".pact-title__meta")).to_contain_text("Terms locked")
            d.click('.persona[data-id="client"]')
            d.idle()
            d.click('[data-action="tab"][data-id="terms"]')
            page.wait_for_timeout(1200)

        # 4. PayPal hold
        with d.scene("s06_fund"):
            d.click('[data-action="order"]')
            d.idle()
            page.wait_for_timeout(900)
            if not live:
                d.click('[data-action="sim-approve"]')
                d.idle()
                d.click('[data-action="authorize"]')
                d.idle()
        if live:
            # the buyer approves in PayPal's real Sandbox checkout on their own device; the wait is cut and labelled
            url = page.locator('a.btn--primary[href*="paypal.com"]').first.get_attribute("href")
            print(f"APPROVAL_URL {url}", flush=True)
            with d.cut("buyer approving in PayPal Sandbox checkout"):
                deadline = time.monotonic() + 900
                while time.monotonic() < deadline:
                    d.click('[data-action="authorize"]')
                    d.idle()
                    if "Funds held" in page.locator(".pact-title__meta").inner_text():
                        break
                    page.wait_for_timeout(10_000)
        with d.scene("s06b_held"):
            expect(page.locator(".pact-title__meta")).to_contain_text("Funds held", timeout=60_000)
            page.wait_for_timeout(1500)

        # 5. deliver: flawed work, then an attack, then the real thing
        with d.scene("s07_flawed"):
            d.click('.persona[data-id="freelancer"]')
            d.idle()
            d.click('[data-action="tab"][data-id="deliver"]')
            page.wait_for_timeout(700)
            d.click('[data-action="submit-sample:flawed_submission"]')
            d.idle()
            d.click('[data-action="evaluate"]')
        with d.cut("tests in the sandbox + AI reviewer"):
            d.idle()
        with d.scene("s08_flawed_result"):
            page.wait_for_timeout(600)
            d.scroll_to(".eval-card--FAIL")
            page.wait_for_timeout(900)
            cite = page.locator(".eval-card--FAIL .cite").first
            if cite.count():
                d.move_to(cite)
                cite.click()
                page.wait_for_timeout(1500)
        with d.scene("s09_attack"):
            d.scroll(0)
            d.click('[data-action="tab"][data-id="deliver"]')
            page.wait_for_timeout(500)
            d.click('[data-action="submit-sample:prompt_injection"]')
            d.idle()
            d.click('[data-action="evaluate"]')
        with d.cut("tests in the sandbox + AI reviewer"):
            d.idle()
        with d.scene("s10_attack_result"):
            page.wait_for_timeout(700)
            d.scroll_to(".flag-banner", "center")
            page.wait_for_timeout(1600)
        with d.scene("s11_fixed"):
            d.scroll(0)
            d.click('[data-action="tab"][data-id="deliver"]')
            page.wait_for_timeout(400)
            d.click('[data-action="submit-sample:fixed_submission"]')
            d.idle()
            d.click('[data-action="evaluate"]')
        with d.cut("tests in the sandbox + AI reviewer"):
            d.idle()
        with d.scene("s12_fixed_result"):
            page.wait_for_timeout(800)
            d.scroll(250, 1200)
            d.scroll(0, 900)

        # 6. a person decides; PayPal captures and pays out
        with d.scene("s13_release"):
            d.click('.persona[data-id="client"]')
            d.idle()
            d.click('[data-action="tab"][data-id="decide"]')
            page.wait_for_timeout(900)
            d.click('[data-action="release"]')
            d.idle()
            page.wait_for_timeout(800)
        with d.cut("PayPal processing the payout batch"):
            deadline = time.monotonic() + (300 if live else 5)
            while time.monotonic() < deadline:
                if page.locator('[data-action="webhooks"]').count():
                    d.click('[data-action="webhooks"]')
                    d.idle()
                if "Paid out" in page.locator(".pact-title__meta").inner_text() or "unclaimed" in \
                        page.locator(".pact-title__meta").inner_text().lower():
                    break
                if page.locator('[data-action="reconcile"]').count():
                    d.click('[data-action="reconcile"]')
                    d.idle()
                page.wait_for_timeout(8000 if live else 300)
        with d.scene("s14_paid"):
            page.wait_for_timeout(600)
            d.scroll_to(".payout-table", "center")
            page.wait_for_timeout(1500)
            d.scroll(0)

        # 7. the same release again: refused
        with d.scene("s15_duplicate"):
            d.click('[data-action="release-again"]')
            page.wait_for_timeout(2500)

        # 8. evidence
        with d.scene("s16_evidence"):
            d.click('[data-action="tab"][data-id="evidence"]')
            d.idle()
            page.wait_for_timeout(900)
            d.scroll(420, 1500)
            d.scroll(0, 900)
            d.click('[data-action="traces"]')
            page.wait_for_timeout(1800)
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)

        with d.scene("s17_close"):
            page.goto(base + "/", wait_until="networkidle")
            page.wait_for_timeout(1000)

        video_path = page.video.path()
        ctx.close()
        browser.close()
    (out / "timeline.json").write_text(json.dumps({"video": str(video_path), "offset": offset, "live": live,
                                                    "complete": True, "events": d.timeline}, indent=1))
    print("recorded", video_path)


def approve_in_paypal(page, d: Director) -> None:
    """The real PayPal Sandbox checkout, recorded. Stops if PayPal shows a human check."""
    email, password = os.environ["PAYPAL_SANDBOX_BUYER_EMAIL"], os.environ["PAYPAL_SANDBOX_BUYER_PASSWORD"]
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(2500)
    if page.locator("#email").count():
        page.fill("#email", email)
        if page.locator("#btnNext").count():
            page.click("#btnNext")
            page.wait_for_timeout(1500)
    page.wait_for_selector("#password", timeout=60_000)
    page.fill("#password", password)
    page.click("#btnLogin")
    page.wait_for_timeout(4000)
    for sel in ("#payment-submit-btn", "button[data-testid='submit-button-initial']", "#consentButton",
                "button:has-text('Continue')", "button:has-text('Agree & Continue')"):
        if page.locator(sel).count():
            d.move_to(page.locator(sel).first)
            page.locator(sel).first.click()
            break


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--live", action="store_true")
    a = ap.parse_args()
    run(a.base.rstrip("/"), Path(a.out), a.live)
