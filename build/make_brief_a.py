"""Generate out/brief-a.workflow.json from Brief A and tools/openapi.json."""
import json, sys, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
API = json.load(open(ROOT / "tools/openapi.json"))
BASE = API["servers"][0]["url"]

def req_schema(tool):
    return API["paths"]["/" + tool]["post"]["requestBody"]["content"]["application/json"]["schema"]

# ---------------------------------------------------------------- tools
def api_tool(name, desc, fields, pre="", post="", mapping=None, retries=0, fname=None):
    """fields: dict field -> dict(description, enum, type). customer_id is baked.
    fname: the function name the model and the counters see (defaults to the bank's tool name)."""
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
            "name": fname or name,
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
        fail_msg = 'Three wrong answers: the identity check has ended. Do not state anything about the account, the debt or the product. Tell the customer to use the Freya Bank mobile app or visit a branch. The call then ends by itself.'
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

GENERIC_POST = r"""
try:
    if isinstance(result, dict):
        result.pop('header', None)
except Exception:
    pass
"""

# Write tools (create_promise, schedule_callback, transfer_to_agent, close_call) carry NO Python hooks.
# Hooks run on Freya's code sandbox. When the sandbox is unreachable, a pre-hook failure means the request is never
# sent, and a post-hook failure loses the answer of a request that did reach the bank, so the agent repeats it.
# Both were seen in practice runs. Routing reads the raw `<tool>_response.data.*` keys instead.

def var_true(v):
    return [{"variable": v, "operator": "equals", "operand": "true"}, {"variable": v, "operator": "equals", "operand": "True"}]

def var_false(v):
    return [{"variable": v, "operator": "equals", "operand": "false"}, {"variable": v, "operator": "equals", "operand": "False"}]

def go(target, desc, conds):
    return {"target_node": target, "description": desc, "condition": {"logic": "or", "conditions": conds}}

QUEUE_REASON = {"restructuring": "restructuring", "support": "support", "general": "human"}

def transfer_tool(queue, when):
    """One tool per queue, the queue baked in, so the model cannot pick a wrong one and no hook is needed."""
    return api_tool("transfer_to_agent", "Hand the call to the " + queue + " team. " + when + " Do not say the transfer is done before the result says ok true.",
                    {"queue": {"default": queue}}, fname="transfer_" + queue)

def transfer_routes(queues):
    return [go("end_" + QUEUE_REASON[q], "The transfer to " + q + " is done", var_true("transfer_" + q + "_response.data.ok")) for q in queues]

# Every way a call can end. The engine calls close_call itself, with the outcome and reason baked in, exactly once,
# after a fixed goodbye line. key: (outcome, reason, goodbye line)
ENDINGS = {
    "end_promise":       ("promise", None, "Thank you for your time, {{title}} {{last_name}}. Goodbye."),
    "end_paid":          ("paid", None, "Payments can take one business day to appear. Thank you for your time, {{title}} {{last_name}}. Goodbye."),
    "end_restructuring": ("transferred", "restructuring", "A colleague will take over now. Thank you, goodbye."),
    "end_support":       ("transferred", "support", "A colleague from the support team will take over now. Thank you, goodbye."),
    "end_human":         ("transferred", "human", "A colleague will take over now. Thank you, goodbye."),
    "end_refused":       ("no_promise", "refused", "Thank you for your time, {{title}} {{last_name}}. Goodbye."),
    "end_hardship":      ("no_promise", "hardship", "Thank you for your time, {{title}} {{last_name}}. Take care, goodbye."),
    "end_dispute":       ("no_promise", "dispute", "Thank you for your time, {{title}} {{last_name}}. Goodbye."),
    "end_service_error": ("no_promise", "service_error", "I am sorry, I could not record your promise because of a technical problem, so nothing is recorded. Thank you for your time. Goodbye."),
    "end_account_error": ("no_promise", "service_error", "I am sorry, I cannot access the account details right now. You can use the Freya Bank mobile app. Thank you for your time. Goodbye."),
    "end_failed":        ("not_verified", "failed", "I am sorry, I could not verify your identity, so I cannot continue. Please use the Freya Bank mobile app or visit a branch. Thank you, goodbye."),
    "end_declined":      ("not_verified", "declined", "Thank you for your time. Goodbye."),
    "end_callback":      ("not_reached", "callback", "Thank you, we will call back then. Goodbye."),
    "end_no_callback":   ("not_reached", "no_callback", "Thank you for your time. Goodbye."),
    "end_wrong_number":  ("not_reached", "wrong_number", "I am sorry for the trouble. Goodbye."),
    "end_deceased":      ("not_reached", "deceased", "I am sorry for your loss. Goodbye."),
    "end_unclear":       ("not_reached", "unclear", "Thank you for your time. Goodbye."),
}

def ending_node(name):
    outcome, reason, line = ENDINGS[name]
    fields = {"outcome": {"default": outcome}}
    if reason:
        fields["reason"] = {"default": reason}
    close = api_tool("close_call", "Close the call record.", fields, retries=2)   # a 500 records nothing, so a repeat is safe
    return {
        "name": name, "node_type": "api_request", "passthrough": True, "passthrough_default_transition": "end",
        "on_enter": {"type": "tts", "text": line}, "disable_inactivity_monitor": True,
        "pre_actions": [close],
        "transitions": [{"target_node": "end", "description": "always", "condition": {"operator": "always"}}],
    }

def end_by_model(name, when):
    """A transition the dialogue model triggers by calling transition_to_<name>. It says the goodbye for the model."""
    return {"target_node": name, "description": when, "is_tool_call": True,
            "condition": {"operator": "matches_intent", "description": when}}

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

for c in (SETUP_CODE, ACCOUNT_CODE, VERIFY_POST, GENERIC_POST):
    assert "{{" not in c and "}}" not in c
    compile(c, "x", "exec")

def code_node(name, code, target, extra=()):
    """A code step. If Freya's code sandbox fails, the step runs again, up to three tries."""
    retry = {"target_node": name, "description": "The code step failed, run it again",
             "condition": {"logic": "or", "conditions": [{"logic": "and", "conditions": [
                 {"variable": name + "_success_count", "operator": "is_not_set"},
                 {"variable": name + "_failure_count", "operator": "less_than", "operand": 3}]}]}}
    return {
        "name": name, "node_type": "code", "passthrough": True, "passthrough_default_transition": target,
        "on_enter": {"type": "silent"}, "disable_inactivity_monitor": True,
        "pre_actions": [{"type": "function", "function": {"name": "code_exec", "handler": "__function__:code_exec",
            "parameters": {"type": "object", "properties": {"code": {"type": "string", "default": code}, "timeout_ms": {"type": "number", "default": 10000}}}}}],
        "transitions": [retry] + list(extra) + [{"target_node": target, "description": "always", "condition": {"operator": "always"}}],
    }

PROMISE_OK = go("promise_done", "The promise was recorded", var_true("create_promise_response.data.ok"))
PROMISE_FAIL = go("retry_promise", "The first create_promise call failed",
                  [{"variable": "create_promise_failure_count", "operator": "greater_than", "operand": 0}] + var_false("create_promise_response.data.ok"))

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
- Never say something is done, booked or transferred unless the tool result says ok true.
- Ending the call: when a rule says to end the call, call the transition_to_end_... tool it names. That tool thanks the customer and says goodbye for you, so do not say goodbye yourself. You may say one short sentence in the same message, then call the tool."""

OPENING_LINE = "Hello, this is the digital assistant of Freya Bank calling. This call is recorded. Am I speaking with {{title}} {{last_name}}?"

OPENING_TASK = """The opening line was already said: you introduced yourself as Freya Bank's digital assistant, said the call is recorded, and asked "Am I speaking with {{title}} {{last_name}}?".
Your job in this step: find out whether you are speaking with {{title}} {{first_name}} {{last_name}}.
STRICT: until identity is verified, say nothing about why you are calling. Never mention collections, debt, payment, money, an account or a product, to anyone, including a relative who asks or offers to pay. If asked what it is about, say you can only discuss it with {{title}} {{last_name}} in person.

1. Only "yes", "that's me", "speaking" or the customer's own name counts as confirmation. "Hello", "go ahead", "who is this" or any question do not.
2. Not confirmed: answer any question first (for example "who is this": you are Freya Bank's digital assistant), then ask again "Am I speaking with {{title}} {{last_name}}?". That is the second and last ask. If the reply to the second ask is still not a confirmation and none of the cases below, call transition_to_end_unclear.
3. Someone else answers (a relative, a colleague, anyone who is not the customer): do not say why you call, ask no security questions, tell them nothing about any account, even if they offer to pay. Ask when {{title}} {{last_name}} can be reached.
   Allowed callback times: {{callback_facts}}
   - Day and time inside the window: call schedule_callback with date as YYYY-MM-DD and time as HH:MM (24 hour; "around eleven in the morning" is 11:00). Call it once. When it returns ok true the call ends by itself.
   - Only a day or only a vague time: ask for the missing part.
   - Outside the window: say that time is not possible and ask once for another time today or the next business day between 10:00 and 19:00. If the next answer is also outside, do not schedule: call transition_to_end_no_callback. Same if they do not know or will not say.
4. The person says the number does not belong to the customer: call transition_to_end_wrong_number.
5. The person says the customer has died: ask nothing else and call transition_to_end_deceased right away. It says "I am sorry for your loss." for you.
6. A request for a human: ask once whether you can help first. If they insist, call transfer_general.
7. The customer says they do not want to talk: say it will only take a moment and ask again. If they say it a second time, call transition_to_end_declined.
Do not ask any security question in this step."""

IDENTITY_LINE = "Thank you. Before we continue I need to ask two security questions. What is your date of birth?"

IDENTITY_TASK = """{{title}} {{last_name}} confirmed who they are. You already asked for the date of birth. Now check identity with two security questions, one at a time: first the date of birth, then the last four digits of the phone number on file.
STRICT: until both answers pass, say nothing about why you are calling: no collections, debt, payment, amount or product. If asked what it is about, say you can explain right after the security questions.

- For every answer the customer gives, call verify_identity.
  - field date_of_birth: answer as YYYY-MM-DD. Convert whatever the customer says ("fourteenth of March eighty-eight" -> 1988-03-14). A date given only as numbers is day, then month, then year ("3/4/1990" -> 1990-04-03). If the customer corrects themself, use the corrected answer.
  - field phone_last4: exactly four digits. If they give the digits in pieces, wait until you have all four.
- Follow what_to_do_next in each result. Do not tell the customer they are verified; the next step does that.
- A wrong answer may be asked again. After the third wrong answer in total the call ends by itself; never state anything about the account.
- The customer does not want to answer, or asks what the call is about instead of answering: explain once that these questions protect their account, and ask again. If they decline again or ask again, call transition_to_end_declined.
- The customer says twice that they do not want to talk: call transition_to_end_declined.
- A request for a human: ask once whether you can help first. If they insist, call transfer_general."""

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
   When the allowed attempts are used up and the customer still refuses: stop. Do not repeat an offer they already refused. Call transition_to_end_refused.

CONFIRMING AND RECORDING
8. When amount and date are agreed, read back the product, the amount and the date (say the date as weekday, day and month) and ask "Do you confirm?". Call create_promise only after a clear yes, with promise_amount as a number and promise_date as YYYY-MM-DD.
9. If they change the date or the amount at the read-back, check the new value with rules 3 to 6 and read back again.
10. Call create_promise once. Never say the promise is recorded yourself: the next step says it once the bank confirms it. If it fails, the next step tries again.

OTHER SITUATIONS (these come before rule 7)
11. Illness, a death in the family or job loss: say one short sentence of sympathy and make NO persuasion attempt. Offer to connect them with the customer support team. If they accept, call transfer_support. If they decline, call transition_to_end_hardship. If they ask for a discount, say you cannot offer one. If they themselves name a date or an amount, handle it as a promise with rules 3 to 10.
12. "I already paid" (this past-due amount): take no promise and call transition_to_end_paid. It tells them that payments can take one business day to appear.
13. "This debt is not mine", "I never had this product", or the product was closed or paid off in the past: do not argue, take no promise, call transition_to_end_dispute.
14. A request for a new payment plan or lower instalments: do not describe any plan. Say you will transfer them to the team that handles this and call transfer_restructuring.
15. A request for a human: ask once whether you can help first. If they insist, call transfer_general.
16. The customer says twice that they do not want to talk: call transition_to_end_refused."""

PROMISE_DONE_LINE = "Your promise is recorded. You will receive a text message confirming this promise. Is there anything else I can help you with?"

PROMISE_DONE_TASK = """A payment promise is recorded and you already told the customer, including that a text message will confirm it.
One promise only: it cannot be changed or cancelled in this call; if asked, say they can change it in the mobile app. Do not take another promise.
If they ask for a new payment plan or lower instalments, call transfer_restructuring. If they insist on a human after you asked once whether you can help, call transfer_general.
Otherwise, when nothing else is needed, call transition_to_end_promise."""

# ---------------------------------------------------------------- nodes
verify_tool = api_tool("verify_identity", "Check one security answer. CALL it every time the customer gives an answer to a security question.",
    {"field": {"description": "Which question was answered: date_of_birth or phone_last4."},
     "answer": {"description": "date_of_birth as YYYY-MM-DD (convert from words; numbers only = day, month, year). phone_last4 as four digits."}},
    post=VERIFY_POST)

callback_tool = api_tool("schedule_callback", "Book a callback for the customer. CALL it once, when the person on the line gives a day and time inside the allowed callback window.",
    {"date": {"description": "Callback date as YYYY-MM-DD.", "var": "callback_date"},
     "time": {"description": "Callback time as HH:MM, 24 hour.", "var": "callback_time"}})

promise_tool = api_tool("create_promise", "Record the payment promise. CALL it once, only after the customer said a clear yes to your read-back of product, amount and date.",
    {"product_id": {"default": "{{product_id}}"},
     "amount": {"description": "Agreed amount, a number such as 637.5.", "var": "promise_amount"},
     "date": {"description": "Agreed payment date as YYYY-MM-DD.", "var": "promise_date"}})

# The retry is run by the engine with the values of the first try, so the model cannot skip it or change them.
retry_promise_tool = api_tool("create_promise", "Record the promise again.",
    {"product_id": {"default": "{{product_id}}"},
     "amount": {"default": "{{create_promise_args.promise_amount}}"},
     "date": {"default": "{{create_promise_args.promise_date}}"}})

account_tool_pre = api_tool("get_account", "Get the late products.", {}, retries=2)

def conversation(name, task, tools, transitions, on_enter=None, turns=30):
    n = {"name": name, "node_type": "conversation", "task_message": task, "tools": tools,
         "max_node_turns": turns, "transitions": transitions}
    if on_enter:
        n["on_enter"] = on_enter
        n["on_enter_first_entry_only"] = True
    return n

nodes = {
    "setup": code_node("setup", SETUP_CODE, "opening"),
    "opening": conversation("opening", OPENING_TASK,
        [callback_tool, transfer_tool("general", "CALL it only when the person insists on a human after you asked once whether you can help.")],
        [go("end_callback", "The callback is booked", var_true("schedule_callback_response.data.ok"))]
        + transfer_routes(["general"])
        + [go("identity", "The customer confirmed who they are", [{"variable": "party", "operator": "equals", "operand": "customer"}]),
           end_by_model("end_unclear", "Still not clear who is on the line after the second ask"),
           end_by_model("end_no_callback", "No callback time inside the window after asking twice, or the person will not say"),
           end_by_model("end_wrong_number", "The number does not belong to the customer"),
           end_by_model("end_deceased", "The person says the customer has died"),
           end_by_model("end_declined", "The person said twice that they do not want to talk")],
        on_enter={"type": "tts", "text": OPENING_LINE}),
    "identity": conversation("identity", IDENTITY_TASK,
        [verify_tool, transfer_tool("general", "CALL it only when the customer insists on a human after you asked once whether you can help.")],
        [go("load_account", "Both security answers verified", var_true("verify_identity_response.data.all_verified")),
         go("end_failed", "Three wrong answers", [{"variable": "verify_identity_response.data.wrong_answers_left", "operator": "equals", "operand": "0"},
                                                  {"variable": "id_failed", "operator": "equals", "operand": "yes"}])]
        + transfer_routes(["general"])
        + [end_by_model("end_declined", "The customer declined the security questions twice, or said twice that they do not want to talk")],
        on_enter={"type": "tts", "text": IDENTITY_LINE}),
    "load_account": {
        "name": "load_account", "node_type": "api_request", "passthrough": True, "passthrough_default_transition": "account_facts",
        "on_enter": {"type": "silent"}, "disable_inactivity_monitor": True,
        "pre_actions": [account_tool_pre],
        "response_mapping": [{"path": "data.products", "variable": "products"}],
        "transitions": [{"target_node": "account_facts", "description": "always", "condition": {"operator": "always"}}],
    },
    "account_facts": code_node("account_facts", ACCOUNT_CODE, "negotiate",
        extra=[go("end_account_error", "The account could not be loaded", [{"variable": "account_state", "operator": "equals", "operand": "error"}])]),
    "negotiate": conversation("negotiate", NEGOTIATE_TASK,
        [promise_tool,
         transfer_tool("restructuring", "CALL it when the customer asks for a new payment plan or lower instalments."),
         transfer_tool("support", "CALL it when a customer in hardship accepted your offer of the support team."),
         transfer_tool("general", "CALL it only when the customer insists on a human after you asked once whether you can help.")],
        [PROMISE_OK, PROMISE_FAIL] + transfer_routes(["restructuring", "support", "general"])
        + [end_by_model("end_refused", "The customer refused and the allowed persuasion attempts are used up, or said twice they do not want to talk"),
           end_by_model("end_hardship", "A customer in hardship declined the support team and named no date or amount"),
           end_by_model("end_dispute", "The customer disputes the debt"),
           end_by_model("end_paid", "The customer says they already paid this past-due amount")],
        on_enter={"type": "tts", "text": "{{debt_statement}}"}, turns=60),
    "retry_promise": {
        "name": "retry_promise", "node_type": "api_request", "passthrough": True, "passthrough_default_transition": "end_service_error",
        "on_enter": {"type": "tts", "text": "I am sorry, the system did not record that yet. I am trying once more."}, "disable_inactivity_monitor": True,
        "pre_actions": [retry_promise_tool],
        "response_mapping": [{"path": "data.ok", "variable": "retry_ok"}],
        "transitions": [go("promise_done", "The retry recorded the promise", var_true("retry_ok")),
                        {"target_node": "end_service_error", "description": "The retry failed too", "condition": {"operator": "always"}}],
    },
    "promise_done": conversation("promise_done", PROMISE_DONE_TASK,
        [transfer_tool("restructuring", "CALL it when the customer asks for a new payment plan or lower instalments."),
         transfer_tool("support", "CALL it when a customer in hardship accepted your offer of the support team."),
         transfer_tool("general", "CALL it only when the customer insists on a human after you asked once whether you can help.")],
        transfer_routes(["restructuring", "support", "general"])
        + [end_by_model("end_promise", "Nothing else is needed after the recorded promise")],
        on_enter={"type": "tts", "text": PROMISE_DONE_LINE}, turns=20),
    "end": {
        "name": "end", "node_type": "end_call", "on_enter": {"type": "silent"}, "disable_inactivity_monitor": True,
        "transitions": [],
        "terminal": {"type": "function", "function": {"name": "call_closed", "handler": "__function__:end_call",
                     "parameters": {"type": "object", "properties": {"message_mode": {"type": "string", "default": "none"}}}}},
    },
}
nodes["opening"]["extraction_schema"] = [{"name": "party", "type": "string", "enum_values": ["customer", "other"],
    "description": "customer: the person on the line clearly confirmed being {{title}} {{last_name}}, by saying yes, that's me, speaking, or their own name. other: anything else (hello, go ahead, who is this, a question, someone else, unclear).",
    "min_confidence": 0.6}]
for k in ENDINGS:
    nodes[k] = ending_node(k)

# every transition target must exist
for k, n in nodes.items():
    for t in n.get("transitions", []):
        assert t["target_node"] in nodes, (k, t["target_node"])

workflow = {
    "initial_node": "setup",
    "system_prompt": SYSTEM,
    "test_variables": {"customer_id": "c_test", "first_name": "Daniel", "last_name": "Reyes", "title": "Mr", "now": "2026-10-14T11:00"},
    "global_nodes": {},
    "nodes": nodes,
}

out = ROOT / "out" / "brief-a.workflow.json"
json.dump(workflow, open(out, "w"), indent=1)
print("wrote", out, len(nodes), "steps")
