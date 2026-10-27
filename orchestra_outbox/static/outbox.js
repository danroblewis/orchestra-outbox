// The Outbox page: drafts waiting for the owner, approve (perhaps edited) or reject, and the record.
// Reads /api/plugins/outbox/state every 4 s; the waiting cards are drawn again only when the set of
// waiting drafts changes, so an edit in progress is never lost.
(function () {
  const API = "/api/plugins/outbox";
  let waitingKey = null, D = null;

  const when = (t) => (t ? new Date(t * 1000).toLocaleString() : "");
  const age = (t) => (t && D ? ago(Math.max(D.now - t, 0)) : "");
  const target = (t) => Object.entries(t || {}).map(([k, v]) => `${esc(k)} <b>${esc(v)}</b>`).join(" · ");
  const badge = (s) => `<span class="ob-badge ob-s-${esc(s)}">${esc(s)}</span>`;
  const kindOf = (name) => (D.kinds || []).find((k) => k.name === name) || {};

  async function post(path, body) {
    const r = await fetch(API + path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
    return j;
  }

  function card(d) {
    const k = kindOf(d.kind);
    const hidden = k.sender === "github.pr_comment" ? `<div class="ob-note">A hidden mark (an HTML comment naming this draft) is added at the end, so a lost answer never sends it twice.</div>` : "";
    const payload = k.sender === "jira.create" ? `<div class="ob-note">The issue's other fields come from the kind's payload in orchestra.toml, with this text as <code>$content</code>; fields: </div><pre class="ob-pre">${esc(JSON.stringify(d.fields, null, 2))}</pre>` : "";
    return `<div class="ob-card ${d.held ? "ob-held" : ""}" id="c-${esc(d.id)}">
      <div class="ob-meta"><span class="ob-kind">${esc(d.kind)}</span><span class="muted">via ${esc(k.sender || "?")}</span>
        <span>to ${target(d.target)}</span><span>by <b>${esc(d.drafter)}</b></span>
        ${d.causationid ? `<span>because <code>${esc(d.causationid)}</code></span>` : ""}
        <span class="muted">${esc(age(d.created_at))} ago</span>
        ${d.held ? `<span class="ob-badge ob-s-held">held: over the ${esc(d.kind)} limit, so it waits for you</span>` : ""}
        ${d.recipients && d.recipients.length ? `<span class="muted ob-hide-sm">reaches ${d.recipients.map(esc).join(", ")}</span>` : ""}</div>
      <textarea class="ob-text" id="t-${esc(d.id)}" aria-label="the text to send">${esc(d.content)}</textarea>
      ${hidden}${payload}
      <div class="ob-actions">
        <button class="ob-approve" id="a-${esc(d.id)}">Approve and send</button>
        <select id="r-${esc(d.id)}" aria-label="why">${(D.reasons || []).map((r) => `<option value="${esc(r)}">reject: ${esc(r.replace("_", " "))}</option>`).join("")}</select>
        <input id="n-${esc(d.id)}" placeholder="a note to ${esc(d.drafter)} (optional, never on the bus)">
        <button class="ob-reject" id="x-${esc(d.id)}">Reject</button>
      </div><div class="ob-err" id="e-${esc(d.id)}"></div></div>`;
  }

  function wire(d) {
    const t = $(`t-${d.id}`), a = $(`a-${d.id}`), err = $(`e-${d.id}`);
    t.addEventListener("input", () => {
      const ed = t.value !== d.content;
      t.classList.toggle("ob-edited", ed);
      a.textContent = ed ? "Approve the edit and send" : "Approve and send";
    });
    a.onclick = async () => {
      err.textContent = "";
      try { await post(`/drafts/${d.id}/approve`, t.value !== d.content ? { content: t.value } : {}); waitingKey = null; refresh(); }
      catch (e) { err.textContent = e.message; }
    };
    $(`x-${d.id}`).onclick = async () => {
      err.textContent = "";
      try { await post(`/drafts/${d.id}/reject`, { reason: $(`r-${d.id}`).value, note: $(`n-${d.id}`).value }); waitingKey = null; refresh(); }
      catch (e) { err.textContent = e.message; }
    };
  }

  function recent(d) {
    const where = d.remote ? `<span class="muted">landed ${esc(JSON.stringify(d.remote))}</span>` : "";
    const why = d.state === "rejected" ? `<span>${esc(d.reason)}${d.note ? `: ${esc(d.note)}` : ""}</span>` : "";
    const err = d.error ? `<span class="ob-bad">${esc(d.error)}</span>` : "";
    return `<details class="ob-row"><summary>${badge(d.state)}<span class="ob-kind">${esc(d.kind)}</span>
        <span>${target(d.target)}</span><span class="muted">by ${esc(d.drafter)}</span>
        ${d.edited ? `<span class="ob-badge">edited</span>` : ""}${d.approved_by === "policy" ? `<span class="ob-badge">auto</span>` : ""}
        ${why}${err}${where}<span class="muted">${esc(when(d.sent_at || d.decided_at))}</span></summary>
      <pre class="ob-pre">${esc(d.content)}</pre>
      ${d.edited ? `<div class="ob-note">as drafted:</div><pre class="ob-pre">${esc(d.original)}</pre>` : ""}
      ${d.output ? `<div class="ob-note">the script's output:</div><pre class="ob-pre">${esc(d.output)}</pre>` : ""}</details>`;
  }

  function draw() {
    const s = D.running ? "the outbox machine is running" : `<span class="ob-bad">the outbox machine is not running: approvals wait until <code>orchestra run</code> starts it</span>`;
    $("ob-status").innerHTML = D.configured ? `${s} · approvals publish as <code>${esc(D.owner)}</code>`
      : `<span class="ob-bad">[outbox] is not configured${D.config_error ? `: ${esc(D.config_error)}` : ""}</span>`;
    const key = D.waiting.map((d) => d.id).join(",");
    $("ob-nwait").textContent = D.waiting.length;
    if (key !== waitingKey) {
      waitingKey = key;
      $("ob-waiting").innerHTML = D.waiting.length ? D.waiting.map(card).join("") : `<div class="empty">Nothing is waiting.</div>`;
      D.waiting.forEach(wire);
    }
    $("ob-nrecent").textContent = D.recent.length;
    $("ob-recent").innerHTML = D.recent.length ? D.recent.map(recent).join("") : `<div class="empty">Nothing decided yet.</div>`;
    $("ob-kinds").innerHTML = D.kinds.length ? `<table class="ob-table"><tr><th>kind</th><th>sender</th><th>approval</th><th>limit per recipient</th><th class="ob-hide-sm">unedited approvals in a row</th></tr>${
      D.kinds.map((k) => `<tr><td class="ob-kind">${esc(k.name)}</td><td>${esc(k.sender)}</td><td>${esc(k.approval)}</td>
        <td>${k.limit == null ? "none" : `${k.limit} per ${esc(ago(k.per))}`}</td><td class="ob-hide-sm">${k.unedited_streak}</td></tr>`).join("")}</table>`
      : `<div class="empty">No kinds are configured ([outbox.kinds] in orchestra.toml). Ask the Supervisor to add one.</div>`;
    $("ob-audit").innerHTML = D.audit.length ? `<table class="ob-table"><tr><th>when</th><th>event</th><th>source</th><th class="ob-hide-sm">data</th></tr>${
      D.audit.map((a) => `<tr><td>${esc(when(a.at))}</td><td>${esc(a.type)}${a.published ? "" : ' <span class="muted">(not on the bus yet)</span>'}</td>
        <td>${esc(a.source)}</td><td class="ob-data ob-hide-sm">${esc(JSON.stringify(a.data))}</td></tr>`).join("")}</table>`
      : `<div class="empty">No events yet.</div>`;
  }

  async function refresh() {
    try {
      D = await (await fetch(API + "/state")).json();
      draw();
      $("conn").textContent = "live";
    } catch (e) { $("conn").textContent = "disconnected"; }
  }
  refresh(); setInterval(refresh, 4000);
})();
