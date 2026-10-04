import { dt, rt } from "../api";
import { ErrorBox, Mermaid, useLoad } from "../components";

export default function Flows() {
  const runtime = useLoad(() => rt.get<string>("/graph"), []);
  const authoring = useLoad(() => dt.get<string>("/graphs/authoring"), []);
  const learning = useLoad(() => dt.get<string>("/graphs/learning"), []);
  return (
    <>
      <h1>Flows</h1>
      <p className="lede">The three LangGraph graphs, drawn from the running services. Design-time's two graphs end in
        the same place: a candidate version folder in the config root, scored by the runtime graph on the left, waiting
        for a sign-off and a release.</p>
      <div className="card">
        <h2>How a version reaches production</h2>
        <Mermaid source={LIFECYCLE} />
      </div>
      <div className="grid g3">
        <div className="card">
          <h2>Runtime: one extraction</h2>
          <p className="muted small">Ingest (with the ingestion filter), parse each item in parallel, classify with the
            detection rules, extract, verify, route flags.</p>
          <ErrorBox error={runtime.error} />
          <Mermaid source={runtime.data} />
        </div>
        <div className="card">
          <h2>Design-time: authoring</h2>
          <p className="muted small">A labelled corpus becomes a new minor version: schemas, rules, skills and
            thresholds, evaluated twice in the runtime.</p>
          <ErrorBox error={authoring.error} />
          <Mermaid source={authoring.data} />
        </div>
        <div className="card">
          <h2>Design-time: pattern learning</h2>
          <p className="muted small">One sample becomes a new patch version: a skill is written and retried until the
            sample passes without breaking earlier ones.</p>
          <ErrorBox error={learning.error} />
          <Mermaid source={learning.data} />
        </div>
      </div>
    </>
  );
}

const LIFECYCLE = `flowchart LR
  corpus[(Labelled corpus)] --> authoring[Authoring run]
  sample[(One sample + ground truth)] --> learning[Pattern learning]
  review[Reviewer corrections] --> learning
  authoring -- next minor --> folder
  learning -- next patch --> folder
  folder[/Config version folder\\ncandidate/] --> eval{Scored by the runtime}
  eval --> signoff[Sign-off\\nnot by its creator]
  signoff --> release[Release\\nreleases.json]
  release --> served((Runtime serves it))
  served -- flagged results --> review
  release -. rollback .-> served
  folder -. reject .-> rejected[Rejected: never latest]
`;
