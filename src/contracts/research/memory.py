"""Versioned research records. These checks validate references, never mathematics."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PureWindowsPath
from urllib.parse import unquote

from pace.artifacts.workspace import is_call_path, path_has_symlink_component, work_ref

OUTCOMES = {
    "APPLICATION_PROPOSED",
    "LOCAL_RESULT",
    "METHOD_OBSTRUCTION",
    "NOT_ESTABLISHED",
    "CONTEXT_GAP",
}


def digest(value) -> str:
    data = (
        value
        if isinstance(value, bytes)
        else json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    )
    return hashlib.sha256(data).hexdigest()


def read_json(root: Path, ref: str) -> dict:
    return json.loads(public_file(root, ref).read_text(encoding="utf-8"))


def public_file(root: Path, ref: str) -> Path:
    if (
        not isinstance(ref, str)
        or not ref
        or ref != ref.strip()
        or "\\" in ref
        or Path(ref).is_absolute()
        or PureWindowsPath(ref).drive
        or any(p in {"", ".", ".."} for p in ref.split("/"))
        or is_call_path(Path(ref))
        or any(p.startswith("private_") for p in Path(ref).parts)
    ):
        raise ValueError(f"invalid public research reference: {ref!r}")
    root = Path(root).resolve()
    path = root / ref
    if (
        path_has_symlink_component(root, path)
        or not path.is_file()
        or not path.resolve().is_relative_to(root)
    ):
        raise ValueError(f"missing or unsafe research reference: {ref}")
    return path


def binding(root: Path, ref: str, *, kind="MATHEMATICAL_SOURCE") -> dict:
    row = {
        "ref": ref,
        "sha256": digest(public_file(root, ref).read_bytes()),
        "kind": kind,
    }
    for parent in (root / ref).parents:
        if parent == root:
            break
        if (parent / "assignment/task.json").is_file():
            row["source_result_ref"] = str(
                parent.relative_to(root) / "agent_result.json"
            )
            break
    return row


def check_binding(root: Path, row: dict) -> None:
    if binding(root, row["ref"])["sha256"] != row["sha256"]:
        raise ValueError(f"research input version changed: {row['ref']}")


def check_source_ownership(root: Path, row: dict, *, local_result_ref="") -> None:
    """Cross-task mathematical inputs come from parent-committed owners."""
    owner = row.get("source_result_ref", "")
    if not owner or owner == local_result_ref:
        return
    turn = Path(owner).parts[1]
    state_path = root / work_ref(root, "state/director_state.json", turn_id=turn)
    state = json.loads(state_path.read_text()) if state_path.is_file() else {}
    if owner not in state.get("completed_result_refs", []):
        proof_path = root / work_ref(root, "state/proof_state.json", turn_id=turn)
        if (
            not state_path.is_file()
            and proof_path.is_file()
            and (root / owner).is_file()
        ):
            proof = json.loads(proof_path.read_text())
            result = read_json(root, owner)
            if result.get("status") == "SUCCEEDED" and result.get(
                "deliverable_ref"
            ) in proof.get("completed_refs", []):
                return
        raise ValueError(f"source belongs to an uncommitted task: {row['ref']}")


def enabled(root: Path) -> bool:
    path = Path(root) / work_ref(root, "state/research_execution_policy.json")
    return (
        path.is_file()
        and json.loads(path.read_text()).get("research_memory_version") == 1
    )


def reporter_owned(root: Path) -> bool:
    """Saved policy keeps historical runs on their original handoff contract."""
    path = Path(root) / work_ref(root, "state/research_execution_policy.json")
    return (
        path.is_file()
        and json.loads(path.read_text()).get("research_delta_owner") == "reporter"
    )


def identifiers(value, field) -> list[str]:
    if (
        not isinstance(value, list)
        or any(not isinstance(x, str) or not x.strip() for x in value)
        or len(value) != len(set(value))
    ):
        raise ValueError(f"{field} requires unique nonempty strings")
    return value


def check_reference_fields(root: Path, value) -> None:
    """Collect malformed file references before any route/delta is committed."""
    errors = []

    def visit(node, pointer=""):
        if isinstance(node, dict):
            for key, child in node.items():
                location = pointer + "/" + key
                refs = (
                    [child]
                    if key.endswith("_ref")
                    else child
                    if key.endswith("_refs") and isinstance(child, list)
                    else None
                )
                if refs is None:
                    visit(child, location)
                    continue
                for index, ref in enumerate(refs):
                    if ref == "":
                        continue  # Required references are checked by their owner.
                    try:
                        public_file(root, ref)
                    except (ValueError, OSError, TypeError) as exc:
                        errors.append(
                            {
                                "json_pointer": location
                                + (f"/{index}" if key.endswith("_refs") else ""),
                                "message": str(exc),
                            }
                        )
        elif isinstance(node, list):
            for index, child in enumerate(node):
                visit(child, pointer + f"/{index}")

    visit(value)
    if errors:
        raise ValueError(
            "Repair all file references as exact paths, without annotations: "
            + json.dumps(errors, ensure_ascii=False)
        )


def _check_locator(path: Path, locator: str) -> None:
    """Resolve a recorded section or unique JSON record identity."""
    if not isinstance(locator, str) or not locator.startswith("#") or not locator[1:]:
        raise ValueError("route locator must be a nonempty #anchor")
    anchor = unquote(locator[1:])
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        nodes = [json.loads(text)]
        matches = 0
        while nodes:
            node = nodes.pop()
            if isinstance(node, dict):
                if any(node.get(key) == anchor for key in ("id", "clause_id")):
                    matches += 1
                nodes.extend(node.values())
            elif isinstance(node, list):
                nodes.extend(node)
        if matches != 1:
            raise ValueError(
                f"route locator must identify one JSON record: {path.name}{locator}"
            )
        return
    explicit = re.findall(r"<[^>]+\b(?:id|name)\s*=\s*[\"\']([^\"\']+)[\"\']", text)
    explicit += re.findall(r"\{#([^\s}]+)\}", text)
    headings = []
    for heading in re.findall(r"^#{1,6}\s+(.+?)\s*#*\s*$", text, re.M):
        heading = re.sub(r"<[^>]+>", "", heading).lower()
        headings.append(re.sub(r"[^\w\-\s]", "", heading).replace(" ", "-"))
    if anchor not in (*explicit, *headings):
        raise ValueError(
            f"route locator does not identify an existing section: {path.name}{locator}"
        )


def repair_route_references(root: Path, ref: str) -> list[dict]:
    """Split verified file#anchor references without changing any mathematical text."""
    from pace.artifacts.workspace import _write_json_atomic

    path = public_file(root, ref)
    if (path.parent / "memory_commit.json").exists():
        return []  # Committed evidence is immutable.
    original = read_json(root, ref)
    if original.get("schema_version") != "route_relations/v1":
        return []
    data = json.loads(json.dumps(original))
    changes = []
    errors = []
    for index, row in enumerate(data.get("obligations", [])):
        if not isinstance(row, dict):
            continue
        for field in ("statement", "scope"):
            key, locator_key = field + "_ref", field + "_locator"
            value = row.get(key)
            if not isinstance(value, str) or "#" not in value:
                continue
            # A literal filename containing # takes precedence over URL syntax.
            try:
                public_file(root, value)
                continue
            except ValueError:
                pass
            file_ref, fragment = value.split("#", 1)
            locator = "#" + fragment
            try:
                source = public_file(root, file_ref)
                _check_locator(source, locator)
                if row.get(locator_key, locator) != locator:
                    raise ValueError("conflicting route reference and section locator")
            except (ValueError, OSError) as exc:
                errors.append(
                    {"json_pointer": f"/obligations/{index}/{key}", "message": str(exc)}
                )
                continue
            row[key], row[locator_key] = file_ref, locator
            changes.append(
                {
                    "json_pointer": f"/obligations/{index}/{key}",
                    "before": value,
                    "after": file_ref,
                    "locator": locator,
                }
            )
    if errors:
        raise ValueError(
            "Invalid route reference locators: "
            + json.dumps(errors, ensure_ascii=False)
        )
    if changes:
        # Persist the original JSON before replacing its control representation.
        # Replaying after either write is idempotent; original manuscripts are untouched.
        audit = path.with_name("route_reference_repairs.json")
        history = json.loads(audit.read_text()) if audit.exists() else []
        repair = {
            "before_sha256": digest(original),
            "after_sha256": digest(data),
            "changes": changes,
            "original": original,
        }
        if not any(
            item["before_sha256"] == repair["before_sha256"] for item in history
        ):
            _write_json_atomic(audit, [*history, repair])
        _write_json_atomic(path, data)
    return changes


def prepare_route(root: Path, ref: str, known: dict) -> dict:
    """Bind proposed relations; parent edges do not grant implication authority."""
    data = read_json(root, ref)
    if (
        set(data) != {"schema_version", "obligations", "change_ref"}
        or data["schema_version"] != "route_relations/v1"
    ):
        raise ValueError("route_relations/v1 requires obligations and change_ref")
    check_reference_fields(root, data)
    public_file(root, data["change_ref"])
    if not isinstance(data["obligations"], list):
        raise ValueError("obligations must be an array")
    result, ids = [], set()
    for row in data["obligations"]:
        fields = {
            "id",
            "kind",
            "statement_ref",
            "scope_ref",
            "parent_ids",
            "branch_id",
            "premise_groups",
        }
        if (
            not isinstance(row, dict)
            or not fields <= set(row)
            or set(row) - fields - {"disposition", "statement_locator", "scope_locator"}
        ):
            raise ValueError(f"obligation requires {sorted(fields)}")
        if row["id"] == "target":
            raise ValueError(
                "the root target is fixed by initialization; reference target as a parent instead"
            )
        disposition = row.get("disposition", "OPEN")
        if disposition not in {"OPEN", "DEFERRED", "SUPERSEDED"}:
            raise ValueError(
                "Planner can propose OPEN, DEFERRED or SUPERSEDED, never CLOSED"
            )
        if not all(
            isinstance(row[k], str) and row[k].strip() for k in ("id", "branch_id")
        ):
            raise ValueError("obligation and branch identities must be nonempty")
        if row["id"] in ids or row["kind"] not in {
            "TARGET_REQUIRED",
            "ROUTE_SUFFICIENT",
        }:
            raise ValueError("duplicate obligation or unsupported kind")
        ids.add(row["id"])
        identifiers(row["parent_ids"], "parent_ids")
        if not isinstance(row["premise_groups"], list):
            raise ValueError("premise_groups is an OR list of AND lists")
        for group in row["premise_groups"]:
            if not identifiers(group, "premise_groups"):
                raise ValueError("empty dependency group")
        refs = [binding(root, row[k]) for k in ("statement_ref", "scope_ref")]
        for field, source in zip(("statement", "scope"), refs):
            if field + "_locator" in row:
                locator = row[field + "_locator"]
                _check_locator(public_file(root, source["ref"]), locator)
                source["locator"] = locator
        owner = binding(root, ref).get("source_result_ref", "")
        for source in refs:
            check_source_ownership(root, source, local_result_ref=owner)
        version = digest(
            {
                "bindings": refs,
                "kind": row["kind"],
                "parent_ids": row["parent_ids"],
                "premise_groups": row["premise_groups"],
            }
        )
        result.append(
            {
                **row,
                "disposition": disposition,
                "version": version,
                "bindings": refs,
                "status": disposition,
            }
        )
    all_ids = ids | set(known)
    for row in result:
        dependencies = [
            *row["parent_ids"],
            *(x for group in row["premise_groups"] for x in group),
        ]
        if any(x not in all_ids or x == row["id"] for x in dependencies):
            raise ValueError("unknown or self-dependent obligation")
        if row["id"] in known and row["kind"] != known[row["id"]]["kind"]:
            raise ValueError("cannot change obligation kind under the same identity")
    # Route premises are acyclic; parent links describe intent, not proof edges.
    graph = {
        k: [x for g in v.get("premise_groups", []) for x in g] for k, v in known.items()
    }
    graph.update({r["id"]: [x for g in r["premise_groups"] for x in g] for r in result})

    def visit(node, active, done):
        if node in active:
            raise ValueError("cyclic obligation prerequisites")
        if node in done:
            return
        for nxt in graph.get(node, []):
            visit(nxt, active | {node}, done)
        done.add(node)

    done = set()
    for node in graph:
        visit(node, set(), done)
    return {"obligations": result, "change": binding(root, data["change_ref"])}


def prepare_delta(root: Path, ref: str, obligations: dict) -> dict:
    return prepare_delta_payload(
        root,
        read_json(root, ref),
        obligations,
        local_result_ref=binding(root, ref).get("source_result_ref", ""),
    )


def application_review_bindings(app: dict) -> list[dict]:
    """Bind an application to its exact target, manuscript, premises and scope."""
    bindings = [
        app["target"],
        app["argument"],
        *app["premises"],
        *app["obligation_bindings"],
    ]
    return list({digest(row): row for row in bindings}.values())


def check_joint_finding_record(root: Path, draft_ref: str, record: dict) -> None:
    joint = record.get("joint_receipt_ref")
    if not joint:
        return
    receipt = read_json(root, joint)
    check_binding(root, receipt["request_binding"])
    request = read_json(root, receipt["request_ref"])
    if (
        record != {**receipt, "joint_receipt_ref": joint}
        or request.get("verification_ref") != joint
        or request.get("publication_ref")
        != str(Path(draft_ref).with_suffix(".verification.json"))
        or request.get("argument_ref") != draft_ref
        or receipt["request_binding"]["ref"] != receipt["request_ref"]
        or not request.get("publication_requested")
    ):
        raise ValueError(
            "finding publication differs from the independently accepted manuscript review"
        )


def load_application_review(root: Path, app: dict) -> dict | None:
    """Resolve a scope's reference to the single, immutable manuscript review."""
    ref = str(
        Path(app["result_ref"]).parent
        / "verification"
        / f"application-{app['application_key']}.json"
    )
    if not (root / ref).is_file():
        return None
    pointer = read_json(root, ref)["verification_binding"]
    if not (root / pointer["ref"]).is_file():
        return None
    check_binding(root, pointer)
    record = read_json(root, pointer["ref"])
    check_binding(root, record["request_binding"])
    request = read_json(root, record["request_ref"])
    item = next(
        (
            row
            for row in request["applications"]
            if row["application_key"] == app["application_key"]
        ),
        None,
    )
    if (
        item is None
        or item["bindings"] != application_review_bindings(app)
        or item["application_ref"] != ref
        or request["verification_ref"] != pointer["ref"]
        or record["request_binding"]["ref"] != record["request_ref"]
        or record["source_binding"] != app["argument"]
        or request["argument_ref"] != app["argument_ref"]
        or (
            record.get("review_binding")
            and record["review_binding"]["ref"] != request["review_ref"]
        )
    ):
        raise ValueError("application review has different dependencies")
    if record.get("review_binding"):
        check_binding(root, record["review_binding"])
    return record


def application_review_incomplete(app: dict, record: dict) -> bool:
    verdict = record["verdict"]
    if record.get("separate_scope_decisions"):
        for row in verdict.get("application_checks", []):
            if row.get("application_key") == app["application_key"]:
                return row.get("status") == "VERIFIER_ERROR"
    return verdict["stage_status"] == "VERIFIER_ERROR"


def application_is_approved(app: dict, record: dict) -> bool:
    verdict = record["verdict"]
    checks = verdict.get("application_checks", [])
    review = record.get("review_binding")
    matching = [
        row for row in checks if row.get("application_key") == app["application_key"]
    ]
    return (
        len(matching) == 1
        and (
            record.get("separate_scope_decisions")
            and matching[0].get("reuse_authorized") is True
            or (
                verdict.get("stage_status") == "PASS"
                and verdict.get("reuse_authorized") is True
                and not record.get("separate_scope_decisions")
            )
        )
        and matching[0].get("obligation_id") == app["obligation_id"]
        and matching[0].get("obligation_version") == app["obligation_version"]
        and matching[0].get("status") == "APPLICABLE"
        and bool(review)
        and matching[0].get("review_ref") == review["ref"]
    )


def application_review_identity(app: dict) -> str:
    """Identify the review by its bound authority, never by a reporter label."""
    return digest(
        {
            "target": app["target"],
            "argument": app["argument"],
            "obligation_id": app["obligation_id"],
            "obligation_version": app["obligation_version"],
            "evidence": sorted(
                {digest(row) for row in application_review_bindings(app)}
            ),
        }
    )


def prepare_delta_payload(
    root: Path, data: dict, obligations: dict, *, local_result_ref: str
) -> dict:
    if set(data) != {
        "schema_version",
        "obligation_ids",
        "branch_id",
        "change_ref",
        "applications",
        "attempt",
    }:
        raise ValueError(
            "research_delta requires schema_version, obligation_ids, branch_id, change_ref, applications, attempt"
        )
    if data["schema_version"] != "research_delta/v1":
        raise ValueError("unsupported research delta")
    check_reference_fields(root, data)
    ids = identifiers(data["obligation_ids"], "obligation_ids")
    if any(x not in obligations for x in ids):
        raise ValueError("delta references an unknown obligation")
    if not isinstance(data["branch_id"], str) or not data["branch_id"].strip():
        raise ValueError("branch_id required")
    change = binding(root, data["change_ref"])
    check_source_ownership(root, change, local_result_ref=local_result_ref)
    attempt = data["attempt"]
    if (
        not isinstance(attempt, dict)
        or set(attempt) != {"outcome", "obstacle_ref", "reopen_condition_ref"}
        or attempt["outcome"] not in OUTCOMES
    ):
        raise ValueError("invalid attempt record; authors cannot grant acceptance")
    for k in ("obstacle_ref", "reopen_condition_ref"):
        if attempt[k]:
            check_source_ownership(
                root, binding(root, attempt[k]), local_result_ref=local_result_ref
            )
    if not isinstance(data["applications"], list):
        raise ValueError("applications must be an array")
    grouped, app_ids = {}, set()
    for app in data["applications"]:
        if not isinstance(app, dict):
            raise ValueError("each application must be an object")
        if set(app) != {"id", "obligation_id", "argument_ref", "premise_refs"}:
            raise ValueError(
                "application requires id, obligation_id, argument_ref and premise_refs"
            )
        if (
            not isinstance(app["id"], str)
            or not app["id"].strip()
            or app["id"] in app_ids
        ):
            raise ValueError("invalid application identity")
        app_ids.add(app["id"])
        if app["obligation_id"] not in ids:
            raise ValueError("application is outside the assigned obligations")
        argument = binding(root, app["argument_ref"])
        premises = [
            binding(root, p) for p in identifiers(app["premise_refs"], "premise_refs")
        ]
        for row in [argument, *premises]:
            if (
                row["ref"].startswith(("memory/", "results/"))
                or "/state/" in row["ref"]
                or "/assignment/" in row["ref"]
                or Path(row["ref"]).name == "progress.md"
            ):
                raise ValueError(
                    "applications must cite immutable manuscripts, not mutable memory or result views"
                )
            check_source_ownership(root, row, local_result_ref=local_result_ref)
        obligation = obligations[app["obligation_id"]]
        key = (
            app["obligation_id"],
            obligation["version"],
            argument["ref"],
            argument["sha256"],
            tuple(sorted(digest(row) for row in premises)),
        )
        review = grouped.setdefault(
            key,
            {
                **app,
                "obligation_version": obligation["version"],
                "argument": argument,
                "premises": [],
                "obligation_bindings": obligation["bindings"],
                "status": "PROPOSED",
                "proposal_ids": [],
            },
        )
        review["proposal_ids"].append(app["id"])
        review["premises"].extend(premises)
    apps = list(grouped.values())
    for review in apps:
        premises = {row["ref"]: row for row in review["premises"]}
        if any(premises[row["ref"]] != row for row in review["premises"]):
            raise ValueError("application premise changed while preparing review")
        review["premise_refs"] = sorted(premises)
        review["premises"] = [premises[ref] for ref in review["premise_refs"]]
    return {
        **data,
        "change": change,
        "applications": apps,
        "obligation_versions": {i: obligations[i]["version"] for i in ids},
    }
