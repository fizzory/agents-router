// Every hit target is at least 24 × 24 (UX spec §1 and §6). jsdom has no layout
// and no pseudo-elements, so this reads the stylesheets themselves: each
// control's box, border and ::after inset give its hit size, the way WebKit
// sizes it (an absolutely positioned ::after insets from the padding box).
import { describe, expect, it } from 'vitest';
import tokensCss from '../../tokens.css?raw';
import settingsCss from '../settings/settings.css?raw';
import controlsCss from './controls.css?raw';
import sharedCss from './shared.css?raw';

type Declarations = Map<string, string>;

/** Top-level rules only: @media blocks hold no geometry the checks read. */
function rules(css: string): Map<string, Declarations> {
  const text = css.replace(/\/\*[\s\S]*?\*\//g, '');
  const found = new Map<string, Declarations>();
  let depth = 0;
  let start = 0;
  let selector = '';
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (char === '{') {
      if (depth === 0) selector = text.slice(start, index).trim();
      depth += 1;
      start = index + 1;
    } else if (char === '}') {
      depth -= 1;
      if (depth === 0 && !selector.startsWith('@')) {
        const body = text.slice(start, index);
        for (const name of selector.split(',').map((part) => part.trim())) {
          const declarations = found.get(name) ?? new Map<string, string>();
          for (const declaration of body.split(';')) {
            const at = declaration.indexOf(':');
            if (at > 0) declarations.set(declaration.slice(0, at).trim(), declaration.slice(at + 1).trim());
          }
          found.set(name, declarations);
        }
      }
      start = index + 1;
    }
  }
  return found;
}

const sheet = rules([tokensCss, sharedCss, controlsCss, settingsCss].join('\n'));
const root = sheet.get(':root')!;
const px = (value: string | undefined): number => {
  if (value === undefined) return 0;
  const token = /^var\((--[\w-]+)\)$/.exec(value);
  if (token) return px(root.get(token[1]!));
  if (value === '0') return 0;
  const match = /^(-?\d+(?:\.\d+)?)px$/.exec(value);
  if (!match) throw new Error(`not a length: ${value}`);
  return Number(match[1]);
};
/** Four sides from a 1–4 value shorthand: top, right, bottom, left. */
const sides = (value: string | undefined): [number, number, number, number] => {
  const parts = (value ?? '0').split(/\s+/).map(px);
  const [top = 0, right = top, bottom = top, left = right] = parts;
  return [top, right, bottom, left];
};
const declarations = (...selectors: string[]): Declarations => {
  const merged = new Map<string, string>();
  for (const selector of selectors) {
    const found = sheet.get(selector);
    if (!found) throw new Error(`no rule for ${selector}`);
    for (const [name, value] of found) merged.set(name, value);
  }
  return merged;
};
const borders = (style: Declarations): [number, number, number, number] => {
  const shorthand = style.get('border');
  const width = shorthand === undefined ? 0 : shorthand === '0' ? 0 : px(shorthand.split(/\s+/)[0]);
  const all: [number, number, number, number] = [width, width, width, width];
  if (style.has('border-width')) return sides(style.get('border-width'));
  if (style.has('border-right-width')) all[1] = px(style.get('border-right-width'));
  if (style.has('border-bottom-width')) all[2] = px(style.get('border-bottom-width'));
  return all;
};
/** A fixed or minimum length; NaN for a relative one (100%), which the checks don't read. */
const size = (style: Declarations, axis: 'height' | 'width') => {
  const value = style.get(axis) ?? style.get(`min-${axis}`);
  return value !== undefined && /%|calc/.test(value) ? Number.NaN : px(value);
};

/** Hit size of a control whose ::after (absolute, inset from the padding box) extends it. */
function hit(selectors: string[], after: string[]) {
  const style = declarations(...selectors);
  const [top, right, bottom, left] = borders(style);
  const [insetTop, insetRight, insetBottom, insetLeft] = sides(declarations(...after).get('inset'));
  expect(declarations(...selectors).get('position')).toBe('relative');
  return {
    height: size(style, 'height') - top - bottom - insetTop - insetBottom,
    width: size(style, 'width') - left - right - insetLeft - insetRight,
  };
}

describe('hit targets are at least 24 × 24', () => {
  it('22px push, default and popup buttons, and 20px inline buttons, tags and badges', () => {
    for (const [control, minHeight] of [['.button-push', 22], ['.button-default', 22], ['.popup-button', 22], ['.button-inline', 20],
      ['.source-tag', 20], ['.account-badge', 20]] as const) {
      const style = declarations(control);
      expect(size(style, 'height'), `${control} looks`).toBe(minHeight);
      const { height } = hit([control], [`${control}::after`]);
      expect(height, `${control} hit height`).toBeGreaterThanOrEqual(24);
      expect(height, `${control} stays inside a 28px row`).toBeLessThanOrEqual(28);
    }
    // Their widths come from text; the minimum keeps a 24px target.
    for (const control of ['.button-push', '.popup-button', '.source-tag']) {
      expect(px(declarations(control).get('min-width')), `${control} min-width`).toBeGreaterThanOrEqual(24);
    }
  });

  it('switches, regular and small', () => {
    expect(hit(['.switch'], ['.switch::after'])).toEqual({ height: 25, width: 28 });
    expect(hit(['.switch', '.switch.is-small'], ['.switch::after', '.switch.is-small::after'])).toEqual({ height: 25, width: 24 });
  });

  it('fields look 22px tall in a 24px box that keeps the 22px layout', () => {
    for (const field of ['.number-field input', '.text-field']) {
      const style = declarations(field);
      expect(px(style.get('height')), field).toBe(24);
      expect(style.get('margin'), field).toBe('-1px');
      expect(style.get('border'), field).toBe('1px solid transparent');
    }
    expect(px(declarations('.number-field input').get('width'))).toBe(58);
  });

  it('each stepper half is 24 × 24 and stays clear of the field', () => {
    const first = hit(['.stepper button', '.stepper button:first-child'], ['.stepper button::after', '.stepper button:first-child::after']);
    const last = hit(['.stepper button', '.stepper button:last-child'], ['.stepper button::after', '.stepper button:last-child::after']);
    expect(first).toEqual({ height: 24, width: 24 });
    expect(last).toEqual({ height: 24, width: 24 });
    // The − half reaches 2px past its edge, the field's box 1px past its own: the gap is wider than both.
    const reach = -sides(declarations('.stepper button:first-child::after').get('inset'))[3] - borders(declarations('.stepper button'))[3];
    const overhang = -px(declarations('.number-field input').get('margin'));
    expect(px(declarations('.number-field').get('gap'))).toBeGreaterThan(reach + overhang);
  });

  it('icon, text, menu, chip, nav and disclosure targets are 24px or more by size', () => {
    expect(size(declarations('.button-icon'), 'height')).toBe(24);
    expect(size(declarations('.button-icon'), 'width')).toBe(24);
    expect(size(declarations('.button-text'), 'height')).toBe(24);
    expect(size(declarations('.menu-item'), 'height')).toBe(24);
    expect(size(declarations('.provider-chip'), 'height')).toBe(24);
    expect(size(declarations('.nav-item'), 'height')).toBe(28);
    expect(size(declarations('.disclosure-summary'), 'height')).toBe(28);
    expect(size(declarations('.search-field'), 'height')).toBe(24);
  });
});
