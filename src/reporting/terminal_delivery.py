"""One saved report/paper delivery sequence after each completed research turn."""
from pace.reporting.writing import read_state, writing_artifacts
from pace.runtime.output.events import publish_progress
from pace.runtime.output.workspace import _write_json_atomic, work_ref
from pace.reporting.research_report import outline_state_ref, report_state_ref
from pace.reporting.theory_paper import paper_outline_state_ref, paper_state_ref


def delivery_state(root):
    return read_state(root, work_ref(root, "state/terminal_delivery.json"))


def complete_terminal_delivery(pipeline, ws):
    ref = ws.work_ref("state/terminal_delivery.json")
    state = delivery_state(ws.root) or {"completed": [], "status": "pending"}
    if state["status"] == "completed":
        return
    policy = read_state(ws.root, ws.work_ref("state/research_execution_policy.json"))
    variant = policy["resource_variant"]
    actions = (
        ("outline", outline_state_ref, lambda: pipeline._outliner(ws).request(
            "依据本轮原始目标、已核验成果、条件与未解决问题，拟定完整中文研究报告提纲。",
            requirements="中文报告；准确区分既有文献、已核验成果和开放缺口。", resource_variant=variant)),
        ("report", report_state_ref, lambda: pipeline._writer(ws).request(
            message="完成本轮中文研究报告。", resource_variant=variant)),
        ("paper-outline", paper_outline_state_ref, lambda: pipeline._paper_writer(ws).request_outline(
            "Outline a standalone English paper from this turn's saved evidence. "
            "Preserve assumptions, source attribution and unresolved gaps. Do not invent results.",
            resource_variant=variant)),
        ("paper", paper_state_ref, lambda: pipeline._paper_writer(ws).request(
            message="Write a standalone English paper with the available proofs and explicit open gaps.",
            resource_variant=variant)),
    )
    _write_json_atomic(ws.root / ref, state)
    for action, request_ref, prepare in actions:
        if action in state["completed"]:
            continue
        state.update(status="running", action=action)
        _write_json_atomic(ws.root / ref, state)
        try:
            if not read_state(ws.root, request_ref(ws.root)):
                prepare()
            pipeline._complete_writing(ws, action)
        except BaseException:
            state["status"] = "interrupted"
            _write_json_atomic(ws.root / ref, state)
            raise
        state["completed"].append(action)
        _write_json_atomic(ws.root / ref, state)
    state.update(status="completed", action="")
    _write_json_atomic(ws.root / ref, state)
    publish_progress(ws.root, task_id="turn-delivery", stage="run", status="finished",
                     publication_refs=writing_artifacts(ws.root, snapshots=True))
