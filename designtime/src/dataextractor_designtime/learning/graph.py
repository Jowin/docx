"""The pattern-learning loop, as a LangGraph state graph.

    START -> extract_base -> judge_base --(passed)----------------------------> finish -> END
                                        \\-(failed)-> load_regressions -> write_skill
             write_skill --(new hints or body)--> build_candidate -> test_candidate
                         \\-(nothing new)------------------------------------> finish
             test_candidate --(passes, no regressions)--> publish -> finish
                            \\-(attempts left)--> write_skill
                            \\-(out of attempts)--> publish (if any candidate was accepted) | finish

Every extraction runs in the isolated runtime (``learning.runtime``) against a
scratch copy of the configs, so the base and each candidate are tested by the
engine that will serve them, and nothing reaches the live configs until
``publish`` copies the accepted candidate in as a new patch version.

A candidate is accepted when it scores strictly better than the best so far
(fewer wrong values, then fewer review reasons) and every earlier sample that
passed on the base still passes. The loop stops at the first candidate that
passes, when the writer has nothing new to add, or after ``max_iterations``.
"""

from __future__ import annotations

import operator
import uuid
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from ..agents.base import AgentError
from ..agents.extraction_judge import ExtractionJudge, ExtractionJudgeInput, ExtractionJudgeOutput
from ..agents.pattern_skill_writer import PatternSkillWriter, PatternSkillWriterInput
from ..model.base import ModelClient
from . import configs
from .models import Attempt, LearnRequest, LearnResponse
from .runtime import IsolatedRuntime
from .store import LearningStore

GENERATED_BY = "designtime:pattern-learning@0.1.0"
_HTTP = {"file_not_found": 404, "config_not_found": 404, "location_outside_input_root": 400,
         "config_invalid_name": 400, "config_ambiguous": 400, "input_too_large": 413,
         "model_unavailable": 503, "model_timeout": 504}


class LearnState(TypedDict, total=False):
    run_id: str
    req: LearnRequest
    scratch: Path
    max_iterations: int
    # resolved by the base run
    client: str
    usecase: str
    base_version: str
    dictionary: dict[str, Any]
    object: str
    source_sha256: str
    base_result: dict[str, Any]
    verdict_before: ExtractionJudgeOutput
    # regression set: earlier samples that pass on the base
    regressions: list[dict[str, Any]]
    # the loop
    attempt: int
    latest_result: dict[str, Any]
    latest_verdict: ExtractionJudgeOutput
    hints: dict[str, Any]
    skill: dict[str, Any]                 # the writer's latest output
    candidate_version: str
    best: dict[str, Any] | None           # {"markdown", "verdict", "result", "attempt"}
    attempts: Annotated[list[Attempt], operator.add]
    tried: Annotated[list[dict[str, Any]], operator.add]   # hint sets already tested
    result_version: str | None
    published: bool
    path: Annotated[list[str], operator.add]


def _ok(result: dict[str, Any], what: str) -> dict[str, Any]:
    if result.get("ok"):
        return result
    err = result.get("error") or {}
    code = str(err.get("error") or "runtime_failed")
    raise AgentError(f"{what}: {err.get('message', code)}", code=code,
                     status=int(err.get("status") or _HTTP.get(code, 422)), detail=err.get("detail"))


def build_learning_graph(*, runtime: IsolatedRuntime, store: LearningStore, model: ModelClient | None,
                         config_root: Path, regression_limit: int = 20):
    judge = ExtractionJudge(model=model)
    writer = PatternSkillWriter(model=model)

    def run_once(state: LearnState, version: str, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        runs = [{"file_location": s["source"], "client": state["client"], "usecase": state["usecase"],
                 "version": version} for s in sources]
        return runtime.run(state["scratch"], runs, include_evidence=True)

    def judged(state: LearnState, result: dict[str, Any], ground_truth, strict: bool) -> ExtractionJudgeOutput:
        return judge.run(ExtractionJudgeInput(output=result["output"], dictionary=result["dictionary"],
                                              ground_truth=ground_truth, strict=strict))

    # ------------------------------------------------------------------ nodes

    def extract_base(state: LearnState) -> dict[str, Any]:
        req = state["req"]
        # learning builds on the newest version unless told otherwise, so each call
        # adds to what earlier calls learned (a pinned default in defaults.json is
        # for serving, not for learning)
        [result] = runtime.run(state["scratch"], [{"file_location": req.source, "client": req.client,
                                                   "usecase": req.usecase, "version": req.version or "latest"}],
                               include_evidence=True)
        result = _ok(result, "the base extraction failed")
        cfg, dictionary = result["config"], result["dictionary"]
        if req.object and req.object != dictionary.get("name"):
            raise AgentError(f"{cfg['client']}/{cfg['usecase']}@{cfg['version']} extracts "
                             f"{dictionary.get('name')!r}, not {req.object!r}", code="object_mismatch",
                             detail={"config_object": dictionary.get("name")})
        return {"client": cfg["client"], "usecase": cfg["usecase"], "base_version": cfg["version"],
                "dictionary": dictionary, "object": dictionary.get("name") or "",
                "source_sha256": result["output"]["metadata"]["input"]["sha256"],
                "base_result": result, "latest_result": result, "path": ["extract_base"]}

    def judge_base(state: LearnState) -> dict[str, Any]:
        req = state["req"]
        verdict = judged(state, state["base_result"], req.ground_truth, req.strict)
        folder = state["scratch"] / state["client"] / state["usecase"] / state["base_version"]
        return {"verdict_before": verdict, "latest_verdict": verdict, "attempt": 0, "best": None,
                "hints": configs.existing_hints(folder, req.pattern_name), "path": ["judge_base"]}

    def load_regressions(state: LearnState) -> dict[str, Any]:
        rows = store.regression_samples(state["client"], state["usecase"],
                                        exclude_sha256=state["source_sha256"], limit=regression_limit)
        samples = [{"source": r.source, "ground_truth": r.ground_truth, "pattern": r.pattern_name}
                   for r in rows]
        keep = []
        for sample, result in zip(samples, run_once(state, state["base_version"], samples)):
            if result.get("ok") and judged(state, result, sample["ground_truth"], True).passed:
                keep.append(sample)      # only samples the base gets right can regress
        return {"regressions": keep, "path": ["load_regressions"]}

    def write_skill(state: LearnState) -> dict[str, Any]:
        req, attempt = state["req"], state["attempt"] + 1
        verdict = state["latest_verdict"]
        out = writer.run(PatternSkillWriterInput(
            pattern_name=req.pattern_name, object=state["object"], dictionary=state["dictionary"],
            failures=verdict.failures, failing_fields=verdict.failing_fields,
            ground_truth=req.ground_truth, evidence=state["latest_result"].get("evidence", []),
            reference_text=req.reference_text, existing_hints=state["hints"], attempt=attempt,
            trace={"audit_id": state["run_id"], "client": state["client"], "usecase": state["usecase"],
                   "config_version": state["base_version"]}))
        return {"attempt": attempt, "skill": out.to_dict(), "path": ["write_skill"]}

    def has_new(state: LearnState) -> bool:
        skill = state["skill"]
        return skill["hints"] not in state.get("tried", []) or state["attempt"] == 1

    def build_candidate(state: LearnState) -> dict[str, Any]:
        req = state["req"]
        version = configs.next_patch(state["base_version"],
                                     configs.versions(config_root, state["client"], state["usecase"]),
                                     configs.versions(state["scratch"], state["client"], state["usecase"]))
        configs.write_candidate(state["scratch"], state["client"], state["usecase"], state["base_version"],
                                version, req.pattern_name, state["skill"]["markdown"])
        return {"candidate_version": version, "tried": [state["skill"]["hints"]], "path": ["build_candidate"]}

    def test_candidate(state: LearnState) -> dict[str, Any]:
        req, version = state["req"], state["candidate_version"]
        regressions = state.get("regressions", [])
        results = run_once(state, version, [{"source": req.source}] + regressions)
        notes: list[str] = []
        main = results[0]
        if not main.get("ok"):
            # the candidate broke the config (bad hints) or the run: record and try again
            err = main.get("error") or {}
            notes.append(f"candidate_failed:{err.get('error')}:{err.get('message', '')}"[:300])
            attempt = Attempt(attempt=state["attempt"], version=version, passed=False,
                              score=state["verdict_before"].score.replace(
                                  value_failures=state["verdict_before"].score.value_failures + 1),
                              accepted=False, notes=notes,
                              new_hints=state["skill"]["new_hints"])
            return {"attempts": [attempt], "path": ["test_candidate"]}
        verdict = judged(state, main, req.ground_truth, req.strict)
        broken = [s["source"] for s, r in zip(regressions, results[1:])
                  if not r.get("ok") or not judged(state, r, s["ground_truth"], True).passed]
        best = state.get("best")
        bar = (best["verdict"] if best else state["verdict_before"]).score.key()
        accepted = verdict.score.key() < bar and not broken
        attempt = Attempt(attempt=state["attempt"], version=version, passed=verdict.passed and not broken,
                          score=verdict.score, failures=verdict.failures, regressions=broken,
                          accepted=accepted, new_hints=state["skill"]["new_hints"],
                          notes=state["skill"]["notes"])
        update: dict[str, Any] = {"attempts": [attempt], "latest_result": main, "latest_verdict": verdict,
                                  "path": ["test_candidate"]}
        if accepted:
            update["best"] = {"markdown": state["skill"]["markdown"], "verdict": verdict, "result": main,
                              "attempt": state["attempt"]}
            update["hints"] = state["skill"]["hints"]
        return update

    def publish(state: LearnState) -> dict[str, Any]:
        req, best = state["req"], state["best"]
        assert best is not None
        candidate = configs.write_candidate(state["scratch"], state["client"], state["usecase"],
                                            state["base_version"], state["candidate_version"],
                                            req.pattern_name, best["markdown"])
        if not req.publish:
            return {"result_version": state["candidate_version"], "published": False, "path": ["publish"]}
        version = configs.publish(candidate, config_root, state["client"], state["usecase"],
                                  state["base_version"])
        return {"result_version": version, "published": True, "path": ["publish"]}

    def finish(state: LearnState) -> dict[str, Any]:
        return {"path": ["finish"]}

    # ------------------------------------------------------------------ routing

    def after_judge(state: LearnState) -> str:
        return "finish" if state["verdict_before"].passed else "load_regressions"

    def after_write(state: LearnState) -> str:
        if not has_new(state):
            return "publish" if state.get("best") else "finish"      # nothing new to try
        return "build_candidate"

    def after_test(state: LearnState) -> str:
        last = state["attempts"][-1]
        if last.accepted and last.passed:
            return "publish"
        if state["attempt"] >= state["max_iterations"]:
            return "publish" if state.get("best") else "finish"
        return "write_skill"

    g = StateGraph(LearnState)
    for name, fn in (("extract_base", extract_base), ("judge_base", judge_base),
                     ("load_regressions", load_regressions), ("write_skill", write_skill),
                     ("build_candidate", build_candidate), ("test_candidate", test_candidate),
                     ("publish", publish), ("finish", finish)):
        g.add_node(name, fn)
    g.add_edge(START, "extract_base")
    g.add_edge("extract_base", "judge_base")
    g.add_conditional_edges("judge_base", after_judge, ["finish", "load_regressions"])
    g.add_edge("load_regressions", "write_skill")
    g.add_conditional_edges("write_skill", after_write, ["build_candidate", "publish", "finish"])
    g.add_edge("build_candidate", "test_candidate")
    g.add_conditional_edges("test_candidate", after_test, ["publish", "write_skill", "finish"])
    g.add_edge("publish", "finish")
    g.add_edge("finish", END)
    return g.compile()


def learn(req: LearnRequest, *, runtime: IsolatedRuntime, store: LearningStore, model: ModelClient | None,
          config_root: Path, max_iterations: int, regression_limit: int = 20) -> LearnResponse:
    """Run the learning graph for one request and record it in the registry."""
    graph = build_learning_graph(runtime=runtime, store=store, model=model, config_root=Path(config_root),
                                 regression_limit=regression_limit)
    tmp, scratch = configs.scratch_copy(config_root)
    run_id = str(uuid.uuid4())
    try:
        iterations = req.max_iterations or max_iterations
        state = graph.invoke({"run_id": run_id, "req": req, "scratch": scratch, "max_iterations": iterations,
                              "attempts": [], "tried": [], "path": []},
                             {"recursion_limit": 10 + 4 * iterations})
    finally:
        tmp.cleanup()

    before, best = state["verdict_before"], state.get("best")
    if before.passed:
        outcome = "passed"
    elif best is None:
        outcome = "failed"
    else:
        outcome = "learned" if best["verdict"].passed else "improved"
    after = best["verdict"] if best else None
    final_result = best["result"] if best else state["base_result"]
    skill_path = f"{state['client']}/{state['usecase']}/{state['result_version']}/skills/" \
                 f"{req.pattern_name}.md" if state.get("result_version") else None
    response = LearnResponse(
        id=run_id, outcome=outcome, client=state["client"], usecase=state["usecase"], object=state["object"],
        pattern_name=req.pattern_name, source=req.source, base_version=state["base_version"],
        result_version=state.get("result_version"), published=bool(state.get("published")),
        passed_before=before.passed, passed_after=after.passed if after else None,
        verdict_before=before, verdict_after=after, attempts=state["attempts"],
        regression_samples=len(state.get("regressions", [])), skill_path=skill_path,
        skill=best["markdown"] if best else None, data=final_result["output"]["data"], path=state["path"])
    store.add(id=run_id, client_id=state["client"], usecase=state["usecase"], object_name=state["object"],
              pattern_name=req.pattern_name, source=req.source, source_sha256=state["source_sha256"],
              ground_truth=req.ground_truth, reference_text=req.reference_text,
              base_version=state["base_version"],
              result_version=state.get("result_version") if response.published else None,
              outcome=outcome, passed_before=before.passed,
              passed_after=after.passed if after else None, verdict_before=before.to_dict(),
              verdict_after=after.to_dict() if after else None,
              attempts=[a.to_dict() for a in state["attempts"]],
              skill_path=skill_path if response.published else None,
              skill_markdown=best["markdown"] if best else None,
              generated_by=GENERATED_BY, requested_by=req.requested_by)
    return response
