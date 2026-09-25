/* Audit Shorts front end: library + vertical short-video feed. No build step. */
(() => {
  const $ = (sel, el = document) => el.querySelector(sel);
  const api = async (path, opts) => {
    const res = await fetch(path, opts);
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    return res.status === 204 ? null : res.json();
  };
  const store = {
    get(key, fallback) { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; } },
    set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode */ } },
  };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmtTime = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
  let toastTimer;
  const toast = (msg) => {
    const t = $("#toast"); t.textContent = msg; t.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), 2200);
  };

  // ------------------------------------------------------------ routing
  let pollTimer = null;
  function route() {
    clearTimeout(pollTimer);
    const m = location.hash.match(/^#\/watch\/(\w+)(?:\/(\d+))?/);
    if (m) openFeed(m[1], Number(m[2] || 0));
    else showLibrary();
  }
  window.addEventListener("hashchange", route);

  // ------------------------------------------------------------ library
  async function showLibrary() {
    feedState.teardown();
    $("#player").hidden = true;
    $("#library").hidden = false;
    document.title = "Audit Shorts";
    let reports = [];
    try { reports = await api("/api/reports"); } catch (e) { toast(e.message); }
    renderReports(reports);
    if (reports.some((r) => r.status === "queued" || r.status === "processing")) pollTimer = setTimeout(showLibrary, 2000);
  }

  function renderReports(reports) {
    const box = $("#reports");
    $("#empty").hidden = reports.length > 0;
    box.innerHTML = reports.map((r) => {
      const busy = r.status === "queued" || r.status === "processing";
      const acks = store.get(`ack:${r.id}`, []);
      const watchable = r.status === "ready" || (busy && (r.progress || 0) > 12);
      return `<article class="report">
        <a class="thumb" ${watchable ? `href="#/watch/${r.id}"` : ""} data-poster="${r.id}">${watchable ? '<span class="play">▶</span>' : ""}</a>
        <div>
          <h3>${esc(r.title || r.filename)}</h3>
          <div class="muted small">${esc([r.reference, r.date].filter(Boolean).join(" · ") || r.filename)}</div>
          <div class="pills">
            ${r.finding_count ? `<span class="pill">${r.finding_count} findings</span>` : ""}
            ${r.status === "ready" ? `<span class="pill">${acks.length}/${(r.finding_count || 0) + 2} acknowledged</span>` : ""}
            ${r.status === "error" ? `<span class="pill" style="color:var(--high)">Failed</span>` : ""}
          </div>
          ${busy ? `<div class="progress"><div style="width:${r.progress || 2}%"></div></div><div class="muted small" style="margin-top:6px">${esc(r.stage || "Processing")}… ${r.progress || 0}%</div>` : ""}
        </div>
        <div class="actions">
          ${watchable ? `<a class="btn primary" style="margin:0" href="#/watch/${r.id}">▶ Watch</a>` : ""}
          ${r.status === "ready" ? `<button class="btn" data-share="${r.id}">Share link</button>` : ""}
          <button class="btn ghost" data-delete="${r.id}">Delete</button>
        </div>
      </article>`;
    }).join("");
    // Posters come from the full report (first short); load lazily.
    box.querySelectorAll("[data-poster]").forEach(async (el) => {
      try {
        const rep = await api(`/api/reports/${el.dataset.poster}`);
        const poster = rep.shorts?.find((s) => s.poster)?.poster;
        if (poster) el.style.backgroundImage = `url(${poster})`;
      } catch { /* ignore */ }
    });
  }

  $("#reports").addEventListener("click", async (e) => {
    const del = e.target.closest("[data-delete]");
    const share = e.target.closest("[data-share]");
    if (del && confirm("Delete this report and its videos?")) {
      await api(`/api/reports/${del.dataset.delete}`, { method: "DELETE" });
      showLibrary();
    }
    if (share) shareUrl(`${location.origin}/#/watch/${share.dataset.share}`, "Audit findings in short videos");
  });

  // upload
  const dz = $("#dropzone");
  const fileInput = $("#file");
  dz.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); } });
  ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
  dz.addEventListener("drop", (e) => e.dataTransfer.files[0] && upload(e.dataTransfer.files[0]));
  fileInput.addEventListener("change", () => fileInput.files[0] && upload(fileInput.files[0]));

  async function upload(file) {
    const err = $("#upload-error");
    err.hidden = true;
    if (!/\.pdf$/i.test(file.name) && file.type !== "application/pdf") {
      err.textContent = "Please choose a PDF file."; err.hidden = false; return;
    }
    const body = new FormData();
    body.append("file", file);
    $(".dz-title", dz).textContent = `Uploading ${file.name}…`;
    try {
      await api("/api/reports", { method: "POST", body });
      toast("Uploaded. Creating your shorts…");
    } catch (e) {
      err.textContent = e.message; err.hidden = false;
    } finally {
      $(".dz-title", dz).textContent = "Drop an audit report here";
      fileInput.value = "";
      showLibrary();
    }
  }

  // ------------------------------------------------------------ feed
  const feed = $("#feed");
  const feedState = {
    id: null, report: null, slides: [], current: 0, muted: true, autoNext: store.get("autoNext", true), observer: null,
    teardown() {
      this.observer?.disconnect();
      this.slides.forEach((s) => s.video?.pause());
      feed.innerHTML = "";
      this.slides = []; this.id = null; this.report = null;
    },
  };

  async function openFeed(id, start) {
    $("#library").hidden = true;
    $("#player").hidden = false;
    if (feedState.id !== id) {
      feedState.teardown();
      feedState.id = id;
      let report;
      try { report = await api(`/api/reports/${id}`); } catch (e) { toast("Report not found"); location.hash = ""; return; }
      feedState.report = report;
      buildFeed(report);
      requestAnimationFrame(() => scrollToSlide(start, false));
    }
    pollFeed();
  }

  function buildFeed(report) {
    document.title = `${report.title || "Audit"} · Audit Shorts`;
    $("#feed-title").textContent = report.title || report.filename;
    const tpl = $("#slide-tpl");
    const acks = new Set(store.get(`ack:${report.id}`, []));
    report.shorts.forEach((short, i) => {
      const node = tpl.content.firstElementChild.cloneNode(true);
      const video = $("video", node);
      const slide = { node, video, short, index: i };
      $(".risk", node).textContent = short.kind === "finding" ? `${short.risk_level.toUpperCase()} RISK` : short.kind === "overview" ? "OVERVIEW" : "ACTIONS";
      $(".risk", node).classList.add(short.risk_level);
      $(".title", node).textContent = short.title;
      $(".sub", node).textContent = short.kind === "finding"
        ? `Finding ${short.finding_number} of ${report.finding_count} · ${short.scenes.at(-1).headline}` : short.kind === "overview"
          ? `${report.finding_count} findings · swipe up` : "What happens next";
      const ack = $(".ack", node);
      ack.setAttribute("aria-pressed", acks.has(short.id));
      ack.addEventListener("click", () => toggleAck(slide));
      $(".details", node).addEventListener("click", () => openDetails(slide));
      $(".share", node).addEventListener("click", () => shareUrl(`${location.origin}/#/watch/${report.id}/${i}`, short.title));
      $(".mute", node).addEventListener("click", () => setMuted(!feedState.muted));
      video.addEventListener("click", () => togglePlay(slide));
      video.addEventListener("timeupdate", () => {
        if (video.duration) $(".scrub-fill", node).style.width = `${(100 * video.currentTime) / video.duration}%`;
      });
      video.addEventListener("ended", () => {
        markWatched(slide);
        if (feedState.autoNext) scrollToSlide(i + 1);
        else { video.currentTime = 0; video.play().catch(() => {}); }
      });
      video.addEventListener("error", () => {
        if (!video.src || $(".video-error", node)) return;
        const box = document.createElement("div");
        box.className = "video-error";
        box.innerHTML = `<div>This browser can't play the video here.</div><a class="btn primary" style="margin:0" href="${esc(short.video || "")}" download>Download MP4</a>`;
        $(".frame", node).insertBefore(box, $(".overlay", node));
      });
      $(".scrub", node).addEventListener("click", (e) => {
        const r = e.currentTarget.getBoundingClientRect();
        if (video.duration) video.currentTime = ((e.clientX - r.left) / r.width) * video.duration;
      });
      node.dataset.index = i;
      feed.appendChild(node);
      feedState.slides.push(slide);
      applyShort(slide, short);
    });
    const end = document.createElement("section");
    end.className = "slide end";
    end.dataset.index = report.shorts.length;
    feed.appendChild(end);
    feedState.endNode = end;
    renderEndCard();

    feedState.observer = new IntersectionObserver((entries) => {
      entries.forEach((en) => { if (en.isIntersecting && en.intersectionRatio > 0.6) activate(Number(en.target.dataset.index)); });
    }, { root: feed, threshold: [0.6] });
    feed.querySelectorAll(".slide").forEach((n) => feedState.observer.observe(n));
    updateMuteUi();
    $("#autoplay").classList.toggle("on", feedState.autoNext);
    $("#autoplay").setAttribute("aria-pressed", feedState.autoNext);
  }

  function applyShort(slide, short) {
    slide.short = short;
    const ready = short.status === "ready" && short.video;
    slide.node.classList.toggle("ready", !!ready);
    const dl = $(".download", slide.node);
    dl.classList.toggle("disabled", !ready);
    if (ready && !slide.video.src) {
      slide.video.poster = short.poster;
      slide.video.src = short.video;
      dl.href = short.video;
      dl.setAttribute("download", `${short.id}.mp4`);
      if (slide.index === feedState.current) playCurrent();
    }
  }

  function pollFeed() {
    const r = feedState.report;
    if (!r || r.status === "ready" || r.status === "error") {
      if (r?.status === "error") toast(`Processing failed: ${r.error}`);
      return;
    }
    pollTimer = setTimeout(async () => {
      if (feedState.id !== r.id) return;
      try {
        const fresh = await api(`/api/reports/${r.id}`);
        feedState.report = fresh;
        fresh.shorts.forEach((s, i) => feedState.slides[i] && applyShort(feedState.slides[i], s));
      } catch { /* retry next tick */ }
      pollFeed();
    }, 2000);
  }

  function activate(index) {
    if (index === feedState.current && feedState.activated) return;
    feedState.activated = true;
    feedState.slides.forEach((s, i) => { if (i !== index) { s.video.pause(); s.node.classList.remove("paused"); } });
    feedState.current = index;
    const total = feedState.slides.length;
    $("#feed-count").textContent = index < total ? `${index + 1}/${total}` : "";
    history.replaceState(null, "", `#/watch/${feedState.id}/${index}`);
    const slide = feedState.slides[index];
    document.querySelector(".player").style.setProperty("--backdrop", slide?.short.poster ? `url(${slide.short.poster})` : "none");
    if (!slide) { renderEndCard(); return; }
    playCurrent();
  }

  function playCurrent() {
    const slide = feedState.slides[feedState.current];
    if (!slide || !slide.video.src) return;
    slide.video.muted = feedState.muted;
    slide.video.currentTime = slide.video.ended ? 0 : slide.video.currentTime;
    slide.video.play().then(() => slide.node.classList.remove("paused")).catch(() => {
      // Autoplay with sound blocked: fall back to muted autoplay, like Reels/Shorts do.
      if (!slide.video.muted) { setMuted(true); slide.video.play().catch(() => slide.node.classList.add("paused")); }
      else slide.node.classList.add("paused");
    });
  }

  function togglePlay(slide) {
    if (feedState.muted && !slide.video.paused) { setMuted(false); return; } // first tap turns sound on
    playPause(slide);
  }

  function playPause(slide) {
    if (slide.video.paused) slide.video.play().then(() => slide.node.classList.remove("paused")).catch(() => {});
    else { slide.video.pause(); slide.node.classList.add("paused"); }
  }

  function setMuted(m) {
    feedState.muted = m;
    feedState.slides.forEach((s) => (s.video.muted = m));
    updateMuteUi();
  }
  function updateMuteUi() {
    $("#unmute").hidden = !feedState.muted;
    feedState.slides.forEach((s) => ($(".mute .ic", s.node).textContent = feedState.muted ? "🔇" : "🔊"));
  }
  $("#unmute").addEventListener("click", () => { setMuted(false); playCurrent(); });

  function scrollToSlide(i, smooth = true) {
    const nodes = feed.querySelectorAll(".slide");
    const target = nodes[Math.max(0, Math.min(i, nodes.length - 1))];
    target?.scrollIntoView({ behavior: smooth ? "smooth" : "instant", block: "start" });
  }

  function toggleAck(slide) {
    const key = `ack:${feedState.id}`;
    const acks = new Set(store.get(key, []));
    const on = !acks.has(slide.short.id);
    on ? acks.add(slide.short.id) : acks.delete(slide.short.id);
    store.set(key, [...acks]);
    $(".ack", slide.node).setAttribute("aria-pressed", on);
    if (on) toast("Marked as understood");
    renderEndCard();
  }

  function markWatched(slide) {
    const key = `watched:${feedState.id}`;
    const w = new Set(store.get(key, []));
    w.add(slide.short.id);
    store.set(key, [...w]);
  }

  function renderEndCard() {
    const end = feedState.endNode;
    if (!end || !feedState.report) return;
    const total = feedState.slides.length;
    const acks = store.get(`ack:${feedState.id}`, []).length;
    const watched = store.get(`watched:${feedState.id}`, []).length;
    end.innerHTML = `<div class="endcard">
      <div class="big">✅</div>
      <h2>You're all caught up</h2>
      <p class="muted">You watched ${watched} of ${total} shorts and marked ${acks} as understood.</p>
      <div class="btns">
        <button class="btn" data-restart>↺ Watch again</button>
        <a class="btn primary" style="margin:0" href="#/">All reports</a>
      </div>
    </div>`;
    $("[data-restart]", end).addEventListener("click", () => scrollToSlide(0));
  }

  function openDetails(slide) {
    const s = slide.short;
    $("#sheet-title").textContent = s.title;
    const chapters = (s.scene_starts || []).map((t, i) => {
      const sc = s.scenes[i];
      return `<button class="chapter" data-t="${t}"><span class="t">${fmtTime(t)}</span><span><b>${esc(sc.label.charAt(0) + sc.label.slice(1).toLowerCase())}</b><br><span class="muted">${esc(sc.headline)}</span></span></button>`;
    }).join("");
    const sources = Object.entries(s.source || {}).filter(([, v]) => v)
      .map(([k, v]) => `<h4>${esc(k)}</h4><p>${esc(v)}</p>`).join("");
    $("#sheet-body").innerHTML = `${chapters ? `<h4>Jump to</h4><div class="chapters">${chapters}</div>` : ""}
      ${sources ? `<h4 style="margin-top:24px;color:var(--text)">From the report</h4>${sources}` : ""}`;
    $("#sheet-body").querySelectorAll(".chapter").forEach((b) => b.addEventListener("click", () => {
      slide.video.currentTime = Number(b.dataset.t) + 0.05;
      slide.video.play().catch(() => {});
      slide.node.classList.remove("paused");
      closeSheet();
    }));
    slide.video.pause();
    $("#sheet").hidden = false;
  }
  function closeSheet() {
    $("#sheet").hidden = true;
  }
  $("#sheet").addEventListener("click", (e) => { if (e.target.closest("[data-close]")) { closeSheet(); playCurrent(); } });

  async function shareUrl(url, title) {
    if (navigator.share) {
      try { await navigator.share({ title, url }); return; } catch { /* cancelled */ }
    }
    try { await navigator.clipboard.writeText(url); toast("Link copied"); } catch { prompt("Copy this link:", url); }
  }

  $("#back").addEventListener("click", () => (location.hash = "#/"));
  $("#prev").addEventListener("click", () => scrollToSlide(feedState.current - 1));
  $("#next").addEventListener("click", () => scrollToSlide(feedState.current + 1));
  $("#autoplay").addEventListener("click", (e) => {
    feedState.autoNext = !feedState.autoNext;
    store.set("autoNext", feedState.autoNext);
    e.currentTarget.classList.toggle("on", feedState.autoNext);
    e.currentTarget.setAttribute("aria-pressed", feedState.autoNext);
    toast(feedState.autoNext ? "Next short plays automatically" : "Shorts loop until you swipe");
  });
  document.addEventListener("keydown", (e) => {
    if ($("#player").hidden || e.target.closest("input")) return;
    if (e.key === "Escape" && !$("#sheet").hidden) { closeSheet(); return; }
    const slide = feedState.slides[feedState.current];
    if (["ArrowDown", "j", "PageDown"].includes(e.key)) { e.preventDefault(); scrollToSlide(feedState.current + 1); }
    else if (["ArrowUp", "k", "PageUp"].includes(e.key)) { e.preventDefault(); scrollToSlide(feedState.current - 1); }
    else if (e.key === " " && slide) { e.preventDefault(); playPause(slide); }
    else if (e.key === "m") setMuted(!feedState.muted);
    else if (e.key === "ArrowRight" && slide) slide.video.currentTime += 5;
    else if (e.key === "ArrowLeft" && slide) slide.video.currentTime -= 5;
  });

  route();
})();
