(() => {
  'use strict';
  const entrance = document.getElementById('entrance');
  const motionPreference = window.matchMedia('(prefers-reduced-motion: reduce)');
  if (entrance) {
    let frame = 0;
    let inView = true;
    const updateEntrance = () => {
      frame = 0;
      const rect = entrance.getBoundingClientRect();
      const progress = motionPreference.matches ? 0 : Math.max(0, Math.min(1, -rect.top / rect.height));
      entrance.style.setProperty('--entry-progress', progress.toFixed(4));
    };
    const scheduleUpdate = () => {
      if (inView && !frame) frame = requestAnimationFrame(updateEntrance);
    };
    if ('IntersectionObserver' in window) {
      new IntersectionObserver(([entry]) => {
        inView = entry.isIntersecting;
        if (inView) scheduleUpdate();
      }).observe(entrance);
    }
    window.addEventListener('scroll', scheduleUpdate, { passive: true });
    window.addEventListener('resize', scheduleUpdate, { passive: true });
    motionPreference.addEventListener('change', updateEntrance);
    updateEntrance();
  }
  const button = document.getElementById('copy-citation');
  const citation = document.getElementById('citation');
  const status = document.getElementById('copy-status');
  if (!button || !citation || !status) return;
  button.addEventListener('click', async () => {
    try {
      if (!navigator.clipboard || !window.isSecureContext) throw new Error('Clipboard unavailable');
      await navigator.clipboard.writeText(citation.textContent.trim() + '\n');
      status.textContent = 'Citation copied.';
    } catch {
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(citation);
      selection.removeAllRanges();
      selection.addRange(range);
      status.textContent = 'Citation selected. Copy it, or use the download link below.';
    }
  });
})();
