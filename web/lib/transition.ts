type Nav = { push: (h: string) => void; replace: (h: string) => void };

/** Tấm màn tối trượt lên phủ kín màn hình, chuyển trang, rồi trượt tiếp lên để lộ trang mới. */
export function go(router: Nav, href: string, replace = false) {
  const root = document.documentElement;
  if (root.classList.contains("leaving")) return;
  if (href === window.location.pathname) {
    window.scrollTo({ top: 0, behavior: "smooth" });
    return;
  }
  root.classList.add("leaving");
  setTimeout(() => (replace ? router.replace(href) : router.push(href)), 560);
  setTimeout(() => root.classList.remove("leaving", "revealing"), 5000);
}

/** Gọi khi trang mới đã hiện ra (app/template.tsx). */
export function endLeave() {
  const root = document.documentElement;
  if (!root.classList.contains("leaving")) return;
  root.classList.remove("leaving");
  root.classList.add("revealing");
  setTimeout(() => root.classList.remove("revealing"), 750);
}
