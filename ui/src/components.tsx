import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from "react";
import { ApiError } from "./api";

// ---------------------------------------------------------------- data loading

export function useLoad<T>(fn: () => Promise<T>, deps: unknown[] = [], pollMs?: number) {
  const [data, setData] = useState<T | undefined>();
  const [error, setError] = useState<Error | undefined>();
  const [loading, setLoading] = useState(true);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const reload = useCallback(async () => {
    setLoading(true);
    try {
      setData(await fnRef.current());
      setError(undefined);
    } catch (e) {
      setError(e as Error);
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    reload();
    if (!pollMs) return;
    const t = setInterval(reload, pollMs);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return { data, error, loading, reload };
}

export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Error | undefined>();
  const run = useCallback(async <T,>(fn: () => Promise<T>): Promise<T | undefined> => {
    setBusy(true);
    setError(undefined);
    try {
      return await fn();
    } catch (e) {
      setError(e as Error);
      return undefined;
    } finally {
      setBusy(false);
    }
  }, []);
  return { busy, error, run, clear: () => setError(undefined) };
}

// ---------------------------------------------------------------- the person acting

const IDENTITY_KEY = "dx.identity";

export function getIdentity(): string {
  try {
    return localStorage.getItem(IDENTITY_KEY) || "";
  } catch {
    return "";
  }
}

export function setIdentity(v: string) {
  try {
    localStorage.setItem(IDENTITY_KEY, v);
  } catch {
    /* private window: keep it in memory only */
  }
}

// ---------------------------------------------------------------- display bits

export function ErrorBox({ error }: { error?: Error }) {
  if (!error) return null;
  const e = error as ApiError;
  return (
    <div className="err">
      <strong>{e.code || "error"}</strong> {e.message}
      {e.detail ? <pre className="block small" style={{ marginTop: 6, maxHeight: 160 }}>{JSON.stringify(e.detail, null, 2)}</pre> : null}
    </div>
  );
}

type Tone = "ok" | "warn" | "bad" | "info" | "accent" | "";

export function Badge({ tone = "", children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return <span className={`badge ${tone}`} title={title}>{children}</span>;
}

const STATUS_TONE: Record<string, Tone> = {
  active: "ok", candidate: "accent", retired: "", rejected: "bad", unmanaged: "warn",
  extracted: "ok", inprogress: "accent", flagged: "warn",
  learned: "ok", passed: "ok", published: "ok", improved: "warn", failed: "bad", error: "bad",
  gate_failed: "warn", evaluated: "accent", eval_failed: "bad", running: "accent", interrupted: "warn",
  matched: "ok", ambiguous: "warn", out_of_scope: "bad", unclassified: "",
  open: "warn", corrected: "ok",
  authoring: "info", pattern: "accent", learning: "accent", manual: "",
};

export function Status({ value }: { value?: string | null }) {
  if (!value) return <span className="muted">—</span>;
  return <Badge tone={STATUS_TONE[value] ?? ""}>{value.replace(/_/g, " ")}</Badge>;
}

export function Flags({ flags, max }: { flags?: string[]; max?: number }) {
  if (!flags || flags.length === 0) return <Badge tone="ok">clean</Badge>;
  const shown = max ? flags.slice(0, max) : flags;
  return (
    <div className="chips" title={flags.join("\n")}>
      {max && flags.length > max ? <Badge>+{flags.length - max}</Badge> : null}
      {shown.map((f) => (
        <Badge key={f} tone={f.startsWith("error") || f.startsWith("out_of_scope") ? "bad" : "warn"}>{f}</Badge>
      ))}
    </div>
  );
}

export function Json({ value, max = 460 }: { value: unknown; max?: number }) {
  return <pre className="block" style={{ maxHeight: max }}>{JSON.stringify(value, null, 2)}</pre>;
}

export function Kv({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="kv">
      {items.map(([k, v]) => (
        <FragmentKv key={k} k={k} v={v} />
      ))}
    </dl>
  );
}

function FragmentKv({ k, v }: { k: string; v: ReactNode }) {
  return (
    <>
      <dt>{k}</dt>
      <dd>{v ?? <span className="muted">—</span>}</dd>
    </>
  );
}

export function Stat({ label, value, sub }: { label: string; value: ReactNode; sub?: ReactNode }) {
  return (
    <div className="card stat">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {sub ? <div className="sub">{sub}</div> : null}
    </div>
  );
}

export function Tabs({ tabs, value, onChange }: { tabs: string[]; value: string; onChange: (t: string) => void }) {
  return (
    <div className="tabs">
      {tabs.map((t) => (
        <button key={t} className={t === value ? "on" : ""} onClick={() => onChange(t)}>{t}</button>
      ))}
    </div>
  );
}

export function ago(ts?: number | string | null): string {
  if (ts === undefined || ts === null || ts === "") return "—";
  const ms = typeof ts === "number" ? ts * (ts < 1e12 ? 1000 : 1) : Date.parse(ts);
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return new Date(ms).toLocaleString();
}

export function pct(v?: number | null): string {
  return v === undefined || v === null ? "—" : `${Math.round(v * 1000) / 10}%`;
}

export function Diff({ text }: { text: string }) {
  if (!text.trim()) return <div className="empty">No differences.</div>;
  return (
    <pre className="block diff">
      {text.split("\n").map((line, i) => {
        const cls = line.startsWith("+++") || line.startsWith("---") ? "hunk" : line.startsWith("@@") ? "hunk"
          : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "";
        return <div key={i} className={cls}>{line || " "}</div>;
      })}
    </pre>
  );
}

// ---------------------------------------------------------------- mermaid graphs

let mermaidReady: Promise<typeof import("mermaid")["default"]> | null = null;

function loadMermaid() {
  if (!mermaidReady) {
    mermaidReady = import("mermaid").then((m) => {
      const dark = window.matchMedia?.("(prefers-color-scheme: dark)").matches;
      m.default.initialize({ startOnLoad: false, theme: dark ? "dark" : "neutral", securityLevel: "strict",
        flowchart: { curve: "basis" } });
      return m.default;
    });
  }
  return mermaidReady;
}

/** Render a LangGraph mermaid drawing; ``visited`` nodes are highlighted (the path a run took). */
export function Mermaid({ source, visited = [], current }: { source?: string; visited?: string[]; current?: string }) {
  const id = useId().replace(/[^a-zA-Z0-9]/g, "");
  const [svg, setSvg] = useState<string>("");
  const [err, setErr] = useState<string>("");
  useEffect(() => {
    if (!source) return;
    let text = source;
    const nodes = Array.from(new Set(visited.map((v) => v.split(":")[0])));
    if (nodes.length) {
      text += "\n\tclassDef visited fill:#2563eb,stroke:#1d4ed8,color:#ffffff;\n";
      text += nodes.map((n) => `\tclass ${n} visited;`).join("\n") + "\n";
    }
    if (current) text += `\tclassDef here fill:#f59e0b,stroke:#b45309,color:#111;\n\tclass ${current} here;\n`;
    loadMermaid()
      .then((m) => m.render(`g${id}${Math.random().toString(36).slice(2, 7)}`, text))
      .then((r) => { setSvg(r.svg); setErr(""); })
      .catch((e) => setErr(String(e?.message || e)));
  }, [source, visited.join("|"), current, id]);
  if (err) return <div className="err">graph: {err}</div>;
  if (!source) return <div className="empty">Loading graph…</div>;
  return <div className="mermaid-box" dangerouslySetInnerHTML={{ __html: svg }} />;
}
