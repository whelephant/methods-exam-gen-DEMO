import Link from "next/link";
import Image from "next/image";
import { notFound } from "next/navigation";
import { supabase, publicAssetUrl } from "@/lib/supabase";
import { Markdown } from "@/lib/markdown";
import type { Question, Source, McOption, StudyPoint, Answer } from "@/lib/database.types";

export const dynamic = "force-dynamic";

type Params = { id: string };

async function getQuestionBundle(id: string) {
  const [qRes, aRes, tRes] = await Promise.all([
    supabase.from("questions").select("*").eq("id", id).maybeSingle(),
    supabase.from("answers").select("*").eq("question_id", id).maybeSingle(),
    supabase
      .from("question_tags")
      .select("*")
      .eq("question_id", id)
      .order("is_primary", { ascending: false }),
  ]);
  if (qRes.error) throw qRes.error;
  const question = qRes.data as Question | null;
  if (!question) return null;

  const sourceRes = await supabase
    .from("sources")
    .select("*")
    .eq("id", question.source_id)
    .maybeSingle();
  const source = sourceRes.data as Source | null;

  // Fetch the human-readable text for each tag's dot point.
  const tagRows = tRes.data ?? [];
  const dpKeys = tagRows.map((t) => ({ subject: t.subject, aos: t.aos, sort_order: t.dot_point_sort_order }));
  let points: StudyPoint[] = [];
  if (dpKeys.length > 0) {
    const aosList = Array.from(new Set(dpKeys.map((k) => k.aos)));
    const soList = Array.from(new Set(dpKeys.map((k) => k.sort_order)));
    const pRes = await supabase
      .from("study_points")
      .select("*")
      .in("aos", aosList)
      .in("sort_order", soList);
    points = (pRes.data ?? []).filter((p) =>
      dpKeys.some((k) => k.aos === p.aos && k.sort_order === p.sort_order),
    );
  }

  return {
    question,
    source,
    answer: (aRes.data as Answer | null) ?? null,
    tags: tagRows.map((t) => ({
      ...t,
      label: points.find(
        (p) => p.aos === t.aos && p.sort_order === t.dot_point_sort_order,
      )?.text,
    })),
  };
}

function paperLabel(source: Source | null, q: Question): string {
  if (!source) return q.id;
  const sec = q.section ? ` Section ${q.section}` : "";
  return `${source.year} Paper ${source.paper}${sec}`;
}

function questionLabel(q: Question): string {
  const partStr = q.part ? `.${q.part}` : "";
  return `Q${q.question_number}${partStr}`;
}

function McOptions({ options, correct }: { options: McOption[]; correct: string | null }) {
  const letters = ["A", "B", "C", "D", "E"];
  return (
    <ol className="space-y-2 mt-3">
      {options.map((opt, idx) => {
        const letter = letters[idx];
        const isCorrect = correct && letter === correct;
        return (
          <li
            key={idx}
            className={`flex gap-3 items-start border rounded-md p-3 ${
              isCorrect ? "border-emerald-300 bg-emerald-50" : "border-stone-200 bg-white"
            }`}
          >
            <span className="font-mono text-sm w-6 shrink-0 text-stone-500">{letter}.</span>
            <div className="flex-1 min-w-0">
              {opt.kind === "text" ? (
                <Markdown>{opt.md}</Markdown>
              ) : opt.path ? (
                <Image
                  src={publicAssetUrl(opt.path)!}
                  alt={`option ${letter}`}
                  width={300}
                  height={150}
                  className="max-h-32 w-auto"
                  unoptimized
                />
              ) : (
                <span className="text-xs text-stone-400">(diagram option pending)</span>
              )}
            </div>
            {isCorrect && (
              <span className="text-xs font-medium text-emerald-700 self-center">correct</span>
            )}
          </li>
        );
      })}
    </ol>
  );
}

export default async function QuestionPage({ params }: { params: Promise<Params> }) {
  const { id } = await params;
  const bundle = await getQuestionBundle(id);
  if (!bundle) notFound();
  const { question, source, answer, tags } = bundle;
  const diagramUrl = publicAssetUrl(question.diagram_path);
  const answerImageUrl = publicAssetUrl(answer?.answer_image_path);

  return (
    <article className="max-w-3xl mx-auto">
      <div className="text-xs text-stone-500 mb-2 flex items-baseline gap-3">
        <Link href="/" className="hover:underline">← Browse</Link>
        <span className="font-mono">{question.id}</span>
      </div>

      <header className="mb-6">
        <h1 className="text-2xl font-semibold tracking-tight mb-1">
          {paperLabel(source, question)} — {questionLabel(question)}
        </h1>
        <div className="flex flex-wrap items-center gap-3 text-sm text-stone-600">
          {question.marks !== null && (
            <span>{question.marks} mark{question.marks === 1 ? "" : "s"}</span>
          )}
          {question.is_mc && <span className="text-stone-400">·</span>}
          {question.is_mc && <span>Multiple choice</span>}
          {source && <span className="text-stone-400">·</span>}
          {source && (
            <span className="text-stone-400">
              page {question.source_page_start}
              {question.source_page_end !== question.source_page_start && `–${question.source_page_end}`}
            </span>
          )}
        </div>

        {tags.length > 0 && (
          <div className="flex flex-wrap gap-2 mt-3">
            {tags.map((t) => (
              <span
                key={`${t.aos}-${t.dot_point_sort_order}`}
                className={`inline-flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full ${
                  t.is_primary
                    ? "bg-stone-900 text-white"
                    : "bg-stone-100 text-stone-700"
                }`}
                title={t.label ?? undefined}
              >
                <span className="font-mono">
                  {t.aos}.{t.dot_point_sort_order}
                </span>
                <span className="truncate max-w-[28ch]">{t.label}</span>
              </span>
            ))}
          </div>
        )}
      </header>

      <section className="border border-stone-200 bg-white rounded-lg p-6 mb-6">
        <Markdown>{question.prompt_md}</Markdown>

        {question.has_diagram && diagramUrl && (
          <div className="mt-5 border border-stone-100 rounded bg-stone-50 p-3">
            <Image
              src={diagramUrl}
              alt={`diagram for ${question.id}`}
              width={800}
              height={500}
              className="w-full h-auto max-h-[500px] object-contain"
              unoptimized
            />
          </div>
        )}

        {question.is_mc && question.mc_options_md && (
          <McOptions options={question.mc_options_md} correct={question.mc_correct} />
        )}
      </section>

      <section>
        <h2 className="text-sm font-semibold uppercase tracking-wide text-stone-500 mb-3">
          Examiner report
        </h2>
        {answerImageUrl ? (
          <div className="border border-stone-200 bg-white rounded-lg p-3">
            <Image
              src={answerImageUrl}
              alt={`examiner answer for ${question.id}`}
              width={800}
              height={1000}
              className="w-full h-auto"
              unoptimized
            />
          </div>
        ) : answer?.commentary_md ? (
          <div className="border border-stone-200 bg-white rounded-lg p-6">
            <Markdown>{answer.commentary_md}</Markdown>
          </div>
        ) : (
          <div className="border border-dashed border-stone-300 rounded p-6 text-sm text-stone-500">
            No examiner report on file for this question yet.
          </div>
        )}
      </section>
    </article>
  );
}
