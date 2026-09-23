const DEFAULT_PUSH_OPTS = {
  "do-cal": true,
  "do-mail": true,
  "do-drive": true,
  "do-gh": false,
  "do-zip": false,
  wipe: false,
  rebase: true,
  "rebase-cal": false,
  "push-anyway": false,
};

const state = {
  bootstrap: null,
  run: null,
  selected: null,
  passwords: {},
  reveal: {},
  jobId: null,
  pushingKey: null,
  log: [],
  skipped: null,
  csvMsg: "",
  oauthMsg: "",
  csvQuoted: false,
  pushOpts: { ...DEFAULT_PUSH_OPTS },
  pushThreads: 10,
  pushUsersPerThread: 20,
};

const $ = (id) => document.getElementById(id);

function chip(label, tone) {
  return `<span class="chip ${tone || ""}">${label}</span>`;
}

function authTone(s) {
  if (s === "authorized") return "ok";
  if (s === "mismatch" || s === "expired") return "err";
  if (s === "unknown") return "warn";
  return "";
}

function pushTone(s) {
  if (s === "ok") return "ok";
  if (s === "partial") return "warn";
  if (s === "failed" || s === "running") return s === "running" ? "warn" : "err";
  return "";
}

function disableReason() {
  const row = selectedRow();
  if (!state.run) return "upload a CSV first";
  if (!row) return "select an account";
  if (row.persona_status !== "matched") return "persona not matched";
  if (state.run.auth_interactive !== false && row.auth.state !== "authorized") {
    return "authorize this account first";
  }
  return null;
}

function rememberPushOpts() {
  const next = { ...(state.pushOpts || DEFAULT_PUSH_OPTS) };
  for (const id of Object.keys(DEFAULT_PUSH_OPTS)) {
    const el = $(id);
    if (el) next[id] = el.checked;
  }
  state.pushOpts = next;
  const threadsEl = $("push-threads");
  const usersEl = $("push-users");
  if (threadsEl) state.pushThreads = Math.max(1, Math.min(20, parseInt(threadsEl.value, 10) || 10));
  if (usersEl) state.pushUsersPerThread = Math.max(1, Math.min(50, parseInt(usersEl.value, 10) || 20));
}

function optChecked(id) {
  const opts = state.pushOpts || DEFAULT_PUSH_OPTS;
  return !!opts[id];
}

function optAttrs(id, extraDisabled) {
  const checked = optChecked(id) ? "checked" : "";
  const disabled = extraDisabled ? "disabled" : "";
  return `${checked} ${disabled}`.trim();
}

function moduleChecked(id, fallback) {
  const el = $(id);
  if (el) return el.checked;
  if (Object.prototype.hasOwnProperty.call(DEFAULT_PUSH_OPTS, id)) return optChecked(id);
  return fallback;
}

function missingSources(row) {
  if (row && row.persona_status === "matched") return [];
  const want = [];
  if (!(row.sources && row.sources.calendar && row.sources.calendar.path)) want.push("calendar");
  if (!(row.sources && row.sources.gmail && row.sources.gmail.path)) want.push("gmail");
  if (!(row.sources && row.sources.filesystem && row.sources.filesystem.path)) want.push("drive");
  return want;
}

function fixedPushBody() {
  return {
    calendar: true,
    gmail: true,
    drive: true,
    github: false,
    github_zip: true,
    wipe: false,
    rebase_dates: false,
    rebase_calendar: false,
    allow_missing_attachments: true,
    threads: 10,
    users_per_thread: 20,
    only_skipped: false,
  };
}

function rowKey(row) {
  if (!row) return "";
  return `${(row.email || "").toLowerCase()}::${row.persona_key || ""}`;
}

function personaQuery(row) {
  if (!row) return "";
  const p = row.persona_key || row.persona_dir || row.persona_raw || "";
  return p ? `?persona=${encodeURIComponent(p)}` : "";
}

function accountUrl(row, suffix) {
  return `/api/run/${state.run.run_id}/account/${encodeURIComponent(row.email)}${suffix}${personaQuery(row)}`;
}

function uniqueEmails(run) {
  const seen = [];
  const used = new Set();
  for (const a of (run && run.accounts) || []) {
    const email = (a.email || "").trim();
    if (!email || used.has(email)) continue;
    used.add(email);
    seen.push(email);
  }
  return seen;
}

function selectedRow() {
  if (!state.run) return null;
  return state.run.accounts.find((a) => rowKey(a) === state.selected) || null;
}

function accountIsPushing(row) {
  if (!row) return false;
  if (state.pushingKey && state.pushingKey === rowKey(row)) return true;
  return Boolean(row.push && row.push.state === "running");
}

function busyAccount() {
  if (state.pushingKey && state.run) {
    const row = state.run.accounts.find((a) => rowKey(a) === state.pushingKey);
    if (row) return row;
  }
  if (state.run) {
    return (state.run.accounts || []).find((a) => a.push && a.push.state === "running") || null;
  }
  return null;
}

function workInProgress() {
  if (state.localBusy) return true;
  if (state.run && state.run.batch_job_id) return true;
  return Boolean(busyAccount());
}

function blockIfBusy(what) {
  if (!workInProgress()) return false;
  const row = busyAccount();
  const who = row && row.email ? row.email : "this account";
  alert(`A push is still running for ${who}. Wait for it to finish before ${what}.`);
  return true;
}

function installBusyGuard() {
  if (state._guardOn) return;
  state._guardOn = true;
  const allow = (el) => el.closest("#log, #copy-emails, #copy-emails-reset, [data-reveal], [data-copy-pw], a[target='_blank']");
  document.addEventListener("click", (e) => {
    if (!workInProgress()) return;
    const t = e.target.closest("button, label.btn, tr[data-key], select.persona-fix, input[type=file]");
    if (!t || allow(t)) return;
    if (t.id === "go" && accountIsPushing(selectedRow())) {
      e.preventDefault();
      e.stopPropagation();
      tellPushBusy();
      return;
    }
    e.preventDefault();
    e.stopPropagation();
    blockIfBusy("doing something else");
  }, true);
  document.addEventListener("change", (e) => {
    if (!workInProgress()) return;
    if (!e.target.matches("input[type=file], select.persona-fix")) return;
    e.stopPropagation();
    e.target.value = "";
    blockIfBusy("changing files or the account");
  }, true);
  window.addEventListener("beforeunload", (e) => {
    if (!workInProgress()) return;
    e.preventDefault();
    e.returnValue = "";
  });
}

function tellPushBusy() {
  const note = $("push-busy");
  if (note) {
    note.hidden = false;
    note.textContent = "Push already running — wait for the log.";
  }
  const go = $("go");
  if (go) {
    go.textContent = "Pushing…";
    go.classList.add("busy");
  }
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = data.detail;
    const msg = typeof d === "string" ? d : Array.isArray(d) ? d.map((x) => x.msg || JSON.stringify(x)).join("; ") : res.statusText;
    throw new Error(msg);
  }
  return data;
}

function oauthClientReady() {
  const cred = (state.bootstrap && state.bootstrap.credentials) || {};
  const auth = (state.bootstrap && state.bootstrap.auth) || {};
  if (auth.interactive === false) return Boolean(cred.present);
  return cred.present && cred.kind === "web";
}

function oauthStepHtml() {
  const auth = (state.bootstrap && state.bootstrap.auth) || { name: "consumer_oauth", interactive: true };
  if (auth.interactive === false) {
    const cred = (state.bootstrap && state.bootstrap.credentials) || {};
    const email = cred.client_email ? escapeHtml(cred.client_email) : "";
    if (cred.present) {
      return `<p class="note">Service account impersonates @${escapeHtml(auth.domain || "deccanexperts.us")} — no sign-in.${email ? ` ${email}` : ""}</p>`;
    }
    return `
      <p class="reason">Upload the gab-seed service-account JSON key.</p>
      <input id="cred" class="sr-file" type="file" />
      <div class="drop oauth-drop" id="oauth-drop">
        <div class="row">
          <label class="btn" for="cred">Choose service-account JSON</label>
          <span class="note">Domain-wide delegation key</span>
        </div>
      </div>
      ${state.oauthMsg ? `<p class="reason">${escapeHtml(state.oauthMsg)}</p>` : ""}
    `;
  }
  const cred = (state.bootstrap && state.bootstrap.credentials) || {};
  const ready = cred.present && cred.kind === "web";
  const missing = cred.missing_redirects || [];
  let status;
  if (!cred.present) status = chip("no client", "err");
  else if (cred.kind === "installed") status = chip("desktop client — replace", "err");
  else if (missing.length) status = chip("web client, missing redirects", "warn");
  else status = chip("web client ready", "ok");
  return `
    ${status}
    <input id="cred" class="sr-file" type="file" />
    <div class="drop oauth-drop" id="oauth-drop">
      <div class="row">
        <label class="btn" for="cred">Choose OAuth JSON</label>
        <span class="note">Web client · drop or pick</span>
      </div>
    </div>
    ${missing.length ? `<p class="reason">Add these redirects, then re-download: ${missing.map(escapeHtml).join(" · ")}</p>` : ""}
    ${state.oauthMsg ? `<p class="${ready && !state.oauthMsg.startsWith("That") ? "note" : "reason"}">${escapeHtml(state.oauthMsg)}</p>` : ""}
  `;
}

async function uploadOauthBlob(blob, filename) {
  if (blockIfBusy("replacing the OAuth client")) return;
  const fd = new FormData();
  fd.append("file", blob, filename);
  const data = await api("/api/credentials", { method: "POST", body: fd });
  state.bootstrap = await api("/api/bootstrap");
  const missing = data.missing_redirects || [];
  if (data.kind === "workspace") {
    state.oauthMsg = "Service-account key saved. Accounts authorize automatically.";
  } else {
    state.oauthMsg = missing.length
      ? "Saved, but the JSON is missing redirect URIs. Add them in Google Cloud and re-download."
      : "Web OAuth client saved.";
  }
  paint();
}

function bindOauthStep() {
  const input = $("cred");
  if (!input) return;
  input.onchange = async (ev) => {
    const file = ev.target.files[0];
    if (!file) return;
    try {
      await uploadOauthBlob(file, file.name || "credentials.json");
    } catch (err) {
      state.oauthMsg = String(err.message || err);
      paint();
    }
  };
  const drop = $("oauth-drop");
  if (drop) {
    drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("hot"); };
    drop.ondragleave = () => drop.classList.remove("hot");
    drop.ondrop = async (e) => {
      e.preventDefault();
      drop.classList.remove("hot");
      const file = e.dataTransfer.files[0];
      if (!file) return;
      if (blockIfBusy("replacing the OAuth client")) return;
      try {
        await uploadOauthBlob(file, file.name || "credentials.json");
      } catch (err) {
        state.oauthMsg = String(err.message || err);
        paint();
      }
    };
  }
}

// The Audience "Add users" field commits one chip per Enter and never splits a pasted
// list, so the clipboard hands over a single address at a time.
function copyNextLabel(run) {
  const n = uniqueEmails(run).length;
  const i = state.copyIdx || 0;
  return i >= n ? `All ${n} copied — click to start over` : `Copy next email (${i + 1}/${n})`;
}

function renderAccounts() {
  const el = $("m-accounts");
  const run = state.run;
  const csvReady = Boolean(run);
  const oauthReady = oauthClientReady();
  const sys = (state.bootstrap && state.bootstrap.system) || {};
  const setupWarnings = [];
  if (sys.persona_root_present === false) {
    setupWarnings.push("Persona folder not found. Set GAB_PERSONA_ROOT before starting the server, or drop each JSON manually in 03.");
  }
  if (sys.git_available === false) {
    setupWarnings.push("Git is not installed. Google seeding works, but GitHub private repo push is unavailable.");
  }
  if (sys.runs_writable === false) {
    setupWarnings.push("The runs folder is not writable. Move the app to a writable folder before uploading.");
  }
  const summary = run
    ? `${run.summary.accounts} · ${run.summary.authorized} auth · ${run.summary.seeded} seeded${run.summary.unmatched ? ` · ${run.summary.unmatched} unmatched` : ""}`
    : "No CSV yet";
  const savedTableScroll = (el.querySelector(".table-wrap") || {}).scrollTop || 0;
  el.innerHTML = `
    <h2>01 · Accounts</h2>
    ${setupWarnings.map((m) => `<p class="reason">${escapeHtml(m)}</p>`).join("")}
    <div class="steps">
      <div class="step ${csvReady ? "done" : "next"}">
        <h3>CSV</h3>
        <div class="row">
          <label class="btn" for="csv">Choose CSV</label>
          <input id="csv" class="sr-file" type="file" accept=".csv,text/csv,text/plain" />
        </div>
        ${state.csvMsg ? `<p class="reason">${escapeHtml(state.csvMsg)}</p>` : ""}
        ${state.csvQuoted ? `<p class="reason">Quoted fields — check passwords that contain a comma.</p>` : ""}
        <div class="summary">${summary}</div>
      </div>
      <div class="step ${oauthReady ? "done" : (csvReady ? "next" : "")}">
        <h3>${((state.bootstrap && state.bootstrap.auth) || {}).interactive === false ? "Service account" : "OAuth"}</h3>
        ${oauthStepHtml()}
      </div>
    </div>
    ${(run && run.warnings && run.warnings.length) ? `<div class="note">${run.warnings.join(" · ")}</div>` : ""}
    ${run && ((state.bootstrap && state.bootstrap.auth) || {}).interactive !== false ? `<div class="row">
      <button type="button" id="copy-emails">${escapeHtml(copyNextLabel(run))}</button>
      <button type="button" class="ghost" id="copy-emails-reset">Restart</button>
      <span class="note" id="copy-emails-note">Audience: paste one, Enter, next.</span>
    </div>` : ""}
    ${run ? `<div class="table-wrap">${tableHtml(run)}</div>` : ""}
  `;
  const csvInput = $("csv");
  if (csvInput) csvInput.onchange = async (ev) => {
    const file = ev.target.files[0];
    ev.target.value = "";
    if (!file) return;
    if (blockIfBusy("uploading another CSV")) return;
    state.localBusy = true;
    try {
      const text = await file.text();
      state.passwords = parsePasswordsClient(text);
      const fd = new FormData();
      fd.append("file", file);
      state.run = await api("/api/run", { method: "POST", body: fd });
      const keep = state.selected && state.run.accounts.some((a) => rowKey(a) === state.selected);
      state.selected = keep ? state.selected : (state.run.accounts[0] && rowKey(state.run.accounts[0]));
      state.csvMsg = "";
      paint();
    } catch (err) {
      state.csvMsg = String(err.message || err);
      paint();
    } finally {
      state.localBusy = false;
    }
  };
  bindOauthStep();
  el.querySelectorAll("tr[data-key]").forEach((tr) => {
    tr.onclick = () => {
      const next = tr.getAttribute("data-key");
      if (next !== state.selected && blockIfBusy("switching accounts")) return;
      state.selected = next;
      paint();
    };
  });
  el.querySelectorAll("select.persona-fix").forEach((sel) => {
    sel.onclick = (e) => e.stopPropagation();
    sel.onchange = async (e) => {
      e.stopPropagation();
      if (blockIfBusy("changing the persona")) {
        sel.value = "";
        return;
      }
      const key = sel.getAttribute("data-key");
      const row = state.run.accounts.find((a) => rowKey(a) === key);
      if (!row) return;
      state.run = await api(accountUrl(row, "/persona"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ persona_dir: sel.value }),
      });
      paint();
    };
  });
  el.querySelectorAll("[data-reveal]").forEach((btn) => {
    btn.onclick = (e) => {
      e.stopPropagation();
      const email = btn.getAttribute("data-reveal");
      state.reveal[email] = !state.reveal[email];
      paint();
    };
  });
  el.querySelectorAll("[data-copy-pw]").forEach((btn) => {
    btn.onclick = (e) => {
      e.stopPropagation();
      const email = btn.getAttribute("data-copy-pw");
      navigator.clipboard.writeText(state.passwords[email] || "");
    };
  });
  const copyEmails = $("copy-emails");
  if (copyEmails) copyEmails.onclick = async (e) => {
    e.stopPropagation();
    const list = uniqueEmails(state.run);
    const i = (state.copyIdx || 0) % list.length;
    const addr = list[i];
    const note = $("copy-emails-note");
    try {
      await navigator.clipboard.writeText(addr);
      state.copyIdx = i + 1;
      if (note) note.textContent = `Copied ${addr} — paste it, press Enter, then click for the next.`;
      copyEmails.textContent = copyNextLabel(state.run);
    } catch (err) {
      if (note) note.textContent = "Clipboard blocked — copy the address below by hand.";
      const box = document.createElement("input");
      box.type = "text";
      box.className = "mono";
      box.value = addr;
      box.readOnly = true;
      copyEmails.parentElement.after(box);
      box.select();
    }
  };
  const copyReset = $("copy-emails-reset");
  if (copyReset) copyReset.onclick = (e) => {
    e.stopPropagation();
    state.copyIdx = 0;
    paint();
  };
  const wrap = el.querySelector(".table-wrap");
  if (wrap) wrap.scrollTop = savedTableScroll;
  const selected = el.querySelector("tr.selected");
  if (selected) selected.scrollIntoView({ block: "nearest", inline: "nearest" });
}

function parsePasswordsClient(text) {
  const lines = text.replace(/^\uFEFF/, "").split(/\r?\n/);
  if (!lines.length) return {};
  state.csvQuoted = /"/.test(text);
  const headers = csvLine(lines[0]).map((h) => h.trim().toLowerCase().replace(/\s+/g, " "));
  const ei = headers.findIndex((h) => ["email", "google account", "account", "gmail"].includes(h));
  const pi = headers.findIndex((h) => ["password", "pass", "pwd"].includes(h));
  const out = {};
  if (ei < 0 || pi < 0) return out;
  for (let i = 1; i < lines.length; i++) {
    if (!lines[i].trim()) continue;
    const cols = csvLine(lines[i]);
    const email = (cols[ei] || "").trim().toLowerCase();
    if (!email || out[email]) continue;
    out[email] = (cols[pi] || "").trim();
  }
  return out;
}

function csvLine(line) {
  const out = [];
  let cur = "";
  let q = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (ch === '"') {
      if (q && line[i + 1] === '"') {
        cur += '"';
        i += 1;
        continue;
      }
      q = !q;
      continue;
    }
    if (ch === "," && !q) {
      out.push(cur);
      cur = "";
      continue;
    }
    cur += ch;
  }
  out.push(cur);
  return out;
}

function tableHtml(run) {
  const showPw = !!run.has_passwords;
  const rows = run.accounts.map((a, i) => {
    const pw = state.passwords[a.email] || "";
    const shown = state.reveal[a.email] ? pw : "••••••••";
    const personaCell = a.persona_status === "unmatched"
      ? `<select class="persona-fix" data-key="${escapeHtml(rowKey(a))}"><option value="">Select folder</option>${run.folders.map((f) => `<option value="${escapeHtml(f)}">${escapeHtml(f)}</option>`).join("")}</select>`
      : `<span class="mono">${escapeHtml(a.persona_dir || a.persona_raw || "")}</span>`;
    const letters = ["calendar", "gmail", "filesystem"].map((k, idx) => {
      const letter = "CGD"[idx];
      return `<span class="${a.drops[k] ? "on" : ""}">${letter}</span>`;
    }).join(" ");
    const v = (a.state && a.state.verify && a.state.verify.modules) || {};
    const counts = ["calendar", "gmail", "drive"].map((k) => {
      const m = v[k];
      if (!m || m.expect == null) return "–";
      return `${m.got}/${m.expect}`;
    }).join(" · ");
    const pwCell = showPw ? `<td>
        <span class="pw mono">${shown}</span>
        <button data-reveal="${escapeHtml(a.email)}">${state.reveal[a.email] ? "Hide" : "Show"}</button>
        <button data-copy-pw="${escapeHtml(a.email)}">Copy</button>
      </td>` : "";
    return `<tr data-key="${escapeHtml(rowKey(a))}" class="${rowKey(a) === state.selected ? "selected" : ""}">
      <td>${i + 1}</td>
      <td class="mono">${escapeHtml(a.email)}</td>
      <td>${personaCell}</td>
      <td>${chip(a.auth.state, authTone(a.auth.state))}</td>
      <td class="letters">${letters}</td>
      <td>${chip(a.push.state, pushTone(a.push.state))}</td>
      <td class="mono">${counts}</td>
      ${pwCell}
    </tr>`;
  }).join("");
  return `<table>
    <thead><tr><th>#</th><th>Email</th><th>Persona</th><th>Auth</th><th>Drops</th><th>Push</th><th>Verify</th>${showPw ? "<th>for Gemini login</th>" : ""}</tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

function renderAuth() {
  const el = $("m-auth");
  const row = selectedRow();
  const auth = (state.bootstrap && state.bootstrap.auth) || { name: "consumer_oauth", interactive: true };
  if (!auth.interactive) {
    el.classList.add("hidden");
    el.innerHTML = "";
    return;
  }
  el.classList.remove("hidden");
  const locked = !state.run || !row;
  const needClient = !oauthClientReady();
  el.innerHTML = `
    <h2>02 · Authorize</h2>
    ${row && row.auth.state === "mismatch" ? `<p class="reason">Mismatch: expected ${row.email}, Google returned ${row.auth.got_email || row.auth.detail || "?"}</p>` : ""}
    <div class="row">
      <button class="primary" id="btn-auth" ${locked || needClient ? "disabled" : ""}>Authorize ${row ? escapeHtml(row.email) : ""}</button>
      ${needClient ? `<span class="reason">upload OAuth JSON first</span>` : ""}
      <span class="note" id="auth-url"></span>
    </div>
  `;
  const btn = $("btn-auth");
  if (btn) btn.onclick = async () => {
    if (blockIfBusy("authorizing another account")) return;
    try {
      const data = await api(accountUrl(row, "/authorize"), { method: "POST" });
      $("auth-url").innerHTML = `Copy link fallback: <a href="${data.url}" target="_blank" rel="noreferrer">open consent</a>`;
    } catch (err) {
      $("auth-url").textContent = String(err.message);
    }
  };
}

function applyAccount(account) {
  if (!state.run || !account) return;
  const i = state.run.accounts.findIndex((a) => rowKey(a) === rowKey(account));
  if (i >= 0) state.run.accounts[i] = account;
}

function sourceLabel(src) {
  if (!src) return "—";
  if (src.label) return src.label;
  if (src.source === "drop") return "dropped file";
  if (src.source === "persona") return "persona JSON";
  return "—";
}

function renderEnv() {
  const el = $("m-env");
  if (el) {
    el.classList.add("hidden");
    el.innerHTML = "";
  }
}

function renderGithub() {
  const el = $("m-github");
  const row = selectedRow();
  const show = !!(row && row.has_github);
  el.classList.toggle("hidden", !show);
  if (!show) {
    el.innerHTML = "";
    return;
  }
  const login = state.bootstrap && state.bootstrap.github_login;
  const workflow = state.bootstrap && state.bootstrap.github_workflow;
  const warn = state.bootstrap && state.bootstrap.github_warning;
  el.innerHTML = `
    <h2>04 · GitHub</h2>
    <p class="note">Repo is created under this PAT, not the demo Gmail. Needs <strong>repo + workflow</strong>.</p>
    ${workflow === false ? `<p class="reason">${escapeHtml(warn || "PAT is missing workflow scope.")}</p>` : ""}
    <div class="row">
      <input id="pat" type="password" placeholder="ghp_… repo + workflow" />
      <button class="primary" id="save-pat">Store PAT</button>
      <span class="note">${login ? escapeHtml(login) : "no PAT"}</span>
    </div>
    ${row && row.github && row.github.repo_url ? `<p class="mono">${escapeHtml(row.github.repo_url)} <button id="copy-repo">Copy</button></p>` : ""}
  `;
  $("save-pat").onclick = async () => {
    if (blockIfBusy("storing a GitHub PAT")) return;
    try {
      const data = await api("/api/github/pat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: $("pat").value }),
      });
      state.bootstrap.github_login = data.login;
      state.bootstrap.github_pat = true;
      state.bootstrap.github_scopes = data.scopes || [];
      state.bootstrap.github_workflow = data.workflow;
      state.bootstrap.github_warning = data.warning || null;
      $("pat").value = "";
      paint();
    } catch (err) {
      alert(err.message);
    }
  };
  const copy = $("copy-repo");
  if (copy) copy.onclick = () => navigator.clipboard.writeText(row.github.repo_url);
}

function renderPush() {
  const el = $("m-push");
  const row = selectedRow();
  const why = disableReason();
  const running = accountIsPushing(row);
  const busyNote = running
    ? "Push in progress for this account. Watch the log below."
    : (why || "");
  const jobId = state.jobId || (row && row.push && row.push.job_id);
  const runId = state.run && state.run.run_id;
  const anyRunning = Boolean(
    state.run &&
    (state.run.accounts || []).some((a) => a.push && a.push.state === "running")
  );
  const canBatch = Boolean(state.run && !running && !anyRunning && !state.run.batch_job_id);
  const skipInfo = state.skipped;
  const skipCount = skipInfo && skipInfo.files ? skipInfo.files : 0;
  const skipAccounts = skipInfo && skipInfo.accounts ? skipInfo.accounts : 0;
  const canSkip = Boolean(canBatch && skipCount > 0);
  const skipLabel = skipCount
    ? `Upload skipped files only (${skipCount} · ${skipAccounts} accounts)`
    : "Upload skipped files only";
  const durableLinks = runId
    ? `<div class="row">
        ${jobId ? `<a href="/api/run/${encodeURIComponent(runId)}/job/${encodeURIComponent(jobId)}/log" target="_blank">Job log</a>` : ""}
        ${row ? `<a href="/api/run/${encodeURIComponent(runId)}/account/${encodeURIComponent(row.email)}/log${row.persona_key ? `?persona=${encodeURIComponent(row.persona_key)}` : ""}" target="_blank">This account log</a>` : ""}
        ${state.run.failure_log ? `<a href="/api/run/${encodeURIComponent(runId)}/failures" target="_blank">Failures</a>` : ""}
      </div>`
    : "";
  el.innerHTML = `
    <h2>05 · Push</h2>
    <p class="lede">Upload a CSV, then Push all. Each matched account gets Calendar, Gmail (real attachments from filesystem/Drive; missing files are omitted, not empty placeholders), Drive, and the persona GitHub folder when it exists. No wipe.</p>
    <div class="row">
      <button class="primary${running ? " busy" : ""}" id="go" ${why && !running ? "disabled" : ""}>${running ? "Pushing…" : "Push into Google account"}</button>
      ${canBatch ? `<button id="go-all">Push all matched accounts</button>` : ""}
      ${canSkip ? `<button id="go-skipped">${escapeHtml(skipLabel)}</button>` : ""}
      ${canBatch ? `<button id="go-atts">Fix email attachments</button>` : ""}
      <span class="reason" id="push-busy">${escapeHtml(busyNote || (anyRunning && !running ? "Wait for the current uploads to finish." : ""))}</span>
    </div>
    <div class="log" id="log"></div>
    ${durableLinks}
  `;
  drawLog();
  const go = $("go");
  if (go && (!why || running)) go.onclick = startPush;
  const all = $("go-all");
  if (all && canBatch) all.onclick = () => startPushAll();
  const skipped = $("go-skipped");
  if (skipped && canSkip) skipped.onclick = () => startPushAll(undefined, true);
  const atts = $("go-atts");
  if (atts && canBatch) atts.onclick = () => startPushAll(undefined, false, true);
}

function logClass(msg) {
  if (/^WARN |WARN \|/.test(msg)) return "warn";
  if (/FAIL |ERROR|Skip |failed/i.test(msg)) return "err";
  if (/ok|Created|Inserted|Verify .*\/|Rewriting|Pushed GitHub/i.test(msg)) return "ok";
  return "";
}

function drawLog() {
  const box = $("log");
  if (!box) return;
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
  box.innerHTML = state.log.map((m) => `<div class="${logClass(m)}">${escapeHtml(m)}</div>`).join("");
  if (atBottom) box.scrollTop = box.scrollHeight;
}

function escapeHtml(s) {
  return s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

async function startPushAll(persona, onlySkipped, fixAtts) {
  const body = fixedPushBody();
  if (onlySkipped) body.only_skipped = true;
  if (fixAtts) {
    body.fix_gmail_attachments = true;
    body.threads = 10;
    body.users_per_thread = 10;
  }
  const q = persona ? `?persona=${encodeURIComponent(persona)}` : "";
  const data = await api(`/api/run/${state.run.run_id}/push-all${q}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  state.jobId = data.job_id;
  state.log = [];
  attachStream(data.job_id);
}

async function startPush() {
  const row = selectedRow();
  if (!row) return;
  if (accountIsPushing(row)) {
    tellPushBusy();
    return;
  }
  const body = fixedPushBody();
  state.pushingKey = rowKey(row);
  row.push = { ...(row.push || {}), state: "running" };
  paint();
  try {
    const data = await api(accountUrl(row, "/push"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const current = selectedRow();
    if (current && rowKey(current) === rowKey(row)) {
      current.push = { ...(current.push || {}), state: "running", job_id: data.job_id };
    }
    state.jobId = data.job_id;
    state.log = [];
    paint();
    attachStream(data.job_id);
  } catch (err) {
    const msg = String(err.message || err);
    if (/already running/i.test(msg)) {
      tellPushBusy();
      return;
    }
    state.pushingKey = null;
    if (row.push && row.push.state === "running") row.push.state = "failed";
    paint();
    alert(msg);
  }
}

let liveStream = null;

function attachStream(jobId) {
  if (!jobId) return;
  if (liveStream) {
    liveStream.close();
    liveStream = null;
  }
  state.jobId = jobId;
  const es = new EventSource(`/api/job/${jobId}/stream`);
  liveStream = es;
  es.onmessage = async (ev) => {
    let p;
    try {
      p = JSON.parse(ev.data);
    } catch {
      return;
    }
    if (p.kind === "log") {
      state.log.push(p.message);
      drawLog();
    }
    if (p.kind === "done") {
      es.close();
      if (liveStream === es) liveStream = null;
      state.pushingKey = null;
      await reloadRun();
    }
  };
  es.onerror = () => {
    if (es.readyState !== EventSource.CLOSED) return;
    if (liveStream === es) liveStream = null;
    if (state._reconnecting || state.jobId !== jobId) return;
    if (!accountIsPushing(selectedRow()) && !(state.run && state.run.batch_job_id === jobId)) return;
    state._reconnecting = true;
    setTimeout(() => {
      state._reconnecting = false;
      if (state.jobId === jobId) attachStream(jobId);
    }, 1500);
  };
}

function maybeResumeStream() {
  const row = selectedRow();
  const rowJob = row && row.push && row.push.state === "running" && row.push.job_id;
  const batchJob = state.run && state.run.batch_job_id;
  const jobId = rowJob || batchJob;
  if (!jobId || state.jobId === jobId) return;
  if (!state.log.length) state.log = ["(reconnected to in-flight push)"];
  if (row) state.pushingKey = rowKey(row);
  attachStream(jobId);
}

function renderVerify() {
  const el = $("m-verify");
  const row = selectedRow();
  const v = row && row.state && row.state.verify;
  const mods = (v && v.modules) || {};
  const titles = { calendar: "Calendar", gmail: "Gmail", drive: "Drive" };
  const cards = ["calendar", "gmail", "drive"].map((k) => {
    const m = mods[k];
    if (!m || m.expect == null) {
      return `<div class="mod"><div>${titles[k]}</div><div class="mono">–</div></div>`;
    }
    const inelig = k === "drive" && m.ineligible ? ` · ${m.ineligible} skipped` : "";
    return `<div class="mod ${m.tone}">
      <div>${titles[k]}</div>
      <div class="mono">${m.got}/${m.expect}${inelig}</div>
    </div>`;
  }).join("");
  const skips = row && row.state && row.state.skips;
  const skipHtml = skips && Object.keys(skips).length
    ? Object.entries(skips).map(([c, n]) => `<span class="note">${n} × ${escapeHtml(c)}</span>`).join(" · ")
    : "";
  el.innerHTML = `
    <h2>06 · Verify</h2>
    <div class="verify">${cards}</div>
    ${v && v.overall === "partial" ? `<p class="reason">Short — push again with Replace seed.</p>` : ""}
    ${skipHtml ? `<p class="note">${skipHtml}</p>` : ""}
  `;
}

async function loadSkipped() {
  if (!state.run) {
    state.skipped = null;
    return;
  }
  try {
    state.skipped = await api(`/api/run/${state.run.run_id}/skipped`);
  } catch (e) {
    state.skipped = null;
  }
}

async function reloadRun() {
  if (!state.run) return;
  state.run = await api(`/api/run/${state.run.run_id}`);
  await loadSkipped();
  const row = selectedRow();
  if (!accountIsPushing(row)) state.pushingKey = null;
  paint();
}

function paint() {
  rememberPushOpts();
  const cred = (state.bootstrap && state.bootstrap.credentials) || {};
  const auth = (state.bootstrap && state.bootstrap.auth) || {};
  $("cred-note").textContent = auth.name
    ? (
      auth.interactive === false
        ? `auth: ${auth.name} · ${auth.domain || "deccanexperts.us"}${cred.client_email ? ` · ${cred.client_email}` : ""}`
        : `auth: ${auth.name} · ${cred.kind || "no oauth json"}`
    )
    : "auth unknown";
  renderAccounts();
  renderAuth();
  renderEnv();
  renderGithub();
  renderPush();
  renderVerify();
  maybeResumeStream();
}

async function boot() {
  state.bootstrap = await api("/api/bootstrap");
  $("cred-note").textContent = (state.bootstrap.auth && state.bootstrap.auth.name)
    ? `auth: ${state.bootstrap.auth.name}`
    : (state.bootstrap.credentials.present ? `${state.bootstrap.credentials.kind} client` : "No OAuth Web client uploaded");
  const q = new URLSearchParams(location.search);
  const run = q.get("run");
  if (run) {
    try {
      state.run = await api(`/api/run/${run}`);
      const qAccount = q.get("account");
      const qPersona = q.get("persona") || "";
      const fromQuery = qAccount && state.run.accounts.find((a) => a.email === qAccount && (!qPersona || (a.persona_key || "") === qPersona));
      state.selected = fromQuery ? rowKey(fromQuery) : (state.run.accounts[0] && rowKey(state.run.accounts[0]));
      await loadSkipped();
    } catch (e) {}
  }
  if (q.get("auth") === "error") {
    state.oauthMsg = "Google sign-in did not finish. Click Authorize again — the previous code cannot be reused.";
  } else if (q.get("auth") === "expired") {
    state.oauthMsg = "That sign-in link expired. Click Authorize again.";
  }
  paint();
}

installBusyGuard();
boot();
