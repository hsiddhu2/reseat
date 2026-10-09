// re:Seat web app. Counts down, polls /api/state every 10 seconds, and sends Approve
// and Dismiss by plan id. No framework, no network beyond this laptop.
(() => {
  "use strict";
  const body = document.body;
  // The page carries the laptop's clock, so countdowns match it (and the demo clock).
  const offset = Number(body.dataset.now || 0) * 1000 - Date.now();
  const now = () => (Date.now() + offset) / 1000;
  let busy = false;

  function tick() {
    document.querySelectorAll("[data-until]").forEach((el) => {
      el.textContent = Math.max(0, Math.round(Number(el.dataset.until) - now())) + " s";
    });
    document.querySelectorAll("[data-leave]").forEach((el) => {
      const mins = Math.ceil((Number(el.dataset.leave) - now()) / 60);
      el.textContent = mins > 0 ? mins + " min" : "now";
      const label = document.querySelector("[data-leave-label]");
      if (label) label.textContent = mins > 0 ? "LEAVE IN" : "LEAVE NOW";
    });
  }

  async function poll() {
    if (busy) return;
    try {
      const r = await fetch("/api/state", { credentials: "same-origin", cache: "no-store" });
      if (!r.ok) return;
      const v = (await r.json()).view;
      if (!v) return;
      const plans = v.cards.map((c) => c.plan_id).join(",");
      const sweep = v.status.last_sweep || "";
      // A new or gone proposal always reloads. A new sweep reloads the read-only views, and the
      // dashboard only while it is scrolled to the top, so reading the week is never interrupted.
      const swept = sweep !== (body.dataset.sweep || "");
      const readOnly = body.classList.contains("v-today") || (body.classList.contains("v-week") && window.scrollY < 40);
      if (plans !== (body.dataset.plans || "") || body.dataset.empty || (swept && readOnly)) {
        location.reload();
        return;
      }
      const text = document.querySelector("[data-field=text]");
      if (text) text.textContent = v.status.text;
      const st = document.querySelector(".status[data-state]");
      if (st) st.dataset.state = v.status.state;
      const until = document.querySelector("[data-until]");
      if (until && v.status.next_at) until.dataset.until = String(v.status.next_at);
    } catch (e) {
      // The laptop may be restarting. The next poll tries again.
    }
  }

  function describe(status, j, approve) {
    if (!approve) return status === 200 ? "Dismissed. Nothing was sent." : "That proposal is already gone.";
    if (status === 404) return "This proposal expired or was already used. Nothing was sent.";
    if (status === 200) {
      const held = (j.held_now || []).join(", ") || "nothing";
      return "Swap " + String(j.state).replace("_", " ") + "." + (j.alert ? " " + j.alert : "") + " Held now: " + held + ".";
    }
    return j.error || "The laptop did not run it. Check the journal before trying again.";
  }

  document.addEventListener("click", async (ev) => {
    const b = ev.target.closest("[data-approve],[data-skip]");
    if (!b || busy) return;
    busy = true;
    const card = b.closest(".card");
    const out = card.querySelector(".result");
    const approve = b.hasAttribute("data-approve");
    const id = approve ? b.dataset.approve : b.dataset.skip;
    card.querySelectorAll("button").forEach((x) => { x.disabled = true; });
    out.textContent = approve ? "Running on the laptop. Do not close this page." : "Dismissing.";
    try {
      const r = await fetch((approve ? "/approve/" : "/skip/") + encodeURIComponent(id), {
        method: "POST", headers: { "X-Reseat": "1" }, credentials: "same-origin",
      });
      out.textContent = describe(r.status, await r.json(), approve);
    } catch (e) {
      out.textContent = "No answer from the laptop. Check the journal before trying again.";
    }
    setTimeout(() => location.reload(), 4000);
  });

  tick();
  setInterval(tick, 1000);
  setInterval(poll, 10000);
  if (body.dataset.empty) setTimeout(() => location.reload(), 3000);
})();
