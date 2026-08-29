import { useEffect, useState } from 'react';
import {
  createFrontendModule,
  AppRootElementBlueprint,
} from '@backstage/frontend-plugin-api';
import {
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Table,
  TableBody,
  TableCell,
  TableRow,
} from '@material-ui/core';

/**
 * Global shortcuts:
 *  - Cmd+K / Ctrl+K opens the search modal.
 *  - "?" opens the shortcut help dialog.
 */
const SHORTCUTS: Array<{ keys: string; action: string }> = [
  { keys: 'Cmd+K / Ctrl+K', action: 'Global search' },
  { keys: '?', action: 'This help dialog' },
  { keys: 'g then c', action: '(browser tab) — Catalog' },
];

function SearchHotkeyHost() {
  const [helpOpen, setHelpOpen] = useState(false);

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const typing =
        target &&
        (target.tagName === 'INPUT' ||
          target.tagName === 'TEXTAREA' ||
          target.isContentEditable);

      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        const searchTrigger =
          document.querySelector<HTMLElement>('a[href="/search"]');
        searchTrigger?.click();
        if (!searchTrigger) window.location.assign('/search');
        return;
      }

      if (!typing && e.key === '?') {
        e.preventDefault();
        setHelpOpen(true);
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, []);

  return (
    <Dialog
      open={helpOpen}
      onClose={() => setHelpOpen(false)}
      maxWidth="xs"
      fullWidth
    >
      <DialogTitle>Keyboard shortcuts</DialogTitle>
      <DialogContent>
        <Table size="small">
          <TableBody>
            {SHORTCUTS.map(s => (
              <TableRow key={s.keys}>
                <TableCell>
                  <code>{s.keys}</code>
                </TableCell>
                <TableCell>{s.action}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </DialogContent>
      <DialogActions>
        <Button onClick={() => setHelpOpen(false)}>Close</Button>
      </DialogActions>
    </Dialog>
  );
}

const searchHotkey = AppRootElementBlueprint.make({
  name: 'search-hotkey',
  params: {
    element: <SearchHotkeyHost />,
  },
});

export default createFrontendModule({
  pluginId: 'app',
  extensions: [searchHotkey],
});
