"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

const SAFE_LINK = /^(https?:|mailto:)/i;

/**
 * AI answers are Markdown (tables, bold, lists). Raw HTML in the answer is
 * dropped (no rehype-raw), images are not rendered, and only http(s)/mailto
 * links stay clickable — the same policy as the DOMPurify setup of the old UI.
 */
export default function Markdown({ text }: { text: string }) {
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        disallowedElements={["img"]}
        unwrapDisallowed
        components={{
          a: ({ href, children }) =>
            href && SAFE_LINK.test(href) ? (
              <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>
            ) : (
              <span>{children}</span>
            ),
          table: ({ children }) => (
            <div className="md-table"><table>{children}</table></div>
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
