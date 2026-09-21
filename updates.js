(() => {
  const start = () => {
    const rotators = document.querySelectorAll("[data-update-rotator]");
    if (!rotators.length) return;

    document.documentElement.classList.add("has-update-rotator");
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    rotators.forEach((rotator) => {
      const items = [...rotator.querySelectorAll(".update-rotator-item")];
      if (!items.length) return;

      let activeIndex = 0;
      const show = (nextIndex) => {
        items.forEach((item, index) => {
          const active = index === nextIndex;
          item.classList.toggle("is-active", active);
          item.setAttribute("aria-hidden", String(!active));
        });
      };

      show(activeIndex);
      if (reducedMotion || items.length < 2) return;

      window.setInterval(() => {
        activeIndex = (activeIndex + 1) % items.length;
        show(activeIndex);
      }, 4500);
    });
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start, { once: true });
  else start();
})();
