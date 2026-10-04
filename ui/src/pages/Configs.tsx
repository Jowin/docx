import { useState } from "react";
import { dt } from "../api";
import { ago, Badge, Diff, ErrorBox, Json, Kv, pct, Status, Tabs, useAction, useLoad } from "../components";

export default function Configs({ client, usecase, version, who }: { client?: string; usecase?: string; version?: string; who: string }) {
  if (client && usecase) return <UseCase client={client} usecase={usecase} version={version} who={who} />;
  return <AllUseCases />;
}

function AllUseCases() {
  const all = useLoad(() => dt.get("/configs"), [], 15000);
  return (
    <>
      <h1>Config versions</h1>
      <p className="lede">The runtime's config folders are the source of truth. Each use case keeps immutable version
        folders and a <span className="mono">releases.json</span> that says which one is served. Authoring runs publish
        the next minor version, pattern learning the next patch; both are candidates until released.</p>
      <ErrorBox error={all.error} />
      {all.data ? <div className="muted small" style={{ marginBottom: 10 }}>Root: <span className="mono">{all.data.root}</span></div> : null}
      <div className="grid g3">
        {(all.data?.usecases ?? []).map((u: any) => (
          <a key={`${u.client}/${u.usecase}`} className="card" href={`#/configs/${u.client}/${u.usecase}`} style={{ color: "inherit" }}>
            <div className="card-head"><h2 style={{ margin: 0 }}>{u.client}/{u.usecase}</h2>
              {u.managed ? <Badge tone="ok">managed</Badge> : <Badge tone="warn" title="no releases.json yet">unmanaged</Badge>}</div>
            <Kv items={[
              ["Served", <span className="mono">{u.served ?? "—"}</span>],
              ["Versions", <div className="chips">{u.versions.map((v: any) => <Badge key={v.version} tone={tone(v.status)}>{v.version}</Badge>)}</div>],
              ["Last release", u.history?.length ? `${u.history[u.history.length - 1].action} ${u.history[u.history.length - 1].version} by ${u.history[u.history.length - 1].by}` : "—"],
            ]} />
          </a>
        ))}
      </div>
    </>
  );
}

function tone(status: string) {
  return ({ active: "ok", candidate: "accent", rejected: "bad", unmanaged: "warn" } as Record<string, any>)[status] ?? "";
}

function UseCase({ client, usecase, version, who }: { client: string; usecase: string; version?: string; who: string }) {
  const uc = useLoad(() => dt.get(`/configs/${client}/${usecase}`), [client, usecase]);
  const act = useAction();
  const [note, setNote] = useState("");

  const rollback = () => act.run(async () => {
    await dt.post(`/configs/${client}/${usecase}/rollback`, { by: who || "console", note: note || undefined });
    uc.reload();
  });

  return (
    <>
      <div className="row"><a href="#/configs">Config versions</a><span className="muted">/</span><h1 style={{ margin: 0 }}>{client}/{usecase}</h1></div>
      <p className="lede" style={{ marginTop: 6 }}>Served now: <span className="mono">{uc.data?.served ?? "—"}</span>
        {uc.data && !uc.data.managed ? " (no releases.json yet: the first published candidate pins what is served)" : null}</p>
      <ErrorBox error={uc.error} />
      <div className="card tight">
        <table>
          <thead><tr><th>Version</th><th>Status</th><th>Origin</th><th>From</th><th>Created</th><th>Gates</th><th>Sign-offs</th></tr></thead>
          <tbody>
            {[...(uc.data?.versions ?? [])].reverse().map((v: any) => (
              <tr key={v.version} className={`click ${v.version === version ? "sel" : ""}`}
                onClick={() => (window.location.hash = `/configs/${client}/${usecase}/${v.version}`)}>
                <td className="mono"><strong>{v.version}</strong></td>
                <td><Status value={v.status} /></td>
                <td><Status value={v.origin} /></td>
                <td className="mono">{v.base_version ?? "—"}</td>
                <td className="small">{v.created_by ?? "—"}<div className="muted">{v.created_at ? ago(v.created_at) : ""}</div></td>
                <td>{v.record?.gates_passed === true ? <Badge tone="ok">passed</Badge> : v.record?.gates_passed === false ? <Badge tone="bad">failed</Badge> : <span className="muted">—</span>}</td>
                <td>{v.record?.signoffs?.length ? v.record.signoffs.map((s: any) => <div key={s.identity} className="small">{s.identity}</div>) : <span className="muted">none</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {version ? <VersionDetail client={client} usecase={usecase} version={version} who={who} onChange={uc.reload} /> : null}
      <div className="grid g2">
        <div className="card">
          <h2>Release log</h2>
          {(uc.data?.release_log ?? []).length === 0 ? <div className="muted">No releases recorded yet.</div> : (
            <div className="steps">
              {uc.data.release_log.map((e: any, i: number) => (
                <div className="step" key={i}><span className={`dot ${e.action === "release" ? "ok" : e.action === "reject" ? "bad" : ""}`} />
                  <div><strong>{e.action}</strong> <span className="mono">{e.version}</span>
                    {e.previous ? <span className="muted"> (was {e.previous})</span> : null} by {e.by}
                    {e.gate_override ? <> <Badge tone="warn">gate override</Badge></> : null}
                    {e.note ? <div className="muted small">{e.note}</div> : null}</div>
                  <span className="muted small">{ago(e.at)}</span></div>
              ))}
            </div>
          )}
        </div>
        <div className="card form">
          <h2>Roll back</h2>
          <p className="muted small" style={{ margin: 0 }}>Serves the version that was released before the active one again. The
            active version stays on disk; reject it separately if it should never be built on.</p>
          <label className="field">Why<input value={note} onChange={(e) => setNote(e.target.value)} placeholder="incident #…" /></label>
          <ErrorBox error={act.error} />
          <div className="row"><button className="danger" disabled={act.busy || !uc.data?.active} onClick={rollback}>Roll back</button>
            <span className="muted small">as {who || "console"}</span></div>
        </div>
      </div>
    </>
  );
}

function VersionDetail({ client, usecase, version, who, onChange }: { client: string; usecase: string; version: string; who: string; onChange: () => void }) {
  const base = `/configs/${client}/${usecase}/${version}`;
  const v = useLoad(() => dt.get(base), [base]);
  const [tab, setTab] = useState("Review");
  const [file, setFile] = useState("manifest.json");
  const content = useLoad(() => (tab === "Files" ? dt.get<string>(`${base}/files/${file}`) : Promise.resolve("")), [base, file, tab]);
  const diff = useLoad(() => (tab === "Diff" ? dt.get<string>(`${base}/diff`) : Promise.resolve("")), [base, tab]);
  const act = useAction();
  const [note, setNote] = useState("");
  const [override, setOverride] = useState(false);
  const me = who || "console";

  const doIt = (what: string, body: any) => act.run(async () => {
    await dt.post(`${base}/${what}`, body);
    v.reload();
    onChange();
  });

  if (v.error) return <ErrorBox error={v.error} />;
  if (!v.data) return <div className="card empty">Loading…</div>;
  const d = v.data;
  const ev = d.record?.evaluation;
  const status = d.status;

  return (
    <div className="card">
      <div className="card-head">
        <div>
          <h2 style={{ marginBottom: 2 }}>{client}/{usecase} <span className="mono">{version}</span></h2>
          <div className="row"><Status value={status} /><Status value={d.provenance?.origin ?? "manual"} />
            {d.intact ? <Badge tone="ok">folder intact</Badge> : <Badge tone="bad">folder changed after publish</Badge>}
            {d.provenance?.run_id ? <a className="small" href={`#/runs/${d.provenance.run_id}`}>design run →</a> : null}</div>
        </div>
      </div>
      <Tabs tabs={["Review", "Files", "Diff", "Manifest"]} value={tab} onChange={setTab} />
      {tab === "Review" ? (
        <div className="grid g2">
          <div>
            <h3>Provenance</h3>
            <Kv items={[
              ["Origin", d.provenance?.origin ?? "manual"], ["Built on", d.provenance?.base_version ?? "—"],
              ["Created by", d.provenance?.created_by ?? "—"], ["Created", d.provenance?.created_at ? ago(d.provenance.created_at) : "—"],
              ["SHA-256", <span className="mono small">{d.sha256.slice(0, 16)}…</span>],
              ["Types", Object.keys(d.manifest.types ?? {}).join(", ") || "single type"],
              ["Skills", [...(d.manifest.skills ?? []), ...Object.values(d.manifest.types ?? {}).flatMap((t: any) => t.skills ?? [])].join(", ") || "—"],
            ]} />
            <h3>Evaluation</h3>
            {ev ? <Evaluation ev={ev} /> : <div className="muted">No evaluation recorded (hand-written or not yet recorded).</div>}
          </div>
          <div className="form">
            <h3>Sign-offs</h3>
            {(d.record?.signoffs ?? []).length ? d.record.signoffs.map((s: any) => (
              <div key={s.identity} className="small"><Badge tone="ok">✓</Badge> {s.identity} <span className="muted">{ago(s.at)}</span>{s.note ? ` — ${s.note}` : ""}</div>
            )) : <div className="muted small">None yet. Releasing needs one, from someone other than {d.record?.created_by ?? d.provenance?.created_by ?? "the creator"}.</div>}
            <label className="field">Note<input value={note} onChange={(e) => setNote(e.target.value)} placeholder="UAT passed on …" /></label>
            {d.record?.gates_passed === false ? (
              <label className="check"><input type="checkbox" checked={override} onChange={(e) => setOverride(e.target.checked)} />
                Release although an evaluation gate failed (needs a note)</label>
            ) : null}
            <ErrorBox error={act.error} />
            <div className="row">
              <button disabled={act.busy} onClick={() => doIt("signoff", { identity: me, note: note || undefined })}>Sign off</button>
              <button className="primary" disabled={act.busy || status === "active" || status === "rejected"}
                onClick={() => doIt("release", { by: me, note: note || undefined, accept_gate_failure: override })}>Release</button>
              <button className="danger" disabled={act.busy || status === "active" || status === "rejected"}
                onClick={() => doIt("reject", { by: me, note: note || undefined })}>Reject</button>
            </div>
            <span className="muted small">as {me}</span>
          </div>
        </div>
      ) : null}
      {tab === "Files" ? (
        <div className="grid" style={{ gridTemplateColumns: "240px 1fr" }}>
          <div className="steps">
            {d.files.map((f: any) => (
              <button key={f.path} className={f.path === file ? "primary" : ""} style={{ textAlign: "left" }} onClick={() => setFile(f.path)}>
                <span className="mono small">{f.path}</span></button>
            ))}
          </div>
          <div>{content.error ? <ErrorBox error={content.error} /> : <pre className="block">{content.data}</pre>}</div>
        </div>
      ) : null}
      {tab === "Diff" ? (diff.error ? <ErrorBox error={diff.error} /> : <><div className="muted small" style={{ marginBottom: 6 }}>Against the served version.</div><Diff text={diff.data ?? ""} /></>) : null}
      {tab === "Manifest" ? <Json value={d.manifest} /> : null}
    </div>
  );
}

function Evaluation({ ev }: { ev: any }) {
  if (ev.kind === "judge") {
    return (
      <Kv items={[
        ["Judge", ev.passed ? <Badge tone="ok">sample passes</Badge> : <Badge tone="warn">still failing</Badge>],
        ["Score", <span className="mono small">{JSON.stringify(ev.score)}</span>],
        ["Failing fields", (ev.failing_fields ?? []).join(", ") || "none"],
        ["Regression set", `${ev.regression_samples ?? 0} earlier sample(s) re-run`],
      ]} />
    );
  }
  const m = ev.metrics ?? {};
  const g = ev.gate_results ?? {};
  return (
    <table>
      <thead><tr><th>Metric</th><th>Value</th><th>Gate</th></tr></thead>
      <tbody>
        {Object.entries(m).map(([k, val]: any) => (
          <tr key={k}><td>{k.replace(/_/g, " ")}</td><td className="num">{val === null ? "—" : k.includes("gap") ? val : pct(val)}</td>
            <td>{g[k] === undefined ? "—" : g[k] ? <Badge tone="ok">pass</Badge> : <Badge tone="bad">fail</Badge>}</td></tr>
        ))}
        {ev.scope ? <tr><td>out-of-scope turned away</td><td className="num">{ev.scope.turned_away}/{ev.scope.out_of_scope_samples}</td><td /></tr> : null}
      </tbody>
    </table>
  );
}
