import { dt, rt } from "../api";
import { ago, Badge, ErrorBox, Flags, pct, Stat, Status, useLoad } from "../components";

export default function Overview() {
  const rh = useLoad(() => rt.get("/health"), [], 15000);
  const dh = useLoad(() => dt.get("/health"), [], 15000);
  const metrics = useLoad(() => rt.get("/metrics"), [], 15000);
  const jobs = useLoad(() => rt.get<any[]>("/extractions?limit=8"), [], 10000);
  const configs = useLoad(() => dt.get("/configs"), [], 30000);
  const runs = useLoad(() => dt.get<any[]>("/learning/runs?limit=8"), [], 15000);

  const groups: any[] = metrics.data?.groups ?? [];
  const total = groups.reduce((n, g) => n + g.jobs, 0);
  const flagged = groups.reduce((n, g) => n + g.flagged, 0);
  const extracted = groups.reduce((n, g) => n + g.extracted, 0);
  const candidates = (configs.data?.usecases ?? []).reduce(
    (n: number, u: any) => n + u.versions.filter((v: any) => v.status === "candidate").length, 0);

  return (
    <>
      <h1>Overview</h1>
      <p className="lede">
        Both flows at a glance: the <strong>runtime</strong> extracts with the released config of each use case;
        <strong> design-time</strong> authors and learns new config versions, which wait as candidates until someone
        releases them.
      </p>
      <div className="grid g4">
        <Stat label="Runtime" value={<Health h={rh.data} err={rh.error} />}
          sub={rh.data ? `engine ${rh.data.engine_version} · workers ${rh.data.workers}` : null} />
        <Stat label="Design-time" value={<Health h={dh.data} err={dh.error} />}
          sub={dh.data ? `version ${dh.data.version}` : null} />
        <Stat label="Extractions" value={total} sub={`${extracted} extracted · ${pct(extracted ? flagged / extracted : 0)} flagged`} />
        <Stat label="Candidates awaiting release" value={candidates}
          sub={<a href="#/configs">review config versions →</a>} />
      </div>
      {rh.data?.config_problems?.length ? (
        <div className="err">Config problems: {rh.data.config_problems.map((p: any) => `${p.config}: ${p.message}`).join("; ")}</div>
      ) : null}

      <div className="grid g2">
        <div className="card tight">
          <div className="card-head" style={{ padding: "12px 14px 0" }}>
            <h2>Recent extractions</h2><a href="#/extractions">all →</a>
          </div>
          <ErrorBox error={jobs.error} />
          <table>
            <thead><tr><th>Job</th><th>Use case</th><th>Status</th><th>Flags</th><th>When</th></tr></thead>
            <tbody>
              {(jobs.data ?? []).map((j) => (
                <tr key={j.job_id} className="click" onClick={() => (window.location.hash = `/extractions/${j.job_id}`)}>
                  <td className="mono">{j.job_id.slice(0, 8)}</td>
                  <td>{j.client}/{j.usecase}</td>
                  <td><Status value={j.status} /></td>
                  <td>{j.status === "extracted" ? <Flags flags={j.flags} max={2} /> : null}</td>
                  <td className="muted">{ago(j.created_at)}</td>
                </tr>
              ))}
              {jobs.data?.length === 0 ? <tr><td colSpan={5} className="empty">No extractions yet.</td></tr> : null}
            </tbody>
          </table>
        </div>
        <div className="card tight">
          <div className="card-head" style={{ padding: "12px 14px 0" }}>
            <h2>Recent design runs</h2><a href="#/runs">all →</a>
          </div>
          <ErrorBox error={runs.error} />
          <table>
            <thead><tr><th>Kind</th><th>Use case</th><th>Outcome</th><th>Version</th><th>When</th></tr></thead>
            <tbody>
              {(runs.data ?? []).map((r) => (
                <tr key={r.id} className="click" onClick={() => (window.location.hash = `/runs/${r.id}`)}>
                  <td><Status value={r.kind} /></td>
                  <td>{r.client}/{r.usecase}{r.kind === "pattern" ? <span className="muted"> · {r.pattern_name}</span> : null}</td>
                  <td><Status value={r.outcome} /></td>
                  <td className="mono">{r.base_version ?? "—"} → {r.result_version ?? "—"}</td>
                  <td className="muted">{ago(r.created_at)}</td>
                </tr>
              ))}
              {runs.data?.length === 0 ? <tr><td colSpan={5} className="empty">No design runs yet.</td></tr> : null}
            </tbody>
          </table>
        </div>
      </div>

      <div className="card tight">
        <div className="card-head" style={{ padding: "12px 14px 0" }}>
          <h2>Use cases</h2><a href="#/configs">config versions →</a>
        </div>
        <ErrorBox error={configs.error} />
        <table>
          <thead><tr><th>Use case</th><th>Served</th><th>Candidates</th><th>Jobs</th><th>Flag rate</th><th>p95 latency</th><th>Top flags</th></tr></thead>
          <tbody>
            {(configs.data?.usecases ?? []).map((u: any) => {
              const g = groups.find((x) => x.client === u.client && x.usecase === u.usecase);
              const cands = u.versions.filter((v: any) => v.status === "candidate");
              return (
                <tr key={`${u.client}/${u.usecase}`} className="click"
                  onClick={() => (window.location.hash = `/configs/${u.client}/${u.usecase}`)}>
                  <td><strong>{u.client}</strong>/{u.usecase}</td>
                  <td className="mono">{u.served ?? "—"} {u.managed ? null : <Badge tone="warn" title="no releases.json: the newest version is served">unmanaged</Badge>}</td>
                  <td>{cands.length ? cands.map((v: any) => <Badge key={v.version} tone="accent">{v.version}</Badge>) : <span className="muted">none</span>}</td>
                  <td className="num">{g?.jobs ?? 0}</td>
                  <td className="num">{g ? pct(g.flag_rate) : "—"}</td>
                  <td className="num">{g?.latency_ms?.p95 ? `${Math.round(g.latency_ms.p95)} ms` : "—"}</td>
                  <td><div className="chips">{Object.entries(g?.flags ?? {}).slice(0, 3).map(([k, n]) => <Badge key={k} tone="warn">{k} · {String(n)}</Badge>)}</div></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}

function Health({ h, err }: { h?: any; err?: Error }) {
  if (err) return <Badge tone="bad">unreachable</Badge>;
  if (!h) return <span className="muted">…</span>;
  return <Badge tone={h.status === "ok" ? "ok" : "warn"}>{h.status}</Badge>;
}
