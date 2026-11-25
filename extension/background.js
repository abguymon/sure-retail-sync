const AMAZON_TX_URL = "https://www.amazon.com/cpe/yourpayments/transactions";
const DAILY_ALARM = "sure-retail-sync-daily";
const MAX_PAGES = 10;
const FETCH_TIMEOUT_MS = 30000;

let activeController = null;

const sleep = (ms, signal) =>
  new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(new DOMException("Cancelled", "AbortError"));
    const id = setTimeout(resolve, ms);
    signal?.addEventListener(
      "abort",
      () => {
        clearTimeout(id);
        reject(new DOMException("Cancelled", "AbortError"));
      },
      { once: true }
    );
  });

async function fetchWithTimeout(url, options = {}, parentSignal = null) {
  const controller = new AbortController();
  const onParentAbort = () => controller.abort(parentSignal.reason || new DOMException("Cancelled", "AbortError"));
  if (parentSignal) {
    if (parentSignal.aborted) onParentAbort();
    else parentSignal.addEventListener("abort", onParentAbort, { once: true });
  }
  const timer = setTimeout(() => controller.abort(new Error(`Request timed out after ${FETCH_TIMEOUT_MS / 1000}s`)), FETCH_TIMEOUT_MS);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } catch (err) {
    if (controller.signal.aborted) {
      const reason = controller.signal.reason;
      if (reason instanceof Error) throw reason;
      const e = new Error("Sync cancelled.");
      e.name = "AbortError";
      throw e;
    }
    throw err;
  } finally {
    clearTimeout(timer);
    if (parentSignal) parentSignal.removeEventListener("abort", onParentAbort);
  }
}

async function getSettings() {
  const data = await chrome.storage.local.get({
    serverUrl: "",
    apiToken: "",
    days: 45,
    autoSync: false,
  });
  data.serverUrl = (data.serverUrl || "").trim().replace(/\/+$/, "");
  data.apiToken = (data.apiToken || "").trim();
  return data;
}

async function setProgress(inProgress, message, extra = {}) {
  await chrome.storage.local.set({
    syncState: {
      inProgress,
      message,
      updatedAt: new Date().toISOString(),
      ...extra,
    },
  });
}

async function getReconciledSyncState() {
  const { syncState } = await chrome.storage.local.get({ syncState: null });
  if (syncState?.inProgress && !activeController) {
    const cleared = {
      inProgress: false,
      message: "Previous sync was interrupted. Ready to retry.",
      error: null,
      needsAmazonSignIn: false,
      updatedAt: new Date().toISOString(),
    };
    await chrome.storage.local.set({ syncState: cleared });
    await chrome.action.setBadgeText({ text: "" });
    return cleared;
  }
  return syncState;
}

function looksLikeSignIn(url, html) {
  if ((url || "").includes("/ap/signin")) return true;
  if (!html) return false;
  return (
    html.includes('name="signIn"') &&
    html.includes("ap_email") &&
    !html.includes("transaction")
  );
}

async function fetchAmazonText(url, options = {}, signal = null) {
  const resp = await fetchWithTimeout(
    url,
    {
      credentials: "include",
      redirect: "follow",
      ...options,
    },
    signal
  );
  const text = await resp.text();
  if (looksLikeSignIn(resp.url, text)) {
    const err = new Error("Not signed in to Amazon. Sign in and try again.");
    err.code = "AMAZON_SIGNIN_REQUIRED";
    throw err;
  }
  if (!resp.ok) {
    throw new Error(`Amazon returned HTTP ${resp.status} for ${url}`);
  }
  return text;
}

async function callServer(settings, path, { method = "GET", body = null, signal = null } = {}) {
  if (!settings.serverUrl || !settings.apiToken) {
    throw new Error("Configure your server URL and Sync API token in Settings first.");
  }
  const headers = {
    Authorization: `Bearer ${settings.apiToken}`,
  };
  const opts = { method, headers };
  if (body !== null) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const resp = await fetchWithTimeout(`${settings.serverUrl}${path}`, opts, signal);
  const text = await resp.text();
  let json;
  try {
    json = JSON.parse(text);
  } catch {
    throw new Error(`Server returned HTTP ${resp.status}: ${text.slice(0, 160)}`);
  }
  if (!resp.ok) {
    throw new Error(json.error || `Server error HTTP ${resp.status}`);
  }
  return json;
}

async function runAmazonSync({ dryRun = false, days = null } = {}) {
  if (activeController) {
    throw new Error("A sync is already running. Click Cancel to stop it first.");
  }

  const controller = new AbortController();
  activeController = controller;
  const { signal } = controller;

  const settings = await getSettings();
  const lookback = Number(days || settings.days || 45);

  await chrome.action.setBadgeText({ text: "..." });
  await chrome.action.setBadgeBackgroundColor({ color: "#3b82f6" });
  await setProgress(true, "Fetching Amazon Transactions page 1...");

  try {
    const firstHtml = await fetchAmazonText(AMAZON_TX_URL, {}, signal);
    let plan = await callServer(settings, "/api/amazon/plan", {
      method: "POST",
      body: { transactions_html: [firstHtml], days: lookback },
      signal,
    });

    const allCharges = [...(plan.charges || [])];
    const neededMap = new Map(
      (plan.needed_orders || []).map((o) => [o.order_number, o.url])
    );

    let nextPage = plan.next_page;
    let pageNum = 1;
    while (nextPage && pageNum < MAX_PAGES) {
      pageNum += 1;
      await setProgress(true, `Fetching Amazon Transactions page ${pageNum}...`);
      await sleep(800, signal);

      let pagePlan;
      if (nextPage.kind === "json") {
        const resp = await fetchWithTimeout(
          nextPage.url,
          {
            method: "POST",
            credentials: "include",
            headers: nextPage.headers,
            body: JSON.stringify(nextPage.body),
          },
          signal
        );
        if (!resp.ok) break;
        const payload = await resp.json();
        pagePlan = await callServer(settings, "/api/amazon/plan", {
          method: "POST",
          body: {
            transactions_json_pages: [{ payload, meta: nextPage.meta }],
            days: lookback,
          },
          signal,
        });
      } else {
        const formBody = new URLSearchParams(nextPage.form_data).toString();
        const html = await fetchAmazonText(
          nextPage.url,
          {
            method: "POST",
            headers: { "Content-Type": "application/x-www-form-urlencoded" },
            body: formBody,
          },
          signal
        );
        pagePlan = await callServer(settings, "/api/amazon/plan", {
          method: "POST",
          body: { transactions_html: [html], days: lookback },
          signal,
        });
      }

      for (const c of pagePlan.charges || []) allCharges.push(c);
      for (const o of pagePlan.needed_orders || []) neededMap.set(o.order_number, o.url);
      nextPage = pagePlan.next_page;
    }

    const neededList = Array.from(neededMap.entries());
    const ordersHtml = {};
    for (let i = 0; i < neededList.length; i += 1) {
      const [orderNum, url] = neededList[i];
      await setProgress(
        true,
        `Fetching order details (${i + 1}/${neededList.length}): ${orderNum}`
      );
      try {
        ordersHtml[orderNum] = await fetchAmazonText(url, {}, signal);
      } catch (err) {
        if (err.code === "AMAZON_SIGNIN_REQUIRED" || err.name === "AbortError") throw err;
        console.warn("Order fetch failed", orderNum, err);
      }
      if (i + 1 < neededList.length) await sleep(1200, signal);
    }

    await setProgress(
      true,
      dryRun
        ? "Running dry-run categorization on server..."
        : "Categorizing & updating transactions in Sure..."
    );

    const result = await callServer(settings, "/api/amazon/sync", {
      method: "POST",
      body: {
        charges: allCharges,
        orders_html: ordersHtml,
        days: lookback,
        dry_run: dryRun,
      },
      signal,
    });

    const summary = {
      ...result,
      days: lookback,
      pagesFetched: pageNum,
      ordersFetched: Object.keys(ordersHtml).length,
      completedAt: new Date().toISOString(),
    };

    const touched = (result.stats?.updated || 0) + (result.stats?.split || 0);
    await chrome.storage.local.set({ lastResult: summary });
    await setProgress(false, dryRun ? "Dry run complete." : "Sync complete.", {
      error: null,
      needsAmazonSignIn: false,
    });
    await chrome.action.setBadgeText({ text: touched > 0 ? String(touched) : "" });
    await chrome.action.setBadgeBackgroundColor({ color: "#16a34a" });
    return summary;
  } catch (err) {
    const cancelled = err.name === "AbortError" && signal.aborted;
    const needsSignIn = err.code === "AMAZON_SIGNIN_REQUIRED";
    await setProgress(false, cancelled ? "Sync cancelled." : err.message || String(err), {
      error: cancelled ? null : err.message || String(err),
      needsAmazonSignIn: needsSignIn,
    });
    await chrome.action.setBadgeText({ text: cancelled ? "" : needsSignIn ? "AUTH" : "ERR" });
    if (!cancelled) await chrome.action.setBadgeBackgroundColor({ color: "#dc2626" });
    throw err;
  } finally {
    if (activeController === controller) {
      activeController = null;
    }
  }
}

async function cancelActiveSync() {
  if (activeController) {
    activeController.abort(new DOMException("Cancelled", "AbortError"));
    activeController = null;
  }
  await setProgress(false, "Sync cancelled.", { error: null, needsAmazonSignIn: false });
  await chrome.action.setBadgeText({ text: "" });
}

async function configureAlarm(enabled) {
  await chrome.alarms.clear(DAILY_ALARM);
  if (enabled) {
    chrome.alarms.create(DAILY_ALARM, {
      delayInMinutes: 60,
      periodInMinutes: 1440,
    });
  }
}

chrome.runtime.onInstalled.addListener(async () => {
  const s = await getSettings();
  await configureAlarm(s.autoSync);
  await getReconciledSyncState();
});

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name !== DAILY_ALARM) return;
  try {
    await runAmazonSync({ dryRun: false });
  } catch (err) {
    console.warn("Scheduled sync failed:", err.message);
  }
});

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === "RUN_SYNC") {
    runAmazonSync({ dryRun: Boolean(msg.dryRun), days: msg.days })
      .then((res) => sendResponse({ ok: true, result: res }))
      .catch((err) => sendResponse({ ok: false, error: err.message, code: err.code }));
    return true;
  }
  if (msg.type === "CANCEL_SYNC") {
    cancelActiveSync()
      .then(() => sendResponse({ ok: true }))
      .catch((err) => sendResponse({ ok: false, error: err.message }));
    return true;
  }
  if (msg.type === "GET_SYNC_STATE") {
    getReconciledSyncState()
      .then((syncState) => sendResponse({ ok: true, syncState }))
      .catch((err) => sendResponse({ ok: false, error: err.message }));
    return true;
  }
  if (msg.type === "TEST_STATUS") {
    getSettings()
      .then((s) => callServer(s, "/api/status"))
      .then((status) => sendResponse({ ok: true, status }))
      .catch((err) => sendResponse({ ok: false, error: err.message }));
    return true;
  }
  if (msg.type === "UPDATE_ALARM") {
    configureAlarm(Boolean(msg.enabled))
      .then(() => sendResponse({ ok: true }))
      .catch((err) => sendResponse({ ok: false, error: err.message }));
    return true;
  }
  return false;
});
