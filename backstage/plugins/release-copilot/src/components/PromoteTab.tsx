import { useState } from 'react';
import { Box, Button, Card, CardContent, CardHeader, makeStyles, Typography } from '@material-ui/core';
import { TurnResult } from './TurnResult';

const useStyles = makeStyles(theme => ({
  row: { display: 'flex', alignItems: 'center', gap: theme.spacing(1.5), flexWrap: 'wrap', marginTop: theme.spacing(1) },
  kind: { minWidth: 72, fontWeight: 600 },
  note: { marginTop: theme.spacing(2), opacity: 0.8 },
}));

type Kind = 'CARE' | 'DF';

// Where each kind of release goes next. CARE climbs SIT → UAT → PRD / PRL1; a DF
// release lands on its UAT branch and goes on to PRD (DF_RELEASE_BRANCHES).
const TARGETS: Record<Kind, { env: string; label: string; approval: boolean }[]> = {
  CARE: [
    { env: 'uat', label: 'UAT', approval: false },
    { env: 'prd', label: 'PRD', approval: true },
    { env: 'prl1', label: 'PRL1', approval: true },
  ],
  DF: [{ env: 'prd', label: 'PRD', approval: true }],
};

/** The request a promotion button sends — the same words the chat would take. */
export function promotePrompt(kind: Kind, env: string): string {
  return `promote the ${kind} release to ${env}`;
}

/**
 * Promote the current release — the step that used to need typing into the
 * chat. PRD and PRL1 pause for a yes/no: the approval comes up in the banner at
 * the top of Ship, like every other decision; UAT goes straight on.
 */
export function PromoteTab(props: {
  onSend: (text: string) => Promise<void> | void;
  busy?: boolean;
  result?: { text: string; streaming: boolean; pendingToken: string | null; progress?: string[] };
}) {
  const classes = useStyles();
  const { onSend, busy = false, result = { text: '', streaming: false, pendingToken: null } } = props;
  const [asked, setAsked] = useState<string | null>(null);
  const ask = (kind: Kind, env: string) => {
    setAsked(`${kind} → ${env.toUpperCase()}`);
    void onSend(promotePrompt(kind, env));
  };
  return (
    <Card>
      <CardHeader
        title="Promote a release"
        subheader="Copies the current release's file-set onto the next environment branch and merges it. PRD and PRL1 ask for your approval first."
      />
      <CardContent>
        {(['CARE', 'DF'] as Kind[]).map(kind => (
          <div key={kind} className={classes.row}>
            <Typography className={classes.kind}>{kind}</Typography>
            {TARGETS[kind].map(t => (
              <Button
                key={t.env}
                variant="outlined"
                color={t.approval ? 'secondary' : 'primary'}
                disabled={busy}
                onClick={() => ask(kind, t.env)}
                title={t.approval ? 'Asks for your approval before anything changes' : 'Promotes straight away'}
              >
                Promote to {t.label}
                {t.approval ? ' 🛡️' : ''}
              </Button>
            ))}
          </div>
        ))}
        {asked && (
          <Typography variant="caption" display="block" className={classes.note}>
            Asked: {asked}
          </Typography>
        )}
        <Box mt={2}>
          <TurnResult {...result} onConfirm={() => {}} onCancel={() => {}} />
        </Box>
      </CardContent>
    </Card>
  );
}
