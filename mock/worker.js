// Freya Bank statement/debt services: mock for the voice assistant (Cloudflare Worker).
// Paths and shapes follow the 2026-10-01 service contract (sections 3-6).
// Test-customer answers come verbatim from test-customers.json via data.js (see build_data.py).
// Stateless: statement.week.change and statement.pdf.send do not change anything.

import { CUSTOMERS, CARDS } from "./data.js";

const BASE = "/voice/v1/";
const SERVICES = [
  "cards.list",
  "cards.balance",
  "statement.week.get",
  "statement.week.decide",
  "statement.week.change",
  "contact.email.get",
  "statement.pdf.send",
];

// Used when the panel's "Test API Request" sends unfilled "{{...}}" variables.
const TEMPLATE_CUSTOMER = "90000001";
const TEMPLATE_CARD = "540000******1234";

const CORS = { "Access-Control-Allow-Origin": "*" };

function json(body, status = 200) {
  return Response.json(body, { status, headers: CORS });
}

function guid() {
  return crypto.randomUUID().replace(/-/g, "");
}

function errorBody(code, message, type, rootCause) {
  const g = guid();
  return {
    result: {
      guid: g,
      returnCode: code,
      messageText: message,
      code: code,
      message: message,
      type: type,
      details: { guid: g, rootCause: rootCause, errorOrigin: "ivr-gateway-test:ivr-channel-v0" },
    },
  };
}

const NOT_FOUND = "com.freyabank.ivrgateway.ivrchannel.rest.exception.NotFoundException";

function invalidArgs(message) {
  return json(errorBody("INVALID_ARGUMENTS_ERROR", message, "ERROR", "InvalidArgumentsException"), 400);
}

// Contract section 4: the 404 of each service (400 where the service has no 404).
const NOT_FOUND_ERRORS = {
  "cards.list": () =>
    json(errorBody("FB_IVR_CHANNEL_102", "Müşteriye ait geçerli kart bilgisi bulunamadı.", "WARNING", NOT_FOUND), 404),
  "cards.balance": () =>
    json(errorBody("FB_IVR_CHANNEL_120", "Kart bakiye servisi hatası: kart bulunamadı", "WARNING", NOT_FOUND), 404),
  "statement.week.get": () =>
    json(errorBody("FB_IVR_CHANNEL_121", "Hesap kesim haftası servisi hatası: kart bulunamadı", "WARNING", NOT_FOUND), 404),
  "statement.pdf.send": () => json(errorBody("FB_IVR_CHANNEL_107", "Pdf dokümanı bulunamadı", "WARNING", NOT_FOUND), 404),
  "statement.week.decide": () => invalidArgs("no statement week preview for this card and week"),
  "statement.week.change": () => invalidArgs("no statement week change for this card and week"),
  "contact.email.get": () => invalidArgs("customer not found"),
};

function isTemplate(v) {
  return typeof v === "string" && (v.trim().startsWith("{{") || v.trim().startsWith("%7B%7B"));
}

function isEmpty(v) {
  return v === undefined || v === null || (typeof v === "string" && v.trim() === "");
}

function norm(v) {
  return String(v).trim();
}

async function serve(entry) {
  if (entry.delaySeconds) await new Promise((r) => setTimeout(r, entry.delaySeconds * 1000));
  // entry.body is the test file's response, pre-serialized (keeps 0.0 etc. exactly as listed).
  return new Response(entry.body, {
    status: entry.http,
    headers: { ...CORS, "Content-Type": "application/json; charset=utf-8" },
  });
}

// Required fields per service, with the contract's message style ("card number cannot be empty").
const REQUIRED = {
  "cards.list": [["customerNo", "customer number cannot be empty"]],
  "cards.balance": [["cardNumber", "card number cannot be empty"]],
  "statement.week.get": [["cardNumber", "card number cannot be empty"]],
  "statement.week.decide": [
    ["cardNumber", "card number cannot be empty"],
    ["newStatementWeek", "newStatementWeek cannot be empty"],
  ],
  "statement.week.change": [
    ["cardNumber", "card number cannot be empty"],
    ["newStatementWeek", "newStatementWeek cannot be empty"],
  ],
  "contact.email.get": [
    ["customerNo", "customer number cannot be empty"],
    ["intAddressInfoType", "intAddressInfoType cannot be empty"],
  ],
  "statement.pdf.send": [
    ["customerNo", "customer number cannot be empty"],
    ["cardNumber", "card number cannot be empty"],
    ["currencyType", "currencyType cannot be empty"],
    ["statementDate", "statementDate cannot be empty"],
    ["emailAddress", "emailAddress cannot be empty"],
  ],
};

async function handle(service, request) {
  let body;
  try {
    body = await request.json();
  } catch {
    return invalidArgs("request body must be JSON");
  }
  const data = body && typeof body === "object" && body.data && typeof body.data === "object" ? body.data : body || {};

  // Any unfilled "{{...}}" variable: answer with a valid record for a test customer.
  const templated = Object.values(data).some(isTemplate);

  if (!templated) {
    for (const [field, msg] of REQUIRED[service]) {
      if (isEmpty(data[field])) return invalidArgs(msg);
    }
  }

  const customerNo = isTemplate(data.customerNo) || isEmpty(data.customerNo) ? TEMPLATE_CUSTOMER : norm(data.customerNo);
  const cardNumber = isTemplate(data.cardNumber) || isEmpty(data.cardNumber) ? TEMPLATE_CARD : norm(data.cardNumber);

  let entry;
  switch (service) {
    case "cards.list":
    case "contact.email.get":
    case "statement.pdf.send":
      entry = CUSTOMERS[customerNo] && CUSTOMERS[customerNo][service];
      break;
    case "cards.balance":
    case "statement.week.get":
      entry = CARDS[cardNumber] && CARDS[cardNumber][service];
      break;
    case "statement.week.decide":
    case "statement.week.change": {
      const weeks = CARDS[cardNumber] && CARDS[cardNumber][service];
      if (weeks) {
        let week = isTemplate(data.newStatementWeek) || isEmpty(data.newStatementWeek) ? null : norm(data.newStatementWeek);
        if (week === null && templated) week = Object.keys(weeks).find((w) => weeks[w].http === 200) ?? null;
        entry = week !== null ? weeks[week] : undefined;
      }
      break;
    }
  }
  if (!entry) return NOT_FOUND_ERRORS[service]();
  return serve(entry);
}

function token() {
  // Contract section 2. The test environment does not check tokens; this is a fixed dummy value.
  return json({ access_token: "mock-access-token", token_type: "Bearer", expires_in: 1800 });
}

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "");
    if (request.method === "OPTIONS") {
      return new Response(null, {
        headers: { ...CORS, "Access-Control-Allow-Methods": "GET, POST, OPTIONS", "Access-Control-Allow-Headers": "*" },
      });
    }
    if (request.method === "GET" && (path === "/health" || path === "")) return json({ status: "ok" });
    if (request.method === "POST" && path === "/oauth2/token") return token();

    // Accept /voice/v1/<service> (contract path) and bare /<service> as an alias.
    const name = path.startsWith(BASE) ? path.slice(BASE.length) : path.slice(1);
    if (SERVICES.includes(name)) {
      if (request.method !== "POST") return json({ error: "method not allowed, use POST" }, 405);
      return handle(name, request);
    }
    return json(
      { error: "not found", paths: ["GET /health", "POST /oauth2/token", ...SERVICES.map((s) => "POST " + BASE + s)] },
      404
    );
  },
};
