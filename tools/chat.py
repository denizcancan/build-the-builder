"""Chat with a phone agent in the terminal, as one of the practice customers.

    python3 tools/chat.py <workflow_id> [case_id]        # case_id defaults to PUB-01

Creates a fresh bank session for the case, prints who you are and your security answers,
then sends each line you type to the workflow. Type /quit to end. The bank's records are printed at the end.
"""
import json, pathlib, sys
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tools"))
import freya
from score import http, score_ledger

def main():
    if len(sys.argv) < 2: sys.exit(__doc__)
    wf, case_id = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "PUB-01")
    case = next(c for c in json.load(open(ROOT / "public-test-set.json")) if c["id"] == case_id)
    try:
        v = http("/v1/practice/sessions", {"case": case_id})["variables"]
    except Exception as e:
        sys.exit(f"Could not create a bank session (the bank test server may be down): {e}")
    print(f"\nYou are {v['title']} {v['first_name']} {v['last_name']}  ({case_id}: {case['title']})")
    print("Security answers:", ", ".join(f"{k} = {x}" for k, x in case["identity"].items()))
    print("Script:", case["script"])
    print("Products:", json.dumps(case["products"]), "\nType /quit to end.\n")
    call_id, msg = None, "Hello?"
    while True:
        body = {"workflowId": wf, "message": msg, "customerVariables": v}
        if call_id: body["callId"] = call_id
        try: r = freya.api("POST", "/api/v2/chat/message", body)
        except RuntimeError as e: print("  (error:", str(e)[:200], ")"); r = {}
        call_id = r.get("callId") or call_id
        print("AGENT:", r.get("response", "(no reply)"))
        msg = input("YOU:   ").strip()
        if msg == "/quit": break
    if call_id:
        try: freya.api("POST", "/api/v2/chat/end", {"callId": call_id})
        except RuntimeError: pass
    print("\ncall id:", call_id)
    try:
        led = http("/v1/practice/ledger/" + v["customer_id"])
    except Exception as e:
        sys.exit(f"Could not read the bank's records (the bank test server may be down): {e}")
    print("\nBank tool calls:", " > ".join(c["tool"] for c in led.get("calls", [])) or "(none)")
    print("Ledger check:", json.dumps(score_ledger(case, led)))

if __name__ == "__main__": main()
