/* PactPay web client: one pact, three parties, every money step confirmed by PayPal's own record.
 *
 * No build step and no inline handlers (the CSP forbids them): every control carries data-action and one delegated
 * listener dispatches it. Every server string is escaped before it reaches innerHTML.
 */
"use strict";

(() => {
  // ------------------------------------------------------------------------------------------------ helpers ----
  const $ = (sel, root = document) => root.querySelector(sel);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const usd = (cents) => (cents === null || cents === undefined ? "—" : `$${Math.floor(cents / 100).toLocaleString("en-US")}.${String(Math.abs(cents) % 100).padStart(2, "0")}`);
  // "500", "500.5", "500.50" -> integer cents; anything else -> null (no binary-float arithmetic on money)
  const toCents = (v) => { const m = /^\s*(\d{1,7})(?:\.(\d{1,2}))?\s*$/.exec(String(v ?? "")); return m ? Number(m[1]) * 100 + Number((m[2] || "0").padEnd(2, "0")) : null; };
  const short = (h, n = 10) => (h ? `${String(h).slice(0, n)}…` : "—");
  const pct = (x) => `${Number(x || 0).toFixed(Number.isInteger(Number(x)) ? 0 : 2)}%`;
  const when = (iso) => { try { return new Date(iso).toLocaleString(); } catch { return iso || ""; } };
  const store = {
    get(k, d = null) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode: state lives in memory */ } },
  };

  const ICON = {
    check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>',
    x: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>',
    alert: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4M12 17h.01"/></svg>',
    lock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>',
    unlock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 9.9-1"/></svg>',
    shield: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z"/><path d="m9 12 2 2 4-4"/></svg>',
    copy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
    doc: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M9 13h6M9 17h6"/></svg>',
    arrow: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M19 12l-7 7-7-7"/></svg>',
    eye: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8Z"/><circle cx="12" cy="12" r="3"/></svg>',
    spark: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v4M12 17v4M3 12h4M17 12h4M5.6 5.6l2.8 2.8M15.6 15.6l2.8 2.8M5.6 18.4l2.8-2.8M15.6 8.4l2.8-2.8"/></svg>',
  };
  const MARK = '<svg class="brand__mark" viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="8" fill="#0a5ad4"/><path d="M9 23V9h8a5 5 0 0 1 0 10h-4" fill="none" stroke="#fff" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/><circle cx="21.5" cy="22.5" r="2.5" fill="#ffc439"/></svg>';

  const ROLE_LABEL = { client: "Client", freelancer: "Freelancer", arbiter: "Arbiter" };
  const ROLE_DESC = { client: "pays: approves terms, funds the hold, decides the release", freelancer: "delivers: approves terms, submits the work", arbiter: "decides only if the parties dispute" };
  const STEPS = [
    { id: "terms", label: "Agree", sub: "AI drafts, both approve" },
    { id: "fund", label: "Fund", sub: "PayPal authorization" },
    { id: "deliver", label: "Deliver", sub: "freelancer submits" },
    { id: "evaluate", label: "Verify", sub: "tests + AI reviewer" },
    { id: "decide", label: "Decide", sub: "a person releases" },
    { id: "capture", label: "Capture", sub: "PayPal capture" },
    { id: "payout", label: "Pay out", sub: "PayPal Payouts split" },
  ];
  const STATUS_STEP = {
    DRAFT: 0, TERMS_APPROVED: 1, AWAITING_BUYER_APPROVAL: 1, FUNDS_HELD: 2, SUBMITTED: 3, AUDITED: 4,
    RELEASE_PROPOSED: 4, DISPUTED: 4, CAPTURING: 5, CAPTURE_PENDING: 5, CAPTURE_FAILED: 5, CAPTURED: 6,
    PAYOUT_PENDING: 6, PAID_OUT: 7, PAYOUT_UNCLAIMED: 6, PAYOUT_ATTENTION: 6, VOIDED: 1, CANCELLED: 0,
  };
  const STATUS_TEXT = {
    DRAFT: "Draft terms", TERMS_APPROVED: "Terms locked", AWAITING_BUYER_APPROVAL: "Awaiting buyer approval",
    FUNDS_HELD: "Funds held by PayPal", SUBMITTED: "Work submitted", AUDITED: "Evaluated — awaiting decision",
    RELEASE_PROPOSED: "Partial release proposed", DISPUTED: "In dispute", CAPTURING: "Capturing",
    CAPTURE_PENDING: "Capture pending at PayPal", CAPTURE_FAILED: "Capture failed", CAPTURED: "Captured",
    PAYOUT_PENDING: "Payout in progress", PAID_OUT: "Paid out", PAYOUT_UNCLAIMED: "Paid / unclaimed by a payee",
    PAYOUT_ATTENTION: "Payout needs attention", VOIDED: "Authorization voided", CANCELLED: "Cancelled",
  };

  // ------------------------------------------------------------------------------------------------- state ----
  const S = {
    config: null, presets: [], pact: null, pactId: null, session: null, role: "client", tab: null,
    busy: null, error: null, samples: [], submission: null, citeKey: null, traces: null, evidence: null,
    drawer: false, editing: false, customChat: "", customAmount: "",
  };

  async function api(method, path, body, { role = S.role, idem = false } = {}) {
    const headers = { Accept: "application/json" };
    const tok = S.session?.personas?.[role]?.token;
    if (tok) headers.Authorization = `Bearer ${tok}`;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (idem) headers["Idempotency-Key"] = `ui-${(crypto.randomUUID?.() || Math.random().toString(36).slice(2))}`;
    const res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
    let data = {};
    try { data = await res.json(); } catch { data = {}; }
    if (!res.ok) {
      const err = new Error(data.message || `HTTP ${res.status}`);
      err.status = res.status; err.body = data;
      throw err;
    }
    return data;
  }

  function toast(msg, bad = false) {
    const t = $("#toast");
    t.textContent = msg; t.className = `toast${bad ? " toast--bad" : ""}`; t.hidden = false;
    clearTimeout(toast._t); toast._t = setTimeout(() => { t.hidden = true; }, bad ? 7000 : 3500);
  }

  async function run(label, fn) {
    if (S.busy) return;
    S.busy = label; S.error = null; render();
    try { await fn(); } catch (e) {
      S.error = { action: label, message: e.message, body: e.body || null, status: e.status };
      if (e.status === 409 && e.body?.error) toast(`Refused (${e.body.error}): ${e.message}`, true);
      else toast(e.message, true);
    } finally { S.busy = null; render(); }
  }

  // ------------------------------------------------------------------------------------------------ session ----
  function saveSession() {
    const all = store.get("pactpay.sessions", {});
    all[S.pactId] = S.session; store.set("pactpay.sessions", all);
  }
  function loadSession(pid) { return (store.get("pactpay.sessions", {}) || {})[pid] || null; }

  async function openPact(pid, { tab = null } = {}) {
    S.pactId = pid; S.session = loadSession(pid); S.pact = null; S.tab = tab; S.submission = null; S.evidence = null;
    S.traces = null; S.citeKey = null; S.editing = false;
    if (!S.session) { S.error = { message: "This browser holds no identity for that pact." }; render(); return; }
    S.role = S.session.active || "client";
    const url = new URL(location.href); url.searchParams.set("pact", pid);
    history.replaceState(null, "", `${url.pathname}?${url.searchParams}`);
    await refresh();
  }

  async function refresh() {
    S.pact = await api("GET", `/api/pacts/${S.pactId}`);
    const sub = S.pact.submissions?.[S.pact.submissions.length - 1];
    if (sub && S.submission?.id !== sub.id) {
      try { S.submission = await api("GET", `/api/pacts/${S.pactId}/submissions/${sub.id}`); } catch { S.submission = null; }
    }
    if (S.tab === "evidence") S.evidence = await api("GET", `/api/pacts/${S.pactId}/evidence`);
    render();
  }

  // ------------------------------------------------------------------------------------------------- render ----
  function render() {
    document.body.dataset.busy = S.busy ? "1" : "";
    renderBanner();
    const root = $("#app");
    root.innerHTML = S.pactId && S.pact ? pactScreen() : startScreen();
    renderDrawer();
    const view = `${S.pactId || ""}|${S.pactId ? (S.tab || "") : "start"}`;
    if (view !== render.lastView) { render.lastView = view; if (!S.citeKey) window.scrollTo(0, 0); }
    if (S.citeKey) {
      const line = $(`#vl-${S.citeKey.split(":")[1]}`);
      if (line) line.scrollIntoView({ block: "center" });
    }
  }

  function renderBanner() {
    const b = $("#mode-banner");
    if (!S.config) return;
    const live = S.config.paypal_mode === "live_sandbox";
    b.className = `mode-banner ${live ? "mode-live" : "mode-sim"}`;
    b.innerHTML = live
      ? '<span class="mode-banner__tag">PayPal Sandbox · live</span><span class="mode-banner__text">Every PayPal step below is a real call to api-m.sandbox.paypal.com (Sandbox money, not real funds).</span>'
      : '<span class="mode-banner__tag">Simulator</span><span class="mode-banner__text">Offline PayPal protocol simulator: no PayPal account is contacted and no money moves. Set Sandbox credentials for live calls.</span>';
  }

  function topbar(inner = "") {
    return `<header class="topbar"><a class="brand" href="/" data-action="home">${MARK}<span class="brand__name">PactPay</span></a>${inner}</header>`;
  }

  // ---------- start ----------
  function startScreen() {
    const ai = S.health?.ai || {};
    const presets = S.presets.map((p) => `
      <article class="card preset">
        <div class="row row--between"><span class="chip chip--brand">${esc(p.badge || "Preset")}</span><span class="amount">${usd(Math.round(p.total_amount_usd * 100))}</span></div>
        <div class="preset__title">${esc(p.title)}</div>
        <p class="muted small">${esc(p.summary)}</p>
        <div class="preset__foot"><span class="tiny muted">Reference acceptance tests · 3 demo parties</span>
          <button class="btn btn--primary" data-action="start-preset" data-id="${esc(p.id)}" ${S.busy ? "disabled" : ""}>${S.busy === `start:${p.id}` ? '<span class="spinner"></span>' : ""}Start this pact</button></div>
      </article>`).join("");
    const recent = Object.keys(store.get("pactpay.sessions", {}) || {}).slice(-5).reverse();
    return `${topbar('<div class="topbar__tools" style="margin-left:auto"><a class="btn btn--ghost btn--sm" href="/docs" target="_blank" rel="noopener">API docs</a></div>')}
      <section class="hero">
        <span class="eyebrow">PayPal Sandbox · executable payment policy</span>
        <h1>Agreements that pay out exactly as written.</h1>
        <p>PactPay turns a freelance agreement into a locked payment policy: amount in cents, acceptance tests, payees and shares.
        PayPal holds the client's funds with an authorization. Tests and an AI reviewer check the delivered work, but <strong>the AI can only hold money back</strong>:
        a person releases it, and PayPal's own records confirm every capture and payout.</p>
      </section>
      <div class="how">
        <div class="how__item"><span class="how__n">1</span><span class="how__t">Agree</span><span class="how__d">AI drafts terms from the chat; anything not found verbatim in the chat is dropped. Both parties approve one SHA-256.</span></div>
        <div class="how__item"><span class="how__n">2</span><span class="how__t">Fund</span><span class="how__d">PayPal Orders v2 <code>intent: AUTHORIZE</code> holds the exact locked amount.</span></div>
        <div class="how__item"><span class="how__n">3</span><span class="how__t">Verify</span><span class="how__d">Agreed tests run in a sandbox; an AI reviewer cites the exact lines it relies on.</span></div>
        <div class="how__item"><span class="how__n">4</span><span class="how__t">Settle</span><span class="how__d">A human release triggers one capture and one Payouts batch, split to the cent and reconciled per payee.</span></div>
      </div>
      <div class="stack-lg">
        <div class="presets">${presets}</div>
        <article class="card">
          <div class="card__head"><div class="card__title">${ICON.spark} Or write your own agreement</div>
            <span class="chip ${ai.drafter ? "chip--ok" : "chip--warn"}">${ai.drafter ? `AI drafter: ${esc(ai.drafter)}` : "No AI drafter configured: heuristic draft"}</span></div>
          <div class="stack">
            <textarea class="textarea" id="custom-chat" rows="7" placeholder="Paste the chat or brief. Include the price (e.g. $300), each requirement, and each payee's PayPal e-mail with their share (e.g. 60%).">${esc(S.customChat)}</textarea>
            <div class="row row--between"><span class="tiny muted">The draft is checked against your text: requirements, e-mails and the amount must appear in it word for word, or they are dropped and reported.</span>
              <button class="btn btn--primary" data-action="start-custom" ${S.busy ? "disabled" : ""}>${S.busy === "start:custom" ? '<span class="spinner"></span> Drafting with AI…' : "Draft payment policy"}</button></div>
          </div>
        </article>
        ${recent.length ? `<article class="card card--soft"><div class="card__title">Your recent pacts (this browser)</div><div class="row" style="margin-top:8px">${recent.map((id) => `<button class="btn btn--sm" data-action="open" data-id="${esc(id)}">${esc(id)}</button>`).join("")}</div></article>` : ""}
        <div class="cast">${["client", "freelancer", "arbiter"].map((r) => `<div class="cast__item"><span class="persona__avatar av-${r}">${r[0].toUpperCase()}</span><div><div class="strong">${ROLE_LABEL[r]}</div><div class="tiny muted">${ROLE_DESC[r]}</div></div></div>`).join("")}</div>
      </div>`;
  }

  // ---------- pact ----------
  function currentStep(p) { return STATUS_STEP[p.status] ?? 0; }

  function pactScreen() {
    const p = S.pact;
    const cur = currentStep(p);
    const tab = S.tab || STEPS[Math.min(cur, STEPS.length - 1)].id;
    const stopped = ["VOIDED", "CANCELLED", "CAPTURE_FAILED"].includes(p.status);
    const warn = ["DISPUTED", "PAYOUT_ATTENTION", "PAYOUT_UNCLAIMED", "CAPTURE_PENDING"].includes(p.status);
    const steps = STEPS.map((s, i) => {
      const cls = i < cur ? "step--done" : i === cur ? (stopped ? "step--stopped" : warn ? "step--warn" : "step--current") : "";
      return `<li class="step ${cls}"><button class="step__btn" data-action="tab" data-id="${s.id}" aria-selected="${tab === s.id}">
        <span class="step__dot">${i < cur ? ICON.check : i + 1}</span><span class="step__label">${s.label}</span><span class="step__sub">${esc(s.sub)}</span></button></li>`;
    }).join("");
    const live = p.paypal.mode === "live_sandbox";
    const personas = ["client", "freelancer", "arbiter"].filter((r) => S.session.personas[r]).map((r) => {
      const due = whoseTurn(p).includes(r);
      return `<button class="persona" data-action="role" data-id="${r}" aria-pressed="${S.role === r}" title="Act as the ${r}">
        <span class="persona__avatar av-${r}">${r[0].toUpperCase()}</span>${due ? '<span class="persona__dot" title="Their turn"></span>' : ""}
        <span class="persona__text"><span class="persona__name">${esc((p.parties[r]?.name || ROLE_LABEL[r]).replace(/ \(.*\)/, ""))}</span><span class="persona__role">${ROLE_LABEL[r]}</span></span></button>`;
    }).join("");
    return `${topbar(`
        <div class="pact-title"><span class="pact-title__name">${esc(p.terms.title)}</span>
          <span class="pact-title__meta"><span class="mono">${esc(p.pact_id)}</span><span class="chip ${live ? "chip--live" : "chip--sim"}">${live ? "Live Sandbox" : "Simulator"}</span>
          <span class="chip ${stopped ? "chip--bad" : warn ? "chip--warn" : p.status === "PAID_OUT" ? "chip--ok" : "chip--brand"}">${esc(STATUS_TEXT[p.status] || p.status)}</span>
          <span class="amount" style="font-size:15px">${usd(p.terms.amount_cents)}</span></span></div>
        <div class="personas" role="group" aria-label="Act as">${personas}</div>
        <div class="topbar__tools"><button class="btn btn--sm" data-action="traces">PayPal calls</button><button class="btn btn--sm btn--ghost" data-action="refresh" title="Reload">↻</button></div>`)}
      <div class="stepper-wrap"><ol class="stepper">${steps}</ol>
        <button class="evidence-tab" data-action="tab" data-id="evidence" aria-selected="${tab === "evidence"}">${ICON.shield}<span>Evidence</span><span class="evidence-tab__sub">hash-chained</span></button></div>
      ${turnBar(p)}
      ${S.error?.body?.problems ? `<div class="err">${ICON.alert}<div>${esc(S.error.message)}<ul>${S.error.body.problems.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div></div>` : ""}
      <main class="panel" style="margin-top:4px">${panel(tab, p)}</main>`;
  }

  function whoseTurn(p) {
    const m = {
      DRAFT: ["client", "freelancer"].filter((r) => !p.approvals.some((a) => a.role === r && a.terms_sha256 === p.terms_sha256)),
      TERMS_APPROVED: ["client"], AWAITING_BUYER_APPROVAL: ["client"], FUNDS_HELD: ["freelancer"], SUBMITTED: ["client", "freelancer", "arbiter"],
      AUDITED: ["client"], RELEASE_PROPOSED: ["freelancer"], DISPUTED: ["arbiter"],
    };
    return m[p.status] || [];
  }

  function btn(action, label, cls = "", extra = "") {
    const busy = S.busy === action;
    return `<button class="btn ${cls}" data-action="${action}" ${S.busy ? "disabled" : ""} ${extra}>${busy ? '<span class="spinner"></span>' : ""}${label}</button>`;
  }

  function turnBar(p) {
    const due = whoseTurn(p);
    const mine = due.includes(S.role);
    const live = p.paypal.mode === "live_sandbox";
    let headline = "", detail = "", ctas = "";
    const st = p.status;
    if (st === "DRAFT") {
      const approved = p.approvals.filter((a) => a.terms_sha256 === p.terms_sha256).map((a) => a.role);
      headline = mine ? "Review the payment policy, then approve its fingerprint" : `Waiting for ${due.map((r) => ROLE_LABEL[r]).join(" and ")} to approve`;
      detail = `Both client and freelancer approve SHA-256 ${short(p.terms_sha256, 12)}. Any edit creates a new fingerprint and cancels earlier approvals. ${approved.length ? `Approved so far: ${approved.join(", ")}.` : ""}`;
      if (mine) ctas = btn("approve", `${ICON.lock} Approve ${short(p.terms_sha256, 8)}`, "btn--primary") + btn("edit", "Edit terms", "btn--ghost");
    } else if (st === "TERMS_APPROVED") {
      headline = mine ? "Create the PayPal order for the locked amount" : "Waiting for the client to fund";
      detail = `Orders v2 intent AUTHORIZE for ${usd(p.terms.amount_cents)}: PayPal holds the funds; nothing is captured yet.`;
      if (mine) ctas = btn("order", "Create PayPal order", "btn--primary");
    } else if (st === "AWAITING_BUYER_APPROVAL") {
      headline = mine ? (live ? "Approve in PayPal Sandbox, then authorize" : "Simulate the buyer's PayPal approval, then authorize") : "Waiting for the client to authorize";
      detail = live ? "Opens PayPal Checkout (sandbox). Log in with a Sandbox personal (buyer) account and approve; PayPal returns here." : "In the simulator the buyer's approval is a labelled stand-in for PayPal Checkout.";
      if (mine) ctas = (live ? `<a class="btn btn--primary" href="${esc(p.paypal.approve_url)}">Approve in PayPal Sandbox</a>` : btn("sim-approve", "Simulate buyer approval", "btn--sim")) + btn("authorize", "Authorize & verify hold", live ? "" : "btn--primary");
    } else if (st === "FUNDS_HELD") {
      headline = mine ? "Deliver the work" : "Funds held — waiting for the freelancer to deliver";
      detail = `PayPal authorization ${esc(p.paypal.authorization_id)} holds ${usd(p.paypal.authorized_cents)} until ${when(p.paypal.authorization_expires_at)}.`;
      if (mine) ctas = btn("tab:deliver", "Choose a deliverable", "btn--primary");
      else if (S.role === "client") ctas = btn("void", "Cancel & void hold", "btn--danger");
    } else if (st === "SUBMITTED") {
      headline = "Run the evaluation: agreed tests in a sandbox, then the AI reviewer";
      detail = `Submission ${esc(p.submissions.at(-1)?.filename)} (${short(p.submissions.at(-1)?.sha256, 10)}). The AI review can take ~30 s on a local model.`;
      ctas = btn("evaluate", `${ICON.spark} Evaluate submission`, "btn--primary");
    } else if (st === "AUDITED") {
      const ev = p.evaluation;
      headline = mine ? `Decide the release: recommendation ${usd(ev.recommended_cents)} (${pct(ev.score_pct)})` : `Waiting for the client's decision (recommended ${usd(ev.recommended_cents)})`;
      detail = "The evaluation is a recommendation. Releasing the full amount captures at once; less is a proposal the freelancer accepts or disputes.";
      if (mine) ctas = btn("tab:decide", "Review & decide", "btn--primary");
      else if (S.role === "freelancer") ctas = btn("tab:deliver", "Resubmit", "btn--ghost");
    } else if (st === "RELEASE_PROPOSED") {
      const r = p.releases.find((x) => x.status === "PROPOSED");
      headline = mine ? `The client proposes ${usd(r?.amount_cents)} — accept or dispute` : "Waiting for the freelancer to accept the proposal";
      detail = r?.reason ? `Client's reason: “${esc(r.reason)}”` : "";
      if (mine) ctas = btn("accept", "Accept & capture", "btn--ok") + btn("tab:decide", "Dispute…", "btn--ghost");
    } else if (st === "DISPUTED") {
      headline = mine ? "Decide the dispute" : "Waiting for the arbiter";
      detail = "The arbiter sees the same evidence and decides an amount between $0 (void the hold) and the authorized amount.";
      if (mine) ctas = btn("tab:decide", "Open dispute", "btn--primary");
    } else if (["CAPTURING", "CAPTURE_PENDING", "CAPTURED", "PAYOUT_PENDING", "PAYOUT_UNCLAIMED", "PAYOUT_ATTENTION"].includes(st)) {
      headline = { CAPTURING: "Capture in flight — reconcile reads PayPal's record", CAPTURE_PENDING: "PayPal reports the capture PENDING", CAPTURED: "Captured — payout batch next", PAYOUT_PENDING: "Payouts batch created — waiting for PayPal's per-payee results", PAYOUT_UNCLAIMED: "Paid; a payee has not claimed their payout yet", PAYOUT_ATTENTION: "A payout item failed or is unconfirmed" }[st];
      detail = "Statuses are only what PayPal reports. Reconcile re-reads PayPal; it never resends money.";
      ctas = (live ? "" : btn("webhooks", "Deliver PayPal webhooks", "btn--sim")) + btn("reconcile", "Reconcile with PayPal", "btn--primary")
        + (S.role === "client" && st !== "CAPTURING" ? btn("release-again", "Try a second release", "btn--danger") : "");
    } else if (st === "PAID_OUT") {
      headline = "Paid out — every payee's item is SUCCESS in PayPal's record";
      detail = "Try releasing again: PactPay refuses it, and PayPal would too (same PayPal-Request-Id / sender_batch_id).";
      ctas = btn("tab:evidence", `${ICON.shield} Evidence`, "btn--ok") + (S.role === "client" ? btn("release-again", "Try a second release", "btn--danger") : "");
    } else if (st === "VOIDED") {
      headline = "The authorization was voided: the buyer's funds are released"; detail = "Confirmed by PayPal's authorization record (status VOIDED).";
    } else if (st === "CAPTURE_FAILED") {
      headline = "PayPal declined the capture"; detail = "See the PayPal calls drawer for PayPal's error and debug id.";
    }
    const cls = st === "PAID_OUT" ? "turn--done" : ["DISPUTED", "PAYOUT_ATTENTION", "CAPTURE_FAILED", "PAYOUT_UNCLAIMED"].includes(st) ? "turn--alert" : mine ? "turn--mine" : "";
    const who = due.length ? due : [S.role];
    const switchTo = !mine && due.length && S.session.personas[due[0]] ? `<button class="btn btn--sm" data-action="role" data-id="${due[0]}">Switch to ${ROLE_LABEL[due[0]]}</button>` : "";
    return `<section class="turn ${cls}"><div class="turn__who"><span class="persona__avatar av-${who[0]}">${who[0][0].toUpperCase()}</span>
        <div><div class="turn__label">${mine ? "Your turn" : due.length ? "Next" : "Status"}</div><div class="strong small">${due.map((r) => ROLE_LABEL[r]).join(" / ") || ROLE_LABEL[S.role]}</div></div></div>
      <div class="turn__body"><div class="turn__headline">${headline || esc(STATUS_TEXT[st] || st)}</div><div class="turn__detail">${detail}</div></div>
      <div class="turn__ctas">${ctas}${switchTo}</div></section>`;
  }

  function panel(tab, p) {
    switch (tab) {
      case "terms": return termsPanel(p);
      case "fund": return fundPanel(p);
      case "deliver": return deliverPanel(p);
      case "evaluate": return evaluatePanel(p);
      case "decide": return decidePanel(p);
      case "capture": case "payout": return moneyPanel(p);
      case "evidence": return evidencePanel(p);
      default: return termsPanel(p);
    }
  }

  // ---------- terms ----------
  function highlightChat(chat, quotes) {
    let html = esc(chat);
    for (const q of quotes) {
      if (!q) continue;
      const e = esc(q);
      const i = html.indexOf(e);
      if (i >= 0) html = `${html.slice(0, i)}<mark>${e}</mark>${html.slice(i + e.length)}`;
    }
    return html;
  }

  function termsPanel(p) {
    const t = p.terms;
    const locked = !!p.locked_terms_sha256;
    const splits = splitCents(t.amount_cents || 0, t.payees || []);
    const methodChip = { tests: '<span class="chip chip--ok">executable tests</span>', ai_review: '<span class="chip chip--violet">AI review</span>', manual: '<span class="chip chip--warn">manual review</span>' };
    const crit = (t.criteria || []).map((c) => `
      <div class="criterion"><div class="criterion__head"><span class="criterion__id">${esc(c.id)}</span><span class="criterion__title">${esc(c.title)}</span>
        ${methodChip[c.method] || ""}<span class="criterion__weight">${pct(c.weight_pct)} · ${usd(Math.round((t.amount_cents || 0) * (c.weight_pct || 0) / 100))}</span></div>
        <div class="small muted">${esc(c.description)}</div>
        ${c.source_quote ? `<blockquote class="quote"><span class="quote__src">Found verbatim in the chat</span>${esc(c.source_quote)}</blockquote>` : ""}
        ${c.tests ? `<details class="code-details"><summary>Acceptance tests (${esc(c.tests_origin || "agreed")}) · ${(c.tests.match(/^def test_/gm) || []).length} tests</summary>${codeBlock(c.tests)}</details>` : ""}
      </div>`).join("");
    const payees = (t.payees || []).map((x, i) => `<tr><td data-label="Payee"><div class="strong">${esc(x.name)}</div><div class="tiny muted">${esc(x.role || "")}</div></td>
      <td data-label="PayPal e-mail" class="break mono">${esc(x.paypal_email)}</td><td data-label="Share" class="num">${pct(x.share_pct)}</td><td data-label="Of the total" class="num">${usd(splits[i])}</td></tr>`).join("");
    const approvals = ["client", "freelancer"].map((r) => {
      const a = p.approvals.find((x) => x.role === r && x.terms_sha256 === p.terms_sha256);
      const stale = !a && p.approvals.some((x) => x.role === r);
      return `<div class="approval"><span class="persona__avatar av-${r}">${r[0].toUpperCase()}</span><span class="approval__who">${esc(p.parties[r]?.name || ROLE_LABEL[r])}</span>
        ${a ? `<span class="chip chip--ok">${ICON.check} approved ${short(a.terms_sha256, 8)}</span>` : stale ? '<span class="chip chip--warn">earlier version approved — lapsed</span>' : '<span class="chip">not yet</span>'}</div>`;
    }).join("");
    const notes = [...(p.draft_notes || []), ...(S.draftNotes?.[p.pact_id] || [])];
    const problems = S.draftProblems?.[p.pact_id] || [];
    return `<div class="split">
      <div class="stack-lg">
        <div class="panel__head"><div><div class="panel__title"><h2>Payment policy</h2>${locked ? `<span class="chip chip--ok">${ICON.lock} locked</span>` : '<span class="chip chip--warn">draft — editable</span>'}</div>
          <p class="panel__lead small">${esc(t.summary || "")}</p></div><div class="amount amount--xl">${usd(t.amount_cents)} <small>${esc(t.currency)}</small></div></div>
        ${S.editing && !locked ? editForm(t) : ""}
        ${problems.length ? `<div class="callout callout--warn">${ICON.alert}<div><strong>Needs human review before approval:</strong><ul>${problems.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div></div>` : ""}
        ${notes.length ? `<div class="callout callout--warn">${ICON.alert}<div><strong>The AI draft was checked against the chat:</strong><ul>${notes.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div></div>` : ""}
        <div class="card"><div class="card__head"><div class="card__title">Acceptance criteria</div><span class="tiny muted">weights sum to ${pct((t.criteria || []).reduce((s, c) => s + (Number(c.weight_pct) || 0), 0))}</span></div><div class="criteria">${crit || '<p class="muted">No criteria yet — edit the terms.</p>'}</div></div>
        <div class="card"><div class="card__head"><div class="card__title">Payees (PayPal Payouts)</div><span class="tiny muted">split of what PayPal actually captures, net of PayPal's fee, to the cent</span></div>
          <div class="table-wrap"><table class="table payout-table"><thead><tr><th>Payee</th><th>PayPal e-mail</th><th class="num">Share</th><th class="num">Of ${usd(t.amount_cents)}</th></tr></thead><tbody>${payees}</tbody></table></div></div>
      </div>
      <div class="stack-lg">
        <div class="${locked ? "lock" : "lock lock--open"}">${locked ? ICON.lock : ICON.unlock}<div><div class="strong">${locked ? "Terms locked by both parties" : "Not locked yet"}</div>
          <div class="tiny">SHA-256 of the canonical terms (amount, criteria, tests, payees, parties, chat hash):</div>
          <div class="hash" style="margin-top:4px"><span class="break">${esc(p.locked_terms_sha256 || p.terms_sha256)}</span><button data-action="copy" data-text="${esc(p.locked_terms_sha256 || p.terms_sha256)}" title="Copy">${ICON.copy}</button></div>
          <div class="tiny" style="margin-top:6px">A fingerprint for tamper-evidence inside PactPay — not a legal or cryptographic signature.</div></div></div>
        <div class="card"><div class="card__title" style="margin-bottom:8px">Approvals</div><div class="approvals">${approvals}</div></div>
        <div class="card"><div class="card__head"><div class="card__title">Source agreement</div><span class="tiny muted">${esc(p.drafted_by)}</span></div>
          <div class="chat">${highlightChat(S.chat || "", (t.criteria || []).map((c) => c.source_quote))}</div>
          <div class="tiny muted" style="margin-top:6px">chat SHA-256 <span class="mono">${short(t.source?.chat_sha256, 16)}</span> is part of the locked terms.</div></div>
      </div></div>`;
  }

  function editForm(t) {
    const rows = (t.payees || []).map((x, i) => `<div class="grid-3" data-payee="${i}">
      <div class="field"><label>Name</label><input class="input" name="name" value="${esc(x.name)}"></div>
      <div class="field"><label>PayPal e-mail</label><input class="input" name="paypal_email" value="${esc(x.paypal_email)}"></div>
      <div class="field"><label>Share %</label><input class="input" name="share_pct" inputmode="decimal" value="${esc(x.share_pct)}"></div></div>`).join("");
    const crit = (t.criteria || []).map((c, i) => `<div class="row" data-crit="${i}"><span class="criterion__id">${esc(c.id)}</span><span class="small" style="flex:1">${esc(c.title)}</span>
      <input class="input" style="max-width:110px" name="weight_pct" inputmode="decimal" value="${esc(c.weight_pct)}"><span class="tiny muted">% weight</span></div>`).join("");
    return `<form class="card card--soft decision-form" id="edit-form">
      <div class="grid-2"><div class="field"><label>Title</label><input class="input" name="title" value="${esc(t.title)}"></div>
        <div class="field"><label>Amount (USD)</label><div class="money-input"><span>$</span><input class="input" name="amount" inputmode="decimal" value="${((t.amount_cents || 0) / 100).toFixed(2)}"></div></div></div>
      <div class="stack"><div class="eyebrow">Payees</div>${rows}</div>
      <div class="stack"><div class="eyebrow">Criterion weights</div>${crit}</div>
      <div class="decision-actions">${btn("save-terms", "Save — creates a new fingerprint", "btn--primary")}${btn("cancel-edit", "Cancel", "btn--ghost")}</div></form>`;
  }

  // ---------- fund ----------
  function fundPanel(p) {
    const pp = p.paypal;
    const live = pp.mode === "live_sandbox";
    const st = p.status;
    const ordered = !!pp.order_id, held = !!pp.authorization_id;
    const approved = ordered && (pp.order_status === "APPROVED" || held);
    const item = (done, cur, n, title, body) => `<li class="sl-item ${done ? "sl-item--done" : cur ? "sl-item--current" : ""}"><span class="sl-dot">${done ? ICON.check : n}</span><div class="sl-body"><div class="sl-title">${title}</div>${body}</div></li>`;
    return `<div class="split"><div class="card"><div class="card__head"><div class="card__title">Hold the funds with PayPal</div><span class="chip ${live ? "chip--live" : "chip--sim"}">${live ? "Live Sandbox" : "Simulator"}</span></div>
      <ol class="steps-list">
        ${item(ordered, st === "TERMS_APPROVED", 1, "Create order — <code>POST /v2/checkout/orders</code>", ordered ? `<dl class="kv"><dt>Order</dt><dd class="mono">${esc(pp.order_id)}</dd><dt>Intent</dt><dd>AUTHORIZE · ${usd(p.terms.amount_cents)} · reference_id ${esc(p.pact_id)}</dd><dt>PayPal status</dt><dd>${esc(pp.order_status)}</dd></dl>` : '<span class="muted small">Built only from the locked terms; PayPal-Request-Id makes a retry return the same order.</span>')}
        ${item(approved, st === "AWAITING_BUYER_APPROVAL" && !held, 2, "Buyer approves in PayPal Checkout", live ? (pp.approve_url ? `<a class="btn btn--sm" href="${esc(pp.approve_url)}">Open PayPal Sandbox approval</a><span class="tiny muted">Approval happens on paypal.com, never in PactPay.</span>` : "") : '<span class="sim-label">SIMULATED — in live mode this is PayPal Checkout</span>')}
        ${item(held, st === "AWAITING_BUYER_APPROVAL", 3, "Authorize & verify — <code>POST …/authorize</code>, then <code>GET</code> the order", held ? `<dl class="kv"><dt>Authorization</dt><dd class="mono">${esc(pp.authorization_id)}</dd><dt>PayPal status</dt><dd>${esc(pp.authorization_status)}</dd><dt>Held</dt><dd class="strong">${usd(pp.authorized_cents)}</dd><dt>Expires</dt><dd>${when(pp.authorization_expires_at)}</dd></dl><div class="callout callout--ok">${ICON.shield}<div>Accepted only because PayPal's record of the order shows this authorization for exactly the locked amount, currency and pact id.</div></div>` : '<span class="muted small">Refused unless PayPal\'s own record matches the locked amount and pact.</span>')}
      </ol></div>
      <div class="stack-lg"><div class="card card--soft"><div class="card__title">Why an authorization?</div><p class="small muted" style="margin-top:6px">PayPal places a hold on the buyer's funds without moving them. PactPay captures later, once, for the amount a person decides — or voids the hold. PayPal's authorization is honoured for 3 days and valid for up to 29; PactPay is a demo of this lifecycle, not a licensed escrow service.</p></div>
      ${st === "FUNDS_HELD" && S.role === "client" ? `<div class="card"><div class="card__title">Cancel before delivery</div><p class="small muted">Voids the authorization at PayPal and confirms VOIDED from PayPal's record.</p><div style="margin-top:8px">${btn("void", "Void hold", "btn--danger")}</div></div>` : ""}</div></div>`;
  }

  // ---------- deliver ----------
  function deliverPanel(p) {
    const can = (p.next_actions || []).includes("submit") && S.role === "freelancer";
    const samples = S.samples.map((s) => `<div class="sample ${s.kind === "adversarial" ? "sample--adv" : ""}">
      <div class="row row--between"><span class="sample__name">${esc(s.label)}</span>${s.kind === "adversarial" ? '<span class="chip chip--warn">manipulated</span>' : '<span class="chip">honest</span>'}</div>
      <span class="sample__file">${esc(s.filename)}</span>
      <div class="row row--between"><span class="tiny muted">expected score ${esc(s.expected_score)}%</span>${can ? btn(`submit-sample:${s.id}`, "Submit this", "btn--sm") : ""}</div></div>`).join("");
    const subs = (p.submissions || []).slice().reverse().map((s) => `<tr><td class="mono">${esc(s.filename)}</td><td class="mono tiny">${short(s.sha256, 16)}</td><td class="num">${(s.chars || 0).toLocaleString()} chars</td><td class="tiny muted">${when(s.created_at)}</td></tr>`).join("");
    return `<div class="split"><div class="stack-lg">
        <div class="panel__head"><div><h2>Deliver the work</h2><p class="panel__lead small">The freelancer submits a text deliverable (code or Markdown). It is hashed, then evaluated against the locked criteria.</p></div></div>
        ${can ? "" : `<div class="callout callout--brand">${ICON.alert}<div>${S.role !== "freelancer" ? "Only the freelancer submits work. Switch persona to submit." : "Submissions are accepted while funds are held and before a release."}</div></div>`}
        ${S.samples.length ? `<div class="card"><div class="card__head"><div class="card__title">Demo deliverables</div><span class="tiny muted">including manipulated ones that try to fool the reviewer</span></div><div class="samples">${samples}</div></div>` : ""}
        ${can ? `<div class="card"><div class="card__title" style="margin-bottom:8px">Or submit your own</div><div class="stack">
          <div class="field"><label for="sub-name">File name</label><input class="input" id="sub-name" value="deliverable.py"></div>
          <div class="field"><label for="sub-body">Content</label><textarea class="textarea mono" id="sub-body" rows="10" placeholder="Paste code or Markdown"></textarea></div>
          <div class="file-drop"><input type="file" id="sub-file" accept=".py,.md,.txt,.js,.ts,.json"><span class="tiny muted">UTF-8 text, up to 200,000 characters</span></div>
          <div>${btn("submit-own", "Submit deliverable", "btn--primary")}</div></div></div>` : ""}
      </div>
      <div class="card"><div class="card__title" style="margin-bottom:8px">Submissions</div>${subs ? `<div class="table-wrap"><table class="table"><thead><tr><th>File</th><th>SHA-256</th><th class="num">Size</th><th>When</th></tr></thead><tbody>${subs}</tbody></table></div>` : '<p class="muted small">Nothing submitted yet.</p>'}</div></div>`;
  }

  // ---------- evaluate ----------
  function evaluatePanel(p) {
    const ev = p.evaluation;
    if (!ev) return `<div class="card"><p class="muted">No evaluation yet. ${p.status === "SUBMITTED" ? btn("evaluate", `${ICON.spark} Evaluate submission`, "btn--primary") : "The freelancer submits first."}</p></div>`;
    const r = ev.report || {};
    const terms = Object.fromEntries((p.terms.criteria || []).map((c) => [c.id, c]));
    const weights = (r.criteria || []).map((c) => `<span class="${{ PASS: "w-pass", FAIL: "w-fail", NEEDS_HUMAN_REVIEW: "w-review" }[c.status] || "w-none"}" style="flex:${terms[c.id]?.weight_pct || 1}" title="${esc(c.id)} ${esc(c.status)}"></span>`).join("");
    const flags = [...(r.flags || [])];
    const inj = r.injection_evidence || [];
    const cards = (r.criteria || []).map((c) => {
      const t = terms[c.id] || {};
      const tests = (c.tests || []).map((x) => `<li class="test ${x.passed ? "test--pass" : "test--fail"}">${x.passed ? ICON.check : ICON.x}<span><span class="test__name">${esc(x.name)}</span>${x.passed ? "" : `<span class="test__detail">${esc(x.detail)}</span>`}</span><span class="test__ms">${esc(x.duration_ms)} ms</span></li>`).join("");
      const ai = c.ai;
      const cites = (ai?.citations || []).map((q, i) => {
        const key = `${c.id}:${q.line_start}`;
        return `<button class="cite ${q.verified ? "" : "cite--rejected"} ${S.citeKey === key ? "cite--active" : ""}" data-action="cite" data-id="${esc(key)}" data-end="${esc(q.line_end)}" title="${q.verified ? "Quote found on the cited lines" : "Quote NOT found on those lines — rejected"}">
          <span class="cite__lines">${ICON.eye} L${esc(q.line_start)}${q.line_end !== q.line_start ? `–${esc(q.line_end)}` : ""}</span><span class="cite__quote">${esc(q.quote)}</span></button>`;
      }).join("");
      const verdictChip = ai ? `<span class="chip ${ai.verdict === "pass" ? "chip--ok" : ai.verdict === "fail" ? "chip--bad" : "chip--warn"}">AI: ${esc(ai.verdict)}</span>${ai.accepted ? '<span class="chip chip--ok">citations verified</span>' : '<span class="chip chip--warn">not counted</span>'}` : "";
      return `<div class="eval-card eval-card--${esc(c.status)}">
        <div class="criterion__head"><span class="criterion__id">${esc(c.id)}</span><span class="criterion__title">${esc(t.title || "")}</span>
          <span class="chip ${c.status === "PASS" ? "chip--ok" : c.status === "FAIL" ? "chip--bad" : "chip--warn"}">${esc(c.status === "NEEDS_HUMAN_REVIEW" ? "NEEDS HUMAN REVIEW" : c.status)}</span>
          <span class="criterion__weight">${pct(c.earned_pct)} of ${pct(t.weight_pct)}</span></div>
        ${tests ? `<div><div class="eyebrow" style="margin-bottom:4px">Agreed tests in the sandbox · ${esc(c.tests_passed)}/${esc(c.tests_total)} passed</div><ul class="tests">${tests}</ul></div>` : `<div class="small muted">${esc(c.evidence || "")}</div>`}
        ${ai ? `<div class="ai"><div class="ai__head">${ICON.spark}<span class="strong">AI reviewer</span>${verdictChip}<span class="tiny muted">${esc(ai.reason || "")}</span></div>
          <div class="ai__exp">${esc(ai.explanation || "")}</div>${cites ? `<div class="cites">${cites}</div>` : ""}</div>` : ""}
      </div>`;
    }).join("");
    const rv = r.reviewer || {};
    const hl = new Set(), flagLines = new Set((inj || []).map((x) => x.line));
    if (S.citeKey) {
      const [cid, start] = S.citeKey.split(":");
      const q = (r.criteria || []).find((c) => c.id === cid)?.ai?.citations?.find((x) => String(x.line_start) === start);
      if (q) for (let i = q.line_start; i <= q.line_end; i++) hl.add(i);
    }
    const lines = (S.submission?.content || "").split("\n").map((t, i) => `<div class="vline ${hl.has(i + 1) ? "vline--hl" : ""} ${flagLines.has(i + 1) ? "vline--flag" : ""}" id="vl-${i + 1}"><span class="vline__n">${i + 1}</span><span class="vline__t">${esc(t) || " "}</span></div>`).join("");
    return `<div class="split"><div class="stack-lg">
        <div class="card"><div class="score">
          <div class="ring" style="--p:${Number(ev.score_pct)};--ring:${ev.score_pct >= 99.99 ? "var(--ok)" : ev.score_pct > 0 ? "var(--warn-strong)" : "var(--bad)"}"><div class="ring__in">${Math.round(ev.score_pct)}%</div></div>
          <div class="stack" style="flex:1;min-width:220px"><div class="row row--between"><span class="strong">Recommended release ${usd(ev.recommended_cents)} of ${usd(p.paypal.authorized_cents)}</span><span class="chip">${esc(r.engine_version || "")}</span></div>
            <div class="weights">${weights}</div>
            <div class="ai-note">${ICON.shield}<span>${esc(r.release_policy || "recommendation only")}. The AI can turn a passing test into “needs human review”, never a failing one into a pass.</span></div>
            <div class="tiny muted">Reviewer: ${esc(rv.model || "none")} · ${esc(rv.status || "")} ${rv.latency_ms ? `· ${(rv.latency_ms / 1000).toFixed(1)} s` : ""} ${rv.detail ? `· ${esc(rv.detail)}` : ""}</div></div></div></div>
        ${flags.length || inj.length ? `<div class="flag-banner ${flags.some((f) => /tamper|injection/.test(f)) ? "" : "flag-banner--warn"}"><div class="flag-banner__title">${ICON.alert} Flags raised by the evaluator</div><ul>${flags.map((f) => `<li><code>${esc(f)}</code></li>`).join("")}${inj.map((x) => `<li>line ${esc(x.line)} <span class="chip">${esc(x.kind || "")}</span> <code>${esc(x.excerpt || "")}</code></li>`).join("")}</ul></div>` : ""}
        ${cards}
      </div>
      <div class="card viewer"><div class="viewer__head"><div class="row row--between"><span class="strong mono">${esc(S.submission?.filename || "")}</span><span class="tiny muted mono">${short(S.submission?.sha256, 12)}</span></div>
        <span class="tiny muted">Click a citation to see the exact lines the AI relied on. Struck-through citations did not match the file and were ignored.</span></div>
        <div class="viewer__body">${lines}</div></div></div>`;
  }

  // ---------- decide ----------
  function decidePanel(p) {
    const ev = p.evaluation;
    if (!ev) return '<div class="card"><p class="muted">The decision comes after an evaluation.</p></div>';
    const st = p.status;
    const proposal = p.releases.find((x) => x.status === "PROPOSED");
    let form = "";
    if (S.role === "client" && st === "AUDITED") {
      form = `<form class="card decision-form" id="decide-form"><div class="card__title">Client decision</div>
        <div class="grid-2"><div class="field"><label for="amt">Release amount (USD)</label><div class="money-input"><span>$</span><input class="input" id="amt" inputmode="decimal" value="${(ev.recommended_cents / 100).toFixed(2)}"></div>
          <span class="field__hint">Between $0.01 and ${usd(p.paypal.authorized_cents)}. Full amount captures now; less is proposed to the freelancer.</span></div>
          <div class="field"><label for="why">Reason <span class="tiny muted">(required if it differs from the recommendation)</span></label><textarea class="textarea" id="why" rows="3"></textarea></div></div>
        <div class="decision-actions">${btn("release", "Release", "btn--ok")}${btn("release-full", `Release full ${usd(p.paypal.authorized_cents)}`, "")}${btn("dispute", "Dispute → arbiter", "btn--ghost")}</div></form>`;
    } else if (S.role === "freelancer" && ["AUDITED", "RELEASE_PROPOSED"].includes(st)) {
      form = `<form class="card decision-form"><div class="card__title">${proposal ? `The client proposes ${usd(proposal.amount_cents)}` : "Disagree with the evaluation?"}</div>
        ${proposal?.reason ? `<blockquote class="quote"><span class="quote__src">Client's reason</span>${esc(proposal.reason)}</blockquote>` : ""}
        <div class="field"><label for="why">Reason for a dispute</label><textarea class="textarea" id="why" rows="3"></textarea></div>
        <div class="decision-actions">${proposal ? btn("accept", `Accept ${usd(proposal.amount_cents)}`, "btn--ok") : ""}${btn("dispute", "Dispute → arbiter", "btn--ghost")}</div></form>`;
    } else if (S.role === "arbiter" && st === "DISPUTED") {
      form = `<form class="card decision-form"><div class="card__title">Arbiter decision</div>
        <div class="grid-2"><div class="field"><label for="amt">Amount to capture (USD)</label><div class="money-input"><span>$</span><input class="input" id="amt" inputmode="decimal" value="${(ev.recommended_cents / 100).toFixed(2)}"></div><span class="field__hint">$0 voids the hold and returns everything to the buyer.</span></div>
          <div class="field"><label for="why">Reason (on the record)</label><textarea class="textarea" id="why" rows="3"></textarea></div></div>
        <div class="decision-actions">${btn("resolve", "Decide", "btn--primary")}</div></form>`;
    } else {
      form = `<div class="callout callout--brand">${ICON.alert}<div>${st === "AUDITED" ? "The client decides. Switch to the client persona." : st === "DISPUTED" ? "The arbiter decides. Switch to the arbiter persona." : st === "RELEASE_PROPOSED" ? "The freelancer accepts or disputes." : "The decision has been made."}</div></div>`;
    }
    const rels = p.releases.map((r) => `<tr><td>#${esc(r.seq)}</td><td>${esc(r.decided_role)}</td><td class="num strong">${usd(r.amount_cents)}</td><td class="num">${usd(r.recommended_cents)}</td><td>${esc(r.status)}</td><td class="small">${esc(r.reason || "")}</td></tr>`).join("");
    return `<div class="stack-lg"><div class="decision-summary">
        <div class="metric"><span class="metric__label">Held by PayPal</span><span class="metric__value">${usd(p.paypal.authorized_cents)}</span><span class="metric__sub">authorization ${esc(p.paypal.authorization_id)}</span></div>
        <div class="metric"><span class="metric__label">Evaluation score</span><span class="metric__value">${pct(ev.score_pct)}</span><span class="metric__sub">tests + AI reviewer</span></div>
        <div class="metric metric--brand"><span class="metric__label">Recommended release</span><span class="metric__value">${usd(ev.recommended_cents)}</span><span class="metric__sub">a person decides</span></div></div>
      ${form}
      ${rels ? `<div class="card"><div class="card__title" style="margin-bottom:8px">Decisions on record</div><div class="table-wrap"><table class="table"><thead><tr><th>#</th><th>By</th><th class="num">Amount</th><th class="num">Recommended</th><th>Status</th><th>Reason</th></tr></thead><tbody>${rels}</tbody></table></div></div>` : ""}</div>`;
  }

  // ---------- money ----------
  function moneyPanel(p) {
    const rel = [...p.releases].reverse().find((r) => ["CAPTURED", "PENDING", "APPROVED", "FAILED"].includes(r.status));
    const po = p.payout;
    if (!rel) return `<div class="card"><p class="muted">Nothing captured yet. PayPal captures only after a person's release decision.</p></div>`;
    const items = (po?.items || []).map((it) => {
      const payee = (p.terms.payees || []).find((x) => x.id === it.payee_id) || {};
      const s = it.transaction_status || "not sent";
      const cls = s === "SUCCESS" ? "chip--ok" : ["UNCLAIMED", "PENDING", "ONHOLD", "not sent"].includes(s) ? "chip--warn" : "chip--bad";
      return `<tr><td data-label="Payee"><div class="strong">${esc(payee.name || it.payee_id)}</div><div class="tiny muted mono">${esc(it.receiver)}</div></td><td data-label="Share" class="num">${pct(payee.share_pct)}</td>
        <td data-label="Amount" class="num strong">${usd(it.amount_cents)}</td><td data-label="PayPal status"><span class="chip ${cls}">${esc(s)}</span>${it.errors ? `<div class="tiny muted">${esc(it.errors.name || "")}</div>` : ""}</td>
        <td data-label="Payout item" class="mono tiny">${esc(it.payout_item_id || "—")}</td></tr>`;
    }).join("");
    const paid = (po?.items || []).filter((x) => x.transaction_status === "SUCCESS").reduce((s, x) => s + x.amount_cents, 0);
    return `<div class="stack-lg">
      <div class="ledger">
        <div class="metric"><span class="metric__label">Captured (gross)</span><span class="metric__value">${usd(rel.captured_cents ?? rel.amount_cents)}</span><span class="metric__sub">capture ${esc(rel.capture_id || "—")} · ${esc(rel.capture_status || rel.status)}</span></div>
        <div class="metric"><span class="metric__label">PayPal fee</span><span class="metric__value">${usd(rel.fee_cents)}</span><span class="metric__sub">from PayPal's seller_receivable_breakdown</span></div>
        <div class="metric metric--brand"><span class="metric__label">Net to split</span><span class="metric__value">${usd(rel.net_cents)}</span><span class="metric__sub">largest-remainder split, exact to the cent</span></div>
        <div class="metric"><span class="metric__label">Confirmed paid</span><span class="metric__value">${usd(paid)}</span><span class="metric__sub">items PayPal reports SUCCESS</span></div></div>
      <div class="card"><div class="card__head"><div class="card__title">Payouts batch ${po?.payout_batch_id ? `<span class="mono">${esc(po.payout_batch_id)}</span>` : ""}<span class="chip">${esc(po?.batch_status || "not created")}</span></div>
        <span class="tiny muted mono">sender_batch_id ${esc(po?.sender_batch_id || "—")}</span></div>
        ${po?.error ? `<div class="callout callout--warn">${ICON.alert}<div>${esc(po.error.name || "")}: ${esc(po.error.message || "")}</div></div>` : ""}
        ${items ? `<div class="table-wrap"><table class="table payout-table"><thead><tr><th>Payee</th><th class="num">Share</th><th class="num">Amount</th><th>PayPal status</th><th>Payout item</th></tr></thead><tbody>${items}</tbody></table></div>` : '<p class="muted small">The batch is created right after PayPal confirms the capture COMPLETED.</p>'}
        <div class="tiny muted" style="margin-top:8px">A batch being accepted is not payment: each item is final only when PayPal reports it (SUCCESS, UNCLAIMED, FAILED, RETURNED…). Unclaimed items return to the sender after 30 days.</div></div>
      ${rel.error ? `<div class="callout callout--bad">${ICON.alert}<div>PayPal: ${esc(rel.error.name || "")} ${esc(rel.error.issue || "")} — debug id ${esc(rel.error.debug_id || "")}</div></div>` : ""}</div>`;
  }

  // ---------- evidence ----------
  function evidencePanel(p) {
    const e = S.evidence;
    if (!e) return '<div class="card"><span class="spinner"></span> Loading evidence…</div>';
    const ch = e.chain || {};
    const reqs = (e.requirements || []).map((r) => `<div class="req"><div class="criterion__head"><span class="criterion__id">${esc(r.criterion.id)}</span><span class="criterion__title">${esc(r.criterion.title)}</span><span class="chip ${r.status === "PASS" ? "chip--ok" : r.status === "FAIL" ? "chip--bad" : "chip--warn"}">${esc(r.status || "—")}</span></div>
      <div class="links">
        <div class="link"><span class="link__k">Agreed</span><span class="link__v">${esc(r.criterion.source_quote || r.criterion.description)}</span></div>
        <div class="link"><span class="link__k">Tests</span><span class="link__v">${r.acceptance_tests_sha256 ? `${esc(r.tests_passed)}/${esc(r.tests_total)} passed · <span class="mono">${short(r.acceptance_tests_sha256, 8)}</span>` : "no executable tests"}</span></div>
        <div class="link"><span class="link__k">AI reviewer</span><span class="link__v">${r.ai ? `${esc(r.ai.verdict)} — ${esc(r.ai.explanation || "")}` : "not consulted"}</span></div>
        <div class="link"><span class="link__k">Earned</span><span class="link__v strong">${pct(r.earned_pct)}</span></div>
        <div class="link"><span class="link__k">Decision</span><span class="link__v">${e.release ? `${esc(e.release.decided_role)} released ${usd(e.release.amount_cents)}` : "pending"}</span></div></div></div>`).join("");
    const kind = (k) => (k.startsWith("paypal.") ? "tl--paypal" : k.startsWith("release.") || k.startsWith("dispute.") || k.startsWith("terms.") ? "tl--decision" : k.startsWith("evaluation.") ? "tl--eval" : "");
    const tl = (e.events || []).map((ev) => `<li class="tl ${kind(ev.kind)}"><span class="tl__seq">#${esc(ev.seq)}</span><div class="tl__body"><span class="tl__what">${esc(ev.kind)} ${ev.data?.status ? `→ <span class="mono small">${esc(ev.data.status)}</span>` : ""}</span>
      <span class="tl__meta"><span>${when(ev.at)}</span><span>${esc(ev.actor === "system" ? "system" : Object.entries(p.parties).find(([, v]) => v.user_id === ev.actor)?.[0] || ev.actor)}</span>${ev.data?.mode ? `<span class="chip ${ev.data.mode === "live_sandbox" ? "chip--live" : "chip--sim"}">${esc(ev.data.mode)}</span>` : ""}<span class="tl__hash">${short(ev.sha256, 12)}</span></span>
      ${eventDetail(ev)}</div></li>`).join("");
    return `<div class="stack-lg"><div class="card"><div class="chain-badge ${ch.valid ? "chain-ok" : "chain-bad"}"><span class="chain-badge__icon">${ch.valid ? ICON.shield : ICON.alert}</span>
        <div style="flex:1"><div class="strong">${ch.valid ? `Event log intact: ${esc(ch.events)} events, each hashing the previous one` : `Event log broken at #${esc(ch.broken_at)}`}</div><div class="tiny muted mono break">head ${esc(ch.head_sha256)}</div></div>
        <button class="btn btn--sm" data-action="download-evidence">Download evidence JSON</button></div></div>
      <div class="card"><div class="card__title" style="margin-bottom:10px">Requirement → test → AI → decision</div><div class="req-chain">${reqs}</div></div>
      <div class="card"><div class="card__title" style="margin-bottom:10px">Every state change</div><ol class="timeline">${tl}</ol></div></div>`;
  }

  function eventDetail(ev) {
    const d = ev.data || {};
    const keys = ["order_id", "authorization_id", "capture_id", "payout_batch_id", "paypal_status", "amount_cents", "score_pct", "terms_sha256", "reason", "sender_batch_id"];
    const parts = keys.filter((k) => d[k] !== undefined && d[k] !== null).map((k) => `${k}=${k.endsWith("cents") ? usd(d[k]) : k.endsWith("sha256") ? short(d[k], 10) : d[k]}`);
    return parts.length ? `<span class="tiny mono muted break">${esc(parts.join(" · "))}</span>` : "";
  }

  // ---------- drawer ----------
  function renderDrawer() {
    const root = $("#drawer-root");
    if (!S.drawer) { root.innerHTML = ""; return; }
    const tr = S.traces?.traces || [];
    const live = S.traces?.mode === "live_sandbox";
    root.innerHTML = `<div class="drawer-backdrop" data-action="close-drawer"></div><aside class="drawer" role="dialog" aria-label="PayPal calls">
      <div class="drawer__head"><h2>PayPal REST calls</h2><span class="chip ${live ? "chip--live" : "chip--sim"}">${live ? "Live Sandbox" : "Simulator"}</span><button class="btn btn--sm btn--ghost" data-action="close-drawer">Close</button></div>
      <div class="drawer__note small muted">Every request PactPay sent and PayPal's answer, with PayPal-Request-Id and paypal-debug-id. Tokens are redacted.</div>
      <div class="drawer__body">${tr.length ? tr.slice().reverse().map((t) => `<details class="trace ${live ? "trace--live" : "trace--sim"}"><summary><span class="trace__top"><span class="http http--${String(t.status_code)[0]}">${esc(t.status_code)}</span><span class="trace__step">${esc(t.step)}</span><span class="trace__ep">${esc(t.method)} ${esc(t.endpoint)}</span></span>
        <span class="trace__meta"><span>${when(t.at)}</span><span>${Number(t.duration_ms).toFixed(0)} ms</span>${t.paypal_request_id ? `<span class="mono">req ${esc(t.paypal_request_id)}</span>` : ""}${t.paypal_debug_id ? `<span class="mono">debug ${esc(t.paypal_debug_id)}</span>` : ""}</span></summary>
        <pre class="trace__json">${esc(JSON.stringify({ request: t.request, response: t.response }, null, 1))}</pre></details>`).join("") : '<p class="muted">No PayPal calls yet.</p>'}</div></aside>`;
  }

  function codeBlock(src) {
    return `<div class="code">${String(src).replace(/^\n/, "").split("\n").map((l, i) => `<div class="code__line"><span class="code__n">${i + 1}</span><span class="code__t">${esc(l) || " "}</span></div>`).join("")}</div>`;
  }

  function splitCents(total, payees) {
    const basis = payees.map((x) => Math.round(Number(x.share_pct || 0) * 100));
    if (basis.reduce((a, b) => a + b, 0) !== 10000) return payees.map(() => null);
    const exact = basis.map((b) => total * b);
    const parts = exact.map((e) => Math.floor(e / 10000));
    const order = payees.map((_, i) => i).sort((a, b) => (exact[b] % 10000) - (exact[a] % 10000) || a - b);
    for (const i of order.slice(0, total - parts.reduce((a, b) => a + b, 0))) parts[i] += 1;
    return parts;
  }

  // ------------------------------------------------------------------------------------------------ actions ----
  const ACTIONS = {
    home: () => { S.pactId = null; S.pact = null; history.replaceState(null, "", "/"); render(); },
    open: (el) => openPact(el.dataset.id),
    "start-preset": (el) => run(`start:${el.dataset.id}`, async () => startDemo({ preset_id: el.dataset.id })),
    "start-custom": () => {
      S.customChat = $("#custom-chat").value;
      if (S.customChat.trim().length < 30) { toast("Write the agreement first (at least a few sentences).", true); return; }
      return run("start:custom", async () => startDemo({ chat_text: S.customChat }));
    },
    role: (el) => { S.role = el.dataset.id; S.session.active = S.role; saveSession(); S.error = null; return run("role", refresh); },
    tab: async (el) => { S.tab = el.dataset.id; S.citeKey = null; if (S.tab === "evidence") await run("evidence", refresh); else render(); },
    refresh: () => run("refresh", refresh),
    copy: (el) => { navigator.clipboard?.writeText(el.dataset.text).then(() => toast("Copied"), () => {}); },
    edit: () => { S.editing = true; S.tab = "terms"; render(); },
    "cancel-edit": () => { S.editing = false; render(); },
    "save-terms": () => {
      // read the form before run() re-renders it (a re-render resets every input to the stored terms)
      const f = $("#edit-form");
      const t = S.pact.terms;
      const payees = t.payees.map((x, i) => {
        const row = f.querySelector(`[data-payee="${i}"]`);
        return { ...x, name: row.querySelector('[name="name"]').value, paypal_email: row.querySelector('[name="paypal_email"]').value.trim(), share_pct: row.querySelector('[name="share_pct"]').value.trim() };
      });
      const criteria = t.criteria.map((c, i) => ({ ...c, weight_pct: f.querySelector(`[data-crit="${i}"] [name="weight_pct"]`).value.trim() }));
      const amount = toCents(f.querySelector('[name="amount"]').value);
      const title = f.querySelector('[name="title"]').value;
      if (amount === null) { toast("Enter the amount as dollars with at most 2 decimals, e.g. 500 or 500.00", true); return; }
      return run("save-terms", async () => {
        S.pact = await api("PUT", `/api/pacts/${S.pactId}/terms`, { title, amount_cents: amount, payees, criteria });
        S.editing = false; toast("Saved: new fingerprint, earlier approvals lapsed");
      });
    },
    approve: () => run("approve", async () => { S.pact = await api("POST", `/api/pacts/${S.pactId}/terms/approve`, { terms_sha256: S.pact.terms_sha256 }); toast(S.pact.locked_terms_sha256 ? "Both approved: terms locked" : "Approved"); }),
    order: () => run("order", async () => { S.pact = await api("POST", `/api/pacts/${S.pactId}/paypal/order`, {}, { idem: true }); S.tab = "fund"; }),
    "sim-approve": () => run("sim-approve", async () => { S.pact = await api("POST", `/api/pacts/${S.pactId}/paypal/simulate-buyer-approval`, {}); S.pact.paypal.order_status = "APPROVED"; toast("Buyer approval simulated"); }),
    authorize: () => run("authorize", async () => { S.pact = await api("POST", `/api/pacts/${S.pactId}/paypal/authorize`, {}, { idem: true }); toast("Hold verified against PayPal's record"); }),
    void: () => run("void", async () => { if (!confirm("Void the PayPal authorization? The buyer's hold is released.")) return; S.pact = await api("POST", `/api/pacts/${S.pactId}/void`, { reason: "cancelled by the client before delivery" }, { idem: true }); }),
    "submit-sample": (el) => run(el.dataset.action, async () => {
      const s = S.samples.find((x) => x.id === el.dataset.action.split(":")[1]);
      await api("POST", `/api/pacts/${S.pactId}/submissions`, { filename: s.filename, content: s.content });
      S.tab = "evaluate"; await refresh(); toast(`Submitted ${s.filename}`);
    }),
    "submit-own": () => {
      const filename = $("#sub-name").value, content = $("#sub-body").value;
      return run("submit-own", async () => {
        await api("POST", `/api/pacts/${S.pactId}/submissions`, { filename, content });
        S.tab = "evaluate"; await refresh();
      });
    },
    evaluate: () => run("evaluate", async () => { S.pact = await api("POST", `/api/pacts/${S.pactId}/evaluate`); S.tab = "evaluate"; S.citeKey = null; await refresh(); }),
    cite: (el) => { S.citeKey = el.dataset.id; render(); },
    release: () => {
      const amount = $("#amt").value.trim(), reason = $("#why").value;
      if (toCents(amount) === null) { toast("Enter the amount as dollars with at most 2 decimals", true); return; }
      return run("release", async () => releaseWith({ amount_usd: amount }, reason));
    },
    "release-full": () => { const reason = $("#why")?.value; return run("release-full", async () => releaseWith({ amount_cents: S.pact.paypal.authorized_cents }, reason)); },
    "release-again": () => run("release-again", async () => { await api("POST", `/api/pacts/${S.pactId}/release`, {}, { idem: true }); }),
    accept: () => run("accept", async () => { S.pact = await api("POST", `/api/pacts/${S.pactId}/release/accept`, {}, { idem: true }); S.tab = "payout"; await refresh(); }),
    dispute: () => { const reason = $("#why")?.value || ""; return run("dispute", async () => { S.pact = await api("POST", `/api/pacts/${S.pactId}/dispute`, { reason }); }); },
    resolve: () => {
      const amount = $("#amt").value.trim(), reason = $("#why").value;
      if (toCents(amount) === null) { toast("Enter the amount as dollars with at most 2 decimals", true); return; }
      return run("resolve", async () => { S.pact = await api("POST", `/api/pacts/${S.pactId}/resolve`, { amount_usd: amount, reason }, { idem: true }); S.tab = "payout"; await refresh(); });
    },
    reconcile: () => run("reconcile", async () => { S.pact = await api("POST", `/api/pacts/${S.pactId}/reconcile`); toast(`PayPal's record: ${STATUS_TEXT[S.pact.status] || S.pact.status}`); }),
    webhooks: () => run("webhooks", async () => { const r = await api("POST", "/api/demo/deliver-webhooks", {}); await refresh(); toast(`${r.delivered.length} signed webhook(s) verified and applied`); }),
    traces: async () => { S.drawer = true; S.traces = await api("GET", `/api/pacts/${S.pactId}/traces`); renderDrawer(); },
    "close-drawer": () => { S.drawer = false; renderDrawer(); },
    "download-evidence": () => {
      const blob = new Blob([JSON.stringify(S.evidence, null, 2)], { type: "application/json" });
      const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = `${S.pactId}-evidence.json`; a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    },
  };

  async function releaseWith(amount, reason) {
    S.pact = await api("POST", `/api/pacts/${S.pactId}/release`, { ...amount, reason: reason || null, evaluation_id: S.pact.evaluation?.evaluation_id }, { idem: true });
    S.tab = S.pact.status === "RELEASE_PROPOSED" ? "decide" : "payout";
    await refresh();
  }

  async function startDemo(body) {
    const d = await api("POST", "/api/demo/start", body);
    S.session = { personas: Object.fromEntries(Object.entries(d.personas).map(([r, x]) => [r, { token: x.token, user_id: x.user_id, name: x.name }])), active: "client", preset_id: d.preset_id, chat: body.chat_text || null };
    S.pactId = d.pact_id; saveSession();
    S.draftNotes = { [d.pact_id]: d.pact.draft_notes || [] };
    S.draftProblems = { [d.pact_id]: d.pact.draft_problems || [] };
    await openPact(d.pact_id, { tab: "terms" });
    await loadPactExtras();
  }

  async function loadPactExtras() {
    const pid = S.session?.preset_id;
    S.chat = S.session?.chat || null;
    S.samples = [];
    if (pid) {
      try {
        const [preset, samples] = await Promise.all([api("GET", `/api/presets/${pid}`), api("GET", `/api/demo/samples?preset_id=${encodeURIComponent(pid)}`)]);
        S.chat = preset.chat_text; S.samples = samples.samples || [];
      } catch { /* demo extras are optional */ }
    }
    render();
  }

  document.addEventListener("click", (ev) => {
    const el = ev.target.closest("[data-action]");
    if (!el) return;
    const name = el.dataset.action;
    if (name.startsWith("tab:")) { ev.preventDefault(); ACTIONS.tab({ dataset: { id: name.slice(4) } }); return; }
    const fn = ACTIONS[name] || ACTIONS[name.split(":")[0]];
    if (!fn) return;
    if (el.tagName === "A" && name === "home") ev.preventDefault();
    if (el.tagName === "BUTTON") ev.preventDefault();
    fn(el);
  });
  document.addEventListener("change", async (ev) => {
    if (ev.target.id === "sub-file" && ev.target.files?.[0]) {
      const f = ev.target.files[0];
      $("#sub-name").value = f.name; $("#sub-body").value = await f.text();
    }
  });
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && S.drawer) ACTIONS["close-drawer"](); });

  // ------------------------------------------------------------------------------------------------- boot -----
  (async () => {
    try {
      const [cfg, presets, health] = await Promise.all([api("GET", "/api/config"), api("GET", "/api/presets"), api("GET", "/api/health")]);
      S.config = cfg; S.presets = presets.presets || []; S.health = health;
      const q = new URLSearchParams(location.search);
      const pid = q.get("pact");
      if (pid && loadSession(pid)) {
        await openPact(pid, { tab: q.get("paypal") === "return" ? "fund" : null });
        await loadPactExtras();
        if (q.get("paypal") === "return") toast("Back from PayPal: authorize to verify the hold.");
        if (q.get("paypal") === "cancel") toast("The buyer cancelled in PayPal.", true);
      } else render();
    } catch (e) {
      $("#app").innerHTML = `<div class="err" style="margin-top:24px">${ICON.alert} PactPay could not start: ${esc(e.message)}</div>`;
    }
  })();
})();
