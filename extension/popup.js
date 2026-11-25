const $ = (id) => document.getElementById(id);

let cachedServerUrl = "";
let hasAllHostPerms = false;

function showTab(tab) {
  const isSync = tab === "sync";
  $("tab-sync").classList.toggle("active", isSync);
  $("tab-settings").classList.toggle("active", !isSync);
  $("view-sync").classList.toggle("hidden", !isSync);
  $("view-settings").classList.toggle("hidden", isSync);
}

function setPill(text, kind) {
  const el = $("status-pill");
  el.textContent = text;
  el.className = `pill pill-${kind}`;
}

function requiredOrigins(serverUrl) {
  const origins = ["https://www.amazon.com/*"];
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
  try {
    hasAllHostPerms = await chrome.permissions.contains({
      origins: requiredOrigins(serverUrl),
    });
  } catch {
    hasAllHostPerms = false;
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

  signin.classList.toggle("hidden", !state?.needsAmazonSignIn);
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
  $("results-title").textContent = res.dry_run ? "Last Dry Run (no writes)" : "Last Sync";
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
    empty.textContent = "No new Amazon transactions needed updates.";
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

      const note = document.createElement("div");
      note.className = "change-note";
      note.textContent = ch.notes || "";

      li.append(top, note);
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

  // Request host permissions synchronously inside the click gesture (before any
  // other await) when not yet granted, as required by Firefox MV3.
  if (!hasAllHostPerms) {
    try {
      const granted = await chrome.permissions.request({
        origins: requiredOrigins(cachedServerUrl),
      });
      hasAllHostPerms = Boolean(granted);
      if (granted === false) {
        renderProgress({
          inProgress: false,
          error: "Host permission for Amazon and your sync server is required.",
          message: "Host permission for Amazon and your sync server is required.",
        });
        return;
      }
    } catch {
      // Continue; browser may already allow via manifest or refuse redundant prompt
    }
  }

  await chrome.storage.local.set({ days });
  renderProgress({ inProgress: true, message: "Starting sync..." });
  chrome.runtime.sendMessage({ type: "RUN_SYNC", dryRun, days }, (resp) => {
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
      renderProgress({
        inProgress: false,
        error: resp.error,
        message: resp.error,
        needsAmazonSignIn: resp.code === "AMAZON_SIGNIN_REQUIRED",
      });
    }
  });
}

async function init() {
  $("tab-sync").addEventListener("click", () => showTab("sync"));
  $("tab-settings").addEventListener("click", () => showTab("settings"));
  $("btn-sync").addEventListener("click", () => triggerSync(false));
  $("btn-dry-run").addEventListener("click", () => triggerSync(true));
  $("btn-cancel").addEventListener("click", () => {
    chrome.runtime.sendMessage({ type: "CANCEL_SYNC" }, () => {
      renderProgress({ inProgress: false, error: null, message: "" });
    });
  });
  $("btn-open-amazon").addEventListener("click", () => {
    chrome.tabs.create({ url: "https://www.amazon.com/cpe/yourpayments/transactions" });
  });

  const stored = await chrome.storage.local.get({
    serverUrl: "",
    apiToken: "",
    days: 45,
    autoSync: false,
    syncState: null,
    lastResult: null,
  });

  cachedServerUrl = stored.serverUrl;
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
      hasAllHostPerms = Boolean(granted);
    } catch {
      // Continue to test connection
    }

    cachedServerUrl = rawUrl;
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
