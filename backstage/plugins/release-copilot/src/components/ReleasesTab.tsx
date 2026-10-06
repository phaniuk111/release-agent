import { ReactNode, useEffect, useMemo, useRef, useState } from 'react';
import {
  Box,
  Button,
  Card,
  CardContent,
  CardHeader,
  Checkbox,
  FormControl,
  FormControlLabel,
  Grid,
  InputLabel,
  makeStyles,
  OutlinedTextFieldProps,
  MenuItem,
  Select,
  TextField,
  Tooltip,
  Typography,
} from '@material-ui/core';
import { Progress } from '@backstage/core-components';
import { apiGet, apiPost, useApiBase } from '../api';
import { DEV_PORTAL as P } from '../look';
import {
  cleanJira,
  JIRA_LABEL,
  JIRA_PLACEHOLDER,
  jiraError,
} from './jiraFormat';
import { shortName } from './queueFormat';
import {
  artifactLines,
  artifactNames,
  AutoWrite,
  buildSummary,
  canReplace,
  CHIPS,
  chipState,
  DF_DRAFT_FIELDS,
  DraftField,
  emptyQueueNote,
  forRelease,
  itemsKey,
  localToday,
  monoReleaseNote,
  PROSE_FIELDS,
  prl1Names,
  QueuedItem,
  ReleaseFields,
  releasePayload,
  releaseProblem,
  releaseRouteText,
  withQueuedItem,
} from './releaseFormat';
import { TurnResult } from './TurnResult';

type Kind = 'care' | 'df';

type QueueCtx = {
  queue?: QueuedItem[];
  default_repo?: string;
  df_default_repo?: string;
  care_release_mode?: string;
  care_release_repo?: string;
  care_release_file?: string;
};

type DefaultsResponse = {
  ok?: boolean;
  fields?: Partial<Record<DraftField | 'release_name', string>>;
  number?: number | null;
};

type DraftResponse = {
  ok?: boolean;
  disabled?: boolean;
  error?: string;
  draft?: Record<string, string>;
  sources?: Record<string, string>;
  grounded_on?: number;
};

type Tone = 'muted' | 'warn' | 'review';

const useStyles = makeStyles(theme => {
  const dark = theme.palette.type === 'dark';
  return {
    note: {
      marginBottom: theme.spacing(1.5),
      color: dark ? P.amber : '#b45309',
    },
    empty: {
      marginBottom: theme.spacing(1.5),
      padding: theme.spacing(1, 1.5),
      borderRadius: P.radius.control,
      border: `1px solid ${theme.palette.divider}`,
    },
    queued: {
      marginBottom: theme.spacing(1.5),
      padding: theme.spacing(1, 1.5),
      borderRadius: P.radius.control,
      border: `1px solid ${
        dark ? 'rgba(16,185,129,.4)' : 'rgba(5,150,105,.35)'
      }`,
      background: dark ? 'rgba(16,185,129,.05)' : 'rgba(5,150,105,.04)',
    },
    queuedTitle: { fontSize: '0.75rem', color: dark ? P.emerald : '#047857' },
    row: { fontFamily: P.mono, fontSize: '0.8rem' },
    tag: { marginLeft: theme.spacing(0.75), fontSize: '0.75rem' },
    who: { marginLeft: theme.spacing(1), color: theme.palette.text.secondary },
    chip: {
      padding: '1px 6px',
      borderRadius: 4,
      fontSize: '0.65rem',
      whiteSpace: 'nowrap',
      background: dark ? 'rgba(51,65,85,.9)' : 'rgba(15,23,42,.08)',
    },
    chipAi: {
      background: 'rgba(16,185,129,.15)',
      color: dark ? P.emerald : '#047857',
    },
    chipFallback: {
      background: 'rgba(245,158,11,.15)',
      color: dark ? P.amber : '#b45309',
    },
    flagRow: {
      display: 'flex',
      alignItems: 'center',
      gap: theme.spacing(1.5),
      fontFamily: P.mono,
      fontSize: '0.75rem',
    },
    flagName: {
      width: 180,
      flexShrink: 0,
      overflow: 'hidden',
      textOverflow: 'ellipsis',
    },
    muted: { color: theme.palette.text.secondary },
    warn: { color: dark ? P.amber : '#b45309' },
  };
});

/** Each prose key's field in the form. */
const FIELD_OF: Record<DraftField, keyof ReleaseFields> = {
  change_summary: 'summary',
  change_description: 'description',
  change_reason: 'reason',
  associated_risk: 'risk',
  consequence: 'consequence',
  user_service_impact: 'impact',
};

// The initiator is the person creating the release: remembered from the last
// one they created, else the email they queue with (QueueTab's key).
const INITIATOR_KEY = 'release-copilot:release-initiator';
const QUEUE_EMAIL_KEY = 'release-copilot:email';
const savedInitiator = () => {
  try {
    return (
      window.localStorage.getItem(INITIATOR_KEY) ??
      window.localStorage.getItem(QUEUE_EMAIL_KEY) ??
      ''
    );
  } catch {
    return '';
  }
};
const saveInitiator = (email: string) => {
  try {
    window.localStorage.setItem(INITIATOR_KEY, email);
  } catch {
    /* private mode: just not remembered */
  }
};

const REQUIRED =
  'Release name, start, end, initiator and summary are required.';

/**
 * The CARE / DF release form — the portal's release_form.js. The queued items
 * of this kind arrive ticked (untick to defer one to the next release); every
 * change-request field but start and end is filled from the facts
 * (/api/release-defaults) and follows them only while nobody has edited it.
 * Submitting sends the release JSON through the chat, which previews it and
 * asks for its CONFIRM token — nothing is pushed before that.
 */
function ReleaseForm(props: {
  kind: Kind;
  qctx: QueueCtx;
  loadError: string | null;
  busy: boolean;
  llm: boolean;
  onSend: (text: string) => Promise<void> | void;
}) {
  const classes = useStyles();
  const { kind, qctx, loadError, busy, llm, onSend } = props;
  const apiBase = useApiBase();
  const isDf = kind === 'df';
  const mono = !isDf && qctx.care_release_mode === 'mono';
  const queue = useMemo(() => forRelease(qctx.queue, isDf), [qctx.queue, isDf]);
  const otherQueued = forRelease(qctx.queue, !isDf).length;
  const targetRepo = isDf
    ? qctx.df_default_repo || qctx.default_repo
    : (mono && qctx.care_release_repo) || qctx.default_repo;
  // Mono mode: the release file lives in one configured repo.
  const repoLocked = mono && !!qctx.care_release_repo;
  const queuedByName = useMemo(() => {
    const out: Record<string, QueuedItem> = {};
    queue.forEach(q => {
      if (q.artifact_name) out[q.artifact_name] = q;
    });
    return out;
  }, [queue]);

  // DF, and CARE in mono mode, open already drafted (chips, Regenerate); CARE
  // in fileset mode keeps the button draft. With no model (LLM_ENABLED=false)
  // there is no draft at all: every form takes the standard wording.
  const autoDraft = llm && (mono || isDf);
  const draftFields: readonly DraftField[] = isDf
    ? DF_DRAFT_FIELDS
    : PROSE_FIELDS;

  // Fields live in a ref as well as state: the defaults and the draft land
  // asynchronously and must compare against what the field holds NOW.
  const [f, setF] = useState<ReleaseFields>(() => ({
    name: '',
    jira: '',
    start: '',
    end: '',
    initiator: savedInitiator(),
    summary: '',
    description: '',
    reason: '',
    risk: '',
    consequence: '',
    impact: '',
    repo: targetRepo || '',
    artifacts: queue.reduce((text, q) => withQueuedItem(text, q, true), ''),
  }));
  const fRef = useRef(f);
  const update = (patch: Partial<ReleaseFields>) => {
    fRef.current = { ...fRef.current, ...patch };
    setF(fRef.current);
  };

  const [ticked, setTicked] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(queue.map(q => [q.artifact_name ?? '', true])),
  );
  const [manualPrl1, setManualPrl1] = useState<Set<string>>(() => new Set());
  const [proseAuto, setProseAuto] = useState<
    Partial<Record<DraftField, AutoWrite>>
  >({});
  const proseAutoRef = useRef<Partial<Record<DraftField, AutoWrite>>>({});
  const autoVals = useRef<Record<string, string>>({});
  const summaryAuto = useRef<AutoWrite>(null);
  const draftFor = useRef<string | null>(null);
  const initiatorEdited = useRef(false);
  const [draftNote, setDraftNote] = useState<{
    text: string;
    tone: Tone;
  } | null>(null);
  const [drafting, setDrafting] = useState(false);
  const draftingRef = useRef(false);
  const [llmOff, setLlmOff] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const note = (text: string, tone: Tone = 'muted') =>
    setDraftNote({ text, tone });

  // A field follows the defaults only while it holds the last value they put there.
  const fillAuto = (key: keyof ReleaseFields, value: string | undefined) => {
    if (value === undefined) return;
    const cur = fRef.current[key];
    if (!cur.trim() || cur === autoVals.current[key]) {
      update({ [key]: value } as Partial<ReleaseFields>);
      autoVals.current[key] = value;
    }
  };

  // Mono mode: the release file's summary IS the release name, so the name is
  // the summary's one writer — until the person edits the summary itself.
  const syncSummary = () => {
    if (!canReplace(fRef.current.summary, summaryAuto.current)) return;
    update({ summary: fRef.current.name });
    summaryAuto.current = { value: fRef.current.name, source: 'team' };
  };

  // One automatic writer per prose field at a time, never over the person's text.
  const writeProse = (
    field: DraftField,
    value: string | undefined,
    source: string,
  ) => {
    const key = FIELD_OF[field];
    if (
      value !== undefined &&
      canReplace(fRef.current[key], proseAutoRef.current[field])
    ) {
      update({ [key]: value } as Partial<ReleaseFields>);
      proseAutoRef.current = {
        ...proseAutoRef.current,
        [field]: { value, source },
      };
      setProseAuto(proseAutoRef.current);
    }
  };

  // The release number is looked up once (the first call reads the repo's
  // release PRs — slow); every later recompute sends it back.
  const knownNumber = useRef<number | null>(null);
  const defaultsSeq = useRef(0);
  const defaultsTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const defaultsInflight = useRef<Promise<void> | null>(null);

  const runDefaults = async () => {
    const seq = ++defaultsSeq.current;
    const cur = fRef.current;
    let res: DefaultsResponse | null = null;
    try {
      res = await apiPost<DefaultsResponse>(
        apiBase,
        '/api/release-defaults',
        {
          artifacts: artifactLines(cur.artifacts),
          kind,
          repo: cur.repo.trim(),
          date: cur.start ? cur.start.slice(0, 10) : localToday(),
          number: knownNumber.current,
        },
        { allowFailure: true },
      );
    } catch {
      return; // defaults are a convenience
    }
    if (res?.number) knownNumber.current = res.number;
    if (seq !== defaultsSeq.current || !res || !res.ok) return; // a newer request won
    const fields = res.fields ?? {};
    fillAuto('name', fields.release_name);
    if (!autoDraft) {
      fillAuto('summary', fields.change_summary);
      fillAuto('description', fields.change_description);
      fillAuto('reason', fields.change_reason);
      fillAuto('risk', fields.associated_risk);
      fillAuto('consequence', fields.consequence);
      fillAuto('impact', fields.user_service_impact);
      return;
    }
    if (mono) syncSummary();
    // A draft of other items no longer describes this release: the standard
    // wording takes its fields back (edits stay) until Regenerate.
    if (
      draftFor.current !== null &&
      draftFor.current !== itemsKey(artifactLines(fRef.current.artifacts))
    ) {
      draftFor.current = null;
      note(
        'The items changed since the AI draft — the standard wording is back. Regenerate to draft these items.',
      );
    }
    if (draftFor.current === null)
      draftFields.forEach(field => writeProse(field, fields[field], 'team'));
  };

  const startDefaults = () => {
    const p: Promise<void> = runDefaults().finally(() => {
      if (defaultsInflight.current === p) defaultsInflight.current = null;
    });
    defaultsInflight.current = p;
  };

  const refreshDefaults = () => {
    if (defaultsTimer.current) clearTimeout(defaultsTimer.current);
    defaultsTimer.current = setTimeout(() => {
      defaultsTimer.current = null;
      startDefaults();
    }, 250);
  };

  // Before a submit reads the fields: finish any recompute still pending, so a
  // start date set just before Create cannot send the previous date's name.
  const settleDefaults = async (): Promise<boolean> => {
    if (defaultsTimer.current) {
      clearTimeout(defaultsTimer.current);
      defaultsTimer.current = null;
      startDefaults();
    }
    const inflight = defaultsInflight.current;
    if (!inflight) return true;
    return Promise.race([
      inflight.then(() => true),
      new Promise<boolean>(r => setTimeout(() => r(false), 8000)),
    ]);
  };

  // DF, and CARE in mono mode: one model call from every artifact line;
  // Regenerate replaces only the fields the person has not edited.
  const requestDraft = async () => {
    if (draftingRef.current) return;
    const lines = artifactLines(fRef.current.artifacts);
    if (!lines.length) {
      note('Tick or type at least one artifact first.');
      return;
    }
    const key = itemsKey(lines);
    draftingRef.current = true;
    setDrafting(true);
    note('Drafting the change request from the queued items…');
    let res: DraftResponse;
    try {
      res = await apiPost<DraftResponse>(
        apiBase,
        '/api/release-draft',
        { artifacts: lines, kind },
        { allowFailure: true },
      );
    } catch {
      res = {
        ok: false,
        error:
          'Could not reach the drafting service — the standard wording stays.',
      };
    }
    draftingRef.current = false;
    setDrafting(false);
    if (!res || !res.ok) {
      if (res?.disabled) setLlmOff(true);
      note(
        res?.error || 'Could not draft — the standard wording stays.',
        'warn',
      );
      return;
    }
    if (key !== itemsKey(artifactLines(fRef.current.artifacts))) {
      note(
        'The items changed while drafting — Regenerate to draft these items.',
      );
      return;
    }
    draftFor.current = key;
    const draft = res.draft ?? {};
    const sources = res.sources ?? {};
    draftFields.forEach(field =>
      writeProse(field, draft[field], sources[field] || 'ai'),
    );
    const n = res.grounded_on || lines.length;
    note(
      `AI draft of ${n} item${
        n === 1 ? '' : 's'
      } — review every field; anything you edit is kept.`,
      'review',
    );
  };

  // CARE in fileset mode: a button, not automatic — it costs a model call, and
  // a governance field that fills itself silently stops being read.
  const buttonDraft = async () => {
    const tickedItems = queue
      .filter(q => ticked[q.artifact_name ?? ''])
      .map(q => `${q.artifact_name}:${q.artifact_version}`);
    if (!tickedItems.length) {
      note('Tick at least one item first.');
      return;
    }
    setDrafting(true);
    note('Drafting…');
    let res: DraftResponse;
    try {
      res = await apiPost<DraftResponse>(
        apiBase,
        '/api/release-draft',
        { artifacts: tickedItems, kind },
        { allowFailure: true },
      );
    } catch (e) {
      res = { ok: false, error: String(e) };
    }
    setDrafting(false);
    if (!res || !res.ok) {
      if (res?.disabled) setLlmOff(true);
      note(res?.error || 'Could not draft — fill it in manually.');
      return;
    }
    const d = res.draft ?? {};
    const patch: Partial<ReleaseFields> = {};
    if (d.change_summary) patch.summary = d.change_summary;
    if (d.change_description) patch.description = d.change_description;
    if (d.change_reason) patch.reason = d.change_reason;
    if (d.associated_risk) patch.risk = d.associated_risk;
    if (d.consequence) patch.consequence = d.consequence;
    if (d.user_impact) patch.impact = d.user_impact;
    update(patch);
    note(
      `Draft from ${res.grounded_on} item(s) — review every field before submitting.`,
      'review',
    );
  };

  useEffect(() => {
    refreshDefaults(); // also when nothing is queued: name, number, wording
    // Signed in: default to the verified user — still editable (a change can
    // be raised on someone's behalf), but never over a value typed here.
    apiGet<{ signed_in?: boolean; email?: string }>(apiBase, '/api/whoami')
      .then(who => {
        if (who?.signed_in && who.email && !initiatorEdited.current)
          update({ initiator: who.email });
      })
      .catch(() => {});
    if (queue.length && autoDraft) void requestDraft(); // once, as the form opens
    return () => {
      if (defaultsTimer.current) clearTimeout(defaultsTimer.current);
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const toggle = (q: QueuedItem, on: boolean) => {
    setTicked(prev => ({ ...prev, [q.artifact_name ?? '']: on }));
    update({ artifacts: withQueuedItem(fRef.current.artifacts, q, on) });
    refreshDefaults();
  };

  const submit = async () => {
    setError(null);
    if (!(await settleDefaults())) {
      setError(
        'Still computing the release name and wording — try again in a moment.',
      );
      return;
    }
    const cur = fRef.current;
    const jiraProblem = jiraError(cur.jira);
    const problem = releaseProblem(cur, jiraProblem);
    // As the portal: the initiator is remembered once the required fields
    // and the JIRA are in, whatever is wrong after that.
    if (problem && (problem === REQUIRED || problem === jiraProblem)) {
      setError(problem);
      return;
    }
    saveInitiator(cur.initiator.trim());
    if (problem) {
      setError(problem);
      return;
    }
    const payload = releasePayload(cur, {
      isDf,
      jira: cleanJira(cur.jira),
      prl1Only: prl1Names(cur.artifacts, queuedByName, manualPrl1, isDf),
    });
    await onSend(JSON.stringify(payload));
  };

  const chip = (field: DraftField): ReactNode => {
    if (!autoDraft || !draftFields.includes(field)) return null;
    const state = chipState(f[FIELD_OF[field]], proseAuto[field]);
    if (!state) return null;
    let tone = '';
    if (state === 'ai') tone = classes.chipAi;
    if (state === 'fallback') tone = classes.chipFallback;
    return (
      <Tooltip title={CHIPS[state].title}>
        <span
          className={`${classes.chip} ${tone}`}
          data-testid={`chip-${field}`}
        >
          {CHIPS[state].label}
        </span>
      </Tooltip>
    );
  };

  const text = (
    id: string,
    label: string,
    key: keyof ReleaseFields,
    extra: Partial<Omit<OutlinedTextFieldProps, 'variant'>> & {
      field?: DraftField;
    } = {},
  ) => {
    const { field, ...rest } = extra;
    return (
      <TextField
        id={id}
        fullWidth
        variant="outlined"
        size="small"
        label={label}
        // Where the text came from, under the field (a chip in the label would
        // be drawn twice — once more in the outlined notch).
        helperText={field ? chip(field) : undefined}
        value={f[key]}
        onChange={e =>
          update({ [key]: e.target.value } as Partial<ReleaseFields>)
        }
        {...rest}
      />
    );
  };

  const names = Array.from(new Set(artifactNames(f.artifacts)));
  const showDraftRow = llm && queue.length > 0 && !llmOff;
  let intro =
    'Artifacts + change request → the release file-set (artefact.json, SDLC governance, ' +
    "per-env workflows), generated by the repo's updater script. You'll see the full diff and " +
    'RCTL timeline before anything is pushed.';
  if (mono)
    intro = monoReleaseNote(qctx.care_release_file, qctx.care_release_repo);
  if (isDf) {
    intro =
      'Dataflow images + change request → the release file-set (artefact.json + SDLC governance), ' +
      "generated by the repo's updater script. DF images are excluded from the helm deploy workflows " +
      '— they ship via the Dataflow dispatch. Raised in its own repo, separately from the CARE ' +
      "release. You'll see the full diff and RCTL timeline before anything is pushed.";
  }
  const createLabel = isDf ? 'Create DF release' : 'Create CARE release';

  return (
    <>
      <Typography
        variant="body2"
        color="textSecondary"
        style={{ marginBottom: 12 }}
      >
        {intro}
      </Typography>

      {loadError && (
        <Typography variant="body2" className={classes.note}>
          Couldn't load the intake queue ({loadError}) — the form still works;
          fields aren't pre-filled.
        </Typography>
      )}
      {!loadError && !queue.length && (
        <Typography
          variant="body2"
          className={classes.empty}
          data-testid="release-empty"
        >
          {emptyQueueNote(isDf, otherQueued)}
        </Typography>
      )}

      {queue.length > 0 && (
        <Box className={classes.queued} data-testid="release-queued">
          <Typography className={classes.queuedTitle}>
            Queued for this release ({queue.length}) — untick to defer to the
            next one
          </Typography>
          {queue.map(q => {
            const bs = buildSummary(q);
            const tip = [q.change_details, q.note].filter(Boolean).join(' · ');
            return (
              <div key={`${q.artifact_name}:${q.artifact_version}`}>
                <FormControlLabel
                  control={
                    <Checkbox
                      size="small"
                      checked={!!ticked[q.artifact_name ?? '']}
                      onChange={e => toggle(q, e.target.checked)}
                      inputProps={{
                        'aria-label': `${q.artifact_name}:${q.artifact_version}`,
                      }}
                    />
                  }
                  label={
                    <span
                      className={classes.row}
                      title={
                        tip ? `${tip} — ${q.requested_by ?? ''}` : undefined
                      }
                    >
                      {q.artifact_name}:{q.artifact_version}
                      {bs.state === 'verified' && (
                        <span
                          className={classes.tag}
                          title={bs.title}
                          style={{ color: P.emerald }}
                        >
                          ✓
                        </span>
                      )}
                      {bs.state === 'unverified' && (
                        <span
                          className={`${classes.tag} ${classes.warn}`}
                          title={bs.title}
                        >
                          ⚠
                        </span>
                      )}
                      {q.jira_ticket && (
                        <span className={`${classes.tag} ${classes.warn}`}>
                          {q.jira_ticket}
                        </span>
                      )}
                      {q.prl1_only && (
                        <span
                          className={classes.tag}
                          style={{ color: '#a78bfa' }}
                        >
                          PRL1
                        </span>
                      )}
                      {q.df_only && (
                        <span className={classes.tag} style={{ color: P.sky }}>
                          DF
                        </span>
                      )}
                      <span className={classes.who}>
                        {shortName(q.requested_by)}
                      </span>
                    </span>
                  }
                />
              </div>
            );
          })}
        </Box>
      )}

      {showDraftRow && (
        <Box display="flex" alignItems="center" mb={1.5} style={{ gap: 8 }}>
          <Button
            size="small"
            variant="outlined"
            disabled={drafting}
            onClick={() => void (autoDraft ? requestDraft() : buttonDraft())}
            title={
              autoDraft
                ? 'Draft the description, reason, risk, consequence and impact again from the items above — fields you have edited are kept'
                : 'Writes summary/reason/risk/consequence/impact from the ticked items — review before submitting'
            }
          >
            {autoDraft ? 'Regenerate' : 'Draft change request'}
          </Button>
          {draftNote && (
            <Typography
              variant="caption"
              data-testid="draft-note"
              className={
                draftNote.tone === 'muted' ? classes.muted : classes.warn
              }
            >
              {draftNote.text}
            </Typography>
          )}
        </Box>
      )}
      {!showDraftRow && llmOff && draftNote && (
        <Typography
          variant="caption"
          display="block"
          className={classes.warn}
          style={{ marginBottom: 8 }}
        >
          {draftNote.text}
        </Typography>
      )}
      {drafting && <Progress />}

      <Grid container spacing={2}>
        <Grid item xs={12} sm={8}>
          {text('rel-name', 'Release name *', 'name', {
            placeholder: 'e.g. July 20th 2026 : Release 31',
            onChange: e => {
              update({ name: e.target.value });
              if (mono) syncSummary();
            },
          })}
        </Grid>
        <Grid item xs={12} sm={4}>
          {/* The release manager's JIRA: every commit of this release — and of
              its promotions to PRD/PRL1 — starts with it. */}
          {text('rel-jira', JIRA_LABEL, 'jira', {
            placeholder: JIRA_PLACEHOLDER,
          })}
        </Grid>
        <Grid item xs={12} sm={6}>
          {text('rel-start', 'Start *', 'start', {
            type: 'datetime-local',
            InputLabelProps: { shrink: true },
            onChange: e => {
              update({ start: e.target.value });
              refreshDefaults();
            },
          })}
        </Grid>
        <Grid item xs={12} sm={6}>
          {text('rel-end', 'End *', 'end', {
            type: 'datetime-local',
            InputLabelProps: { shrink: true },
          })}
        </Grid>
        <Grid item xs={12}>
          {text('rel-initiator', 'Change initiator (email) *', 'initiator', {
            placeholder: 'you@company.com',
            onChange: e => {
              initiatorEdited.current = true;
              update({ initiator: e.target.value });
            },
          })}
        </Grid>
        <Grid item xs={12}>
          {text('rel-summary', 'Change summary *', 'summary', {
            field: 'change_summary',
          })}
        </Grid>
        <Grid item xs={12}>
          {text('rel-desc', 'Change description', 'description', {
            field: 'change_description',
            multiline: true,
            minRows: 2,
          })}
        </Grid>
        <Grid item xs={12}>
          {text('rel-reason', 'Change reason', 'reason', {
            field: 'change_reason',
            multiline: true,
            minRows: 2,
          })}
        </Grid>
        <Grid item xs={12}>
          {text('rel-risk', 'Associated risk', 'risk', {
            field: 'associated_risk',
            multiline: true,
            minRows: 2,
          })}
        </Grid>
        <Grid item xs={12}>
          {text('rel-consequence', 'Consequence', 'consequence', {
            field: 'consequence',
            multiline: true,
            minRows: 2,
          })}
        </Grid>
        <Grid item xs={12}>
          {text('rel-impact', 'User/service impact', 'impact', {
            field: 'user_service_impact',
            multiline: true,
            minRows: 2,
          })}
        </Grid>
        <Grid item xs={12}>
          {text('rel-repo', 'Deployment repo (owner/repo) *', 'repo', {
            placeholder: 'e.g. my-org/deployment-repo',
            title: repoLocked
              ? 'CARE releases are raised in this repo (CARE_RELEASE_REPO)'
              : undefined,
            InputProps: { readOnly: repoLocked },
            onBlur: () => refreshDefaults(),
          })}
        </Grid>
        <Grid item xs={12}>
          {text(
            'rel-artifacts',
            isDf
              ? 'DF images * — one per line (full URL or name:version); all excluded from helm deploys'
              : 'Artifacts * — one per line (full URL or name:version)',
            'artifacts',
            {
              multiline: true,
              minRows: 5,
              placeholder: isDf
                ? 'order-enrichment:1.4.2\nhttps://artifactory…/df-position-agg:2.1.0'
                : 'acme-workflow-service:4.0.66\nhttps://artifactory…/acme-risk-fetcher:4.0.153',
              InputProps: { style: { fontFamily: P.mono } },
              inputProps: { spellCheck: false },
              onChange: e => {
                update({ artifacts: e.target.value });
                refreshDefaults();
              },
            },
          )}
        </Grid>
      </Grid>
      <Typography
        variant="caption"
        color="textSecondary"
        display="block"
        style={{ margin: '6px 0 10px' }}
      >
        Filled from the queue and the release history — set the start and end,
        review the rest. Anything you edit is kept.
      </Typography>

      {/* Routing per service is READ from the queue — chosen with the tick
          boxes when it was queued. Only a chart typed straight into the form
          has no choice to read; that one case still offers PRL1-only. */}
      {names.length > 0 && (
        <Box mb={1.5} data-testid="release-routing">
          <Typography variant="caption" color="textSecondary" display="block">
            {isDf ? 'Pipelines (from the queue):' : 'Routing (from the queue):'}
          </Typography>
          {names.map(n => {
            const q = queuedByName[n];
            return (
              <div key={n} className={classes.flagRow}>
                <span className={classes.flagName}>{n}</span>
                {q && (
                  <>
                    <span className={classes.muted} style={{ flex: 1 }}>
                      {releaseRouteText(q, isDf)}
                    </span>
                    <span className={classes.muted}>from the queue</span>
                  </>
                )}
                {!q && isDf && (
                  <span className={classes.muted} style={{ flex: 1 }}>
                    not queued — pipelines chosen at deploy time
                  </span>
                )}
                {!q && !isDf && (
                  <>
                    <span className={classes.warn} style={{ flex: 1 }}>
                      not queued — no routing choice to read
                    </span>
                    <FormControlLabel
                      control={
                        <Checkbox
                          size="small"
                          checked={manualPrl1.has(n)}
                          onChange={e =>
                            setManualPrl1(prev => {
                              const next = new Set(prev);
                              if (e.target.checked) next.add(n);
                              else next.delete(n);
                              return next;
                            })
                          }
                          inputProps={{ 'aria-label': `${n} PRL1-only` }}
                        />
                      }
                      label="PRL1-only"
                    />
                  </>
                )}
              </div>
            );
          })}
        </Box>
      )}

      {error && (
        <Typography
          color="error"
          style={{ marginTop: 8 }}
          data-testid="release-error"
        >
          {error}
        </Typography>
      )}
      <Button
        variant="contained"
        color="primary"
        style={{ marginTop: 12 }}
        onClick={submit}
        disabled={busy}
      >
        {busy ? 'Working…' : createLabel}
      </Button>
    </>
  );
}

export function ReleasesTab(props: {
  onSend: (text: string) => Promise<void> | void;
  /** Any turn is in flight (one at a time, across tabs). */
  busy?: boolean;
  /** The reply to THIS tab's latest submission — preview, token, outcome. */
  result?: {
    text: string;
    streaming: boolean;
    pendingToken: string | null;
    progress?: string[];
  };
  onConfirm?: () => void;
  onCancel?: () => void;
  /** A model is on (/api/ui-config): AI drafting is offered. Default true. */
  llm?: boolean;
}) {
  const {
    onSend,
    busy = false,
    llm = true,
    result = { text: '', streaming: false, pendingToken: null },
    onConfirm = () => {},
    onCancel = () => {},
  } = props;
  const apiBase = useApiBase();
  const [kind, setKind] = useState<Kind>('care');
  const [qctx, setQctx] = useState<QueueCtx | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    apiGet<QueueCtx>(apiBase, '/api/release-queue')
      .then(ctx => {
        if (live) setQctx(ctx);
      })
      .catch(e => {
        if (!live) return;
        setLoadError((e as Error).message);
        setQctx({});
      });
    return () => {
      live = false;
    };
  }, [apiBase]);

  return (
    <Card>
      <CardHeader
        title={kind === 'df' ? 'DF Release' : 'CARE Release'}
        subheader="Queued items + change request → the chat's preview → CONFIRM token; nothing is pushed before that"
      />
      <CardContent>
        <FormControl
          variant="outlined"
          size="small"
          style={{ minWidth: 200, marginBottom: 12 }}
        >
          <InputLabel id="release-kind-label">Release kind</InputLabel>
          <Select
            id="release-kind"
            labelId="release-kind-label"
            value={kind}
            label="Release kind"
            onChange={e => setKind(e.target.value as Kind)}
          >
            <MenuItem value="care">CARE release</MenuItem>
            <MenuItem value="df">Dataflow release</MenuItem>
          </Select>
        </FormControl>
        {!qctx && <Progress />}
        {/* Keyed by kind: switching starts that release's form afresh, as
            opening the other pill does in the portal. */}
        {qctx && (
          <ReleaseForm
            key={kind}
            kind={kind}
            qctx={qctx}
            loadError={loadError}
            busy={busy}
            llm={llm}
            onSend={onSend}
          />
        )}
        <TurnResult
          {...result}
          onConfirm={onConfirm}
          onCancel={onCancel}
          confirmLabel="Confirm & release"
        />
      </CardContent>
    </Card>
  );
}
