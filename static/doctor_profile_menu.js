(function () {
  console.log("doctor_profile_menu.js loaded");

  if (window.__doctorProfileMenuInitialized) {
    console.log("doctor_profile_menu.js already initialized, skipping duplicate run.");
    return;
  }
  window.__doctorProfileMenuInitialized = true;

  function init() {
    console.log("doctor_profile_menu.js initializing");
    var btn = document.getElementById("dr-profile-btn");
    var menu = document.getElementById("dr-profile-menu");
    var wrap = btn ? btn.closest(".dr-profile-wrap") : null;
    console.log("dr-profile-btn:", btn);
    console.log("dr-profile-menu:", menu);
    console.log("dr-profile-wrap:", wrap);
    if (!btn || !menu || !wrap) {
      console.error("Missing dropdown elements. btn:", btn, "menu:", menu, "wrap:", wrap);
      return;
    }

    function setOpen(open) {
      console.log("Setting open state to:", open);
      if (open) {
        menu.removeAttribute("hidden");
        btn.setAttribute("aria-expanded", "true");
      } else {
        menu.setAttribute("hidden", "");
        btn.setAttribute("aria-expanded", "false");
      }
    }

    btn.addEventListener("click", function (e) {
      console.log("Button clicked!");
      e.stopPropagation();
      setOpen(menu.hasAttribute("hidden"));
    });

    document.addEventListener("click", function (e) {
      if (menu.hasAttribute("hidden")) return;
      if (wrap.contains(e.target)) return;
      console.log("Click outside dropdown, closing menu");
      setOpen(false);
    });

    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !menu.hasAttribute("hidden")) {
        console.log("Escape pressed, closing menu");
        setOpen(false);
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
