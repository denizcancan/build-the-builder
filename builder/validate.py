"""Check a spec against the tool API file and run the generated code steps on sample data.

    python3 builder/validate.py <spec.json> <openapi.json>

Prints OK, or one problem per line. The builder's model fixes the spec until it prints OK.
"""
import datetime, json, pathlib, re, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from generate import generate

STAGES = {"opening", "identity", "negotiate", "after_promise"}
STAGE_RULES = {"opening": "opening_rules", "identity": "identity_rules", "negotiate": "negotiate_rules", "after_promise": "after_promise_rules"}


def enum(api, tool, field, where="request"):
    try:
        if where == "request":
            sch = api["paths"]["/" + tool]["post"]["requestBody"]["content"]["application/json"]["schema"]
        else:
            sch = api["paths"]["/" + tool]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
        return sch["properties"][field].get("enum")
    except KeyError:
        return None


def response_fields(api, tool):
    try:
        return set(api["paths"]["/" + tool]["post"]["responses"]["200"]["content"]["application/json"]["schema"]["properties"])
    except KeyError:
        return set()


def product_schema(api):
    try:
        p = api["paths"]["/get_account"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]["properties"]
        for v in p.values():
            if v.get("type") == "array":
                return v["items"]["properties"]
    except KeyError:
        pass
    return {}


def check(spec, api):
    bad = []
    need = ["persona", "address", "full_name", "opening", "opening_rules", "identity", "identity_rules", "callback", "calendar",
            "account", "bands", "attempts", "negotiate_rules", "promise", "transfers", "endings", "notes"]
    bad += ["missing field: " + k for k in need if k not in spec]
    if bad:
        return bad
    tf = {"verify_identity": {"field": "field", "answer": "answer"}, "create_promise": {"product": "product_id", "amount": "amount", "date": "date"},
          "add_note": {"product": "product_id", "reason": "reason"}, "schedule_callback": {"date": "date", "time": "time"},
          "transfer_to_agent": {"queue": "queue"}, "close_call": {"outcome": "outcome", "reason": "reason"}}
    for t, m in (spec.get("tool_fields") or {}).items():
        tf.setdefault(t, {}).update(m)
    for tool in ("verify_identity", "get_account", "create_promise", "add_note", "schedule_callback", "transfer_to_agent", "close_call"):
        if "/" + tool not in api.get("paths", {}):
            bad.append("openapi.json has no tool " + tool)
    # tool fields exist and required fields are covered
    for tool, m in tf.items():
        try:
            sch = api["paths"]["/" + tool]["post"]["requestBody"]["content"]["application/json"]["schema"]
        except KeyError:
            continue
        props = sch.get("properties", {})
        for role, f in m.items():
            if f not in props:
                bad.append(f"{tool}: request field '{f}' (for {role}) is not in openapi.json; set tool_fields")
        covered = set(m.values()) | set(spec.get("call_variables", [])) | {spec.get("customer_id_variable", "customer_id")} | set((spec.get("tool_constants") or {}).get(tool, {}))
        for r in sch.get("required", []):
            if r not in covered:
                bad.append(f"{tool}: required field '{r}' is not filled; add it to tool_fields or tool_constants")
    # enums
    outcomes, reasons = enum(api, "close_call", tf["close_call"]["outcome"]), enum(api, "close_call", tf["close_call"]["reason"])
    for k, e in spec["endings"].items():
        if outcomes and e.get("outcome") not in outcomes:
            bad.append(f"ending {k}: outcome '{e.get('outcome')}' is not one of {outcomes}")
        if reasons and e.get("reason") and e["reason"] not in reasons:
            bad.append(f"ending {k}: reason '{e['reason']}' is not one of {reasons}")
        if not e.get("line"):
            bad.append(f"ending {k}: no goodbye line")
        for st in e.get("stages", []):
            if st not in STAGES:
                bad.append(f"ending {k}: unknown stage {st}")
        if e.get("stages") and not e.get("when"):
            bad.append(f"ending {k}: has stages but no 'when'")
    for k in ("promise", "service_error", "id_failed", "callback"):
        if k not in spec["endings"]:
            bad.append("missing required ending: " + k)
    queues = enum(api, "transfer_to_agent", tf["transfer_to_agent"]["queue"])
    for q, v in spec["transfers"].items():
        if queues and q not in queues:
            bad.append(f"transfer queue '{q}' is not one of {queues}")
        if v.get("ending") not in spec["endings"]:
            bad.append(f"transfer {q}: ending '{v.get('ending')}' does not exist")
        if not v.get("when"):
            bad.append(f"transfer {q}: no 'when'")
    if queues:
        for q in queues:
            if q not in spec["transfers"]:
                bad.append(f"openapi queue '{q}' has no entry in transfers")
    fields = enum(api, "verify_identity", tf["verify_identity"]["field"])
    for q in spec["identity"].get("questions", []):
        if fields and q.get("field") not in fields:
            bad.append(f"identity question field '{q.get('field')}' is not one of {fields}")
        for k in ("field", "ask", "format"):
            if not q.get(k):
                bad.append(f"identity question {q.get('field')}: missing {k}")
    rf = response_fields(api, "verify_identity")
    for role, name in (spec["identity"].get("result") or {}).items():
        if rf and name and name not in rf:
            bad.append(f"identity.result.{role} = '{name}' is not a response field of verify_identity ({sorted(rf)})")
    for h, txt in (spec["identity"].get("hints") or {}).items():
        if "{{" in str(txt):
            bad.append(f"identity.hints.{h}: no {{{{variables}}}} in hints, write it without names")
    hints = enum(api, "verify_identity", (spec["identity"].get("result") or {}).get("hint", "hint"), "response") or []
    for h in hints:
        if h not in (spec["identity"].get("hints") or {}):
            bad.append(f"verify_identity hint '{h}' has no entry in identity.hints")
    notes = spec.get("notes") or {}
    if notes.get("enabled"):
        nr = enum(api, "add_note", tf["add_note"]["reason"])
        for r in notes.get("reasons", []):
            if nr and r not in nr:
                bad.append(f"note reason '{r}' is not one of {nr}")
        for r, e in (notes.get("reason_endings") or {}).items():
            if e not in spec["endings"]:
                bad.append(f"notes.reason_endings: ending '{e}' does not exist")
        if notes.get("all_paid_ending") and notes["all_paid_ending"] not in spec["endings"]:
            bad.append("notes.all_paid_ending does not exist")
        if not any("[[wrap_up]]" in r for r in spec.get("negotiate_rules", [])):
            bad.append("notes are enabled but no negotiate rule says when to [[wrap_up]]")
    if spec.get("fallback_ending", "service_error") not in spec["endings"]:
        bad.append("fallback_ending does not exist")
    # product types and fields
    ps = product_schema(api)
    acc = spec["account"]
    for role, f in acc.get("fields", {}).items():
        if ps and f not in ps:
            bad.append(f"account.fields.{role} = '{f}' is not a product field of get_account ({sorted(ps)})")
    types = (ps.get(acc.get("fields", {}).get("type", "type")) or {}).get("enum") or []
    for t in types:
        if t not in acc.get("types", {}):
            bad.append(f"product type '{t}' has no entry in account.types")
    for t, v in acc.get("types", {}).items():
        if v.get("floor_field") and ps and v["floor_field"] not in ps:
            bad.append(f"account.types.{t}.floor_field '{v['floor_field']}' is not a product field")
        for f in re.findall(r"\{(\w+)\}", v.get("words", "")):
            if ps and f not in ps:
                bad.append(f"account.types.{t}.words uses unknown field {{{f}}}")
    # calendar, bands
    for h in spec["calendar"].get("holidays", []):
        try:
            datetime.date.fromisoformat(h)
        except Exception:
            bad.append(f"holiday '{h}' is not YYYY-MM-DD")
    if spec["calendar"].get("non_business_date", "next") not in ("next", "previous"):
        bad.append("calendar.non_business_date must be next or previous")
    for b in spec["bands"]:
        if (b.get("deadline") or {}).get("if_not_business_day", "previous") not in ("next", "previous"):
            bad.append(f"band {b.get('name')}: deadline.if_not_business_day must be next or previous")
        if (b.get("deadline") or {}).get("unit") not in ("business_days", "calendar_days"):
            bad.append(f"band {b.get('name')}: deadline unit must be business_days or calendar_days")
    for a in spec["attempts"]:
        if a.get("amount") not in ("full", "floor", "full_or_floor") or a.get("date") not in ("today", "next_business_day", "end_of_week", "deadline"):
            bad.append(f"attempt {a} uses an unknown amount or date")
    # references in rules, per stage
    avail = {st: {"end:" + k for k, e in spec["endings"].items() if st in e.get("stages", [])} | {"transfer:" + q for q, v in spec["transfers"].items() if st in v.get("stages", [])} for st in STAGES}
    for st, key in STAGE_RULES.items():
        for r in spec.get(key, []):
            for ref in re.findall(r"\[\[([^\]]+)\]\]", r):
                if ref.startswith(("end:", "transfer:")) and ref not in avail[st]:
                    bad.append(f"{key}: [[{ref}]] is not available in stage {st} (add the stage to that ending or transfer)")
                if ref == "note" and not notes.get("enabled"):
                    bad.append(f"{key}: [[note]] used but notes are disabled")
    for r in spec.get("global_rules", []):
        for ref in re.findall(r"\[\[([^\]]+)\]\]", r):
            if ref.startswith(("end:", "transfer:")):
                bad.append(f"global_rules: [[{ref}]] is not allowed in a global rule; put it in the stage rules")
    early = {"opening.line": spec["opening"].get("line", ""), "identity.first_line": spec["identity"].get("first_line", ""),
             "account.statement_intro": acc.get("statement_intro", ""), "account.statement_product": acc.get("statement_product", ""),
             "account.first_ask_one": acc.get("first_ask_one", ""), "account.first_ask_many": acc.get("first_ask_many", "")}
    early.update({f"identity.questions[{q.get('field')}].ask": q.get("ask", "") for q in spec["identity"].get("questions", [])})
    for k, txt in early.items():
        if re.search(r"\brecord(ed|s)?\b", txt or "", re.I):
            bad.append(f"{k} says 'record...' before any promise exists; the judge reads that as a false claim. Say 'we are recording this call' style wording without 'recorded'")
    if bad:
        return bad
    # generate and run the code steps
    try:
        wf = generate(spec, api)
    except AssertionError as e:
        return ["generator: " + str(e)]
    def run(node, v):
        exec(wf["nodes"][node]["pre_actions"][0]["function"]["parameters"]["properties"]["code"]["default"], {"vars": v})
    sample = {}
    for t, v in acc.get("types", {}).items():
        p = {acc["fields"]["id"]: "p1", acc["fields"]["type"]: t, acc["fields"]["past_due"]: 500.0, acc["fields"]["days_late"]: 10}
        for f in re.findall(r"\{(\w+)\}", v.get("words", "")):
            p[f] = "1234"
        if v.get("floor_field"):
            p[v["floor_field"]] = 150.0
        sample[t] = p
    v = {spec.get("now_variable", "now"): "2026-10-14T11:00", "products": list(sample.values())[:spec.get("max_products", 3)]}
    run("setup", v)
    run("account_facts", v)
    if v.get("account_state") != "ok":
        bad.append("account step failed on sample products: " + str(v.get("deal_facts")))
    for b in spec["bands"]:
        lo = b.get("min_days") or 1
        v2 = {spec.get("now_variable", "now"): "2026-10-14T11:00", "products": [dict(list(sample.values())[0], **{acc["fields"]["days_late"]: lo})]}
        run("setup", v2); run("account_facts", v2)
        if v2.get("account_state") != "ok":
            bad.append(f"account step failed for band {b.get('name')}")
    for k, n in wf["nodes"].items():
        for txt in (n.get("task_message", ""), (n.get("on_enter") or {}).get("text", "")):
            if "[[" in txt:
                bad.append(f"step {k}: unresolved reference in text")
    return bad


if __name__ == "__main__":
    spec, api = json.load(open(sys.argv[1])), json.load(open(sys.argv[2]))
    problems = check(spec, api)
    print("OK" if not problems else "\n".join(problems))
    sys.exit(1 if problems else 0)
