const CONFIG = Object.freeze({
  spreadsheetId: 'REPLACE_WITH_CONTROL_SHEET_ID',
  jobsSheet: 'Reset Jobs [DO NOT EDIT]',
  workerSheet: 'Worker Status',
  allowedDomain: 'deccan.ai',
  logoUrl: 'https://cdn.prod.website-files.com/66d017bee914d25aa0a45529/6937793e59b1671f75acf6b2_WhatsApp%20Image%202025-12-09%20at%2006.12.55.jpeg',
  workerFreshMs: 2 * 60 * 1000,
  cooldownMs: 5 * 60 * 1000,
  maxFormAgeMs: 2 * 60 * 60 * 1000,
  minFormAgeMs: 800,
  accounts: Object.freeze([
    { id: 'test-account-410', email: 'test-account-410@example.com', persona: 'Student' },
    { id: 'test-account-411', email: 'test-account-411@example.com', persona: 'Applied ML and Data Scientist' },
    { id: 'test-account-412', email: 'test-account-412@example.com', persona: 'Startup Founder' },
    { id: 'test-account-413', email: 'test-account-413@example.com', persona: 'Educator and Instructional Designer' },
    { id: 'test-account-414', email: 'test-account-414@example.com', persona: 'Backend Software Engineer' },
    { id: 'test-account-415', email: 'test-account-415@example.com', persona: 'Legal and Contracts Analyst' },
    { id: 'test-account-416', email: 'test-account-416@example.com', persona: 'Luxury Travel Advisor' },
    { id: 'test-account-417', email: 'test-account-417@example.com', persona: 'Indie Game Designer' },
    { id: 'test-account-418', email: 'test-account-418@example.com', persona: 'Life Sciences Researcher' },
  ]),
});

const JOB_HEADERS = Object.freeze([
  'Job ID', 'Requested At', 'Requester Email', 'Account ID', 'Account Email', 'Persona',
  'Mode', 'Status', 'Phase', 'Progress', 'Detail', 'Started At', 'Completed At',
  'Updated At', 'Result JSON', 'Error', 'Client Nonce', 'Worker ID',
]);
const WORKER_HEADERS = Object.freeze([
  'Worker ID', 'Heartbeat At', 'State', 'Current Job ID', 'Detail', 'Host', 'Version',
]);
const ACTIVE_STATUSES = Object.freeze(['QUEUED', 'RUNNING', 'RETRY_WAIT']);

function doGet() {
  const template = HtmlService.createTemplateFromFile('Index');
  template.appConfig = JSON.stringify({
    logoUrl: CONFIG.logoUrl,
    accounts: CONFIG.accounts,
  });
  return template.evaluate()
    .setTitle('GAB Environment Reset Console')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1');
}

function getBootstrap() {
  const email = requireDeccanUser_();
  return dashboardFor_(email);
}

function getDashboard() {
  const email = requireDeccanUser_();
  return dashboardFor_(email);
}

function createResetRequest(payload) {
  const requester = requireDeccanUser_();
  const clean = validateRequest_(payload || {});
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    const worker = getWorkerStatus_();
    if (!worker.online) {
      throw new Error('Reset worker is offline. Ask the reset-console owner to restore the central Mac worker.');
    }
    const sheet = getSheet_(CONFIG.jobsSheet, JOB_HEADERS);
    const jobs = readJobs_(sheet);
    const duplicate = jobs.find((job) => job.clientNonce === clean.clientNonce && job.requesterEmail === requester);
    if (duplicate) return { ok: true, job: duplicate, message: 'This request was already received.' };
    const active = jobs.find((job) => job.accountId === clean.account.id && ACTIVE_STATUSES.indexOf(job.status) !== -1);
    if (active) throw new Error(clean.account.id + ' already has an active reset job: ' + active.jobId);
    const recent = jobs
      .filter((job) => job.accountId === clean.account.id && job.mode === 'DELTA' && job.status === 'COMPLETED')
      .sort((a, b) => String(b.completedAt).localeCompare(String(a.completedAt)))[0];
    if (recent) {
      const completed = new Date(recent.completedAt).getTime();
      if (Number.isFinite(completed) && Date.now() - completed < CONFIG.cooldownMs) {
        throw new Error(clean.account.id + ' was reset recently. Wait five minutes before requesting another reset.');
      }
    }
    const now = new Date().toISOString();
    const jobId = 'RST-' + Utilities.getUuid().replace(/-/g, '').slice(0, 12).toUpperCase();
    sheet.appendRow([
      jobId, now, requester, clean.account.id, clean.account.email, clean.account.persona,
      'DELTA', 'QUEUED', 'QUEUED', 0, 'Waiting for the central reset worker.', '', '',
      now, '', '', clean.clientNonce, '',
    ]);
    SpreadsheetApp.flush();
    const job = findJobById_(sheet, jobId);
    return { ok: true, job: job, message: 'Restore queued. Keep this page open to monitor progress.' };
  } finally {
    lock.releaseLock();
  }
}

function getJobStatus(jobId) {
  requireDeccanUser_();
  const job = findJobById_(getSheet_(CONFIG.jobsSheet, JOB_HEADERS), normalizeText_(jobId, 80));
  if (!job) throw new Error('Reset job not found.');
  return { job: job, worker: getWorkerStatus_() };
}

function dashboardFor_(email) {
  const jobs = readJobs_(getSheet_(CONFIG.jobsSheet, JOB_HEADERS));
  const recentJobs = jobs.slice(-25).reverse();
  const accountState = {};
  CONFIG.accounts.forEach((account) => {
    const active = recentJobs.find((job) => job.accountId === account.id && ACTIVE_STATUSES.indexOf(job.status) !== -1);
    const latest = recentJobs.find((job) => job.accountId === account.id);
    accountState[account.id] = active || latest || null;
  });
  return {
    ready: true,
    email: email,
    accounts: CONFIG.accounts,
    worker: getWorkerStatus_(),
    accountState: accountState,
    recentJobs: recentJobs,
  };
}

function validateRequest_(payload) {
  if (normalizeText_(payload.website, 200)) throw new Error('Request rejected.');
  const startedAt = Number(payload.startedAt || 0);
  const age = Date.now() - startedAt;
  if (!Number.isFinite(startedAt) || age < CONFIG.minFormAgeMs || age > CONFIG.maxFormAgeMs) {
    throw new Error('This page session expired. Reload and try again.');
  }
  const accountId = normalizeText_(payload.accountId, 30).toLowerCase();
  const confirmation = normalizeText_(payload.confirmation, 200).toLowerCase();
  const clientNonce = normalizeText_(payload.clientNonce, 100);
  const account = CONFIG.accounts.find((item) => item.id === accountId);
  if (!account) throw new Error('Select a recognized benchmark account.');
  if (confirmation !== account.email.toLowerCase()) throw new Error('Confirmation must exactly match the selected account email.');
  if (payload.acknowledged !== true) throw new Error('Confirm that prior-run evidence is saved.');
  if (!/^[A-Za-z0-9_-]{20,100}$/.test(clientNonce)) throw new Error('Reload the page and try again.');
  return { account: account, clientNonce: clientNonce };
}

function requireDeccanUser_() {
  const email = String(Session.getActiveUser().getEmail() || '').trim().toLowerCase();
  if (!email || !email.endsWith('@' + CONFIG.allowedDomain)) {
    throw new Error('Sign in with your verified @' + CONFIG.allowedDomain + ' account.');
  }
  return email;
}

function getWorkerStatus_() {
  const sheet = getSheet_(CONFIG.workerSheet, WORKER_HEADERS);
  if (sheet.getLastRow() < 2) {
    return { online: false, state: 'OFFLINE', detail: 'No worker heartbeat has been recorded.' };
  }
  const row = sheet.getRange(2, 1, 1, WORKER_HEADERS.length).getDisplayValues()[0];
  const heartbeat = String(row[1] || '');
  const ageMs = Date.now() - new Date(heartbeat).getTime();
  const online = Number.isFinite(ageMs) && ageMs >= 0 && ageMs < CONFIG.workerFreshMs;
  return {
    workerId: row[0] || '', heartbeatAt: heartbeat, state: online ? (row[2] || 'ONLINE') : 'OFFLINE',
    currentJobId: row[3] || '', detail: online ? (row[4] || '') : 'Worker heartbeat is stale.',
    host: row[5] || '', version: row[6] || '', online: online,
  };
}

function readJobs_(sheet) {
  const lastRow = sheet.getLastRow();
  if (lastRow < 2) return [];
  const rows = sheet.getRange(2, 1, lastRow - 1, JOB_HEADERS.length).getDisplayValues();
  return rows.map(jobFromRow_);
}

function findJobById_(sheet, jobId) {
  const jobs = readJobs_(sheet);
  for (let i = jobs.length - 1; i >= 0; i -= 1) {
    if (jobs[i].jobId === jobId) return jobs[i];
  }
  return null;
}

function jobFromRow_(row) {
  return {
    jobId: row[0] || '', requestedAt: row[1] || '', requesterEmail: row[2] || '',
    accountId: row[3] || '', accountEmail: row[4] || '', persona: row[5] || '',
    mode: row[6] || '', status: row[7] || '', phase: row[8] || '',
    progress: Number(row[9] || 0), detail: row[10] || '', startedAt: row[11] || '',
    completedAt: row[12] || '', updatedAt: row[13] || '', error: row[15] || '',
    workerId: row[17] || '',
  };
}

function getSheet_(name, headers) {
  const sheet = SpreadsheetApp.openById(CONFIG.spreadsheetId).getSheetByName(name);
  if (!sheet) throw new Error('Reset-console ledger is missing sheet: ' + name);
  const actual = sheet.getRange(1, 1, 1, headers.length).getDisplayValues()[0];
  if (actual.join('\u001f') !== headers.join('\u001f')) throw new Error('Reset-console schema mismatch in ' + name + '.');
  return sheet;
}

function normalizeText_(value, maxLength) {
  return String(value == null ? '' : value)
    .replace(/[\u0000-\u001f\u007f]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, maxLength);
}

function staticAssertions_() {
  if (JOB_HEADERS.length !== 18) throw new Error('Job schema assertion failed.');
  if (CONFIG.accounts.length !== 9) throw new Error('Account mapping assertion failed.');
  if (CONFIG.accounts[1].id !== 'test-account-411') throw new Error('Account order assertion failed.');
}
