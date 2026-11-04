// Line icons for the chat page, drawn on a 16 px grid like the landing page demo.

const PATHS = {
  search: "M7 2.5a4.5 4.5 0 1 1 0 9 4.5 4.5 0 0 1 0-9zM10.4 10.4 14 14",
  compose: "M7 2.5H3.5v10h10V9M12.2 2.3l1.5 1.5L8 9.5 6 10l.5-2z",
  clip: "M10.5 5 6 9.5a1.5 1.5 0 0 0 2 2L13 6.5a3 3 0 0 0-4.2-4.2L3.5 7.5a4.5 4.5 0 0 0 6.4 6.4l3-3",
  up: "M8 13V3M4 7l4-4 4 4",
  report: "M4 1.5h5.5l3 3v10H4zM9.5 1.5v3h3M6.5 8.5h3.5M6.5 11h3.5",
  news: "M2.5 3h9v10.5h-7a2 2 0 0 1-2-2zM11.5 6h2v6a1.5 1.5 0 0 1-3 0M5 6h4M5 8.5h4M5 11h2.5",
  grid: "M2.5 2.5h4.5v4.5H2.5zM9 2.5h4.5v4.5H9zM2.5 9h4.5v4.5H2.5zM9 9h4.5v4.5H9z",
  list: "M5.5 4h8M5.5 8h8M5.5 12h8M2.5 4h.01M2.5 8h.01M2.5 12h.01",
  chevron: "M4.5 6.5 8 10l3.5-3.5",
  upload: "M8 10.5v-8M5 5.5l3-3 3 3M2.5 10v3.5h11V10",
  globe: "M8 1.5a6.5 6.5 0 1 1 0 13 6.5 6.5 0 0 1 0-13zM1.5 8h13M8 1.5c2 2.2 2 10.8 0 13M8 1.5c-2 2.2-2 10.8 0 13",
} as const;

export function Icon({ name, size = 16 }: { name: keyof typeof PATHS; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth={1.5}
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={PATHS[name]} />
    </svg>
  );
}
