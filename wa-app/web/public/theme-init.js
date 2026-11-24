// The stored theme goes on <html> before first paint (no flash). Mirrors theme.ts; "system" sets nothing.
try {
  var t = localStorage.getItem('wa-theme')
  if (t === 'dark' || t === 'light') document.documentElement.dataset.theme = t
  var dark = t === 'dark' || (t !== 'light' && window.matchMedia('(prefers-color-scheme: dark)').matches)
  document.querySelector('meta[name="theme-color"]').setAttribute('content', dark ? '#0b0b0c' : '#f7f5f1')
} catch (e) {}
