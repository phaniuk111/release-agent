import '@testing-library/jest-dom';
import { render, screen } from '@testing-library/react';
import { StatusSummary } from './StatusSummary';

describe('StatusSummary — the release status, readable (not raw JSON)', () => {
  it('says nothing is open, with the DF state and the queue count', () => {
    render(
      <StatusSummary
        status={{
          date_utc: '2026-09-12',
          now_utc: '18:00',
          reason: 'No PRD release open today.',
          prd_release_pr: null,
          prd_charts: [{ helm_chart_name: 'orders-svc', helm_chart_version: '2.0.0' }],
          queued_next: 1,
          df: { error: null, prd_release_pr: null },
        }}
      />,
    );
    expect(
      screen.getByText('CARE: no release open · DF: none open · 1 queued for next release'),
    ).toBeInTheDocument();
    expect(screen.getByText(/No PRD release open today\. · as of 18:00 UTC/)).toBeInTheDocument();
    expect(screen.getByText('orders-svc:2.0.0')).toBeInTheDocument();
  });

  it('shows an open release with its staged charts and a link, and a blocking PR', () => {
    render(
      <StatusSummary
        status={{
          prd_release_pr: {
            number: 42,
            url: 'https://github.example/o/deploy/pull/42',
            charts: [{ helm_chart_name: 'a', helm_chart_version: '1' }],
          },
          pending_to_prod: [{}, {}],
          blocking_pr: { number: 7, url: 'https://github.example/o/deploy/pull/7', head: 'x', base: 'PRD' },
        }}
      />,
    );
    expect(screen.getByText('CARE: PR #42 open · 2 changes staged · ⚠ adds blocked by PR #7')).toBeInTheDocument();
    expect(screen.getByText('open PR #42').closest('a')).toHaveAttribute(
      'href',
      'https://github.example/o/deploy/pull/42',
    );
    expect(screen.getByText(/Adds to the release are blocked/)).toBeInTheDocument();
  });

  it('says so when the status could not be read', () => {
    render(<StatusSummary status={{ error: 'GitHub unreachable' }} />);
    expect(screen.getByText("Couldn't fetch release status: GitHub unreachable")).toBeInTheDocument();
  });
});
