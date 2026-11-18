// Sleeper: theme toggle and the Cmd/Ctrl-K quick search. No framework; the pages work without it.
(() => {
  const root = document.documentElement;

  // --- theme ---
  const prefersDark = () =>
    window.matchMedia("(prefers-color-scheme: dark)").matches;
  const current = () =>
    root.dataset.theme || (prefersDark() ? "dark" : "light");
  const label = (btn) => {
    btn.textContent = current() === "dark" ? "Light" : "Dark";
  };
  document.querySelectorAll("[data-theme-toggle]").forEach((btn) => {
    label(btn);
    btn.addEventListener("click", () => {
      const next = current() === "dark" ? "light" : "dark";
      root.dataset.theme = next;
      try {
        localStorage.setItem("theme", next);
      } catch (_e) {
        /* private mode: the choice lasts this page only */
      }
      label(btn);
    });
  });

  // --- quick search ---
  const dlg = document.querySelector("dialog.palette");
  if (!dlg) return;
  const input = dlg.querySelector("input");
  const list = dlg.querySelector("ul");
  let index = null;
  let hits = [];
  let sel = 0;

  const load = async () => {
    if (index) return index;
    const res = await fetch("/api/search", {
      headers: { Accept: "application/json" },
    });
    index = res.ok ? await res.json() : { wants: [], deals: [] };
    return index;
  };

  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };

  const render = () => {
    list.replaceChildren();
    if (!hits.length) {
      list.append(
        el(
          "li",
          "empty",
          input.value
            ? "Nothing matches. Try a card name or a set like FMA."
            : "Type a card name or a set.",
        ),
      );
      return;
    }
    hits.forEach((h, i) => {
      const li = el("li");
      const a = el("a");
      a.href = h.href;
      a.setAttribute("role", "option");
      a.setAttribute("aria-selected", String(i === sel));
      const img = el("img");
      img.alt = "";
      img.loading = "lazy";
      if (h.img) img.src = h.img;
      const name = el("span", "hit-name", h.name);
      const side = el("span", "hit-side", h.side);
      a.append(img, name, side);
      li.append(a);
      list.append(li);
    });
    list
      .querySelector('[aria-selected="true"]')
      ?.scrollIntoView({ block: "nearest" });
  };

  const search = () => {
    const words = input.value.toLowerCase().split(/\s+/).filter(Boolean);
    const all = [
      ...index.deals.map((d) => ({
        ...d,
        hay: `${d.name} ${d.set} ${d.kind} deal`.toLowerCase(),
      })),
      ...index.wants.map((w) => ({
        ...w,
        hay: `${w.name} ${w.set} want`.toLowerCase(),
      })),
    ];
    hits = words.length
      ? all.filter((x) => words.every((w) => x.hay.includes(w))).slice(0, 40)
      : index.deals.slice(0, 12);
    sel = 0;
    render();
  };

  const open = async () => {
    if (dlg.open) return;
    dlg.showModal();
    input.value = "";
    await load();
    search();
  };

  document
    .querySelectorAll("[data-open-search]")
    .forEach((b) => b.addEventListener("click", open));
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      open();
    } else if (
      e.key === "/" &&
      !dlg.open &&
      !/^(input|textarea|select)$/i.test(e.target.tagName)
    ) {
      e.preventDefault();
      open();
    }
  });
  input.addEventListener("input", () => index && search());
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!hits.length) return;
      sel = (sel + (e.key === "ArrowDown" ? 1 : hits.length - 1)) % hits.length;
      render();
    } else if (e.key === "Enter" && hits[sel]) {
      e.preventDefault();
      window.location.href = hits[sel].href;
    }
  });
  dlg.addEventListener("click", (e) => {
    if (e.target === dlg) dlg.close(); // a click on the backdrop
  });
})();

// Card picker and price slider on the listings page (without JS the form has a plain select): a pick
// reloads the page; the slider steps along a fixed price scale and reloads on release.
(() => {
  const form = document.querySelector("form[data-pick]");
  if (!form) return;
  const range = form.querySelector('input[type="range"]');
  const out = form.querySelector("output");
  const scale = range.dataset.scale.split(",").map(Number);
  let cap = Number(range.dataset.cap) || 0; // a cap off the scale (typed in the URL) stays exact
  const show = () => {
    out.textContent = cap ? `$${cap.toLocaleString("en-US")}` : "any price";
  };
  range.addEventListener("input", () => {
    cap = scale[Number(range.value)] || 0;
    show();
  });
  const go = () => {
    form.querySelectorAll('input[name="max"]').forEach((f) => f.remove()); // one kept by Back
    if (cap) {
      const field = document.createElement("input");
      field.type = "hidden";
      field.name = "max";
      field.value = String(cap);
      form.append(field);
    }
    form.submit();
  };
  form.querySelector('select[name="want"]').addEventListener("change", go);
  range.addEventListener("change", go);
  range.hidden = false;
  out.hidden = false;
  show();
})();
