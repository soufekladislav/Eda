(function () {
  const toggle = document.getElementById('mobileMenuToggle');
  const sidebar = document.getElementById('appSidebar');
  const overlay = document.getElementById('mobileNavOverlay');

  if (!toggle || !sidebar || !overlay) return;

  function setOpen(open) {
    document.body.classList.toggle('mobile-menu-open', open);
    sidebar.classList.toggle('mobile-open', open);
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    overlay.setAttribute('aria-hidden', open ? 'false' : 'true');
  }

  toggle.addEventListener('click', function () {
    setOpen(!document.body.classList.contains('mobile-menu-open'));
  });
  overlay.addEventListener('click', function () { setOpen(false); });
  sidebar.querySelectorAll('nav a').forEach(function (link) {
    link.addEventListener('click', function () { setOpen(false); });
  });
  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') setOpen(false);
  });
  window.addEventListener('resize', function () {
    if (window.innerWidth > 980) setOpen(false);
  });
})();
