"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Children, isValidElement } from "react";

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
          p: ({ children }) => {
            const parts = Children.toArray(children);
            const first = parts[0];
            // A paragraph that is only bold text ("**1. Khả năng sinh lời**") is a section heading.
            if (parts.length === 1 && isValidElement(first) && first.type === "strong") {
              return <p className="md-heading">{children}</p>;
            }
            // "*Nhận xét*: …" is the model's commentary on the figures above it.
            const second = parts[1];
            if (isValidElement(first) && first.type === "em" && typeof second === "string" && /^\s*:/.test(second)) {
              const label = Children.toArray((first.props as { children?: React.ReactNode }).children).join("");
              return (
                <p className="md-note">
                  <strong className="md-note-label">{label}</strong>{second.replace(/^\s*:\s*/, "")}{parts.slice(2)}
                </p>
              );
            }
            const label = typeof first === "string"
              ? /^(Kết quả|Cách tính|Diễn giải kết quả|Nguồn):\s*/u.exec(first)
              : null;
            return label && typeof first === "string" ? (
              <p>
                <strong className="md-section-label">{label[1]}</strong>
                {first.slice(label[0].length)}{parts.slice(1)}
              </p>
            ) : <p>{children}</p>;
          },
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
