const AMAZON_TX_URL = "https://www.amazon.com/cpe/yourpayments/transactions";
const DAILY_ALARM = "sure-retail-sync-daily";
const MAX_PAGES = 10;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

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

function looksLikeSignIn(url, html) {
  if ((url || "").includes("/ap/signin")) return true;
  if (!html) return false;
  return (
    html.includes('name="signIn"') &&
    html.includes("ap_email") &&
    !html.includes("transaction")
  );
}

async function fetchAmazonText(url, options = {}) {
  const resp = await fetch(url, {
    credentials: "include",
    redirect: "follow",
    ...options,
  });
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

async function callServer(settings, path, { method = "GET", body = null } = {}) {
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
  const resp = await fetch(`${settings.serverUrl}${path}`, opts);
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
  const settings = await getSettings();
  const lookback = Number(days || settings.days || 45);

  await chrome.action.setBadgeText({ text: "..." });
  await chrome.action.setBadgeBackgroundColor({ color: "#3b82f6" });
  await setProgress(true, "Fetching Amazon Transactions page 1...");

  try {
    const firstHtml = await fetchAmazonText(AMAZON_TX_URL);
    let plan = await callServer(settings, "/api/amazon/plan", {
      method: "POST",
      body: { transactions_html: [firstHtml], days: lookback },
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
      await sleep(800);

      let pagePlan;
      if (nextPage.kind === "json") {
        const resp = await fetch(nextPage.url, {
          method: "POST",
          credentials: "include",
          headers: nextPage.headers,
          body: JSON.stringify(nextPage.body),
        });
        if (!resp.ok) break;
        const payload = await resp.json();
        pagePlan = await callServer(settings, "/api/amazon/plan", {
          method: "POST",
          body: {
            transactions_json_pages: [{ payload, meta: nextPage.meta }],
            days: lookback,
          },
        });
      } else {
        const formBody = new URLSearchParams(nextPage.form_data).toString();
        const html = await fetchAmazonText(nextPage.url, {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: formBody,
        });
        pagePlan = await callServer(settings, "/api/amazon/plan", {
          method: "POST",
          body: { transactions_html: [html], days: lookback },
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
        ordersHtml[orderNum] = await fetchAmazonText(url);
      } catch (err) {
        if (err.code === "AMAZON_SIGNIN_REQUIRED") throw err;
        console.warn("Order fetch failed", orderNum, err);
      }
      if (i + 1 < neededList.length) await sleep(1200);
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
    const needsSignIn = err.code === "AMAZON_SIGNIN_REQUIRED";
    await setProgress(false, err.message || String(err), {
      error: err.message || String(err),
      needsAmazonSignIn: needsSignIn,
    });
    await chrome.action.setBadgeText({ text: needsSignIn ? "AUTH" : "ERR" });
    await chrome.action.setBadgeBackgroundColor({ color: "#dc2626" });
    throw err;
  }
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
