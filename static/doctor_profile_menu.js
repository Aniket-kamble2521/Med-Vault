(function () {
  var btn = document.getElementById("dr-profile-btn");
  var menu = document.getElementById("dr-profile-menu");
  var wrap = btn ? btn.closest(".dr-profile-wrap") : null;
  if (!btn || !menu || !wrap) return;

  function setOpen(open) {
    if (open) {
      menu.removeAttribute("hidden");
      btn.setAttribute("aria-expanded", "true");
    } else {
      menu.setAttribute("hidden", "");
      btn.setAttribute("aria-expanded", "false");
    }
  }

  btn.addEventListener("click", function (e) {
    e.stopPropagation();
    setOpen(menu.hasAttribute("hidden"));
  });

  document.addEventListener("click", function (e) {
    if (menu.hasAttribute("hidden")) return;
    if (wrap.contains(e.target)) return;
    setOpen(false);
  });

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !menu.hasAttribute("hidden")) setOpen(false);
  });
})();
