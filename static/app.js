async function generateEmergencyQr() {
  const btn = document.getElementById("btn-generate-qr");
  const box = document.getElementById("qr-result");
  const img = document.getElementById("qr-image");
  const link = document.getElementById("qr-link");
  const expiry = document.getElementById("qr-expiry");

  if (!btn || !box || !img || !link || !expiry) return;

  const htmlIdle = btn.innerHTML;
  btn.disabled = true;
  btn.textContent = "Generating…";
  try {
    const res = await fetch("/generate_emergency_qr", { method: "POST" });
    if (!res.ok) throw new Error("Failed to generate QR");
    const data = await res.json();

    img.src = data.qr_image_url + "?t=" + encodeURIComponent(Date.now());
    link.href = data.emergency_url;
    link.textContent = data.emergency_url;
    expiry.textContent = data.expires_at;
    box.classList.remove("hidden");
  } catch (e) {
    alert("Could not generate emergency QR. Please try again.");
  } finally {
    btn.disabled = false;
    btn.innerHTML = htmlIdle;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const btn = document.getElementById("btn-generate-qr");
  if (btn) btn.addEventListener("click", generateEmergencyQr);

  function setTab(tab) {
    document.querySelectorAll(".tab-btn, .tab-mini").forEach((el) => {
      el.classList.toggle("is-active", el.dataset.tab === tab);
    });
    document.querySelectorAll(".tab-pane, .dr-tab-pane").forEach((el) => {
      el.classList.toggle("is-active", el.dataset.pane === tab);
    });
  }

  document.querySelectorAll(".tab-btn, .tab-mini").forEach((el) => {
    el.addEventListener("click", () => setTab(el.dataset.tab));
  });

  const hash = (location.hash || "").replace(/^#/, "");
  if (hash === "medical") {
    setTab("meds");
  }

  const steps = Array.from(document.querySelectorAll(".onboard-step"));
  if (steps.length > 0) {
    let current = 0;
    const show = (idx) => {
      steps.forEach((s, i) => s.classList.toggle("is-active", i === idx));
      current = idx;
    };
    document.querySelectorAll(".onboard-next").forEach((el) =>
      el.addEventListener("click", () => show(Math.min(current + 1, steps.length - 1)))
    );
    document.querySelectorAll(".onboard-prev").forEach((el) =>
      el.addEventListener("click", () => show(Math.max(current - 1, 0)))
    );
    show(0);
  }
});

