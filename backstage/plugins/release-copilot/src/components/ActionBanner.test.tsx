import { fireEvent, render, screen } from '@testing-library/react';
import { ActionBanner, summaryOf } from './ActionBanner';

describe('ActionBanner', () => {
  it('summarises a preview by its first readable line', () => {
    expect(summaryOf('**Deploy** orders-api 1.2.0 → UAT\n```json\n{}\n```')).toBe('Deploy orders-api 1.2.0 → UAT');
    expect(summaryOf('{\n  "release": "x"\n}\nReply CONFIRM-AB12 to confirm.')).toBe('"release": "x"');
    expect(summaryOf('')).toBe('');
  });

  it('asks to confirm a token, with the preview on demand', () => {
    const onConfirm = jest.fn();
    const onCancel = jest.fn();
    render(
      <ActionBanner
        what="Deploy to CARE UAT"
        token="CONFIRM-AB12CD"
        detail={'Deploy orders-api 1.2.0 to UAT.\n\nReply CONFIRM-AB12CD to confirm.'}
        onConfirm={onConfirm}
        onCancel={onCancel}
      />,
    );
    expect(screen.getByText('Deploy to CARE UAT — ready to confirm')).toBeInTheDocument();
    expect(screen.getByText('CONFIRM-AB12CD')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Show preview'));
    expect(screen.getByText('Hide preview')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Confirm'));
    fireEvent.click(screen.getByText('Cancel'));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it('a yes/no approval says Approve / Reject and shows no token', () => {
    render(
      <ActionBanner what="Promotion" token={null} detail="Promote the release to **PRD**?" onConfirm={jest.fn()} onCancel={jest.fn()} />,
    );
    expect(screen.getByText('Promotion — approval needed')).toBeInTheDocument();
    expect(screen.getByText('Approve')).toBeInTheDocument();
    expect(screen.getByText('Reject')).toBeInTheDocument();
    expect(screen.queryByText(/CONFIRM-/)).toBeNull();
  });

  it('while the preview is built it shows the progress, and no buttons', () => {
    render(
      <ActionBanner
        what="Deploy to CARE UAT"
        token={null}
        detail=""
        building
        steps={['Reading uat/deployment.json', 'Building the preview']}
        onConfirm={jest.fn()}
        onCancel={jest.fn()}
      />,
    );
    expect(screen.getByText('Deploy to CARE UAT — building the preview…')).toBeInTheDocument();
    expect(screen.getByText('Building the preview…')).toBeInTheDocument();
    expect(screen.queryByText('Confirm')).toBeNull();
  });
});
