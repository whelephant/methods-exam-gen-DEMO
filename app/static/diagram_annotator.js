// Diagram annotation canvas — click-drag to draw the bbox, save to backend.
// All bbox coords are NORMALISED [0..1], top-left origin, matching what the
// backend's crop_from_manual_bbox expects.

(function () {
  const ctx = window.__diagramCtx;
  if (!ctx) return;
  const img = document.getElementById("page-img");
  const canvas = document.getElementById("bbox-canvas");
  const readout = document.getElementById("bbox-readout");
  const saveBtn = document.getElementById("save-btn");
  const saveNextBtn = document.getElementById("save-next-btn");
  const clearBtn = document.getElementById("clear-btn");
  const previewWrap = document.getElementById("preview-wrap");
  const cropPreview = document.getElementById("crop-preview");
  const c = canvas.getContext("2d");

  // bbox in normalised coords [x0, y0, x1, y1] | null
  let bbox = ctx.existing || null;
  // drag state
  let dragging = false;
  let dragStart = null; // {x_norm, y_norm}

  function sizeCanvasToImage() {
    canvas.width = img.clientWidth;
    canvas.height = img.clientHeight;
    canvas.style.width = img.clientWidth + "px";
    canvas.style.height = img.clientHeight + "px";
  }

  function draw() {
    c.clearRect(0, 0, canvas.width, canvas.height);
    if (!bbox) {
      readout.textContent = "none";
      saveBtn.disabled = saveNextBtn.disabled = true;
      return;
    }
    const [x0, y0, x1, y1] = bbox;
    const w = canvas.width, h = canvas.height;
    c.strokeStyle = "#e8500e";
    c.lineWidth = 2;
    c.fillStyle = "rgba(232, 80, 14, 0.12)";
    const X = x0 * w, Y = y0 * h, W = (x1 - x0) * w, H = (y1 - y0) * h;
    c.fillRect(X, Y, W, H);
    c.strokeRect(X, Y, W, H);
    readout.textContent =
      `[${x0.toFixed(3)}, ${y0.toFixed(3)}, ${x1.toFixed(3)}, ${y1.toFixed(3)}]`;
    saveBtn.disabled = saveNextBtn.disabled = false;
  }

  function toNorm(ev) {
    const r = canvas.getBoundingClientRect();
    const x = Math.max(0, Math.min(1, (ev.clientX - r.left) / r.width));
    const y = Math.max(0, Math.min(1, (ev.clientY - r.top) / r.height));
    return { x, y };
  }

  canvas.addEventListener("mousedown", (ev) => {
    dragging = true;
    dragStart = toNorm(ev);
    bbox = [dragStart.x, dragStart.y, dragStart.x, dragStart.y];
    draw();
  });
  canvas.addEventListener("mousemove", (ev) => {
    if (!dragging) return;
    const p = toNorm(ev);
    bbox = [
      Math.min(dragStart.x, p.x),
      Math.min(dragStart.y, p.y),
      Math.max(dragStart.x, p.x),
      Math.max(dragStart.y, p.y),
    ];
    draw();
  });
  canvas.addEventListener("mouseup", () => { dragging = false; });
  canvas.addEventListener("mouseleave", () => { dragging = false; });

  clearBtn.addEventListener("click", () => { bbox = null; draw(); });

  const isOption = ctx.mode === "option";

  async function save() {
    if (!bbox) return;
    // Reject zero-area boxes
    if (bbox[2] - bbox[0] < 0.005 || bbox[3] - bbox[1] < 0.005) {
      alert("Box is too small. Draw a real rectangle.");
      return;
    }
    saveBtn.disabled = saveNextBtn.disabled = true;
    const url = isOption ? "/admin/diagrams/save_option" : "/admin/diagrams/save";
    const body = isOption
      ? { question_id: ctx.questionId, option_label: ctx.optionLabel,
          page: ctx.page, bbox_normalised: bbox }
      : { question_id: ctx.questionId, page: ctx.page, bbox_normalised: bbox };
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const t = await r.text();
      alert("save failed: " + t);
      saveBtn.disabled = saveNextBtn.disabled = false;
      return false;
    }
    cropPreview.src = isOption
      ? `/diagrams/${ctx.questionId}-opt-${ctx.optionLabel}.png?v=${Date.now()}`
      : `/diagrams/${ctx.questionId}.png?v=${Date.now()}`;
    previewWrap.style.display = "block";
    saveBtn.disabled = saveNextBtn.disabled = false;
    return true;
  }

  function nextUrl() {
    if (isOption) {
      return ctx.nextOption
        ? `/admin/diagrams/${ctx.sourceId}/q/${ctx.questionId}/opt/${ctx.nextOption}`
        : `/admin/diagrams/${ctx.sourceId}`;
    }
    return ctx.nextId
      ? `/admin/diagrams/${ctx.sourceId}/q/${ctx.nextId}`
      : `/admin/diagrams/${ctx.sourceId}`;
  }
  function prevUrl() {
    if (isOption) {
      return ctx.prevOption
        ? `/admin/diagrams/${ctx.sourceId}/q/${ctx.questionId}/opt/${ctx.prevOption}`
        : null;
    }
    return ctx.prevId ? `/admin/diagrams/${ctx.sourceId}/q/${ctx.prevId}` : null;
  }

  saveBtn.addEventListener("click", save);
  saveNextBtn.addEventListener("click", async () => {
    const ok = await save();
    if (ok) location.href = nextUrl();
  });

  window.addEventListener("keydown", (ev) => {
    if (ev.target.tagName === "INPUT" || ev.target.tagName === "TEXTAREA") return;
    if (ev.key === "Enter") { ev.preventDefault(); saveNextBtn.click(); }
    else if (ev.key === "s") { ev.preventDefault(); saveBtn.click(); }
    else if (ev.key === "Escape") { ev.preventDefault(); clearBtn.click(); }
    else if (ev.key === "n") {
      const u = nextUrl();
      if (u) location.href = u;
    } else if (ev.key === "p") {
      const u = prevUrl();
      if (u) location.href = u;
    }
  });

  // Re-size canvas on image load + window resize.
  if (img.complete) {
    sizeCanvasToImage();
    draw();
  } else {
    img.addEventListener("load", () => { sizeCanvasToImage(); draw(); });
  }
  window.addEventListener("resize", () => { sizeCanvasToImage(); draw(); });
})();
