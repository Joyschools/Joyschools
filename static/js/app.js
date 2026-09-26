document.addEventListener('DOMContentLoaded', () => {
  const reportClientError = (error, source) => {
    try {
      fetch('/api/client-error', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          message: String(error?.message || error || 'Unknown client error'),
          error_type: String(error?.name || 'ClientError'),
          source: source || 'browser',
          route: location.href
        }),
        keepalive: true
      }).catch(() => {});
    } catch (_) {}
  };
  window.addEventListener('error', event => reportClientError(event.error || event.message, 'browser-error'));
  window.addEventListener('unhandledrejection', event => reportClientError(event.reason, 'browser-promise'));

  document.querySelectorAll('.quick-play').forEach(btn => {
    btn.addEventListener('click', e => {
      e.preventDefault(); e.stopPropagation();
      window.location.href = btn.dataset.play;
    });
  });

  const listButtons = document.querySelectorAll('[data-list]');
  const list = JSON.parse(localStorage.getItem('tms_my_list') || '[]');
  listButtons.forEach(btn => {
    const id = String(btn.dataset.id);
    if (list.includes(id)) btn.textContent = '✓ In My List';
    btn.addEventListener('click', () => {
      const current = new Set(JSON.parse(localStorage.getItem('tms_my_list') || '[]'));
      if (current.has(id)) { current.delete(id); btn.textContent = '＋ My List'; }
      else { current.add(id); btn.textContent = '✓ In My List'; }
      localStorage.setItem('tms_my_list', JSON.stringify([...current]));
    });
  });

  const video = document.getElementById('video');
  if (video) {
    const key = 'tms_progress_' + location.pathname + location.search;
    const saved = Number(localStorage.getItem(key) || 0);
    if (saved > 20 && saved < video.duration - 20) video.addEventListener('loadedmetadata', () => { if (saved < video.duration - 20) video.currentTime = saved; }, {once:true});
    video.addEventListener('timeupdate', () => localStorage.setItem(key, String(Math.floor(video.currentTime))));
  }

  const sourceType = document.getElementById('source-type');
  if (sourceType) {
    const urlInput = document.querySelector('input[name="source_url"]');
    const fileInput = document.querySelector('input[name="media"]');
    const toggle = () => {
      const remote = sourceType.value === 'remote';
      urlInput.disabled = !remote; fileInput.disabled = remote;
      urlInput.parentElement.style.opacity = remote ? '1' : '.45';
      fileInput.parentElement.style.opacity = remote ? '.45' : '1';
    };
    sourceType.addEventListener('change', toggle); toggle();
  }
});
