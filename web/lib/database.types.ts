// Hand-written TypeScript types for the public Supabase schema.
// Mirrors supabase/migrations/0001_init.sql. Keep in sync when the schema changes.
//
// (Could be auto-generated with `supabase gen types typescript --project-id <id>`
// once we install the Supabase CLI, but hand-rolling is fine for 6 tables.)

export type StudyArea = {
  subject: string;
  aos: number;
  title: string;
  intro: string;
};

export type StudyPoint = {
  subject: string;
  aos: number;
  is_header: boolean;
  text: string;
  sort_order: number;
};

export type Source = {
  id: number;
  year: number;
  paper: 1 | 2;
  format_era: "legacy" | "modern_2024";
  skipped_pages: number[] | null;
};

export type McOption =
  | { kind: "text"; md: string }
  | { kind: "diagram"; bbox: [number, number, number, number]; path?: string };

export type Question = {
  id: string;
  source_id: number;
  section: "A" | "B" | null;
  question_number: number;
  part: string | null;
  marks: number | null;
  prompt_md: string;
  is_mc: boolean;
  mc_options_md: McOption[] | null;
  mc_correct: string | null;
  has_diagram: boolean;
  diagram_path: string | null;
  source_page_start: number;
  source_page_end: number;
  created_at: string;
};

export type QuestionTag = {
  question_id: string;
  subject: string;
  aos: number;
  dot_point_sort_order: number;
  is_primary: boolean;
  confidence: number | null;
  tagged_by: string | null;
};

export type Answer = {
  question_id: string;
  final_answer_md: string | null;
  commentary_md: string;
  answer_image_path: string | null;
  created_at: string;
};

// supabase-js's column-string parser requires `Relationships: []` on each
// table or it falls back to typing rows as `never`. Empty list is fine for
// hand-rolled types where we don't model joins via the SDK.
type TableShape<T> = { Row: T; Insert: T; Update: Partial<T>; Relationships: [] };

export type Database = {
  public: {
    Tables: {
      study_areas:    TableShape<StudyArea>;
      study_points:   TableShape<StudyPoint>;
      sources:        TableShape<Source>;
      questions:      TableShape<Question>;
      question_tags:  TableShape<QuestionTag>;
      answers:        TableShape<Answer>;
    };
    Views: Record<string, never>;
    Functions: Record<string, never>;
    Enums: Record<string, never>;
    CompositeTypes: Record<string, never>;
  };
};
