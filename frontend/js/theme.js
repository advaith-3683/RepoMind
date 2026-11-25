// Classic, blocking head script: choose the palette before the stylesheet paints.
(() => {
  const key = "repomind.theme";
  const system = window.matchMedia("(prefers-color-scheme: dark)");
  let choice;
  try { choice = localStorage.getItem(key); } catch { /* System preference still works. */ }
  let explicit = choice === "light" || choice === "dark";
  document.documentElement.dataset.theme = explicit ? choice : system.matches ? "dark" : "light";

  function updateButtons() {
    const dark = document.documentElement.dataset.theme === "dark";
    document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
      button.setAttribute("aria-label", dark ? "Switch to light mode" : "Switch to dark mode");
      button.setAttribute("aria-pressed", String(dark));
    });
  }

  function svgNode(tag, attributes = {}) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.entries(attributes).forEach(([name, value]) => node.setAttribute(name, value));
    return node;
  }

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-theme-toggle]").forEach((button, index) => {
      const id = `theme-cutout-${index}`;
      const svg = svgNode("svg", { viewBox: "0 0 24 24", "aria-hidden": "true", focusable: "false" });
      const defs = svgNode("defs"), mask = svgNode("mask", { id, maskUnits: "userSpaceOnUse", x: 0, y: 0, width: 24, height: 24 });
      // Black and white are mask coverage, not palette colours.
      mask.append(svgNode("rect", { width: 24, height: 24, fill: "white" }),
        svgNode("circle", { class: "theme-cutout", cx: 17, cy: 7, r: 8, fill: "black" }));
      defs.append(mask);
      const rays = svgNode("g", { class: "theme-rays", stroke: "currentColor", "stroke-width": 1.5, "stroke-linecap": "round" });
      for (let angle = 0; angle < 360; angle += 45) {
        rays.append(svgNode("line", { x1: 12, y1: 2, x2: 12, y2: 4, transform: `rotate(${angle} 12 12)` }));
      }
      svg.append(defs, rays, svgNode("circle", { class: "theme-disc", cx: 12, cy: 12, r: 5, fill: "currentColor", mask: `url(#${id})` }));
      button.append(svg);
      button.addEventListener("click", () => {
        const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
        explicit = true;
        document.documentElement.dataset.theme = theme;
        try { localStorage.setItem(key, theme); } catch { /* The current page can still change theme. */ }
        updateButtons();
      });
    });
    updateButtons();
  });
  system.addEventListener("change", (event) => {
    if (explicit) return;
    document.documentElement.dataset.theme = event.matches ? "dark" : "light";
    updateButtons();
  });
})();
