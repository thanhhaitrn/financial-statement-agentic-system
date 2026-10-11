(() => {
  "use strict";

  const burger = document.getElementById("navBurger");
  const menu = document.getElementById("mobileMenu");
  if (burger && menu) {
    burger.addEventListener("click", () => {
      const open = menu.classList.toggle("open");
      burger.setAttribute("aria-expanded", String(open));
    });

    menu.querySelectorAll("a").forEach((a) =>
      a.addEventListener("click", () => {
        menu.classList.remove("open");
        burger.setAttribute("aria-expanded", "false");
      })
    );
  }

  // The session cookie is HttpOnly, so ask the server whether we are signed in.
  fetch("/api/me", { credentials: "same-origin" })
    .then((response) => {
      if (!response.ok) return;
      document
        .querySelectorAll('a[href^="auth.html"], a[href^="/login"], a[href^="/register"]')
        .forEach((a) => (a.href = "/chat"));
    })
    .catch(() => {});
})();
