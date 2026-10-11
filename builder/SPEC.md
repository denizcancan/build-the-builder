# The spec: what you write from a bank's brief

You read a bank's brief (it may be a clean specification, or raw material: mails, a policy extract, a memo that
changes earlier rules, a code list) and the bank's tool API file (`openapi.json`). You write one JSON file, the
**spec**. Our generator turns the spec into a phone agent. You do not write the agent.

The generator already handles the mechanics of every collections call:
- the stages: opening (who is on the line), identity check, account loading, negotiation, wrap-up, endings;
- every tool's URL and wiring, one promise tool and one note tool per product, baked product ids;
- `close_call`: never called by the model. Each ending is a fixed step that says a goodbye line and closes with a
  baked outcome and reason;
- the date arithmetic (business days, holidays, deadlines, attempt offers, a calendar for the model);
- telling the customer what was recorded (from the bank's answers, never from the model);
- retries when a tool or a code step fails.

Your job is to put **every rule of the brief** into the spec, with the brief's own values and wording.
Most failed calls come from one rule that was skipped. When a memo or a later mail changes a rule, use the
changed rule. When a code list gives names (outcomes, reasons, queues), use exactly those names, and check them
against the enums in `openapi.json`.

Never copy values from an example spec. Every value comes from the brief or the tool file you were given.

## Rule text and references

Rule lists (`global_rules`, `opening_rules`, `identity_rules`, `negotiate_rules`, `after_promise_rules`) are
instructions for the phone agent's model, in English, one rule per string. Write them as instructions, keep the
brief's exact phrases where it gives them (for example a sentence the agent must say). Do not mention steps or nodes.
Inside rule text, refer to actions with these references. The generator replaces them with the right tool names:

| Reference | Meaning |
|---|---|
| `[[end:<ending key>]]` | end the call with that ending (its goodbye line is said for you) |
| `[[transfer:<queue>]]` | transfer the call to that queue (`<queue>` is a key of `transfers`) |
| `[[callback]]` | book the callback |
| `[[verify]]` | check a security answer |
| `[[promise]]` | record the promise of a product |
| `[[note]]` | record a note for a product without a promise (only when `notes.enabled`) |
| `[[wrap_up]]` | finish negotiating; the system then reports what was recorded and ends the call |

Text with `{{title}}`, `{{last_name}}` and other call variables is filled in per call.

A rule may only reference an ending or a transfer that is available in its stage (see `stages` below).

## Fields

```jsonc
{
 "name": "short-name",
 "language": "English",                  // the language the agent speaks
 "persona": "You are the digital assistant of <bank>, on an outbound phone call to a customer.",
 "call_variables": ["customer_id", "first_name", "last_name", "title", "now"],   // every call variable the brief lists
 "customer_id_variable": "customer_id",   // the call variable passed to every tool
 "now_variable": "now",                   // the call variable holding the current date and time
 "address": "{{title}} {{last_name}}",    // how to address the customer
 "full_name": "{{title}} {{first_name}} {{last_name}}",
 "test_variables": { ... },               // one example value per call variable

 "global_rules": [ ... ],                 // rules that hold in every stage (robot/AI question, scam question,
                                          // unknown information, no discounts or other terms, words forbidden
                                          // before identity is verified, ...)

 "opening": {
   "line": "...",                         // the agent's exact first sentence(s), ending with the confirmation question
   "confirmation": "..."                  // what counts as the customer confirming who they are, and what does not
 },
 "opening_rules": [ ... ],                // confirmation, second ask, third party, callback, wrong number, deceased,
                                          // not wanting to talk, human request, ... (everything before identity checks)

 "identity": {
   "first_line": "...",                   // what the agent says when the customer confirmed, ending with question 1
   "questions": [                          // in the order the brief asks them
     {"field": "<value of the field enum of verify_identity>", "ask": "<question>", "format": "<exactly how to send the answer>"}
   ],
   "result": {"passed": "...", "all_passed": "...", "left": "...", "hint": "..."},   // names of verify_identity's
                                          // response fields: this answer passed, all answers passed, wrong answers
                                          // left, hint. Take them from openapi.json.
   "hints": {"<hint value>": "<what the agent does>"}   // one entry per hint value in openapi.json
 },
 "identity_rules": [ ... ],               // wrong answers, retries, declining, what not to say, ...

 "callback": {"today": true, "next_business_days": 1, "start": "10:00", "end": "19:00", "after_now": true},
                                          // when a callback may be booked: today allowed?, how many following
                                          // business days, the time window (both ends included), later than now?
 "calendar": {"business_weekdays": [0,1,2,3,4], "holidays": ["YYYY-MM-DD", ...],   // 0 = Monday
              "non_business_date": "next"},  // customer names a weekend or holiday: offer the "next" business day
                                          // or the "previous" one (the business day before it)

 "account": {
   "products_path": "data.products",      // where get_account's product list is, starting with data.
   "fields": {"id": "...", "type": "...", "past_due": "...", "days_late": "..."},   // names in get_account's products
   "types": {                              // one entry per product type value in openapi.json
     "<type>": {"words": "how to name it, with {field} from the product, e.g. credit card ending {card_last4}",
                "floor_field": "<product field that is the lowest amount that may be recorded>",
                "floor_name": "<what the brief calls that amount>"}
   },
   "statement_intro": "...",              // said once verified, before the products (e.g. which team, why)
   "statement_product": "...",            // one sentence per product. Fields: {words} {days_late} {past_due} {floor} {floor_name} {deadline}
   "first_ask_one": "...",                // the first question when there is one product
   "first_ask_many": "..."                // the first question when there are several
 },
 "max_products": 3,                       // most products one customer can have
 "bands": [                                // by days late; the first matching band wins
   {"name": "...", "min_days": 1, "max_days": 7, "deadline": {"count": 12, "unit": "business_days"}, "attempts": 1}
 ],                                        // unit: business_days (the Nth business day after today) or calendar_days
                                          // calendar_days only: "if_not_business_day": "previous" (default) or "next",
                                          // which business day the deadline moves to when it falls on a weekend or holiday
 "attempts": [                             // the persuasion offers in order; a band with attempts N uses the first N
   {"amount": "full | floor | full_or_floor", "date": "today | next_business_day | end_of_week | deadline"}
 ],                                        // end_of_week: last business day of this week, or of next week if today is it
 "next_weekday": "following_week",        // "next Friday": following_week (the Friday of next calendar week) or
                                          // next_occurrence (the coming Friday)
 "promise_retries": 1,                    // how many times a failed promise call is repeated

 "negotiate_rules": [ ... ],              // everything after the debt is stated: order of questions, amounts, dates,
                                          // vague dates, floors, attempts, refusals, read-back and confirmation,
                                          // hardship, paid, dispute, restructuring, human, not wanting to talk,
                                          // several products, notes. End with [[wrap_up]] when notes are used.
 "promise": {
   "recorded_sentence": "... {amount} ... {date} ... {words} ...",   // said per recorded promise
   "failed_sentence": "... {words} ...",                             // said per promise that could not be recorded
   "after_record_sentence": "...",                                  // the brief's sentence after a recorded promise
   "anything_else": "Is there anything else I can help you with?",
   "change_answer": "..."                                           // what to say when asked to change a promise
 },
 "after_promise_rules": [ ... ],          // what may still happen after a promise is recorded

 "notes": {                                // set enabled false if the brief never uses add_note
   "enabled": true,
   "reasons": ["..."],                     // add_note reason values (openapi enum)
   "paid_reason": "paid",                  // the note reason meaning already paid
   "service_error_reason": "service_error",// the note reason the system uses for a failed promise
   "all_paid_ending": "paid",              // ending when every product is noted paid
   "reason_endings": {"<note reason>": "<ending key>"}   // ending when no promise: first non-paid note in product order
 },
 "fallback_ending": "...",                // ending when wrap-up finds nothing recorded or noted

 "transfers": {                            // one entry per queue value of transfer_to_agent
   "<queue>": {"ending": "<ending key>", "when": "when ... (the trigger, as an instruction)", "stages": ["opening", "identity", "negotiate", "after_promise"]}
 },

 "endings": {                              // every outcome/reason pair of the brief's outcome table
   "<key>": {"outcome": "...", "reason": "... or null", "line": "goodbye line said before closing",
             "stages": ["..."], "when": "..."}   // stages+when only for endings the model chooses
 },

 "tool_fields": {},                        // only if openapi.json renames request fields, e.g.
                                          // {"create_promise": {"amount": "sum"}}. Keys: verify_identity(field,answer),
                                          // create_promise(product,amount,date), add_note(product,reason),
                                          // schedule_callback(date,time), transfer_to_agent(queue), close_call(outcome,reason)
 "tool_constants": {}                     // fixed values for extra required request fields: {"<tool>": {"<field>": value}}
}
```

### Endings the generator needs

These ending keys must exist. The generator reaches them by itself:

| Key | When |
|---|---|
| `promise` | at least one promise was recorded |
| `service_error` | a promise could not be recorded after the retries |
| `id_failed` | no wrong identity answers left |
| `callback` | the callback was booked |

Optional: `account_error` (the account could not be loaded). Every transfer's ending must exist.
All other endings are chosen by the model: give them `stages` (where the model may choose them) and `when`
(one line, the trigger). With notes enabled, product-level reasons (refused, paid, ...) are usually notes, not
endings the model chooses: the wrap-up picks the ending from the notes.

The goodbye `line` must contain everything the brief wants said at that ending (for example a condolence
sentence, a pointer to the app or a branch, that payments take time to appear), then thanks and goodbye.

## Wording that the judge reads

A judge model reads every transcript against short checks such as "the agent does not say 'recorded' before a
successful call". It reads them word for word. So:
- Before a promise is recorded, never use the word "recorded" or "record", not even in the opening. If the brief
  wants the customer told that the call is recorded, say "we are recording this call".
- Do not use words the brief forbids before identity is verified anywhere in the opening line, the identity
  questions or answers to "what is this about?".
- When the brief says to do something once (ask once, offer once), the rule must also say what happens on the next
  turn, including when the customer does not answer the offer but asks something else: "Never offer it a second
  time. If they decline, or answer with anything else without accepting, ... and [[end:...]]".
- Use the brief's exact sentences where it gives them (for example the sentence after a recorded promise).

## Checklist before you finish

- Every numbered rule of the brief is in the spec, or is handled by the generator as described above.
- Changes from memos and later mails replaced the old values.
- Outcomes, reasons, queues, note reasons and identity fields are exactly the enum values in `openapi.json`.
- Holidays are complete and in `YYYY-MM-DD`.
- No line said before a promise is recorded contains "recorded" or "record".
- `python3 builder/validate.py <spec> <openapi.json>` prints `OK`.
