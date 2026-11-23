// Remember only a small number of account rows for the next first read.
export const SKELETON_ROWS_KEY = 'agents-router.popover.rows';

/** @param {unknown} value */
export function skeletonRowCount(value) {
  const count = typeof value === 'number' ? value
    : typeof value === 'string' && value.trim() ? Number(value) : NaN;
  return Number.isFinite(count) ? Math.max(1, Math.min(6, Math.trunc(count))) : 3;
}
