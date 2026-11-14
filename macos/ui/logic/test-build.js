// Settings → General's Test build rows, from the test_build_paths invoke. Pure: no DOM.
export const TEST_BUILD_LABELS = { app_support: 'App support', bin_dir: 'Bin folder', agt_home: 'agt home', config_dir: 'Settings folder' };
const CHIPS = { flag: 'Flag', env: 'Env' };

// Keeps the end of a path, where the folder names differ: …/Agents Router Test/agt-home.
export function truncateStart(text, max = 40) {
  if (text.length <= max) return text;
  const parts = text.split('/');
  let kept = parts.pop();
  if (kept.length + 2 > max) return `…${kept.slice(kept.length - (max - 1))}`;
  while (parts.length && `…/${parts.at(-1)}/${kept}`.length <= max) kept = `${parts.pop()}/${kept}`;
  return `…/${kept}`;
}

export function testBuildRows(rows, max = 40) {
  if (!Array.isArray(rows)) return [];
  return rows.filter(row => TEST_BUILD_LABELS[row.key] && typeof row.path === 'string').map(row => {
    const label = TEST_BUILD_LABELS[row.key];
    const shown = row.display || row.path;
    return { key: row.key, label, path: row.path, value: truncateStart(shown, max), full: shown,
      chip: CHIPS[row.source] || null,
      copyLabel: `Copy ${label}: ${row.path}${row.origin ? ` (${row.origin})` : ''}` };
  });
}

export function testBuildMatches(rows, query) {
  const needle = query.trim().toLowerCase();
  if (!needle || !rows.length) return false;
  return rows.some(row => `test build ${row.label} ${row.path} ${row.full} ${row.chip || ''}`.toLowerCase().includes(needle));
}
