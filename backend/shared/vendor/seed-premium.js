/* ============================================================
   SeedPremium JS — 轻量交互增强(零依赖)
   与 seed-premium.css 配套。后端落盘时可内联进 <script>。
   提供:
     1. 进场渐显: 元素带 class="reveal" 进入视口时加 .in
     2. 磁吸按钮: 元素带 data-magnetic 在鼠标靠近时向光标轻微偏移
   全部尊重 prefers-reduced-motion。
   ============================================================ */
(function () {
  "use strict";
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* —— 1. 进场渐显 —— */
  function initReveal() {
    var els = document.querySelectorAll(".reveal");
    if (!els.length) return;
    if (reduce || !("IntersectionObserver" in window)) {
      els.forEach(function (el) { el.classList.add("in"); });
      return;
    }
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) {
          e.target.classList.add("in");
          io.unobserve(e.target);
        }
      });
    }, { threshold: 0.12, rootMargin: "0px 0px -8% 0px" });
    els.forEach(function (el) { io.observe(el); });
  }

  /* —— 2. 磁吸按钮 —— */
  function initMagnetic() {
    if (reduce) return;
    var els = document.querySelectorAll("[data-magnetic]");
    els.forEach(function (el) {
      var strength = parseFloat(el.getAttribute("data-magnetic")) || 0.25;
      el.addEventListener("mousemove", function (ev) {
        var r = el.getBoundingClientRect();
        var x = (ev.clientX - (r.left + r.width / 2)) * strength;
        var y = (ev.clientY - (r.top + r.height / 2)) * strength;
        el.style.transform = "translate(" + x + "px," + y + "px)";
      });
      el.addEventListener("mouseleave", function () {
        el.style.transform = "";
      });
    });
  }

  function init() {
    initReveal();
    initMagnetic();
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
