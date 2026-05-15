import ReactMarkdown from "react-markdown";
import remarkMath from "remark-math";
import remarkGfm from "remark-gfm";
import rehypeKatex from "rehype-katex";

// KaTeX-aware markdown renderer. Use anywhere a question prompt or answer
// commentary is displayed.
//
// remarkGfm gives us tables (used in some prompts), and remarkMath/rehypeKatex
// pair to handle $..$ and $$..$$ in KaTeX. The local FastAPI app uses a custom
// placeholder swap (see CLAUDE.md "markdown-it-py with math sentinels") because
// markdown-it-py would interpret `_` inside math as italics; remark-math handles
// that natively, so no manual sentinel work needed here.
export function Markdown({ children }: { children: string }) {
  return (
    <div className="prose-vcaa">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeKatex]}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
