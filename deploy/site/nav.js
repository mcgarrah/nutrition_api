// Shared top-nav for the tool pages (barcode lookup, search, the data
// explorer). Not used by the home page (its own hero/grid navigation is a
// different shape) or status.html (deliberately kept separate -- different
// audience, different deploy path -- see PLAN.md item 11).
//
// One list, one place to add/rename/reorder a destination, instead of each
// page hand-writing its own `<span class="nav">` with its own subset, order,
// and label -- which is what item 11 found and fixed (`/gpc` alone had three
// different labels across the pages that linked it).
//
// Served as a plain static file (deploy/site/nav.js, Caddy's file_server),
// not through the FastAPI backend -- it's genuinely static and every page
// that includes it does so via a root-relative <script src="/nav.js">, so it
// resolves the same regardless of which layer serves the HTML around it.
const NAV_LINKS = [
  { href: "/lookup", label: "Barcode lookup" },
  { href: "/search", label: "Search" },
  { href: "/data", label: "Explore the data" },
  { href: "/status", label: "Status" },
  { href: "/docs", label: "/docs" },
];

// current: the page's own path (e.g. "/lookup"), omitted from its own nav.
function renderNav(current) {
  const el = document.getElementById("nav");
  if (!el) return;
  const links = [`<a href="/">← Home</a>`].concat(
    NAV_LINKS.filter(l => l.href !== current).map(l => `<a href="${l.href}">${l.label}</a>`)
  );
  el.innerHTML = links.join(" · ");
}
