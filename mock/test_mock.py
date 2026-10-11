#!/usr/bin/env python3
"""Compare the mock against every entry in test-customers.json (status + body), plus error cases.

Usage:
  python3 -I test_mock.py [base_url] [path/to/test-customers.json]
  base_url defaults to http://localhost:8787
"""
import json
import os
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:8787"
SRC = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
    HERE, "..", "..", "Build the Builder - Final Case", "test-environment", "test-customers.json"
)
CALL = {"ani": "5000000000", "lang": "tr"}


def post(service, data):
    req = urllib.request.Request(
        f"{BASE}/voice/v1/{service}",
        data=json.dumps({"data": data, "call": CALL}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer mock", "User-Agent": "Mozilla/5.0 (mock check)"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8")
            return r.status, json.loads(raw), raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        return e.code, json.loads(raw), raw


def cases():
    doc = json.load(open(SRC, encoding="utf-8"))
    for cu in doc["customers"]:
        cno = cu["customerNo"]
        s = cu["services"]
        if "cards.list" in s:
            yield "cards.list", {"customerNo": cno, "aiFlow": "CardOps"}, s["cards.list"]
        if "contact.email.get" in s:
            yield "contact.email.get", {"customerNo": int(cno), "intAddressInfoType": "E"}, s["contact.email.get"]
        if "statement.pdf.send" in s:
            yield "statement.pdf.send", {
                "customerNo": cno, "cardNumber": "540000******0000", "currencyType": "TL",
                "statementDate": "05.10.2026", "emailAddress": "x@freyabank.example"}, s["statement.pdf.send"]
        for svc in ("cards.balance", "statement.week.get"):
            for card, e in s.get(svc, {}).items():
                yield svc, {"cardNumber": card}, e
        for svc in ("statement.week.decide", "statement.week.change"):
            for card, weeks in s.get(svc, {}).items():
                for w, e in weeks.items():
                    # alternate string / number week to test both forms
                    wk = int(w) if int(w) % 2 else w
                    yield svc, {"cardNumber": card, "newStatementWeek": wk, "statementDay": 5}, e


def check(c):
    svc, data, exp = c
    status, body, raw = post(svc, data)
    # exact: same status, same parsed body, and same JSON text (e.g. 0.0 stays 0.0)
    exact_text = json.dumps(exp["response"], ensure_ascii=False, separators=(",", ":"))
    ok = status == exp["http"] and body == exp["response"] and raw == exact_text
    return ok, svc, data, status, exp["http"]


def main():
    all_cases = list(cases())
    with ThreadPoolExecutor(16) as ex:
        results = list(ex.map(check, all_cases))
    bad = [r for r in results if not r[0]]
    print(f"test-customers.json entries: {len(results) - len(bad)} of {len(results)} matched (status + body, byte-exact JSON text)")
    for r in bad[:20]:
        print("MISMATCH", r[1:])

    # Error / edge cases: (service, data, expected status, expected code)
    edge = [
        ("cards.list", {"customerNo": "11111111"}, 404, "FB_IVR_CHANNEL_102"),
        ("cards.balance", {"cardNumber": "540000******9999"}, 404, "FB_IVR_CHANNEL_120"),
        ("cards.balance", {"cardNumber": ""}, 400, "INVALID_ARGUMENTS_ERROR"),
        ("statement.week.get", {"cardNumber": "540000******9999"}, 404, "FB_IVR_CHANNEL_121"),
        ("statement.week.decide", {"cardNumber": "540000******9999", "newStatementWeek": 2, "statementDay": 5}, 400, "INVALID_ARGUMENTS_ERROR"),
        ("statement.week.change", {"cardNumber": "540000******9999", "newStatementWeek": 2, "statementDay": 5}, 400, "INVALID_ARGUMENTS_ERROR"),
        ("contact.email.get", {"customerNo": "11111111", "intAddressInfoType": "E"}, 400, "INVALID_ARGUMENTS_ERROR"),
        ("contact.email.get", {"customerNo": "90000001", "intAddressInfoType": ""}, 400, "INVALID_ARGUMENTS_ERROR"),
        ("statement.pdf.send", {"customerNo": "94907600", "cardNumber": "540000******7211", "currencyType": "TL",
                                "statementDate": "05.10.2026", "emailAddress": "a@b.example"}, 404, "FB_IVR_CHANNEL_107"),
        ("cards.list", {"customerNo": "{{customerNumber}}"}, 200, "1"),
        ("cards.balance", {"cardNumber": "{{cardNumber}}"}, 200, "1"),
        ("statement.week.decide", {"cardNumber": "{{cardNumber}}", "newStatementWeek": "{{week}}", "statementDay": "{{day}}"}, 200, "1"),
        ("statement.pdf.send", {"customerNo": "{{customerNumber}}", "cardNumber": "{{c}}", "currencyType": "{{x}}",
                                "statementDate": "{{d}}", "emailAddress": "{{e}}"}, 200, "1"),
    ]
    eok = 0
    for svc, data, st, code in edge:
        status, body, _ = post(svc, data)
        good = status == st and body["result"]["code"] == code and ("data" in body) == (code == "1")
        eok += good
        if not good:
            print("EDGE FAIL", svc, data, status, body)
    print(f"error/edge cases: {eok} of {len(edge)} as expected")


if __name__ == "__main__":
    main()
