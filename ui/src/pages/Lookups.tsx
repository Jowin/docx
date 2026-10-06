import { useEffect, useState } from "react";
import { dt, qs } from "../api";
import { Badge, ErrorBox, useAction, useLoad } from "../components";

const KEYS = [
  ["ignore_names", "Ignore names", "docusign\n*.ics\nre:^certificate of completion"],
  ["keep_names", "Keep names (beats wider levels)", "docusign invoice"],
  ["ignore_hashes", "Ignore hashes", "<sha256>  or  md5:<md5>"],
  ["keep_hashes", "Keep hashes", ""],
  ["ignore_kinds", "Ignore kinds", "image"],
] as const;

export default function Lookups({ who }: { who: string }) {
  const configs = useLoad(() => dt.get("/configs"), []);
  const [target, setTarget] = useState("");
  const [client, usecase] = target ? target.split("/") : [undefined, undefined];
  const view = useLoad(() => dt.get(`/lookups${qs({ client, usecase })}`), [target]);
  const clients = Array.from(new Set<string>((configs.data?.usecases ?? []).map((u: any) => u.client)));

  return (
    <>
      <h1>Lookups</h1>
      <p className="lede">Before anything is parsed, every attachment is checked against these lists: the use case's,
        then the client's, then the global ones. The most specific level with a verdict wins, so a use case can keep
        what the global list ignores. Lookups live beside the version folders, so a change applies at once without a
        new config version. Each level can also hold ZEN transform rules (<span className="mono">transform.decision.json</span>
        and its <span className="mono">tables/</span>), run over every extracted record after the version's own rules,
        global first and use case last.</p>
      <div className="card row">
        <label className="field" style={{ minWidth: 320 }}>Scope
          <select value={target} onChange={(e) => setTarget(e.target.value)}>
            <option value="">global only</option>
            {clients.map((c) => <option key={c} value={c}>{c} (client)</option>)}
            {(configs.data?.usecases ?? []).map((u: any) => (
              <option key={`${u.client}/${u.usecase}`} value={`${u.client}/${u.usecase}`}>{u.client}/{u.usecase} (use case)</option>
            ))}
          </select>
        </label>
      </div>
      <ErrorBox error={view.error ?? configs.error} />
      <div className="grid g3">
        {(view.data?.levels ?? []).map((lv: any) => (
          <Level key={`${target}:${lv.level}`} level={lv} client={lv.level === "global" ? undefined : client}
            usecase={lv.level === "usecase" ? usecase : undefined} who={who} onSaved={view.reload} />
        ))}
      </div>
    </>
  );
}

function Level({ level, client, usecase, who, onSaved }: { level: any; client?: string; usecase?: string; who: string; onSaved: () => void }) {
  const [form, setForm] = useState<Record<string, string>>({});
  const act = useAction();
  useEffect(() => {
    const data = level.ingestion ?? {};
    setForm(Object.fromEntries(KEYS.map(([k]) => [k, (data[k] ?? []).join("\n")])));
  }, [level]);
  const save = () => act.run(async () => {
    const body: any = { client, usecase, by: who || "console" };
    for (const [k] of KEYS) body[k] = (form[k] ?? "").split("\n").map((s) => s.trim()).filter(Boolean);
    await dt.put("/lookups", body);
    onSaved();
  });
  const title = level.level === "global" ? "Global" : level.level === "client" ? `Client ${client}` : `Use case ${client}/${usecase}`;
  return (
    <div className="card form">
      <div className="card-head"><h2 style={{ margin: 0 }}>{title}</h2>
        {level.ingestion ? <Badge tone="ok">set</Badge> : <Badge>empty</Badge>}
        {level.decision ? <Badge tone="info" title="ingestion.decision.json: a ZEN decision table">decision</Badge> : null}
        {level.transform?.entry ? <Badge tone="info" title="transform.decision.json: ZEN rules run over every record">transform</Badge> : null}</div>
      <div className="muted small mono">{level.path}</div>
      {level.transform?.entry ? (
        <div className="note small">Transform rules: <span className="mono">transform.decision.json</span>
          {level.transform.tables.length ? <> calling {level.transform.tables.map((t: string) => <span key={t} className="mono"> {t}</span>)}</> : null}.
          Edit them in the ZEN editor and commit the files; the runtime checks them when it loads the config.</div>
      ) : null}
      {KEYS.map(([k, label, ph]) => (
        <label className="field" key={k}>{label}
          <textarea rows={k.includes("names") ? 4 : 2} value={form[k] ?? ""} placeholder={ph}
            onChange={(e) => setForm({ ...form, [k]: e.target.value })} />
        </label>
      ))}
      <ErrorBox error={act.error} />
      <div className="row"><button className="primary" disabled={act.busy} onClick={save}>Save {level.level}</button></div>
    </div>
  );
}
