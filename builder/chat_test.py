"""Talk to a published workflow with a scripted caller and print what happened.

    python3 builder/chat_test.py <workflow_id> '<call variables as JSON>' "first caller line" "second caller line" ...

Prints each caller line, the agent's answer, then every tool call, tool result and step change of the call.
Used by the builder to test the agent it built.
"""
import json, pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import freya


def main():
    if len(sys.argv) < 4: sys.exit(__doc__)
    wf, variables, lines = sys.argv[1], json.loads(sys.argv[2]), sys.argv[3:]
    call_id = None
    for msg in lines:
        body = {"workflowId": wf, "message": msg, "customerVariables": variables}
        if call_id: body["callId"] = call_id
        try:
            r = freya.api("POST", "/api/v2/chat/message", body)
        except RuntimeError as e:
            print("CHAT ERROR:", str(e)[:400]); break
        call_id = r.get("callId") or call_id
        print("CALLER:", msg); print("AGENT: ", r.get("response")); print()
        if r.get("ended") or r.get("callEnded"): break
    if not call_id: return
    try: freya.api("POST", "/api/v2/chat/end", {"callId": call_id})
    except RuntimeError: pass
    time.sleep(2)
    try: call = freya.get_call(call_id)
    except Exception as e: print("could not read the call:", e); return
    print("---- events")
    for m in call.get("messages", []):
        if m.get("role") != "system": continue
        try: e = json.loads(m["message"])
        except Exception: continue
        ev = e.get("event")
        if ev == "node_enter": print("STEP", e.get("node"))
        elif ev == "function_call_start": print("TOOL", e.get("function"), json.dumps(e.get("args", {}).get("body", e.get("args")))[:300])
        elif ev == "function_call_complete": print("  ->", json.dumps(e.get("result"))[:400])
        elif ev == "error": print("ERROR", json.dumps(e)[:300])
    print("ended:", call.get("endedReason"))


if __name__ == "__main__":
    main()
