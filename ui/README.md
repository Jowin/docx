# DataExtractor console

A Vite + React console over both services, showing both flows:

| Page | Service | What you see and do |
| --- | --- | --- |
| Overview | both | health, recent extractions and design runs, every use case with what is served and what waits |
| Flows | both | the runtime, authoring and pattern-learning LangGraph graphs, drawn live, plus the release lifecycle |
| Extractions | runtime | submit a file, follow the job, its records and sources, its classification scores, and the graph path it took |
| Review queue | runtime | correct or reject flagged results; send corrections to pattern learning |
| Config versions | design-time | every version with status, origin and evaluation; sign off, release, reject, roll back; browse files and diffs |
| Design runs | design-time | authoring and pattern-learning runs side by side (stages, attempts, skills, graph path); start either |
| Ingestion lookups | design-time | edit the use-case, client and global `ingestion.json` |

The browser talks to one origin; the Vite server proxies `/api/runtime/*` to
`RUNTIME_URL` and `/api/designtime/*` to `DESIGNTIME_URL`.

```bash
cd ui
npm install
RUNTIME_URL=http://localhost:8001 DESIGNTIME_URL=http://localhost:8000 npm run dev   # http://localhost:5173
npm run build && npm run preview                                                      # http://localhost:4173
```

The defaults match `designtime/docker-compose.yml`, which also runs the console
as the `ui` service on port 4173. "Acting as" (top right) is the name recorded
on sign-offs, releases and review decisions; it is kept in the browser only.
There is no authentication: run it where only the team can reach it.
