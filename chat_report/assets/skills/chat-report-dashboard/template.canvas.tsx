import {
  BarChart,
  Callout,
  Card,
  CardBody,
  CardHeader,
  Divider,
  Grid,
  H1,
  H2,
  LineChart,
  Stack,
  Stat,
  Table,
  Text,
  UsageBar,
} from "cursor/canvas";
import type { StatTone, TableRowTone } from "cursor/canvas";

type Quality = "trusted" | "adjusted" | "excluded";
type Evidence = "measurement" | "experiment" | "observational" | "judgment";
type Unit = "%" | "ms" | "count" | "tokens" | "ratio";
type Efficacy = "met" | "partial" | "missed" | "undetermined";
type Severity = "high" | "medium" | "low" | "info";

type Metric = {
  id: string;
  label: string;
  group: "efficiency" | "context" | "customization" | "delegation" | "efficacy";
  value: number | null;
  unit: Unit;
  formula: string;
  source: string;
  evidence: Evidence;
  quality: Quality;
  headline?: boolean;
  note?: string;
};

type Metrics = {
  schema: "noether-metrics/1";
  report_kind: "single-session" | "diff" | "aggregate";
  subject: {
    session_ids: string[];
    title: string | null;
    model: string | null;
    generated_at: string;
    source_json: string;
  };
  verdict: { efficacy: Efficacy; summary: string };
  metrics: Metric[];
  context: {
    limit: number | null;
    categories: { name: string; tokens: number }[];
    top_nodes: { label: string; category: string; tokens: number }[];
  };
  tool_classes: {
    name: string;
    completed: number;
    error: number;
    error_benign: number;
    denied: number;
    total_ms: number;
  }[];
  turns: {
    index: number;
    bubble_id: string | null;
    duration_ms: number | null;
    tool_calls: number;
    errors: number;
    efficacy: Efficacy;
    summary: string;
  }[];
  findings: {
    id: string;
    source: "chat-report" | "noether";
    severity: Severity;
    turn: number | null;
    summary: string;
    hint: string;
  }[];
  customizations: {
    path: string;
    kind: "skill" | "rule" | "agent_doc" | "hook";
    referenced: boolean;
    attached: boolean;
    read: boolean;
  }[];
  subagents: {
    type: string;
    status: string | null;
    in_flight?: boolean;
    tool_calls: number;
    errors: number;
    denials: number;
    duration_ms: number | null;
  }[];
  data_quality: { field: string; quality: "adjusted" | "excluded"; reason: string }[];
  diff?: {
    metrics: { label: string; unit: Unit; before: number; after: number }[];
    findings: { new: string[]; resolved: string[]; persisting: string[] };
  };
  aggregate?: {
    sessions: { id: string; title: string | null; tool_calls: number; error_rate: number; findings: number }[];
    heuristic_frequency: { id: string; count: number }[];
    skill_read_frequency: { path: string; count: number }[];
  };
};

// SYNTHETIC EXAMPLE. Replace DATA entirely with the session's metrics.json; never reuse these values.
const DATA: Metrics = {
  schema: "noether-metrics/1",
  report_kind: "single-session",
  subject: {
    session_ids: ["5e5e5e5e-0000-4000-8000-000000000001"],
    title: "SYNTHETIC EXAMPLE: refactor auth middleware",
    model: "example-model",
    generated_at: "2026-01-15T14:02:11Z",
    source_json: ".cursor/local/chat-reports/5e5e5e5e-0000-4000-8000-000000000001.json",
  },
  verdict: {
    efficacy: "partial",
    summary: "Turns 1-2 met intent. Turn 3 stalled in a lint retry loop on a file the edit did not touch.",
  },
  metrics: [
    { id: "tool_success_adjusted", label: "Tool success (adjusted)", group: "efficiency", value: 88.9, unit: "%", formula: "(completed + benign_error) / (total - loading)", source: "turns[].tool_calls[].status", evidence: "measurement", quality: "adjusted", headline: true, note: "2 shell exit-1 results reclassified as benign" },
    { id: "tool_success_raw", label: "Tool success (raw)", group: "efficiency", value: 83.3, unit: "%", formula: "completed / (total - loading)", source: "aggregates.status_counts", evidence: "measurement", quality: "trusted" },
    { id: "empty_search_rate", label: "Empty-search rate", group: "efficiency", value: 12.5, unit: "%", formula: "empty searches / searches", source: "turns[].tool_calls[].output_char_len", evidence: "measurement", quality: "trusted" },
    { id: "redundant_reads", label: "Redundant reads", group: "efficiency", value: 2, unit: "count", formula: "same path read again with no edit between", source: "turns[].tool_calls[].target_path", evidence: "measurement", quality: "trusted" },
    { id: "tool_time_share", label: "Tool-time share", group: "efficiency", value: 41.5, unit: "%", formula: "union of tool intervals / turn duration_ms", source: "turns[].tool_calls[].started_at_ms, completed_at_ms", evidence: "measurement", quality: "adjusted", note: "relative timing only" },
    { id: "thinking_share", label: "Thinking share", group: "efficiency", value: null, unit: "%", formula: "thinking ms / turn ms", source: "turns[].thinking_blocks[].duration_ms", evidence: "measurement", quality: "excluded", note: "block durations unreliable" },
    { id: "context_usage", label: "Context used", group: "context", value: 61.2, unit: "%", formula: "total_context_tokens_used / context_token_limit", source: "session.token_breakdown", evidence: "measurement", quality: "trusted", headline: true },
    { id: "fixed_overhead", label: "Fixed context overhead", group: "context", value: 33.9, unit: "%", formula: "(system_prompt+rules+skills+tools+mcp+subagents) / total", source: "session.token_breakdown.by_category", evidence: "measurement", quality: "trusted", headline: true },
    { id: "skill_compliance", label: "Referenced skills read", group: "customization", value: 66.7, unit: "%", formula: "referenced skills read / referenced skills", source: "session.customizations", evidence: "measurement", quality: "trusted", headline: true },
    { id: "findings_high_medium", label: "chat-report HIGH + MEDIUM", group: "customization", value: 2, unit: "count", formula: "by_severity.high + by_severity.medium", source: "diagnostics.by_severity", evidence: "measurement", quality: "trusted", headline: true },
    { id: "subagents", label: "Subagents dispatched", group: "delegation", value: 1, unit: "count", formula: "count(session.subagents)", source: "aggregates.subagent_rollup.count", evidence: "measurement", quality: "trusted" },
  ],
  context: {
    limit: 200000,
    categories: [
      { name: "system_prompt", tokens: 3400 },
      { name: "rules", tokens: 5200 },
      { name: "skills", tokens: 14800 },
      { name: "tools", tokens: 6500 },
      { name: "mcp", tokens: 9100 },
      { name: "subagents", tokens: 2500 },
      { name: "conversation", tokens: 80900 },
    ],
    top_nodes: [
      { label: "Read output: src/auth/middleware.ts", category: "conversation", tokens: 9800 },
      { label: "Skill: api-conventions", category: "skills", tokens: 6200 },
      { label: "Shell output: npm test", category: "conversation", tokens: 5400 },
    ],
  },
  tool_classes: [
    { name: "native-read", completed: 12, error: 0, error_benign: 0, denied: 0, total_ms: 5400 },
    { name: "native-search", completed: 8, error: 0, error_benign: 0, denied: 0, total_ms: 3900 },
    { name: "native-edit", completed: 6, error: 0, error_benign: 0, denied: 1, total_ms: 2100 },
    { name: "native-shell", completed: 3, error: 3, error_benign: 2, denied: 0, total_ms: 41200 },
    { name: "subagent-dispatch", completed: 1, error: 0, error_benign: 0, denied: 0, total_ms: 96000 },
  ],
  turns: [
    { index: 1, bubble_id: "aaaa1111", duration_ms: 84000, tool_calls: 12, errors: 0, efficacy: "met", summary: "Mapped middleware call sites via explore subagent." },
    { index: 2, bubble_id: "bbbb2222", duration_ms: 121000, tool_calls: 14, errors: 0, efficacy: "met", summary: "Refactored token validation; tests pass." },
    { index: 3, bubble_id: "cccc3333", duration_ms: 97000, tool_calls: 10, errors: 3, efficacy: "missed", summary: "Lint retries on an untouched file; user stopped the turn." },
  ],
  findings: [
    { id: "H03_TOOL_RETRY_THRASH", source: "chat-report", severity: "high", turn: 3, summary: "3 failures shared kind:shell_exec.", hint: "Inspect the first failure and change the next attempt." },
    { id: "H06_PROMPT_OR_TOOL_TOKEN_SPIKE", source: "chat-report", severity: "medium", turn: 2, summary: "Tool output exceeded the 40000-char threshold.", hint: "Limit unbounded tool output." },
    { id: "N01", source: "noether", severity: "medium", turn: 3, summary: "Lint failures were in a file the edit did not touch; retries could not succeed.", hint: "Fix the pre-existing lint error or scope the lint run." },
  ],
  customizations: [
    { path: "~/.cursor/skills/api-conventions/SKILL.md", kind: "skill", referenced: true, attached: false, read: true },
    { path: "~/.cursor/skills/testing-conventions/SKILL.md", kind: "skill", referenced: true, attached: false, read: false },
    { path: "~/.cursor/skills/deploy-checklist/SKILL.md", kind: "skill", referenced: true, attached: false, read: true },
    { path: ".cursor/rules/ts-style.mdc", kind: "rule", referenced: false, attached: true, read: false },
  ],
  subagents: [
    { type: "explore", status: "completed", tool_calls: 22, errors: 4, denials: 0, duration_ms: 96000 },
  ],
  data_quality: [
    { field: "turns[].input_tokens / output_tokens", quality: "excluded", reason: "0 on every turn = not captured." },
    { field: "thinking_blocks[].duration_ms", quality: "excluded", reason: "ms-scale fragments alternate with multi-second blocks." },
    { field: "shell status=error", quality: "adjusted", reason: "2 exit-1 results are benign (rg no-match, fc diff)." },
    { field: "absolute *_at_ms", quality: "adjusted", reason: "Events run past generated_at; relative timing only." },
  ],
};

const SOURCE = `Source: chat-report JSON v2 | ${DATA.subject.session_ids.map((id) => id.slice(0, 8)).join(", ")}`;

function fmt(value: number | null, unit: Unit): string {
  if (value === null) return "--";
  switch (unit) {
    case "%":
      return `${value.toFixed(1)}%`;
    case "ms":
      return value >= 1000 ? `${(value / 1000).toFixed(1)} s` : `${value} ms`;
    case "tokens":
      return value >= 1000 ? `${(value / 1000).toFixed(1)}K` : `${value}`;
    case "ratio":
      return value.toFixed(2);
    case "count":
      return `${value}`;
    default: {
      const unhandled: never = unit;
      return unhandled;
    }
  }
}

const EFFICACY_TONE: Record<Efficacy, TableRowTone | undefined> = {
  met: "success",
  partial: "warning",
  missed: "danger",
  undetermined: undefined,
};

const SEVERITY_TONE: Record<Severity, TableRowTone> = {
  high: "danger",
  medium: "warning",
  low: "info",
  info: "neutral",
};

function headlineTone(m: Metric): StatTone | undefined {
  if (m.id === "findings_high_medium") return m.value === 0 ? "success" : "danger";
  if (m.quality === "adjusted") return "info";
  return undefined;
}

function Caption({ children }: { children: string }) {
  return (
    <Text size="small" tone="tertiary">
      {children}
    </Text>
  );
}

function SingleSession() {
  const headline = DATA.metrics.filter((m) => m.headline && m.quality !== "excluded").slice(0, 5);
  const ctxTotal = DATA.context.categories.reduce((sum, c) => sum + c.tokens, 0);
  const classes = DATA.tool_classes;
  const hasDenied = classes.some((c) => c.denied > 0);
  const toolSeries = [
    { name: "Completed", data: classes.map((c) => c.completed), tone: "success" as const },
    { name: "Benign non-zero exit", data: classes.map((c) => c.error_benign), tone: "info" as const },
    { name: "Error", data: classes.map((c) => c.error), tone: "danger" as const },
    ...(hasDenied ? [{ name: "Denied", data: classes.map((c) => c.denied), tone: "warning" as const }] : []),
  ];

  return (
    <Stack gap={20}>
      {headline.length > 0 && (
        <Grid columns={headline.length} gap={12}>
          {headline.map((m) => (
            <Stat
              key={m.id}
              value={fmt(m.value, m.unit)}
              label={m.quality === "adjusted" ? `${m.label} (adj.)` : m.label}
              tone={headlineTone(m)}
            />
          ))}
        </Grid>
      )}

      {DATA.context.categories.length > 0 && (
        <Stack gap={8}>
          <H2>Context window composition (tokens)</H2>
          <UsageBar
            total={DATA.context.limit ?? ctxTotal}
            topLeftLabel={`${fmt(ctxTotal, "tokens")} used`}
            topRightLabel={DATA.context.limit ? `${fmt(DATA.context.limit, "tokens")} limit` : undefined}
            segments={DATA.context.categories.map((c) => ({ id: c.name, value: c.tokens }))}
          />
          <Grid columns={2} gap={16}>
            <Table
              headers={["Category", "Tokens", "% of used"]}
              columnAlign={["left", "right", "right"]}
              rows={DATA.context.categories.map((c) => [c.name, c.tokens.toLocaleString(), `${ctxTotal > 0 ? ((c.tokens / ctxTotal) * 100).toFixed(1) : "0.0"}%`])}
            />
            {DATA.context.top_nodes.length > 0 && (
              <Table
                headers={["Largest context nodes", "Category", "Tokens"]}
                columnAlign={["left", "left", "right"]}
                rows={DATA.context.top_nodes.map((n) => [n.label, n.category, n.tokens.toLocaleString()])}
              />
            )}
          </Grid>
          <Caption>{`${SOURCE} | session.token_breakdown`}</Caption>
        </Stack>
      )}

      {classes.length > 0 && (
        <Grid columns={2} gap={16}>
          <Card>
            <CardHeader>Tool calls by class and outcome (count)</CardHeader>
            <CardBody>
              <BarChart categories={classes.map((c) => c.name)} series={toolSeries} stacked horizontal />
              <Caption>{`${SOURCE} | loading calls omitted`}</Caption>
            </CardBody>
          </Card>
          <Card>
            <CardHeader>Tool time by class (ms)</CardHeader>
            <CardBody>
              <BarChart
                categories={classes.map((c) => c.name)}
                series={[{ name: "Elapsed", data: classes.map((c) => c.total_ms) }]}
                horizontal
                valueSuffix=" ms"
              />
              <Caption>{`${SOURCE} | sum of tool_calls[].elapsed_ms`}</Caption>
            </CardBody>
          </Card>
        </Grid>
      )}

      {DATA.turns.length > 0 && (
        <Stack gap={8}>
          <H2>Turns</H2>
          <Table
            headers={["Turn", "Bubble", "Duration", "Tool calls", "Errors", "Efficacy (judged)", "Summary"]}
            columnAlign={["right", "left", "right", "right", "right", "left", "left"]}
            rowTone={DATA.turns.map((t) => EFFICACY_TONE[t.efficacy])}
            rows={DATA.turns.map((t) => [t.index, t.bubble_id ? t.bubble_id.slice(0, 8) : "--", fmt(t.duration_ms, "ms"), t.tool_calls, t.errors, t.efficacy, t.summary])}
          />
        </Stack>
      )}

      {DATA.findings.length > 0 && (
        <Stack gap={8}>
          <H2>Findings</H2>
          <Table
            headers={["ID", "Source", "Severity", "Turn", "Summary", "Remediation"]}
            rowTone={DATA.findings.map((f) => SEVERITY_TONE[f.severity])}
            rows={DATA.findings.map((f) => [f.id, f.source, f.severity, f.turn ?? "--", f.summary, f.hint])}
          />
        </Stack>
      )}

      {DATA.customizations.length > 0 && (
        <Stack gap={8}>
          <H2>Customization ledger</H2>
          <Table
            headers={["Path", "Kind", "Referenced", "Attached", "Read"]}
            rowTone={DATA.customizations.map((c) => (c.referenced && !c.read ? "danger" : undefined))}
            rows={DATA.customizations.map((c) => [c.path, c.kind, c.referenced ? "yes" : "no", c.attached ? "yes" : "no", c.read ? "yes" : "no"])}
          />
        </Stack>
      )}

      {DATA.subagents.length > 0 && (
        <Stack gap={8}>
          <H2>Subagents</H2>
          <Table
            headers={["Type", "Status", "Tool calls", "Errors", "Denials", "Duration"]}
            columnAlign={["left", "left", "right", "right", "right", "right"]}
            rowTone={DATA.subagents.map((s) => ((!s.in_flight && s.status !== "completed") || s.errors > 0 || s.denials > 0 ? "warning" : undefined))}
            rows={DATA.subagents.map((s) => [s.type, s.in_flight ? "in_flight" : (s.status ?? "--"), s.tool_calls, s.errors, s.denials, fmt(s.duration_ms, "ms")])}
          />
        </Stack>
      )}
    </Stack>
  );
}

function DiffSection({ diff }: { diff: NonNullable<Metrics["diff"]> }) {
  const f = diff.findings;
  const findingRows = [
    ...f.new.map((id) => [id, "new"]),
    ...f.resolved.map((id) => [id, "resolved"]),
    ...f.persisting.map((id) => [id, "persisting"]),
  ];
  return (
    <Stack gap={16}>
      {diff.metrics.length > 0 && (
        <Card>
          <CardHeader>Before vs after (metric units per label)</CardHeader>
          <CardBody>
            <BarChart
              categories={diff.metrics.map((m) => `${m.label} (${m.unit})`)}
              series={[
                { name: "Before", data: diff.metrics.map((m) => m.before) },
                { name: "After", data: diff.metrics.map((m) => m.after) },
              ]}
              horizontal
              showValues
            />
            <Caption>{`${SOURCE} | chat-report --diff, n=1, confounded by prompt and model variance`}</Caption>
          </CardBody>
        </Card>
      )}
      {findingRows.length > 0 && (
        <Table
          headers={["Finding key", "Change"]}
          rowTone={findingRows.map((r) => (r[1] === "new" ? "danger" : r[1] === "resolved" ? "success" : "warning"))}
          rows={findingRows}
        />
      )}
    </Stack>
  );
}

function AggregateSection({ agg }: { agg: NonNullable<Metrics["aggregate"]> }) {
  return (
    <Stack gap={16}>
      {agg.sessions.length >= 3 && (
        <Card>
          <CardHeader>Tool error rate by session (%)</CardHeader>
          <CardBody>
            <LineChart
              categories={agg.sessions.map((s) => s.id.slice(0, 8))}
              series={[{ name: "Error rate", data: agg.sessions.map((s) => s.error_rate), tone: "danger" }]}
              valueSuffix="%"
            />
            <Caption>{`${SOURCE} | chat-report --aggregate, observational`}</Caption>
          </CardBody>
        </Card>
      )}
      <Grid columns={2} gap={16}>
        {agg.heuristic_frequency.length > 0 && (
          <Card>
            <CardHeader>Findings by heuristic (count across sessions)</CardHeader>
            <CardBody>
              <BarChart
                categories={agg.heuristic_frequency.map((h) => h.id)}
                series={[{ name: "Findings", data: agg.heuristic_frequency.map((h) => h.count) }]}
                horizontal
              />
            </CardBody>
          </Card>
        )}
        {agg.skill_read_frequency.length > 0 && (
          <Card>
            <CardHeader>Skill reads (sessions reading each skill)</CardHeader>
            <CardBody>
              <BarChart
                categories={agg.skill_read_frequency.map((s) => s.path)}
                series={[{ name: "Sessions", data: agg.skill_read_frequency.map((s) => s.count) }]}
                horizontal
              />
            </CardBody>
          </Card>
        )}
      </Grid>
    </Stack>
  );
}

export default function ChatReportDashboard() {
  const verdictTone = DATA.verdict.efficacy === "met" ? "success" : DATA.verdict.efficacy === "missed" ? "danger" : DATA.verdict.efficacy === "partial" ? "warning" : "neutral";
  const meta = [DATA.report_kind, DATA.subject.model, DATA.subject.generated_at].filter(Boolean).join(" | ");

  return (
    <Stack gap={24}>
      <Stack gap={4}>
        <H1>{DATA.subject.title ?? "Chat session analysis"}</H1>
        <Text tone="secondary">{meta}</Text>
      </Stack>

      <Callout tone={verdictTone} title={`Efficacy (judged): ${DATA.verdict.efficacy}`}>
        {DATA.verdict.summary}
      </Callout>

      {DATA.report_kind === "single-session" && <SingleSession />}
      {DATA.diff && <DiffSection diff={DATA.diff} />}
      {DATA.aggregate && <AggregateSection agg={DATA.aggregate} />}

      <Divider />

      <Stack gap={8}>
        <H2>Metric ledger</H2>
        <Table
          headers={["Metric", "Value", "Formula", "Source", "Evidence", "Quality"]}
          columnAlign={["left", "right", "left", "left", "left", "left"]}
          rowTone={DATA.metrics.map((m) => (m.quality === "excluded" ? "neutral" : m.quality === "adjusted" ? "info" : undefined))}
          rows={DATA.metrics.map((m) => [m.label, fmt(m.value, m.unit), m.formula, m.source, m.evidence, m.note ? `${m.quality}: ${m.note}` : m.quality])}
        />
      </Stack>

      {DATA.data_quality.length > 0 && (
        <Callout tone="warning" title="Data quality: adjusted and excluded fields">
          <Stack gap={4}>
            {DATA.data_quality.map((d) => (
              <Text key={d.field} size="small">{`\`${d.field}\` (${d.quality}): ${d.reason}`}</Text>
            ))}
          </Stack>
        </Callout>
      )}
    </Stack>
  );
}
