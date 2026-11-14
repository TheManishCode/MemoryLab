"use client";
import Link from "next/link";
import { useEffect, useState } from "react";
import { api, describe } from "@/lib/client";
import type { Compare, Experiment, Run } from "@/lib/types";
import { Banner, Button, StatusChip } from "./ui";
import { CompareChart } from "./compare-chart";

export function ExperimentView({ id, viewerId }: { id: string; viewerId: string | null }) {
  const [exp, setExp] = useState<Experiment | null>(null);
  const [cmp, setCmp] = useState<Compare | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [mine, setMine] = useState<Run[]>([]);
  const [pick, setPick] = useState("");
  const owner = viewerId !== null && exp?.owner_id === viewerId;

  async function load() {
    try {
      const [e, c] = await Promise.all([api<Experiment>(`/experiments/${id}`), api<Compare>(`/experiments/${id}/compare?metric=score`)]);
      setExp(e); setCmp(c); setError(null);
    } catch (err) { setError(describe(err)); }
  }
  // eslint-disable-next-line react-hooks/set-state-in-effect -- loads once on mount
  useEffect(() => { void load(); }, [id]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (owner) api<{ runs: Run[] }>("/runs?limit=100").then((p) => setMine(p.runs)).catch(() => undefined); }, [owner]);

  async function edit(body: { add_run_ids?: string[]; remove_run_ids?: string[] }) {
    try { await api(`/experiments/${id}`, { method: "PATCH", body }); setPick(""); await load(); }
    catch (err) { setError(describe(err)); }
  }

  if (error && !exp) return <Banner tone="bad">{error}</Banner>;
  if (!exp) return <p role="status">Loading…</p>;
  const s = exp.summary;
  const labels = Object.fromEntries(exp.runs.map((r) => [r.run_id, `${r.title ?? r.run_id.slice(0, 8)}`]));
  const addable = mine.filter((r) => !exp.runs.some((x) => x.run_id === r.id));
  return (
    <section aria-labelledby="exp-h">
      <h1 id="exp-h" className="text-xl font-semibold">{exp.title}</h1>
      <p className="mb-3 text-sm text-muted">One declared variable: <span className="mono text-ink">{exp.variable}</span> · {exp.visibility}</p>
      {error ? <Banner tone="bad">{error}</Banner> : null}
      {/* the banner names the single changed field and refuses to look like a verdict */}
      <Banner tone={s.warnings.length ? "warn" : "info"}>
        Runs differ only in <strong className="mono">{exp.variable}</strong>. {s.n} scored run{s.n === 1 ? "" : "s"}
        {s.mean !== null ? <>: mean <span className="mono">{s.mean.toFixed(1)}</span>, range <span className="mono">{s.min}–{s.max}</span></> : ""}.
        {s.warnings.includes("n_below_5") ? " Fewer than 5 scored runs: this is not a finding yet." : ""}
        {s.warnings.includes("runs_still_running") ? ` ${s.runs_in_progress} run${s.runs_in_progress === 1 ? " is" : "s are"} still running: the numbers will change.` : ""}
      </Banner>
      {s.arms.length > 1 ? (
        <div className="mt-4">
          <h2 className="mb-1 text-base font-semibold">By {exp.variable}</h2>
          <div className="overflow-x-auto">
            <table className="w-full border border-line bg-panel text-left text-sm">
              <caption className="sr-only">Score statistics for each value of {exp.variable}</caption>
              <thead><tr className="border-b border-line">
                <th scope="col" className="p-2">{exp.variable}</th><th scope="col" className="p-2">n</th>
                <th scope="col" className="p-2">Mean</th><th scope="col" className="p-2">95% CI</th>
                <th scope="col" className="p-2">Median</th><th scope="col" className="p-2">Range</th>
              </tr></thead>
              <tbody>
                {s.arms.map((a) => (
                  <tr key={a.label} className="border-b border-line">
                    <td className="mono p-2">{a.label}</td><td className="mono p-2">{a.n}</td>
                    <td className="mono p-2">{a.mean !== null ? a.mean.toFixed(1) : "–"}</td>
                    <td className="mono p-2">{a.ci95 ? `${a.ci95[0].toFixed(1)}–${a.ci95[1].toFixed(1)}` : "–"}</td>
                    <td className="mono p-2">{a.median ?? "–"}</td>
                    <td className="mono p-2">{a.min !== null ? `${a.min}–${a.max}` : "–"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {s.comparison ? (
            <Banner tone={s.comparison.underpowered ? "warn" : "info"}>
              <strong className="mono">{s.comparison.b_label}</strong> vs <strong className="mono">{s.comparison.a_label}</strong>:
              mean difference <span className="mono">{s.comparison.diff_means.toFixed(1)}</span>
              {" "}(95% CI <span className="mono">{s.comparison.diff_ci95[0].toFixed(1)}–{s.comparison.diff_ci95[1].toFixed(1)}</span>),
              {" "}permutation p {s.comparison.p_value < 0.001 ? "< 0.001" : `= ${s.comparison.p_value.toFixed(3)}`}{s.comparison.exact ? " (exact)" : " (sampled)"}. {s.comparison.note}
            </Banner>
          ) : null}
        </div>
      ) : null}
      {cmp ? <CompareChart data={cmp} labels={labels} /> : null}
      <h2 className="mb-1 mt-4 text-base font-semibold">Runs</h2>
      {exp.runs.length === 0 ? <p className="text-sm text-muted">No runs you may see.</p> : (
        <div className="overflow-x-auto">
          <table className="w-full border border-line bg-panel text-left text-sm">
            <caption className="sr-only">Runs in this experiment</caption>
            <thead><tr className="border-b border-line"><th scope="col" className="p-2">Run</th><th scope="col" className="p-2">Status</th><th scope="col" className="p-2">Model</th><th scope="col" className="p-2">Steps</th><th scope="col" className="p-2">Score</th>{owner ? <th scope="col" className="p-2"><span className="sr-only">Remove</span></th> : null}</tr></thead>
            <tbody>
              {exp.runs.map((r) => (
                <tr key={r.run_id} className="border-b border-line">
                  <td className="p-2"><Link href={`/runs/${r.run_id}`}>{r.title ?? r.run_id.slice(0, 8)}</Link></td>
                  <td className="p-2"><StatusChip status={r.status} /></td><td className="mono p-2">{r.model_id ?? "–"}</td>
                  <td className="mono p-2">{r.steps}</td><td className="mono p-2">{r.progress_score ?? "–"}</td>
                  {owner ? <td className="p-2"><Button variant="plain" onClick={() => void edit({ remove_run_ids: [r.run_id] })} aria-label={`Remove ${r.title ?? r.run_id.slice(0, 8)}`}>Remove</Button></td> : null}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {owner ? (
        <div className="mt-3 flex items-end gap-2">
          <div><label htmlFor="add-run" className="mb-1 block text-sm font-medium">Add one of your runs</label>
            <select id="add-run" className="rounded border border-line bg-panel px-2 py-1.5 text-sm" value={pick} onChange={(e) => setPick(e.target.value)}>
              <option value="">Choose a run</option>{addable.map((r) => <option key={r.id} value={r.id}>{r.title ?? r.id.slice(0, 8)} ({r.model_id})</option>)}</select></div>
          <Button disabled={!pick} onClick={() => void edit({ add_run_ids: [pick] })}>Add</Button>
        </div>
      ) : null}
    </section>
  );
}
