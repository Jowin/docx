import { useEffect, useState } from "react";
import { dt, rt } from "../api";
import { ago, ErrorBox, Flags, Status, useAction, useLoad } from "../components";

export default function Review({ jobId, who }: { jobId?: string; who: string }) {
  const [status, setStatus] = useState("open");
  const list = useLoad(() => rt.get<any[]>(`/review?status=${status}&limit=200`), [status], 8000);
  return (
    <>
      <h1>Review queue</h1>
      <p className="lede">Flagged results wait here. A correction is kept with the result and its audit trail, and is
        exported to design-time, where pattern learning turns it into a candidate config version.</p>
      <div className="split">
        <div className="card tight">
          <div className="row" style={{ padding: 10 }}>
            <select value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="open">open</option><option value="corrected">corrected</option>
              <option value="rejected">rejected</option><option value="">all</option>
            </select>
            <span className="spacer" /><ImportCorrections />
          </div>
          <ErrorBox error={list.error} />
          <table>
            <tbody>
              {(list.data ?? []).map((r) => (
                <tr key={r.job_id} className={`click ${r.job_id === jobId ? "sel" : ""}`}
                  onClick={() => (window.location.hash = `/review/${r.job_id}`)}>
                  <td>
                    <div className="mono">{r.job_id.slice(0, 12)}</div>
                    <div className="muted small">{r.client}/{r.usecase} · {ago(r.opened_at)}</div>
                    <div style={{ marginTop: 4 }}><Flags flags={r.flags} /></div>
                  </td>
                  <td style={{ textAlign: "right" }}><Status value={r.status} /></td>
                </tr>
              ))}
              {list.data?.length === 0 ? <tr><td className="empty">The queue is empty.</td></tr> : null}
            </tbody>
          </table>
        </div>
        <div>{jobId ? <Entry jobId={jobId} who={who} onDone={list.reload} /> : <div className="card empty">Pick an entry.</div>}</div>
      </div>
    </>
  );
}

function ImportCorrections() {
  const act = useAction();
  const [done, setDone] = useState<string>("");
  return (
    <span className="row">
      <button disabled={act.busy} title="Learn from every corrected result the runtime exports"
        onClick={() => act.run(async () => {
          const r = await dt.post("/learning/corrections", {});
          setDone(`${r.results.length} correction(s) processed`);
        })}>{act.busy ? "Learning…" : "Learn from corrections"}</button>
      {done ? <span className="small muted">{done}</span> : null}
      {act.error ? <span className="small" style={{ color: "var(--bad)" }}>{act.error.message}</span> : null}
    </span>
  );
}

function Entry({ jobId, who, onDone }: { jobId: string; who: string; onDone: () => void }) {
  const entry = useLoad(() => rt.get(`/review/${jobId}`), [jobId]);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [note, setNote] = useState("");
  const act = useAction();
  useEffect(() => setEdits({}), [jobId]);

  if (entry.error) return <ErrorBox error={entry.error} />;
  if (!entry.data) return <div className="card empty">Loading…</div>;
  const e = entry.data;
  const records: any[] = e.result?.records ?? [];
  const open = e.status === "open";

  const resolve = (action: "correct" | "reject") => act.run(async () => {
    const corrections = Object.entries(edits).map(([key, raw]) => {
      const [rec, field] = key.split("|");
      let value: unknown = raw;
      if (raw.trim() !== "" && !isNaN(Number(raw))) value = Number(raw);
      if (raw.trim() === "") value = null;
      return { record: Number(rec), field, value };
    });
    await rt.post(`/review/${jobId}/resolve`, { reviewer: who || "console", action, corrections, note: note || undefined });
    entry.reload();
    onDone();
  });

  return (
    <div className="card">
      <div className="card-head">
        <div>
          <h2 className="mono" style={{ marginBottom: 2 }}>{jobId}</h2>
          <div className="row"><Status value={e.status} /><Flags flags={e.flags} />
            <a href={`#/extractions/${jobId}`} className="small">open extraction →</a></div>
        </div>
      </div>
      {records.length === 0 ? <div className="empty">No records were extracted{e.result?.error ? `: ${e.result.error.message}` : ""}.</div> : null}
      {records.map((r, i) => (
        <div key={i} style={{ marginBottom: 14 }}>
          <div className="row" style={{ marginBottom: 6 }}><strong>Record {i + 1}</strong><Flags flags={r.flags} /></div>
          <table>
            <thead><tr><th>Field</th><th>Extracted</th><th>Confidence</th><th>Corrected value</th></tr></thead>
            <tbody>
              {Object.entries(r.fields ?? {}).filter(([, f]: any) => !f.items).map(([name, f]: any) => {
                const key = `${i}|${name}`;
                return (
                  <tr key={name}>
                    <td>{name}</td>
                    <td className="mono">{f.value === null ? <span className="muted">—</span> : String(f.value)}</td>
                    <td className="num">{f.confidence}</td>
                    <td>
                      <input disabled={!open} value={edits[key] ?? ""} placeholder="keep"
                        onChange={(ev) => setEdits({ ...edits, [key]: ev.target.value })} style={{ width: "100%" }} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ))}
      {open ? (
        <div className="form">
          <label className="field">Note<input value={note} onChange={(ev) => setNote(ev.target.value)} /></label>
          <ErrorBox error={act.error} />
          <div className="row">
            <button className="primary" disabled={act.busy} onClick={() => resolve("correct")}>
              {Object.keys(edits).length ? `Save ${Object.keys(edits).length} correction(s)` : "Accept as extracted"}</button>
            <button className="danger" disabled={act.busy} onClick={() => resolve("reject")}>Reject</button>
            <span className="muted small">as {who || "console"}</span>
          </div>
        </div>
      ) : <div className="note">Resolved by {e.reviewer ?? "—"}{e.note ? `: ${e.note}` : ""}.</div>}
    </div>
  );
}
