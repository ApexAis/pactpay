"""Browser check of the decision paths: partial release proposal -> freelancer disputes -> arbiter decides.

    python scripts/ui_dispute_e2e.py http://127.0.0.1:PORT out_dir
"""
import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

base, out = sys.argv[1].rstrip("/"), Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
errors = []


def idle(pg):
    pg.wait_for_function("() => !document.body.dataset.busy", timeout=300_000)
    pg.wait_for_timeout(120)


def click(pg, sel):
    idle(pg)
    pg.locator(sel).first.click()
    idle(pg)


def status(pg, text):
    expect(pg.locator(".pact-title__meta")).to_contain_text(text, timeout=120_000)


with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width": 1440, "height": 900})
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(base + "/", wait_until="networkidle")
    click(pg, '[data-action="start-preset"][data-id="paypal_webhook_sprint"]')
    before = pg.locator('[data-action="approve"]').inner_text()
    click(pg, '[data-action="edit"]')
    pg.fill('#edit-form [name="amount"]', "450.00")
    pg.fill('#edit-form [data-payee="1"] [name="paypal_email"]', "sam.edited.sandbox@example.com")
    click(pg, '[data-action="save-terms"]')
    expect(pg.locator(".pact-title__meta")).to_contain_text("$450.00")
    expect(pg.locator(".payout-table")).to_contain_text("sam.edited.sandbox@example.com")
    assert pg.locator('[data-action="approve"]').inner_text() != before, "the fingerprint must change after an edit"
    click(pg, '[data-action="approve"]')
    click(pg, '.persona[data-id="freelancer"]')
    click(pg, '[data-action="approve"]')
    click(pg, '.persona[data-id="client"]')
    click(pg, '[data-action="order"]')
    click(pg, '[data-action="sim-approve"]')
    click(pg, '[data-action="authorize"]')
    status(pg, "Funds held")
    click(pg, '.persona[data-id="freelancer"]')
    click(pg, '[data-action="tab"][data-id="deliver"]')
    click(pg, '[data-action="submit-sample:flawed_submission"]')
    click(pg, '[data-action="evaluate"]')
    status(pg, "Evaluated")
    click(pg, '.persona[data-id="client"]')
    click(pg, '[data-action="tab"][data-id="decide"]')
    pg.fill("#amt", "300.00")
    click(pg, '[data-action="release"]')  # differs from the recommendation without a reason: refused
    expect(pg.locator("#toast")).to_contain_text("reason", timeout=10_000)
    pg.fill("#amt", "300.00")
    pg.fill("#why", "C3 missing and the runbook is thin; proposing $300.")
    click(pg, '[data-action="release"]')
    status(pg, "Partial release proposed")
    pg.screenshot(path=str(out / "01_proposed.png"))
    click(pg, '.persona[data-id="freelancer"]')
    click(pg, '[data-action="tab"][data-id="decide"]')
    pg.fill("#why", "Three of four requirements pass their tests: that is $375, not $300.")
    click(pg, '[data-action="dispute"]')
    status(pg, "In dispute")
    click(pg, '.persona[data-id="arbiter"]')
    click(pg, '[data-action="tab"][data-id="decide"]')
    pg.screenshot(path=str(out / "02_arbiter.png"))
    pg.fill("#amt", "337.50")
    pg.fill("#why", "The tests show three of four requirements met; the recommendation stands.")
    click(pg, '[data-action="resolve"]')
    expect(pg.locator(".pact-title__meta")).to_contain_text("Pay", timeout=60_000)
    click(pg, '[data-action="webhooks"]')
    if pg.locator('[data-action="reconcile"]').count():
        click(pg, '[data-action="reconcile"]')
    # the edited payee has no PayPal account in the simulator: PayPal reports that item UNCLAIMED
    status(pg, "unclaimed")
    expect(pg.locator(".payout-table")).to_contain_text("UNCLAIMED")
    txt = pg.locator(".ledger").inner_text()
    assert "$337.50" in txt, txt  # 75 % of the edited $450.00
    pg.screenshot(path=str(out / "03_paid.png"))
    click(pg, '[data-action="tab"][data-id="evidence"]')
    expect(pg.locator(".timeline")).to_contain_text("dispute.resolved")
    b.close()
print("dispute path OK; page errors:", errors)
sys.exit(1 if errors else 0)
