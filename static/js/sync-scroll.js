/**
 * sync-scroll.js
 * On desktop (two-column view): scroll one column and the other follows proportionally.
 * On mobile: tab switcher between RU and EN columns.
 */
(function () {
  'use strict';

  const colRu = document.getElementById('col-ru');
  const colEn = document.getElementById('col-en');

  /* ── Desktop sync-scroll ── */
  if (colRu && colEn && window.innerWidth > 900) {
    let syncing = false;

    function syncScroll(source, target) {
      if (syncing) return;
      syncing = true;
      const sourceMax = source.scrollHeight - source.clientHeight;
      const targetMax = target.scrollHeight - target.clientHeight;
      if (sourceMax > 0) {
        target.scrollTop = (source.scrollTop / sourceMax) * targetMax;
      }
      requestAnimationFrame(() => { syncing = false; });
    }

    /* Columns must have overflow:auto to scroll independently.
       We set it here so the CSS default (visible) works on mobile. */
    [colRu, colEn].forEach(col => {
      col.style.overflowY = 'auto';
      col.style.maxHeight = '85vh';
    });

    colRu.addEventListener('scroll', () => syncScroll(colRu, colEn));
    colEn.addEventListener('scroll', () => syncScroll(colEn, colRu));
  }

  /* ── Mobile tab switcher ── */
  const tabs = document.querySelectorAll('.tab-btn');
  if (tabs.length && colRu && colEn) {
    tabs.forEach(btn => {
      btn.addEventListener('click', () => {
        tabs.forEach(b => b.classList.remove('tab-btn--active'));
        btn.classList.add('tab-btn--active');
        const target = btn.dataset.target;
        if (target === 'col-ru') {
          colRu.classList.remove('tab-hidden');
          colEn.classList.remove('tab-visible');
          colEn.classList.add('tab-hidden');   /* triggers CSS display:none */
          colRu.classList.add('tab-visible');
        } else {
          colEn.classList.remove('tab-hidden');
          colRu.classList.remove('tab-visible');
          colRu.classList.add('tab-hidden');
          colEn.classList.add('tab-visible');
        }
      });
    });
  }
})();
