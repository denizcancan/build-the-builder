"""Our builder: brief files in, phone agent on Freya out.

    python3 builder/build.py --slot final-1 --api <tool API file> <brief file> [more brief files]
    python3 builder/build.py --slot brief-b --api tools/openapi.json briefs/brief-a.md briefs/brief-b.md --test B

1. Claude reads the brief and the tool API and writes a spec (builder/SPEC.md), checked by builder/validate.py.
2. builder/generate.py turns the spec into a workflow. No model is involved in this step.
3. The workflow is validated by Freya, saved in place under the slot, published and exported.
4. Optional: the practice customers are run against it (--test A or B; practice briefs only).
Everything is written to a plain-text trace in traces/, the file to submit.

Needs FREYA_API_KEY (and FREYA_BANK_TEAM_KEY for --test). Claude runs through the Claude Agent SDK with the
installed `claude` login, or ANTHROPIC_API_KEY.
"""
import argparse, asyncio, json, os, pathlib, shutil, subprocess, sys, time

KIT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT)); sys.path.insert(0, str(KIT / "builder"))
import freya
from generate import generate
from validate import check

MODEL = os.environ.get("BUILDER_MODEL", "claude-opus-5-5")


class Trace:
    """Plain-text trace: times, model calls, what was created on Freya, tests."""
    def __init__(self, slot):
        self.start = time.time()
        (KIT / "traces").mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.path = KIT / "traces" / f"{slot}-{stamp}.txt"
        self.raw = KIT / "traces" / f"{slot}-{stamp}.jsonl"
        self.f = open(self.path, "w")
        self.calls = 0

    def line(self, text):
        msg = f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {text}"
        print(msg, flush=True)
        self.f.write(msg + "\n"); self.f.flush()

    def model_call(self, model, purpose):
        self.calls += 1
        self.line(f"MODEL CALL {self.calls}: model={model} purpose={purpose}")

    def close(self):
        self.line(f"END. Total time {time.time() - self.start:.0f} s, {self.calls} model calls.")
        self.f.close()


EXTRACT_PROMPT = """You are the first step of a builder that turns a bank's brief into a phone agent.

Inputs:
- Brief file(s), in this order (a later file builds on or changes an earlier one): {briefs}
- The bank's tool API file: {api}

Do this:
1. Read builder/SPEC.md completely. It defines the spec you write and what the generator already does.
2. Read every brief file completely, then the tool API file. Raw material (mails, memos, code lists) counts: a later memo or mail changes earlier rules.
3. Read builder/specs/example.spec.json. It is a spec for a DIFFERENT bank brief. Use it only to see the format, the level of detail and how rules are worded. Never copy its values, names, dates, numbers or sentences unless your brief says the same.
4. Write the spec to {spec_path}.
5. Run: python3 builder/validate.py {spec_path} {api}
   Fix every problem it prints and run it again, until it prints OK.
6. Finish with one line: the spec path.

Work only with the files named here. Do not run anything else on Freya."""


REVIEW_PROMPT = """You are the checking step of a builder that turns a bank's brief into a phone agent.
Another session wrote a spec from the brief. Your job is to find what it missed or got wrong, and fix it.

Inputs:
- Brief file(s), in this order (a later file builds on or changes an earlier one): {briefs}
- The bank's tool API file: {api}
- The spec: {spec_path}

Do this:
1. Read builder/SPEC.md completely: what the spec holds and what the generator already does by itself.
2. Read every brief file completely, then the tool API file, then the spec.
3. Split the brief into single rules: every numbered rule, every table row, every sentence that sets a value, a
   sentence to say, a limit, a date or a name, and every change made by a memo, mail or later file. Where a later
   text changes an earlier one, only the changed version counts.
4. For each rule, find where the spec holds it, with the brief's exact values. If it is missing, wrong, or still
   uses a value that a later text changed, fix the spec.
5. Write the checklist to {review_path}: a JSON list with one object per rule:
   {{"rule": "<short quote or summary of the brief rule>", "source": "<file and rule number or section>",
     "where": "<spec field, or 'generator' if SPEC.md says the generator does it>", "status": "ok" | "fixed",
     "note": "<what you changed, if fixed>"}}
   Every rule of the brief must be in the list.
6. Run: python3 builder/validate.py {spec_path} {api}
   Fix every problem it prints and run it again, until it prints OK.
7. Finish with one line: the number of rules checked and the number fixed.

Work only with the files named here."""


GENERAL_PROMPT = """You are a builder that turns a client's case material into a Freya phone agent (a workflow).
The case does not fit a fixed form, so you write the workflow yourself, test it and fix it.

Inputs:
- The case folder: {case}. Read EVERY file in it, including PDF drawings and sub-folders. The material was written
  at different times by different people and does not always agree: a later decision (a recap mail, a later meeting,
  a later version of a document) replaces an earlier one. Note each conflict and which version you used.
- The address of the mock of the client's services: {mock_url}. Every tool calls it, on the same paths the client's
  integration document gives under its own base address.

Do this:
1. Read docs/workflow-format.md and docs/tools-and-code.md completely. They are the reference for the workflow JSON:
   node types, prompts, tools, response_mapping, variables, transitions, code steps, how a call ends.
2. Read the whole case folder. List for yourself: the call variables the agent receives, every service (path,
   request body, response fields, error responses), every rule, every fixed sentence, every outcome and hand-off,
   the language of the calls, and the example conversations.
3. Write the workflow to {wf_path}. Rules that held in earlier working builds:
   - Conversation steps with a focused task each; the system_prompt holds identity, language and the rules that
     always hold. Speak only the language of the calls. Fixed sentences the client gives are said word for word.
   - Tools are api_call tools with the mock URL + the contract's path, the contract's method and body, call
     variables written as {{{{name}}}}. Map response fields into variables with response_mapping (paths start with data.).
   - Route on tool results with deterministic transitions (a boolean may arrive as "true" or "True": check both).
   - Dates and amounts: use the call's own date variable, never the system clock. Do date arithmetic in a code step
     when it matters (code reads variables with vars.get(...); no double braces inside code).
   - Never tell the caller that something was done (sent, changed, booked, transferred) before the tool result
     confirms it. If a service fails or returns an error, say so honestly and follow the material's rule for it.
   - Every way the call can end goes through a step that ends the call; hand-offs to a human use the material's
     hand-off rule.
   - Put one of the client's test customers in test_variables, using the case's call-variable names.
4. Publish it: python3 builder/publish.py {slot} {wf_path} <speech language code, e.g. en or tr>
   Fix whatever Freya rejects and publish again.
5. Test it with scripted callers built from the example conversations and the test customers:
   python3 builder/chat_test.py <workflow_id> '<call variables JSON>' "caller line 1" "caller line 2" ...
   Cover at least: each example scenario, a service error case from the test data, and a hand-off. Read the agent's
   answers, the tool calls and the step changes. Fix the workflow, publish, and test again until they behave as
   the material requires. If the chat route returns 504 or is very slow, retry once, then move on.
6. Write {meta_path}: {{"language": "<speech language code>", "conflicts": ["<conflict: which version you used>"],
   "tests": ["<scenario: pass or what still fails>"]}}
7. Finish with one line: the workflow path.

Work fast: aim to be done within 40 minutes. Do not hard-code anything that is not in the case material."""


async def run_claude(trace, label, prompt, max_turns=80, tools=None):
    """One Claude session through the Agent SDK. Every model turn is written to the trace."""
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, ToolUseBlock, query
    options = ClaudeAgentOptions(
        cwd=KIT, model=MODEL,
        cli_path=os.environ.get("CLAUDE_CLI_PATH") or shutil.which("claude"),
        setting_sources=["project"],
        allowed_tools=tools or ["Read", "Write", "Edit", "Glob", "Grep", "Bash(python3 builder/validate.py:*)"],
        permission_mode="dontAsk", max_turns=max_turns,
    )
    with open(trace.raw, "a") as raw:
        async for m in query(prompt=prompt, options=options):
            raw.write(json.dumps({"step": label, "type": type(m).__name__, "data": str(m)[:4000]}) + "\n")
            if isinstance(m, AssistantMessage):
                purpose = label
                for b in m.content:
                    if isinstance(b, ToolUseBlock):
                        arg = b.input.get("file_path") or b.input.get("command") or b.input.get("pattern") or ""
                        purpose = f"{label}: {b.name} {str(arg)[-80:]}"
                        break
                    if isinstance(b, TextBlock) and b.text.strip():
                        purpose = f"{label}: says " + b.text.strip().replace("\n", " ")[:100]
                trace.model_call(getattr(m, "model", MODEL), purpose)
            elif isinstance(m, ResultMessage):
                trace.line(f"{label} done: {m.num_turns} turns, {m.duration_ms / 1000:.0f} s, cost ${m.total_cost_usd or 0:.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("briefs", nargs="+")
    ap.add_argument("--api", help="the bank's tool API file (spec mode)")
    ap.add_argument("--general", action="store_true", help="the case does not fit the spec form: Claude writes and tests the workflow itself")
    ap.add_argument("--mock-url", help="address of the mock of the client's services (general mode)")
    ap.add_argument("--slot", required=True, help="your label; the same slot updates the same workflow")
    ap.add_argument("--spec", help="skip extraction and use this spec")
    ap.add_argument("--skip-review", action="store_true", help="skip the rule-by-rule check (only with --spec, for quick tests)")
    ap.add_argument("--test", choices=["A", "B"], help="run the practice customers of this brief afterwards")
    ap.add_argument("--reps", type=int, default=1)
    a = ap.parse_args()

    trace = Trace(a.slot)
    trace.line(f"START builder. Briefs: {a.briefs}. Tool API: {a.api}. Slot: {a.slot}. Builder model: {MODEL}.")
    if a.general:
        return general(a, trace)
    api = json.load(open(a.api))
    out = KIT / "out"; out.mkdir(exist_ok=True)
    spec_path = a.spec or str(out / f"{a.slot}.spec.json")

    if not a.spec:
        trace.line("STEP 1a: Claude reads the brief and writes the spec.")
        asyncio.run(run_claude(trace, "write spec", EXTRACT_PROMPT.format(briefs=", ".join(a.briefs), api=a.api, spec_path=spec_path)))
    if not a.skip_review:
        review_path = str(out / f"{a.slot}.review.json")
        pathlib.Path(review_path).unlink(missing_ok=True)
        trace.line("STEP 1b: a second Claude session checks the spec against the brief, rule by rule.")
        asyncio.run(run_claude(trace, "review spec", REVIEW_PROMPT.format(briefs=", ".join(a.briefs), api=a.api, spec_path=spec_path, review_path=review_path)))
        try:
            review = json.load(open(review_path))
            assert isinstance(review, list) and review
        except Exception as e:
            trace.line(f"Review checklist missing or unreadable ({e}).")
            trace.close(); sys.exit("The review step did not write its checklist; see the trace.")
        fixed = [r for r in review if str(r.get("status")).lower() == "fixed"]
        trace.line(f"Review checklist: {len(review)} rules checked, {len(fixed)} fixed -> {review_path}")
        for r in fixed:
            trace.line(f"  fixed: {str(r.get('rule'))[:120]} -> {str(r.get('note'))[:160]}")
    spec = json.load(open(spec_path))
    problems = check(spec, api)
    trace.line("Spec validation: " + ("OK" if not problems else "PROBLEMS: " + " | ".join(problems)))
    if problems:
        trace.close(); sys.exit("The spec is not valid; see the trace.")

    trace.line("STEP 2: generate the workflow from the spec (code, no model).")
    wf = generate(spec, api)
    wf_path = out / f"{a.slot}.workflow.json"
    json.dump(wf, open(wf_path, "w"), indent=1)
    n_tools = sum(len(n.get("tools", [])) + len(n.get("pre_actions", [])) for n in wf["nodes"].values())
    trace.line(f"Workflow: {len(wf['nodes'])} steps, {n_tools} tools and actions -> {wf_path}")

    trace.line("STEP 3: validate on Freya, save in place, publish, export.")
    try:
        trace.line("Freya dry run: " + json.dumps(freya.validate_workflow(a.slot, wf)))
    except RuntimeError as e:
        if " -> 5" not in str(e): raise          # a rejected workflow stops the build; a server outage only skips this check
        trace.line("Freya dry run skipped, the server did not answer: " + str(e)[:200])
    saved = freya.save_workflow(a.slot, wf, state_file=str(KIT / ".freya-agents.json"))
    trace.line(f"Freya resources: workflow_id={saved['workflow_id']} agent_id={saved['agent_id']} version={saved.get('version', 1)} steps={len(wf['nodes'])} tools={n_tools}")
    exp = out / f"{a.slot}.export.json"
    freya.export_workflow(saved["workflow_id"], str(exp))
    trace.line(f"Exported to {exp}")

    if a.test:
        trace.line(f"STEP 4: self-test on the practice customers of brief {a.test}, {a.reps} run(s).")
        res = out / f"{a.slot}.test.json"
        p = subprocess.run([sys.executable, str(KIT / "tools/run_cases.py"), saved["agent_id"], "--brief", a.test, "--reps", str(a.reps), "--out", str(res)],
                           cwd=KIT, capture_output=True, text=True)
        for l in p.stdout.splitlines():
            if l.startswith(("PASS", "FAIL")) or "passed" in l:
                trace.line("test: " + l)
        if p.returncode != 0:
            err = (p.stderr.strip().splitlines() or ["no error output"])[-1]
            trace.line(f"test: the self-test stopped with an error: {err[:300]}")
    trace.close()
    print(f"\nTrace: {trace.path}")


def general(a, trace):
    """General mode: Claude reads the whole case, writes the workflow, publishes it, tests it by chat and fixes it."""
    out = KIT / "out"; out.mkdir(exist_ok=True)
    wf_path, meta_path = out / f"{a.slot}.workflow.json", out / f"{a.slot}.meta.json"
    for f in (wf_path, meta_path): f.unlink(missing_ok=True)
    case = " ".join(str(pathlib.Path(b).resolve()) for b in a.briefs)
    trace.line(f"GENERAL MODE. Case: {case}. Mock: {a.mock_url}")
    trace.line("STEP 1: Claude reads the case, writes the workflow, publishes it, tests it by chat and fixes it.")
    tools = ["Read", "Write", "Edit", "Glob", "Grep", "Bash(python3 builder/publish.py:*)", "Bash(python3 builder/chat_test.py:*)",
             "Bash(curl:*)", "Bash(ls:*)", "Bash(pdftotext:*)"]
    asyncio.run(run_claude(trace, "build and test", GENERAL_PROMPT.format(case=case, mock_url=a.mock_url, slot=a.slot, wf_path=wf_path, meta_path=meta_path),
                           max_turns=200, tools=tools))
    if not wf_path.exists():
        trace.line("No workflow was written."); trace.close(); sys.exit("No workflow; see the trace.")
    meta = {}
    try: meta = json.load(open(meta_path))
    except Exception as e: trace.line(f"meta file missing or unreadable ({e})")
    if meta.get("language"): freya.SPEECH_LANGUAGE = meta["language"]
    for c in meta.get("conflicts", []): trace.line("conflict: " + str(c)[:300])
    for t in meta.get("tests", []): trace.line("self-test: " + str(t)[:300])
    wf = json.load(open(wf_path))
    trace.line("STEP 2: validate on Freya, save in place, publish, export.")
    trace.line("Freya dry run: " + json.dumps(freya.validate_workflow(a.slot, wf)))
    saved = freya.save_workflow(a.slot, wf, state_file=str(KIT / ".freya-agents.json"))
    trace.line(f"Freya resources: workflow_id={saved['workflow_id']} agent_id={saved['agent_id']} version={saved.get('version', 1)} steps={len(wf['nodes'])}")
    exp = out / f"{a.slot}.export.json"
    freya.export_workflow(saved["workflow_id"], str(exp))
    trace.line(f"Exported to {exp}")
    trace.close()
    print(f"\nTrace: {trace.path}")


if __name__ == "__main__":
    main()
