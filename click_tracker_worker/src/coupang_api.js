const COUPANG_DOMAIN = "https://api-gateway.coupang.com";

export function formatCoupangDatetime(nowDate = new Date()) {
  const yy = String(nowDate.getUTCFullYear()).slice(-2);
  const mm = String(nowDate.getUTCMonth() + 1).padStart(2, "0");
  const dd = String(nowDate.getUTCDate()).padStart(2, "0");
  const hh = String(nowDate.getUTCHours()).padStart(2, "0");
  const mi = String(nowDate.getUTCMinutes()).padStart(2, "0");
  const ss = String(nowDate.getUTCSeconds()).padStart(2, "0");
  return `${yy}${mm}${dd}T${hh}${mi}${ss}Z`;
}

export async function generateCoupangAuthHeader(
  accessKey,
  secretKey,
  method,
  path,
  queryString = "",
  nowDate = new Date()
) {
  const datetimeStr = formatCoupangDatetime(nowDate);
  const message = `${datetimeStr}${String(method || "GET").toUpperCase()}${path}${queryString}`;
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(String(secretKey || "")),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const sigBuf = await crypto.subtle.sign("HMAC", key, enc.encode(message));
  const signature = Array.from(new Uint8Array(sigBuf))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
  return `CEA algorithm=HmacSHA256, access-key=${accessKey}, signed-date=${datetimeStr}, signature=${signature}`;
}

export async function coupangRequest(env, method, path, params = null, data = null) {
  const accessKey = String(env?.COUPANG_ACCESS_KEY || "").trim();
  const secretKey = String(env?.COUPANG_SECRET_KEY || "").trim();
  if (!accessKey || !secretKey) {
    throw new Error("쿠팡 ACCESS_KEY 또는 SECRET_KEY가 설정되지 않았습니다.");
  }

  let queryString = "";
  if (params && typeof params === "object") {
    const usp = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null && v !== "") {
        usp.append(k, String(v));
      }
    }
    queryString = usp.toString();
  }

  const url = `${COUPANG_DOMAIN}${path}${queryString ? `?${queryString}` : ""}`;
  const jsonBody = data ? JSON.stringify(data) : undefined;
  let lastErr = null;

  for (let attempt = 0; attempt < 2; attempt += 1) {
    const authHeader = await generateCoupangAuthHeader(accessKey, secretKey, method, path, queryString);
    try {
      const response = await fetch(url, {
        method: String(method || "GET").toUpperCase(),
        headers: {
          Authorization: authHeader,
          "Content-Type": "application/json;charset=UTF-8",
        },
        body: jsonBody,
      });

      if (response.status === 401) {
        const errTxt = (await response.text()).slice(0, 200);
        throw new Error(`쿠팡 API 401 (키 확인 필요) ${path}: ${errTxt}`);
      }
      if (response.status === 429 || (response.status >= 500 && response.status < 600)) {
        const errTxt = (await response.text()).slice(0, 200);
        lastErr = new Error(`HTTP ${response.status}: ${errTxt}`);
        if (attempt === 0) {
          await new Promise((resolve) => setTimeout(resolve, 400));
          continue;
        }
        break;
      }

      const text = await response.text();
      try {
        return JSON.parse(text);
      } catch {
        if (!response.ok) {
          throw new Error(`HTTP ${response.status}: ${text.slice(0, 200)}`);
        }
        return { rCode: String(response.status), rMessage: text };
      }
    } catch (err) {
      if (String(err?.message || "").includes("401")) {
        throw err;
      }
      lastErr = err;
      if (attempt === 0) {
        await new Promise((resolve) => setTimeout(resolve, 400));
      }
    }
  }

  throw new Error(`쿠팡 API 실패 (${path}): ${lastErr?.message || lastErr}`);
}

export async function searchProducts(env, keyword, limit = 10, subId = "") {
  const params = { keyword, limit };
  if (subId) params.subId = subId;
  return coupangRequest(
    env,
    "GET",
    "/v2/providers/affiliate_open_api/apis/openapi/products/search",
    params
  );
}

export async function createDeeplink(env, coupangUrls, subId = "") {
  const payload = { coupangUrls };
  if (subId) payload.subId = subId;
  return coupangRequest(
    env,
    "POST",
    "/v2/providers/affiliate_open_api/apis/openapi/v1/deeplink",
    null,
    payload
  );
}

export async function getOrderReport(env, startDate, endDate, page = 0, subId = "") {
  const params = { startDate, endDate, page };
  if (subId) params.subId = subId;
  return coupangRequest(
    env,
    "GET",
    "/v2/providers/affiliate_open_api/apis/openapi/v1/reports/orders",
    params
  );
}

export async function getCommissionReport(env, startDate, endDate, subId = "") {
  const params = { startDate, endDate };
  if (subId) params.subId = subId;
  return coupangRequest(
    env,
    "GET",
    "/v2/providers/affiliate_open_api/apis/openapi/v1/reports/commission",
    params
  );
}

export function classifySubId(subId) {
  const s = String(subId || "").trim().toLowerCase();
  if (!s) return ["poliwiki", "폴리위키/미지정"];
  if (s.startsWith("tg") || s.startsWith("auto_") || s.startsWith("hotpang")) {
    if (s.includes("arca")) return ["telegram", "아카라이브"];
    if (s.includes("ruli")) return ["telegram", "루리웹"];
    if (s.includes("ppom")) return ["telegram", "뽐뿌"];
    if (s.includes("dealbada")) return ["telegram", "딜바다"];
    if (s.includes("clien")) return ["telegram", "클리앙"];
    if (s.includes("gold")) return ["telegram", "골드박스"];
    if (s.includes("manual")) return ["telegram", "수동링크"];
    if (s.includes("search")) return ["telegram", "봇검색"];
    return ["telegram", "기타"];
  }
  return ["other", s];
}

function emptyStatBucket() {
  return { clicks: 0, orders: 0, cancels: 0, gmv: 0, commission: 0 };
}

function addCommRow(bucket, row) {
  bucket.clicks += Number.parseInt(String(row?.click || 0), 10) || 0;
  bucket.orders += Number.parseInt(String(row?.order || 0), 10) || 0;
  bucket.cancels += Number.parseInt(String(row?.cancel || 0), 10) || 0;
  bucket.gmv += Math.round(Number(row?.gmv || 0) || 0);
  bucket.commission += Math.round(Number(row?.commission || 0) || 0);
}

export function summarizePartnerStats(commRows, orderRows = null) {
  const total = emptyStatBucket();
  const telegram = emptyStatBucket();
  const poliwiki = emptyStatBucket();
  const other = emptyStatBucket();
  const tgSources = {};
  const groups = { telegram, poliwiki, other };

  for (const row of commRows || []) {
    addCommRow(total, row);
    const [grp, srcLabel] = classifySubId(row?.subId);
    addCommRow(groups[grp], row);
    if (grp === "telegram") {
      if (!tgSources[srcLabel]) tgSources[srcLabel] = emptyStatBucket();
      addCommRow(tgSources[srcLabel], row);
    }
  }

  const activeTgSources = {};
  for (const [k, val] of Object.entries(tgSources)) {
    if (val.clicks > 0 || val.orders > 0 || val.cancels > 0 || val.gmv > 0 || val.commission > 0) {
      activeTgSources[k] = {
        clicks: val.clicks,
        orders: val.orders,
        gmv: val.gmv,
        commission: val.commission,
      };
    }
  }

  let latestDate = "";
  for (const r of commRows || []) {
    const d = String(r?.date || "");
    if (d > latestDate) latestDate = d;
  }

  const latestTotal = emptyStatBucket();
  const latestTelegram = emptyStatBucket();
  const latestPoliwiki = emptyStatBucket();
  if (latestDate) {
    for (const row of commRows || []) {
      if (String(row?.date || "") !== latestDate) continue;
      addCommRow(latestTotal, row);
      const [grp] = classifySubId(row?.subId);
      if (grp === "telegram") addCommRow(latestTelegram, row);
      else if (grp === "poliwiki") addCommRow(latestPoliwiki, row);
    }
  }

  const taggedOrders = [];
  for (const it of orderRows || []) {
    const [grp, srcLabel] = classifySubId(it?.subId);
    taggedOrders.push({
      ...it,
      channel_group: grp,
      source_label: srcLabel,
    });
  }

  return {
    total,
    telegram,
    poliwiki,
    other,
    tg_sources: activeTgSources,
    latest_date: latestDate,
    latest_total: latestTotal,
    latest_telegram: latestTelegram,
    latest_poliwiki: latestPoliwiki,
    tagged_orders: taggedOrders,
  };
}

export function formatOrderSourceBadge(subId) {
  const [grp, srcLabel] = classifySubId(subId);
  if (grp === "telegram") return `✈️텔레그램·${srcLabel}`;
  if (grp === "poliwiki") return "🌐폴리위키";
  return `🔖${srcLabel}`;
}

export function formatChannelSplitBlock(stats, heading) {
  const tg = stats.telegram;
  const pw = stats.poliwiki;
  const ot = stats.other;
  const lines = [
    `📡 <b>${heading}</b>`,
    `• ✈️ 텔레그램(@HOTpang911): 클릭 <b>${tg.clicks.toLocaleString("en-US")}회</b> / 주문 <b>${tg.orders.toLocaleString("en-US")}건</b> / ${tg.gmv.toLocaleString("en-US")}원 (수익 <b>${tg.commission.toLocaleString("en-US")}원</b>)`,
  ];
  const tgSources = stats.tg_sources || {};
  const srcEntries = Object.entries(tgSources);
  if (srcEntries.length > 0) {
    const srcParts = srcEntries.map(
      ([src, v]) => `${src}: ${v.clicks.toLocaleString("en-US")}클릭·${v.orders.toLocaleString("en-US")}건`
    );
    lines.push(`  └ ${srcParts.join(", ")}`);
  }
  lines.push(
    `• 🌐 폴리위키/미지정: 클릭 <b>${pw.clicks.toLocaleString("en-US")}회</b> / 주문 <b>${pw.orders.toLocaleString("en-US")}건</b> / ${pw.gmv.toLocaleString("en-US")}원 (수익 <b>${pw.commission.toLocaleString("en-US")}원</b>)`
  );
  if (ot.clicks > 0 || ot.orders > 0 || ot.gmv > 0 || ot.commission > 0) {
    lines.push(
      `• 🔖 기타 채널: 클릭 <b>${ot.clicks.toLocaleString("en-US")}회</b> / 주문 <b>${ot.orders.toLocaleString("en-US")}건</b> / ${ot.gmv.toLocaleString("en-US")}원 (수익 <b>${ot.commission.toLocaleString("en-US")}원</b>)`
    );
  }
  return lines.join("\n");
}
