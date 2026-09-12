import {
  Card,
  CardContent,
  CardHeader,
  Grid,
  List,
  ListItem,
  ListItemIcon,
  ListItemText,
  makeStyles,
  Typography,
} from '@material-ui/core';
import ChevronRightIcon from '@material-ui/icons/ChevronRight';
import { Content, Header, Page } from '@backstage/core-components';
import { ChatTab, QuickAsk } from './ChatTab';
import { useAgentChat } from './useAgentChat';
import { DEV_PORTAL as P } from '../look';

/**
 * Consumer onboarding, on its own page: for people from other teams who want
 * to CONSUME our APIs, not release anything. The agent answers from the
 * consumer-onboarding skill's documents, one step at a time — which needs a
 * conversation that remembers the previous step (useAgentChat keeps one per
 * page).
 *
 * The path and the questions mirror the sections of the skill's
 * references/onboarding.md and faq.md, so every click lands on a documented
 * answer.
 */
const PATH: QuickAsk[] = [
  { label: 'Am I eligible?', hint: 'Who can consume our APIs', text: 'Am I eligible to use your APIs, and what do I need first?' },
  { label: 'Request access', hint: 'How to ask for access', text: 'How do I request access to your APIs?' },
  { label: 'Credentials & auth', hint: 'Getting credentials and authenticating', text: 'How do I get credentials and authenticate?' },
  { label: 'Environments', hint: 'Which environment to use when', text: 'Which environment should I use, and how do they differ?' },
  { label: 'First call', hint: 'Make a first successful request', text: 'Walk me through making my first API call.' },
  { label: 'Going live', hint: 'What is needed for production', text: 'What do I need to do to go live in production?' },
];

const FAQ: QuickAsk[] = [
  { label: 'How long does access take?', hint: 'Lead times', text: 'How long does an access request usually take?' },
  { label: 'My request was rejected', hint: 'Rejected requests', text: 'My access request was rejected — what now?' },
  { label: 'Getting 401 / 403', hint: 'Auth errors', text: 'I am getting 401 or 403 errors — how do I fix it?' },
  { label: 'Raise my quota', hint: 'Quota raises', text: 'How do I get my quota raised?' },
  { label: 'Who do I contact?', hint: 'Help and production contacts', text: 'Who do I contact for help, and in production?' },
];

// Sent with the question so the agent routes it to the onboarding skill even
// when the wording alone is ambiguous ("how do I get a token?"); the chat log
// shows only what the person asked.
const CONTEXT = 'Consumer onboarding question: ';

const useStyles = makeStyles(theme => ({
  sideTitle: {
    fontSize: '0.75rem',
    fontWeight: 600,
    letterSpacing: '0.04em',
    textTransform: 'uppercase' as const,
    color: theme.palette.text.secondary,
    margin: theme.spacing(1, 0, 0.5, 2),
  },
  item: {
    borderRadius: P.radius.control,
    '&:hover': { background: 'rgba(16, 185, 129, 0.10)' },
  },
  step: {
    minWidth: 28,
    height: 22,
    marginRight: theme.spacing(1.5),
    borderRadius: P.radius.pill,
    background: P.gradient,
    color: P.ink,
    fontSize: '0.72rem',
    fontWeight: 700,
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
  },
  chevron: { minWidth: 28, color: theme.palette.text.secondary },
  // Backstage truncates dense list text with an ellipsis; these are questions,
  // so they wrap instead.
  wrap: { whiteSpace: 'normal', overflow: 'visible' },
  note: { color: theme.palette.text.secondary, fontSize: '0.8rem', padding: theme.spacing(0, 2, 1) },
}));

export function OnboardingPage() {
  const classes = useStyles();
  const chat = useAgentChat('onboarding');
  const ask = (text: string) => chat.send(CONTEXT + text, { origin: 'chat', display: text });

  return (
    <Page themeId="tool">
      <Header
        title="Consumer onboarding"
        subtitle="Start using our APIs — access, credentials, your first call, going live"
      />
      <Content>
        <Grid container spacing={3}>
          <Grid item xs={12} md={8}>
            <ChatTab
              messages={chat.messages}
              busy={chat.busy}
              onSend={ask}
              title="Ask about onboarding"
              subheader="Answered step by step from the onboarding documents — if something isn't documented, it says so and tells you who to ask"
              emptyHint="Pick a step on the right, or ask in your own words — e.g. “I already have credentials, what's next?”"
              placeholder="Ask about getting access to our APIs…"
              quickAsks={[]}
            />
          </Grid>
          <Grid item xs={12} md={4}>
            <Card>
              <CardHeader title="The path" subheader="From first question to production" />
              <CardContent style={{ paddingTop: 0 }}>
                <List dense disablePadding>
                  {PATH.map((q, i) => (
                    <ListItem
                      key={q.label}
                      button
                      className={classes.item}
                      title={q.hint}
                      disabled={chat.busy}
                      onClick={() => ask(q.text)}
                    >
                      <span className={classes.step}>{i + 1}</span>
                      <ListItemText className={classes.wrap} primary={q.label} />
                    </ListItem>
                  ))}
                </List>
                <Typography className={classes.sideTitle}>Common questions</Typography>
                <List dense disablePadding>
                  {FAQ.map(q => (
                    <ListItem
                      key={q.label}
                      button
                      className={classes.item}
                      title={q.hint}
                      disabled={chat.busy}
                      onClick={() => ask(q.text)}
                    >
                      <ListItemIcon className={classes.chevron}>
                        <ChevronRightIcon fontSize="small" />
                      </ListItemIcon>
                      <ListItemText className={classes.wrap} primary={q.label} />
                    </ListItem>
                  ))}
                </List>
                <Typography className={classes.note}>
                  Read-only guidance: this page raises no tickets and grants no
                  access.
                </Typography>
              </CardContent>
            </Card>
          </Grid>
        </Grid>
      </Content>
    </Page>
  );
}
