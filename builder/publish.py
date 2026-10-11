"""Check a workflow file with Freya, then save it in place under a slot and publish it.

    python3 builder/publish.py <slot> <workflow.json> [speech language, e.g. tr]

Prints Freya's verdict, or the workflow_id and agent_id of the published version.
"""
import json, pathlib, sys
KIT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KIT))
import freya


def main():
    if len(sys.argv) < 3: sys.exit(__doc__)
    slot, path = sys.argv[1], sys.argv[2]
    if len(sys.argv) > 3: freya.SPEECH_LANGUAGE = sys.argv[3]
    wf = json.load(open(path))
    try:
        print("Freya check:", json.dumps(freya.validate_workflow(slot, wf)))
    except RuntimeError as e:
        print("Freya rejected the workflow:", str(e)[:1500]); sys.exit(1)
    saved = freya.save_workflow(slot, wf, state_file=str(KIT / ".freya-agents.json"))
    print("Published:", json.dumps(saved))


if __name__ == "__main__":
    main()
