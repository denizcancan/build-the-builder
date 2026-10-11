"""Turn a spec into a Freya workflow.

    python3 builder/generate.py builder/specs/brief-a.spec.json tools/openapi.json out/brief-a.workflow.json

The spec (see builder/SPEC.md) holds everything that changes from one bank brief to the next: wording, rules per
stage, identity questions, calendar, bands, outcome table, queues. This file holds what stays the same in every
collections call: the stages, how tools are wired, the endings, retries and the date arithmetic.

Design rules learned in practice runs:
- Tools that write to the bank carry no Python hooks. Hooks run on Freya's code sandbox; when it is unreachable a
  pre-hook failure stops the request and a post-hook failure loses the answer, so the agent repeats the write.
- close_call is never called by the dialogue model. Every ending is an api_request step with outcome and reason
  baked in, reached by a deterministic route or by a transition tool the model calls.
- Promises and notes use one tool per product with the product id baked in, so each product has its own result
  variables. What was recorded is computed in code and said by a fixed line, never claimed by the model.
- Code steps retry when the sandbox fails.
"""
import json, os, pathlib, re, sys

MAX_PRODUCTS = 3

# ---------------------------------------------------------------- small helpers

def hexcfg(obj):
    """Embed data in sandbox code without braces: '{{' or '}}' in code breaks the engine's templating."""
    return "json.loads(bytes.fromhex('" + json.dumps(obj).encode().hex() + "').decode())"

def var_true(v):
    return [{"variable": v, "operator": "equals", "operand": "true"}, {"variable": v, "operator": "equals", "operand": "True"}]

def var_is(v, value):
    return [{"variable": v, "operator": "equals", "operand": value}]

def go(target, desc, conds):
    return {"target_node": target, "description": desc, "condition": {"logic": "or", "conditions": conds}}

def always(target):
    return {"target_node": target, "description": "always", "condition": {"operator": "always"}}

def by_model(target, when):
    """A transition the dialogue model triggers by calling the tool transition_to_<target>."""
    return {"target_node": target, "description": when, "is_tool_call": True,
            "condition": {"operator": "matches_intent", "description": when}}

def check_code(code):
    assert "{{" not in code and "}}" not in code, "code must not contain double braces"
    compile(code, "snippet", "exec")
    return code

# ---------------------------------------------------------------- sandbox code

DATE_LIB = r'''
import datetime, json, re
TD = datetime.timedelta
def fill(t):
    # call variables inside a line written by this code are not filled in by the engine, so fill them here
    return re.sub(r'\{\{\s*([\w.]+)\s*\}\}', lambda m: str(vars.get(m.group(1)) or ''), str(t))
WD = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
MO = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December']
H = set(CFG.get('holidays') or [])
BW = set(CFG.get('business_weekdays') or [0, 1, 2, 3, 4])
def isbd(d):
    return d.weekday() in BW and d.isoformat() not in H
def nextbd(d):
    k = 0
    while not isbd(d) and k < 60:
        d = d + TD(days=1)
        k = k + 1
    return d
def prevbd(d):
    k = 0
    while not isbd(d) and k < 60:
        d = d - TD(days=1)
        k = k + 1
    return d
def addbd(d, n):
    k = 0
    while k < n:
        d = d + TD(days=1)
        if isbd(d):
            k = k + 1
    return d
def say(d):
    return WD[d.weekday()] + ' ' + str(d.day) + ' ' + MO[d.month - 1] + ' ' + str(d.year)
def sayiso(d):
    return say(d) + ' (' + d.isoformat() + ')'
def money(x):
    try:
        return CFG.get('money_format', '{:,.2f}').format(float(x))
    except Exception:
        return str(x)
raw = str(vars.get(CFG.get('now_var', 'now')) or '')
try:
    nowdt = datetime.datetime.fromisoformat(raw.replace('Z', '')[:16])
except Exception:
    try:
        nowdt = datetime.datetime.fromisoformat(raw[:10] + 'T12:00')
    except Exception:
        nowdt = datetime.datetime.now()
today = nowdt.date()
'''

SETUP_CODE = r'''
try:
    vars['today_say'] = say(today)
    vars['today_iso'] = today.isoformat()
    cb = CFG.get('callback') or dict()
    start = str(cb.get('start') or '10:00')
    end = str(cb.get('end') or '19:00')
    hm = nowdt.strftime('%H:%M')
    lines = []
    if cb.get('today', True) and isbd(today) and hm < end:
        if cb.get('after_now', True) and hm >= start:
            lines.append('today, ' + sayiso(today) + ', at a time later than ' + hm + ' and up to ' + end)
        else:
            lines.append('today, ' + sayiso(today) + ', from ' + start + ' to ' + end)
    d = today
    for i in range(int(cb.get('next_business_days', 1))):
        d = addbd(d, 1)
        lines.append(sayiso(d) + ', from ' + start + ' to ' + end)
    words = []
    for i, w in enumerate(['today', 'tomorrow', 'the day after tomorrow']):
        d = today + TD(days=i)
        words.append('"' + w + '" = ' + sayiso(d))
    vars['callback_facts'] = ('Allowed callback times (both ends included): ' + '; or '.join(lines) + '. Any other day or time is outside the window. '
        + 'Dates: ' + ', '.join(words) + '. Work out the date of a relative day yourself; never ask the person for a date they already gave as a relative day. '
        + 'Times: "eleven in the morning" or "around eleven" = 11:00, "three in the afternoon" = 15:00.')
except Exception as e:
    vars['today_say'] = raw
    vars['today_iso'] = raw[:10]
    vars['callback_facts'] = 'Allowed callback times: see the rules.'
'''

ACCOUNT_CODE = r'''
def get_products():
    P = vars.get('products')
    if isinstance(P, str):
        try:
            P = json.loads(P)
        except Exception:
            try:
                P = json.loads(P.replace("'", '"').replace('True', 'true').replace('False', 'false').replace('None', 'null'))
            except Exception:
                P = None
    if isinstance(P, dict):
        P = P.get('products') or [P]
    if isinstance(P, list) and P:
        return P
    found = dict()
    for k in list(vars.keys()):
        m = re.search(r'products[.\[](\d+)\]?\.(\w+)$', str(k))
        if m:
            found.setdefault(int(m.group(1)), dict())[m.group(2)] = vars.get(k)
    return [found[i] for i in sorted(found)]
def num(x):
    try:
        return float(str(x).replace(',', ''))
    except Exception:
        return None
def end_of_week(d):
    mon = d - TD(days=d.weekday())
    wk = [mon + TD(days=i) for i in range(7) if isbd(mon + TD(days=i))]
    if wk and max(wk) > d:
        return max(wk)
    nmon = mon + TD(days=7)
    wk2 = [nmon + TD(days=i) for i in range(7) if isbd(nmon + TD(days=i))]
    return max(wk2) if wk2 else nextbd(d + TD(days=1))
def deadline_for(band):
    dl = band.get('deadline') or dict()
    n = int(dl.get('count', 0))
    if dl.get('unit') == 'calendar_days':
        d = today + TD(days=n)
        return nextbd(d) if dl.get('if_not_business_day') == 'next' else prevbd(d)
    return addbd(today, n)
def band_for(days):
    for b in CFG.get('bands') or []:
        lo = b.get('min_days')
        hi = b.get('max_days')
        if (lo is None or days >= lo) and (hi is None or days <= hi):
            return b
    bs = CFG.get('bands') or [dict(name='default', deadline=dict(count=5, unit='business_days'), attempts=1)]
    return bs[-1]
def pick_date(kind, deadline):
    if kind == 'today':
        d = today
    elif kind == 'next_business_day':
        d = addbd(today, 1)
    elif kind == 'end_of_week':
        d = end_of_week(today)
    else:
        d = deadline
    return min(d, deadline)
try:
    F = CFG['fields']
    allp = get_products()
    prods = allp[:CFG.get('max_products', 3)]
    vars['products_not_handled'] = str(len(allp) - len(prods))
    if not prods:
        raise ValueError('no products')
    n = len(prods)
    blocks = []
    say_lines = []
    deadlines = []
    latest = today
    for i, p in enumerate(prods):
        k = str(i + 1)
        ptype = str(p.get(F['type']))
        T = (CFG.get('types') or dict()).get(ptype) or dict()
        try:
            words = str(T.get('words') or ptype.replace('_', ' ')).format(**p)
        except Exception:
            words = ptype.replace('_', ' ')
        full = num(p.get(F['past_due']))
        days = int(num(p.get(F['days_late'])) or 0)
        floor = num(p.get(T.get('floor_field'))) if T.get('floor_field') else None
        if floor is None:
            floor = full
        fname = str(T.get('floor_name') or 'minimum amount')
        band = band_for(days)
        deadline = deadline_for(band)
        latest = max(latest, deadline)
        deadlines.append(deadline)
        attempts = int(band.get('attempts', 1))
        vars['pid_' + k] = str(p.get(F['id']))
        vars['product_words_' + k] = words
        vars['full_' + k] = money(full)
        vars['floor_' + k] = money(floor)
        vars['deadline_' + k] = say(deadline)
        vars['deadline_iso_' + k] = deadline.isoformat()
        say_lines.append(str(CFG.get('statement_product') or 'Your {words} is {days_late} days late, with a past-due amount of {past_due}.').format(words=words, days_late=days, past_due=money(full), floor=money(floor), floor_name=fname, band=band.get('name', ''), deadline=say(deadline)))
        b = []
        b.append('PRODUCT ' + k + ': ' + words + ' (tools create_promise_' + k + (' and add_note_' + k if CFG.get('notes') else '') + ').')
        b.append('  Full past-due amount: ' + money(full) + '. Days late: ' + str(days) + '. Band: ' + str(band.get('name', '')) + '.')
        b.append('  Floor (' + fname + '): ' + money(floor) + '. An amount from ' + money(floor) + ' up to ' + money(full) + ', both included, can be recorded. Below ' + money(floor) + ' cannot.')
        b.append('  Deadline (latest promise date): ' + sayiso(deadline) + '. Persuasion attempts allowed: ' + str(attempts) + '.')
        b.append('  No date after ' + sayiso(deadline) + ' for this product, also when the customer wants one date for all products: then say the latest date for this product is ' + say(deadline) + '.')
        if attempts == 1:
            b.append('  ONLY ONE OFFER for this product (attempt 1 below). Other products may allow more; this one does not.')
        offers = CFG.get('attempts') or []
        for j in range(attempts):
            if j < len(offers):
                o = offers[j]
                am = o.get('amount', 'full')
                if am == 'floor':
                    amt = 'the ' + fname + ' ' + money(floor)
                elif am == 'full_or_floor':
                    amt = 'the full past-due amount ' + money(full) + ' (or the ' + fname + ' ' + money(floor) + ' if the customer already said the full amount is too much)'
                else:
                    amt = 'the full past-due amount ' + money(full)
                b.append('  Attempt ' + str(j + 1) + ' offer: ' + amt + ' on ' + sayiso(pick_date(o.get('date', 'deadline'), deadline)) + '.')
        b.append('  After a flat refusal of attempt ' + str(attempts) + ' (the last one for this product): make no other offer for this product, not even the floor. ' + ('Then call add_note_' + k + ' with the refusal reason and go on with the next product.' if CFG.get('notes') else 'Then end the call as a refusal.'))
        if CFG.get('floor_offer'):
            b.append('  When you offer the floor, say: "' + str(CFG['floor_offer']).format(floor=money(floor), floor_name=fname, full=money(full), words=words) + '"')
        floor_in = [str(j + 1) for j, o in enumerate(offers[:attempts]) if o.get('amount', 'full') == 'floor']
        b.append('  Offer the floor ' + money(floor) + ' only when the customer said the full amount is too much (once), or as the attempt offer above that names it' + (' (attempt ' + ', '.join(floor_in) + ')' if floor_in else '') + '. "I will not pay" is a flat refusal, not "too much": it gets the next attempt offer, or none if the attempts are used up.' + (' A transfer comes before the floor: ' + '; '.join(CFG['transfer_whens']) + '. If the customer asks for that, even in the same message as "too much", follow the transfer rule and offer no floor.' if CFG.get('transfer_whens') else ''))
        b.append('  The floor is offered once, in one sentence together with the date of the attempt you are on (never first for today and then again for another day).')
        b.append('  Every offer above is said ONCE. Until the customer answers it, do not say its amount or date again: to ask again, say only "Is that possible for you?".')
        blocks.append('\n'.join(b))
    cal = []
    d = today
    while d <= latest + TD(days=10):
        if isbd(d):
            lab = 'business day' + (' (today)' if d == today else '')
        else:
            why = 'bank holiday' if d.isoformat() in H else 'weekend'
            if CFG.get('non_business_date') == 'previous' and prevbd(d) >= today:
                lab = why + ', not allowed. Offer the business day before it: ' + say(prevbd(d))
            else:
                lab = why + ', not allowed. Next business day: ' + say(nextbd(d))
        late = [str(i + 1) for i, dl in enumerate(deadlines) if d > dl]
        if late and isbd(d) and len(late) < len(deadlines):
            lab = lab + '. Allowed for product ' + ' and '.join(str(i + 1) for i, dl in enumerate(deadlines) if d <= dl)
        if late:
            lab = lab + '. AFTER the deadline of product ' + ' and '.join(late) + ': ' + '; '.join('use ' + say(deadlines[int(j) - 1]) + ' for product ' + j for j in late)
        cal.append('- ' + d.isoformat() + ' ' + WD[d.weekday()] + ' ' + str(d.day) + ' ' + MO[d.month - 1] + ': ' + lab)
        d = d + TD(days=1)
    mon = today - TD(days=today.weekday())
    nmon = mon + TD(days=7)
    rel = []
    for i in range(7):
        t = mon + TD(days=i)
        if CFG.get('next_weekday') == 'next_occurrence':
            nx = today + TD(days=((i - today.weekday()) % 7) or 7)
        else:
            nx = nmon + TD(days=i)
        rel.append('- "this ' + WD[i] + '" = ' + t.isoformat() + (' (already past)' if t < today else '') + '; "next ' + WD[i] + '" = ' + nx.isoformat())
    hol = [say(datetime.date.fromisoformat(h)) for h in sorted(H) if today <= datetime.date.fromisoformat(h) <= latest + TD(days=10)]
    facts = ['Today: ' + sayiso(today) + '. Products: ' + str(n) + ', in this order.']
    if len(allp) > n:
        facts.append('The account has ' + str(len(allp) - n) + ' more late product(s) that this call cannot handle. Do not discuss them; if the customer asks, say that you do not have that information.')
    if hol:
        facts.append('BANK HOLIDAYS, never allowed as a payment date: ' + '; '.join(hol) + '. Weekends are never allowed either.')
    facts = facts + blocks
    facts.append('Words for "this/next <weekday>":')
    facts.extend(rel)
    facts.append('CALENDAR (look up every date the customer names; a date after a product deadline is not allowed for that product):')
    facts.extend(cal)
    vars['deal_facts'] = fill('\n'.join(facts))
    vars['product_count'] = str(n)
    ask = CFG.get('first_ask_one') if n == 1 else CFG.get('first_ask_many')
    vars['debt_statement'] = fill(' '.join([str(CFG.get('statement_intro') or '')] + say_lines + [str(ask or '')]).strip())
    vars['account_state'] = 'ok'
except Exception as e:
    vars['account_state'] = 'error'
    vars['product_count'] = '0'
    vars['deal_facts'] = 'Account details are not available: ' + str(e)
    vars['debt_statement'] = ''
'''

WRAP_CODE = r'''
def truth(x):
    return str(x).strip().lower() == 'true'
try:
    n = int(str(vars.get('product_count') or '1'))
    recorded = []
    failed = []
    notes = []
    report = []
    for i in range(1, n + 1):
        k = str(i)
        words = str(vars.get('product_words_' + k) or ('product ' + k))
        ok = truth(vars.get('create_promise_' + k + '_response.data.ok')) or truth(vars.get('retry_ok_' + k))
        tried = vars.get('create_promise_' + k + '_response.status') is not None or vars.get('create_promise_' + k + '_failure_count') is not None or vars.get('create_promise_' + k + '_success_count') is not None
        if ok:
            recorded.append(k)
            amt = vars.get('create_promise_' + k + '_args.promise_amount_' + k)
            dt = vars.get('create_promise_' + k + '_args.promise_date_' + k)
            try:
                dts = say(datetime.date.fromisoformat(str(dt)[:10]))
            except Exception:
                dts = str(dt)
            report.append(CFG['recorded_sentence'].format(words=words, amount=money(amt), date=dts))
            notes.append('')
            continue
        if tried:
            failed.append(k)
            report.append(CFG['failed_sentence'].format(words=words))
        nr = vars.get('add_note_' + k + '_args.note_reason_' + k)
        nok = truth(vars.get('add_note_' + k + '_response.data.ok'))
        if nr and nok:
            notes.append(str(nr))
        elif tried and CFG.get('notes'):
            vars['svc_note_' + k] = 'yes'
            notes.append(CFG.get('service_error_note') or 'service_error')
        else:
            notes.append(str(nr or ''))
    if recorded:
        report.append(CFG['after_record_sentence'])
        end = CFG['endings']['promise']
    elif CFG.get('notes') and [x for x in notes if x]:
        given = [x for x in notes if x]
        if all(x == CFG.get('paid_note') for x in given) and len(given) == n:
            end = CFG['endings']['all_paid']
        else:
            first = [x for x in given if x != CFG.get('paid_note')]
            end = CFG['note_endings'].get(first[0] if first else given[0]) or CFG['endings']['service_error']
    elif failed:
        end = CFG['endings']['service_error']
    else:
        end = CFG['endings']['fallback']
    E = CFG['ending_table'][end]
    vars['final_outcome'] = E['outcome']
    if E.get('reason'):
        vars['final_reason'] = E['reason']
    vars['final_line'] = fill(E['line'])
    vars['report_line'] = fill(' '.join(report))
    vars['any_recorded'] = 'yes' if recorded else 'no'
    vars['close_report'] = '' if recorded or not report else ' '.join(report) + ' '
except Exception as e:
    E = CFG['ending_table'][CFG['endings']['service_error']]
    vars['final_outcome'] = E['outcome']
    if E.get('reason'):
        vars['final_reason'] = E['reason']
    vars['final_line'] = fill(E['line'])
    vars['report_line'] = ''
    vars['any_recorded'] = 'no'
    vars['close_report'] = ''
'''

VERIFY_POST = r'''
try:
    if isinstance(result, dict):
        result.pop('header', None)
        d = result.get('data')
        if not isinstance(d, dict):
            d = dict()
            result['data'] = d
        R = CFG['result']
        def t(x):
            return x is True or str(x).lower() == 'true'
        left = d.get(R.get('left'))
        try:
            left = int(left)
        except Exception:
            left = None
        hint = d.get(R.get('hint')) if R.get('hint') else None
        if t(d.get(R.get('all_passed'))):
            w = 'All security answers are verified.'
        elif hint and hint in CFG.get('hints', dict()):
            w = CFG['hints'][hint]
        elif t(d.get(R.get('passed'))):
            w = 'This answer is correct. Now ask the next security question that has not passed yet, then call verify_identity again.'
        elif left == 0:
            w = 'No wrong answers are left. The identity check has ended. The call now ends by itself.'
        elif d.get(R.get('passed')) is not None:
            w = 'This answer does not match our records. Say so and ask the same question again. Wrong answers left: ' + str(left) + '.'
        else:
            w = 'The answer could not be checked (' + str(d.get('error') or '') + '). It does not count as a wrong answer. Ask the customer to say it again, then call verify_identity again.'
        d['what_to_do_next'] = w
except Exception:
    pass
'''

GATE = True   # promises can only be saved after an extracted yes to a read-back


def code_with(cfg, *parts):
    return check_code("import json\nCFG = " + hexcfg(cfg) + "\n" + "\n".join(parts))

# ---------------------------------------------------------------- the generator

class Builder:
    def __init__(self, spec, api):
        self.s, self.api = spec, api
        self.base = api["servers"][0]["url"].rstrip("/")
        self.cid = spec.get("customer_id_variable", "customer_id")
        self.f = {"verify_identity": {"field": "field", "answer": "answer"},
                  "create_promise": {"product": "product_id", "amount": "amount", "date": "date"},
                  "add_note": {"product": "product_id", "reason": "reason"},
                  "schedule_callback": {"date": "date", "time": "time"},
                  "transfer_to_agent": {"queue": "queue"},
                  "close_call": {"outcome": "outcome", "reason": "reason"}}
        for tool, m in (spec.get("tool_fields") or {}).items():
            self.f.setdefault(tool, {}).update(m)
        self.endings = spec["endings"]
        self.notes = bool((spec.get("notes") or {}).get("enabled"))
        self.used_endings = set()

    # -- tools
    def schema(self, tool):
        return self.api["paths"]["/" + tool]["post"]["requestBody"]["content"]["application/json"]["schema"]

    def tool(self, tool, desc, fields, fname=None, retries=0, post=None):
        """fields: request field -> {'default': value} (baked) or {'var': name, 'description': ...} (model fills)."""
        sch = self.schema(tool)
        consts = (self.s.get("tool_constants") or {}).get(tool, {})
        props, req = {}, []
        for f, spec in sch.get("properties", {}).items():
            if f in fields:
                o = fields[f]
                leaf = {"type": spec.get("type", "string"), "default": o["default"] if "default" in o else "{{%s}}" % o["var"]}
                if o.get("description"): leaf["description"] = o["description"]
                enum = o.get("enum", spec.get("enum"))
                if enum and "var" in o: leaf["enum"] = enum
            elif f == self.cid or f in self.s.get("call_variables", []):
                leaf = {"type": spec.get("type", "string"), "default": "{{%s}}" % f}
            elif f in consts:
                leaf = {"type": spec.get("type", "string"), "default": consts[f]}
            else:
                continue
            props[f] = leaf
            if f in sch.get("required", []): req.append(f)
        t = {"type": "function", "config": {"timeoutMs": 12000, "maxRetries": retries},
             "function": {"name": fname or tool, "handler": "__function__:api_call", "description": desc,
                          "parameters": {"type": "object", "properties": {
                              "url": {"type": "string", "default": self.base + "/" + tool},
                              "method": {"type": "string", "default": "POST"},
                              "headers": {"type": "object", "properties": {"Content-Type": {"type": "string", "default": "application/json"}}},
                              "body": {"type": "object", "properties": props, "required": req}}}}}
        if post: t["post_process_code"] = post
        return t

    def transfer_tool(self, queue):
        q = self.s["transfers"][queue]
        return self.tool("transfer_to_agent", "Hand the call to the " + queue + " team. CALL it " + q["when"] + " Never say the transfer is done before the result says ok true.",
                         {self.f["transfer_to_agent"]["queue"]: {"default": queue}}, fname="transfer_" + queue, retries=1)

    def transfer_routes(self, queues):
        out = []
        for q in queues:
            e = self.s["transfers"][q]["ending"]
            self.used_endings.add(e)
            out.append(go("end_" + e, "The transfer to " + q + " is done", var_true("transfer_" + q + "_response.data.ok")))
        return out

    def queues_for(self, stage):
        return [q for q, v in self.s["transfers"].items() if stage in v.get("stages", [])]

    def model_endings(self, stage):
        out = []
        for k, e in self.endings.items():
            if stage in e.get("stages", []):
                self.used_endings.add(k)
                out.append(by_model("end_" + k, e["when"]))
        return out

    # -- prompt text
    def refs(self, text):
        """Resolve [[end:x]], [[transfer:q]], [[wrap_up]], [[promise]], [[note]] in rule text."""
        def sub(m):
            kind, _, arg = m.group(1).partition(":")
            if kind == "end":
                assert arg in self.endings, "unknown ending in rule: " + arg
                return "call transition_to_end_" + arg
            if kind == "transfer":
                assert arg in self.s["transfers"], "unknown queue in rule: " + arg
                if "offer" in str(self.s["transfers"][arg].get("when", "")):
                    return "call transfer_" + arg + " (you offer this once in the whole call; after a no, never offer it again, also not after a later question or a repeated message)"
                return "call transfer_" + arg
            if kind == "wrap_up": return "call transition_to_wrap_up"
            if kind == "promise": return "call the create_promise tool of that product (create_promise_1 for product 1, and so on)"
            if kind == "note": return "call the add_note tool of that product (add_note_1 for product 1, and so on)"
            if kind == "callback": return "call schedule_callback"
            if kind == "verify": return "call verify_identity"
            raise AssertionError("unknown reference [[" + m.group(1) + "]]")
        return re.sub(r"\[\[([a-z_]+(?::[a-z_0-9]+)?)\]\]", sub, text)

    def rules(self, key):
        return "\n".join("- " + self.refs(r) for r in self.s.get(key, []))

    def system_prompt(self):
        s = self.s
        lines = [s["persona"], "Speak " + s.get("language", "English") + " only. Keep each turn to one or two short sentences and ask one question at a time.",
                 "Today is {{today_say}} (now: {{%s}}). Use only this date and time, never any other clock." % s.get("now_variable", "now"),
                 "Address the customer as " + s["address"] + ".", "Rules that always hold:"]
        lines += ["- " + self.refs(r) for r in s.get("global_rules", [])]
        lines += ["- Tools: when a rule says to call a tool, actually call it; saying it is not enough. Call one tool per message, with no words in that message (except when ending the call, below); speak once, after the result. Never say the same sentence twice. After every tool result, speak to the customer or call the next tool; never end a turn with nothing. When a tool result contains what_to_do_next, follow it.",
                  "- First look for an answer in every customer message: a date, a number, an amount, a yes or a no. If there is one, use it as usual, even when the message then trails off or stops in the middle of a sentence. Only a message with no answer that stops in the middle of a sentence is cut off: the first time in a row, say only: \"Sorry, I think you were cut off. Please go on.\" If the next message is cut off too, ask a very short yes or no question instead (after an offer: \"Is that possible for you?\"). Never count a cut-off message as a refusal or a decline, and never repeat an amount, a date or an offer because of it.",
                  "- Small talk or a story is not an answer and not a refusal, however often it happens. Never treat it as declining, refusing or not wanting to talk. Reply in a few neutral words such as \"I see.\" (never \"sorry\" and no sympathy for small talk; sympathy is only for what a rule names) and ask your question again in its shortest form. An offer you already made is never repeated: to ask again, say only \"Is that possible for you?\", without naming the amount, the date or the team again. Only clear words of refusal (for example \"I won't answer that\", \"I don't want to talk\"), or what a rule above names as declining, count as a refusal.",
                  "- When a rule says to offer something (a team, a transfer, an amount), make the offer even if the customer said earlier that they do not want it, but make it only once: after the customer declines it, never mention it again in the call, not even added to another answer (\"I cannot do that, but I can connect you...\" is a second offer). Then follow their answer.",
                  "- When you must say no to a request, say no in one short sentence and add no alternative the customer already declined.",
                  "- Never say that something is recorded, booked, noted or transferred. The system tells the customer after the bank confirms it.",
                  "- Ending the call: when a rule says to end the call, call the transition_to_end_... tool it names. That tool thanks the customer and says goodbye for you, so do not say goodbye yourself. You may say one short sentence in the same message, then call the tool."]
        return "\n".join(lines)

    # -- nodes
    def code_node(self, name, code, target, extra=()):
        retry = {"target_node": name, "description": "The code step failed, run it again",
                 "condition": {"logic": "or", "conditions": [{"logic": "and", "conditions": [
                     {"variable": name + "_success_count", "operator": "is_not_set"},
                     {"variable": name + "_failure_count", "operator": "less_than", "operand": 3}]}]}}
        return {"name": name, "node_type": "code", "passthrough": True, "passthrough_default_transition": target,
                "on_enter": {"type": "silent"}, "disable_inactivity_monitor": True,
                "pre_actions": [{"type": "function", "function": {"name": "code_exec", "handler": "__function__:code_exec",
                    "parameters": {"type": "object", "properties": {"code": {"type": "string", "default": code}, "timeout_ms": {"type": "number", "default": 10000}}}}}],
                "transitions": [retry] + list(extra) + [always(target)]}

    def conversation(self, name, task, tools, transitions, on_enter=None, turns=30, turn_target=None):
        n = {"name": name, "node_type": "conversation", "task_message": task, "tools": tools,
             "max_node_turns": turns, "transitions": transitions}
        if turn_target: n["max_turn_transition"] = turn_target
        if on_enter:
            n["on_enter"] = on_enter
            n["on_enter_first_entry_only"] = True
        return n

    def close_node(self, name, line, outcome, reason):
        """Say the goodbye line, then close_call with outcome and reason baked in."""
        cf = self.f["close_call"]
        fields = {cf["outcome"]: {"default": outcome}}
        if reason: fields[cf["reason"]] = {"default": reason}
        return {"name": name, "node_type": "api_request", "passthrough": True, "passthrough_default_transition": "end",
                "on_enter": {"type": "tts", "text": line}, "disable_inactivity_monitor": True,
                "pre_actions": [self.tool("close_call", "Close the call record.", fields, retries=2)],
                "transitions": [always("end")]}

    def build(self):
        s, nodes = self.s, {}
        idn, acc = s["identity"], s["account"]
        n_max = min(int(s.get("max_products", MAX_PRODUCTS)), MAX_PRODUCTS)
        for k in ("promise", "service_error", "id_failed", "callback"):
            assert k in self.endings, "the spec needs an ending named " + k

        dates = {"now_var": s.get("now_variable", "now"), "holidays": s["calendar"].get("holidays", []),
                 "business_weekdays": s["calendar"].get("business_weekdays", [0, 1, 2, 3, 4]),
                 "non_business_date": s["calendar"].get("non_business_date", "next"), "money_format": s.get("money_format", "{:,.2f}")}

        # 1. setup: today and the callback window
        nodes["setup"] = self.code_node("setup", code_with({**dates, "callback": s.get("callback", {})}, DATE_LIB, SETUP_CODE), "opening")

        # 2. opening: who is on the line
        cb = self.tool("schedule_callback", "Book a callback. CALL it once, when the person on the line gives a day and a time inside the allowed callback window.",
                       {self.f["schedule_callback"]["date"]: {"var": "callback_date", "description": "Callback date as YYYY-MM-DD."},
                        self.f["schedule_callback"]["time"]: {"var": "callback_time", "description": "Callback time as HH:MM, 24 hour."}}, retries=1)
        self.used_endings.add("callback")
        q_open = self.queues_for("opening")
        opening_task = ("The opening line was already said. Your job in this step: find out whether you are speaking with " + s["full_name"] + ".\n"
                        "A person who gives a different title (for example Mrs or Ms when you asked for Mr) or a different first name is someone else, for example a relative with the same last name.\n"
                        "Until identity is verified, say nothing about why you are calling, to anyone.\n{{callback_facts}}\n"
                        "Rules:\n" + self.rules("opening_rules") + "\nWhen schedule_callback returns ok true the call ends by itself.\nDo not ask any security question in this step.")
        nodes["opening"] = self.conversation("opening", opening_task, [cb] + [self.transfer_tool(q) for q in q_open],
            [go("end_callback", "The callback is booked", var_true("schedule_callback_response.data.ok"))] + self.transfer_routes(q_open)
            + [go("identity", "The customer confirmed who they are", var_is("party", "customer"))] + self.model_endings("opening"),
            on_enter={"type": "tts", "text": s["opening"]["line"]})
        nodes["opening"]["extraction_schema"] = [{"name": "party", "type": "string", "enum_values": ["customer", "other"],
            "description": "customer: the person on the line clearly confirmed being " + s["address"] + ". " + s["opening"].get("confirmation", "") + " A different title (for example Mrs or Ms when you asked for Mr) or a different first name than " + s["full_name"] + " means someone else: other. other: anything else.",
            "min_confidence": 0.6}]

        # 3. identity
        vf = self.f["verify_identity"]
        fields_enum = [q["field"] for q in idn["questions"]]
        verify = self.tool("verify_identity", "Check one security answer. CALL it every time the customer gives an answer to a security question.",
                           {vf["field"]: {"var": "verify_field", "enum": fields_enum, "description": "Which question was answered: " + ", ".join(fields_enum) + "."},
                            vf["answer"]: {"var": "verify_answer", "description": " ".join(q["field"] + ": " + q["format"] + "." for q in idn["questions"])}},
                           post=code_with({"result": idn["result"], "hints": {h: self.refs(t) for h, t in idn.get("hints", {}).items()}}, VERIFY_POST))
        qs = "\n".join("  %d. %s Answer format for verify_identity (field %s): %s" % (i + 1, q["ask"], q["field"], q["format"]) for i, q in enumerate(idn["questions"]))
        identity_task = (s["address"] + " confirmed who they are. You already asked the first security question. Ask the security questions one at a time, in this order:\n" + qs + "\n"
                         "For every answer the customer gives, call verify_identity. A message that contains an answer (a date, digits) is an answer, even when it also complains, asks something or trails off: check it first, then reply to the rest. If they give an answer in pieces, wait until you have all of it. If they correct themself, use the corrected answer.\n"
                         "Until all answers pass, say nothing about why you are calling. Do not tell the customer they are verified; the next step does that.\n"
                         "When no wrong answers are left the call ends by itself; never state anything about the account.\nRules:\n" + self.rules("identity_rules"))
        R = idn["result"]
        q_id = self.queues_for("identity")
        self.used_endings.add("id_failed")
        nodes["identity"] = self.conversation("identity", identity_task, [verify] + [self.transfer_tool(q) for q in q_id],
            [go("load_account", "All security answers verified", var_true("verify_identity_response.data." + R["all_passed"])),
             go("end_id_failed", "No wrong answers left", var_is("verify_identity_response.data." + R["left"], "0"))]
            + self.transfer_routes(q_id) + self.model_endings("identity"),
            on_enter={"type": "tts", "text": idn.get("first_line") or ("Thank you. Before we continue I need to ask some security questions. " + idn["questions"][0]["ask"])})

        # 4. account and facts
        nodes["load_account"] = {"name": "load_account", "node_type": "api_request", "passthrough": True, "passthrough_default_transition": "account_facts",
            "on_enter": {"type": "silent"}, "disable_inactivity_monitor": True,
            "pre_actions": [self.tool("get_account", "Get the late products.", {}, retries=2)],
            "response_mapping": [{"path": acc.get("products_path", "data.products"), "variable": "products"}],
            "transitions": [always("account_facts")]}
        acfg = {**dates, "fields": acc["fields"], "types": acc["types"], "max_products": n_max, "bands": s["bands"], "attempts": s.get("attempts", []),
                "next_weekday": s.get("next_weekday", "following_week"), "statement_intro": acc.get("statement_intro", ""),
                "statement_product": acc.get("statement_product"), "first_ask_one": acc.get("first_ask_one", ""), "first_ask_many": acc.get("first_ask_many", acc.get("first_ask_one", "")),
                "notes": self.notes, "floor_offer": acc.get("floor_offer", ""),
                "transfer_whens": [str(v.get("when", "")).strip().rstrip(".") for v in s.get("transfers", {}).values()
                                   if "negotiate" in v.get("stages", []) and "offer" not in str(v.get("when", "")) and "insist" not in str(v.get("when", ""))]}
        self.used_endings.add("account_error" if "account_error" in self.endings else "service_error")
        acc_err = "end_account_error" if "account_error" in self.endings else "end_service_error"
        nodes["account_facts"] = self.code_node("account_facts", code_with(acfg, DATE_LIB, ACCOUNT_CODE), "negotiate_1",
            extra=[go(acc_err, "The account could not be loaded", var_is("account_state", "error"))]
                  + [go("negotiate_%d" % k, "%d products" % k, var_is("product_count", str(k))) for k in range(1, n_max + 1)])

        # 5. negotiation, one step per product count so that only real products have tools
        cp, an = self.f["create_promise"], self.f["add_note"]
        q_neg = self.queues_for("negotiate")
        note_reasons = (s.get("notes") or {}).get("reasons", [])
        for n in range(1, n_max + 1):
            tools, save_tools = [], []
            for k in range(1, n + 1):
                save_tools.append(self.tool("create_promise", "Record the payment promise for product %d. CALL it once, and only when ALL of these are true: (1) you checked the date in CALENDAR and it is allowed for this product (never a weekend, a bank holiday or a date after the deadline); (2) in your previous message you read back the product, the amount and the date and asked for confirmation; (3) the customer then said a clear yes. Agreeing to a date is not a yes to the read-back." % k,
                    {cp["product"]: {"default": "{{pid_%d}}" % k},
                     cp["amount"]: {"var": "promise_amount_%d" % k, "description": "Agreed amount for product %d, a number such as 637.5." % k},
                     cp["date"]: {"var": "promise_date_%d" % k, "description": "Agreed payment date for product %d as YYYY-MM-DD." % k}},
                    fname="create_promise_%d" % k, retries=0))
                if not GATE:
                    tools.append(save_tools.pop())
                if self.notes:
                    tools.append(self.tool("add_note", "Record why product %d ends without a promise. CALL it once for product %d when it gets no promise, ALWAYS with the reason. If the result is not ok, call it again with the reason." % (k, k),
                        {an["product"]: {"default": "{{pid_%d}}" % k},
                         an["reason"]: {"var": "note_reason_%d" % k, "enum": note_reasons, "description": "One of: " + ", ".join(note_reasons) + "."}},
                        fname="add_note_%d" % k, retries=1))
            tools += [self.transfer_tool(q) for q in q_neg]
            facts = ("FACTS (computed for you; trust them, do not recalculate):\n{{deal_facts}}\n\n")
            if GATE:
                tool_line = ("Saving a promise: read back the product, the amount and the date and ask for confirmation. When the customer says yes, "
                             "the system opens the saving step by itself; you do not save promises here. If the customer clearly said yes and you are still "
                             "in this step, ask once more, briefly: \"Do you confirm?\"")
            else:
                tool_line = "Tools: create_promise_N records the promise for product N; call it only after the read-back was confirmed, once per product."
            task = ("The customer is verified. You already stated the debt and asked the first question.\n\n" + facts + tool_line
                    + (" add_note_N records why product N ends without a promise." if self.notes else "") + "\n"
                    "Never say that a promise is recorded: after the last promise or note, call transition_to_wrap_up and the system tells the customer what was recorded.\n"
                    "Rules:\n" + self.rules("negotiate_rules"))
            # a failed promise is repeated once by the engine, with the values of the first try
            trans = []
            for k in range(1, n + 1):
                r = "retry_%d_%d" % (n, k)
                failed = [{"variable": "create_promise_%d_failure_count" % k, "operator": "greater_than", "operand": 0},
                          {"variable": "create_promise_%d_response.data.ok" % k, "operator": "equals", "operand": "false"},
                          {"variable": "create_promise_%d_response.data.ok" % k, "operator": "equals", "operand": "False"}]
                trans.append({"target_node": r, "description": "The promise call of product %d failed" % k, "condition": {"logic": "or", "conditions": [
                    {"logic": "and", "conditions": [{"logic": "or", "conditions": failed},
                                                    {"variable": r + "_success_count", "operator": "is_not_set"},
                                                    {"variable": r + "_failure_count", "operator": "is_not_set"}]}]}})
                back = "wrap_up" if n == 1 else ("save_%d" % n if GATE else "negotiate_%d" % n)
                nodes[r] = {"name": r, "node_type": "api_request", "passthrough": True, "passthrough_default_transition": back,
                    "on_enter": {"type": "silent"}, "disable_inactivity_monitor": True,
                    "pre_actions": [self.tool("create_promise", "Repeat the failed promise.",
                        {cp["product"]: {"default": "{{pid_%d}}" % k},
                         cp["amount"]: {"default": "{{create_promise_%d_args.promise_amount_%d}}" % (k, k)},
                         cp["date"]: {"default": "{{create_promise_%d_args.promise_date_%d}}" % (k, k)}})],
                    "response_mapping": [{"path": "data.ok", "variable": "retry_ok_%d" % k}, {"path": "status", "variable": "retry_done_%d" % k}],
                    "transitions": [always(back)]}
            # every product has a promise or a note the bank accepted (or a finished retry): wrap up without waiting for the model
            def done(k):
                o = [{"variable": "create_promise_%d_response.data.ok" % k, "operator": "equals", "operand": "true"},
                     {"variable": "create_promise_%d_response.data.ok" % k, "operator": "equals", "operand": "True"},
                     {"variable": "retry_done_%d" % k, "operator": "is_set"}]
                if self.notes:
                    o += [{"variable": "add_note_%d_response.data.ok" % k, "operator": "equals", "operand": "true"},
                          {"variable": "add_note_%d_response.data.ok" % k, "operator": "equals", "operand": "True"}]
                return {"logic": "or", "conditions": o}
            trans.append({"target_node": "wrap_up", "description": "Every product has a promise or a note the bank accepted", "condition": {"logic": "or", "conditions": [
                {"logic": "and", "conditions": [done(k) for k in range(1, n + 1)]}]}})
            trans += self.transfer_routes(q_neg)
            trans += self.model_endings("negotiate") + [by_model("wrap_up", "Every product has a promise call or a note, or nothing more can be agreed")]
            if not GATE:
                nodes["negotiate_%d" % n] = self.conversation("negotiate_%d" % n, task, tools, trans,
                    on_enter={"type": "tts", "text": "{{debt_statement}}"}, turns=60, turn_target="wrap_up")
                continue
            # the promise gate: negotiation has no promise tool. A separate extraction model must hear a yes to a read-back
            # before the call moves to the saving step, where the promise tools are.
            gate_var = "readback_yes_%d" % n
            neg = self.conversation("negotiate_%d" % n, task, tools,
                [go("save_%d" % n, "The customer confirmed the read-back", var_is(gate_var, "yes"))] + trans,
                on_enter={"type": "tts", "text": "{{debt_statement}}"}, turns=60, turn_target="wrap_up")
            neg["extraction_schema"] = [{"name": gate_var, "type": "string", "enum_values": ["yes", "no"], "min_confidence": 0.6,
                "description": "yes only if the agent's last message read back a payment promise with an amount and a date and asked the customer to confirm it, "
                               "and the customer's latest message clearly agrees to that read-back. no in every other case, for example when the "
                               "customer only names a date or an amount, agrees to pay without a read-back, or changes something."}]
            nodes["negotiate_%d" % n] = neg
            # a date check after the yes was tried and dropped: the extraction model hears only the customer, not the
            # agent's read-back, so the date it extracts is not reliable enough to reject a promise on.
            save_task = ("The customer just confirmed your read-back.\n\n" + facts +
                         "Now call create_promise_N for every product in the read-back, one call per message, with exactly the amount and date you read back. "
                         "Say nothing before the calls. If the customer changes something instead, check it with the rules, read back again and wait for a new yes before saving."
                         + (" add_note_N records why product N ends without a promise." if self.notes else "") + "\n"
                         "Never say that a promise is recorded: the system tells the customer what was recorded.\n"
                         "Rules:\n" + self.rules("negotiate_rules"))
            nodes["save_%d" % n] = self.conversation("save_%d" % n, save_task, save_tools + tools, trans, turns=30, turn_target="wrap_up")

        # 6. wrap up: what was recorded, notes for failed promises, the outcome
        table = {k: {"outcome": e["outcome"], "reason": e.get("reason"), "line": e["line"]} for k, e in self.endings.items()}
        notes_cfg = s.get("notes") or {}
        wcfg = {**dates, "money_format": s.get("money_format", "{:,.2f}"), "notes": self.notes, "ending_table": table,
                "endings": {"promise": "promise", "service_error": "service_error", "all_paid": notes_cfg.get("all_paid_ending", "paid"),
                            "fallback": s.get("fallback_ending", "service_error")},
                "note_endings": notes_cfg.get("reason_endings", {}), "paid_note": notes_cfg.get("paid_reason", "paid"),
                "service_error_note": notes_cfg.get("service_error_reason", "service_error"),
                "recorded_sentence": s["promise"]["recorded_sentence"], "failed_sentence": s["promise"]["failed_sentence"],
                "after_record_sentence": s["promise"]["after_record_sentence"]}
        chain = ["svc_note_%d" % k for k in range(1, n_max + 1)] if self.notes else []
        def next_after(i):
            """From position i in the service-note chain, go to the next product that needs one, else report."""
            out = [go(chain[j], "product %d needs a service_error note" % (j + 1), var_is("svc_note_%d" % (j + 1), "yes")) for j in range(i, len(chain))]
            return out + [go("report", "promise recorded", var_is("any_recorded", "yes")), always("close_final")]
        nodes["wrap_up"] = self.code_node("wrap_up", code_with(wcfg, DATE_LIB, WRAP_CODE), "close_final", extra=next_after(0))
        for j, name in enumerate(chain):
            k = j + 1
            nodes[name] = {"name": name, "node_type": "api_request", "passthrough": True, "passthrough_default_transition": "close_final",
                "on_enter": {"type": "silent"}, "disable_inactivity_monitor": True,
                "pre_actions": [self.tool("add_note", "Note a failed promise.", {an["product"]: {"default": "{{pid_%d}}" % k}, an["reason"]: {"default": notes_cfg.get("service_error_reason", "service_error")}}, retries=1)],
                "transitions": next_after(k)}
        q_done = self.queues_for("after_promise")
        nodes["report"] = self.conversation("report",
            "The system just told the customer which promises are recorded. Do not take another promise and do not change one; if asked, "
            + s["promise"].get("change_answer", "say that is not possible in this call") + ".\nRules:\n" + self.rules("after_promise_rules")
            + "\nWhen nothing else is needed, call transition_to_close_final.",
            [self.transfer_tool(q) for q in q_done], self.transfer_routes(q_done) + [by_model("close_final", "Nothing else is needed")],
            on_enter={"type": "tts", "text": "{{report_line}} " + s["promise"].get("anything_else", "Is there anything else I can help you with?")}, turns=20, turn_target="close_final")
        # final_reason is left unset for outcomes without a reason, and an unset single placeholder drops the field
        nodes["close_final"] = self.close_node("close_final", "{{close_report}}{{final_line}}", "{{final_outcome}}", "{{final_reason}}")

        # 7. endings
        for k in sorted(self.used_endings):
            e = self.endings[k]
            nodes["end_" + k] = self.close_node("end_" + k, e["line"], e["outcome"], e.get("reason"))
        nodes["end"] = {"name": "end", "node_type": "end_call", "on_enter": {"type": "silent"}, "disable_inactivity_monitor": True, "transitions": [],
                        "terminal": {"type": "function", "function": {"name": "call_closed", "handler": "__function__:end_call",
                                     "parameters": {"type": "object", "properties": {"message_mode": {"type": "string", "default": "none"}}}}}}

        for k, n in nodes.items():
            for t in n.get("transitions", []):
                assert t["target_node"] in nodes, (k, t["target_node"])
        layout(nodes, "setup")
        return {"initial_node": "setup", "system_prompt": self.system_prompt(), "global_nodes": {}, "nodes": nodes,
                "test_variables": s.get("test_variables", {})}


def layout(nodes, start):
    """Place the nodes for the visual editor: one column per step from the start, endings in the last column."""
    col = {start: 0}; queue = [start]
    while queue:
        k = queue.pop(0)
        for t in nodes[k].get("transitions", []):
            if t["target_node"] not in col:
                col[t["target_node"]] = col[k] + 1; queue.append(t["target_node"])
    last = max(col.values()) + 1
    rows = {}
    for k, n in nodes.items():
        c = last if k.startswith("end") or k not in col else col[k]
        n["position"] = {"x": c * 380, "y": rows.get(c, 0) * 160}
        rows[c] = rows.get(c, 0) + 1


def generate(spec, api):
    return Builder(spec, api).build()


if __name__ == "__main__":
    spec, api, out = json.load(open(sys.argv[1])), json.load(open(sys.argv[2])), sys.argv[3]
    wf = generate(spec, api)
    pathlib.Path(out).parent.mkdir(parents=True, exist_ok=True)
    json.dump(wf, open(out, "w"), indent=1)
    print("wrote", out, len(wf["nodes"]), "steps")
