(() => {
  'use strict';
  const video = document.querySelector('.case-video');
  const source = video?.querySelector('source');
  if (!source || !window.fetch || !window.URL?.createObjectURL) return;

  let preparing = false;
  let objectURL = null;

  // This host serves the whole MP4 even for byte-range requests. Loading the
  // complete, small film as a Blob gives native controls a seekable source.
  // Keep the original <source> as a no-JavaScript / network-error fallback.
  const prepare = async () => {
    if (preparing || objectURL) return;
    preparing = true;
    try {
      const response = await fetch(source.src);
      if (!response.ok) throw new Error('Video download failed');
      const blob = await response.blob();
      if (!blob.size || !blob.type.startsWith('video/')) {
        throw new Error('Invalid video response');
      }

      objectURL = URL.createObjectURL(blob);
      const position = video.currentTime;
      const resumePlayback = !video.paused;
      const playbackRate = video.playbackRate;
      const restorePlayback = () => {
        video.playbackRate = playbackRate;
        if (position > 0) video.currentTime = Math.min(position, video.duration);
        if (resumePlayback) video.play().catch(() => {});
      };
      const restoreSource = () => {
        video.removeEventListener('loadedmetadata', restorePlayback);
        URL.revokeObjectURL(objectURL);
        objectURL = null;
        video.removeAttribute('src');
        video.load();
      };
      video.addEventListener('loadedmetadata', restorePlayback, { once: true });
      video.addEventListener('error', restoreSource, { once: true });
      video.src = objectURL;
      video.preload = 'auto';
      video.load();
    } catch {
      // The direct MP4 remains playable; another interaction can retry.
    } finally {
      preparing = false;
    }
  };

  if ('IntersectionObserver' in window) {
    const observer = new IntersectionObserver(([entry]) => {
      if (!entry.isIntersecting) return;
      observer.disconnect();
      prepare();
    }, { rootMargin: '1200px 0px' });
    observer.observe(video);
  } else {
    prepare();
  }
  video.addEventListener('pointerdown', prepare, { passive: true });
  video.addEventListener('focusin', prepare);
  video.addEventListener('play', prepare);
  window.addEventListener('pagehide', event => {
    // Keep the URL alive when navigating back through the browser's page cache.
    if (!event.persisted && objectURL) URL.revokeObjectURL(objectURL);
  });
})();
