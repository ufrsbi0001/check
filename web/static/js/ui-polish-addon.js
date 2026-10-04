/* ══════════════════════════════════════════════════════════════
   OBSIDIAN PRO v4.0 — UI POLISH ADD-ON (optional)
   Load AFTER app.js. Adds: card shimmer layers + tab count pop.
   No changes to existing logic required.
   ══════════════════════════════════════════════════════════════ */
(function(){
  // inject shimmer layer into every stat card (purely visual)
  document.querySelectorAll('.stat').forEach(card => {
    if (!card.querySelector('.shimmer')) {
      const s = document.createElement('div');
      s.className = 'shimmer';
      s.setAttribute('aria-hidden', 'true');
      card.prepend(s);
    }
  });

  // pop animation on tab-count change
  const countObserver = new MutationObserver(muts => {
    muts.forEach(m => {
      const el = m.target;
      if (el.classList && el.classList.contains('tab-count')) {
        el.classList.remove('tick');
        void el.offsetWidth;
        el.classList.add('tick');
      }
    });
  });
  document.querySelectorAll('.tab-count').forEach(el => {
    countObserver.observe(el, { childList: true, characterData: true, subtree: true });
  });
})();
