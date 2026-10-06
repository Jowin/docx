import { useMemo, useState } from "react";
import { qs, rt } from "../api";
import { ago, Badge, ErrorBox, Flags, Json, Kv, Mermaid, Status, Tabs, useAction, useLoad } from "../components";

export default function Extractions({ jobId }: { jobId?: string }) {
  const [status, setStatus] = useState("");
  const [flagged, setFlagged] = useState("");
  const list = useLoad(() => rt.get<any[]>(`/extractions${qs({ status, flagged, limit: 100 })}`), [status, flagged], 5000);

  return (
    <>
      <h1>Extractions</h1>
      <p className="lede">Submit a file to the runtime and follow it through the extraction graph: ingestion filter,
        parsing, classification by the config's detection rules, extraction, verification and routing.</p>
      <div className="split">
        <div>
          <Submit onDone={(id) => { list.reload(); window.location.hash = `/extractions/${id}`; }} />
          <div className="card tight">
            <div className="row" style={{ padding: 10 }}>
              <select value={status} onChange={(e) => setStatus(e.target.value)}>
                <option value="">any status</option><option value="extracted">extracted</option>
                <option value="inprogress">in progress</option>
              </select>
              <select value={flagged} onChange={(e) => setFlagged(e.target.value)}>
                <option value="">flagged or not</option><option value="true">flagged</option><option value="false">clean</option>
              </select>
              <span className="spacer" />
              <button onClick={list.reload}>Refresh</button>
            </div>
            <ErrorBox error={list.error} />
            <table>
              <tbody>
                {(list.data ?? []).map((j) => (
                  <tr key={j.job_id} className={`click ${j.job_id === jobId ? "sel" : ""}`}
                    onClick={() => (window.location.hash = `/extractions/${j.job_id}`)}>
                    <td>
                      <div className="mono">{j.job_id.slice(0, 12)}</div>
                      <div className="muted small">{j.client}/{j.usecase} · {ago(j.created_at)}</div>
                    </td>
                    <td style={{ textAlign: "right" }}>
                      <Status value={j.status} />{" "}
                      {j.status === "extracted" ? (j.flagged ? <Badge tone="warn">flagged</Badge> : <Badge tone="ok">clean</Badge>) : null}
                    </td>
                  </tr>
                ))}
                {list.data?.length === 0 ? <tr><td className="empty">Nothing yet.</td></tr> : null}
              </tbody>
            </table>
          </div>
        </div>
        <div>{jobId ? <JobDetail jobId={jobId} /> : <div className="card empty">Pick an extraction, or submit one.</div>}</div>
      </div>
    </>
  );
}

function Submit({ onDone }: { onDone: (jobId: string) => void }) {
  const inputs = useLoad(() => rt.get("/inputs"), []);
  const configs = useLoad(() => rt.get("/configs"), []);
  const [file, setFile] = useState("");
  const [target, setTarget] = useState("");
  const [version, setVersion] = useState("");
  const act = useAction();
  const entries: any[] = configs.data?.configs ?? [];
  const chosen = entries.find((c) => `${c.client}/${c.usecase}` === target);

  const submit = () => act.run(async () => {
    const [client, usecase] = target ? target.split("/") : [undefined, undefined];
    const r = await rt.post("/extract", { file_location: file, client, usecase, version: version || undefined, async: true });
    onDone(r.job_id);
  });

  return (
    <div className="card form">
      <h2>New extraction</h2>
      <label className="field">File (under the runtime's input root)
        <input list="dx-inputs" value={file} onChange={(e) => setFile(e.target.value)} placeholder="invoice.pdf" />
        <datalist id="dx-inputs">{(inputs.data?.files ?? []).map((f: any) => <option key={f.path} value={f.path} />)}</datalist>
      </label>
      <div className="grid g2">
        <label className="field">Use case
          <select value={target} onChange={(e) => { setTarget(e.target.value); setVersion(""); }}>
            <option value="">default ({configs.data?.defaults?.client}/{configs.data?.defaults?.usecase})</option>
            {entries.map((c) => <option key={`${c.client}/${c.usecase}`}>{c.client}/{c.usecase}</option>)}
          </select>
        </label>
        <label className="field">Version
          <select value={version} onChange={(e) => setVersion(e.target.value)}>
            <option value="">served{chosen?.active ? ` (${chosen.active})` : ""}</option>
            <option value="latest">latest candidate</option>
            {(chosen?.versions ?? []).map((v: string) => <option key={v} value={v}>{v} · {chosen.status?.[v]}</option>)}
          </select>
        </label>
      </div>
      <ErrorBox error={act.error} />
      <div className="row"><button className="primary" disabled={!file || act.busy} onClick={submit}>{act.busy ? "Submitting…" : "Extract"}</button>
        <span className="muted small">Runs as a job; the result appears on the right.</span></div>
    </div>
  );
}

function JobDetail({ jobId }: { jobId: string }) {
  const job = useLoad(() => rt.get(`/extractions/${jobId}?extended=true`), [jobId], 3000);
  const graph = useLoad(() => rt.get<string>("/graph"), []);
  const [tab, setTab] = useState("Result");
  const res = job.data?.result;
  const meta = res?.metadata ?? {};
  const path: string[] = meta.graph?.path ?? [];

  const fieldRows = useMemo(() => {
    const rec = res?.records ?? [];
    return rec.map((r: any, i: number) => ({ i, r }));
  }, [res]);

  if (job.error) return <ErrorBox error={job.error} />;
  if (!job.data) return <div className="card empty">Loading…</div>;
  const d = job.data;

  return (
    <>
      <div className="card">
        <div className="card-head">
          <div>
            <h2 className="mono" style={{ marginBottom: 2 }}>{d.job_id}</h2>
            <div className="muted small">{d.client}/{d.usecase} · created {ago(d.created_at)}</div>
          </div>
          <div className="row"><Status value={d.status} />{d.status === "extracted" ? <Flags flags={d.flags} /> : null}</div>
        </div>
        {d.status !== "extracted" ? <div className="note">Waiting for a worker… this view refreshes itself.</div> : null}
        {res ? (
          <Kv items={[
            ["Config", <span className="mono">{meta.config?.client}/{meta.config?.usecase}@{meta.config?.version} <Status value={meta.config?.release_status} /></span>],
            ["Email type", meta.classification ? <span>{meta.classification.type ?? "—"} <Status value={meta.classification.status} /> {meta.classification.score != null ? <span className="muted">score {meta.classification.score}</span> : null}</span> : "—"],
            ["Input", meta.input ? `${meta.input.name} (${meta.input.kind}, ${meta.input.bytes} bytes)` : "—"],
            ["Confidence", res.confidence != null ? res.confidence : "—"],
            ["Skills applied", (meta.skills_applied ?? []).join(", ") || "—"],
            ["Audit id", <span className="mono">{d.audit_id}</span>],
            ["Transform rules", meta.transform
              ? <span>{meta.transform.layers.map((l: any) => l.level).join(" → ")} · {meta.transform.records_changed} record(s) changed</span>
              : <span className="muted">none</span>],
            ["Files written", (meta.outputs ?? []).length
              ? <span className="mono small">{meta.outputs.map((o: any) => o.path ?? `${o.format}: ${o.error}`).join(", ")}</span>
              : <span className="muted">none</span>],
          ]} />
        ) : null}
        {res ? (
          <div className="row" style={{ marginTop: 10 }}>
            <span className="muted small">Download</span>
            {["csv", "xlsx", "docx", "pdf"].map((f) => (
              <a key={f} className="btn small" href={`/api/runtime/extractions/${d.job_id}/output/${f}`} download>{f.toUpperCase()}</a>
            ))}
          </div>
        ) : null}
      </div>
      {res ? (
        <div className="card">
          <Tabs tabs={["Result", "Graph", "Documents", "Classification", "Raw"]} value={tab} onChange={setTab} />
          {tab === "Result" ? (
            fieldRows.length === 0 ? <div className="empty">No records{res.error ? `: ${res.error.message}` : ""}.</div> :
              fieldRows.map(({ i, r }: any) => (
                <div key={i} style={{ marginBottom: 14 }}>
                  <div className="row" style={{ marginBottom: 6 }}><strong>Record {i + 1}</strong><span className="muted">confidence {r.confidence}</span><Flags flags={r.flags} /></div>
                  <table>
                    <thead><tr><th>Field</th><th>Value</th><th>Conf.</th><th>Source</th><th>Rule</th></tr></thead>
                    <tbody>
                      {Object.entries(r.fields ?? {}).map(([name, f]: any) => (
                        <tr key={name}>
                          <td>{name}</td>
                          <td className="mono">{f.items ? `${f.items.length} items` : f.value === null ? <span className="muted">—</span> : String(f.value)}</td>
                          <td className="num">{f.confidence ?? "—"}</td>
                          <td className="mono small muted">{f.source ?? ""}</td>
                          <td className="small">{f.transform ? <span title={`was ${JSON.stringify(f.transform.from)}`}>
                            <Badge tone="info">{f.transform.rule}</Badge> <span className="muted mono">{String(f.transform.from ?? "—")} →</span></span> : null}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ))
          ) : null}
          {tab === "Graph" ? (
            <>
              <Mermaid source={graph.data} visited={path} />
              <h3>Path and timings</h3>
              <div className="steps">
                {path.map((n, i) => (
                  <div className="step" key={i}><span className="dot ok" /><span className="mono">{n}</span>
                    <span className="muted small">{meta.timings_ms?.[n] != null ? `${meta.timings_ms[n]} ms` : ""}</span></div>
                ))}
              </div>
            </>
          ) : null}
          {tab === "Documents" ? (
            <>
              <table>
                <thead><tr><th>Id</th><th>Source</th><th>Kind</th><th>Status</th><th>Blocks</th></tr></thead>
                <tbody>{(meta.documents ?? []).map((doc: any) => (
                  <tr key={doc.id}><td className="mono">{doc.id}</td><td className="mono small">{doc.source}</td><td>{doc.kind}</td>
                    <td><Status value={doc.status === "read" ? "extracted" : "failed"} /> {doc.reason ?? ""}</td><td className="num">{doc.blocks}</td></tr>
                ))}</tbody>
              </table>
              <h3>Skipped by intake or the ingestion filter</h3>
              {(meta.skipped ?? []).length ? <Json value={meta.skipped} max={240} /> : <div className="muted">Nothing skipped.</div>}
            </>
          ) : null}
          {tab === "Classification" ? (
            meta.classification?.scores && Object.keys(meta.classification.scores).length ? (
              <table>
                <thead><tr><th>Type</th><th>Score</th><th>Threshold</th><th>Matched</th><th>Negative signals</th></tr></thead>
                <tbody>{Object.entries(meta.classification.scores).map(([t, s]: any) => (
                  <tr key={t}><td>{t}</td><td className="num">{s.score}</td><td className="num">{s.threshold}</td>
                    <td>{s.matched ? <Badge tone="ok">yes</Badge> : <Badge>no</Badge>}</td>
                    <td>{(s.negative_signals ?? []).join(", ")}</td></tr>
                ))}</tbody>
              </table>
            ) : <div className="empty">This config has no detection rules: nothing was classified.</div>
          ) : null}
          {tab === "Raw" ? <Json value={res} /> : null}
        </div>
      ) : null}
    </>
  );
}
