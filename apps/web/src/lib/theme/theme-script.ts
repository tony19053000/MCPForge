/**
 * Applied before paint so the theme never flashes (04_FRONTEND_SPEC.md §1).
 * Runs inline in <head>; keep it small, dependency-free and failure-tolerant.
 */
export const THEME_STORAGE_KEY = "mcpforge-theme";

export const themeInitScript = `
(function(){
  try {
    var stored = localStorage.getItem('${THEME_STORAGE_KEY}');
    var prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    var theme = stored === 'light' || stored === 'dark' ? stored : (prefersDark ? 'dark' : 'light');
    document.documentElement.setAttribute('data-theme', theme);
  } catch (e) {
    document.documentElement.setAttribute('data-theme', 'dark');
  }
})();
`.trim();
