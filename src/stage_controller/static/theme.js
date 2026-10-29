// Apply before first paint. Storage can be unavailable in privacy modes.
(() => {
  let theme = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  try { theme = localStorage.getItem('stage-theme') || theme; } catch { /* Use OS theme. */ }
  document.documentElement.dataset.theme = theme === 'dark' ? 'dark' : 'light';
})();
