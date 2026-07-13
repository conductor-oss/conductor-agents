/* Catalog filtering: category pills + text search over the agent grid.
   Works with Material's instant navigation (document$) and plain loads. */
(function () {
  function init() {
    var grid = document.getElementById("dc-grid");
    if (!grid) return; // not the catalog page

    var pills = Array.prototype.slice.call(document.querySelectorAll("#dc-pills .dc-pill"));
    var cards = Array.prototype.slice.call(grid.querySelectorAll(".dc-card"));
    var search = document.getElementById("dc-search");
    var noresults = document.getElementById("dc-noresults");
    var cat = "All";

    function apply() {
      var q = (search && search.value ? search.value : "").trim().toLowerCase();
      var shown = 0;
      cards.forEach(function (card) {
        var okCat = cat === "All" || card.getAttribute("data-cat") === cat;
        var hay = ((card.getAttribute("data-text") || "") + " " + card.textContent).toLowerCase();
        var okQ = !q || hay.indexOf(q) !== -1;
        var show = okCat && okQ;
        card.hidden = !show;
        if (show) shown++;
      });
      if (noresults) noresults.style.display = shown === 0 ? "block" : "none";
    }

    pills.forEach(function (pill) {
      pill.addEventListener("click", function () {
        cat = pill.getAttribute("data-cat");
        pills.forEach(function (p) { p.setAttribute("aria-selected", p === pill ? "true" : "false"); });
        apply();
      });
    });
    if (search) search.addEventListener("input", apply);
    apply();
  }

  if (window.document$ && typeof window.document$.subscribe === "function") {
    window.document$.subscribe(init); // Material instant navigation
  } else {
    document.addEventListener("DOMContentLoaded", init);
  }
})();
