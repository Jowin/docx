import { useEffect, useState } from "react";
import { getIdentity, setIdentity } from "./components";
import Overview from "./pages/Overview";
import Extractions from "./pages/Extractions";
import Review from "./pages/Review";
import Configs from "./pages/Configs";
import Lookups from "./pages/Lookups";
import DesignRuns from "./pages/DesignRuns";
import Flows from "./pages/Flows";

type Route = { page: string; parts: string[] };

function parse(): Route {
  const h = window.location.hash.replace(/^#\/?/, "");
  const [page = "overview", ...parts] = h.split("/").map(decodeURIComponent);
  return { page: page || "overview", parts };
}

export function go(...segments: string[]) {
  window.location.hash = "/" + segments.map(encodeURIComponent).join("/");
}

const NAV: { group: string; items: [string, string][] }[] = [
  { group: "", items: [["overview", "Overview"], ["flows", "Flows"]] },
  { group: "Runtime", items: [["extractions", "Extractions"], ["review", "Review queue"]] },
  { group: "Design-time", items: [["configs", "Config versions"], ["runs", "Design runs"], ["lookups", "Ingestion lookups"]] },
];

export default function App() {
  const [route, setRoute] = useState<Route>(parse());
  const [who, setWho] = useState(getIdentity());
  useEffect(() => {
    const on = () => setRoute(parse());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);

  let page;
  switch (route.page) {
    case "extractions": page = <Extractions jobId={route.parts[0]} />; break;
    case "review": page = <Review jobId={route.parts[0]} who={who} />; break;
    case "configs": page = <Configs client={route.parts[0]} usecase={route.parts[1]} version={route.parts[2]} who={who} />; break;
    case "lookups": page = <Lookups who={who} />; break;
    case "runs": page = <DesignRuns runId={route.parts[0]} who={who} />; break;
    case "flows": page = <Flows />; break;
    default: page = <Overview />;
  }

  return (
    <div className="shell">
      <aside className="side">
        <div className="brand"><span className="logo">DX</span> DataExtractor</div>
        <nav className="nav">
          {NAV.map((g) => (
            <div key={g.group}>
              {g.group ? <div className="nav-group">{g.group}</div> : null}
              {g.items.map(([key, label]) => (
                <a key={key} href={`#/${key}`} className={route.page === key ? "active" : ""}>{label}</a>
              ))}
            </div>
          ))}
        </nav>
        <p className="muted small" style={{ margin: "24px 8px 0" }}>
          Config folders are the source of truth. Authoring and pattern learning publish candidates; a person
          signs off and releases.
        </p>
      </aside>
      <main className="main">
        <div className="topbar">
          <label className="row small muted">
            Acting as
            <input
              value={who}
              placeholder="you@company.example"
              onChange={(e) => { setWho(e.target.value); setIdentity(e.target.value); }}
              style={{ width: 220 }}
            />
          </label>
        </div>
        <div className="content">{page}</div>
      </main>
    </div>
  );
}
