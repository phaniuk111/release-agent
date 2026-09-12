import '@testing-library/jest-dom';
import { fireEvent, render, screen } from '@testing-library/react';
import { TurnResult } from './TurnResult';

const noop = () => {};

describe('TurnResult — a form tab shows the reply to its own submission', () => {
  it('shows nothing before anything was sent', () => {
    const { container } = render(
      <TurnResult text="" streaming={false} pendingToken={null} onConfirm={noop} onCancel={noop} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('streams the preview and offers no confirm until the reply is complete', () => {
    render(
      <TurnResult
        text="Deploy payments-api:1.2.2 to UAT…"
        streaming
        pendingToken="CONFIRM-ABC123"
        onConfirm={noop}
        onCancel={noop}
      />,
    );
    expect(screen.getByText('Agent is replying…')).toBeInTheDocument();
    expect(screen.queryByTestId('inline-confirm')).toBeNull();
  });

  it('puts Confirm and Cancel right under a finished preview', () => {
    const onConfirm = jest.fn();
    const onCancel = jest.fn();
    render(
      <TurnResult
        text="Deploy payments-api:1.2.2 to UAT. Reply CONFIRM-ABC123 to confirm."
        streaming={false}
        pendingToken="CONFIRM-ABC123"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />,
    );
    expect(screen.getByText('Result')).toBeInTheDocument();
    expect(screen.getByTestId('inline-confirm')).toHaveTextContent('CONFIRM-ABC123');
    fireEvent.click(screen.getByText('Confirm & deploy'));
    fireEvent.click(screen.getByText('Cancel'));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it('shows the outcome without a confirm once nothing is pending', () => {
    render(
      <TurnResult
        text="Deployed payments-api:1.2.2 to UAT."
        streaming={false}
        pendingToken={null}
        onConfirm={noop}
        onCancel={noop}
      />,
    );
    expect(screen.getByText('Deployed payments-api:1.2.2 to UAT.')).toBeInTheDocument();
    expect(screen.queryByTestId('inline-confirm')).toBeNull();
  });
});
