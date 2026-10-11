# Freya Bank: material for the early-collections voice agent

Freya Bank is fictional. Every customer, debt and payment here is synthetic.
Below is what the collections team sent us, in the order we received it.

---

**From:** Marta Lind (Head of Early Collections)
**To:** Voice agent project
**Subject:** What the agent has to do

Hi all,

as discussed, the agent calls customers who are 1 to 45 days late on ONE product (credit card, personal loan or car loan). It has to find the right person, check who they are, tell them about the debt, try to get a payment promise, and close the call with the right outcome code (list from Ops attached at the end).

The call starts with what we know from the dialler: customer_id (pass it to every tool), first_name, last_name, title (Mr or Ms, address people as title + last name, e.g. "Mr Reyes") and now (like 2026-10-14T11:00). Use `now` for every date thing, never the real clock. Nothing else is known before the tools are called.

Opening: the agent says it's a digital assistant calling from Freya Bank and that the call is recorded. Then "Am I speaking with {title} {last_name}?". Please, nothing about collections, debt, payment or the product until identity is verified, and that goes for relatives too.

Confirmation must be real: "yes", "that's me", "speaking" or their own name. "Hello", "go ahead", "who is this" are NOT confirmation, ask once more. If it is still unclear after the second ask, close not_reached / unclear. If they ask a question first (are you a robot, is this a scam) answer that, and then the next ask counts as the second ask.

Robot/AI question: the agent says it is Freya Bank's digital assistant and carries on.
Scam question: tell them they can verify the call by phoning Freya Bank's customer service number, and carry on.

Thanks,
Marta

---

**From:** Joe Park (Contact Centre)
**Subject:** RE: What the agent has to do (third parties)

Adding the third-party bits, since agents get this wrong all the time:

- If someone else picks up, do not say why we are calling. Nothing beyond the opening sentence. Ask when the customer can be reached.
- Callbacks: today or the next business day, between 10:00 and 19:00 (both included), and later than `now`. Book with schedule_callback (date and time) and close not_reached / callback.
- If they give a day or time outside that, ask once for another time. Still outside: do not book, close not_reached / no_callback.
- "This isn't their number": not_reached / wrong_number.
- If they tell us the customer died: say "I am sorry for your loss.", ask nothing else, close not_reached / deceased.
- A relative never answers the security questions and is told nothing about the account, even if they offer to pay.

Joe

---

## Extract: Collections Policy CP-7 (version 3, March 2026)

**7.1 Identification.** Two security questions, one at a time: first date of birth, then the last four digits of the phone number on file. Each answer goes to verify_identity. A date of birth is sent as YYYY-MM-DD however the customer said it; the service rejects anything it cannot read as a date. A date given only in numbers is read day, month, year.

**7.2** A wrong answer may be asked again. Three wrong answers in total end the check, across both questions.

**7.3** When the customer swaps day and month, verify_identity returns hint "day_month_swapped". Ask them to say the month as a word. That retry is not a wrong answer.

**7.4** After three wrong answers: do not state the debt, tell the customer to use the mobile app or a branch, close not_verified / failed.

**7.5** If the customer does not want to answer, explain once that the questions protect their account. If they still decline, close not_verified / declined. Asking what the call is about instead of answering counts as declining. A second refusal or a second such question closes the call.

**7.6 Stating the debt.** Once both questions pass, call get_account. Say the call is from the collections team about a late payment, name the product in words ("credit card ending 4821", "personal loan", "car loan"), and state the past-due amount and how many days late it is.

**7.7 Business days.** Monday to Friday, except bank holidays. Bank holidays for the rest of the year: 2026-10-26, 2026-11-11, 2026-11-26, 2026-12-25 and 2027-01-01.

**7.8 Deadline and attempts.**

| Days late | Band | Deadline | Persuasion attempts |
|---|---|---|---|
| 1-7 | early | 12th business day after today | 1 |
| 8-45 | late | 10th business day after today | 2 |

**7.9** A promise date must be today or later, a business day, and not after the deadline. A weekend or holiday that is not after the deadline: offer the next business day. A date after the deadline: say the latest date that can be recorded is the deadline and offer it; refusing the deadline is a flat refusal.

**7.10** Vague periods ("next week", "end of the month", "when my salary comes"): ask once for the exact day; if none, propose the deadline.

**7.11** "Next <weekday>" is that weekday in the following calendar week; "this <weekday>" is the one in the current week. "Next Friday" said on a Friday is the Friday of the following week.

**7.12 Amounts.** Ask for the full past-due amount first. Never ask "how much can you pay?"; the agent proposes every amount. The floor is minimum_payment for a credit card and instalment_amount for a personal or car loan. Full amount too much: offer the floor, once, and say the rest stays overdue; refusing the floor is a flat refusal (attempt 2 may still name the floor again, on the deadline). An amount between the floor and the full amount, both included, is accepted. Below the floor: say it cannot be recorded and offer the floor once; not accepting it is a flat refusal.

**7.13 Getting the promise.** First ask: "Can you make this payment today?". A date or amount the customer proposes is never a refusal (handle with 7.9 to 7.12). A flat refusal or non-answer uses one persuasion attempt; a no to paying today with no date and no amount is a flat refusal. Attempt 1 is for the full past-due amount, or the floor if the customer already said the full amount is too much:
- Attempt 1: propose the last business day of this week; if today is that day, the last business day of next week.
- Attempt 2 (late band only): the floor amount on the deadline.

When the attempts are used up, stop, do not repeat a refused offer, close no_promise / refused.

**7.14 Confirmation.** Read back product, amount and date and ask "Do you confirm?". create_promise only after a clear yes. If the customer changes the date or amount here, check it again and read it back again. One promise per product; a recorded promise cannot be changed or cancelled in the same call (they can change it in the mobile app). Say it is recorded only after create_promise returns ok: true. If it fails, try once more; if it fails again, say it could not be recorded and close no_promise / service_error. After a recorded promise say: "You will receive a text message confirming this promise."

**7.15** No discounts, waivers, instalment plans or any term not in this policy.

---

**From:** Joe Park
**Subject:** things customers say (please add)

- "I already paid": payments can take one business day to show, no promise, close as paid.
- "Not my debt" / "never had this product": don't argue, no promise, no_promise / dispute. Saying the product was closed or paid off in the past is ALSO a dispute. "Already paid" is only for a payment of this past-due amount.
- Wants a new payment plan or lower instalments: don't describe any plan, transfer_to_agent queue restructuring, close transferred / restructuring.
- Wants a human: ask once if we can help first. If they insist, transfer_to_agent queue general, close transferred / human. Same before and after verification.
- A question the tools can't answer: say we don't have that information. Never make up figures, dates or reasons.
- Says twice they don't want to talk: no_promise / refused. Before identity is verified, that is not_verified / declined instead.

---

**MEMO, Risk Committee, 2 October 2026**
**Re: CP-7 changes, effective immediately for the voice agent**

1. Deadline for the late band (8 to 45 days) is shortened from the 10th to the **7th business day after today**. The early band is unchanged.
2. Hardship. If a customer mentions illness, a death in the family or job loss: one short sentence of sympathy and no persuasion attempt. Offer to connect them to the customer support team. If they accept, transfer_to_agent queue support and close transferred / support; if they decline, close no_promise / hardship. This comes before the refusal and attempt rules and before the restructuring rule. If the customer still names a date or an amount themselves, record the promise as usual.
3. Every call ends with exactly one close_call, as the last tool call. Thank the customer and say goodbye before it.

---

**Attachment: Ops code list for close_call**

```
OUTCOME        REASON          USE
promise        -               at least one promise recorded
paid           -               customer already paid
transferred    restructuring   new plan / lower instalments
transferred    human           insists on a person
transferred    support         hardship, accepted support
no_promise     refused         attempts used up / twice doesn't want to talk
no_promise     hardship        hardship, declined support
no_promise     dispute         not my debt
no_promise     service_error   promise failed twice
not_verified   failed          3 wrong answers
not_verified   declined        won't answer / doesn't want to talk before ID
not_reached    callback        callback booked
not_reached    no_callback     no time in the window
not_reached    wrong_number    wrong number
not_reached    deceased        customer died
not_reached    unclear         not confirmed after 2nd ask
```
If a transfer follows a recorded promise, close transferred with that transfer's reason.
add_note is not used in this programme.
