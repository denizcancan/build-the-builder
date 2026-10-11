"""Run the workflow's code steps locally with fake call data, to check the date and amount logic.

    python3 builder/test_code.py out/gen-brief-a.workflow.json
"""
import json, sys

def code_of(wf, node):
    return wf["nodes"][node]["pre_actions"][0]["function"]["parameters"]["properties"]["code"]["default"]

def run(wf, node, v):
    exec(code_of(wf, node), {"vars": v})
    return v

def account(wf, now, products):
    v = {"now": now, "products": products}
    run(wf, "setup", v)
    run(wf, "account_facts", v)
    return v

if __name__ == "__main__":
    wf = json.load(open(sys.argv[1]))
    cases = json.load(open("public-test-set.json"))
    for c in cases:
        prods = [dict(p, product_id="p%d" % (i + 1)) for i, p in enumerate(c["products"])]
        v = account(wf, c["variables"]["now"], prods)
        dl = [v.get("deadline_%d" % (i + 1)) for i in range(len(prods))]
        print(c["id"], v["account_state"], "| deadlines:", dl, "| expected promises:", [(p["product_id"], p["date"]) for p in c["expected"]["promises"]])
    print("\n--- callback facts:\n" + v["callback_facts"])
    print("\n--- debt statement (last case):\n" + v["debt_statement"])
    print("\n--- deal facts (PUB-06):")
    c = next(x for x in cases if x["id"] == "PUB-06")
    print(account(wf, c["variables"]["now"], [dict(c["products"][0], product_id="p1")])["deal_facts"][:1500])

def wrap(wf, v):
    run(wf, "wrap_up", v)
    return {k: v.get(k) for k in ("final_outcome", "final_reason", "any_recorded", "report_line", "close_report", "svc_note_1", "svc_note_2", "svc_note_3")}

def wrap_scenarios(wf):
    base = account(wf, "2026-10-14T11:00", [{"product_id": "p1", "type": "credit_card", "card_last4": "4821", "past_due": 420, "days_late": 6, "minimum_payment": 126},
                                             {"product_id": "p2", "type": "personal_loan", "past_due": 640, "days_late": 9, "instalment_amount": 213},
                                             {"product_id": "p3", "type": "car_loan", "past_due": 75, "days_late": 3, "instalment_amount": 25}])
    ok = lambda k, a, d: {f"create_promise_{k}_response.status": "success", f"create_promise_{k}_response.data.ok": "true", f"create_promise_{k}_args.promise_amount_{k}": a, f"create_promise_{k}_args.promise_date_{k}": d}
    bad = lambda k: {f"create_promise_{k}_response.status": "success", f"create_promise_{k}_failure_count": 2}
    note = lambda k, r: {f"add_note_{k}_args.note_reason_{k}": r, f"add_note_{k}_response.data.ok": "true"}
    one = dict(base, product_count="1")
    S = {"1 product, promise ok": dict(one, **ok(1, 420, "2026-10-16")),
         "1 product, promise failed twice": dict(one, **bad(1)),
         "3 products: p1 ok, p2 p3 refused": dict(base, **ok(1, 510, "2026-10-15"), **note(2, "refused"), **note(3, "refused")),
         "3 products: all paid": dict(base, **note(1, "paid"), **note(2, "paid"), **note(3, "paid")),
         "3 products: p1 paid, p2 dispute, p3 refused": dict(base, **note(1, "paid"), **note(2, "dispute"), **note(3, "refused")),
         "3 products: p1 failed, p2 refused, p3 paid": dict(base, **bad(1), **note(2, "refused"), **note(3, "paid"))}
    for name, v in S.items():
        print(f"{name:45} ->", json.dumps(wrap(wf, dict(v))))
