(() => {
  "use strict";

  const burger = document.getElementById("navBurger");
  const menu = document.getElementById("mobileMenu");
  if (!burger || !menu) return;

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

  // If already logged in, send visitors straight to the app.
  try {
    if (localStorage.getItem("finx_token")) {
      const ctaLinks = document.querySelectorAll('a[href^="auth.html"]');
      ctaLinks.forEach((a) => (a.href = "chat.html"));
    }
  } catch (_) {
    /* localStorage unavailable — ignore */
  }
})();
