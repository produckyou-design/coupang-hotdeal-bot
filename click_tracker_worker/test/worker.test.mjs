import test from "node:test";
import assert from "node:assert/strict";

import {
  classifySubId,
  formatChannelSplitBlock,
  formatOrderSourceBadge,
  generateCoupangAuthHeader,
  summarizePartnerStats,
} from "../src/coupang_api.js";
import {
  compareOffers,
  createPad,
  describeOffer,
  formatRate,
  makeFrac,
  naturalOffer,
  padApply,
  padScreen,
  parseOffer,
  waterRankings,
} from "../src/unit_compare.js";
import worker, { runDailyMaintenance, runScheduledDispatch } from "../src/index.js";

function createMockD1() {
  const store = {
    links: [],
    clicks: [],
    keyword_alerts: [],
    alert_prefs: new Map(),
    channel_stats: new Map(),
    channel_meta: new Map(),
    posted_messages: [],
    bot_sessions: new Map(),
    nextAlertId: 1,
    nextClickId: 1,
  };

  function runQuery(sql, args, mode) {
    const s = sql.replace(/\s+/g, " ").trim();

    if (s.startsWith("SELECT state_json FROM bot_sessions")) {
      const row = store.bot_sessions.get(String(args[0]));
      return row ? { state_json: row } : null;
    }
    if (s.startsWith("INSERT INTO bot_sessions")) {
      store.bot_sessions.set(String(args[0]), String(args[1]));
      return { meta: { changes: 1 } };
    }
    if (s.startsWith("SELECT value FROM channel_meta WHERE key =")) {
      const m = /key = '([^']+)'/.exec(s);
      const key = m ? m[1] : String(args[0]);
      return store.channel_meta.has(key) ? { value: store.channel_meta.get(key) } : null;
    }
    if (s.startsWith("DELETE FROM channel_meta WHERE key LIKE 'source_funnel_%'")) {
      const cutoff = String(args[0]);
      let changes = 0;
      for (const k of Array.from(store.channel_meta.keys())) {
        if (k.startsWith("source_funnel_") && k < cutoff) {
          store.channel_meta.delete(k);
          changes += 1;
        }
      }
      return { meta: { changes } };
    }
    if (s.startsWith("INSERT INTO channel_meta")) {
      const m = /VALUES \('([^']+)'/.exec(s);
      const key = m ? m[1] : String(args[0]);
      const val = m ? String(args[0]) : String(args[1]);
      store.channel_meta.set(key, val);
      return { meta: { changes: 1 } };
    }
    if (s.startsWith("SELECT start_count, latest_count, peak_count FROM channel_stats")) {
      return store.channel_stats.get(String(args[0])) || null;
    }
    if (s.startsWith("INSERT INTO channel_stats")) {
      store.channel_stats.set(String(args[0]), {
        kst_date: String(args[0]),
        start_count: Number(args[1]),
        latest_count: Number(args[2]),
        peak_count: Number(args[3]),
      });
      return { meta: { changes: 1 } };
    }
    if (s.startsWith("UPDATE channel_stats")) {
      const row = store.channel_stats.get(String(args[2]));
      if (row) {
        row.latest_count = Number(args[0]);
        row.peak_count = Number(args[1]);
      }
      return { meta: { changes: row ? 1 : 0 } };
    }
    if (s.startsWith("SELECT kst_date, start_count, latest_count, peak_count FROM channel_stats")) {
      const list = Array.from(store.channel_stats.values()).sort((a, b) => b.kst_date.localeCompare(a.kst_date));
      return { results: list.slice(0, 7) };
    }
    if (s.startsWith("SELECT paused, quiet_night FROM alert_prefs")) {
      return store.alert_prefs.get(String(args[0])) || null;
    }
    if (s.startsWith("INSERT INTO alert_prefs")) {
      store.alert_prefs.set(String(args[0]), { paused: Number(args[1]), quiet_night: Number(args[2]) });
      return { meta: { changes: 1 } };
    }
    if (s.startsWith("DELETE FROM alert_prefs")) {
      const had = store.alert_prefs.delete(String(args[0]));
      return { meta: { changes: had ? 1 : 0 } };
    }
    if (s.includes("FROM keyword_alerts WHERE chat_id = ? AND keyword_norm = ?")) {
      const hit = store.keyword_alerts.find((a) => a.chat_id === String(args[0]) && a.keyword_norm === String(args[1]));
      return hit || null;
    }
    if (s.startsWith("UPDATE keyword_alerts SET keyword =")) {
      const hit = store.keyword_alerts.find((a) => a.chat_id === String(args[4]) && a.keyword_norm === String(args[5]));
      if (hit) {
        hit.keyword = args[0];
        hit.max_price = args[1];
        hit.min_discount = args[2];
        hit.exclude_terms = args[3];
      }
      return { meta: { changes: hit ? 1 : 0 } };
    }
    if (s.startsWith("SELECT COUNT(*) AS cnt FROM keyword_alerts WHERE chat_id = ?")) {
      const cnt = store.keyword_alerts.filter((a) => a.chat_id === String(args[0])).length;
      return { cnt };
    }
    if (s.startsWith("INSERT OR IGNORE INTO keyword_alerts")) {
      const id = store.nextAlertId++;
      store.keyword_alerts.push({
        id,
        chat_id: String(args[0]),
        keyword: args[1],
        keyword_norm: args[2],
        max_price: Number(args[3]),
        min_discount: Number(args[4]),
        exclude_terms: args[5],
        hit_count: 0,
        last_notified_at: "",
        created_at: args[6],
      });
      return { meta: { last_row_id: id, changes: 1 } };
    }
    if (s.includes("FROM keyword_alerts a") && s.includes("WHERE a.chat_id = ?")) {
      const prefs = store.alert_prefs.get(String(args[0])) || { paused: 0, quiet_night: 1 };
      const rows = store.keyword_alerts
        .filter((a) => a.chat_id === String(args[0]))
        .map((a) => ({ ...a, paused: prefs.paused, quiet_night: prefs.quiet_night }));
      return { results: rows.slice(0, Number(args[1] || 10)) };
    }
    if (s.startsWith("SELECT keyword, keyword_norm,") && s.includes("FROM keyword_alerts WHERE chat_id = ?")) {
      return { results: store.keyword_alerts.filter((a) => a.chat_id === String(args[0])) };
    }
    if (s.startsWith("DELETE FROM keyword_alerts WHERE chat_id = ? AND id = ?")) {
      const before = store.keyword_alerts.length;
      store.keyword_alerts = store.keyword_alerts.filter((a) => !(a.chat_id === String(args[0]) && a.id === Number(args[1])));
      return { meta: { changes: before - store.keyword_alerts.length } };
    }
    if (s.startsWith("DELETE FROM keyword_alerts WHERE chat_id = ? AND keyword_norm = ?")) {
      const before = store.keyword_alerts.length;
      store.keyword_alerts = store.keyword_alerts.filter((a) => !(a.chat_id === String(args[0]) && a.keyword_norm === String(args[1])));
      return { meta: { changes: before - store.keyword_alerts.length } };
    }
    if (s.startsWith("DELETE FROM keyword_alerts WHERE chat_id = ?")) {
      const before = store.keyword_alerts.length;
      store.keyword_alerts = store.keyword_alerts.filter((a) => a.chat_id !== String(args[0]));
      return { meta: { changes: before - store.keyword_alerts.length } };
    }
    if (s.includes("COUNT(*) AS total_keywords")) {
      const users = new Set(store.keyword_alerts.map((a) => a.chat_id));
      const totalHits = store.keyword_alerts.reduce((acc, a) => acc + Number(a.hit_count || 0), 0);
      return { total_keywords: store.keyword_alerts.length, total_users: users.size, total_hits: totalHits };
    }
    if (s.includes("AS paused_users")) {
      const users = new Set(store.keyword_alerts.map((a) => a.chat_id));
      let p = 0;
      for (const u of users) if ((store.alert_prefs.get(u)?.paused || 0) === 1) p += 1;
      return { paused_users: p };
    }
    if (s.includes("GROUP BY keyword_norm")) {
      const map = new Map();
      for (const a of store.keyword_alerts) {
        const cur = map.get(a.keyword_norm) || { keyword: a.keyword, keyword_norm: a.keyword_norm, subscribers: 0, hits: 0 };
        cur.subscribers += 1;
        cur.hits += Number(a.hit_count || 0);
        map.set(a.keyword_norm, cur);
      }
      return { results: Array.from(map.values()) };
    }
    if (s.startsWith("SELECT code, product_id, title, source, sale_price")) {
      return { results: store.links.filter((l) => l.created_at >= String(args[0]) && l.title !== "") };
    }
    if (s.startsWith("SELECT code, target_url, deal_id, product_id, source, track, title, sale_price, discount_rate")) {
      const want = new Set(args.map((a) => String(a)));
      return { results: store.links.filter((l) => want.has(String(l.code))) };
    }
    if (s.startsWith("INSERT INTO links")) {
      store.links.push({
        code: args[0],
        target_url: args[1],
        deal_id: args[2],
        product_id: args[3],
        source: args[4],
        track: args[5],
        title: args[6],
        sale_price: Number(args[7]),
        discount_rate: Number(args[8]),
        unit_info: args.length >= 13 ? String(args[9] || "") : "",
        shipping_label: args.length >= 13 ? String(args[10] || "") : "",
        option_key: args.length >= 13 ? String(args[11] || "") : "",
        created_at: args.length >= 13 ? String(args[12] || "") : String(args[9] || ""),
      });
      return { meta: { changes: 1 } };
    }
    if (s.startsWith("SELECT COUNT(*) AS clicks, COUNT(DISTINCT code)")) {
      const dayClicks = store.clicks.filter((c) => c.kst_date === String(args[0]));
      return { clicks: dayClicks.length, unique_links: new Set(dayClicks.map((c) => c.code)).size };
    }
    if (s.startsWith("SELECT COUNT(*) AS clicks FROM clicks")) {
      return { clicks: store.clicks.filter((c) => c.kst_date >= String(args[0])).length };
    }
    if (s.startsWith("SELECT COUNT(*) AS total_links FROM links")) {
      return { total_links: store.links.length };
    }
    if (s.startsWith("SELECT items_json, all_closed FROM posted_messages")) {
      return { results: [] };
    }
    if (s.startsWith("DELETE FROM bot_sessions")) {
      const before = store.bot_sessions.size;
      store.bot_sessions.clear();
      return { meta: { changes: before } };
    }
    if (s.startsWith("DELETE FROM posted_messages")) {
      const before = store.posted_messages.length;
      store.posted_messages = store.posted_messages.filter((p) => p.posted_at >= String(args[0]));
      return { meta: { changes: before - store.posted_messages.length } };
    }
    if (s.startsWith("DELETE FROM clicks")) {
      const before = store.clicks.length;
      store.clicks = store.clicks.filter((c) => c.clicked_at >= String(args[0]));
      return { meta: { changes: before - store.clicks.length } };
    }
    if (s.startsWith("DELETE FROM links")) {
      const clickedCodes = new Set(store.clicks.map((c) => c.code));
      const before = store.links.length;
      store.links = store.links.filter((l) => !(l.created_at < String(args[0]) && !clickedCodes.has(l.code)));
      return { meta: { changes: before - store.links.length } };
    }
    if (mode === "all") return { results: [] };
    if (mode === "first") return null;
    return { meta: { changes: 0 } };
  }

  return {
    store,
    prepare(sql) {
      let bound = [];
      const stmt = {
        bind(...args) {
          bound = args;
          return stmt;
        },
        async first() {
          return runQuery(sql, bound, "first");
        },
        async all() {
          return runQuery(sql, bound, "all");
        },
        async run() {
          return runQuery(sql, bound, "run");
        },
      };
      return stmt;
    },
    async batch(stmts) {
      for (const st of stmts) await st.run();
      return [];
    },
  };
}

test("unit_compare.js parity with Python test_unit_compare.py", () => {
  assert.equal(
    compareOffers(parseOffer("8,900원 / 500ml×40 / 0원"), parseOffer("11,900원 / 1.5L×12 / 3,000원")),
    "🍯 배송비 포함 단가\n첫째 · 총 8,900원 · 1L당 445원\n둘째 · 총 14,900원 · 1L당 약 827.78원\n\n첫째 상품의 1L당 가격이 더 낮아요."
  );

  assert.equal(
    compareOffers(parseOffer("10,000원 / 1kg / 0원"), parseOffer("2,500원 / 250g / 0원")),
    "🍯 배송비 포함 단가\n첫째 · 총 10,000원 · 100g당 1,000원\n둘째 · 총 2,500원 · 100g당 1,000원\n\n두 상품의 단가가 같아요."
  );

  assert.equal(formatRate(makeFrac(1n, 200n)), "0.01원 미만");
  assert.equal(describeOffer(naturalOffer("생수 500ml 24병 7,200원")), "24병 · 7,200원 (상품값 기준 · 배송비 미확인)\n1병당 300원\n1L당 600원");

  const pad1 = createPad();
  for (const act of ["vml", "v500", "v40", "v12900"]) assert.equal(padApply(pad1, act, null), null);
  const first = padApply(pad1, "v0", null);
  assert.equal(first.total, 12900);
  const pad2 = createPad();
  const [unitPrompt] = padScreen(pad2, first);
  assert.match(unitPrompt, /둘째 상품/);
  assert.equal(padApply(pad2, "vL", first), null);
  assert.equal(padApply(pad2, "v1.5", first), null);
  assert.equal(padApply(pad2, "v12", first), null);
  assert.equal(padApply(pad2, "custom", first), null);
  for (const d of "11900") assert.equal(padApply(pad2, `d${d}`, first), null);
  assert.equal(padApply(pad2, "ok", first), null);
  const second = padApply(pad2, "v3000", first);
  assert.match(compareOffers(first, second), /첫째 상품의 1L당 가격이 더 낮아요/);

  const ranked = waterRankings([
    { categoryName: "식품", productName: "제주 삼다수 생수 2L, 12개", productPrice: 6790, isFreeShipping: true, productId: 1, productUrl: "https://link.coupang.com/a" },
    { categoryName: "생활용품", productName: "생수 공병 2L 10개", productPrice: 3000, isFreeShipping: true, productId: 2, productUrl: "https://link.coupang.com/b" },
  ]);
  assert.equal(ranked.length, 1);
  assert.equal(ranked[0][2], 12);
});

test("coupang_api.js HMAC header, classifySubId, summarizePartnerStats", async () => {
  const fixedDate = new Date("2026-11-28T01:02:03Z");
  const auth = await generateCoupangAuthHeader("ak_test", "sk_test", "GET", "/v2/test", "a=1&b=2", fixedDate);
  assert.match(auth, /^CEA algorithm=HmacSHA256, access-key=ak_test, signed-date=261128T010203Z, signature=[0-9a-f]{64}$/);
  assert.deepEqual(classifySubId("tgclien"), ["telegram", "클리앙"]);
  const stats = summarizePartnerStats([{ date: "20261128", subId: "tgarca", click: 10, order: 2, cancel: 0, gmv: 20000, commission: 600 }]);
  const block = formatChannelSplitBlock(stats, "테스트 집계");
  assert.match(block, /텔레그램\(@HOTpang911\): 클릭 <b>10회<\/b>/);
  assert.equal(formatOrderSourceBadge("tgclien"), "✈️텔레그램·클리앙");
});

test("Telegram webhook auth, chat_member +1/milestone, /alert, ComparePad, /status & cron", async () => {
  const db = createMockD1();
  db.store.channel_meta.set("member_count", "49");
  db.store.channel_meta.set("flyer_status", JSON.stringify({ date: "20261128", community_sent: 5, goldbox_sent: 1, waiting: 2 }));
  db.store.links.push({
    code: "c1",
    deal_id: "d1",
    product_id: "987654321",
    source: "clien",
    title: "애플 에어팟 프로 2세대",
    sale_price: 239000,
    discount_rate: 25,
    created_at: new Date().toISOString(),
  });

  const tgCalls = [];
  const origFetch = globalThis.fetch;
  globalThis.fetch = async (url, opts = {}) => {
    const u = String(url);
    const body = opts.body ? JSON.parse(opts.body) : {};
    tgCalls.push({ url: u, body });
    if (u.endsWith("/getChatMemberCount")) {
      return new Response(JSON.stringify({ ok: true, result: 50 }), { status: 200 });
    }
    if (u.includes("api.github.com")) {
      return new Response(null, { status: 204 });
    }
    return new Response(JSON.stringify({ ok: true, result: { message_id: 700 + tgCalls.length } }), { status: 200 });
  };

  try {
    const env = {
      DB: db,
      TELEGRAM_BOT_TOKEN: "123:test",
      TELEGRAM_ADMIN_CHAT_ID: "999001",
      TELEGRAM_PUBLIC_CHANNEL_ID: "@HOTpang911",
      TELEGRAM_WEBHOOK_SECRET: "super-secret-token",
      CLICK_TRACKER_API_KEY: "api-key",
      GH_DISPATCH_TOKEN: "ghp_test",
    };

    // 1) Bad secret -> 401
    const badReq = new Request("https://hotpang-click.example.workers.dev/telegram/webhook", {
      method: "POST",
      headers: { "X-Telegram-Bot-Api-Secret-Token": "wrong", "Content-Type": "application/json" },
      body: JSON.stringify({ update_id: 1 }),
    });
    const badRes = await worker.fetch(badReq, env);
    assert.equal(badRes.status, 401);

    const postWebhook = async (update) => {
      const req = new Request("https://hotpang-click.example.workers.dev/telegram/webhook", {
        method: "POST",
        headers: { "X-Telegram-Bot-Api-Secret-Token": "super-secret-token", "Content-Type": "application/json" },
        body: JSON.stringify(update),
      });
      const res = await worker.fetch(req, env);
      assert.equal(res.status, 200);
    };

    // 2) Channel member +1 (49 -> 50 milestone)
    await postWebhook({
      update_id: 10,
      chat_member: {
        chat: { id: -10012345, username: "HOTpang911", type: "channel" },
        old_chat_member: { status: "left" },
        new_chat_member: { status: "member" },
      },
    });
    const dmCall = tgCalls.find((c) => c.url.endsWith("/sendMessage") && c.body.chat_id === 999001);
    assert.ok(dmCall);
    assert.match(dmCall.body.text, /50명 돌파/);

    // 3) Smart /alert registration + recent match
    tgCalls.length = 0;
    await postWebhook({
      update_id: 11,
      message: { message_id: 1, chat: { id: 3001, type: "private" }, from: { id: 3001 }, text: "/alert" },
    });
    await postWebhook({
      update_id: 12,
      message: { message_id: 2, chat: { id: 3001, type: "private" }, from: { id: 3001 }, text: "에어팟 25만원이하 -케이스" },
    });
    const alertReply = tgCalls.filter((c) => c.url.endsWith("/sendMessage") && c.body.chat_id === 3001).at(-1);
    assert.ok(alertReply);
    assert.match(alertReply.body.text, /알림 등록 완료/);
    assert.match(alertReply.body.text, /최근 채널에 올라온 관련 핫딜/);

    // 4) ComparePad session roundtrip
    tgCalls.length = 0;
    await postWebhook({
      update_id: 13,
      callback_query: { id: "cb1", from: { id: 3001 }, data: "compare:pad", message: { message_id: 50, chat: { id: 3001, type: "private" } } },
    });
    const padPrompt = tgCalls.find((c) => c.url.endsWith("/sendMessage") && c.body.chat_id === 3001);
    assert.ok(padPrompt);
    const mlBtn = padPrompt.body.reply_markup.inline_keyboard[0][0].callback_data;
    await postWebhook({
      update_id: 14,
      callback_query: { id: "cb2", from: { id: 3001 }, data: mlBtn, message: { message_id: 701, chat: { id: 3001, type: "private" } } },
    });
    const editCall = tgCalls.find((c) => c.url.endsWith("/editMessageText"));
    assert.ok(editCall);
    assert.match(editCall.body.text, /용량이나 무게/);

    // 5) /status + /clicks admin
    tgCalls.length = 0;
    await postWebhook({
      update_id: 15,
      message: { message_id: 3, chat: { id: 999001, type: "private" }, from: { id: 999001 }, text: "/status" },
    });
    const statusMsg = tgCalls.find((c) => c.url.endsWith("/sendMessage") && c.body.chat_id === 999001);
    assert.ok(statusMsg);
    assert.match(statusMsg.body.text, /전단지 현황/);
    assert.match(statusMsg.body.text, /대기 상품 2건/);

    // 6) Scheduled cron dispatch (KST daytime vs quiet hours)
    const kst1405Utc = new Date(Date.UTC(2026, 10, 28, 5, 5, 0)); // 14:05 KST
    const cronDay = await runScheduledDispatch(env, kst1405Utc);
    assert.equal(cronDay.ok, true);
    assert.equal(cronDay.status, 204);

    const kst0305Utc = new Date(Date.UTC(2026, 10, 27, 18, 5, 0)); // 03:05 KST -> skip
    const cronNightSkip = await runScheduledDispatch(env, kst0305Utc);
    assert.equal(cronNightSkip.skipped, true);

    const kst0320Utc = new Date(Date.UTC(2026, 10, 27, 18, 20, 0)); // 03:20 KST -> fire
    const cronNightRun = await runScheduledDispatch(env, kst0320Utc);
    assert.equal(cronNightRun.ok, true);
    assert.equal(cronNightRun.status, 204);
  } finally {
    globalThis.fetch = origFetch;
  }
});

test("runDailyMaintenance self-heals webhook & watchdog handles 3x dispatch failures and stale runner", async () => {
  const db = createMockD1();
  const nowDate = new Date("2026-11-28T04:00:00+09:00");
  db.store.bot_sessions.set("old_user", JSON.stringify({ mode: "" }));
  db.store.posted_messages.push({ message_id: 1, posted_at: "2026-10-01T00:00:00Z" });
  db.store.links.push({ code: "old1", created_at: "2024-01-01T00:00:00Z" });

  const calls = [];
  const origFetch = globalThis.fetch;
  let ghMode = "fail";
  globalThis.fetch = async (url, opts = {}) => {
    const u = String(url);
    const body = opts.body ? JSON.parse(opts.body) : {};
    calls.push({ u, body });
    if (u.endsWith("/getWebhookInfo")) {
      return new Response(JSON.stringify({ ok: true, result: { url: "", allowed_updates: [] } }), { status: 200 });
    }
    if (u.endsWith("/setWebhook")) {
      return new Response(JSON.stringify({ ok: true, result: true }), { status: 200 });
    }
    if (u.includes("api.github.com")) {
      if (ghMode === "fail") return new Response("Unauthorized", { status: 401 });
      return new Response(null, { status: 204 });
    }
    return new Response(JSON.stringify({ ok: true, result: { message_id: 901 } }), { status: 200 });
  };

  try {
    const env = {
      DB: db,
      TELEGRAM_BOT_TOKEN: "123:test",
      TELEGRAM_ADMIN_CHAT_ID: "999001",
      TELEGRAM_WEBHOOK_SECRET: "wh-sec",
      WORKER_BASE_URL: "https://hotpang-click.produckyou.workers.dev",
      GH_DISPATCH_TOKEN: "ghp_test",
    };

    // 1) Daily maintenance prunes D1 & self-heals webhook
    const maint = await runDailyMaintenance(env, nowDate);
    assert.equal(maint.ok, true);
    assert.equal(maint.webhookHealed, true);
    assert.equal(maint.pruned.sessions, 1);
    assert.equal(maint.pruned.posts, 1);
    assert.equal(maint.pruned.links, 1);
    assert.ok(calls.some((c) => c.u.endsWith("/setWebhook") && c.body.url === "https://hotpang-click.produckyou.workers.dev/telegram/webhook"));
    assert.ok(calls.some((c) => c.u.endsWith("/sendMessage") && /자동 복구/.test(c.body.text)));

    // 2) 3 consecutive dispatch failures -> alert DM, then recovery DM
    calls.length = 0;
    const dayTime = new Date("2026-11-28T10:05:00+09:00");
    await runScheduledDispatch(env, dayTime);
    await runScheduledDispatch(env, dayTime);
    assert.equal(calls.filter((c) => c.u.endsWith("/sendMessage")).length, 0);
    await runScheduledDispatch(env, dayTime);
    const failDm = calls.find((c) => c.u.endsWith("/sendMessage"));
    assert.ok(failDm);
    assert.match(failDm.body.text, /3회 연속/);

    // 4th fail throttled within 6h
    calls.length = 0;
    await runScheduledDispatch(env, dayTime);
    assert.equal(calls.filter((c) => c.u.endsWith("/sendMessage")).length, 0);

    // Recovery triggers recovery DM and resets streak
    calls.length = 0;
    ghMode = "ok";
    await runScheduledDispatch(env, dayTime);
    const recDm = calls.find((c) => c.u.endsWith("/sendMessage"));
    assert.ok(recDm);
    assert.match(recDm.body.text, /정상 복구/);
    assert.equal(db.store.channel_meta.get("dispatch_fail_streak"), "0");

    // 3) Stale flyer_status > 4h triggers watchdog DM
    db.store.channel_meta.delete("last_watchdog_alert");
    db.store.channel_meta.set(
      "flyer_status",
      JSON.stringify({ date: "20261128", community_sent: 1, goldbox_sent: 0, waiting: 0, updated_at: "2026-11-28T00:00:00Z" })
    );
    calls.length = 0;
    await runScheduledDispatch(env, new Date("2026-11-28T10:05:00Z"));
    const staleDm = calls.find((c) => c.u.endsWith("/sendMessage"));
    assert.ok(staleDm);
    assert.match(staleDm.body.text, /4시간 이상/);
  } finally {
    globalThis.fetch = origFetch;
  }
});

test("reportCmd places daily performance summary at the very bottom", async () => {
  const db = createMockD1();
  db.store.channel_meta.set("member_count", "50");
  const tgCalls = [];
  const origFetch = globalThis.fetch;
  globalThis.fetch = async (url, opts = {}) => {
    const u = String(url);
    const body = opts.body ? JSON.parse(opts.body) : {};
    tgCalls.push({ u, body });
    if (u.includes("/reports/commission")) {
      return new Response(
        JSON.stringify({
          rCode: "0",
          data: [
            { date: "20261126", subId: "tgarca", click: 5, order: 1, cancel: 0, gmv: 10000, commission: 300 },
            { date: "20261127", subId: "tgclien", click: 8, order: 2, cancel: 0, gmv: 45000, commission: 1350 },
          ],
        }),
        { status: 200 }
      );
    }
    if (u.includes("/reports/orders")) {
      return new Response(
        JSON.stringify({
          rCode: "0",
          data: [{ date: "20261127", subId: "tgclien", productName: "코멧 생수", quantity: 2, gmv: 45000, commission: 1350 }],
        }),
        { status: 200 }
      );
    }
    return new Response(JSON.stringify({ ok: true, result: { message_id: 801 } }), { status: 200 });
  };

  try {
    const env = {
      DB: db,
      TELEGRAM_BOT_TOKEN: "123:test",
      TELEGRAM_ADMIN_CHAT_ID: "999001",
      TELEGRAM_WEBHOOK_SECRET: "sec",
      COUPANG_ACCESS_KEY: "ak",
      COUPANG_SECRET_KEY: "sk",
    };
    const req = new Request("https://hotpang-click.example.workers.dev/telegram/webhook", {
      method: "POST",
      headers: { "X-Telegram-Bot-Api-Secret-Token": "sec", "Content-Type": "application/json" },
      body: JSON.stringify({
        update_id: 99,
        message: { message_id: 1, chat: { id: 999001, type: "private" }, from: { id: 999001 }, text: "/report" },
      }),
    });
    const res = await worker.fetch(req, env);
    assert.equal(res.status, 200);
    const editCall = tgCalls.find((c) => c.u.endsWith("/editMessageText"));
    assert.ok(editCall);
    const txt = editCall.body.text;
    const idxMonth = txt.indexOf("월 누적");
    const idxChan = txt.indexOf("채널별 분리 집계");
    const idxOrders = txt.indexOf("[최근 7일 주문 품목]");
    const idxNote = txt.indexOf("※");
    const idxDaily = txt.indexOf("최근 확정일 실적 (2026.11.27)");
    assert.ok(idxMonth >= 0 && idxChan > idxMonth && idxOrders > idxChan && idxNote > idxOrders);
    assert.ok(idxDaily > idxNote, `expected daily section at bottom, got:\n${txt}`);
    assert.match(txt.slice(idxDaily), /구독자 <b>50명<\/b>/);
  } finally {
    globalThis.fetch = origFetch;
  }
});

test("sales state, source funnel, report funnel order, and flyer watch deep link conversion", async () => {
  const db = createMockD1();
  const tgCalls = [];
  const origFetch = globalThis.fetch;
  globalThis.fetch = async (url, opts = {}) => {
    const u = String(url);
    const body = opts.body ? JSON.parse(opts.body) : {};
    tgCalls.push({ u, body });
    if (u.includes("/reports/commission") || u.includes("/reports/orders")) {
      return new Response(JSON.stringify({ rCode: "0", data: [] }), { status: 200 });
    }
    return new Response(JSON.stringify({ ok: true, result: { message_id: 901 } }), { status: 200 });
  };

  try {
    const env = {
      DB: db,
      CLICK_TRACKER_API_KEY: "api-sec",
      TELEGRAM_BOT_TOKEN: "123:test",
      TELEGRAM_ADMIN_CHAT_ID: "999001",
      TELEGRAM_WEBHOOK_SECRET: "sec",
      COUPANG_ACCESS_KEY: "ak",
      COUPANG_SECRET_KEY: "sk",
    };

    // 1) Private D1 sales state
    const hashHex = "a".repeat(64);
    let res = await worker.fetch(
      new Request("https://hotpang-click.example.workers.dev/api/sales/state", {
        method: "POST",
        headers: { Authorization: "Bearer api-sec", "Content-Type": "application/json" },
        body: JSON.stringify({ month: "202611", month_milestone_step: 2, notified_order_hashes: [hashHex] }),
      }),
      env
    );
    assert.equal(res.status, 200);
    res = await worker.fetch(
      new Request("https://hotpang-click.example.workers.dev/api/sales/state", {
        headers: { Authorization: "Bearer api-sec" },
      }),
      env
    );
    const salesJson = await res.json();
    assert.deepEqual(salesJson.state.notified_order_hashes, [hashHex]);
    assert.equal(salesJson.state.month, "202611");
    assert.equal(salesJson.state.month_milestone_step, 2);

    // 2) Per-source funnel accumulation + /report placement
    const funnelDate = new Date(Date.now() + 9 * 3600 * 1000).toISOString().slice(0, 10);
    for (let i = 0; i < 2; i += 1) {
      const fRes = await worker.fetch(
        new Request("https://hotpang-click.example.workers.dev/api/channel/funnel", {
          method: "POST",
          headers: { Authorization: "Bearer api-sec", "Content-Type": "application/json" },
          body: JSON.stringify({
            date: funnelDate,
            sources: {
              ruliweb: { found: 5, new: 2, price_ok: 1, passed: i, reasons: { low_discount: 1 } },
              ppomppu: { found: 0, new: 0, price_ok: 0, passed: 0, blocked: 1, reasons: {} },
            },
          }),
        }),
        env
      );
      assert.equal(fRes.status, 200);
    }
    const gFunnel = await worker.fetch(
      new Request("https://hotpang-click.example.workers.dev/api/channel/funnel?days=1", {
        headers: { Authorization: "Bearer api-sec" },
      }),
      env
    );
    const gfJson = await gFunnel.json();
    assert.equal(gfJson.runs, 2);
    assert.equal(gfJson.sources.ruliweb.found, 10);
    assert.equal(gfJson.sources.ruliweb.passed, 1);
    assert.equal(gfJson.sources.ruliweb.reasons.low_discount, 2);

    await worker.fetch(
      new Request("https://hotpang-click.example.workers.dev/telegram/webhook", {
        method: "POST",
        headers: { "X-Telegram-Bot-Api-Secret-Token": "sec", "Content-Type": "application/json" },
        body: JSON.stringify({
          update_id: 201,
          message: { message_id: 2, chat: { id: 999001, type: "private" }, from: { id: 999001 }, text: "/report" },
        }),
      }),
      env
    );
    const repEdit = tgCalls.find((c) => c.u.endsWith("/editMessageText"));
    assert.ok(repEdit);
    const repTxt = repEdit.body.text;
    assert.ok(repTxt.indexOf("오늘 출처별 수집 퍼널") > 0);
    assert.ok(repTxt.indexOf("오늘 출처별 수집 퍼널") < repTxt.indexOf("※"));
    assert.match(repTxt, /차단 2회/);

    // 3) Flyer one-click watch card + conversion tracking
    const linkRes = await worker.fetch(
      new Request("https://hotpang-click.example.workers.dev/api/links", {
        method: "POST",
        headers: { Authorization: "Bearer api-sec", "Content-Type": "application/json" },
        body: JSON.stringify({
          target_url: "https://link.coupang.com/re/AFFSDP?lptag=AF123&pageKey=77777777",
          deal_id: "ppomppu_777",
          product_id: "77777777",
          source: "ppomppu",
          track: "A",
          title: "파워에이드 마운틴블라스트 900ml 12병",
          sale_price: 10970,
          discount_rate: 45,
          unit_info: "1L당 1,016원",
          shipping_label: "무료",
          option_key: "pid:77777777:900ml_12",
        }),
      }),
      env
    );
    const linkJson = await linkRes.json();
    assert.equal(linkJson.ok, true);

    tgCalls.length = 0;
    await worker.fetch(
      new Request("https://hotpang-click.example.workers.dev/telegram/webhook", {
        method: "POST",
        headers: { "X-Telegram-Bot-Api-Secret-Token": "sec", "Content-Type": "application/json" },
        body: JSON.stringify({
          update_id: 202,
          message: {
            message_id: 3,
            chat: { id: 555001, type: "private" },
            from: { id: 555001 },
            text: `/start w_${linkJson.code}`,
          },
        }),
      }),
      env
    );
    const watchSend = tgCalls.find((c) => c.u.endsWith("/sendMessage"));
    assert.ok(watchSend);
    assert.match(watchSend.body.text, /이 구성 목표가 알림 등록/);
    assert.match(watchSend.body.text, /1L당 1,016원/);
    const watchBtns = (watchSend.body.reply_markup?.inline_keyboard || []).flat();
    const watchBtn = watchBtns.find((b) => String(b.callback_data || "").startsWith("alert:watch:77777777:"));
    assert.ok(watchBtn);

    await worker.fetch(
      new Request("https://hotpang-click.example.workers.dev/telegram/webhook", {
        method: "POST",
        headers: { "X-Telegram-Bot-Api-Secret-Token": "sec", "Content-Type": "application/json" },
        body: JSON.stringify({
          update_id: 203,
          callback_query: {
            id: "cb-watch-1",
            from: { id: 555001 },
            message: { message_id: 901, chat: { id: 555001, type: "private" } },
            data: watchBtn.callback_data,
          },
        }),
      }),
      env
    );

    const aStatsRes = await worker.fetch(
      new Request("https://hotpang-click.example.workers.dev/api/alerts/stats", {
        headers: { Authorization: "Bearer api-sec" },
      }),
      env
    );
    const aStatsJson = await aStatsRes.json();
    assert.equal(aStatsJson.watch_metrics.opened, 1);
    assert.equal(aStatsJson.watch_metrics.registered, 1);
  } finally {
    globalThis.fetch = origFetch;
  }
});
