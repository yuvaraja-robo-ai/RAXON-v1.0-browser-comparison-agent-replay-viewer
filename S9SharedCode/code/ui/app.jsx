/* RAXON Browser Comparison Terminal — no-build React (Babel CDN).
   Viewer + launch: list sessions, render the 8 §18 panels (goal, planner DAG,
   browser path, action log, screenshots, extracted data, comparison table,
   turns+cost), and launch DAG runs / deterministic captures / login handoff. */
const { useState, useEffect, useRef, useCallback } = React;
const api = (p, opts) => fetch(p, opts).then(r => r.json());
const fmt = n => (n == null ? "—" : n >= 1e6 ? (n/1e6).toFixed(2)+"M"
                : n >= 1e3 ? (n/1e3).toFixed(1)+"k" : ""+n);
const fmtBytes = b => (b == null ? "—" : b >= 1e6 ? (b/1e6).toFixed(1)+" MB"
                     : b >= 1e3 ? (b/1e3).toFixed(0)+" kB" : b+" B");
const fmtAge = ts => {
  if (!ts) return "";
  const s = Math.max(0, Date.now()/1000 - ts);
  return s < 60 ? "just now" : s < 3600 ? Math.floor(s/60)+"m ago"
       : s < 86400 ? Math.floor(s/3600)+"h ago" : Math.floor(s/86400)+"d ago";
};

const STATUS_COLOR = { pending:"#5a6678", running:"#ffb020", complete:"#3ddc97",
                       failed:"#ff5d57", skipped:"#3a4254" };

/* ───────────────────────── planner DAG (Cytoscape) ───────────────────────── */
function DagView({ view }) {
  const elRef = useRef(null), cyRef = useRef(null);
  useEffect(() => {
    if (!elRef.current) return;
    if (!cyRef.current) {
      try { cytoscape.use(cytoscapeDagre); } catch (e) {}
      cyRef.current = cytoscape({ container: elRef.current,
        style: [
          { selector:"node", style:{ "background-color":"data(color)",
            label:"data(label)", color:"#e7ecf5", "font-size":10,
            "text-valign":"center", "text-halign":"center", width:90, height:34,
            shape:"round-rectangle", "text-wrap":"wrap", "text-max-width":84 } },
          { selector:"edge", style:{ width:2, "line-color":"#3a4564",
            "target-arrow-color":"#3a4564", "target-arrow-shape":"triangle",
            "curve-style":"bezier" } },
        ], minZoom:.3, maxZoom:2 });
    }
    const cy = cyRef.current;
    const els = [
      ...(view.nodes||[]).map(n => ({ data:{ id:n.id,
        label:`${n.skill}\n${n.id}`, color: STATUS_COLOR[n.status]||"#5a6678" } })),
      ...(view.edges||[]).map(e => ({ data:{ id:e.source+"_"+e.target,
        source:e.source, target:e.target } })),
    ];
    cy.json({ elements: els });
    cy.layout({ name:"dagre", rankDir:"LR", nodeSep:24, rankSep:48 }).run();
  }, [view]);
  return <div id="dag" ref={elRef} />;
}

/* ───────────────────────── comparison table ───────────────────────── */
function CompareTable({ table }) {
  if (!table) return <div className="muted">No comparison table yet.</div>;
  if (table.markdown) return <pre className="mono" style={{whiteSpace:"pre-wrap"}}>{table.markdown}</pre>;
  const recs = table.records || [];
  if (!recs.length) return <div className="muted">No records.</div>;
  const cols = Object.keys(recs[0]);
  return (
    <table><thead><tr>{cols.map(c => <th key={c}>{c.replace(/_/g," ")}</th>)}</tr></thead>
      <tbody>{recs.map((r,i) => <tr key={i}>{cols.map(c => {
        let v = r[c];
        if (c === "url" && v) return <td key={c}><a href={v} target="_blank">link</a></td>;
        if ((c === "likes" || c === "downloads") && typeof v === "number") v = fmt(v);
        return <td key={c} className="mono">{v == null ? "—" : ""+v}</td>;
      })}</tr>)}</tbody></table>
  );
}

/* ───────────────────────── live progress banner ───────────────────────── */
function Progress({ st }) {
  if (!st || !st.known || st.done) return null;
  const p = st.progress || {};
  const pct = p.total ? Math.min(100, Math.round(100 * (p.step || 0) / p.total)) : null;
  return (
    <div className="card progress-card">
      <div className="row">
        <span className="spinner" />
        <b>{p.message || "working…"}</b>
        <span className="muted" style={{marginLeft:"auto"}}>
          {p.total ? `${p.step ?? 0}/${p.total} · ` : ""}{st.elapsed != null ? `${st.elapsed}s` : ""}
        </span>
      </div>
      <div className="bar">{pct == null
        ? <div className="fill indet" />
        : <div className="fill" style={{width: pct + "%"}} />}</div>
      <div className="muted" style={{fontSize:11}}>
        {st.kind === "dag" ? "DAG run — nodes complete / total" : "deterministic capture — steps done / planned"}
      </div>
    </div>
  );
}

/* ───────────────────────── turns + cost summary (§18 ⑧) ───────────────────── */
function CostCard({ p }) {
  const c = p.cost || {};
  const rows = c.summary || [];
  return (
    <div className="card"><h3>⑧ Turns & cost summary</h3>
      <div className="row" style={{marginBottom:8}}>
        <span className="pill">path: {p.path || "—"}</span>
        <span className="pill">turns: {p.turns ?? 0}</span>
        {p.elapsed_s != null && <span className="pill">elapsed: {p.elapsed_s}s</span>}
        <span className="pill">total: {c.total_dollars != null ? `$${c.total_dollars.toFixed(4)}` : "—"}</span>
      </div>
      {!c.ok && <div className="muted">gateway offline — cost ledger unavailable (viewer still works).</div>}
      {c.ok && rows.length === 0 &&
        <div className="muted">$0.0000 — no LLM calls logged{p.kind === "capture" ? " (deterministic capture)" : ""}.</div>}
      {rows.length > 0 &&
        <table><thead><tr><th>agent</th><th>calls</th><th>in tok</th><th>out tok</th><th>$</th></tr></thead>
        <tbody>{rows.map(r => <tr key={r.agent}>
          <td>{r.agent}</td><td className="mono">{r.calls}</td>
          <td className="mono">{fmt(r.in_tok)}</td><td className="mono">{fmt(r.out_tok)}</td>
          <td className="mono">${(r.dollars ?? 0).toFixed(4)}</td>
        </tr>)}</tbody></table>}
    </div>
  );
}

/* ───────────────────────── panel (one session) ───────────────────────── */
function Panel({ sid, onZoom }) {
  const [p, setP] = useState(null);
  const [st, setSt] = useState(null);            // live run status (progress banner)
  const [tab, setTab] = useState("overview");
  useEffect(() => {
    if (!sid) return;
    setP(null); setSt(null); setTab("overview");
    let es;
    api(`/api/session/${sid}`).then(d => { if (!d.detail) setP(d); });
    api(`/api/session/${sid}/status`).then(setSt);
    es = new EventSource(`/api/session/${sid}/stream`);
    es.onmessage = ev => {
      const d = JSON.parse(ev.data);
      if (d.type === "panel") setP(d);
      if (d.type === "status") setSt(d);
      if (d.type === "done") {
        setSt(s => ({ ...(s||{}), known:true, done:true, error:d.error }));
        api(`/api/session/${sid}`).then(x => { if (!x.detail) setP(x); });
        es.close();
      }
    };
    es.onerror = () => es.close();
    return () => es && es.close();
  }, [sid]);
  if (!sid) return <div className="muted">Pick a session on the left, or launch one above.</div>;
  if (!p) return <div><Progress st={st} />
    <div className="muted">{st && st.known && !st.done ? `starting ${sid}…` : `Loading ${sid}…`}</div></div>;

  const shot = s => `/api/session/${sid}/artifact/${s.replace(/^browser\//,"")}`;
  const tabs = p.kind === "dag"
    ? ["overview","dag","actions","screens","data","table"]
    : ["overview","actions","screens","data","table"];
  const records = (p.table && p.table.records) || [];

  return (
    <div>
      <Progress st={st} />
      <div className="row" style={{justifyContent:"space-between"}}>
        <div><b>{sid}</b> <span className={`tag ${p.kind}`}>{p.kind}</span></div>
        <div className="row">
          <span className="pill">③ path: {p.path || "—"}</span>
          <span className="pill">turns: {p.turns ?? 0}</span>
          {p.authenticated && <span className="pill">🔒 auth</span>}
          <a className="btn ghost small" href={`/api/session/${sid}/export`} download>⬇ export zip</a>
        </div>
      </div>
      <div className="card" style={{marginTop:12}}>
        <h3>① User goal</h3><div>{p.goal || "—"}</div>
        {p.url && <div className="muted mono" style={{marginTop:6}}>{p.url}</div>}
        {p.error && <div style={{color:"var(--red)",marginTop:8}}>error: {p.error}</div>}
        {st && st.done && st.error && <div style={{color:"var(--red)",marginTop:8}}>run error: {st.error}</div>}
      </div>

      <div className="tabs">{tabs.map(t =>
        <div key={t} className={`tab ${tab===t?"active":""}`} onClick={()=>setTab(t)}>{t}</div>)}</div>

      {tab==="overview" && <>
        <div className="card"><h3>⑦ Comparison table</h3>
          <CompareTable table={p.table} /></div>
        <CostCard p={p} />
      </>}

      {tab==="dag" && <div className="card"><h3>② Planner DAG</h3>
        <DagView view={p.dag||{nodes:[],edges:[]}} />
        <div className="muted" style={{marginTop:8}}>
          {(p.dag&&p.dag.nodes||[]).map(n =>
            <span key={n.id} className="pill" style={{marginRight:6,marginTop:6,display:"inline-block",
              borderColor:STATUS_COLOR[n.status]}}>{n.id} {n.skill} · {n.status}
              {n.elapsed!=null?` · ${n.elapsed}s`:""}</span>)}
        </div></div>}

      {tab==="actions" && <div className="card"><h3>④ Action log ({(p.actions||[]).length})</h3>
        <table><thead><tr><th>#</th><th>layer</th><th>action</th><th>target</th><th>status</th><th>shot</th></tr></thead>
        <tbody>{(p.actions||[]).map((a,i)=><tr key={i}>
          <td className="mono">{a.turn}</td><td>{a.layer}</td>
          <td className="mono">{a.action}</td>
          <td className="mono" style={{maxWidth:340,overflow:"hidden",textOverflow:"ellipsis"}}>{a.target}</td>
          <td style={{color: a.status==="skipped" ? "var(--yel)" : "var(--grn)"}}>{a.status || "ok"}</td>
          <td>{a.screenshot ? <a onClick={()=>onZoom(shot(a.screenshot))}>view</a> : "—"}</td>
        </tr>)}</tbody></table></div>}

      {tab==="screens" && <div className="card"><h3>⑤ Screenshots</h3>
        {(p.screenshots||[]).length===0 && <div className="muted">none</div>}
        {(p.screenshots||[]).map((s,i)=>
          <img key={i} className="shot" src={shot(s)} onClick={()=>onZoom(shot(s))} />)}</div>}

      {tab==="data" && <div className="card"><h3>⑥ Extracted data (raw records)</h3>
        {records.length === 0 && !p.table?.markdown && <div className="muted">no structured records.</div>}
        {records.length > 0 &&
          <pre className="mono" style={{whiteSpace:"pre-wrap",fontSize:12}}>{JSON.stringify(records, null, 2)}</pre>}
        {p.table?.markdown &&
          <pre className="mono" style={{whiteSpace:"pre-wrap",fontSize:12}}>{p.table.markdown}</pre>}</div>}

      {tab==="table" && <div className="card"><h3>⑦ Final comparison table</h3>
        <CompareTable table={p.table} /></div>}
    </div>
  );
}

/* ───────────────────────── launch bar ───────────────────────── */
function Launch({ onStarted, health, flash }) {
  const [mode, setMode] = useState("capture");
  const [busy, setBusy] = useState(false);
  const cardRef = useRef(null);
  // "+ new session" remounts this form; make that visible — scroll it into
  // view, flash the card, and put the cursor in the first field.
  useEffect(() => {
    if (!flash || !cardRef.current) return;
    const el = cardRef.current;
    el.scrollIntoView({ behavior:"smooth", block:"start" });
    el.classList.add("flash");
    const inp = el.querySelector("input, textarea");
    if (inp) inp.focus();
    const t = setTimeout(() => el.classList.remove("flash"), 1300);
    return () => clearTimeout(t);
  }, [flash]);
  const [query, setQuery] = useState("Compare the top 3 most-liked Hugging Face text-generation models that use the transformers library. For each give name, parameter count, likes, and a one-line description.");
  const [url, setUrl] = useState("https://huggingface.co/models");
  const [goal, setGoal] = useState("Compare the top 3 most-liked Hugging Face text-generation models");
  // ≥3 visible browser actions (assignment §18): filter → open sort menu →
  // sort by likes → open the top model's detail page. Selectors verified live.
  // snapshot:false on the menu-open click — a full-page screenshot would
  // dismiss the dropdown before the next step can pick "Most likes".
  const HF_STEPS = JSON.stringify([
    {action:"click", selector:"a[href='/models?pipeline_tag=text-generation']"},
    {action:"click", selector:"button:has-text('Sort:')", snapshot:false},
    {action:"click", selector:"li:has-text('Most likes')"},
    {action:"click", selector:"article a[href^='/']"},
  ], null, 1);
  const [steps, setSteps] = useState(HF_STEPS);
  // the HF preset only makes sense on huggingface.co — swap it in/out as the
  // URL host changes so other sites get a plain (steps-free) generic capture
  useEffect(() => {
    const isHF = /huggingface\.co/.test(url);
    if (!isHF && steps === HF_STEPS) setSteps("");
    if (isHF && !steps.trim()) setSteps(HF_STEPS);
  }, [url]);
  const [auth, setAuth] = useState("");
  const [loginUrl, setLoginUrl] = useState("");
  const [loginName, setLoginName] = useState("");

  const go = async () => {
    let parsedSteps = null;
    if (mode === "capture" && steps.trim()) {
      try { parsedSteps = JSON.parse(steps); }
      catch (e) { alert("steps is not valid JSON: " + e); return; }
    }
    setBusy(true);
    try {
      let r;
      if (mode === "dag") r = await api("/api/run", { method:"POST",
        headers:{"Content-Type":"application/json"}, body:JSON.stringify({query}) });
      else if (mode === "capture") r = await api("/api/capture", { method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({url, goal, want:3, auth:auth||null, steps:parsedSteps}) });
      else { r = await api("/api/login", { method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({login_url:loginUrl, name:loginName}) });
        alert(r.ok ? `saved session '${r.name}'` : `login failed: ${r.detail||JSON.stringify(r)}`); }
      if (r && r.sid) onStarted(r.sid);
    } catch (e) { alert("failed: "+e); }
    setBusy(false);
  };

  return (
    <div className="card" ref={cardRef}>
      <div className="tabs">
        {["capture","dag","login"].map(m =>
          <div key={m} className={`tab ${mode===m?"active":""}`} onClick={()=>setMode(m)}>
            {m==="capture"?"Deterministic capture":m==="dag"?"DAG run (LLM)":"Login handoff"}</div>)}
      </div>
      {mode==="dag" && <>
        <label className="muted">Query (planner → browser → distiller → formatter, needs the Orin)</label>
        <textarea rows={3} value={query} onChange={e=>setQuery(e.target.value)} />
      </>}
      {mode==="capture" && <>
        <label className="muted">Start URL — captured deterministically, no LLM clicks</label>
        <input value={url} onChange={e=>setUrl(e.target.value)} />
        <input value={goal} onChange={e=>setGoal(e.target.value)} placeholder="goal / label" />
        <label className="muted">Scripted steps (JSON) — visible browser actions after the initial load
          (filter / sort / open detail); leave empty for a plain generic capture.
          Selectors are site-specific: add {'"optional": true'} to skip steps the
          site doesn't have, {'"timeout": 5'} to fail them faster.</label>
        <textarea rows={6} className="mono" style={{fontSize:12}} value={steps}
          onChange={e=>setSteps(e.target.value)} />
        <label className="muted">Auth session (optional)</label>
        <select value={auth} onChange={e=>setAuth(e.target.value)}>
          <option value="">— none (public) —</option>
          {(health.auth_sessions||[]).map(a => <option key={a} value={a}>{a}</option>)}
        </select>
      </>}
      {mode==="login" && <>
        <div className="muted" style={{marginBottom:6}}>Opens a visible browser; log in by hand;
          the session (cookies, no password) is saved for reuse by capture.</div>
        <input value={loginUrl} onChange={e=>setLoginUrl(e.target.value)} placeholder="login URL" />
        <input value={loginName} onChange={e=>setLoginName(e.target.value)} placeholder="session name" />
      </>}
      <div className="row" style={{marginTop:8}}>
        <button className="btn" disabled={busy} onClick={go}>{busy?"working…":"▶ Launch"}</button>
        {mode==="dag" && !health.gateway?.up && <span className="muted">gateway down — DAG runs need the Orin</span>}
      </div>
    </div>
  );
}

/* ───────────────────────── offline backup card ───────────────────────── */
function Backups() {
  const [list, setList] = useState([]);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);
  const refresh = useCallback(() => api("/api/backups").then(d => setList(d.backups||[])), []);
  useEffect(() => { refresh(); }, [refresh]);
  const backup = async () => {
    setBusy(true); setMsg(null);
    try {
      const r = await api("/api/backup", { method:"POST" });
      setMsg(r.ok ? `✓ ${r.sessions} sessions → ${fmtBytes(r.size)}` : "backup failed");
      refresh();
    } catch (e) { setMsg("backup failed: " + e); }
    setBusy(false);
  };
  return (
    <div style={{marginTop:16,borderTop:"1px solid var(--line)",paddingTop:12}}>
      <div className="muted" style={{marginBottom:8,fontSize:11,textTransform:"uppercase"}}>Offline backups</div>
      <button className="btn ghost small" disabled={busy} onClick={backup}>
        {busy ? <><span className="spinner"/> backing up…</> : "🗄 Back up all data"}</button>
      {msg && <div className="muted" style={{fontSize:11,marginTop:6}}>{msg}</div>}
      {list.map(b =>
        <div key={b.name} className="row" style={{fontSize:11,marginTop:6,justifyContent:"space-between"}}>
          <a href={`/api/backup/${b.name}`} download className="mono">{b.name}</a>
          <span className="muted">{fmtBytes(b.size)} · {fmtAge(b.mtime)}</span>
        </div>)}
      {list.length === 0 && !msg &&
        <div className="muted" style={{fontSize:11,marginTop:6}}>No backups yet — sessions zip for offline review.</div>}
    </div>
  );
}

/* ───────────────────────── app ───────────────────────── */
function App() {
  const [sessions, setSessions] = useState([]);
  const [sel, setSel] = useState(null);
  const [health, setHealth] = useState({});
  const [zoom, setZoom] = useState(null);
  const [launchKey, setLaunchKey] = useState(0);   // bump to reset the Launch form

  const refresh = useCallback(() => {
    api("/api/sessions").then(d => setSessions(d.sessions||[]));
    api("/api/health").then(setHealth);
  }, []);

  const newSession = () => { setSel(null); setLaunchKey(k => k + 1); };

  const delSession = async (e, sid) => {
    e.stopPropagation();
    if (!confirm(`Delete session ${sid}?\nScreenshots, HTML snapshots and records are removed permanently (export first if needed).`)) return;
    try {
      const r = await api(`/api/session/${sid}`, { method:"DELETE" });
      if (!r.ok) { alert(`delete failed: ${r.detail || JSON.stringify(r)}`); return; }
      if (sel === sid) setSel(null);
      refresh();
    } catch (err) { alert("delete failed: " + err); }
  };
  useEffect(() => { refresh(); const t = setInterval(refresh, 4000); return () => clearInterval(t); }, [refresh]);

  return (
    <>
      <header>
        <h1>RAXON · Browser Comparison Terminal</h1>
        <span className="muted"><span className={`dot ${health.gateway?.up?"up":"down"}`}></span>
          gateway {health.gateway?.up?"up":"down"}</span>
        <span className="muted">{sessions.length} sessions</span>
        <span className="muted" style={{marginLeft:"auto"}}>§18 deterministic capture + replay</span>
      </header>
      <div className="layout">
        <div className="side">
          <div className="row" style={{justifyContent:"space-between",marginBottom:8}}>
            <div className="muted" style={{fontSize:11,textTransform:"uppercase"}}>Sessions</div>
            <button className="btn ghost small" onClick={newSession} title="clear the panel and reset the launch form">＋ new session</button>
          </div>
          {sessions.map(s =>
            <div key={s.sid} className={`sess ${sel===s.sid?"active":""}`} onClick={()=>setSel(s.sid)}>
              <div className="row" style={{justifyContent:"space-between"}}>
                <b className="mono" style={{fontSize:12}}>{s.sid}</b>
                <span className="row" style={{gap:6}}>
                  <span className={`tag ${s.kind}`}>{s.kind}</span>
                  {!s.running &&
                    <a title="delete session" onClick={e=>delSession(e, s.sid)}
                       style={{color:"var(--red)",cursor:"pointer",fontSize:12,lineHeight:1}}>✕</a>}
                </span>
              </div>
              <div className="muted" style={{fontSize:11,marginTop:3,whiteSpace:"nowrap",
                overflow:"hidden",textOverflow:"ellipsis"}}>{s.goal||"—"}</div>
              {s.running
                ? <div className="stage"><span className="spinner" style={{width:9,height:9}}/> {s.stage || "running…"}</div>
                : <div className="muted" style={{fontSize:10,marginTop:2}}>{fmtAge(s.mtime)}</div>}
            </div>)}
          <Backups />
        </div>
        <div className="main">
          <Launch key={launchKey} flash={launchKey > 0} health={health}
                  onStarted={sid=>{ refresh(); setSel(sid); }} />
          <Panel sid={sel} onZoom={setZoom} />
        </div>
      </div>
      {zoom && <div className="modal" onClick={()=>setZoom(null)}><img src={zoom} /></div>}
    </>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
