"""Browser end-to-end check of the PactPay UI (Playwright + Chromium), with a screenshot of every step.

    python scripts/ui_e2e.py http://127.0.0.1:18480 out_dir [--video]

Simulator mode: clicks through preset -> approvals -> order -> simulated buyer approval -> authorize -> flawed
submission -> evaluation (real configured AI reviewer) -> fixed submission -> evaluation -> release -> webhooks ->
reconcile -> second release refused -> evidence. Fails on any console error or a step that does not reach its state.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

base, out = sys.argv[1].rstrip("/"), Path(sys.argv[2])
video = "--video" in sys.argv
out.mkdir(parents=True, exist_ok=True)
errors: list[str] = []
n = 0


def shot(page, name):
    global n
    n += 1
    page.wait_for_timeout(350)
    page.screenshot(path=str(out / f"{n:02d}_{name}.png"), full_page=False)


def idle(page, timeout=300_000):
    page.wait_for_function("() => !document.body.dataset.busy", timeout=timeout)
    page.wait_for_timeout(150)


def click(page, text, *, role="button", exact=False, timeout=180_000):
    idle(page)
    page.get_by_role(role, name=text, exact=exact).first.click(timeout=timeout)
    idle(page)


class _L:
    def __init__(self, page, sel):
        self.page, self.loc = page, page.locator(sel)

    def click(self):
        idle(self.page)
        self.loc.first.click()
        idle(self.page)

    def __getattr__(self, k):
        return getattr(self.loc, k)


def L(page, sel):
    return _L(page, sel)


def status(page, text, timeout=240_000):
    expect(page.locator(".pact-title__meta")).to_contain_text(text, timeout=timeout)


with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1,
                        record_video_dir=str(out / "video") if video else None,
                        record_video_size={"width": 1440, "height": 900} if video else None)
    page = ctx.new_page()
    page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.goto(base + "/", wait_until="networkidle")
    shot(page, "start")
    L(page, '[data-action="start-preset"][data-id="paypal_webhook_sprint"]').click()
    status(page, "Draft terms")
    shot(page, "terms_draft")
    L(page, '[data-action="approve"]').click()
    expect(page.locator(".approvals")).to_contain_text("approved", timeout=30_000)
    L(page, '.persona[data-id="freelancer"]').click()
    page.wait_for_timeout(500)
    L(page, '[data-action="approve"]').click()
    status(page, "Terms locked")
    L(page, '.persona[data-id="client"]').click()
    L(page, '[data-action="tab"][data-id="terms"]').click()
    shot(page, "terms_locked")
    L(page, '[data-action="order"]').click()
    status(page, "Awaiting buyer approval")
    shot(page, "order_created")
    L(page, '[data-action="sim-approve"]').click()
    page.wait_for_timeout(500)
    L(page, '[data-action="authorize"]').click()
    status(page, "Funds held")
    shot(page, "funds_held")
    L(page, '.persona[data-id="freelancer"]').click()
    page.wait_for_timeout(400)
    L(page, '[data-action="tab"][data-id="deliver"]').click()
    shot(page, "deliver")
    L(page, '[data-action="submit-sample:flawed_submission"]').click()
    status(page, "Work submitted")
    L(page, '[data-action="evaluate"]').click()
    status(page, "Evaluated")
    shot(page, "eval_flawed")
    cite = page.locator(".cite").first
    if cite.count():
        cite.click()
        shot(page, "eval_flawed_citation")
    L(page, '[data-action="tab"][data-id="deliver"]').click()
    L(page, '[data-action="submit-sample:prompt_injection"]').click()
    status(page, "Work submitted")
    L(page, '[data-action="evaluate"]').click()
    status(page, "Evaluated")
    shot(page, "eval_injection")
    L(page, '[data-action="tab"][data-id="deliver"]').click()
    L(page, '[data-action="submit-sample:fixed_submission"]').click()
    status(page, "Work submitted")
    L(page, '[data-action="evaluate"]').click()
    status(page, "Evaluated")
    shot(page, "eval_fixed")
    L(page, '.persona[data-id="client"]').click()
    page.wait_for_timeout(400)
    L(page, '[data-action="tab"][data-id="decide"]').click()
    shot(page, "decide")
    L(page, '[data-action="release"]').click()
    expect(page.locator(".pact-title__meta")).to_contain_text("Pay", timeout=60_000)
    shot(page, "payout_pending")
    sim = page.locator('[data-action="webhooks"]')
    if sim.count():
        idle(page)
        sim.click()
        idle(page)
        page.wait_for_timeout(800)
    if page.locator('[data-action="reconcile"]').count():
        L(page, '[data-action="reconcile"]').click()
    status(page, "Paid out")
    shot(page, "paid_out")
    L(page, '[data-action="release-again"]').click()
    expect(page.locator("#toast")).to_contain_text("already_released", timeout=20_000)
    shot(page, "second_release_refused")
    L(page, '[data-action="tab"][data-id="evidence"]').click()
    expect(page.locator(".chain-badge")).to_contain_text("intact", timeout=20_000)
    shot(page, "evidence")
    L(page, '[data-action="traces"]').click()
    page.wait_for_timeout(600)
    shot(page, "paypal_calls")
    ctx.close()
    b.close()

real_errors = [e for e in errors if "409" not in e]  # the refused second release logs a 409 fetch error by design
print(f"screens: {n}; console errors: {len(real_errors)}")
for e in real_errors:
    print("  ", e)
sys.exit(1 if real_errors else 0)
