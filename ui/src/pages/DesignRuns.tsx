import { useState } from "react";
import { dt, qs, rt } from "../api";
import { ago, Badge, ErrorBox, Json, Kv, Mermaid, pct, Status, Tabs, useAction, useLoad } from "../components";

export default function DesignRuns({ runId, who }: { runId?: string; who: string }) {
  const [kind, setKind] = useState("");
  const list = useLoad(() => dt.get<any[]>(`/learning/runs${qs({ kind, limit: 100 })}`), [kind], 10000);
  const [start, setStart] = useState<"" | "pattern" | "authoring">("");

  return (
    <>
      <h1>Design runs</h1>
      <p className="lede">One path, two scales. An <strong>authoring run</strong> designs a use case from a labelled corpus;
        <strong> pattern learning</strong> refines it from one sample. Both build a config version folder, score it with the
        real runtime, and publish it as a candidate.</p>
      <div className="row" style={{ marginBottom: 12 }}>
        <button className={start === "pattern" ? "primary" : ""} onClick={() => setStart(start === "pattern" ? "" : "pattern")}>Learn from a sample</button>
        <button className={start === "authoring" ? "primary" : ""} onClick={() => setStart(start === "authoring" ? "" : "authoring")}>Author from a corpus</button>
      </div>
      {start === "pattern" ? <StartLearning who={who} onDone={(id) => { setStart(""); list.reload(); window.location.hash = `/runs/${id}`; }} /> : null}
      {start === "authoring" ? <StartAuthoring who={who} onDone={(id) => { setStart(""); list.reload(); window.location.hash = `/runs/${id}`; }} /> : null}
      <div className="split">
        <div className="card tight">
          <div className="row" style={{ padding: 10 }}>
            <select value={kind} onChange={(e) => setKind(e.target.value)}>
              <option value="">all kinds</option><option value="pattern">pattern learning</option><option value="authoring">authoring</option>
            </select>
            <span className="spacer" /><button onClick={list.reload}>Refresh</button>
          </div>
          <ErrorBox error={list.error} />
          <table>
            <tbody>
              {(list.data ?? []).map((r) => (
                <tr key={r.id} className={`click ${r.id === runId ? "sel" : ""}`} onClick={() => (window.location.hash = `/runs/${r.id}`)}>
                  <td>
                    <div className="row"><Status value={r.kind} /><strong>{r.client}/{r.usecase}</strong></div>
                    <div className="muted small">{r.kind === "pattern" ? `${r.pattern_name} · ${r.source}` : r.source} · {ago(r.created_at)}</div>
                  </td>
                  <td style={{ textAlign: "right" }}>
                    <Status value={r.outcome} />
                    <div className="mono small muted">{r.base_version ?? "—"} → {r.result_version ?? "—"}</div>
                  </td>
                </tr>
              ))}
              {list.data?.length === 0 ? <tr><td className="empty">No design runs yet.</td></tr> : null}
            </tbody>
          </table>
        </div>
        <div>{runId ? <RunDetail runId={runId} /> : <div className="card empty">Pick a run.</div>}</div>
      </div>
    </>
  );
}

function RunDetail({ runId }: { runId: string }) {
  const run = useLoad(() => dt.get(`/learning/runs/${runId}`), [runId], 0);
  const kind = run.data?.kind ?? "pattern";
  const graph = useLoad(() => (run.data ? dt.get<string>(`/graphs/${kind === "authoring" ? "authoring" : "learning"}`) : Promise.resolve("")), [kind, !!run.data]);
  const [tab, setTab] = useState("Summary");
  if (run.error) return <ErrorBox error={run.error} />;
  if (!run.data) return <div className="card empty">Loading…</div>;
  const r = run.data;
  const tabs = kind === "authoring" ? ["Summary", "Stages", "Graph", "Raw"] : ["Summary", "Attempts", "Skill", "Graph", "Raw"];
  return (
    <div className="card">
      <div className="card-head">
        <div>
          <h2 style={{ marginBottom: 2 }}>{kind === "authoring" ? "Authoring run" : `Pattern ${r.pattern_name}`}</h2>
          <div className="row"><Status value={kind} /><Status value={r.outcome} />
            <span className="muted small">{r.client}/{r.usecase} · by {r.requested_by} · {ago(r.created_at)}</span></div>
        </div>
        {r.result_version ? <a href={`#/configs/${r.client}/${r.usecase}/${r.result_version}`}>version {r.result_version} →</a> : null}
      </div>
      <Tabs tabs={tabs} value={tab} onChange={setTab} />
      {tab === "Summary" ? (kind === "authoring" ? <AuthoringSummary r={r} /> : <PatternSummary r={r} />) : null}
      {tab === "Stages" ? (
        <div className="steps">
          {(r.attempts ?? []).map((s: any, i: number) => (
            <div className="step" key={i}><span className="dot ok" />
              <div><strong>{s.stage}</strong> <span className="muted small">{s.agent}</span>
                <pre className="block small" style={{ marginTop: 4, maxHeight: 180 }}>{JSON.stringify(s.summary, null, 2)}</pre></div><span /></div>
          ))}
        </div>
      ) : null}
      {tab === "Attempts" ? (
        (r.attempts ?? []).length === 0 ? <div className="empty">The base already passed: nothing to learn.</div> :
          <table>
            <thead><tr><th>#</th><th>Candidate</th><th>Passed</th><th>Accepted</th><th>Score</th><th>New hints</th><th>Regressions</th></tr></thead>
            <tbody>{r.attempts.map((a: any) => (
              <tr key={a.attempt}><td>{a.attempt}</td><td className="mono">{a.version}</td>
                <td>{a.passed ? <Badge tone="ok">yes</Badge> : <Badge tone="warn">no</Badge>}</td>
                <td>{a.accepted ? <Badge tone="ok">yes</Badge> : <Badge>no</Badge>}</td>
                <td className="mono small">{a.score?.value_failures} wrong · {a.score?.flag_failures ?? 0} flags</td>
                <td className="small">{(a.new_hints ?? []).map((h: any) => `${h.field}: ${h.kind} "${h.hint}"`).join("; ") || "—"}</td>
                <td className="small">{(a.regressions ?? []).join(", ") || "—"}</td></tr>
            ))}</tbody>
          </table>
      ) : null}
      {tab === "Skill" ? (r.skill ? <pre className="block">{r.skill}</pre> : <div className="empty">No skill was written.</div>) : null}
      {tab === "Graph" ? <><Mermaid source={graph.data} visited={r.path ?? []} /><div className="muted small" style={{ marginTop: 6 }}>Highlighted: the nodes this run went through.</div></> : null}
      {tab === "Raw" ? <Json value={r} /> : null}
    </div>
  );
}

function AuthoringSummary({ r }: { r: any }) {
  const m = r.verdict_after?.metrics ?? {};
  const g = r.verdict_after?.gate_results ?? {};
  return (
    <div className="grid g2">
      <Kv items={[
        ["Corpus", <span className="mono">{r.source}</span>], ["Types", r.object ?? "—"],
        ["Built on", r.base_version ?? "new use case"], ["Published", r.result_version ?? "not published"],
        ["Scope check", r.verdict_after?.scope ? `${r.verdict_after.scope.turned_away}/${r.verdict_after.scope.out_of_scope_samples} out-of-scope turned away` : "—"],
        ["Error", r.error ? <span style={{ color: "var(--bad)" }}>{r.error.message}</span> : null],
      ]} />
      <table>
        <thead><tr><th>Metric (runtime)</th><th>Value</th><th>Gate</th></tr></thead>
        <tbody>{Object.entries(m).map(([k, v]: any) => (
          <tr key={k}><td>{k.replace(/_/g, " ")}</td><td className="num">{v === null ? "—" : k.includes("gap") ? v : pct(v)}</td>
            <td>{g[k] === undefined ? "—" : g[k] ? <Badge tone="ok">pass</Badge> : <Badge tone="bad">fail</Badge>}</td></tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function PatternSummary({ r }: { r: any }) {
  return (
    <div className="grid g2">
      <Kv items={[
        ["Source", <span className="mono">{r.source}</span>], ["Object", r.object ?? "—"],
        ["Before", r.passed_before ? <Badge tone="ok">passed</Badge> : <Badge tone="warn">failed</Badge>],
        ["After", r.passed_after === null || r.passed_after === undefined ? "—" : r.passed_after ? <Badge tone="ok">passes</Badge> : <Badge tone="warn">still fails</Badge>],
        ["Versions", <span className="mono">{r.base_version ?? "—"} → {r.result_version ?? "—"}</span>],
        ["Reference text", r.reference_text ?? "—"],
      ]} />
      <div>
        <h3 style={{ marginTop: 0 }}>Failures before</h3>
        {(r.verdict_before?.failures ?? []).length ? (
          <table><tbody>{r.verdict_before.failures.map((f: any, i: number) => (
            <tr key={i}><td>{f.field ?? "—"}</td><td><Badge tone="warn">{f.kind}</Badge></td>
              <td className="mono small">{f.expected !== undefined ? `want ${JSON.stringify(f.expected)}` : ""} {f.got !== undefined ? `got ${JSON.stringify(f.got)}` : ""}</td></tr>
          ))}</tbody></table>
        ) : <div className="muted">None.</div>}
        {r.ground_truth ? <><h3>Ground truth</h3><Json value={r.ground_truth} max={200} /></> : null}
      </div>
    </div>
  );
}

function StartLearning({ who, onDone }: { who: string; onDone: (id: string) => void }) {
  const inputs = useLoad(() => rt.get("/inputs"), []);
  const configs = useLoad(() => dt.get("/configs"), []);
  const [f, setF] = useState({ source: "", target: "", pattern_name: "", ground_truth: "", reference_text: "", scope: "pattern" });
  const act = useAction();
  const submit = () => act.run(async () => {
    const [client, usecase] = f.target ? f.target.split("/") : [undefined, undefined];
    let ground_truth: unknown = undefined;
    if (f.ground_truth.trim()) ground_truth = JSON.parse(f.ground_truth);
    const r = await dt.post("/learning/runs", {
      source: f.source, pattern_name: f.pattern_name, client, usecase, ground_truth,
      reference_text: f.reference_text || undefined, scope: f.scope, requested_by: who || "console",
    });
    onDone(r.id);
  });
  return (
    <div className="card form">
      <h2>Learn from a sample</h2>
      <p className="muted small" style={{ margin: 0 }}>The sample is extracted in an isolated runtime. If it fails (wrong values
        against the ground truth, or any flag without one), a skill is written for its pattern and tested, together with
        every earlier sample, until it passes; the result is published as the next patch candidate.</p>
      <div className="grid g3">
        <label className="field">Source file
          <input list="dx-learn-inputs" value={f.source} onChange={(e) => setF({ ...f, source: e.target.value })} />
          <datalist id="dx-learn-inputs">{(inputs.data?.files ?? []).map((x: any) => <option key={x.path} value={x.path} />)}</datalist>
        </label>
        <label className="field">Use case
          <select value={f.target} onChange={(e) => setF({ ...f, target: e.target.value })}>
            <option value="">default</option>
            {(configs.data?.usecases ?? []).map((u: any) => <option key={`${u.client}/${u.usecase}`}>{u.client}/{u.usecase}</option>)}
          </select>
        </label>
        <label className="field">Pattern name<input value={f.pattern_name} placeholder="acme-remittance" onChange={(e) => setF({ ...f, pattern_name: e.target.value })} /></label>
      </div>
      <div className="grid g2">
        <label className="field">Ground truth (JSON, optional)
          <textarea value={f.ground_truth} placeholder='{"invoice_number": "INV-1", "total_amount": 99.5}' onChange={(e) => setF({ ...f, ground_truth: e.target.value })} /></label>
        <label className="field">Reference text (optional)
          <textarea value={f.reference_text} placeholder='invoice_number is labelled "Our Ref"' onChange={(e) => setF({ ...f, reference_text: e.target.value })} /></label>
      </div>
      <label className="check"><input type="checkbox" checked={f.scope === "global"} onChange={(e) => setF({ ...f, scope: e.target.checked ? "global" : "pattern" })} />
        Apply the skill to every document (not only ones that look like this sample)</label>
      <ErrorBox error={act.error} />
      <div className="row"><button className="primary" disabled={act.busy || !f.source || !f.pattern_name} onClick={submit}>{act.busy ? "Learning… (runs the runtime)" : "Start"}</button></div>
    </div>
  );
}

function StartAuthoring({ who, onDone }: { who: string; onDone: (id: string) => void }) {
  const [request, setRequest] = useState("");
  const [f, setF] = useState({ client_id: "", workflow_id: "", confirmed_types: "", confirmed_critical: "", floor: true });
  const act = useAction();
  const [proposed, setProposed] = useState<string[] | null>(null);
  const load = async (file: File) => setRequest(await file.text());
  const submit = () => act.run(async () => {
    const body = JSON.parse(request);
    const types = f.confirmed_types.split(",").map((s) => s.trim()).filter(Boolean);
    const critical: Record<string, string[]> = {};
    for (const t of types) critical[t] = f.confirmed_critical.split(",").map((s) => s.trim()).filter(Boolean);
    try {
      const r = await dt.post("/runs", {
        corpus: body.corpus ?? body, corpus_root: body.corpus_root, client_id: f.client_id, workflow_id: f.workflow_id,
        confirmed_types: types, confirmed_critical: critical, requested_by: who || "console",
        enforce_corpus_floor: f.floor,
      });
      onDone(r.run_id);
    } catch (e: any) {
      if (e?.code === "confirmation_required" && e?.detail?.proposed) setProposed(e.detail.proposed);
      throw e;
    }
  });
  return (
    <div className="card form">
      <h2>Author from a corpus</h2>
      <p className="muted small" style={{ margin: 0 }}>Profiles the corpus, proposes email types (confirm them below),
        writes field schemas, detection rules and skills, evaluates the candidate folder in the runtime twice (to tune
        thresholds), and publishes the next minor version as a candidate.</p>
      <label className="field">Corpus request (corpus.request.json from samples/generate_corpus.py, or a corpus object)
        <input type="file" accept=".json,application/json" onChange={(e) => e.target.files?.[0] && load(e.target.files[0])} />
        <textarea rows={5} value={request.length > 4000 ? request.slice(0, 4000) + "\n…" : request} readOnly={request.length > 4000}
          onChange={(e) => setRequest(e.target.value)} placeholder='{"corpus": {...}, "corpus_root": "/data/corpus"}' />
      </label>
      <div className="grid g4">
        <label className="field">Client<input value={f.client_id} onChange={(e) => setF({ ...f, client_id: e.target.value })} placeholder="acme" /></label>
        <label className="field">Use case<input value={f.workflow_id} onChange={(e) => setF({ ...f, workflow_id: e.target.value })} placeholder="ap-invoices" /></label>
        <label className="field">Confirmed types<input value={f.confirmed_types} onChange={(e) => setF({ ...f, confirmed_types: e.target.value })} placeholder="invoice" /></label>
        <label className="field">Critical fields<input value={f.confirmed_critical} onChange={(e) => setF({ ...f, confirmed_critical: e.target.value })} placeholder="amount, due_date" /></label>
      </div>
      {proposed ? <div className="note">Proposed types from the corpus: {proposed.map((p) => <Badge key={p} tone="accent">{p}</Badge>)} — confirm them above.</div> : null}
      <label className="check"><input type="checkbox" checked={f.floor} onChange={(e) => setF({ ...f, floor: e.target.checked })} /> Enforce the corpus size floor (DT-18)</label>
      <ErrorBox error={act.error} />
      <div className="row"><button className="primary" disabled={act.busy || !request || !f.client_id || !f.workflow_id} onClick={submit}>
        {act.busy ? "Authoring… (evaluates in the runtime)" : "Start"}</button></div>
    </div>
  );
}
