import { fireEvent, render, screen } from '@testing-library/react';
import { PromoteTab, promotePrompt } from './PromoteTab';

describe('PromoteTab', () => {
  it('sends the promotion the chat would take — CARE to UAT/PRD/PRL1, DF to PRD', () => {
    const onSend = jest.fn();
    render(<PromoteTab onSend={onSend} />);
    fireEvent.click(screen.getAllByText(/Promote to PRD/)[0]);
    expect(onSend).toHaveBeenLastCalledWith('promote the CARE release to prd');
    fireEvent.click(screen.getAllByText(/Promote to PRD/)[1]);
    expect(onSend).toHaveBeenLastCalledWith('promote the DF release to prd');
    expect(screen.getAllByText(/Promote to/)).toHaveLength(4);   // CARE ×3, DF ×1
    expect(promotePrompt('CARE', 'prl1')).toBe('promote the CARE release to prl1');
  });

  it('cannot promote while another turn is running', () => {
    render(<PromoteTab onSend={jest.fn()} busy />);
    for (const b of screen.getAllByRole('button')) expect(b).toBeDisabled();
  });
});
