const $ = (id) => document.getElementById(id);

const SOURCES = {
  amazon: {
    label: "Amazon",
    origin: "https://www.amazon.com/*",
    signInUrl: "https://www.amazon.com/cpe/yourpayments/transactions",
    signInCode: "AMAZON_SIGNIN_REQUIRED",
  },
  paypal: {
    label: "PayPal",
    origin: "https://www.paypal.com/*",
    signInUrl: "https://www.paypal.com/myaccount/transactions/",
    signInCode: "PAYPAL_SIGNIN_REQUIRED",
  },
};

let cachedServerUrl = "";
let currentSource = "amazon";
let signInTargetSource = "amazon";
const grantedBySource = { amazon: false, paypal: false };

function sourceInfo(source) {
  return SOURCES[source] || SOURCES.amazon;
}

function showTab(tab) {
  const isSync = tab === "sync";
  $("tab-sync").classList.toggle("active", isSync);
  $("tab-settings").classList.toggle("active", !isSync);
  $("view-sync").classList.toggle("hidden", !isSync);
  $("view-settings").classList.toggle("hidden", isSync);
}

function setSource(source, persist = false) {
  currentSource = source === "paypal" ? "paypal" : "amazon";
  $("src-amazon").classList.toggle("active", currentSource === "amazon");
  $("src-paypal").classList.toggle("active", currentSource === "paypal");
  $("btn-sync").textContent = `Sync ${sourceInfo(currentSource).label}`;
  if (persist) {
    chrome.storage.local.set({ source: currentSource });
  }
}

function setPill(text, kind) {
  const el = $("status-pill");
  el.textContent = text;
  el.className = `pill pill-${kind}`;
}

function requiredOrigins(serverUrl, source = null) {
  const origins = source
    ? [sourceInfo(source).origin]
    : [SOURCES.amazon.origin, SOURCES.paypal.origin];
  if (serverUrl) {
    try {
      origins.push(new URL(serverUrl).origin + "/*");
    } catch {
      // Ignore malformed URL until saved
    }
  }
  return origins;
}

async function checkHostPerms(serverUrl) {
  for (const key of Object.keys(SOURCES)) {
    try {
      grantedBySource[key] = await chrome.permissions.contains({
        origins: requiredOrigins(serverUrl, key),
      });
    } catch {
      grantedBySource[key] = false;
    }
  }
}

function renderProgress(state) {
  const box = $("progress-box");
  const textEl = $("progress-text");
  const cancelBtn = $("btn-cancel");
  const signin = $("signin-box");
  const busy = Boolean(state?.inProgress);

  $("btn-sync").disabled = busy;
  $("btn-dry-run").disabled = busy;
  $("src-amazon").disabled = busy;
  $("src-paypal").disabled = busy;

  const signInSrc = state?.signInSource || (state?.needsAmazonSignIn ? "amazon" : null);
  if (signInSrc) {
    signInTargetSource = signInSrc;
    const info = sourceInfo(signInSrc);
    $("signin-text").textContent = `${info.label} session not signed in.`;
    $("btn-open-signin").textContent = `Sign in to ${info.label}`;
    signin.classList.remove("hidden");
  } else {
    signin.classList.add("hidden");
  }

  cancelBtn.classList.toggle("hidden", !busy);

  if (!state || (!state.inProgress && !state.error)) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden", "banner-err", "banner-ok");
  if (state.error) box.classList.add("banner-err");
  textEl.textContent = state.message || "";
}

function renderResult(res) {
  const sec = $("results-section");
  if (!res) {
    sec.classList.add("hidden");
    return;
  }
  sec.classList.remove("hidden");
  const srcLabel = sourceInfo(res.source).label;
  $("results-title").textContent = res.dry_run
    ? `Last ${srcLabel} Dry Run (no writes)`
    : `Last ${srcLabel} Sync`;
  const dt = res.completedAt ? new Date(res.completedAt).toLocaleString() : "";
  $("results-meta").textContent = dt;

  const s = res.stats || {};
  const statsGrid = $("stats-grid");
  statsGrid.replaceChildren(
    ...[
      ["Updated", s.updated || 0],
      ["Split", s.split || 0],
      ["Skipped", s.skipped || 0],
      ["Unmatched", s.unmatched || 0],
    ].map(([lbl, num]) => {
      const card = document.createElement("div");
      card.className = "stat";
      const numEl = document.createElement("span");
      numEl.className = "num";
      numEl.textContent = String(num);
      const lblEl = document.createElement("span");
      lblEl.className = "lbl";
      lblEl.textContent = lbl;
      card.append(numEl, lblEl);
      return card;
    })
  );

  const list = $("changes-list");
  const changes = res.changes || [];
  if (!changes.length) {
    const empty = document.createElement("li");
    empty.className = "change-item muted";
    empty.textContent = `No new ${srcLabel} transactions needed updates.`;
    list.replaceChildren(empty);
    return;
  }
  list.replaceChildren(
    ...changes.map((ch) => {
      const catText = ch.splits
        ? "SPLIT: " + Object.entries(ch.splits).map(([k, v]) => `${k} $${v.toFixed(2)}`).join(", ")
        : ch.category || "Notes only";
      const li = document.createElement("li");
      li.className = "change-item";

      const top = document.createElement("div");
      top.className = "change-top";
      const metaSpan = document.createElement("span");
      metaSpan.textContent = `${ch.date} • $${Number(ch.amount).toFixed(2)}`;
      const catSpan = document.createElement("span");
      catSpan.className = "change-cat";
      catSpan.textContent = catText;
      top.append(metaSpan, catSpan);

      li.append(top);

      if (ch.renamed_to) {
        const rename = document.createElement("div");
        rename.className = "change-rename";
        rename.textContent = `Renamed → ${ch.renamed_to}`;
        li.append(rename);
      }

      const note = document.createElement("div");
      note.className = "change-note";
      note.textContent = ch.notes || "";

      li.append(note);
      return li;
    })
  );
}

async function refreshStatus() {
  const { serverUrl, apiToken } = await chrome.storage.local.get(["serverUrl", "apiToken"]);
  if (!serverUrl || !apiToken) {
    setPill("Configure server in Settings", "muted");
    return;
  }
  chrome.runtime.sendMessage({ type: "TEST_STATUS" }, (resp) => {
    if (chrome.runtime.lastError || !resp?.ok) {
      setPill("Server unreachable", "err");
      return;
    }
    const st = resp.status;
    if (!st.sure_connected) {
      setPill("Sure API unreachable", "err");
      return;
    }
    setPill(`Connected • ${st.categorizer} • ${st.synced_transactions} synced`, "ok");
  });
}

async function triggerSync(dryRun) {
  const days = Number($("days-select").value || 45);
  const src = currentSource;
  const info = sourceInfo(src);

  // Request host permissions synchronously inside the click gesture (before any
  // other await) when not yet granted, as required by Firefox MV3.
  if (!grantedBySource[src]) {
    try {
      const granted = await chrome.permissions.request({
        origins: requiredOrigins(cachedServerUrl, src),
      });
      grantedBySource[src] = Boolean(granted);
      if (granted === false) {
        const msg = `Host permission for ${info.label} and your sync server is required.`;
        renderProgress({ inProgress: false, error: msg, message: msg });
        return;
      }
    } catch {
      // Continue; browser may already allow via manifest or refuse redundant prompt
    }
  }

  await chrome.storage.local.set({ days, source: src });
  renderProgress({ inProgress: true, message: `Starting ${info.label} sync...` });
  chrome.runtime.sendMessage({ type: "RUN_SYNC", source: src, dryRun, days }, (resp) => {
    if (chrome.runtime.lastError) {
      renderProgress({
        inProgress: false,
        error: chrome.runtime.lastError.message,
        message: chrome.runtime.lastError.message,
      });
      return;
    }
    if (resp?.ok && resp.result) {
      renderResult(resp.result);
      refreshStatus();
    } else if (resp && !resp.ok && resp.error) {
      const signInSource =
        resp.code === "AMAZON_SIGNIN_REQUIRED"
          ? "amazon"
          : resp.code === "PAYPAL_SIGNIN_REQUIRED"
            ? "paypal"
            : null;
      renderProgress({
        inProgress: false,
        error: resp.error,
        message: resp.error,
        signInSource,
      });
    }
  });
}

async function init() {
  $("tab-sync").addEventListener("click", () => showTab("sync"));
  $("tab-settings").addEventListener("click", () => showTab("settings"));
  $("src-amazon").addEventListener("click", () => setSource("amazon", true));
  $("src-paypal").addEventListener("click", () => setSource("paypal", true));
  $("btn-sync").addEventListener("click", () => triggerSync(false));
  $("btn-dry-run").addEventListener("click", () => triggerSync(true));
  $("btn-cancel").addEventListener("click", () => {
    chrome.runtime.sendMessage({ type: "CANCEL_SYNC" }, () => {
      renderProgress({ inProgress: false, error: null, message: "" });
    });
  });
  $("btn-open-signin").addEventListener("click", () => {
    chrome.tabs.create({ url: sourceInfo(signInTargetSource).signInUrl });
  });

  const stored = await chrome.storage.local.get({
    serverUrl: "",
    apiToken: "",
    days: 45,
    source: "amazon",
    autoSync: false,
    syncState: null,
    lastResult: null,
  });

  cachedServerUrl = stored.serverUrl;
  setSource(stored.source, false);
  await checkHostPerms(cachedServerUrl);

  $("input-url").value = stored.serverUrl;
  $("input-token").value = stored.apiToken;
  $("input-auto").checked = Boolean(stored.autoSync);
  $("days-select").value = String(stored.days || 45);
  renderProgress(stored.syncState);
  renderResult(stored.lastResult);

  // Reconcile storage against live background worker so an interrupted run never stays stuck
  chrome.runtime.sendMessage({ type: "GET_SYNC_STATE" }, (resp) => {
    if (!chrome.runtime.lastError && resp?.ok) {
      renderProgress(resp.syncState);
    }
  });

  if (!stored.serverUrl || !stored.apiToken) {
    showTab("settings");
  }
  refreshStatus();

  chrome.storage.onChanged.addListener((changes) => {
    if (changes.syncState) renderProgress(changes.syncState.newValue);
    if (changes.lastResult) renderResult(changes.lastResult.newValue);
  });

  $("settings-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const msg = $("settings-msg");
    msg.classList.add("hidden");

    const rawUrl = $("input-url").value.trim().replace(/\/+$/, "");
    const apiToken = $("input-token").value.trim();
    const autoSync = $("input-auto").checked;

    let origins;
    try {
      origins = requiredOrigins(rawUrl);
    } catch {
      msg.className = "banner banner-err";
      msg.textContent = "Invalid server URL.";
      return;
    }

    try {
      const granted = await chrome.permissions.request({ origins });
      if (granted) {
        grantedBySource.amazon = true;
        grantedBySource.paypal = true;
      }
    } catch {
      // Continue to test connection
    }

    cachedServerUrl = rawUrl;
    await checkHostPerms(cachedServerUrl);
    await chrome.storage.local.set({ serverUrl: rawUrl, apiToken, autoSync });
    chrome.runtime.sendMessage({ type: "UPDATE_ALARM", enabled: autoSync });

    chrome.runtime.sendMessage({ type: "TEST_STATUS" }, (resp) => {
      if (chrome.runtime.lastError || !resp?.ok) {
        msg.className = "banner banner-err";
        msg.textContent = resp?.error || chrome.runtime.lastError?.message || "Connection failed";
        setPill("Server unreachable", "err");
        return;
      }
      const st = resp.status;
      msg.className = "banner banner-ok";
      msg.textContent = `Connected! Categorizer: ${st.categorizer}, Categories: ${st.spending_categories}`;
      refreshStatus();
    });
  });
}

init();
