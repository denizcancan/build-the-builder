"""Generate out/brief-a.workflow.json from Brief A and tools/openapi.json."""
import json, sys, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
API = json.load(open(ROOT / "tools/openapi.json"))
BASE = API["servers"][0]["url"]

def req_schema(tool):
    return API["paths"]["/" + tool]["post"]["requestBody"]["content"]["application/json"]["schema"]

# ---------------------------------------------------------------- tools
def api_tool(name, desc, fields, pre="", post="", mapping=None, retries=0):
    """fields: dict field -> dict(description, enum, type). customer_id is baked."""
    schema = req_schema(name)
    props, required = {}, []
    for f, spec in schema["properties"].items():
        if f == "customer_id":
            props[f] = {"type": "string", "default": "{{customer_id}}"}
            continue
        if f not in fields:
            continue
        o = fields[f]
        leaf = {"type": spec.get("type", "string"), "default": o.get("default", "{{%s}}" % o.get("var", f))}
        if o.get("description"):
            leaf["description"] = o["description"]
        enum = o.get("enum", spec.get("enum"))
        if enum:
            leaf["enum"] = enum
        props[f] = leaf
        if f in schema.get("required", []):
            required.append(f)
    t = {
        "type": "function",
        "config": {"timeoutMs": 12000, "maxRetries": retries},
        "function": {
            "name": name,
            "handler": "__function__:api_call",
            "description": desc,
            "parameters": {"type": "object", "properties": {
                "url": {"type": "string", "default": BASE + "/" + name},
                "method": {"type": "string", "default": "POST"},
                "headers": {"type": "object", "properties": {"Content-Type": {"type": "string", "default": "application/json"}}},
                "body": {"type": "object", "properties": props, "required": required},
            }},
        },
    }
    if pre: t["pre_process_code"] = pre
    if post: t["post_process_code"] = post
    if mapping: t["response_mapping"] = mapping
    return t

V = "try:\n    V = vars\nexcept Exception:\n    V = {}\n"

VERIFY_POST = V + r'''
try:
    if isinstance(result, dict):
        result.pop('header', None)
        d = result.get('data')
        if not isinstance(d, dict):
            d = dict()
            result['data'] = d
        left = d.get('wrong_answers_left')
        try:
            left = int(left)
        except Exception:
            left = None
        fail_msg = 'Three wrong answers: the identity check has ended. Do not state anything about the account, the debt or the product. Tell the customer to use the Freya Bank mobile app or visit a branch. Then thank them, say goodbye and call close_call with outcome not_verified and reason failed.'
        if d.get('all_verified') is True or str(d.get('all_verified')).lower() == 'true':
            w = 'Both security answers are verified.'
        elif d.get('hint') == 'day_month_swapped':
            w = 'The day and month look swapped. Ask the customer to say the month of their date of birth as a word, for example the third of April. This does NOT count as a wrong answer.'
        elif d.get('verified') is True or str(d.get('verified')).lower() == 'true':
            w = 'This answer is correct. Now ask the other security question that has not passed yet: the last four digits of the phone number on file. Then call verify_identity again.'
        elif left == 0:
            V['id_failed'] = 'yes'
            w = fail_msg
        elif d.get('verified') is False or str(d.get('verified')).lower() == 'false':
            w = 'This answer does not match our records. Say so and ask the same question again. Wrong answers left: ' + str(left) + '.'
        else:
            err = str(d.get('error') or '')
            if 'lock' in err.lower() or 'ended' in err.lower() or 'too many' in err.lower():
                V['id_failed'] = 'yes'
                w = fail_msg
            else:
                w = 'The answer could not be checked (' + err + '). It does not count as a wrong answer. If it was a date of birth, ask the customer to say it again with the month as a word, and send it as YYYY-MM-DD.'
        d['what_to_do_next'] = w
except Exception:
    pass
'''

PROMISE_POST = V + r'''
try:
    if isinstance(result, dict):
        result.pop('header', None)
        d = result.get('data')
        if not isinstance(d, dict):
            d = dict()
            result['data'] = d
        ok = d.get('ok') is True or str(d.get('ok')).lower() == 'true'
        if ok:
            V['promise_recorded'] = 'yes'
            d['what_to_do_next'] = 'The promise IS recorded. Say: "Your promise is recorded. You will receive a text message confirming this promise." Then thank the customer, say goodbye and call close_call with outcome promise. Do not call create_promise again.'
        else:
            d['ok'] = False
            n = 0
            try:
                n = int(V.get('promise_failures') or 0)
            except Exception:
                n = 0
            n = n + 1
            V['promise_failures'] = str(n)
            if n >= 2:
                d['what_to_do_next'] = 'The promise was NOT recorded, for the second time. Do not say it is recorded. Tell the customer you could not record the promise because of a technical problem. Then thank them, say goodbye and call close_call with outcome no_promise and reason service_error.'
            else:
                d['what_to_do_next'] = 'The promise was NOT recorded: the service failed. Do not say it is recorded. Tell the customer there was a technical problem and you are trying again, and in the same message call create_promise once more with exactly the same amount and date. (If this was already the second failed attempt, instead say you could not record it, say goodbye and call close_call with outcome no_promise and reason service_error.)'
except Exception:
    pass
'''

TRANSFER_PRE = V + r'''
try:
    b = config.get('body')
    if isinstance(b, dict):
        V['transfer_queue_try'] = str(b.get('queue') or '')
except Exception:
    pass
'''

TRANSFER_POST = V + r'''
try:
    if isinstance(result, dict):
        result.pop('header', None)
        d = result.get('data')
        if not isinstance(d, dict):
            d = dict()
            result['data'] = d
        ok = d.get('ok') is True or str(d.get('ok')).lower() == 'true'
        if ok:
            V['transfer_queue'] = V.get('transfer_queue_try') or ''
            d['what_to_do_next'] = 'The transfer is done. Tell the customer a colleague will take over, thank them, say goodbye and call close_call with outcome transferred and the reason that matches the queue (restructuring -> restructuring, support -> support, general -> human).'
        else:
            d['what_to_do_next'] = 'The transfer did NOT work. Tell the customer you could not transfer them right now. Do not say they were transferred.'
except Exception:
    pass
'''

CLOSE_PRE = V + r'''
try:
    b = config.get('body')
    if isinstance(b, dict):
        q = str(V.get('transfer_queue') or '')
        if q:
            b['outcome'] = 'transferred'
            b['reason'] = 'human' if q == 'general' else q
        elif str(V.get('promise_recorded') or '') == 'yes':
            b['outcome'] = 'promise'
        elif str(V.get('id_failed') or '') == 'yes':
            b['outcome'] = 'not_verified'
            b['reason'] = 'failed'
        if b.get('outcome') in ('promise', 'paid'):
            b.pop('reason', None)
        r = b.get('reason')
        if r is None or str(r).strip() == '' or str(r).startswith('{'):
            b.pop('reason', None)
except Exception:
    pass
'''

GENERIC_POST = r'''
try:
    if isinstance(result, dict):
        result.pop('header', None)
except Exception:
    pass
'''

PROMISE_PRE = V + r'''
try:
    b = config.get('body')
    if isinstance(b, dict):
        if b.get('amount') not in (None, ''):
            try:
                b['amount'] = float(b.get('amount'))
            except Exception:
                pass
            V['p_amount'] = str(b.get('amount'))
        if b.get('date'):
            V['p_date'] = str(b.get('date'))[:10]
except Exception:
    pass
'''

def close_tool(outcomes, reasons, guide):
    return api_tool(
        "close_call",
        "End the call record. CALL it exactly once, as the very last tool call, in the same message in which you thank the customer and say goodbye. " + guide,
        {
            "outcome": {"description": "How the call ended. One of: " + ", ".join(outcomes) + ".", "enum": outcomes},
            "reason": {"description": "Reason for the outcome. One of: " + ", ".join(reasons) + ". Leave it out for promise and paid.", "enum": reasons},
        },
        pre=CLOSE_PRE, post=GENERIC_POST)

def transfer_tool(queues, guide):
    return api_tool(
        "transfer_to_agent",
        "Hand the call to a person. " + guide,
        {"queue": {"description": "Queue. One of: " + ", ".join(queues) + ".", "enum": queues}},
        pre=TRANSFER_PRE, post=TRANSFER_POST)

# ---------------------------------------------------------------- code
DATE_LIB = r'''
import datetime, json, re
H = set(['2026-10-26', '2026-11-11', '2026-11-26', '2026-12-25', '2027-01-01'])
TD = datetime.timedelta
WD = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
MO = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December']
def isbd(d):
    return d.weekday() < 5 and d.isoformat() not in H
def nextbd(d):
    while not isbd(d):
        d = d + TD(days=1)
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
raw = str(vars.get('now') or '')
try:
    nowdt = datetime.datetime.fromisoformat(raw[:16])
except Exception:
    nowdt = datetime.datetime(2026, 10, 14, 11, 0)
today = nowdt.date()
'''

SETUP_CODE = DATE_LIB + r'''
try:
    vars['today_say'] = say(today)
    vars['today_iso'] = today.isoformat()
    lines = []
    hm = nowdt.strftime('%H:%M')
    if nowdt.hour < 19:
        if nowdt.hour < 10:
            lines.append('today, ' + sayiso(today) + ', from 10:00 to 19:00')
        else:
            lines.append('today, ' + sayiso(today) + ', any time later than ' + hm + ' and up to 19:00')
    nb = addbd(today, 1)
    lines.append('the next business day, ' + sayiso(nb) + ', from 10:00 to 19:00 (both included)')
    vars['callback_facts'] = ' or '.join(lines) + '. Any other day or time is outside the window.'
except Exception:
    vars['today_say'] = raw
    vars['today_iso'] = raw[:10]
    vars['callback_facts'] = 'today later than now and up to 19:00, or the next business day from 10:00 to 19:00'
'''

ACCOUNT_CODE = DATE_LIB + r'''
def money(x):
    try:
        return '{:,.2f}'.format(float(x))
    except Exception:
        return str(x)
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
            i = int(m.group(1))
            found.setdefault(i, dict())[m.group(2)] = vars.get(k)
    return [found[i] for i in sorted(found)]
try:
    prods = get_products()
    p = prods[0]
    ptype = str(p.get('type'))
    pid = str(p.get('product_id') or 'p1')
    full = float(p.get('past_due'))
    days = int(float(p.get('days_late')))
    if ptype == 'credit_card':
        pw = 'credit card ending ' + str(p.get('card_last4'))
        floor = float(p.get('minimum_payment'))
        fname = 'minimum payment'
    else:
        pw = 'personal loan' if ptype == 'personal_loan' else 'car loan'
        floor = float(p.get('instalment_amount'))
        fname = 'instalment amount'
    if days <= 7:
        band = 'early'
        deadline = addbd(today, 12)
        attempts = 1
    else:
        band = 'late'
        deadline = addbd(today, 7)
        attempts = 2
    mon = today - TD(days=today.weekday())
    wk = [mon + TD(days=i) for i in range(5) if isbd(mon + TD(days=i))]
    a1 = max(wk) if wk else None
    if a1 is None or a1 <= today:
        nmon = mon + TD(days=7)
        wk2 = [nmon + TD(days=i) for i in range(5) if isbd(nmon + TD(days=i))]
        a1 = max(wk2)
    if a1 > deadline:
        a1 = deadline
    cal = []
    d = today
    end = deadline + TD(days=10)
    while d <= end:
        if d > deadline:
            lab = 'AFTER the deadline: not allowed. Say the latest date you can record is ' + say(deadline) + ' and offer it.'
        elif isbd(d):
            lab = 'allowed business day' + (' (today)' if d == today else '')
        else:
            why = 'bank holiday' if d.isoformat() in H else 'weekend'
            nb = nextbd(d)
            if nb > deadline:
                lab = why + ': not allowed. Offer the deadline ' + say(deadline) + '.'
            else:
                lab = why + ': not allowed. Offer the next business day, ' + say(nb) + '.'
        cal.append('- ' + d.isoformat() + ' ' + WD[d.weekday()] + ' ' + str(d.day) + ' ' + MO[d.month - 1] + ': ' + lab)
        d = d + TD(days=1)
    rel = []
    nmon = mon + TD(days=7)
    for i in range(7):
        t = mon + TD(days=i)
        n = nmon + TD(days=i)
        rel.append('- "this ' + WD[i] + '" = ' + t.isoformat() + (' (already past)' if t < today else '') + '; "next ' + WD[i] + '" = ' + n.isoformat())
    full_s = money(full)
    floor_s = money(floor)
    vars['product_id'] = pid
    vars['account_state'] = 'ok'
    vars['debt_statement'] = 'Thank you, you are verified. I am calling from the Freya Bank collections team about a late payment on your ' + pw + '. The past-due amount is ' + full_s + ', and the payment is ' + str(days) + ' days late. Can you make this payment today?'
    a1_amount = 'the full past-due amount ' + full_s + ' (or the ' + fname + ' ' + floor_s + ' if the customer already said the full amount is too much)'
    facts = []
    facts.append('Today: ' + sayiso(today) + '.')
    facts.append('Product: ' + pw + ' (product_id ' + pid + ').')
    facts.append('Full past-due amount: ' + full_s + '. Days late: ' + str(days) + '. Band: ' + band + '.')
    facts.append('Floor (' + fname + '): ' + floor_s + '. The lowest amount you may record is ' + floor_s + ', the highest is ' + full_s + '.')
    facts.append('Deadline (latest promise date): ' + sayiso(deadline) + '.')
    facts.append('Persuasion attempts allowed: ' + str(attempts) + '.')
    facts.append('Attempt 1 offer: ' + a1_amount + ' on ' + sayiso(a1) + '.')
    if attempts == 2:
        facts.append('Attempt 2 offer: the ' + fname + ' ' + floor_s + ' on the deadline, ' + sayiso(deadline) + '.')
    else:
        facts.append('There is no attempt 2 in this band. After attempt 1 is refused, stop.')
    facts.append('Words for "this/next <weekday>":')
    facts.extend(rel)
    facts.append('CALENDAR (look up every date the customer names):')
    facts.extend(cal)
    vars['deal_facts'] = '\n'.join(facts)
    vars['floor_s'] = floor_s
    vars['full_s'] = full_s
    vars['floor_name'] = fname
    vars['product_words'] = pw
    vars['deadline_say'] = say(deadline)
except Exception as e:
    vars['account_state'] = 'error'
    vars['product_id'] = 'p1'
    vars['debt_statement'] = 'Thank you, you are verified.'
    vars['deal_facts'] = 'Account details are not available: ' + str(e)
    vars['floor_s'] = ''
    vars['full_s'] = ''
    vars['floor_name'] = ''
    vars['product_words'] = ''
    vars['deadline_say'] = ''
'''

for c in (PROMISE_PRE, SETUP_CODE, ACCOUNT_CODE, VERIFY_POST, PROMISE_POST, TRANSFER_PRE, TRANSFER_POST, CLOSE_PRE, GENERIC_POST):
    assert "{{" not in c and "}}" not in c
    compile(c, "x", "exec")

def code_node(name, code, target):
    return {
        "name": name, "node_type": "code", "passthrough": True, "passthrough_default_transition": target,
        "on_enter": {"type": "silent"}, "disable_inactivity_monitor": True,
        "pre_actions": [{"type": "function", "function": {"name": "code_exec", "handler": "__function__:code_exec",
            "parameters": {"type": "object", "properties": {"code": {"type": "string", "default": code}, "timeout_ms": {"type": "number", "default": 10000}}}}}],
        "transitions": [{"target_node": target, "description": "always", "condition": {"operator": "always"}}],
    }

def var_true(v):
    return [{"variable": v, "operator": "equals", "operand": "true"}, {"variable": v, "operator": "equals", "operand": "True"}]

CLOSED = {"target_node": "end", "description": "close_call was made",
          "condition": {"logic": "or", "conditions": [{"variable": "close_call_success_count", "operator": "greater_than", "operand": 0},
                                                      {"variable": "close_call_failure_count", "operator": "greater_than", "operand": 0}]}}

PROMISE_OK = {"target_node": "promise_done", "description": "The promise was recorded",
    "condition": {"logic": "or", "conditions": [{"variable": "promise_recorded", "operator": "equals", "operand": "yes"}]}}
PROMISE_FAIL = {"target_node": "retry_promise", "description": "The first create_promise call failed",
    "condition": {"logic": "or", "conditions": [{"variable": "create_promise_failure_count", "operator": "greater_than", "operand": 0},
        {"variable": "create_promise_response.data.ok", "operator": "equals", "operand": "false"},
        {"variable": "create_promise_response.data.ok", "operator": "equals", "operand": "False"}]}}
TRANSFER_OK = {"target_node": "after_transfer", "description": "The transfer was done",
    "condition": {"logic": "or", "conditions": [{"variable": "transfer_queue", "operator": "is_set"}] + var_true("transfer_to_agent_response.data.ok")}}

PROMISE_DONE_TASK = """A payment promise is recorded and you already told the customer, including that a text message will confirm it.
One promise only: it cannot be changed or cancelled in this call; if asked, say they can change it in the mobile app. Do not take another promise.
If they ask for a new payment plan or lower instalments, call transfer_to_agent with queue restructuring. If they insist on a human after you asked once whether you can help, call transfer_to_agent with queue general. Never call close_call together with a transfer.
Otherwise, when nothing else is needed, thank them, say goodbye and call close_call with outcome promise."""

# ---------------------------------------------------------------- prompts
SYSTEM = """You are the digital assistant of Freya Bank, on an outbound phone call. Speak English. Keep each turn to one or two short sentences and ask one question at a time.
Today is {{today_say}} (now: {{now}}). Use only this date and time, never any other clock.
Address the customer as {{title}} {{last_name}}.
Rules that always hold:
- If asked whether you are a robot or an AI: say you are Freya Bank's digital assistant, then continue.
- If asked whether this call is a scam: say they can verify the call by phoning Freya Bank's customer service number, then continue.
- If asked something the information you have does not answer, say you do not have that information. Never invent figures, dates or reasons.
- Never offer a discount, a waiver, an instalment plan or any other term you were not given.
- Tools: when a rule says to call a tool, actually call it; saying it is not enough. Call one tool per message. After every tool result, speak to the customer or call the next tool; never end a turn with nothing. When a tool result contains what_to_do_next, follow it exactly.
- Never say something is recorded, scheduled or transferred unless the tool result says ok true.
- Ending the call: in one message, thank the customer and say goodbye, and call close_call in that same message. close_call is always the very last tool call and is called exactly once."""

OPENING_LINE = "Hello, this is the digital assistant of Freya Bank calling. This call is recorded. Am I speaking with {{title}} {{last_name}}?"

OPENING_TASK = """The opening line was already said: you introduced yourself as Freya Bank's digital assistant, said the call is recorded, and asked "Am I speaking with {{title}} {{last_name}}?".
Your job in this step: find out whether you are speaking with {{title}} {{first_name}} {{last_name}}.
STRICT: until identity is verified, say nothing about why you are calling. Never mention collections, debt, payment, money, an account or a product, to anyone, including a relative who asks or offers to pay. If asked what it is about, say you can only discuss it with {{title}} {{last_name}} in person.

1. Only "yes", "that's me", "speaking" or the customer's own name counts as confirmation. "Hello", "go ahead", "who is this" or any question do not.
2. Not confirmed: answer any question first (for example "who is this": you are Freya Bank's digital assistant), then ask again "Am I speaking with {{title}} {{last_name}}?". That is the second and last ask. If the reply to the second ask is still not a confirmation and none of the cases below, thank them, say goodbye and call close_call with outcome not_reached and reason unclear.
3. Someone else answers (a relative, a colleague, anyone who is not the customer): do not say why you call, ask no security questions, tell them nothing about any account, even if they offer to pay. Ask when {{title}} {{last_name}} can be reached.
   Allowed callback times: {{callback_facts}}
   - Day and time inside the window: call schedule_callback with date as YYYY-MM-DD and time as HH:MM (24 hour; "around eleven in the morning" is 11:00). After ok true, thank them, say goodbye and call close_call with outcome not_reached and reason callback.
   - Only a day or only a vague time: ask for the missing part.
   - Outside the window: say that time is not possible and ask once for another time today or the next business day between 10:00 and 19:00. If the next answer is also outside, do not schedule: thank them, say goodbye, close_call with outcome not_reached and reason no_callback. Same if they do not know or will not say.
4. The person says the number does not belong to the customer: apologise, say goodbye and close_call with outcome not_reached and reason wrong_number.
5. The person says the customer has died: say exactly "I am sorry for your loss." Ask nothing else. Say goodbye and close_call with outcome not_reached and reason deceased.
6. A request for a human: ask once whether you can help first. If they insist, call transfer_to_agent with queue general. Do NOT call close_call before or together with the transfer.
7. The customer says they do not want to talk: say it will only take a moment and ask again. If they say it a second time, thank them, say goodbye and close_call with outcome not_verified and reason declined.
Do not ask any security question in this step."""

IDENTITY_LINE = "Thank you. Before we continue I need to ask two security questions. What is your date of birth?"

IDENTITY_TASK = """{{title}} {{last_name}} confirmed who they are. You already asked for the date of birth. Now check identity with two security questions, one at a time: first the date of birth, then the last four digits of the phone number on file.
STRICT: until both answers pass, say nothing about why you are calling: no collections, debt, payment, amount or product. If asked what it is about, say you can explain right after the security questions.

- For every answer the customer gives, call verify_identity.
  - field date_of_birth: answer as YYYY-MM-DD. Convert whatever the customer says ("fourteenth of March eighty-eight" -> 1988-03-14). A date given only as numbers is day, then month, then year ("3/4/1990" -> 1990-04-03). If the customer corrects themself, use the corrected answer.
  - field phone_last4: exactly four digits. If they give the digits in pieces, wait until you have all four.
- Follow what_to_do_next in each result. Do not tell the customer they are verified; the next step does that.
- A wrong answer may be asked again. Three wrong answers in total end the check: then do not state anything about the account, tell them to use the Freya Bank mobile app or visit a branch, thank them, say goodbye and close_call with outcome not_verified and reason failed.
- The customer does not want to answer, or asks what the call is about instead of answering: explain once that these questions protect their account, and ask again. If they decline again or ask again, thank them, say goodbye and close_call with outcome not_verified and reason declined.
- The customer says twice that they do not want to talk: close_call with outcome not_verified and reason declined, after a goodbye.
- A request for a human: ask once whether you can help first. If they insist, call transfer_to_agent with queue general. Do NOT call close_call before or together with the transfer."""

NEGOTIATE_TASK = """The customer is verified. You already told them: you are calling from the collections team about a late payment on their {{product_words}}, the past-due amount, the days late, and you asked "Can you make this payment today?".

FACTS (computed for you; trust them, do not recalculate):
{{deal_facts}}

GETTING THE PROMISE
1. You propose every amount. Never ask "how much can you pay?" or anything like it. If the customer says "you tell me", you propose.
2. The amount is always the full past-due amount {{full_s}} unless the customer said the full amount is too much (rule 5) or named an amount themselves (rule 6). Agreeing to a date without naming an amount means the full amount. Never offer the {{floor_name}} before the customer says the full amount is too much (except Attempt 2).
2b. Yes to paying today: the amount is the full past-due amount {{full_s}} (unless they named another allowed amount), the date is today. Go to the read-back.
3. The customer names a date: look it up in CALENDAR and do exactly what its line says. An allowed business day up to the deadline is accepted. A weekend day or holiday up to the deadline: say that day is not possible and offer the next business day given. A date after the deadline: say the latest date you can record is {{deadline_say}} and offer it. If they refuse the deadline, that is a flat refusal (rule 7).
4. A vague period ("next week", "the week after next", "end of the month", "when my salary comes"): ask once for the exact day. If they still give none, propose the deadline {{deadline_say}}.
5. The customer says the full amount is too much: offer the {{floor_name}} of {{floor_s}} once, and say the rest stays overdue. If they refuse the {{floor_name}}, that is a flat refusal (rule 7).
6. The customer proposes an amount: from {{floor_s}} up to {{full_s}}, both included, accept it. Below {{floor_s}}: say it cannot be recorded and offer the {{floor_name}} {{floor_s}} once; if they do not accept, that is a flat refusal (rule 7).
   A date or an amount the customer proposes is never a refusal.
7. A flat refusal or a non-answer uses one persuasion attempt (see FACTS for how many are allowed). A no to paying today with no date and no amount is a flat refusal.
   - Attempt 1: offer exactly the Attempt 1 offer in FACTS.
   - Attempt 2 (only if FACTS lists it): offer the floor amount on the deadline.
   When the allowed attempts are used up and the customer still refuses: stop. Do not repeat an offer they already refused. Thank them, say goodbye and close_call with outcome no_promise and reason refused.

CONFIRMING AND RECORDING
8. When amount and date are agreed, read back the product, the amount and the date (say the date as weekday, day and month) and ask "Do you confirm?". Call create_promise only after a clear yes, with promise_amount as a number and promise_date as YYYY-MM-DD.
9. If they change the date or the amount at the read-back, check the new value with rules 3 to 6 and read back again.
10. Never say the promise is recorded before create_promise returns ok true. Call create_promise only once; never in the same message as close_call.

OTHER SITUATIONS (these come before rule 7)
11. Illness, a death in the family or job loss: say one short sentence of sympathy and make NO persuasion attempt. Offer to connect them with the customer support team. If they accept, call transfer_to_agent with queue support. Do NOT call close_call before or together with the transfer. If they decline, thank them, goodbye and close_call with outcome no_promise and reason hardship. If they ask for a discount, say you cannot offer one. If they themselves name a date or an amount, handle it as a promise with rules 3 to 10.
12. "I already paid" (this past-due amount): say that payments can take one business day to appear. Take no promise. Thank them, goodbye, close_call with outcome paid.
13. "This debt is not mine", "I never had this product", or the product was closed or paid off in the past: do not argue, take no promise. Thank them, goodbye, close_call with outcome no_promise and reason dispute.
14. A request for a new payment plan or lower instalments: do not describe any plan. Say you will transfer them to the team that handles this, call transfer_to_agent with queue restructuring. Do NOT call close_call before or together with the transfer.
15. A request for a human: ask once whether you can help first. If they insist, call transfer_to_agent with queue general. Do NOT call close_call before or together with the transfer.
16. The customer says twice that they do not want to talk: thank them, goodbye, close_call with outcome no_promise and reason refused.
17. A transfer is always done with transfer_to_agent alone; the call is closed after the transfer result."""

ACCOUNT_ERROR_TASK = """The account details could not be loaded. Tell the customer you cannot access the account details right now and that they can use the Freya Bank mobile app. Do not state any amount. Thank them, say goodbye and call close_call with outcome no_promise and reason service_error."""

# ---------------------------------------------------------------- nodes
verify_tool = api_tool("verify_identity", "Check one security answer. CALL it every time the customer gives an answer to a security question.",
    {"field": {"description": "Which question was answered: date_of_birth or phone_last4."},
     "answer": {"description": "date_of_birth as YYYY-MM-DD (convert from words; numbers only = day, month, year). phone_last4 as four digits."}},
    post=VERIFY_POST)

callback_tool = api_tool("schedule_callback", "Book a callback for the customer. CALL it when the person on the line gives a day and time inside the allowed callback window.",
    {"date": {"description": "Callback date as YYYY-MM-DD.", "var": "callback_date"},
     "time": {"description": "Callback time as HH:MM, 24 hour.", "var": "callback_time"}},
    post=GENERIC_POST.replace("result.pop('header', None)", "result.pop('header', None)\n        d = result.get('data')\n        if isinstance(d, dict):\n            d['what_to_do_next'] = 'Callback booked. Thank them, say goodbye and call close_call with outcome not_reached and reason callback.' if d.get('ok') is True else 'The callback was NOT booked. Do not say it is booked.'"))

promise_tool = api_tool("create_promise", "Record the payment promise. CALL it only after the customer said a clear yes to your read-back of product, amount and date. Never call it twice after a success.",
    {"product_id": {"default": "{{product_id}}"},
     "amount": {"description": "Agreed amount, a number such as 637.5.", "var": "promise_amount"},
     "date": {"description": "Agreed payment date as YYYY-MM-DD.", "var": "promise_date"}},
    pre=PROMISE_PRE, post=PROMISE_POST)

retry_promise_tool = api_tool("create_promise", "Record the promise again. CALL it now, once. All values are filled in.",
    {"product_id": {"default": "{{product_id}}"},
     "amount": {"default": "{{p_amount}}"},
     "date": {"default": "{{p_date}}"}},
    pre=PROMISE_PRE, post=PROMISE_POST)

account_tool_pre = api_tool("get_account", "Get the late products.", {}, retries=2)

workflow = {
    "initial_node": "setup",
    "system_prompt": SYSTEM,
    "test_variables": {"customer_id": "c_test", "first_name": "Daniel", "last_name": "Reyes", "title": "Mr", "now": "2026-10-14T11:00"},
    "global_nodes": {},
    "nodes": {
        "setup": code_node("setup", SETUP_CODE, "opening"),
        "opening": {
            "name": "opening", "node_type": "conversation",
            "on_enter": {"type": "tts", "text": OPENING_LINE}, "on_enter_first_entry_only": True,
            "task_message": OPENING_TASK,
            "tools": [callback_tool,
                      transfer_tool(["general"], "CALL it only when the person insists on a human after you asked once whether you can help."),
                      close_tool(["not_reached", "not_verified"], ["callback", "no_callback", "wrong_number", "deceased", "unclear", "declined"],
                                 "not_reached: callback, no_callback, wrong_number, deceased, unclear. not_verified: declined.")],
            "extraction_schema": [{"name": "party", "type": "string", "enum_values": ["customer", "other"],
                                   "description": "customer: the person on the line clearly confirmed being {{title}} {{last_name}}, by saying yes, that's me, speaking, or their own name. other: anything else (hello, go ahead, who is this, a question, someone else, unclear).",
                                   "min_confidence": 0.6}],
            "max_node_turns": 30,
            "transitions": [
                TRANSFER_OK,
                CLOSED,
                {"target_node": "identity", "description": "The customer confirmed who they are",
                 "condition": {"logic": "or", "conditions": [{"variable": "party", "operator": "equals", "operand": "customer"}]}},
            ],
        },
        "identity": {
            "name": "identity", "node_type": "conversation",
            "on_enter": {"type": "tts", "text": IDENTITY_LINE}, "on_enter_first_entry_only": True,
            "task_message": IDENTITY_TASK,
            "tools": [verify_tool,
                      transfer_tool(["general"], "CALL it only when the customer insists on a human after you asked once whether you can help."),
                      close_tool(["not_verified"], ["failed", "declined"],
                                 "not_verified: failed (three wrong answers) or declined (would not answer).")],
            "max_node_turns": 30,
            "transitions": [
                {"target_node": "load_account", "description": "Both security answers verified",
                 "condition": {"logic": "or", "conditions": var_true("verify_identity_response.data.all_verified")}},
                TRANSFER_OK,
                CLOSED,
            ],
        },
        "load_account": {
            "name": "load_account", "node_type": "api_request", "passthrough": True, "passthrough_default_transition": "account_facts",
            "on_enter": {"type": "silent"}, "disable_inactivity_monitor": True,
            "pre_actions": [account_tool_pre],
            "response_mapping": [{"path": "data.products", "variable": "products"}],
            "transitions": [{"target_node": "account_facts", "description": "always", "condition": {"operator": "always"}}],
        },
        "account_facts": code_node("account_facts", ACCOUNT_CODE, "negotiate"),
        "negotiate": {
            "name": "negotiate", "node_type": "conversation",
            "on_enter": {"type": "tts", "text": "{{debt_statement}}"}, "on_enter_first_entry_only": True,
            "task_message": NEGOTIATE_TASK,
            "tools": [promise_tool,
                      transfer_tool(["restructuring", "support", "general"], "restructuring: the customer asks for a new payment plan or lower instalments. support: a customer in hardship accepted your offer of the support team. general: the customer insists on a human after you asked once whether you can help."),
                      close_tool(["paid", "no_promise"], ["refused", "hardship", "dispute"],
                                 "paid: customer already paid (no reason). no_promise: refused, hardship or dispute. Never call it when a transfer is needed: use transfer_to_agent instead.")],
            "max_node_turns": 60,
            "transitions": [PROMISE_OK, PROMISE_FAIL, TRANSFER_OK, CLOSED],
        },
        "retry_promise": {
            "name": "retry_promise", "node_type": "conversation",
            "on_enter": {"type": "llm", "prompt": "The first attempt to record the promise failed, nothing is recorded yet. In one message, tell the customer the system could not record the promise and that you are trying once more, and call create_promise now. Do not say it is recorded."},
            "task_message": "The first attempt to record the promise failed. Call create_promise once more right now (all values are already filled in). Never say the promise is recorded unless the result says ok true. No other tool in this turn.",
            "tools": [retry_promise_tool],
            "max_node_turns": 5,
            "max_turn_transition": "promise_failed",
            "transitions": [PROMISE_OK,
                {"target_node": "promise_failed", "description": "The retry failed too",
                 "condition": {"logic": "or", "conditions": [{"variable": "create_promise_failure_count", "operator": "greater_than", "operand": 1},
                     {"variable": "promise_failures", "operator": "greater_than", "operand": 1},
                     {"logic": "and", "conditions": [{"variable": "create_promise_failure_count", "operator": "greater_than", "operand": 0},
                                                     {"variable": "promise_failures", "operator": "greater_than", "operand": 0}]}]}}],
        },
        "promise_done": {
            "name": "promise_done", "node_type": "conversation",
            "on_enter": {"type": "tts", "text": "Your promise is recorded. You will receive a text message confirming this promise. Is there anything else I can help you with?"}, "on_enter_first_entry_only": True,
            "task_message": PROMISE_DONE_TASK,
            "tools": [transfer_tool(["restructuring", "support", "general"], "restructuring: new payment plan or lower instalments. support: hardship and the customer accepted the support team. general: the customer insists on a human after you asked once whether you can help."),
                      close_tool(["promise"], ["none"], "outcome promise.")],
            "max_node_turns": 20,
            "transitions": [TRANSFER_OK, CLOSED],
        },
        "promise_failed": {
            "name": "promise_failed", "node_type": "conversation",
            "on_enter": {"type": "llm", "prompt": "Tell the customer that you could not record the promise because of a technical problem, so nothing is recorded. Thank them, say goodbye, and in the same message call close_call with outcome no_promise and reason service_error."},
            "task_message": "The promise could NOT be recorded (two failed attempts). Never say it is recorded. Tell the customer you could not record it because of a technical problem, thank them, say goodbye and call close_call with outcome no_promise and reason service_error.",
            "tools": [close_tool(["no_promise"], ["service_error"], "")],
            "max_node_turns": 10,
            "transitions": [CLOSED],
        },
        "after_transfer": {
            "name": "after_transfer", "node_type": "conversation",
            "on_enter": {"type": "llm", "prompt": "The transfer to a colleague is done. Tell the customer a colleague will take over now, thank them, say goodbye, and in the same message call close_call with outcome transferred."},
            "task_message": "The transfer is done. Tell the customer a colleague will take over, thank them, say goodbye and call close_call with outcome transferred and the reason that matches the queue (restructuring, support, or human for the general queue).",
            "tools": [close_tool(["transferred"], ["restructuring", "human", "support"], "")],
            "max_node_turns": 10,
            "transitions": [CLOSED],
        },
        "account_error": {
            "name": "account_error", "node_type": "conversation",
            "task_message": ACCOUNT_ERROR_TASK,
            "tools": [close_tool(["no_promise"], ["service_error"], "")],
            "transitions": [CLOSED],
        },
        "end": {
            "name": "end", "node_type": "end_call", "on_enter": {"type": "silent"}, "disable_inactivity_monitor": True,
            "transitions": [],
            "terminal": {"type": "function", "function": {"name": "call_closed", "handler": "__function__:end_call",
                         "parameters": {"type": "object", "properties": {"message_mode": {"type": "string", "default": "none"}}}}},
        },
    },
}
# route a failed account load away from negotiation
workflow["nodes"]["account_facts"]["transitions"].insert(0, {"target_node": "account_error", "description": "account could not be loaded",
    "condition": {"logic": "or", "conditions": [{"variable": "account_state", "operator": "equals", "operand": "error"}]}})

out = ROOT / "out" / "brief-a.workflow.json"
json.dump(workflow, open(out, "w"), indent=1)
print("wrote", out)
