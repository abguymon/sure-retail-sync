const AMAZON_TX_URL = "https://www.amazon.com/cpe/yourpayments/transactions";
const PAYPAL_ACTIVITY_URL = "https://www.paypal.com/myaccount/transactions/";
const PAYPAL_FILTER_URL = "https://www.paypal.com/myaccount/transactions/filter";
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

function formatIsoDate(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

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
      signInSource: null,
      updatedAt: new Date().toISOString(),
    };
    await chrome.storage.local.set({ syncState: cleared });
    await chrome.action.setBadgeText({ text: "" });
    return cleared;
  }
  return syncState;
}

function looksLikeAmazonSignIn(url, html) {
  if ((url || "").includes("/ap/signin")) return true;
  if (!html) return false;
  return (
    html.includes('name="signIn"') &&
    html.includes("ap_email") &&
    !html.includes("transaction")
  );
}

function looksLikePayPalSignIn(url, text) {
  const u = (url || "").toLowerCase();
  if (u.includes("/signin") || u.includes("/authflow") || u.includes("/login")) return true;
  if (!text) return false;
  const trimmed = text.trimStart();
  if (trimmed.startsWith("{") || trimmed.startsWith("[")) return false;
  return (
    (text.includes('name="login_email"') || text.includes('id="btnLogin"') || text.includes("Log in to your PayPal account")) &&
    !text.includes("myaccount/transactions/filter")
  );
}

function extractPayPalCsrf(html) {
  if (!html) return "";
  const patterns = [
    /"_csrf"\s*:\s*"([^"]+)"/,
    /"csrf"\s*:\s*"([^"]+)"/,
    /name=["']_csrf["'][^>]*value=["']([^"']+)["']/i,
    /value=["']([^"']+)["'][^>]*name=["']_csrf["']/i,
    /<meta[^>]+name=["']csrf-token["'][^>]+content=["']([^"']+)["']/i,
    /data-csrf-token=["']([^"']+)["']/i,
  ];
  for (const re of patterns) {
    const m = html.match(re);
    if (m && m[1]) return m[1];
  }
  return "";
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
  if (looksLikeAmazonSignIn(resp.url, text)) {
    const err = new Error("Not signed in to Amazon. Sign in and try again.");
    err.code = "AMAZON_SIGNIN_REQUIRED";
    throw err;
  }
  if (!resp.ok) {
    throw new Error(`Amazon returned HTTP ${resp.status} for ${url}`);
  }
  return text;
}

async function fetchPayPalPage(url, signal = null) {
  const resp = await fetchWithTimeout(
    url,
    {
      credentials: "include",
      redirect: "follow",
    },
    signal
  );
  const text = await resp.text();
  if (looksLikePayPalSignIn(resp.url, text) || resp.status === 401) {
    const err = new Error("Not signed in to PayPal. Sign in and try again.");
    err.code = "PAYPAL_SIGNIN_REQUIRED";
    throw err;
  }
  if (!resp.ok) {
    throw new Error(`PayPal returned HTTP ${resp.status} for ${url}`);
  }
  return text;
}

async function fetchPayPalJson(url, csrfToken, signal = null) {
  const headers = {
    Accept: "application/json",
    "X-Requested-With": "XMLHttpRequest",
  };
  if (csrfToken) headers["x-csrf-token"] = csrfToken;

  const resp = await fetchWithTimeout(
    url,
    {
      credentials: "include",
      redirect: "follow",
      headers,
    },
    signal
  );
  const text = await resp.text();
  if (looksLikePayPalSignIn(resp.url, text) || resp.status === 401) {
    const err = new Error("Not signed in to PayPal. Sign in and try again.");
    err.code = "PAYPAL_SIGNIN_REQUIRED";
    throw err;
  }
  if (!resp.ok) {
    throw new Error(`PayPal returned HTTP ${resp.status} for ${url}`);
  }
  try {
    return JSON.parse(text);
  } catch {
    throw new Error(`PayPal returned non-JSON response (${resp.status}): ${text.slice(0, 120)}`);
  }
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
  await setProgress(true, "Fetching Amazon Transactions page 1...", { source: "amazon" });

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
      await setProgress(true, `Fetching Amazon Transactions page ${pageNum}...`, { source: "amazon" });
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
        `Fetching order details (${i + 1}/${neededList.length}): ${orderNum}`,
        { source: "amazon" }
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
        : "Categorizing & updating Amazon transactions in Sure...",
      { source: "amazon" }
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
      source: "amazon",
      days: lookback,
      pagesFetched: pageNum,
      ordersFetched: Object.keys(ordersHtml).length,
      completedAt: new Date().toISOString(),
    };

    const touched = (result.stats?.updated || 0) + (result.stats?.split || 0);
    await chrome.storage.local.set({ lastResult: summary });
    await setProgress(false, dryRun ? "Amazon dry run complete." : "Amazon sync complete.", {
      source: "amazon",
      error: null,
      signInSource: null,
    });
    await chrome.action.setBadgeText({ text: touched > 0 ? String(touched) : "" });
    await chrome.action.setBadgeBackgroundColor({ color: "#16a34a" });
    return summary;
  } catch (err) {
    const cancelled = err.name === "AbortError" && signal.aborted;
    const needsSignIn = err.code === "AMAZON_SIGNIN_REQUIRED";
    await setProgress(false, cancelled ? "Sync cancelled." : err.message || String(err), {
      source: "amazon",
      error: cancelled ? null : err.message || String(err),
      signInSource: needsSignIn ? "amazon" : null,
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

async function runPayPalSync({ dryRun = false, days = null } = {}) {
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
  await setProgress(true, "Checking PayPal session...", { source: "paypal" });

  try {
    const activityHtml = await fetchPayPalPage(PAYPAL_ACTIVITY_URL, signal);
    const csrfToken = extractPayPalCsrf(activityHtml);

    const now = new Date();
    const startObj = new Date(now.getTime() - (lookback + 3) * 86400 * 1000);
    const endObj = new Date(now.getTime() + 2 * 86400 * 1000);
    const startDate = formatIsoDate(startObj);
    const endDate = formatIsoDate(endObj);

    const allCharges = [];
    const neededMap = new Map();
    const seenTokens = new Set();
    let nextToken = "";
    let pageNum = 0;

    while (pageNum < MAX_PAGES) {
      pageNum += 1;
      await setProgress(true, `Fetching PayPal activity page ${pageNum}...`, { source: "paypal" });

      const params = new URLSearchParams({
        transactionType: "ALL",
        nextPageToken: nextToken,
        freeTextSearch: "",
        isClearFreeTextSearch: "false",
        isClearFilterSelection: "false",
        isClientSideFiltering: "false",
        selectedCurrency: "ALL",
        startDate,
        endDate,
      });

      const payload = await fetchPayPalJson(`${PAYPAL_FILTER_URL}?${params.toString()}`, csrfToken, signal);
      const plan = await callServer(settings, "/api/paypal/plan", {
        method: "POST",
        body: { filter_pages: [payload], days: lookback },
        signal,
      });

      for (const c of plan.charges || []) allCharges.push(c);
      for (const d of plan.needed_details || []) neededMap.set(d.id, d.url);

      const tok = plan.next_page_token || "";
      if (!tok || seenTokens.has(tok)) break;
      seenTokens.add(tok);
      nextToken = tok;
      await sleep(700, signal);
    }

    const neededList = Array.from(neededMap.entries());
    const detailsJson = {};
    for (let i = 0; i < neededList.length; i += 1) {
      const [tid, url] = neededList[i];
      await setProgress(
        true,
        `Fetching PayPal details (${i + 1}/${neededList.length})...`,
        { source: "paypal" }
      );
      try {
        detailsJson[tid] = await fetchPayPalJson(url, csrfToken, signal);
      } catch (err) {
        if (err.code === "PAYPAL_SIGNIN_REQUIRED" || err.name === "AbortError") throw err;
        console.warn("PayPal detail fetch failed", tid, err);
      }
      if (i + 1 < neededList.length) await sleep(600, signal);
    }

    await setProgress(
      true,
      dryRun
        ? "Running PayPal dry-run categorization on server..."
        : "Categorizing & updating PayPal transactions in Sure...",
      { source: "paypal" }
    );

    const result = await callServer(settings, "/api/paypal/sync", {
      method: "POST",
      body: {
        charges: allCharges,
        details_json: detailsJson,
        days: lookback,
        dry_run: dryRun,
      },
      signal,
    });

    const summary = {
      ...result,
      source: "paypal",
      days: lookback,
      pagesFetched: pageNum,
      detailsFetched: Object.keys(detailsJson).length,
      completedAt: new Date().toISOString(),
    };

    const touched = (result.stats?.updated || 0) + (result.stats?.split || 0);
    await chrome.storage.local.set({ lastResult: summary });
    await setProgress(false, dryRun ? "PayPal dry run complete." : "PayPal sync complete.", {
      source: "paypal",
      error: null,
      signInSource: null,
    });
    await chrome.action.setBadgeText({ text: touched > 0 ? String(touched) : "" });
    await chrome.action.setBadgeBackgroundColor({ color: "#16a34a" });
    return summary;
  } catch (err) {
    const cancelled = err.name === "AbortError" && signal.aborted;
    const needsSignIn = err.code === "PAYPAL_SIGNIN_REQUIRED";
    await setProgress(false, cancelled ? "Sync cancelled." : err.message || String(err), {
      source: "paypal",
      error: cancelled ? null : err.message || String(err),
      signInSource: needsSignIn ? "paypal" : null,
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
  await setProgress(false, "Sync cancelled.", { error: null, signInSource: null });
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
    console.warn("Scheduled Amazon sync failed:", err.message);
  }
  try {
    await runPayPalSync({ dryRun: false });
  } catch (err) {
    console.warn("Scheduled PayPal sync failed:", err.message);
  }
});

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === "RUN_SYNC") {
    const runner = msg.source === "paypal" ? runPayPalSync : runAmazonSync;
    runner({ dryRun: Boolean(msg.dryRun), days: msg.days })
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
