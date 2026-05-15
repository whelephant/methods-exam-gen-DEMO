import Link from "next/link";
import { supabase } from "@/lib/supabase";
import type { Question, QuestionTag, StudyArea, Source } from "@/lib/database.types";

export const dynamic = "force-dynamic";

// PostgREST defaults to 1000 rows per request even with .range(), so anything
// over that needs explicit pagination. The biggest tables here are questions
// (~1k) and question_tags (~1k), so two chunks is enough.
async function fetchAll<T>(
  builder: () => ReturnType<ReturnType<typeof supabase.from>["select"]>,
  pageSize = 1000,
): Promise<T[]> {
  const out: T[] = [];
  for (let from = 0; ; from += pageSize) {
    const res = await builder().range(from, from + pageSize - 1);
    if (res.error) throw res.error;
    const rows = (res.data ?? []) as T[];
    out.push(...rows);
    if (rows.length < pageSize) break;
  }
  return out;
}

async function loadEverything() {
  const [areas, sources, questions, tags, answers] = await Promise.all([
    supabase.from("study_areas").select("*").order("aos"),
    supabase.from("sources").select("*").order("year").order("paper"),
    fetchAll<Pick<Question, "id" | "source_id" | "is_mc" | "has_diagram" | "marks" | "section">>(
      () => supabase.from("questions").select("id, source_id, is_mc, has_diagram, marks, section"),
    ),
    fetchAll<Pick<QuestionTag, "question_id" | "aos" | "is_primary">>(
      () => supabase.from("question_tags").select("question_id, aos, is_primary"),
    ),
    fetchAll<{ question_id: string }>(() => supabase.from("answers").select("question_id")),
  ]);
  if (areas.error) throw areas.error;
  if (sources.error) throw sources.error;
  return {
    areas: (areas.data ?? []) as StudyArea[],
    sources: (sources.data ?? []) as Source[],
    questions,
    tags,
    answerIds: new Set(answers.map((a) => a.question_id)),
  };
}

function Bar({
  label,
  count,
  max,
  href,
}: {
  label: React.ReactNode;
  count: number;
  max: number;
  href?: string;
}) {
  const pct = max === 0 ? 0 : Math.round((count / max) * 100);
  const inner = (
    <div className="flex items-center gap-3 group">
      <div className="w-32 text-sm text-stone-700 shrink-0">{label}</div>
      <div className="flex-1 h-6 bg-stone-100 rounded relative overflow-hidden">
        <div
          className="absolute inset-y-0 left-0 bg-stone-900 group-hover:bg-stone-700 transition rounded"
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className="w-12 text-sm tabular-nums text-right text-stone-600">{count}</div>
    </div>
  );
  return href ? <Link href={href}>{inner}</Link> : inner;
}

export default async function StatsPage() {
  const { areas, sources, questions, tags, answerIds } = await loadEverything();

  const totalQ = questions.length;
  const mcCount = questions.filter((q) => q.is_mc).length;
  const diagramCount = questions.filter((q) => q.has_diagram).length;
  const withAnswer = questions.filter((q) => answerIds.has(q.id)).length;
  const totalMarks = questions.reduce((s, q) => s + (q.marks ?? 0), 0);

  const byPrimaryAos = new Map<number, number>();
  for (const t of tags) {
    if (!t.is_primary) continue;
    byPrimaryAos.set(t.aos, (byPrimaryAos.get(t.aos) ?? 0) + 1);
  }
  const maxAos = Math.max(...byPrimaryAos.values(), 1);

  const sourcesById = new Map(sources.map((s) => [s.id, s]));
  const byYear = new Map<number, number>();
  for (const q of questions) {
    const s = sourcesById.get(q.source_id);
    if (!s) continue;
    byYear.set(s.year, (byYear.get(s.year) ?? 0) + 1);
  }
  const years = Array.from(byYear.keys()).sort((a, b) => a - b);
  const maxYear = Math.max(...byYear.values(), 1);

  return (
    <div className="max-w-3xl mx-auto space-y-10">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Corpus stats</h1>
        <p className="text-sm text-stone-600 mt-1">
          What's currently sitting in the database.
        </p>
      </header>

      <section className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        {[
          { label: "Questions", value: totalQ },
          { label: "Marks total", value: totalMarks },
          { label: "Source PDFs", value: sources.length },
          { label: "Years", value: years.length },
        ].map((s) => (
          <div key={s.label} className="border border-stone-200 bg-white rounded-lg p-4">
            <div className="text-2xl font-semibold tabular-nums">{s.value.toLocaleString()}</div>
            <div className="text-xs text-stone-500 mt-0.5">{s.label}</div>
          </div>
        ))}
      </section>

      <section>
        <h2 className="text-sm font-semibold uppercase tracking-wide text-stone-500 mb-4">
          By Area of Study <span className="font-normal normal-case text-stone-400">(primary tag)</span>
        </h2>
        <div className="space-y-2">
          {areas.map((a) => (
            <Bar
              key={a.aos}
              href={`/?aos=${a.aos}`}
              label={
                <span>
                  <span className="text-stone-400 font-mono mr-1.5 text-xs">AoS {a.aos}</span>
                  {a.title}
                </span>
              }
              count={byPrimaryAos.get(a.aos) ?? 0}
              max={maxAos}
            />
          ))}
        </div>
      </section>

      <section>
        <h2 className="text-sm font-semibold uppercase tracking-wide text-stone-500 mb-4">
          By year
        </h2>
        <div className="space-y-2">
          {years.map((y) => (
            <Bar key={y} label={<span className="font-mono">{y}</span>} count={byYear.get(y) ?? 0} max={maxYear} />
          ))}
        </div>
      </section>

      <section>
        <h2 className="text-sm font-semibold uppercase tracking-wide text-stone-500 mb-4">
          Coverage
        </h2>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 text-sm">
          {[
            { label: "Multiple choice", count: mcCount, of: totalQ },
            { label: "With diagram", count: diagramCount, of: totalQ },
            { label: "With examiner report", count: withAnswer, of: totalQ },
          ].map((c) => {
            const pct = c.of === 0 ? 0 : Math.round((c.count / c.of) * 100);
            return (
              <div key={c.label} className="border border-stone-200 bg-white rounded-lg p-4">
                <div className="flex items-baseline gap-2">
                  <div className="text-2xl font-semibold tabular-nums">{c.count}</div>
                  <div className="text-xs text-stone-500">/ {c.of}</div>
                </div>
                <div className="text-xs text-stone-500 mt-0.5">{c.label} ({pct}%)</div>
              </div>
            );
          })}
        </div>
      </section>
    </div>
  );
}
