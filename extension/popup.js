const $ = (id) => document.getElementById(id);

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

function renderProgress(state) {
  const box = $("progress-box");
  const signin = $("signin-box");
  const busy = Boolean(state?.inProgress);
  $("btn-sync").disabled = busy;
  $("btn-dry-run").disabled = busy;

  signin.classList.toggle("hidden", !state?.needsAmazonSignIn);

  if (!state || (!state.inProgress && !state.error)) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden", "banner-err", "banner-ok");
  if (state.error) box.classList.add("banner-err");
  box.textContent = state.message || "";
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
  $("stats-grid").innerHTML = [
    ["Updated", s.updated || 0],
    ["Split", s.split || 0],
    ["Skipped", s.skipped || 0],
    ["Unmatched", s.unmatched || 0],
  ]
    .map(([lbl, num]) => `<div class="stat"><span class="num">${num}</span><span class="lbl">${lbl}</span></div>`)
    .join("");

  const list = $("changes-list");
  const changes = res.changes || [];
  if (!changes.length) {
    list.innerHTML = `<li class="change-item muted">No new Amazon transactions needed updates.</li>`;
    return;
  }
  list.innerHTML = changes
    .map((ch) => {
      const catText = ch.splits
        ? "SPLIT: " + Object.entries(ch.splits).map(([k, v]) => `${k} $${v.toFixed(2)}`).join(", ")
        : ch.category || "Notes only";
      return `<li class="change-item">
        <div class="change-top">
          <span>${ch.date} • $${Number(ch.amount).toFixed(2)}</span>
          <span class="change-cat">${escapeHtml(catText)}</span>
        </div>
        <div class="change-note">${escapeHtml(ch.notes || "")}</div>
      </li>`;
    })
    .join("");
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
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
  await chrome.storage.local.set({ days });

  // Firefox MV3 treats host_permissions as opt-in until granted in a user gesture.
  try {
    const { serverUrl } = await chrome.storage.local.get(["serverUrl"]);
    const origins = ["https://www.amazon.com/*"];
    if (serverUrl) {
      origins.push(new URL(serverUrl).origin + "/*");
    }
    const granted = await chrome.permissions.request({ origins });
    if (granted === false) {
      renderProgress({
        inProgress: false,
        error: "Host permission for Amazon and your sync server is required.",
        message: "Host permission for Amazon and your sync server is required.",
      });
      return;
    }
  } catch {
    // Ignore if browser handles permissions via manifest or already granted
  }

  renderProgress({ inProgress: true, message: "Starting sync..." });
  chrome.runtime.sendMessage({ type: "RUN_SYNC", dryRun, days }, (resp) => {
    if (resp?.ok && resp.result) {
      renderResult(resp.result);
      refreshStatus();
    }
  });
}

async function init() {
  $("tab-sync").addEventListener("click", () => showTab("sync"));
  $("tab-settings").addEventListener("click", () => showTab("settings"));
  $("btn-sync").addEventListener("click", () => triggerSync(false));
  $("btn-dry-run").addEventListener("click", () => triggerSync(true));
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

  $("input-url").value = stored.serverUrl;
  $("input-token").value = stored.apiToken;
  $("input-auto").checked = Boolean(stored.autoSync);
  $("days-select").value = String(stored.days || 45);
  renderProgress(stored.syncState);
  renderResult(stored.lastResult);

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

    let origin;
    try {
      origin = new URL(rawUrl).origin + "/*";
    } catch {
      msg.className = "banner banner-err";
      msg.textContent = "Invalid server URL.";
      return;
    }

    try {
      await chrome.permissions.request({ origins: [origin] });
    } catch {
      // Optional permission request can fail if already granted or restricted; continue to test
    }

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
