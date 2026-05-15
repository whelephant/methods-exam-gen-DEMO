import Link from "next/link";
import Image from "next/image";
import { supabase, publicAssetUrl } from "@/lib/supabase";
import { Markdown } from "@/lib/markdown";
import type { Question, StudyArea, StudyPoint } from "@/lib/database.types";

// Always fetch fresh — data only changes when the local sync script runs,
// but the demo is low-traffic enough that we don't need to over-optimise.
export const dynamic = "force-dynamic";

type SearchParams = { aos?: string; dp?: string };

async function getTaxonomy(): Promise<{ areas: StudyArea[]; points: StudyPoint[] }> {
  const [areas, points] = await Promise.all([
    supabase.from("study_areas").select("*").order("aos"),
    supabase.from("study_points").select("*").order("aos").order("sort_order"),
  ]);
  if (areas.error) throw areas.error;
  if (points.error) throw points.error;
  return { areas: areas.data ?? [], points: points.data ?? [] };
}

async function getFilteredQuestions(
  aos: number | null,
  dp: number | null,
): Promise<Question[]> {
  if (aos === null) {
    // No filter selected — show a sample so the page isn't empty.
    const r = await supabase
      .from("questions")
      .select("*")
      .order("id")
      .limit(20);
    if (r.error) throw r.error;
    return r.data ?? [];
  }

  // Get question_ids matching the tag filter, then fetch those questions.
  let tagQuery = supabase
    .from("question_tags")
    .select("question_id")
    .eq("aos", aos);
  if (dp !== null) tagQuery = tagQuery.eq("dot_point_sort_order", dp);
  const tags = await tagQuery;
  if (tags.error) throw tags.error;

  const ids = Array.from(new Set((tags.data ?? []).map((t) => t.question_id)));
  if (ids.length === 0) return [];

  const r = await supabase
    .from("questions")
    .select("*")
    .in("id", ids)
    .order("id")
    .limit(50);
  if (r.error) throw r.error;
  return r.data ?? [];
}

function QuestionCard({ q }: { q: Question }) {
  const diagramUrl = publicAssetUrl(q.diagram_path);
  return (
    <Link
      href={`/q/${q.id}`}
      className="block border border-stone-200 rounded-lg bg-white p-5 hover:border-stone-300 hover:shadow-sm transition"
    >
      <div className="flex items-baseline justify-between mb-3 text-xs text-stone-500">
        <span className="font-mono">{q.id}</span>
        <span>
          {q.marks !== null ? `${q.marks} mark${q.marks === 1 ? "" : "s"}` : "—"}
        </span>
      </div>
      <div className="line-clamp-3">
        <Markdown>{q.prompt_md}</Markdown>
      </div>
      {q.has_diagram && diagramUrl && (
        <div className="mt-3 border border-stone-100 rounded bg-stone-50 p-2">
          <Image
            src={diagramUrl}
            alt={`diagram for ${q.id}`}
            width={400}
            height={200}
            className="w-full h-auto max-h-40 object-contain"
            unoptimized
          />
        </div>
      )}
    </Link>
  );
}

export default async function Home({
  searchParams,
}: {
  searchParams: Promise<SearchParams>;
}) {
  const params = await searchParams;
  const aos = params.aos ? Number(params.aos) : null;
  const dp = params.dp ? Number(params.dp) : null;

  const [{ areas, points }, questions] = await Promise.all([
    getTaxonomy(),
    getFilteredQuestions(aos, dp),
  ]);

  const pointsForAos = aos !== null ? points.filter((p) => p.aos === aos && !p.is_header) : [];
  const selectedArea = aos !== null ? areas.find((a) => a.aos === aos) : null;
  const selectedPoint = dp !== null ? points.find((p) => p.aos === aos && p.sort_order === dp) : null;

  return (
    <div className="grid grid-cols-1 lg:grid-cols-[260px_1fr] gap-8">
      {/* Sidebar: AoS + dot-point filter */}
      <aside>
        <h2 className="text-xs uppercase tracking-wide text-stone-500 mb-3">Areas of Study</h2>
        <ul className="space-y-1 mb-6">
          <li>
            <Link
              href="/"
              className={`block px-3 py-2 rounded text-sm ${
                aos === null
                  ? "bg-stone-900 text-white"
                  : "text-stone-700 hover:bg-stone-100"
              }`}
            >
              All areas
            </Link>
          </li>
          {areas.map((a) => (
            <li key={a.aos}>
              <Link
                href={`/?aos=${a.aos}`}
                className={`block px-3 py-2 rounded text-sm ${
                  aos === a.aos && dp === null
                    ? "bg-stone-900 text-white"
                    : "text-stone-700 hover:bg-stone-100"
                }`}
              >
                <span className="text-stone-400 mr-2 text-xs">AoS {a.aos}</span>
                {a.title}
              </Link>
            </li>
          ))}
        </ul>

        {aos !== null && pointsForAos.length > 0 && (
          <>
            <h2 className="text-xs uppercase tracking-wide text-stone-500 mb-3">Dot points</h2>
            <ul className="space-y-1">
              {pointsForAos.map((p) => (
                <li key={p.sort_order}>
                  <Link
                    href={`/?aos=${aos}&dp=${p.sort_order}`}
                    className={`block px-3 py-2 rounded text-xs leading-tight ${
                      dp === p.sort_order
                        ? "bg-stone-900 text-white"
                        : "text-stone-700 hover:bg-stone-100"
                    }`}
                  >
                    <span className="text-stone-400 mr-1.5">{aos}.{p.sort_order}</span>
                    {p.text}
                  </Link>
                </li>
              ))}
            </ul>
          </>
        )}
      </aside>

      {/* Main column: filter summary + question list */}
      <section>
        <div className="mb-6">
          <h1 className="text-2xl font-semibold tracking-tight mb-1">
            {selectedPoint ? (
              <>
                <span className="text-stone-400 mr-2">{aos}.{dp}</span>
                {selectedPoint.text}
              </>
            ) : selectedArea ? (
              selectedArea.title
            ) : (
              "Browse all questions"
            )}
          </h1>
          <p className="text-sm text-stone-600">
            {questions.length === 50
              ? `Showing 50 questions (limit). Pick a dot point to narrow.`
              : `${questions.length} question${questions.length === 1 ? "" : "s"}`}
            {aos === null && " — pick an Area of Study to filter."}
          </p>
        </div>

        {questions.length === 0 ? (
          <div className="border border-dashed border-stone-300 rounded p-8 text-center text-sm text-stone-500">
            No questions tagged here yet.
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {questions.map((q) => (
              <QuestionCard key={q.id} q={q} />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
