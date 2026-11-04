"use client";

import Markdown from "./Markdown";

const NEWS_HEAD = /^\*\*Tin tức doanh nghiệp\b/;
const NOTES_HEAD = /^(\*Thu thập:\*|\*Giới hạn:\*|Sắc thái tổng hợp \(phụ\):)/;
const NEWS_META = /^(\s*)(Mức xác nhận:|Còn chưa chắc:|Sắc thái \(phụ\):)\s*(.*)$/;

/**
 * Split a final answer into the analysis, the appended company-news block and
 * its collection notes (the backend writes them in this order). The text is
 * unchanged; only how it is laid out differs.
 */
export function splitAnswer(text: string): { main: string; news: string; notes: string } {
  const lines = text.split("\n");
  const newsAt = lines.findIndex((line) => NEWS_HEAD.test(line.trim()));
  if (newsAt < 0) return { main: text, news: "", notes: "" };
  const rest = lines.slice(newsAt);
  const notesAt = rest.findIndex((line, i) => i > 0 && NOTES_HEAD.test(line.trim()));
  const news = notesAt < 0 ? rest : rest.slice(0, notesAt);
  const notes = notesAt < 0 ? [] : rest.slice(notesAt);
  return {
    main: lines.slice(0, newsAt).join("\n").trim(),
    news: news.slice(1).flatMap((line, i, all) => {
      // Per-event meta lines become emphasis, styled as a quiet line under the event.
      const meta = NEWS_META.exec(line);
      const out = meta && meta[3] && !meta[3].includes("*") ? `${meta[1]}*${meta[2]} ${meta[3]}*` : line;
      // A quote ends at its own line: without a blank line, Markdown would pull the
      // event's meta lines into the quote (lazy continuation).
      const quote = /^(\s*)>/.exec(line);
      const next = all[i + 1];
      return quote && next !== undefined && !/^\s*>/.test(next) && next.trim() ? [out, quote[1]] : [out];
    }).join("\n").trim(),
    notes: notes.join("\n").trim(),
  };
}

const LIST_ITEM = /^\s*([-*+]|\d+\.)\s/;
const LABEL_VALUE = /^(\*\*)?([^:*\n]{2,90}?)(\*\*)?((?:\s[^:*\n]{1,40})?):\s+(.*)$/;
const TRAILING_SOURCE = /\s\(([^()]{6,})\)\s*$/;

/**
 * Even out the model's Markdown without changing its words:
 * - a paragraph right under a list (no blank line) starts its own paragraph,
 *   instead of being glued to the last item;
 * - a list item "label: value with a number" always has its label in bold;
 * - a trailing source in capitals, "(BẢNG CÂN ĐỐI KẾ TOÁN)", becomes a quiet tag;
 * - "19.0 %" loses the space before the percent sign.
 */
export function tidyAnswer(md: string): string {
  const out: string[] = [];
  for (let line of md.split("\n")) {
    const prev = out.length ? out[out.length - 1] : "";
    if (line.trim() && !/^\s/.test(line) && !LIST_ITEM.test(line) && LIST_ITEM.test(prev)) out.push("");
    const item = /^(\s*[-*+]\s+)(.*)$/.exec(line);
    if (item) {
      let body = item[2];
      const pair = LABEL_VALUE.exec(body);
      if (pair && /\d/.test(pair[5])) body = `**${pair[2].trim()}${pair[4]}**: ${pair[5]}`;
      const source = TRAILING_SOURCE.exec(body);
      if (source && source[1] === source[1].toUpperCase() && /\p{Lu}/u.test(source[1])) {
        body = body.slice(0, source.index) + ` *(${source[1]})*`;
      }
      line = item[1] + body;
    }
    out.push(line.replace(/(\d)\s+%/g, "$1%"));
  }
  return out.join("\n");
}

/** A bot answer: the analysis, then company news as a card, then folded notes. */
export default function AnswerContent({ text }: { text: string }) {
  const { main, news, notes } = splitAnswer(text);
  const newsTitle = news ? (text.split("\n").find((line) => NEWS_HEAD.test(line.trim())) || "") : "";
  const title = /^\*\*([^*]+)\*\*/.exec(newsTitle.trim())?.[1] || "Tin tức doanh nghiệp";
  const source = /\(([^)]*)\)\s*$/.exec(newsTitle)?.[1] || "";
  return (
    <div className="answer">
      {main && <Markdown text={tidyAnswer(main)} />}
      {news && (
        <section className="answer-news" aria-label="Tin tức doanh nghiệp">
          <header className="answer-news-head">
            <span>{title}</span>
            {source && <span className="answer-news-source">{source}</span>}
          </header>
          <Markdown text={news} />
        </section>
      )}
      {notes && (
        <details className="answer-notes">
          <summary>Cách thu thập và giới hạn của phần tin tức</summary>
          <Markdown text={notes} />
        </details>
      )}
    </div>
  );
}
