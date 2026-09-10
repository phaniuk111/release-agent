import { Fragment } from 'react';
import { Link } from '@material-ui/core';

/**
 * Agent replies carry the links a person has to act on — the PR awaiting
 * review, the GitHub run to watch, the Composer PR to merge — as markdown
 * `[label](url)`, with the occasional bare URL. Rendered as plain text they
 * arrive as `[PR #1644](https://…)`: readable, but the one thing someone needs
 * to click is the one thing they cannot.
 *
 * Builds React elements, never HTML, so reply text cannot inject markup; and
 * only http(s) targets become links, so a `javascript:` URL stays text.
 * Index scanning rather than a regex, per the repo's parsing convention.
 */

const SCHEMES = ['https://', 'http://'];

function isWebUrl(url: string): boolean {
  const lower = url.toLowerCase();
  return SCHEMES.some(s => lower.startsWith(s)) && !url.includes(' ');
}

/** End of a bare URL: first whitespace, or trailing punctuation that is prose. */
function bareUrlEnd(text: string, start: number): number {
  let end = start;
  while (end < text.length && text[end] !== ' ' && text[end] !== '\n' && text[end] !== '\t') {
    end += 1;
  }
  while (end > start && '.,;:!?)]\'"'.includes(text[end - 1])) end -= 1;
  return end;
}

function nextBareUrl(text: string, from: number): number {
  let best = -1;
  for (const s of SCHEMES) {
    const i = text.toLowerCase().indexOf(s, from);
    if (i !== -1 && (best === -1 || i < best)) best = i;
  }
  return best;
}

export type Segment = { text: string; href?: string };

/** Split reply text into plain and linked segments. Exported for tests. */
export function segment(text: string): Segment[] {
  const out: Segment[] = [];
  let plain = '';
  let i = 0;
  const flush = () => {
    if (plain) out.push({ text: plain });
    plain = '';
  };
  while (i < text.length) {
    // [label](url)
    if (text[i] === '[') {
      const close = text.indexOf('](', i + 1);
      const end = close === -1 ? -1 : text.indexOf(')', close + 2);
      if (close !== -1 && end !== -1 && !text.slice(i + 1, close).includes('\n')) {
        const label = text.slice(i + 1, close);
        const url = text.slice(close + 2, end).trim();
        if (label && isWebUrl(url)) {
          flush();
          out.push({ text: label, href: url });
          i = end + 1;
          continue;
        }
      }
    }
    // bare http(s)://…
    const bare = nextBareUrl(text, i);
    if (bare === i) {
      const end = bareUrlEnd(text, i);
      const url = text.slice(i, end);
      if (isWebUrl(url) && end > i + 8) {
        flush();
        out.push({ text: url, href: url });
        i = end;
        continue;
      }
    }
    plain += text[i];
    i += 1;
  }
  flush();
  return out;
}

export function LinkedText({ text }: { text: string }): JSX.Element {
  return (
    <>
      {segment(text || '').map((s, i) =>
        s.href ? (
          <Link key={i} href={s.href} target="_blank" rel="noopener noreferrer">
            {s.text}
          </Link>
        ) : (
          <Fragment key={i}>{s.text}</Fragment>
        ),
      )}
    </>
  );
}
