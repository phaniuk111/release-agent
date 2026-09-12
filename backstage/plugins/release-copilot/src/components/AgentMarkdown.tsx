import { makeStyles } from '@material-ui/core';
import { MarkdownContent } from '@backstage/core-components';
import { DEV_PORTAL as P } from '../look';

/**
 * An agent reply rendered the way the portal renders it: GitHub-flavoured
 * markdown, so the queue arrives as a table rather than a wall of pipes, bold
 * is bold, and `[PR #1644](…)` is a link. Safe for model output: Backstage's
 * MarkdownContent passes any HTML through GitHub's sanitiser (rehype-sanitize,
 * default schema), which strips scripts, event handlers and javascript: links.
 */
const useStyles = makeStyles(theme => {
  const dark = theme.palette.type === 'dark';
  return {
    md: {
      fontSize: '0.875rem',
      lineHeight: 1.6,
      '& p': { margin: 0 },
      '& p + p, & p + ul, & p + ol, & p + table, & table + p, & ul + p, & ol + p': {
        marginTop: theme.spacing(1),
      },
      '& ul, & ol': { margin: 0, paddingLeft: theme.spacing(2.5) },
      '& a': { color: dark ? P.emeraldLight : '#059669', fontWeight: 500 },
      '& strong': { fontWeight: 600, color: theme.palette.text.primary },
      '& code': {
        fontFamily: P.mono,
        fontSize: '0.8rem',
        background: dark ? P.sunken : 'rgba(15,23,42,.06)',
        color: dark ? P.mint : '#047857',
        padding: '1px 5px',
        borderRadius: 6,
      },
      '& pre': {
        background: dark ? P.sunken : 'rgba(15,23,42,.04)',
        padding: theme.spacing(1.5),
        borderRadius: P.radius.control,
        overflowX: 'auto',
      },
      '& pre code': { background: 'none', padding: 0 },
      '& table': {
        display: 'block',
        overflowX: 'auto',
        borderCollapse: 'collapse',
        margin: theme.spacing(1, 0),
        fontSize: '0.78rem',
      },
      '& th': {
        textAlign: 'left',
        fontWeight: 500,
        color: theme.palette.text.secondary,
        padding: theme.spacing(0.5, 1),
        borderBottom: `1px solid ${theme.palette.divider}`,
        whiteSpace: 'nowrap',
      },
      '& td': {
        padding: theme.spacing(0.75, 1),
        borderBottom: `1px solid ${dark ? 'rgba(148,163,184,.08)' : theme.palette.divider}`,
        verticalAlign: 'top',
      },
      '& tr:last-child td': { borderBottom: 'none' },
    },
  };
});

export function AgentMarkdown(props: { text: string }) {
  const classes = useStyles();
  return (
    <MarkdownContent
      className={classes.md}
      content={props.text}
      dialect="gfm"
      linkTarget="_blank"
    />
  );
}
