import AxeBuilder from '@axe-core/playwright';
import { mkdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import {
  expect,
  test,
  type Locator,
  type Page,
  type Request,
  type Response,
} from '@playwright/test';

type IdentityMode = 'local-identity' | 'external-oidc';

interface IdentityConfig {
  readonly mode: IdentityMode;
  readonly baseUrl: string;
  readonly issuer: string;
  readonly identityUrl: string;
  readonly username: string;
  readonly password: string;
  readonly ownerSubject?: string;
}

interface AcceptedRun {
  readonly conversationId: string;
  readonly runId: string;
}

type RunTimeline = {
  admissionDurationMs?: number;
  coreResponseDurationMs?: number;
  coreTerminalDurationMs?: number;
  visibilityWaitStartedAtMs?: number;
  visibilityDurationMs?: number;
  visibilityReloadDurationMs?: number;
  visibilityResult?: 'rendered' | 'recovered' | 'failed';
  runSelectionDurationMs?: number;
  runSelectionResult?: 'selected' | 'failed';
  traceNavigationDurationMs?: number;
  traceNavigationResult?: 'navigated' | 'failed';
  finalRestProbeDurationMs?: number;
  finalRestProbeStatus?: number;
  firstPublicDetail200AtMs?: number;
};

function monotonicMilliseconds(): number {
  return Number(process.hrtime.bigint() / 1_000_000n);
}

function boundedDuration(start: number, end = monotonicMilliseconds()): number {
  return Math.max(0, Math.min(1_800_000, Math.round(end - start)));
}

function boundedTimeline(timeline: RunTimeline): RunTimeline {
  return Object.fromEntries(
    Object.entries(timeline).map(([key, value]) => [
      key,
      typeof value === 'number' ? Math.max(0, Math.min(1_800_000, Math.round(value))) : value,
    ]),
  ) as RunTimeline;
}

/**
 * This suite is intentionally opt-in. The Compose runner supplies the
 * disposable Core and Observatory services plus fixture-only OIDC credentials.
 * It does not use synthetic telemetry: the Core UI creates the run that the
 * Observatory later observes.
 *
 * Required normal-phase variables:
 *   AURA_OBSERVATORY_FULL_STACK=1
 *   AURA_OBSERVATORY_CORE_BASE_URL (default: http://aura-web:8080)
 *   AURA_OBSERVATORY_CORE_MODEL (default: fixture-chat-2)
 *   AURA_OBSERVATORY_BASE_URL (default: http://observatory.localhost:4300)
 *   AURA_OBSERVATORY_* or existing AURA_E2E_* owner OIDC variables
 *   AURA_OBSERVATORY_OPERATIONS_FULL_STACK=1 only with a public seeded
 *     evaluation/operations fixture (the richer stateful owner scenario)
 *
 * The outage phase is a separate opt-in test. The platform runner must stop
 * only Observatory services before invoking it with
 * AURA_OBSERVATORY_SERVICES_STOPPED=1; Core remains reachable and is then
 * exercised through the same authenticated browser session.
 */
const enabled = process.env['AURA_OBSERVATORY_FULL_STACK'] === '1';
const coreBaseUrl = process.env['AURA_OBSERVATORY_CORE_BASE_URL'] ?? 'http://aura-web:8080';
const coreModel = process.env['AURA_OBSERVATORY_CORE_MODEL'] ?? 'fixture-chat-2';
const observatoryBaseUrl =
  process.env['AURA_OBSERVATORY_BASE_URL'] ?? 'http://observatory.localhost:4300';
const prompt =
  process.env['AURA_OBSERVATORY_RUN_PROMPT'] ?? `AURA_OBSERVATORY_E2E_METADATA_RUN_${Date.now()}`;
const privacySentinel =
  process.env['AURA_OBSERVATORY_PRIVACY_SENTINEL'] ?? 'AURA0090_PRIVACY_SENTINEL';
const evaluationPrivacyMarker = `aura0098-${privacySentinel
  .replace(/[^A-Za-z0-9.+-]/gu, '-')
  .slice(0, 48)}`;
const lifecyclePhase = process.env['AURA_OBSERVATORY_LIFECYCLE_PHASE'] ?? '';
const storageStatePath = process.env['AURA_OBSERVATORY_STORAGE_STATE_PATH'];
// This is a disposable Node-side handoff to the composed harness. It is
// deliberately separate from Playwright storage state and contains only the
// identifiers returned by Core admission, never prompt/response content.
const browserTargetPath = process.env['AURA_E2E_BROWSER_TARGET_FILE'];
const browserArtifactDirectory = process.env['AURA_E2E_BROWSER_ARTIFACT_DIR'];
const composedEvaluationEnabled = process.env['AURA_OBSERVATORY_EVALUATION_FULL_STACK'] === '1';
const operationsFullStackEnabled = process.env['AURA_OBSERVATORY_OPERATIONS_FULL_STACK'] === '1';
let browserTargetIdentity: AcceptedRun | null = null;

async function writeBoundedBrowserArtifact(
  name: string,
  value: Record<string, unknown>,
): Promise<void> {
  if (!browserArtifactDirectory) return;
  try {
    await mkdir(browserArtifactDirectory, { recursive: true, mode: 0o700 });
    await writeFile(join(browserArtifactDirectory, name), JSON.stringify(value), {
      encoding: 'utf8',
      mode: 0o600,
    });
  } catch {
    // Artifact diagnostics must never change the browser assertion outcome.
  }
}

type BoundedCollection = { readonly items?: unknown[]; readonly nextCursor?: unknown };

type RunListProbe = {
  readonly status: number;
  readonly detailStatus: number;
  readonly count: number;
  readonly containsTarget: boolean;
};

type TraceBoundaryDiagnostics = {
  getRunStatus?: number;
  getRunTraceId?: string | null;
  getTraceStatus?: number;
  getTraceRunIdMatch?: boolean;
  getTraceSpanCount?: number;
  getTracePartial?: boolean;
};

type TraceRenderedDiagnostics = {
  selectedRunIdMatch: boolean;
  selectedRunState: 'ready' | 'loading' | 'error' | 'empty' | 'unknown';
  detailPresent: boolean;
  detailTraceIdPresent: boolean;
  tracePresent: boolean;
  traceSpanCount: number;
  errorCategory: 'session' | 'unavailable' | 'retryable' | 'error' | 'unknown' | null;
};

type AnalyticsRequestKind = 'catalogue' | 'query';
type AnalyticsTransportFailure =
  'none' | 'http_4xx' | 'http_5xx' | 'timeout' | 'network' | 'unknown';
type AnalyticsFreshness = 'fresh' | 'stale' | 'no_data' | 'unavailable' | 'unknown';

type AnalyticsResponseMetadata = {
  parserState: 'parsed' | 'mismatch';
  scannedRows: number;
  executionMs: number | null;
  executionLimitMs: number | null;
  partialReasons: string[];
  seriesCount: number;
  breakdownCount: number;
  freshnessState: AnalyticsFreshness;
  freshnessLagMs: number | null;
};

type AnalyticsRequestDiagnostic = {
  kind: AnalyticsRequestKind;
  startedAtMs: number;
  endedAtMs: number | null;
  durationMs: number | null;
  status: number;
  transportFailureCategory: AnalyticsTransportFailure;
  response?: AnalyticsResponseMetadata;
};

type AnalyticsRenderedDiagnostics = {
  state: 'loading' | 'error' | 'stale' | 'ready' | 'empty' | 'unknown';
  displayedErrorClass: 'invalid_query' | 'service_unavailable' | 'malformed' | 'other' | null;
};

type RunVisibilityRecovery = {
  result: 'rendered' | 'recovered' | 'failed';
  durationMs: number;
  reloadDurationMs?: number;
};

function boundedAnalyticsNumber(value: unknown, maximum = 2_000_000_000): number | null {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return null;
  return Math.min(maximum, Math.round(value));
}

function boundedAnalyticsReason(value: unknown): string | null {
  if (typeof value !== 'string' || !/^[a-z][a-z0-9_.-]{0,63}$/u.test(value)) return null;
  return value;
}

function analyticsTransportFailure(status: number, errorText = ''): AnalyticsTransportFailure {
  if (status >= 500) return 'http_5xx';
  if (status >= 400) return 'http_4xx';
  const normalized = errorText.toLowerCase();
  if (normalized.includes('timeout')) return 'timeout';
  if (
    normalized.includes('network') ||
    normalized.includes('disconnect') ||
    normalized.includes('failed')
  )
    return 'network';
  return status === 0 ? 'unknown' : 'none';
}

function analyticsDisplayedErrorClass(
  value: string,
): AnalyticsRenderedDiagnostics['displayedErrorClass'] {
  const normalized = value.toLowerCase();
  if (!normalized.trim()) return null;
  if (normalized.includes('malformed')) return 'malformed';
  if (normalized.includes('invalid') || normalized.includes('rejected')) return 'invalid_query';
  if (
    normalized.includes('unavailable') ||
    normalized.includes('timeout') ||
    normalized.includes('network')
  )
    return 'service_unavailable';
  return 'other';
}

function parseAnalyticsResponseMetadata(value: unknown): AnalyticsResponseMetadata {
  const object = recordValue(value);
  const scan = object ? recordValue(object['scan']) : undefined;
  const series = object?.['series'];
  const breakdowns = object?.['breakdowns'];
  const freshness = object ? recordValue(object['freshness']) : undefined;
  const freshnessValue = freshness?.['state'];
  const freshnessState: AnalyticsFreshness =
    freshnessValue === 'fresh' ||
    freshnessValue === 'stale' ||
    freshnessValue === 'no_data' ||
    freshnessValue === 'unavailable'
      ? freshnessValue
      : 'unknown';
  const scannedRows = boundedAnalyticsNumber(scan?.['scannedRows']);
  const executionMs = boundedAnalyticsNumber(scan?.['executionMs']);
  const executionLimitMs = boundedAnalyticsNumber(scan?.['executionLimitMs']);
  const scanLimit = boundedAnalyticsNumber(scan?.['scanLimit']);
  const partialReasons = Array.isArray(object?.['partialReasons'])
    ? object['partialReasons']
        .slice(0, 16)
        .map(boundedAnalyticsReason)
        .filter((reason): reason is string => reason !== null)
    : [];
  const parsed =
    scan !== undefined &&
    scannedRows !== null &&
    executionMs !== null &&
    executionLimitMs !== null &&
    scanLimit !== null &&
    Array.isArray(series) &&
    Array.isArray(breakdowns) &&
    freshness !== undefined &&
    freshnessState !== 'unknown';
  return {
    parserState: parsed ? 'parsed' : 'mismatch',
    scannedRows: scannedRows ?? 0,
    executionMs,
    executionLimitMs,
    partialReasons,
    seriesCount: Array.isArray(series) ? Math.min(series.length, 10_000) : 0,
    breakdownCount: Array.isArray(breakdowns) ? Math.min(breakdowns.length, 10_000) : 0,
    freshnessState,
    freshnessLagMs: boundedAnalyticsNumber(freshness?.['lagMs']),
  };
}

async function captureAnalyticsRenderedDiagnostics(
  analytics: Locator,
): Promise<AnalyticsRenderedDiagnostics> {
  const loading = (await analytics.locator('.loading-panel').count()) > 0;
  const alert = analytics.getByRole('alert').first();
  const errorText = await alert.innerText().catch(() => '');
  const error = (await alert.count()) > 0 && Boolean(errorText.trim());
  const stale = (await analytics.locator('.state.notice').count()) > 0;
  const ready =
    (await analytics.locator('.query-summary').count()) > 0 &&
    (await analytics.locator('table').count()) > 0;
  const empty = (await analytics.locator('.empty').count()) > 0;
  return {
    state: loading
      ? 'loading'
      : error
        ? 'error'
        : stale
          ? 'stale'
          : ready
            ? 'ready'
            : empty
              ? 'empty'
              : 'unknown',
    displayedErrorClass: analyticsDisplayedErrorClass(errorText),
  };
}

async function ensureRunVisibleWithRecovery(
  page: Page,
  runId: string,
): Promise<RunVisibilityRecovery> {
  const startedAtMs = monotonicMilliseconds();
  const runButton = page.getByRole('button', { name: runId, exact: true });
  try {
    await expect(runButton).toBeVisible({ timeout: 60_000 });
    return { result: 'rendered', durationMs: boundedDuration(startedAtMs) };
  } catch {
    const reloadStartedAtMs = monotonicMilliseconds();
    try {
      await page.reload({ waitUntil: 'domcontentloaded' });
      await expect(
        page.getByRole('heading', { level: 1, name: 'Operational metadata' }),
      ).toBeVisible({ timeout: 15_000 });
      await page.getByRole('button', { name: 'Runs', exact: true }).click({ timeout: 15_000 });
      await expect(runButton).toBeVisible({ timeout: 15_000 });
      return {
        result: 'recovered',
        durationMs: boundedDuration(startedAtMs),
        reloadDurationMs: boundedDuration(reloadStartedAtMs),
      };
    } catch {
      return {
        result: 'failed',
        durationMs: boundedDuration(startedAtMs),
        reloadDurationMs: boundedDuration(reloadStartedAtMs),
      };
    }
  }
}

type RunVisibilityFailureDiagnostics = {
  rest: RunListProbe;
  connectionStatus: string;
  visibleRunButtonCount: number;
};

async function captureRunVisibilityFailureDiagnostics(
  page: Page,
  runId: string,
): Promise<RunVisibilityFailureDiagnostics> {
  let rest: RunListProbe = {
    status: 0,
    detailStatus: 0,
    count: 0,
    containsTarget: false,
  };
  try {
    rest = await probeRunList(page, runId);
  } catch {
    // Keep the bounded diagnostic content-free when the public probe cannot start.
  }
  const connectionStatus = (
    await page
      .locator('.connection')
      .innerText()
      .catch(() => 'unavailable')
  )
    .replace(/\s+/gu, ' ')
    .slice(0, 80);
  const visibleRunButtonCount = await page
    .getByRole('button', { name: runId, exact: true })
    .evaluateAll(
      (buttons) =>
        buttons.filter((button) => {
          const style = window.getComputedStyle(button);
          const bounds = button.getBoundingClientRect();
          return (
            style.display !== 'none' &&
            style.visibility !== 'hidden' &&
            bounds.width > 0 &&
            bounds.height > 0
          );
        }).length,
    )
    .catch(() => 0);
  return { rest, connectionStatus, visibleRunButtonCount };
}

function boundedTraceId(value: unknown): string | null {
  return typeof value === 'string' && /^[0-9a-f]{32}$/u.test(value) ? value : null;
}

function boundedHttpStatus(value: number): number {
  return Number.isInteger(value) && value >= 100 && value <= 599 ? value : 0;
}

function boundedErrorCategory(value: string): TraceRenderedDiagnostics['errorCategory'] {
  const normalized = value.toLowerCase();
  if (!normalized.trim()) return null;
  if (normalized.includes('session')) return 'session';
  if (normalized.includes('unavailable')) return 'unavailable';
  if (normalized.includes('retry')) return 'retryable';
  if (normalized.includes('error') || normalized.includes('failed')) return 'error';
  return 'unknown';
}

async function captureTraceRenderedDiagnostics(
  page: Page,
  targetRunId: string,
  detailTraceId: string | null,
): Promise<TraceRenderedDiagnostics> {
  const traceHost = page.locator('observatory-trace');
  const detail = traceHost.locator('.run-card');
  const trace = traceHost.locator('.trace-card');
  const detailPresent = (await detail.count()) > 0;
  const tracePresent = (await trace.count()) > 0;
  const selectedRunId = (
    await detail
      .locator('h3')
      .first()
      .textContent()
      .catch(() => null)
  )?.trim();
  const errorText = await traceHost
    .getByRole('alert')
    .first()
    .innerText()
    .catch(() => '');
  const selectedRunState =
    detailPresent && tracePresent
      ? 'ready'
      : (await traceHost.locator('.trace-skeleton').count()) > 0
        ? 'loading'
        : errorText
          ? 'error'
          : (await traceHost.locator('.empty-state').count()) > 0
            ? 'empty'
            : 'unknown';
  const traceId = (
    await traceHost
      .locator('#trace-id')
      .first()
      .textContent()
      .catch(() => null)
  )?.trim();
  const traceSpanCount = Math.min(
    10_000,
    await traceHost
      .getByRole('list', { name: 'Ordered trace spans' })
      .getByRole('listitem')
      .count(),
  );
  return {
    selectedRunIdMatch: selectedRunId === targetRunId,
    selectedRunState,
    detailPresent,
    detailTraceIdPresent: detailTraceId !== null,
    tracePresent,
    traceSpanCount,
    errorCategory: boundedErrorCategory(errorText),
  };
}

async function probeRunList(page: Page, targetRunId: string): Promise<RunListProbe> {
  const response = await page.request.get(
    new URL('/api/v1/runs?limit=50', observatoryBaseUrl).toString(),
    {
      headers: { Accept: 'application/json', 'Cache-Control': 'no-store' },
      timeout: 5_000,
    },
  );
  let items: unknown[] = [];
  if (response.ok()) {
    items = collectionItems(await response.json());
  }
  const detailResponse = await page.request.get(
    new URL(`/api/v1/runs/${encodeURIComponent(targetRunId)}`, observatoryBaseUrl).toString(),
    {
      headers: { Accept: 'application/json', 'Cache-Control': 'no-store' },
      timeout: 5_000,
    },
  );
  return {
    status: response.status(),
    detailStatus: detailResponse.status(),
    count: items.length,
    containsTarget: items.some((item) => resourceIdentifier(item, 'runId') === targetRunId),
  };
}

async function recordBrowserTarget(accepted: AcceptedRun): Promise<void> {
  if (!browserTargetPath) return;
  const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/u;
  if (!uuidPattern.test(accepted.runId) || !uuidPattern.test(accepted.conversationId)) {
    throw new Error('Core admission returned non-UUID browser target identifiers');
  }
  browserTargetIdentity = accepted;
  await writeFile(
    browserTargetPath,
    JSON.stringify({ conversationId: accepted.conversationId, runId: accepted.runId }),
    { encoding: 'utf8', mode: 0o600 },
  );
}

async function recordBrowserRetentionAction(actionId: string | null): Promise<void> {
  if (!browserTargetPath || !browserTargetIdentity || !actionId) return;
  const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/u;
  if (!uuidPattern.test(actionId)) return;
  await writeFile(
    browserTargetPath,
    JSON.stringify({
      conversationId: browserTargetIdentity.conversationId,
      runId: browserTargetIdentity.runId,
      retentionActionId: actionId,
    }),
    { encoding: 'utf8', mode: 0o600 },
  );
}

async function requirePublicCollection(
  page: Page,
  path: string,
  label: string,
): Promise<unknown[]> {
  const response = await page.request.get(new URL(path, observatoryBaseUrl).toString(), {
    headers: { Accept: 'application/json' },
  });
  if (!response.ok()) {
    throw new Error(
      `AURA-0098 platform blocker: public ${label} collection returned HTTP ${response.status()}`,
    );
  }
  const items = collectionItems(await response.json());
  if (!items.length) {
    throw new Error(`AURA-0098 platform blocker: public ${label} fixture is empty`);
  }
  return items;
}

async function auditResponsiveAccessibility(page: Page, root: string): Promise<void> {
  const original = page.viewportSize() ?? { width: 1440, height: 900 };
  // 320px is the 400%-equivalent reflow of a 1280px desktop viewport.
  for (const viewport of [
    { width: 1024, height: 900 },
    { width: 768, height: 900 },
    { width: 390, height: 844 },
    { width: 320, height: 900 },
  ]) {
    await page.setViewportSize(viewport);
    await page.emulateMedia({ reducedMotion: 'reduce', forcedColors: 'active' });
    const layout = await page.evaluate(() => ({
      documentWidth: document.documentElement.scrollWidth,
      viewportWidth: window.innerWidth,
      reducedMotion: matchMedia('(prefers-reduced-motion: reduce)').matches,
      forcedColors: matchMedia('(forced-colors: active)').matches,
    }));
    expect(layout.documentWidth).toBeLessThanOrEqual(layout.viewportWidth + 1);
    expect(layout.reducedMotion).toBe(true);
    expect(layout.forcedColors).toBe(true);
    await page.keyboard.press('Tab');
    await expect(page.locator(':focus')).toBeVisible();
  }
  await page.setViewportSize(original);
  await page.emulateMedia({ reducedMotion: 'no-preference', forcedColors: 'none' });
  const axe = await new AxeBuilder({ page }).include(root).analyze();
  expect(axe.violations, `${root} accessibility violations`).toEqual([]);
}

function envValue(...names: string[]): string | undefined {
  return names
    .map((name) => process.env[name])
    .find((value) => value !== undefined && value !== '');
}

function identityConfig(
  prefix: 'AURA_E2E_' | 'AURA_OBSERVATORY_',
  baseUrl: string,
): IdentityConfig | undefined {
  const modeValue =
    envValue(`${prefix}IDENTITY_MODE`, 'AURA_E2E_IDENTITY_MODE') ?? 'local-identity';
  const mode: IdentityMode =
    modeValue === 'external' || modeValue === 'external-oidc' ? 'external-oidc' : 'local-identity';
  const modeKey = mode === 'local-identity' ? 'LOCAL' : 'EXTERNAL';
  const value = (suffix: string): string | undefined =>
    envValue(`${prefix}${suffix}`, `AURA_E2E_${modeKey}_${suffix}`, `AURA_E2E_${suffix}`);
  const issuer = value('ISSUER');
  const identityUrl = value('IDENTITY_URL');
  const username = value('OWNER_USERNAME');
  const password = value('OWNER_PASSWORD');
  if (!issuer || !identityUrl || !username || !password) return undefined;
  return {
    mode,
    baseUrl,
    issuer,
    identityUrl,
    username,
    password,
    ownerSubject: value('OWNER_SUBJECT'),
  };
}

async function sessionStatus(
  page: Page,
  config: IdentityConfig,
  path: string,
): Promise<{ status: number; body: Record<string, unknown> }> {
  const response = await page.request.get(new URL(path, config.baseUrl).toString());
  let body: Record<string, unknown> = {};
  try {
    body = (await response.json()) as Record<string, unknown>;
  } catch {
    /* anonymous 401 bodies need not be JSON */
  }
  return { status: response.status(), body };
}

function collectionItems(value: unknown): unknown[] {
  if (!value || typeof value !== 'object') return [];
  const items = (value as BoundedCollection).items;
  return Array.isArray(items) ? items : [];
}

function resourceIdentifier(value: unknown, key: string): string | undefined {
  if (!value || typeof value !== 'object') return undefined;
  const identifier = (value as Record<string, unknown>)[key];
  return typeof identifier === 'string' && identifier.length <= 128 ? identifier : undefined;
}

function recordValue(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

const evaluationTerminalMetadataFields = [
  'attemptId',
  'attemptNumber',
  'artifactCount',
  'claimedAt',
  'completedAt',
  'createdAt',
  'deadlineAt',
  'failureCode',
  'finishedAt',
  'jobId',
  'leaseExpiresAt',
  'resultCount',
  'resultId',
  'retryCount',
  'schedulerId',
  'scheduler_id',
  'startedAt',
  'status',
  'updatedAt',
  'workerId',
  'worker_id',
] as const;

function boundedEvaluationTerminalValue(key: string, value: unknown): string | number | null {
  if (value === null || value === undefined) return null;
  if (typeof value === 'number') {
    return Number.isFinite(value) && value >= 0 && value <= 2_000_000_000
      ? Math.round(value)
      : null;
  }
  if (typeof value !== 'string') return null;
  const bounded = boundedDiagnosticText(value, 128);
  if (!bounded) return null;
  if (key.endsWith('At')) {
    return /^\d{4}-\d{2}-\d{2}T[^\s]{1,64}$/u.test(bounded) ? bounded : null;
  }
  if (
    key === 'status' ||
    key === 'failureCode' ||
    key === 'workerId' ||
    key === 'worker_id' ||
    key === 'schedulerId' ||
    key === 'scheduler_id'
  ) {
    return /^[A-Za-z0-9_.:-]{1,128}$/u.test(bounded) ? bounded : null;
  }
  return /^[0-9a-f-]{1,128}$/iu.test(bounded) ? bounded : null;
}

function boundedEvaluationTerminalMetadata(value: unknown): Record<string, unknown> | null {
  const record = recordValue(value);
  if (!record) return null;
  const result: Record<string, unknown> = {};
  for (const key of evaluationTerminalMetadataFields) {
    if (!(key in record)) continue;
    result[key] = boundedEvaluationTerminalValue(key, record[key]);
  }
  return result;
}

function boundedEvaluationMetadataCollection(value: unknown): Record<string, unknown> {
  const items = collectionItems(value);
  return {
    itemCount: Math.min(items.length, 64),
    items: items.slice(0, 8).map((item) => boundedEvaluationTerminalMetadata(item)),
  };
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/gu, '\\$&');
}

type EvaluationSuiteCheckpoint = {
  readonly captureStatus: 'captured' | 'page-closed' | 'unavailable';
  readonly currentPath: string;
  readonly activeTab: { readonly role: 'tab' | 'combobox' | 'unknown'; readonly name: string };
  readonly heading: { readonly role: 'heading' | 'unknown'; readonly name: string };
  readonly workspaceState: string;
  readonly banner: { readonly role: string; readonly state: string; readonly textLength: number };
  readonly authSession: {
    readonly shellAuthState: 'authenticated' | 'signed-out' | 'checking' | 'unknown';
    readonly sessionRequired: boolean;
    readonly workspaceHasData: boolean;
    readonly suitesPage: {
      readonly present: boolean;
      readonly itemCount: number | null;
      readonly empty: boolean | null;
    };
  };
  readonly suiteRows: ReadonlyArray<{
    readonly role: 'row';
    readonly name: string;
    readonly visible: boolean;
    readonly buttons: ReadonlyArray<{
      readonly role: 'button';
      readonly name: string;
      readonly visible: boolean;
      readonly disabled: boolean;
      readonly ariaDisabled: string | null;
    }>;
  }>;
  readonly suiteRowCount: number;
  readonly suiteButtonCount: number;
  readonly suiteButtonVisibleCount: number;
  readonly suiteButtonDisabledCount: number;
  readonly targetLocator: {
    readonly rowCount: number;
    readonly buttonCount: number;
    readonly buttonVisibleCount: number;
    readonly buttonEnabledCount: number;
  };
};

type EvaluationMilestoneName =
  | 'signInCompletedMs'
  | 'preflightCollectionsCompletedMs'
  | 'pageLoadSseCompletedMs'
  | 'refreshCompletedMs'
  | 'datasetRenderedMs'
  | 'inspectCasesClickedMs'
  | 'suiteTabActionCompletedMs'
  | 'evaluatorTabActionCompletedMs'
  | 'evaluatorSelectionCompletedMs'
  | 'jobsTabActionCompletedMs'
  | 'metadataBoundMs'
  | 'submissionClickedMs';

type EvaluationMilestones = Partial<Record<EvaluationMilestoneName, number>>;

type EvaluationActionCheckpoint = {
  readonly captureStatus: 'captured' | 'page-closed' | 'unavailable';
  readonly currentPath: string;
  readonly activeTab: { readonly role: 'tab' | 'combobox' | 'unknown'; readonly name: string };
  readonly heading: { readonly role: 'heading' | 'unknown'; readonly name: string };
  readonly workspaceState: string;
  readonly authSession: {
    readonly shellAuthState: 'authenticated' | 'signed-out' | 'checking' | 'unknown';
    readonly sessionRequired: boolean;
    readonly workspaceHasData: boolean;
  };
  readonly rowCount: number;
  readonly targetRowCount: number;
  readonly targetButtonCount: number;
  readonly targetButtonVisibleCount: number;
  readonly targetButtonEnabledCount: number;
};

function recordEvaluationMilestone(
  milestones: EvaluationMilestones,
  name: EvaluationMilestoneName,
  scenarioStartedAt: number,
): void {
  milestones[name] = boundedDuration(scenarioStartedAt);
}

function boundedEvaluationMilestones(
  scenarioStartedAt: number,
  milestones: EvaluationMilestones,
): Record<string, number> {
  return {
    scenarioStartedAtMs: 0,
    ...Object.fromEntries(
      Object.entries(milestones).map(([name, elapsedMs]) => [
        name,
        Math.max(0, Math.min(1_800_000, Math.round(elapsedMs ?? 0))),
      ]),
    ),
    totalElapsedMs: boundedDuration(scenarioStartedAt),
  };
}

function boundedDiagnosticText(value: unknown, maximum = 256): string {
  if (typeof value !== 'string') return '';
  return value.replace(/\s+/gu, ' ').trim().slice(0, maximum);
}

function checkpointCaptureStatus(cause: unknown): 'page-closed' | 'unavailable' {
  const message = cause instanceof Error ? cause.message : '';
  return /page, context or browser has been closed|target page.*closed/iu.test(message)
    ? 'page-closed'
    : 'unavailable';
}

function unavailableEvaluationSuiteCheckpoint(
  captureStatus: 'page-closed' | 'unavailable',
): EvaluationSuiteCheckpoint {
  return {
    captureStatus,
    currentPath: 'unavailable',
    activeTab: { role: 'unknown', name: '' },
    heading: { role: 'unknown', name: '' },
    workspaceState: 'unavailable',
    banner: { role: 'none', state: 'unavailable', textLength: 0 },
    authSession: {
      shellAuthState: 'unknown',
      sessionRequired: false,
      workspaceHasData: false,
      suitesPage: { present: false, itemCount: null, empty: null },
    },
    suiteRows: [],
    suiteRowCount: 0,
    suiteButtonCount: 0,
    suiteButtonVisibleCount: 0,
    suiteButtonDisabledCount: 0,
    targetLocator: {
      rowCount: 0,
      buttonCount: 0,
      buttonVisibleCount: 0,
      buttonEnabledCount: 0,
    },
  };
}

async function collectEvaluationSuiteCheckpoint(
  page: Page,
  workspace: Locator,
  suiteId: string,
): Promise<EvaluationSuiteCheckpoint> {
  let domState: Omit<EvaluationSuiteCheckpoint, 'captureStatus' | 'targetLocator'>;
  try {
    domState = await page.evaluate(() => {
      const root = document.querySelector('observatory-evaluation-workspace');
      const shellLayout = document.querySelector('.shell-layout');
      const signedOutState = document.querySelector('.signed-out-state');
      const checkingState = document.querySelector('.signed-out[aria-busy="true"]');
      const visible = (element: Element | null): boolean => {
        if (!(element instanceof HTMLElement)) return false;
        const style = window.getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return (
          style.visibility !== 'hidden' &&
          style.display !== 'none' &&
          rect.width > 0 &&
          rect.height > 0
        );
      };
      const text = (element: Element | null, maximum = 256): string =>
        (element?.textContent ?? '').replace(/\s+/gu, ' ').trim().slice(0, maximum);
      const activeTab = root?.querySelector('[role="tab"][aria-selected="true"]');
      const tabSelect = root?.querySelector('select[aria-label="Evaluation workspace"]');
      const panel = root?.querySelector('[role="tabpanel"]');
      const heading = panel?.querySelector('h2, h3, h4');
      const banner = root?.querySelector('.live-banner');
      const suitesPage = root?.querySelector('section[aria-labelledby="suites-title"]');
      const suiteItemRows = suitesPage
        ? Array.from(suitesPage.querySelectorAll('tbody tr')).filter(
            (row) => !row.classList.contains('empty'),
          )
        : null;
      const workspaceStateValue = root?.getAttribute('data-state') ?? '';
      const suiteRows = Array.from(
        root?.querySelectorAll('section[aria-labelledby="suites-title"] tbody tr') ?? [],
      )
        .slice(0, 16)
        .map((row) => ({
          role: 'row' as const,
          name: text(row),
          visible: visible(row),
          buttons: Array.from(row.querySelectorAll('button'))
            .slice(0, 4)
            .map((button) => ({
              role: 'button' as const,
              name: text(button, 96),
              visible: visible(button),
              disabled: button.hasAttribute('disabled'),
              ariaDisabled: button.getAttribute('aria-disabled'),
            })),
        }));
      const suiteButtons = suiteRows.flatMap((row) => row.buttons);
      const stateClasses = ['partial', 'rejected', 'error', 'loading', 'ready'].filter((name) =>
        banner?.classList.contains(name),
      );
      return {
        currentPath: window.location.pathname.slice(0, 128),
        activeTab: activeTab
          ? { role: 'tab' as const, name: text(activeTab, 96) }
          : tabSelect
            ? { role: 'combobox' as const, name: text(tabSelect, 96) }
            : { role: 'unknown' as const, name: '' },
        heading: heading
          ? { role: 'heading' as const, name: text(heading, 128) }
          : { role: 'unknown' as const, name: '' },
        workspaceState: /^[a-z][a-z0-9_-]{0,31}$/u.test(workspaceStateValue)
          ? workspaceStateValue
          : 'not-exposed',
        banner: {
          role: banner?.getAttribute('role') ?? 'none',
          state: stateClasses.join(',') || 'normal',
          textLength: Math.min(text(banner, 160).length, 160),
        },
        authSession: {
          shellAuthState: shellLayout
            ? ('authenticated' as const)
            : checkingState
              ? ('checking' as const)
              : signedOutState
                ? ('signed-out' as const)
                : ('unknown' as const),
          sessionRequired: signedOutState !== null,
          workspaceHasData: root?.querySelector('tbody tr:not(.empty)') !== null,
          suitesPage: {
            present: suitesPage !== null,
            itemCount: suiteItemRows ? Math.min(suiteItemRows.length, 256) : null,
            empty: suitesPage ? suiteItemRows?.length === 0 : null,
          },
        },
        suiteRows,
        suiteRowCount:
          root?.querySelectorAll('section[aria-labelledby="suites-title"] tbody tr').length ?? 0,
        suiteButtonCount: suiteButtons.length,
        suiteButtonVisibleCount: suiteButtons.filter((button) => button.visible).length,
        suiteButtonDisabledCount: suiteButtons.filter(
          (button) => button.disabled || button.ariaDisabled === 'true',
        ).length,
      };
    });
  } catch (cause) {
    return unavailableEvaluationSuiteCheckpoint(checkpointCaptureStatus(cause));
  }
  try {
    const targetRows = workspace.getByRole('row', {
      name: new RegExp(escapeRegExp(suiteId)),
    });
    const targetRowCount = await targetRows.count();
    let buttonCount = 0;
    let buttonVisibleCount = 0;
    let buttonEnabledCount = 0;
    for (let index = 0; index < targetRowCount && index < 4; index += 1) {
      const button = targetRows
        .nth(index)
        .getByRole('button', { name: 'Use this version', exact: true });
      const count = await button.count();
      buttonCount += count;
      for (let buttonIndex = 0; buttonIndex < count && buttonIndex < 4; buttonIndex += 1) {
        const candidate = button.nth(buttonIndex);
        try {
          if (await candidate.isVisible()) buttonVisibleCount += 1;
        } catch {
          // A detached node is represented by the bounded zero count.
        }
        try {
          if (await candidate.isEnabled()) buttonEnabledCount += 1;
        } catch {
          // A detached node is not actionable.
        }
      }
    }
    return {
      ...domState,
      captureStatus: 'captured',
      suiteRows: domState.suiteRows.map((row) => ({
        ...row,
        name: boundedDiagnosticText(row.name),
        buttons: row.buttons.map((button) => ({
          ...button,
          name: boundedDiagnosticText(button.name, 96),
        })),
      })),
      targetLocator: {
        rowCount: Math.min(targetRowCount, 4),
        buttonCount: Math.min(buttonCount, 4),
        buttonVisibleCount: Math.min(buttonVisibleCount, 4),
        buttonEnabledCount: Math.min(buttonEnabledCount, 4),
      },
    };
  } catch (cause) {
    return {
      ...domState,
      captureStatus: checkpointCaptureStatus(cause),
      suiteRows: domState.suiteRows.map((row) => ({
        ...row,
        name: boundedDiagnosticText(row.name),
        buttons: row.buttons.map((button) => ({
          ...button,
          name: boundedDiagnosticText(button.name, 96),
        })),
      })),
      targetLocator: {
        rowCount: 0,
        buttonCount: 0,
        buttonVisibleCount: 0,
        buttonEnabledCount: 0,
      },
    };
  }
}

async function collectEvaluationActionCheckpoint(
  page: Page,
  workspace: Locator,
  targetId: string,
): Promise<EvaluationActionCheckpoint> {
  let domState: Omit<
    EvaluationActionCheckpoint,
    | 'captureStatus'
    | 'targetRowCount'
    | 'targetButtonCount'
    | 'targetButtonVisibleCount'
    | 'targetButtonEnabledCount'
  >;
  try {
    domState = await page.evaluate(() => {
      const root = document.querySelector('observatory-evaluation-workspace');
      const shellLayout = document.querySelector('.shell-layout');
      const signedOutState = document.querySelector('.signed-out-state');
      const checkingState = document.querySelector('.signed-out[aria-busy="true"]');
      const text = (element: Element | null, maximum = 128): string =>
        (element?.textContent ?? '').replace(/\s+/gu, ' ').trim().slice(0, maximum);
      const activeTab = root?.querySelector('[role="tab"][aria-selected="true"]');
      const tabSelect = root?.querySelector('select[aria-label="Evaluation workspace"]');
      const panel = root?.querySelector('[role="tabpanel"]');
      const heading = panel?.querySelector('h2, h3, h4');
      const workspaceStateValue = root?.getAttribute('data-state') ?? '';
      return {
        currentPath: window.location.pathname.slice(0, 128),
        activeTab: activeTab
          ? { role: 'tab' as const, name: text(activeTab) }
          : tabSelect
            ? { role: 'combobox' as const, name: text(tabSelect) }
            : { role: 'unknown' as const, name: '' },
        heading: heading
          ? { role: 'heading' as const, name: text(heading) }
          : { role: 'unknown' as const, name: '' },
        workspaceState: /^[a-z][a-z0-9_-]{0,31}$/u.test(workspaceStateValue)
          ? workspaceStateValue
          : 'not-exposed',
        authSession: {
          shellAuthState: shellLayout
            ? ('authenticated' as const)
            : checkingState
              ? ('checking' as const)
              : signedOutState
                ? ('signed-out' as const)
                : ('unknown' as const),
          sessionRequired: signedOutState !== null,
          workspaceHasData: root?.querySelector('tbody tr:not(.empty)') !== null,
        },
        rowCount: root?.querySelectorAll('tbody tr').length ?? 0,
      };
    });
  } catch (cause) {
    return {
      captureStatus: checkpointCaptureStatus(cause),
      currentPath: 'unavailable',
      activeTab: { role: 'unknown', name: '' },
      heading: { role: 'unknown', name: '' },
      workspaceState: 'unavailable',
      authSession: {
        shellAuthState: 'unknown',
        sessionRequired: false,
        workspaceHasData: false,
      },
      rowCount: 0,
      targetRowCount: 0,
      targetButtonCount: 0,
      targetButtonVisibleCount: 0,
      targetButtonEnabledCount: 0,
    };
  }

  try {
    const targetRows = workspace.getByRole('row', {
      name: new RegExp(escapeRegExp(targetId)),
    });
    const targetRowCount = await targetRows.count();
    let targetButtonCount = 0;
    let targetButtonVisibleCount = 0;
    let targetButtonEnabledCount = 0;
    for (let index = 0; index < targetRowCount && index < 4; index += 1) {
      const buttons = targetRows
        .nth(index)
        .getByRole('button', { name: 'Use this version', exact: true });
      const count = await buttons.count();
      targetButtonCount += count;
      for (let buttonIndex = 0; buttonIndex < count && buttonIndex < 4; buttonIndex += 1) {
        const button = buttons.nth(buttonIndex);
        try {
          if (await button.isVisible()) targetButtonVisibleCount += 1;
        } catch {
          // A detached node is represented by the bounded zero count.
        }
        try {
          if (await button.isEnabled()) targetButtonEnabledCount += 1;
        } catch {
          // A detached node is not actionable.
        }
      }
    }
    return {
      ...domState,
      captureStatus: 'captured',
      rowCount: Math.min(domState.rowCount, 256),
      targetRowCount: Math.min(targetRowCount, 4),
      targetButtonCount: Math.min(targetButtonCount, 4),
      targetButtonVisibleCount: Math.min(targetButtonVisibleCount, 4),
      targetButtonEnabledCount: Math.min(targetButtonEnabledCount, 4),
    };
  } catch {
    return {
      ...domState,
      captureStatus: 'unavailable',
      rowCount: Math.min(domState.rowCount, 256),
      targetRowCount: 0,
      targetButtonCount: 0,
      targetButtonVisibleCount: 0,
      targetButtonEnabledCount: 0,
    };
  }
}

type BrowserPrivacyScan = {
  readonly eventSafe: boolean;
  readonly urlSafe: boolean;
  readonly storageSafe: boolean;
  readonly cacheSafe: boolean;
  readonly indexedDbSafe: boolean;
  readonly overflowSafe: boolean;
  readonly overflowSources: string[];
  readonly overflowDetails: string[];
};

type SseReplayBoundary = {
  readonly eventType: string;
  readonly resourceId: string;
  readonly replayFromEventId?: string;
};

type ConsoleCapture = {
  readonly messages: string[];
  overflowed: boolean;
};

const maxCapturedConsoleMessages = 10_000;

function captureConsoleMessages(page: Page): ConsoleCapture {
  const capture: ConsoleCapture = { messages: [], overflowed: false };
  page.on('console', (message) => {
    if (capture.messages.length >= maxCapturedConsoleMessages) {
      capture.overflowed = true;
      return;
    }
    capture.messages.push(message.text());
  });
  return capture;
}

async function scanBrowserPrivacySinks(
  page: Page,
  sentinel: string,
  eventPath: string,
  boundary: SseReplayBoundary,
): Promise<BrowserPrivacyScan> {
  return page.evaluate(
    async ({ eventPath: streamPath, sentinel: marker, boundary: expectedBoundary }) => {
      const maxBytes = 256 * 1024;
      const maxEntries = 100;
      const maxDepth = 5;
      let overflowed = false;
      const overflowSources: string[] = [];
      const overflowDetails: string[] = [];
      const markOverflow = (source: string, observed?: number, limit?: number): void => {
        overflowed = true;
        if (!overflowSources.includes(source) && overflowSources.length < 16) {
          overflowSources.push(source);
        }
        if (
          observed !== undefined &&
          limit !== undefined &&
          overflowDetails.length < 16 &&
          !overflowDetails.includes(`${source}:observed=${observed};limit=${limit}`)
        ) {
          overflowDetails.push(`${source}:observed=${observed};limit=${limit}`);
        }
      };
      const containsMarker = (value: string): boolean => value.includes(marker);
      const readBounded = async (
        stream: ReadableStream<Uint8Array> | null,
        source: string,
      ): Promise<string> => {
        if (!stream) return '';
        const reader = stream.getReader();
        const chunks: Uint8Array[] = [];
        let total = 0;
        try {
          while (total < maxBytes) {
            const next = await reader.read();
            if (next.done) break;
            if (!next.value) continue;
            const remaining = maxBytes - total;
            if (next.value.byteLength > remaining) markOverflow(source);
            const chunk =
              next.value.byteLength > remaining ? next.value.slice(0, remaining) : next.value;
            chunks.push(chunk);
            total += chunk.byteLength;
            if (total >= maxBytes) {
              markOverflow(source);
              break;
            }
          }
        } finally {
          await reader.cancel().catch(() => undefined);
        }
        const bytes = new Uint8Array(total);
        let offset = 0;
        for (const chunk of chunks) {
          bytes.set(chunk, offset);
          offset += chunk.byteLength;
        }
        return new TextDecoder().decode(bytes);
      };
      const readSseBounded = async (
        response: Response,
      ): Promise<{ text: string; valid: boolean; markerObserved: boolean }> => {
        const contentType = response.headers.get('content-type')?.toLowerCase() ?? '';
        if (!response.ok || !contentType.includes('text/event-stream') || !response.body) {
          markOverflow('sse.response');
          return { text: '', valid: false, markerObserved: false };
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        const deadline = performance.now() + 3_000;
        let text = '';
        let markerObserved = false;
        let expectedBoundaryObserved = false;

        const hasExpectedBoundary = (value: string): boolean =>
          value
            .split(/\r?\n\r?\n/u)
            .slice(0, -1)
            .some((event) => {
              const eventName = /^event:\s*(.+)$/mu.exec(event)?.[1]?.trim();
              if (eventName !== expectedBoundary.eventType) return false;
              const eventId = /^id:\s*(.+)$/mu.exec(event)?.[1]?.trim();
              if (eventId === expectedBoundary.resourceId) return true;
              const dataLine = /^data:\s*(.+)$/mu.exec(event)?.[1]?.trim();
              if (!dataLine) return false;
              try {
                const payload = JSON.parse(dataLine) as { resourceId?: unknown };
                return payload.resourceId === expectedBoundary.resourceId;
              } catch {
                return false;
              }
            });

        try {
          while (performance.now() < deadline) {
            let timeoutId: number | undefined;
            const remainingMs = Math.max(1, deadline - performance.now());
            let next: ReadableStreamReadResult<Uint8Array>;
            try {
              next = await Promise.race([
                reader.read(),
                new Promise<never>((_, reject) => {
                  timeoutId = window.setTimeout(
                    () => reject(new Error('bounded SSE read timeout')),
                    Math.min(1_000, remainingMs),
                  );
                }),
              ]);
            } finally {
              if (timeoutId !== undefined) window.clearTimeout(timeoutId);
            }
            if (next.done) break;
            if (!next.value) continue;

            text += decoder.decode(next.value, { stream: true });
            if (containsMarker(text)) markerObserved = true;
            if (hasExpectedBoundary(text)) {
              // A live SSE stream has no terminal response. The persisted
              // change event for the seeded resource is the intentional,
              // bounded replay boundary; an earlier snapshot/event is not
              // sufficient evidence that this stream was inspected.
              expectedBoundaryObserved = true;
              break;
            }
            // SSE replay can contain many bounded metadata-only events. Scan
            // every decoded chunk for the sentinel, but retain only a bounded
            // suffix for delimiter/boundary detection. The discarded prefix
            // has already been inspected and is therefore not an overflow.
            if (text.length > maxBytes) text = text.slice(-maxBytes);
          }
          text += decoder.decode();
          if (containsMarker(text)) markerObserved = true;
          if (!expectedBoundaryObserved) markOverflow('sse.boundary');
          return {
            text,
            valid: expectedBoundaryObserved,
            markerObserved,
          };
        } catch {
          markOverflow('sse.read');
          return { text, valid: false, markerObserved };
        } finally {
          await reader.cancel().catch(() => undefined);
        }
      };
      const valueContainsMarker = async (value: unknown, depth = 0): Promise<boolean> => {
        if (value === null || value === undefined) return false;
        if (depth > maxDepth) {
          markOverflow('indexeddb.depth', depth, maxDepth);
          return false;
        }
        if (typeof value === 'string') {
          if (value.length > maxBytes)
            markOverflow('indexeddb.value.bytes', value.length, maxBytes);
          return containsMarker(value.slice(0, maxBytes));
        }
        if (typeof value === 'number' || typeof value === 'boolean' || typeof value === 'bigint')
          return containsMarker(String(value));
        if (value instanceof Blob) {
          if (value.size > maxBytes) markOverflow('indexeddb.value.bytes', value.size, maxBytes);
          return containsMarker((await value.slice(0, maxBytes).text()).slice(0, maxBytes));
        }
        if (value instanceof ArrayBuffer) {
          if (value.byteLength > maxBytes)
            markOverflow('indexeddb.value.bytes', value.byteLength, maxBytes);
          return containsMarker(new TextDecoder().decode(value.slice(0, maxBytes)));
        }
        if (ArrayBuffer.isView(value)) {
          if (value.byteLength > maxBytes)
            markOverflow('indexeddb.value.bytes', value.byteLength, maxBytes);
          return containsMarker(
            new TextDecoder().decode(
              value.buffer.slice(
                value.byteOffset,
                value.byteOffset + Math.min(value.byteLength, maxBytes),
              ),
            ),
          );
        }
        if (Array.isArray(value)) {
          if (value.length > maxEntries)
            markOverflow('indexeddb.entries', value.length, maxEntries);
          for (const item of value.slice(0, maxEntries))
            if (await valueContainsMarker(item, depth + 1)) return true;
          return false;
        }
        if (typeof value !== 'object') return false;
        const entries = Object.entries(value);
        if (entries.length > maxEntries)
          markOverflow('indexeddb.entries', entries.length, maxEntries);
        for (const [key, item] of entries.slice(0, maxEntries)) {
          if (containsMarker(key) || (await valueContainsMarker(item, depth + 1))) return true;
        }
        return false;
      };

      let eventSafe = true;
      try {
        const response = await fetch(streamPath, {
          credentials: 'same-origin',
          headers: {
            Accept: 'text/event-stream',
            ...(expectedBoundary.replayFromEventId
              ? { 'Last-Event-ID': expectedBoundary.replayFromEventId }
              : {}),
          },
        });
        const sample = await readSseBounded(response);
        eventSafe = sample.valid && !sample.markerObserved;
      } catch {
        markOverflow('sse.read');
        eventSafe = false;
      }

      const resourceEntries = performance.getEntriesByType('resource');
      // Performance Resource Timing entries are metadata-only URL values. The
      // browser supplies a finite page-local buffer, so inspect every entry
      // in place instead of applying the collection cap used for persisted
      // storage records and silently omitting URLs after the first 100.
      let urlSafe = !containsMarker(location.href);
      for (const entry of resourceEntries) {
        if (entry.name.length > maxBytes)
          markOverflow('resources.bytes', entry.name.length, maxBytes);
        if (containsMarker(entry.name.slice(0, maxBytes))) urlSafe = false;
      }
      let storageSafe = true;
      for (const storage of [localStorage, sessionStorage]) {
        const keys = Object.keys(storage);
        if (keys.length > maxEntries) markOverflow('storage.entries', keys.length, maxEntries);
        for (const key of keys.slice(0, maxEntries)) {
          const value = storage.getItem(key) ?? '';
          if (key.length > maxBytes) markOverflow('storage.bytes', key.length, maxBytes);
          if (value.length > maxBytes) markOverflow('storage.bytes', value.length, maxBytes);
          if (containsMarker(key) || containsMarker(value)) storageSafe = false;
        }
      }
      let cacheSafe = true;
      try {
        if ('caches' in window) {
          const cacheNames = await caches.keys();
          if (cacheNames.length > 20) markOverflow('cache.names', cacheNames.length, 20);
          for (const cacheName of cacheNames.slice(0, 20)) {
            if (containsMarker(cacheName)) cacheSafe = false;
            const cache = await caches.open(cacheName);
            const requests = await cache.keys();
            if (requests.length > maxEntries)
              markOverflow('cache.entries', requests.length, maxEntries);
            for (const request of requests.slice(0, maxEntries)) {
              if (containsMarker(request.url)) cacheSafe = false;
              const response = await cache.match(request);
              if (
                response &&
                containsMarker(await readBounded(response.clone().body, 'cache.bytes'))
              )
                cacheSafe = false;
            }
          }
        }
      } catch {
        cacheSafe = false;
        markOverflow('cache.read');
      }

      let indexedDbSafe = true;
      try {
        if ('indexedDB' in window && 'databases' in indexedDB) {
          const databases = await indexedDB.databases();
          if (databases.length > 20) markOverflow('indexeddb.databases', databases.length, 20);
          for (const info of databases.slice(0, 20)) {
            if (!info.name) continue;
            if (containsMarker(info.name)) indexedDbSafe = false;
            const database = await new Promise<IDBDatabase>((resolve, reject) => {
              const request = indexedDB.open(info.name as string);
              request.onsuccess = () => resolve(request.result);
              request.onerror = () => reject(request.error);
            });
            try {
              const storeNames = Array.from(database.objectStoreNames);
              if (storeNames.length > 20) markOverflow('indexeddb.stores', storeNames.length, 20);
              for (const storeName of storeNames.slice(0, 20)) {
                if (containsMarker(storeName)) indexedDbSafe = false;
                const records = await new Promise<unknown[]>((resolve, reject) => {
                  const transaction = database.transaction(storeName, 'readonly');
                  const objectStore = transaction.objectStore(storeName);
                  const countRequest = objectStore.count();
                  countRequest.onsuccess = () => {
                    if (countRequest.result > maxEntries)
                      markOverflow('indexeddb.records', countRequest.result, maxEntries);
                    const request = objectStore.getAll(undefined, maxEntries);
                    request.onsuccess = () => resolve(request.result as unknown[]);
                    request.onerror = () => reject(request.error);
                  };
                  countRequest.onerror = () => reject(countRequest.error);
                });
                for (const record of records)
                  if (await valueContainsMarker(record)) indexedDbSafe = false;
              }
            } finally {
              database.close();
            }
          }
        }
      } catch {
        indexedDbSafe = false;
        markOverflow('indexeddb.read');
      }

      return {
        eventSafe,
        urlSafe,
        storageSafe,
        cacheSafe,
        indexedDbSafe,
        overflowSafe: !overflowed,
        overflowSources,
        overflowDetails,
      };
    },
    { eventPath, sentinel, boundary },
  );
}

async function requireSeededEvaluationCollection(
  page: Page,
  collection: string,
): Promise<unknown[]> {
  const response = await page.request.get(
    new URL(`/api/v1/evaluation/${collection}`, observatoryBaseUrl).toString(),
    {
      headers: { Accept: 'application/json' },
    },
  );
  if (!response.ok()) {
    throw new Error(
      `AURA-0098 platform blocker: public evaluation ${collection} route returned ${response.status()}; ` +
        'the composed harness must seed definitions through an authorized public workflow',
    );
  }
  const items = collectionItems(await response.json());
  if (items.length === 0) {
    throw new Error(
      `AURA-0098 platform blocker: public evaluation ${collection} route returned no definitions; ` +
        'browser acceptance cannot use a private repository seam',
    );
  }
  return items;
}

async function signIn(
  page: Page,
  config: IdentityConfig,
  sessionPath: string,
  sessionKind: 'observatory' | 'core',
): Promise<void> {
  const before = await sessionStatus(page, config, sessionPath);
  if (before.status === 200) return;
  expect([401, 403], `owner session should be anonymous before ${config.mode} login`).toContain(
    before.status,
  );

  const loginUrl = new URL('/api/v1/auth/login', config.baseUrl);
  loginUrl.searchParams.set('return_to', '/');
  await page.goto(loginUrl.toString(), { waitUntil: 'domcontentloaded' });
  await page.waitForURL((url) => url.origin === new URL(config.identityUrl).origin, {
    timeout: 30_000,
  });

  if (config.mode === 'local-identity') {
    const username = page.locator('input[name="uidField"]');
    await expect(username).toBeVisible({ timeout: 15_000 });
    await username.fill(config.username);
    await page.getByRole('button', { name: /^log in$/i }).click();
    const password = page.locator('#ak-stage-password-input');
    await expect(password).toBeVisible({ timeout: 15_000 });
    await password.fill(config.password);
    await page.getByRole('button', { name: /^continue$/i }).click();
  } else {
    const username = page.locator('input[name="username"]');
    const password = page.locator('input[name="password"]');
    await expect(username).toBeVisible({ timeout: 15_000 });
    await username.fill(config.username);
    if (await password.isVisible()) {
      await password.fill(config.password);
      await password.press('Enter');
    } else {
      await username.press('Enter');
      await expect(password).toBeVisible({ timeout: 15_000 });
      await password.fill(config.password);
      await password.press('Enter');
    }
  }

  const baseOrigin = new URL(config.baseUrl).origin;
  await page.waitForURL((url) => url.origin === baseOrigin, { timeout: 30_000 });
  await expect
    .poll(async () => (await sessionStatus(page, config, sessionPath)).status, { timeout: 15_000 })
    .toBe(200);
  const session = await sessionStatus(page, config, sessionPath);
  if (sessionKind === 'observatory') {
    const csrfToken = session.body['csrfToken'];
    expect(typeof csrfToken).toBe('string');
    expect((csrfToken as string).length).toBeGreaterThanOrEqual(32);
    expect((csrfToken as string).length).toBeLessThanOrEqual(256);
    return;
  }
  const principal = (session.body['principal'] ?? session.body) as {
    issuer?: string;
    subject?: string;
  };
  expect(principal.issuer).toBe(config.issuer);
  expect(principal.subject).toBeTruthy();
  if (config.ownerSubject) expect(principal.subject).toBe(config.ownerSubject);
}

async function createCoreRun(
  corePage: Page,
  config: IdentityConfig,
  runPrompt: string,
  timeline?: RunTimeline,
): Promise<AcceptedRun> {
  await corePage.goto(config.baseUrl, { waitUntil: 'domcontentloaded' });
  await expect(corePage.getByRole('status', { name: 'Loading your conversation' })).toBeHidden({
    timeout: 30_000,
  });
  const newConversation = corePage.getByRole('button', { name: 'New conversation', exact: true });
  await expect(newConversation).toBeVisible();
  await newConversation.click();
  await corePage.waitForURL((url) => url.pathname === '/conversation', { timeout: 15_000 });
  const composerForm = corePage.locator('form.composer-wrap');
  // The textarea is intentionally rendered before asynchronous model and
  // agent hydration settles.  Filling during that window can be overwritten
  // by the first ready-state reconciliation, leaving the send control
  // disabled even though the control was initially editable.
  await expect(composerForm).toHaveAttribute('data-conversation-ready', 'true', {
    timeout: 30_000,
  });
  const composer = corePage.getByRole('textbox', { name: 'Message Aura', exact: true });
  await expect(composer).toBeVisible();
  await expect(composer).toBeEditable();
  const sendButton = corePage.getByRole('button', { name: /^send$/i, exact: true });
  await expect(
    corePage.getByRole('heading', { name: 'New conversation', exact: true }),
  ).toBeVisible();
  await expect(composer).toHaveValue('');
  await expect(sendButton).toBeDisabled();
  const optionsButton = corePage.getByRole('button', { name: 'Conversation options', exact: true });
  const optionsMenu = corePage.getByRole('dialog', { name: 'Conversation options', exact: true });
  let optionsOpenError: unknown;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      await expect(optionsButton).toBeVisible({ timeout: 5_000 });
      await optionsButton.click();
      // The composer can be replaced during the first conversation render;
      // assert the accessible dialog before querying controls inside it.
      await expect(optionsMenu).toBeVisible({ timeout: 5_000 });
      optionsOpenError = undefined;
      break;
    } catch (error) {
      optionsOpenError = error;
      if (await optionsMenu.isVisible().catch(() => false)) break;
      if (attempt === 2) {
        const buttonState = await optionsButton
          .evaluate((button) => ({
            disabled: (button as HTMLButtonElement).disabled,
            ariaExpanded: button.getAttribute('aria-expanded'),
          }))
          .catch(() => ({ disabled: false, ariaExpanded: null }));
        throw new Error(
          `Conversation options did not open after bounded retries (path=${new URL(corePage.url()).pathname}, button=${JSON.stringify(buttonState)}, dialogs=${await optionsMenu.count()}; ${String(error).replace(/\s+/gu, ' ').slice(0, 240)})`,
        );
      }
    }
  }
  if (optionsOpenError && !(await optionsMenu.isVisible().catch(() => false)))
    throw optionsOpenError;
  const modelSelect = optionsMenu.getByLabel('Model');
  await expect(modelSelect).toBeVisible({ timeout: 15_000 });
  await expect(modelSelect.locator(`option[value="${coreModel}"]`)).toHaveCount(1);
  await modelSelect.selectOption(coreModel);
  await optionsMenu.getByRole('button', { name: 'Close', exact: true }).click();
  await expect(optionsMenu).toBeHidden({ timeout: 15_000 });
  await expect(
    corePage.getByRole('button', { name: `Model ${coreModel}`, exact: true }),
  ).toBeVisible();
  await composer.fill(runPrompt);
  await expect(composer).toHaveValue(runPrompt);
  const sendDiagnostics = async (): Promise<string> =>
    JSON.stringify({
      path: new URL(corePage.url()).pathname,
      conversationReady: await composerForm.getAttribute('data-conversation-ready'),
      sendEnabled: await sendButton.isEnabled().catch(() => false),
      composerEditable: await composer.isEditable().catch(() => false),
      composerLength: await composer
        .inputValue()
        .then((value) => value.length)
        .catch(() => -1),
      selectedModelVisible: await corePage
        .getByRole('button', { name: `Model ${coreModel}`, exact: true })
        .isVisible()
        .catch(() => false),
    });
  await expect(sendButton).toBeVisible();
  try {
    await expect(sendButton).toBeEnabled({ timeout: 30_000 });
  } catch {
    throw new Error(
      `Core send remained disabled after filling the composer (${await sendDiagnostics()})`,
    );
  }
  const createResponse = corePage.waitForResponse(
    (response) =>
      response.request().method() === 'POST' &&
      new URL(response.url()).pathname === '/api/v1/conversations',
    { timeout: 30_000 },
  );
  const admissionStartedAt = monotonicMilliseconds();
  try {
    await sendButton.click();
  } catch (error) {
    void createResponse.catch(() => undefined);
    const clickError = String(error).replace(/\s+/gu, ' ').slice(0, 240);
    throw new Error(
      `Core send click was not actionable (${clickError}; ${await sendDiagnostics()})`,
    );
  }
  let response: Awaited<typeof createResponse>;
  try {
    response = await createResponse;
  } catch {
    throw new Error(
      `Core conversation POST was not observed after send (${await sendDiagnostics()})`,
    );
  }
  const admissionCompletedAt = monotonicMilliseconds();
  if (timeline) {
    timeline.admissionDurationMs = boundedDuration(admissionStartedAt, admissionCompletedAt);
  }
  const admissionStatus = response.status();
  expect(admissionStatus, `Core conversation admission returned HTTP ${admissionStatus}`).toBe(202);
  const accepted = (await response.json()) as {
    conversation?: { id?: string };
    run?: { id?: string };
  };
  const conversationId = accepted.conversation?.id;
  const runId = accepted.run?.id;
  expect(typeof conversationId).toBe('string');
  expect(typeof runId).toBe('string');
  if (typeof conversationId !== 'string' || typeof runId !== 'string')
    throw new Error('Core did not return conversation/run identifiers');
  await recordBrowserTarget({ conversationId, runId });
  const latestUserMessage = corePage.getByRole('article', { name: 'Your message' }).last();
  await expect(latestUserMessage.getByText(runPrompt, { exact: true })).toBeVisible();
  await expect(corePage.getByRole('article', { name: 'Aura response' }).last()).toBeVisible({
    timeout: 90_000,
  });
  if (timeline) {
    timeline.coreResponseDurationMs = boundedDuration(admissionCompletedAt);
  }
  return { conversationId, runId };
}

async function waitForCoreRun(
  corePage: Page,
  config: IdentityConfig,
  accepted: AcceptedRun,
  timeline?: RunTimeline,
): Promise<void> {
  const terminalStatuses = new Set(['failed', 'canceled', 'cancelled', 'interrupted']);
  const terminalWaitStartedAt = monotonicMilliseconds();
  await expect
    .poll(
      async () => {
        const response = await corePage.request.get(
          new URL(`/api/v1/conversations/${accepted.conversationId}`, config.baseUrl).toString(),
        );
        if (!response.ok()) return `http:${response.status()}`;
        const detail = (await response.json()) as {
          messages?: Array<{
            id?: string;
            role?: string;
            content?: string;
            state?: string;
            runId?: string | null;
          }>;
          recentRuns?: Array<{
            id?: string;
            status?: string;
            assistantMessageId?: string | null;
            toolLifecycleStatus?: string | null;
            toolCallsUsed?: number;
            toolCallLimit?: number;
            error?: { code?: string; message?: string; retryable?: boolean };
          }>;
          currentRun?: {
            id?: string;
            status?: string;
            assistantMessageId?: string | null;
            toolLifecycleStatus?: string | null;
            toolCallsUsed?: number;
            toolCallLimit?: number;
            error?: { code?: string; message?: string; retryable?: boolean };
          } | null;
        };
        const run =
          detail.recentRuns?.find((item) => item.id === accepted.runId) ??
          (detail.currentRun?.id === accepted.runId ? detail.currentRun : undefined);
        if (!run) return 'missing';
        const status = run.status ?? 'unknown';
        if (terminalStatuses.has(status)) {
          const code = run.error?.code?.slice(0, 80) ?? 'unknown';
          const retryable = run.error?.retryable ?? 'unknown';
          const message = (run.error?.message ?? 'unknown').replace(/\s+/gu, ' ').slice(0, 200);
          const assistant = detail.messages?.find(
            (item) =>
              item.id === run.assistantMessageId ||
              (item.role === 'assistant' && item.runId === accepted.runId),
          );
          const assistantState = ['complete', 'partial', 'interrupted', 'failed'].includes(
            assistant?.state ?? '',
          )
            ? assistant?.state
            : 'unknown';
          const assistantContentLength =
            typeof assistant?.content === 'string'
              ? Math.min(assistant.content.length, 1_000_000)
              : 'unknown';
          const toolLifecycle = [
            'awaiting_confirmation',
            'approved',
            'denied',
            'executing',
            'completed',
            'failed',
          ].includes(run.toolLifecycleStatus ?? '')
            ? run.toolLifecycleStatus
            : 'none';
          const boundedToolCount = (value: number | undefined): number | 'unknown' =>
            Number.isInteger(value) && value !== undefined && value >= 0 && value <= 1_000
              ? value
              : 'unknown';
          throw new Error(
            `Core run ${status} (code=${code}; retryable=${retryable}; message=${message}; ` +
              `assistant_state=${assistantState}; assistant_content_length=${assistantContentLength}; ` +
              `tool_lifecycle=${toolLifecycle}; tool_calls_used=${boundedToolCount(run.toolCallsUsed)}; ` +
              `tool_call_limit=${boundedToolCount(run.toolCallLimit)})`,
          );
        }
        return status;
      },
      { timeout: 120_000, intervals: [1_000, 2_000, 5_000] },
    )
    .toBe('completed');
  if (timeline) {
    timeline.coreTerminalDurationMs = boundedDuration(terminalWaitStartedAt);
  }
}

type RetentionStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'unknown';

type RetentionGetObservation = {
  readonly httpStatus: number;
  actionId: string | null;
  actionKind: 'delete_before' | 'purge_all' | null;
  status: RetentionStatus;
  deletedObservationCount: number | null;
  deletedReceiptCount: number | null;
  deletedRunCount: number | null;
  failureCode: string | null;
};

type RetentionDiagnostics = {
  postSeen: boolean;
  postStatus: number | null;
  postActionId: string | null;
  postActionKind: 'delete_before' | 'purge_all' | null;
  postStatusValue: RetentionStatus;
  postDeletedObservationCount: number | null;
  postDeletedReceiptCount: number | null;
  postDeletedRunCount: number | null;
  readonly getResponses: RetentionGetObservation[];
  readonly pendingBodies: Promise<void>[];
  stop: () => void;
  drain: () => Promise<void>;
};

const retentionStatuses = new Set<RetentionStatus>(['queued', 'running', 'succeeded', 'failed']);
type RetentionKind = 'delete_before' | 'purge_all';
const retentionKinds: ReadonlySet<RetentionKind> = new Set(['delete_before', 'purge_all']);

function boundedRetentionActionId(value: unknown): string | null {
  return typeof value === 'string' &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/u.test(value)
    ? value
    : null;
}

function boundedRetentionKind(value: unknown): RetentionKind | null {
  return typeof value === 'string' && retentionKinds.has(value as RetentionKind)
    ? (value as RetentionKind)
    : null;
}

function boundedRetentionCount(value: unknown): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null;
}

function boundedRetentionStatus(value: unknown): RetentionStatus {
  return typeof value === 'string' && retentionStatuses.has(value as RetentionStatus)
    ? (value as RetentionStatus)
    : 'unknown';
}

function boundedRetentionFailureCode(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value !== 'string') return 'present';
  return /^[a-z0-9_-]{1,64}$/iu.test(value) ? value : 'present';
}

function observeRetentionActions(page: Page): RetentionDiagnostics {
  const diagnostics: RetentionDiagnostics = {
    postSeen: false,
    postStatus: null,
    postActionId: null,
    postActionKind: null,
    postStatusValue: 'unknown',
    postDeletedObservationCount: null,
    postDeletedReceiptCount: null,
    postDeletedRunCount: null,
    getResponses: [],
    pendingBodies: [],
    stop: () => undefined,
    drain: async () => undefined,
  };
  let stopped = false;
  const handler = (response: Awaited<ReturnType<Page['waitForResponse']>>): void => {
    let pathname: string;
    try {
      pathname = new URL(response.url()).pathname;
    } catch {
      return;
    }
    const request = response.request();
    if (pathname === '/api/v1/retention/actions' && request.method() === 'POST') {
      diagnostics.postSeen = true;
      diagnostics.postStatus = response.status();
      const bodyTask = response
        .json()
        .then((payload: unknown) => {
          if (!payload || typeof payload !== 'object') return;
          const value = payload as Record<string, unknown>;
          diagnostics.postActionId = boundedRetentionActionId(value['actionId']);
          diagnostics.postActionKind = boundedRetentionKind(value['action']);
          diagnostics.postStatusValue = boundedRetentionStatus(value['status']);
          diagnostics.postDeletedObservationCount = boundedRetentionCount(
            value['deletedObservationCount'],
          );
          diagnostics.postDeletedReceiptCount = boundedRetentionCount(value['deletedReceiptCount']);
          diagnostics.postDeletedRunCount = boundedRetentionCount(value['deletedRunCount']);
        })
        .catch(() => undefined);
      diagnostics.pendingBodies.push(bodyTask);
      return;
    }
    if (
      !diagnostics.postSeen ||
      request.method() !== 'GET' ||
      !/^\/api\/v1\/retention\/actions\/[^/]+$/u.test(pathname)
    )
      return;
    if (diagnostics.getResponses.length >= 20) return;
    const observation: RetentionGetObservation = {
      httpStatus: response.status(),
      actionId: null,
      actionKind: null,
      status: 'unknown',
      deletedObservationCount: null,
      deletedReceiptCount: null,
      deletedRunCount: null,
      failureCode: null,
    };
    diagnostics.getResponses.push(observation);
    if (!response.ok()) return;
    const bodyTask = response
      .json()
      .then((payload: unknown) => {
        if (!payload || typeof payload !== 'object') return;
        const value = payload as Record<string, unknown>;
        observation.actionId = boundedRetentionActionId(value['actionId']);
        observation.actionKind = boundedRetentionKind(value['action']);
        observation.status = boundedRetentionStatus(value.status);
        observation.deletedObservationCount = boundedRetentionCount(
          value['deletedObservationCount'],
        );
        observation.deletedReceiptCount = boundedRetentionCount(value['deletedReceiptCount']);
        observation.deletedRunCount = boundedRetentionCount(value['deletedRunCount']);
        observation.failureCode = boundedRetentionFailureCode(value.failureCode);
      })
      .catch(() => undefined);
    diagnostics.pendingBodies.push(bodyTask);
  };
  page.on('response', handler);
  diagnostics.stop = () => {
    if (stopped) return;
    stopped = true;
    page.off('response', handler);
  };
  diagnostics.drain = async () => {
    await Promise.allSettled(diagnostics.pendingBodies);
  };
  return diagnostics;
}

function retentionDiagnosticSummary(diagnostics: RetentionDiagnostics): string {
  const latest = diagnostics.getResponses.at(-1);
  const evidence = latest ?? {
    actionId: null,
    actionKind: null,
    status: 'unknown' as const,
    deletedObservationCount: null,
    deletedReceiptCount: null,
    deletedRunCount: null,
  };
  const getStatuses =
    diagnostics.getResponses
      .slice(-8)
      .map(
        (item) =>
          `${item.httpStatus}:${item.actionKind ?? 'unknown'}:${item.status}:` +
          `${item.deletedObservationCount ?? 'unknown'}:${item.deletedReceiptCount ?? 'unknown'}:` +
          `${item.deletedRunCount ?? 'unknown'}${item.failureCode ? `:${item.failureCode}` : ''}`,
      )
      .join(',') || 'none';
  return (
    `post_seen=${diagnostics.postSeen}; post_status=${diagnostics.postStatus ?? 'none'}; ` +
    `post_action=${diagnostics.postActionKind ?? 'unknown'}:${diagnostics.postActionId ?? 'unknown'}:` +
    `${diagnostics.postStatusValue}:${diagnostics.postDeletedObservationCount ?? 'unknown'}:` +
    `${diagnostics.postDeletedReceiptCount ?? 'unknown'}:${diagnostics.postDeletedRunCount ?? 'unknown'}; ` +
    `get_count=${diagnostics.getResponses.length}; latest_get=${latest ? `${latest.httpStatus}:${latest.status}` : 'none'}; ` +
    `latest_get_evidence=${evidence.actionKind ?? 'unknown'}:${evidence.actionId ?? 'unknown'}:` +
    `${evidence.status}:${evidence.deletedObservationCount ?? 'unknown'}:` +
    `${evidence.deletedReceiptCount ?? 'unknown'}:${evidence.deletedRunCount ?? 'unknown'}; ` +
    `get_statuses=${getStatuses}`
  );
}

async function waitForRetentionTerminal(
  page: Page,
  diagnostics: RetentionDiagnostics,
): Promise<void> {
  let observedState = 'pending';
  try {
    await expect
      .poll(
        async () => {
          // Do not accept a stale result from the previous retention action;
          // the current POST response is the bounded action boundary.
          if (!diagnostics.postSeen) return observedState;
          const result = page.locator('observatory-retention .result');
          if (await result.isVisible().catch(() => false)) {
            const resultText = (await result.innerText().catch(() => ''))
              .replace(/\s+/gu, ' ')
              .slice(0, 120)
              .toLowerCase();
            if (resultText.includes('succeeded')) {
              observedState = 'succeeded';
              return observedState;
            }
            if (resultText.includes('failed')) {
              observedState = 'failed';
              return observedState;
            }
          }
          if (
            await page
              .locator('observatory-retention .failure')
              .isVisible()
              .catch(() => false)
          ) {
            observedState = 'failure';
            return observedState;
          }
          if (
            await page
              .locator('main[aria-labelledby="auth-recovery-title"]')
              .isVisible()
              .catch(() => false)
          ) {
            observedState = 'session-recovery';
            return observedState;
          }
          return observedState;
        },
        { timeout: 60_000, intervals: [500, 1_000, 2_000] },
      )
      .toBe('succeeded');
  } catch {
    await diagnostics.drain();
    const retentionText =
      (
        await page
          .locator('observatory-retention .progress, observatory-retention .failure')
          .allTextContents()
      )
        .join(' ')
        .replace(/https?:\/\/\S+/giu, '[url]')
        .replace(/[A-Za-z0-9+/=_-]{40,}/gu, '[redacted]')
        .replace(/\s+/gu, ' ')
        .slice(0, 160) || 'none';
    throw new Error(
      `Retention action did not succeed (observed_state=${observedState}; ${retentionDiagnosticSummary(diagnostics)}; ` +
        `retention_text=${retentionText})`,
    );
  } finally {
    diagnostics.stop();
    await diagnostics.drain();
  }
}

async function deletedCount(page: Page, label: string): Promise<number> {
  const value = page
    .locator('observatory-retention .result div')
    .filter({ hasText: label })
    .getByRole('definition');
  await expect(value).toBeVisible();
  return Number.parseInt((await value.textContent())?.trim() ?? '0', 10);
}

async function assertDeletedCountWithDiagnostics(
  page: Page,
  label: string,
  diagnostics: RetentionDiagnostics,
): Promise<void> {
  try {
    await expect(await deletedCount(page, label)).toBeGreaterThan(0);
  } catch {
    await diagnostics.drain();
    const actionId = diagnostics.getResponses.at(-1)?.actionId ?? diagnostics.postActionId ?? null;
    await recordBrowserRetentionAction(actionId);
    throw new Error(
      `Retention deleted-count assertion failed (label=${label}; ` +
        `${retentionDiagnosticSummary(diagnostics)})`,
    );
  }
  const actionId = diagnostics.getResponses.at(-1)?.actionId ?? diagnostics.postActionId ?? null;
  await recordBrowserRetentionAction(actionId);
}

async function cancelThenConfirmRetention(
  page: Page,
  trigger: Locator,
  consequence: RegExp,
): Promise<void> {
  await trigger.click();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toBeVisible();
  await expect(dialog).toContainText(consequence);
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  await expect(trigger).toBeFocused();
  await trigger.click();
  await expect(dialog).toBeVisible();
  const confirm = dialog.getByRole('button', { name: /confirm (delete-before|purge-all)/i });
  await confirm.focus();
  await expect(confirm).toBeFocused();
  await page.keyboard.press('Enter');
}

test.describe('Observatory real Core-to-browser acceptance', () => {
  test.skip(!enabled, 'set AURA_OBSERVATORY_FULL_STACK=1 in the disposable Compose runner');
  test.describe.configure({ mode: 'serial' });

  test('authenticates owner, creates a real Core run, observes it through SSE, inspects measurements, and executes retention', async ({
    page,
    context,
  }) => {
    // This scenario retains three real Core runs plus bounded Observatory
    // visibility, trace, analytics, privacy, and retention phases. Its total
    // budget is deliberately local and finite; individual phase timeouts
    // below remain unchanged.
    test.setTimeout(1_800_000);
    const timeline: RunTimeline = {};
    const timelineOriginMs = monotonicMilliseconds();
    page.setDefaultTimeout(15_000);
    page.setDefaultNavigationTimeout(30_000);
    const observatory = identityConfig('AURA_OBSERVATORY_', observatoryBaseUrl);
    const core = identityConfig('AURA_E2E_', coreBaseUrl);
    test.skip(!observatory || !core, 'provide both Observatory and Core owner OIDC variables');
    if (!observatory || !core) return;

    await signIn(page, observatory, '/api/v1/auth/me', 'observatory');
    const privacyConsoleCapture = captureConsoleMessages(page);
    let sseOpened = false;
    let targetRunIdForTiming: string | null = null;
    let targetTraceIdForBoundary: string | null = null;
    let firstPublicDetail200AtMs: number | undefined;
    const traceBoundary: TraceBoundaryDiagnostics = {};
    const observedRunResponses: Array<{ readonly count: number; readonly runIds: string[] }> = [];
    const runResponseTasks: Promise<void>[] = [];
    const recordRunResponse = (response: Awaited<ReturnType<Page['waitForResponse']>>): void => {
      const request = response.request();
      let pathname: string;
      try {
        pathname = new URL(response.url()).pathname;
      } catch {
        return;
      }
      if (request.method() !== 'GET') return;
      if (
        targetRunIdForTiming &&
        pathname === `/api/v1/runs/${encodeURIComponent(targetRunIdForTiming)}`
      ) {
        traceBoundary.getRunStatus = boundedHttpStatus(response.status());
        if (response.ok()) {
          const task = response
            .json()
            .then((payload: unknown) => {
              const value = payload && typeof payload === 'object' ? payload : {};
              targetTraceIdForBoundary = boundedTraceId((value as { traceId?: unknown }).traceId);
              traceBoundary.getRunTraceId = targetTraceIdForBoundary;
            })
            .catch(() => {
              traceBoundary.getRunTraceId = null;
            });
          runResponseTasks.push(task);
        } else {
          traceBoundary.getRunTraceId = null;
        }
      }
      if (targetRunIdForTiming && pathname.startsWith('/api/v1/traces/')) {
        traceBoundary.getTraceStatus = boundedHttpStatus(response.status());
        if (response.ok()) {
          const task = response
            .json()
            .then((payload: unknown) => {
              const value = payload && typeof payload === 'object' ? payload : {};
              const runId = (value as { runId?: unknown }).runId;
              const spans = (value as { spans?: unknown }).spans;
              traceBoundary.getTraceRunIdMatch = runId === targetRunIdForTiming;
              traceBoundary.getTraceSpanCount = Array.isArray(spans)
                ? Math.min(spans.length, 10_000)
                : 0;
              traceBoundary.getTracePartial = (value as { partial?: unknown }).partial === true;
            })
            .catch(() => {
              traceBoundary.getTraceRunIdMatch = false;
              traceBoundary.getTraceSpanCount = 0;
              traceBoundary.getTracePartial = true;
            });
          runResponseTasks.push(task);
        } else {
          traceBoundary.getTraceRunIdMatch = false;
          traceBoundary.getTraceSpanCount = 0;
          traceBoundary.getTracePartial = true;
        }
      }
      if (!response.ok()) return;
      if (
        targetRunIdForTiming &&
        pathname === `/api/v1/runs/${encodeURIComponent(targetRunIdForTiming)}` &&
        firstPublicDetail200AtMs === undefined
      ) {
        firstPublicDetail200AtMs = boundedDuration(timelineOriginMs);
      }
      if (pathname !== '/api/v1/runs') return;
      const task = response
        .json()
        .then((payload: unknown) => {
          const value =
            payload && typeof payload === 'object' ? (payload as { items?: unknown }) : {};
          const items = Array.isArray(value.items) ? value.items : [];
          const runIds = items.slice(0, 50).flatMap((item: unknown) => {
            if (!item || typeof item !== 'object') return [];
            const runId = (item as { runId?: unknown }).runId;
            return typeof runId === 'string' && runId.length <= 128 ? [runId] : [];
          });
          if (observedRunResponses.length < 100)
            observedRunResponses.push({ count: items.length, runIds });
        })
        .catch(() => undefined);
      runResponseTasks.push(task);
    };
    page.on('response', recordRunResponse);
    const liveEventsResponse = page.waitForResponse(
      (response) =>
        response.request().method() === 'GET' &&
        new URL(response.url()).pathname === '/api/v1/events',
      { timeout: 30_000 },
    );
    await page.goto('/', { waitUntil: 'domcontentloaded' });
    const eventsResponse = await liveEventsResponse;
    expect(eventsResponse.status()).toBe(200);
    sseOpened = true;
    await expect(
      page.getByRole('heading', { level: 1, name: 'Operational metadata' }),
    ).toBeVisible();
    await page.emulateMedia({ reducedMotion: 'reduce', forcedColors: 'active' });
    await page.setViewportSize({ width: 390, height: 844 });
    const compactLayout = await page.evaluate(() => ({
      documentWidth: document.documentElement.scrollWidth,
      viewportWidth: window.innerWidth,
      forcedColors: matchMedia('(forced-colors: active)').matches,
      reducedMotion: matchMedia('(prefers-reduced-motion: reduce)').matches,
    }));
    expect(compactLayout.documentWidth).toBeLessThanOrEqual(compactLayout.viewportWidth + 1);
    expect(compactLayout.forcedColors).toBe(true);
    expect(compactLayout.reducedMotion).toBe(true);
    await page.keyboard.press('Tab');
    await expect(page.locator(':focus')).toBeVisible();
    await page.setViewportSize({ width: 1440, height: 900 });

    const overviewBeforeRunsResponse = await page.request.get(
      new URL('/api/v1/overview', observatoryBaseUrl).toString(),
      { headers: { Accept: 'application/json' } },
    );
    expect(overviewBeforeRunsResponse.ok()).toBe(true);
    const overviewBeforeRuns = recordValue(await overviewBeforeRunsResponse.json());
    const replayFromEventId =
      resourceIdentifier(overviewBeforeRuns, 'latestObservationId') ??
      '00000000-0000-4000-8000-000000000000';

    const corePage = await context.newPage();
    corePage.setDefaultTimeout(15_000);
    corePage.setDefaultNavigationTimeout(30_000);
    await signIn(corePage, core, '/api/v1/auth/session', 'core');
    const accepted = await createCoreRun(corePage, core, prompt, timeline);
    targetRunIdForTiming = accepted.runId;
    await waitForCoreRun(corePage, core, accepted, timeline);

    await page.getByRole('button', { name: 'Runs', exact: true }).click();
    const acceptedRunButton = page.getByRole('button', { name: accepted.runId, exact: true });
    const visibilityWaitStartedAt = monotonicMilliseconds();
    timeline.visibilityWaitStartedAtMs = visibilityWaitStartedAt;
    const visibilityRecovery = await ensureRunVisibleWithRecovery(page, accepted.runId);
    timeline.visibilityDurationMs = visibilityRecovery.durationMs;
    timeline.visibilityReloadDurationMs = visibilityRecovery.reloadDurationMs;
    timeline.visibilityResult = visibilityRecovery.result;
    if (visibilityRecovery.result === 'failed') {
      await Promise.allSettled(runResponseTasks);
      let restDiagnostic: RunListProbe = {
        status: 0,
        detailStatus: 0,
        count: 0,
        containsTarget: false,
      };
      const finalRestProbeStartedAt = monotonicMilliseconds();
      try {
        restDiagnostic = await probeRunList(page, accepted.runId);
      } catch {
        // Keep the diagnostic content-free when the same-origin probe cannot start.
      } finally {
        timeline.finalRestProbeDurationMs = boundedDuration(finalRestProbeStartedAt);
        timeline.finalRestProbeStatus = restDiagnostic.status;
        if (restDiagnostic.detailStatus === 200 && firstPublicDetail200AtMs === undefined) {
          firstPublicDetail200AtMs = boundedDuration(timelineOriginMs);
        }
      }
      timeline.firstPublicDetail200AtMs = firstPublicDetail200AtMs;
      const observedTarget = observedRunResponses.some((response) =>
        response.runIds.includes(accepted.runId),
      );
      const connectionStatus = await page
        .locator('.connection')
        .innerText()
        .catch(() => 'unavailable');
      const visibleRunButtonCount = await acceptedRunButton
        .evaluateAll(
          (buttons) =>
            buttons.filter((button) => {
              const style = window.getComputedStyle(button);
              const bounds = button.getBoundingClientRect();
              return (
                style.display !== 'none' &&
                style.visibility !== 'hidden' &&
                bounds.width > 0 &&
                bounds.height > 0
              );
            }).length,
        )
        .catch(() => 0);
      throw new Error(
        `Observatory run did not render (sse_opened=${sseOpened}; observed_target=${observedTarget}; ` +
          `rest_status=${restDiagnostic.status}; rest_count=${restDiagnostic.count}; ` +
          `detail_status=${restDiagnostic.detailStatus}; ` +
          `rest_contains_target=${restDiagnostic.containsTarget}; ` +
          `connection_status=${connectionStatus.replace(/\s+/gu, ' ').slice(0, 80)}; ` +
          `visible_run_buttons=${visibleRunButtonCount}; ` +
          `timeline=${JSON.stringify(boundedTimeline(timeline))})`,
      );
    }
    await expect(
      page
        .getByRole('status')
        .filter({ hasText: /live updates|disconnected|reconnecting/i })
        .first(),
    ).toBeVisible();
    const runSelectionStartedAt = monotonicMilliseconds();
    try {
      await page.getByRole('button', { name: accepted.runId, exact: true }).click({
        timeout: 15_000,
      });
      timeline.runSelectionDurationMs = boundedDuration(runSelectionStartedAt);
      timeline.runSelectionResult = 'selected';
    } catch {
      timeline.runSelectionDurationMs = boundedDuration(runSelectionStartedAt);
      timeline.runSelectionResult = 'failed';
      throw new Error(
        `Observatory run selection failed (timeline=${JSON.stringify(boundedTimeline(timeline))})`,
      );
    }
    const traceNavigationStartedAt = monotonicMilliseconds();
    try {
      await page
        .getByRole('navigation', { name: 'Observatory sections' })
        .getByRole('button', { name: 'Trace', exact: true })
        .click({ timeout: 15_000 });
      timeline.traceNavigationDurationMs = boundedDuration(traceNavigationStartedAt);
      timeline.traceNavigationResult = 'navigated';
    } catch {
      timeline.traceNavigationDurationMs = boundedDuration(traceNavigationStartedAt);
      timeline.traceNavigationResult = 'failed';
      throw new Error(
        `Observatory Trace navigation failed (timeline=${JSON.stringify(boundedTimeline(timeline))})`,
      );
    }
    const traceWaterfall = page.getByText(/trace waterfall/i);
    try {
      await expect(traceWaterfall).toBeVisible({ timeout: 75_000 });
    } catch {
      await Promise.allSettled(runResponseTasks);
      const renderedTrace = await captureTraceRenderedDiagnostics(
        page,
        accepted.runId,
        traceBoundary.getRunTraceId ?? targetTraceIdForBoundary,
      );
      const traceDiagnostics = {
        path: new URL(page.url()).pathname,
        connectionStatus: await page
          .locator('.connection')
          .innerText()
          .catch(() => 'unavailable'),
        waterfallMatches: await traceWaterfall.count().catch(() => 0),
        spanLists: await page
          .getByRole('list', { name: 'Ordered trace spans' })
          .count()
          .catch(() => 0),
        publicBoundary: traceBoundary,
        renderedState: renderedTrace,
        timeline: boundedTimeline(timeline),
      };
      throw new Error(
        `Trace waterfall did not render within the bounded inspection window (${JSON.stringify(traceDiagnostics)})`,
      );
    }
    const traceList = page.getByRole('list', { name: 'Ordered trace spans' });
    try {
      await expect(traceList).toHaveCount(1);
    } catch {
      await Promise.allSettled(runResponseTasks);
      const renderedTrace = await captureTraceRenderedDiagnostics(
        page,
        accepted.runId,
        traceBoundary.getRunTraceId ?? targetTraceIdForBoundary,
      );
      throw new Error(
        `Trace span boundary did not render exactly once (${JSON.stringify({
          publicBoundary: traceBoundary,
          renderedState: renderedTrace,
          timeline: boundedTimeline(timeline),
        })})`,
      );
    }
    const selectedSpan = traceList.getByRole('listitem').first();
    const componentId = (await selectedSpan.locator('small').textContent())?.trim() ?? '';
    expect(componentId).toMatch(/^aura\./);
    await expect(selectedSpan.getByText(/\d+ ms/)).toBeVisible();
    await expect(selectedSpan.getByText(/succeeded|failed|cancelled|unknown/i)).toBeVisible();

    await page.getByRole('button', { name: 'Components', exact: true }).click();
    const componentCard = page
      .locator('observatory-components article')
      .filter({ hasText: componentId });
    await expect(componentCard).toHaveCount(1, { timeout: 30_000 });
    await expect(componentCard.getByText(/latency|errors?/i).first()).toBeVisible();

    // The default bounded 24-hour analytics query is global operational
    // availability; it has no run_id filter and is intentionally not claimed
    // as run-specific evidence. Capture only bounded request metadata so
    // an initial failure and one explicit rendered Retry/Refresh action are
    // diagnosable without recording aggregate contents.
    const analyticsRequests = new WeakMap<
      Request,
      { readonly kind: AnalyticsRequestKind; readonly startedAtMs: number }
    >();
    const analyticsRequestRecords: Array<{
      readonly request: Request;
      readonly kind: AnalyticsRequestKind;
      readonly startedAtMs: number;
      completed: boolean;
    }> = [];
    const analyticsRequestDiagnostics: AnalyticsRequestDiagnostic[] = [];
    const analyticsResponseTasks: Promise<void>[] = [];
    const analyticsRequestPath = (request: Request): AnalyticsRequestKind | null => {
      try {
        if (request.method() !== 'GET' && request.method() !== 'POST') return null;
        const pathname = new URL(request.url()).pathname;
        if (pathname === '/api/v1/analytics/metrics') return 'catalogue';
        if (pathname === '/api/v1/analytics/query') return 'query';
      } catch {
        return null;
      }
      return null;
    };
    const addAnalyticsDiagnostic = (diagnostic: AnalyticsRequestDiagnostic): void => {
      if (analyticsRequestDiagnostics.length < 16) analyticsRequestDiagnostics.push(diagnostic);
    };
    const recordAnalyticsRequest = (request: Request): void => {
      const kind = analyticsRequestPath(request);
      if (!kind || analyticsRequests.has(request)) return;
      const startedAtMs = monotonicMilliseconds();
      analyticsRequests.set(request, { kind, startedAtMs });
      if (analyticsRequestRecords.length < 16)
        analyticsRequestRecords.push({ request, kind, startedAtMs, completed: false });
    };
    const recordAnalyticsResponse = (response: Response): void => {
      const request = response.request();
      const started = analyticsRequests.get(request);
      if (!started) return;
      const record = analyticsRequestRecords.find((item) => item.request === request);
      if (record) record.completed = true;
      const endedAt = monotonicMilliseconds();
      const status = boundedHttpStatus(response.status());
      const base = {
        kind: started.kind,
        startedAtMs: boundedDuration(timelineOriginMs, started.startedAtMs),
        endedAtMs: boundedDuration(timelineOriginMs, endedAt),
        durationMs: boundedDuration(started.startedAtMs, endedAt),
        status,
        transportFailureCategory: analyticsTransportFailure(status),
      } satisfies AnalyticsRequestDiagnostic;
      if (started.kind !== 'query' || status < 200 || status >= 300) {
        addAnalyticsDiagnostic(base);
        return;
      }
      const task = response
        .json()
        .then((payload: unknown) => {
          addAnalyticsDiagnostic({ ...base, response: parseAnalyticsResponseMetadata(payload) });
        })
        .catch(() => {
          addAnalyticsDiagnostic({
            ...base,
            response: {
              parserState: 'mismatch',
              scannedRows: 0,
              executionMs: null,
              executionLimitMs: null,
              partialReasons: [],
              seriesCount: 0,
              breakdownCount: 0,
              freshnessState: 'unknown',
              freshnessLagMs: null,
            },
          });
        });
      analyticsResponseTasks.push(task);
    };
    const recordAnalyticsFailure = (request: Request): void => {
      const started = analyticsRequests.get(request);
      if (!started) return;
      const record = analyticsRequestRecords.find((item) => item.request === request);
      if (record) record.completed = true;
      const endedAt = monotonicMilliseconds();
      addAnalyticsDiagnostic({
        kind: started.kind,
        startedAtMs: boundedDuration(timelineOriginMs, started.startedAtMs),
        endedAtMs: boundedDuration(timelineOriginMs, endedAt),
        durationMs: boundedDuration(started.startedAtMs, endedAt),
        status: 0,
        transportFailureCategory: analyticsTransportFailure(0, request.failure()?.errorText ?? ''),
      });
    };
    const recordPendingAnalyticsRequests = (): void => {
      for (const record of analyticsRequestRecords) {
        if (record.completed) continue;
        addAnalyticsDiagnostic({
          kind: record.kind,
          startedAtMs: boundedDuration(timelineOriginMs, record.startedAtMs),
          endedAtMs: null,
          durationMs: null,
          status: 0,
          transportFailureCategory: 'timeout',
        });
        record.completed = true;
      }
    };
    page.on('request', recordAnalyticsRequest);
    page.on('response', recordAnalyticsResponse);
    page.on('requestfailed', recordAnalyticsFailure);
    try {
      await page.getByRole('button', { name: 'Analytics', exact: true }).click({ timeout: 15_000 });
      const analytics = page.locator('observatory-analytics');
      await expect(page.getByRole('heading', { name: 'Metric explorer', exact: true })).toBeVisible(
        {
          timeout: 15_000,
        },
      );
      const refreshMetrics = analytics
        .getByRole('button', { name: 'Refresh metrics', exact: true })
        .first();
      await expect(refreshMetrics).toBeEnabled({ timeout: 30_000 });
      const queryResponsesBeforeRefresh = analyticsRequestDiagnostics.filter(
        (diagnostic) => diagnostic.kind === 'query',
      ).length;
      await refreshMetrics.click({ timeout: 15_000 });
      try {
        await expect
          .poll(
            () =>
              analyticsRequestDiagnostics.filter((diagnostic) => diagnostic.kind === 'query')
                .length,
            { timeout: 30_000, intervals: [100, 250, 500, 1_000] },
          )
          .toBeGreaterThan(queryResponsesBeforeRefresh);
        await expect(page.getByText(/rows scanned/i)).toBeVisible({ timeout: 30_000 });
        await expect(
          page.getByRole('table', { name: /data in milliseconds/i }).first(),
        ).toBeVisible({
          timeout: 30_000,
        });
      } catch {
        await Promise.allSettled(analyticsResponseTasks);
        recordPendingAnalyticsRequests();
        const rendered = await captureAnalyticsRenderedDiagnostics(analytics);
        throw new Error(
          `Global operational analytics did not become ready after one explicit refresh (${JSON.stringify(
            {
              requests: analyticsRequestDiagnostics.slice(-12),
              renderedState: rendered,
            },
          )})`,
        );
      }
      await Promise.allSettled(analyticsResponseTasks);
      const latestQuery = [...analyticsRequestDiagnostics]
        .reverse()
        .find((diagnostic) => diagnostic.kind === 'query');
      if (!latestQuery?.response || latestQuery.response.parserState !== 'parsed') {
        const rendered = await captureAnalyticsRenderedDiagnostics(analytics);
        throw new Error(
          `Analytics rendered state did not match a parsed 200 query response (${JSON.stringify({
            requests: analyticsRequestDiagnostics.slice(-12),
            renderedState: rendered,
          })})`,
        );
      }
      await expect(analytics).not.toContainText(/prompt|response|credential|bearer token/i);
    } finally {
      await Promise.allSettled(analyticsResponseTasks);
      page.off('request', recordAnalyticsRequest);
      page.off('response', recordAnalyticsResponse);
      page.off('requestfailed', recordAnalyticsFailure);
    }

    // Create a second real Core run before retention so the comparison path
    // exercises the same production telemetry, ClickHouse query, generated
    // client, and browser workspace as the first run.  Run identifiers must
    // remain in the authenticated POST body, never in a URL or browser store.
    const comparisonRun = await createCoreRun(corePage, core, `${prompt}_COMPARE`);
    await waitForCoreRun(corePage, core, comparisonRun);
    const comparisonRequests: string[] = [];
    const comparisonRequestFingerprints = new Set<string>();
    const recordComparisonRequest = (
      request: Awaited<ReturnType<Page['waitForRequest']>>,
    ): void => {
      try {
        if (
          request.method() === 'POST' &&
          new URL(request.url()).pathname === '/api/v1/run-comparisons/query'
        ) {
          const url = request.url();
          const fingerprint = `${url}\n${request.postData() ?? ''}`;
          if (comparisonRequestFingerprints.has(fingerprint)) return;
          comparisonRequestFingerprints.add(fingerprint);
          comparisonRequests.push(url);
        }
      } catch {
        // Ignore malformed diagnostic URLs without exposing request bodies.
      }
    };
    page.on('request', recordComparisonRequest);
    targetRunIdForTiming = comparisonRun.runId;
    await page.getByRole('button', { name: 'Runs', exact: true }).click();
    const comparisonVisibility = await ensureRunVisibleWithRecovery(page, comparisonRun.runId);
    if (comparisonVisibility.result === 'failed') {
      const comparisonDiagnostics = await captureRunVisibilityFailureDiagnostics(
        page,
        comparisonRun.runId,
      );
      throw new Error(
        `Comparison Core run did not render after bounded reload recovery (${JSON.stringify({
          runId: comparisonRun.runId,
          visibility: comparisonVisibility,
          ...comparisonDiagnostics,
        })})`,
      );
    }
    await page.getByRole('button', { name: 'Compare', exact: true }).click();
    const comparison = page.locator('observatory-comparison');
    await comparison.getByRole('combobox', { name: 'Left run' }).selectOption(accepted.runId);
    await comparison.getByRole('combobox', { name: 'Right run' }).selectOption(comparisonRun.runId);
    await comparison.getByRole('button', { name: 'Compare selected runs', exact: true }).click();
    await expect(
      comparison.getByRole('heading', {
        name: new RegExp(`${accepted.runId} versus ${comparisonRun.runId}`),
      }),
    ).toBeVisible({ timeout: 30_000 });
    await expect(
      comparison.getByRole('region', { name: 'Run comparison summary table' }).getByRole('table'),
    ).toBeVisible();
    await expect(
      comparison
        .getByRole('region', { name: 'Component measurement differences table' })
        .getByRole('table'),
    ).toBeVisible();
    await expect(
      comparison.getByRole('region', { name: 'Aligned trace comparison table' }).getByRole('table'),
    ).toBeVisible();
    // A persisted live event may invalidate the comparison immediately after
    // the explicit submit and cause one bounded refresh.  Wait for that short
    // settle window, then prove the request stream is finite and stable rather
    // than making the acceptance depend on the exact SSE scheduling order.
    await expect
      .poll(() => comparisonRequests.length, {
        timeout: 5_000,
        intervals: [250, 500, 1_000],
      })
      .toBeGreaterThan(0);
    const settledComparisonRequestCount = comparisonRequests.length;
    await page.waitForTimeout(1_000);
    expect(comparisonRequests.length).toBe(settledComparisonRequestCount);
    expect(settledComparisonRequestCount).toBeLessThanOrEqual(2);
    for (const requestUrl of comparisonRequests) {
      expect(requestUrl).not.toContain(accepted.runId);
      expect(requestUrl).not.toContain(comparisonRun.runId);
    }
    const comparisonStorage = await page.evaluate(() => ({
      local: Object.keys(localStorage),
      session: Object.keys(sessionStorage),
      url: location.href,
    }));
    expect(comparisonStorage.local).toEqual([]);
    expect(comparisonStorage.session).toEqual([]);
    expect(comparisonStorage.url).not.toMatch(/run-comparisons|leftRunId|rightRunId/);
    page.off('request', recordComparisonRequest);

    const comparisonDetailResponse = await page.request.get(
      new URL(
        `/api/v1/runs/${encodeURIComponent(comparisonRun.runId)}?page_size=10000`,
        observatoryBaseUrl,
      ).toString(),
      { headers: { Accept: 'application/json' } },
    );
    expect(comparisonDetailResponse.ok()).toBe(true);
    const comparisonDetail = recordValue(await comparisonDetailResponse.json());
    const comparisonObservations = Array.isArray(comparisonDetail?.['observations'])
      ? comparisonDetail['observations']
      : [];
    const expectedObservationId = resourceIdentifier(
      comparisonObservations[comparisonObservations.length - 1],
      'observationId',
    );
    expect(expectedObservationId).toBeTruthy();
    if (!expectedObservationId)
      throw new Error('AURA-0098 Core run detail omitted its persisted observation boundary');

    // Send one non-secret synthetic reference through the real Core run and
    // prove it does not cross the Observatory metadata surfaces.  The page
    // evaluates only booleans so a failing diagnostic can never print it.
    const privacyScan = await scanBrowserPrivacySinks(page, privacySentinel, '/api/v1/events', {
      eventType: 'observation.changed',
      resourceId: expectedObservationId,
      replayFromEventId,
    });
    expect(privacyScan).toEqual({
      eventSafe: true,
      urlSafe: true,
      storageSafe: true,
      cacheSafe: true,
      indexedDbSafe: true,
      overflowSafe: true,
      overflowSources: [],
      overflowDetails: [],
    });
    expect(privacyConsoleCapture.overflowed).toBe(false);
    expect(
      privacyConsoleCapture.messages.some((message) => message.includes(privacySentinel)),
    ).toBe(false);

    await page.getByRole('button', { name: 'Retention', exact: true }).click();
    await expect(
      page.getByRole('heading', { name: 'Delete stored observations', exact: true }),
    ).toBeVisible();
    await page
      .getByRole('textbox', { name: 'Delete observations before (UTC)' })
      .fill('2099-01-01T00:00');
    const cutoffDiagnostics = observeRetentionActions(page);
    await cancelThenConfirmRetention(
      page,
      page.getByRole('button', { name: 'Delete before cutoff', exact: true }),
      /observations before|currently stored observations may be affected|catalogues.*preserved/i,
    );
    await waitForRetentionTerminal(page, cutoffDiagnostics);
    await assertDeletedCountWithDiagnostics(page, 'Deleted observations', cutoffDiagnostics);
    await page.getByRole('button', { name: 'Runs', exact: true }).click();
    await expect(
      page.locator('observatory-runs').getByRole('button', { name: accepted.runId, exact: true }),
    ).toHaveCount(0);

    const purgeRun = await createCoreRun(corePage, core, `${prompt}_PURGE`);
    await waitForCoreRun(corePage, core, purgeRun);
    targetRunIdForTiming = purgeRun.runId;
    await page.getByRole('button', { name: 'Runs', exact: true }).click();
    const purgeVisibility = await ensureRunVisibleWithRecovery(page, purgeRun.runId);
    if (purgeVisibility.result === 'failed') {
      const purgeDiagnostics = await captureRunVisibilityFailureDiagnostics(page, purgeRun.runId);
      throw new Error(
        `Purge Core run did not render after bounded reload recovery (${JSON.stringify({
          runId: purgeRun.runId,
          visibility: purgeVisibility,
          ...purgeDiagnostics,
        })})`,
      );
    }
    await page.getByRole('button', { name: 'Components', exact: true }).click();
    await expect(
      page.locator('observatory-components article').filter({ hasText: componentId }),
    ).toHaveCount(1);

    await page.getByRole('button', { name: 'Retention', exact: true }).click();
    const purgeDiagnostics = observeRetentionActions(page);
    await cancelThenConfirmRetention(
      page,
      page.getByRole('button', { name: 'Purge all observations', exact: true }),
      /Permanently purge all stored observations|currently stored observations may be affected|catalogues.*preserved/i,
    );
    await waitForRetentionTerminal(page, purgeDiagnostics);
    await assertDeletedCountWithDiagnostics(page, 'Deleted observations', purgeDiagnostics);
    await page.getByRole('button', { name: 'Runs', exact: true }).click();
    await expect(
      page.locator('observatory-runs').getByRole('button', { name: purgeRun.runId, exact: true }),
    ).toHaveCount(0);
    await page.getByRole('button', { name: 'Components', exact: true }).click();
    await expect(
      page.locator('observatory-components article').filter({ hasText: componentId }),
    ).toHaveCount(1);
    if (storageStatePath) await context.storageState({ path: storageStatePath });
  });

  test('drives the public evaluation round trip and keeps evaluation data out of browser sinks', async ({
    page,
  }) => {
    // The scenario has two independent 120,000 ms terminal polls. The former
    // 120,000 ms test envelope could not contain both polls plus its bounded
    // sign-in, definition-admission, and workspace setup; reserve 60,000 ms
    // for that setup without changing any individual phase timeout.
    test.setTimeout(300_000);
    test.skip(
      !composedEvaluationEnabled,
      'platform harness must set AURA_OBSERVATORY_EVALUATION_FULL_STACK=1 and seed public evaluation definitions',
    );
    const observatory = identityConfig('AURA_OBSERVATORY_', observatoryBaseUrl);
    test.skip(!observatory, 'provide Observatory owner OIDC variables');
    if (!observatory) return;

    const evaluationScenarioStartedAt = monotonicMilliseconds();
    const evaluationMilestones: EvaluationMilestones = {};
    await signIn(page, observatory, '/api/v1/auth/me', 'observatory');
    recordEvaluationMilestone(
      evaluationMilestones,
      'signInCompletedMs',
      evaluationScenarioStartedAt,
    );
    const privacyConsoleCapture = captureConsoleMessages(page);
    const evaluationUiDiagnostics: Array<Record<string, unknown>> = [];
    const evaluationUiDiagnosticTasks: Promise<void>[] = [];
    const evaluationJobGetDiagnostics: Array<Record<string, unknown>> = [];
    const evaluationTargetIds: {
      datasetId?: string;
      suiteId?: string;
      evaluatorId?: string;
      jobId?: string;
    } = {};
    const recordEvaluationUiResponse = (response: Response): void => {
      try {
        const pathname = new URL(response.url()).pathname;
        if (!pathname.startsWith('/api/v1/evaluation/')) return;
        const task = (async () => {
          const diagnostic: Record<string, unknown> = {
            pathname,
            status: response.status(),
          };
          if (pathname.endsWith('/events')) {
            if (evaluationUiDiagnostics.length < 64) evaluationUiDiagnostics.push(diagnostic);
            return;
          }
          try {
            const body = recordValue(await response.json());
            const bodyRecord = recordValue(body);
            const items = Array.isArray(body) ? body : bodyRecord?.['items'];
            diagnostic.responseKeys = bodyRecord
              ? Object.keys(bodyRecord).sort().slice(0, 32)
              : null;
            diagnostic.itemCount = Array.isArray(items) ? items.length : null;
            if (Array.isArray(items)) {
              diagnostic.itemShapes = items.slice(0, 8).map((item) => {
                const value = recordValue(item);
                const provenance = recordValue(value?.['provenance']);
                return {
                  keys: value ? Object.keys(value).sort().slice(0, 32) : null,
                  jobIdType: typeof value?.['jobId'],
                  statusType: typeof value?.['status'],
                  updatedAtType: typeof value?.['updatedAt'],
                  provenanceKeys: provenance ? Object.keys(provenance).sort().slice(0, 32) : null,
                };
              });
            }
            const resourceKey = pathname.endsWith('/datasets')
              ? 'datasetId'
              : pathname.endsWith('/suites')
                ? 'suiteId'
                : pathname.endsWith('/evaluators')
                  ? 'evaluatorId'
                  : pathname.endsWith('/jobs') && response.request().method() === 'GET'
                    ? 'jobId'
                    : null;
            const targetId = resourceKey ? evaluationTargetIds[resourceKey] : undefined;
            diagnostic.targetPresent =
              resourceKey && targetId && Array.isArray(items)
                ? items.some((item) => resourceIdentifier(item, resourceKey) === targetId)
                : null;
            if (resourceKey === 'jobId' && Array.isArray(items)) {
              const target = items.find((item) => resourceIdentifier(item, 'jobId') === targetId);
              const targetRecord = recordValue(target);
              diagnostic.targetStatus = targetRecord?.['status'] ?? null;
              diagnostic.targetKeys = targetRecord
                ? Object.keys(targetRecord).sort().slice(0, 32)
                : null;
              const provenance = recordValue(targetRecord?.['provenance']);
              diagnostic.targetProvenanceKeys = provenance
                ? Object.keys(provenance).sort().slice(0, 32)
                : null;
            }
          } catch {
            diagnostic.jsonBody = false;
          }
          if (evaluationUiDiagnostics.length < 64) evaluationUiDiagnostics.push(diagnostic);
        })();
        if (evaluationUiDiagnosticTasks.length < 64) evaluationUiDiagnosticTasks.push(task);
      } catch {
        // Keep diagnostics bounded and content-free if a response is malformed.
      }
    };
    const drainEvaluationUiDiagnostics = async (): Promise<void> => {
      const pending = Promise.allSettled(evaluationUiDiagnosticTasks);
      await Promise.race([pending, new Promise<void>((resolve) => setTimeout(resolve, 1_000))]);
    };
    page.on('response', recordEvaluationUiResponse);
    const evaluationEvents = page.waitForResponse(
      (response) =>
        response.request().method() === 'GET' &&
        new URL(response.url()).pathname === '/api/v1/evaluation/events',
      { timeout: 30_000 },
    );
    const datasets = await requireSeededEvaluationCollection(page, 'datasets');
    const suites = await requireSeededEvaluationCollection(page, 'suites');
    const evaluators = await requireSeededEvaluationCollection(page, 'evaluators');
    recordEvaluationMilestone(
      evaluationMilestones,
      'preflightCollectionsCompletedMs',
      evaluationScenarioStartedAt,
    );
    const seededDefinitions = datasets.flatMap((dataset) => {
      const datasetRecord = recordValue(dataset);
      const datasetId = resourceIdentifier(dataset, 'datasetId');
      const datasetVersion = datasetRecord?.['datasetVersion'];
      if (!datasetId || typeof datasetVersion !== 'string') return [];
      const suite = suites.find((candidate) => {
        const suiteRecord = recordValue(candidate);
        const suiteDataset = recordValue(suiteRecord?.['dataset']);
        return (
          suiteDataset?.['resourceId'] === datasetId &&
          suiteDataset?.['resourceVersion'] === datasetVersion
        );
      });
      const suiteEvaluators = recordValue(suite)?.['evaluators'];
      const evaluatorBinding = Array.isArray(suiteEvaluators)
        ? suiteEvaluators.find((binding) => {
            const value = recordValue(binding);
            return (
              typeof value?.['evaluatorId'] === 'string' &&
              typeof value?.['evaluatorVersion'] === 'string'
            );
          })
        : undefined;
      const binding = recordValue(evaluatorBinding);
      const evaluator = evaluators.find((candidate) => {
        const evaluatorId = resourceIdentifier(candidate, 'evaluatorId');
        const evaluatorVersion = recordValue(candidate)?.['evaluatorVersion'];
        return (
          evaluatorId === binding?.['evaluatorId'] &&
          evaluatorVersion === binding?.['evaluatorVersion']
        );
      });
      const suiteId = resourceIdentifier(suite, 'suiteId');
      const evaluatorId = resourceIdentifier(evaluator, 'evaluatorId');
      return suiteId && evaluatorId ? [{ datasetId, suiteId, evaluatorId }] : [];
    })[0];
    if (!seededDefinitions) {
      throw new Error(
        'AURA-0098 platform blocker: public evaluation definitions are not a coherent dataset/suite/evaluator set',
      );
    }
    const { datasetId, suiteId, evaluatorId } = seededDefinitions;
    evaluationTargetIds.datasetId = datasetId;
    evaluationTargetIds.suiteId = suiteId;
    evaluationTargetIds.evaluatorId = evaluatorId;
    expect(datasetId).toBeTruthy();
    expect(suiteId).toBeTruthy();
    expect(evaluatorId).toBeTruthy();

    await page.goto('/', { waitUntil: 'domcontentloaded' });
    expect((await evaluationEvents).status()).toBe(200);
    recordEvaluationMilestone(
      evaluationMilestones,
      'pageLoadSseCompletedMs',
      evaluationScenarioStartedAt,
    );
    await page.getByRole('button', { name: 'Evaluations', exact: true }).click();
    const workspace = page.locator('observatory-evaluation-workspace');
    await expect(
      workspace.getByRole('heading', { name: 'Datasets and cases', exact: true }),
    ).toBeVisible();
    // The evaluation workspace is constructed while the authenticated shell is
    // settling. Refresh through the public owner workflow before asserting the
    // authoritative dataset row so the acceptance does not race that initial
    // load; this also keeps the route and response diagnostics observable.
    await workspace.getByRole('button', { name: 'Refresh', exact: true }).click();
    recordEvaluationMilestone(
      evaluationMilestones,
      'refreshCompletedMs',
      evaluationScenarioStartedAt,
    );
    try {
      await expect(
        workspace.getByRole('row', { name: new RegExp(datasetId as string) }),
      ).toBeVisible();
    } catch (cause) {
      const renderedState = await page.evaluate(() => {
        const workspace = document.querySelector('observatory-evaluation-workspace');
        const metrics =
          workspace
            ?.querySelector('.workspace-metrics')
            ?.textContent?.replace(/\s+/g, ' ')
            .trim() ?? '';
        const rows = workspace?.querySelectorAll('tbody tr').length ?? 0;
        const empty = workspace?.querySelector('.empty') !== null;
        const alerts = Array.from(workspace?.querySelectorAll('[role="alert"]') ?? []).length;
        return { metrics: metrics.slice(0, 240), rows, empty, alerts };
      });
      throw new Error(
        `AURA-0098 evaluation dataset row did not render (${JSON.stringify({
          datasetId,
          uiRequests: evaluationUiDiagnostics.slice(-20),
          renderedState,
        })})`,
        { cause },
      );
    }
    recordEvaluationMilestone(
      evaluationMilestones,
      'datasetRenderedMs',
      evaluationScenarioStartedAt,
    );
    const datasetRow = workspace.getByRole('row', {
      name: new RegExp(escapeRegExp(datasetId as string)),
    });
    await datasetRow.getByRole('button', { name: 'Inspect cases', exact: true }).click();
    recordEvaluationMilestone(
      evaluationMilestones,
      'inspectCasesClickedMs',
      evaluationScenarioStartedAt,
    );
    await expect(
      workspace
        .getByRole('row')
        .filter({ hasText: datasetId as string })
        .first(),
    ).toBeVisible();

    const selectTab = async (name: string, heading: string): Promise<void> => {
      if ((page.viewportSize()?.width ?? 1280) <= 640)
        await workspace
          .getByRole('combobox', { name: 'Evaluation workspace' })
          .selectOption(name, { timeout: 15_000 });
      else
        await workspace
          .getByRole('tab', { name: new RegExp(name, 'i') })
          .click({ timeout: 15_000 });
      await expect(workspace.getByRole('heading', { name: heading, exact: true })).toBeVisible();
    };
    try {
      await selectTab('suites', 'Suite composition');
      await workspace
        .getByRole('row', { name: new RegExp(escapeRegExp(suiteId as string)) })
        .getByRole('button', { name: 'Use this version', exact: true })
        .click({ timeout: 15_000 });
      recordEvaluationMilestone(
        evaluationMilestones,
        'suiteTabActionCompletedMs',
        evaluationScenarioStartedAt,
      );
      await drainEvaluationUiDiagnostics();
      page.off('response', recordEvaluationUiResponse);
    } catch (cause) {
      const checkpoint = await collectEvaluationSuiteCheckpoint(page, workspace, suiteId);
      await drainEvaluationUiDiagnostics();
      const errorText = cause instanceof Error ? cause.message : '';
      const timeoutMatch = /Test timeout of (\d{1,9})ms exceeded/u.exec(errorText);
      const failureKind = timeoutMatch
        ? 'overall-test-timeout'
        : /waiting for locator\(/u.test(errorText)
          ? 'locator-wait'
          : 'other';
      const diagnostic = {
        schemaVersion: 1,
        failurePhase: 'suite-selection',
        failureKind,
        phaseTiming: {
          elapsedMs: boundedDuration(evaluationScenarioStartedAt),
          budgetMs: timeoutMatch ? Number(timeoutMatch[1]) : null,
          budgetExhausted: failureKind === 'overall-test-timeout',
        },
        milestones: boundedEvaluationMilestones(evaluationScenarioStartedAt, evaluationMilestones),
        checkpoint,
        suiteEndpointStatuses: evaluationUiDiagnostics
          .filter((entry) => entry['pathname'] === '/api/v1/evaluation/suites')
          .slice(-16),
      };
      await writeBoundedBrowserArtifact('suite-selection-diagnostic.json', diagnostic);
      page.off('response', recordEvaluationUiResponse);
      throw cause;
    }
    const evaluationActionTimeoutMs = 15_000;
    try {
      await selectTab('evaluators', 'Evaluator bindings');
      recordEvaluationMilestone(
        evaluationMilestones,
        'evaluatorTabActionCompletedMs',
        evaluationScenarioStartedAt,
      );
      await workspace
        .getByRole('row', { name: new RegExp(escapeRegExp(evaluatorId as string)) })
        .getByRole('button', { name: 'Use this version', exact: true })
        .click({ timeout: evaluationActionTimeoutMs });
      recordEvaluationMilestone(
        evaluationMilestones,
        'evaluatorSelectionCompletedMs',
        evaluationScenarioStartedAt,
      );
      await drainEvaluationUiDiagnostics();
    } catch (cause) {
      const checkpoint = await collectEvaluationActionCheckpoint(page, workspace, evaluatorId);
      await drainEvaluationUiDiagnostics();
      const errorText = cause instanceof Error ? cause.message : '';
      const timeoutMatch = /Test timeout of (\d{1,9})ms exceeded/u.exec(errorText);
      const failureKind = timeoutMatch
        ? 'overall-test-timeout'
        : /waiting for locator\(/u.test(errorText)
          ? 'locator-wait'
          : 'other';
      await writeBoundedBrowserArtifact('evaluator-selection-diagnostic.json', {
        schemaVersion: 1,
        failurePhase: 'evaluator-selection',
        failureKind,
        phaseTiming: {
          elapsedMs: boundedDuration(evaluationScenarioStartedAt),
          budgetMs: timeoutMatch ? Number(timeoutMatch[1]) : evaluationActionTimeoutMs,
          budgetExhausted: failureKind === 'overall-test-timeout',
        },
        milestones: boundedEvaluationMilestones(evaluationScenarioStartedAt, evaluationMilestones),
        checkpoint,
        evaluatorEndpointStatuses: evaluationUiDiagnostics
          .filter((entry) => entry['pathname'] === '/api/v1/evaluation/evaluators')
          .slice(-16),
      });
      page.off('response', recordEvaluationUiResponse);
      throw cause;
    }
    await selectTab('jobs', 'Jobs and progress');
    recordEvaluationMilestone(
      evaluationMilestones,
      'jobsTabActionCompletedMs',
      evaluationScenarioStartedAt,
    );

    const modelId = process.env['AURA_OBSERVATORY_EVALUATION_MODEL_ID'] ?? coreModel;
    const modelVersion = process.env['AURA_OBSERVATORY_EVALUATION_MODEL_VERSION'] ?? '1.0.0';
    const modelDigest = process.env['AURA_OBSERVATORY_EVALUATION_MODEL_DIGEST'] ?? '0'.repeat(64);
    const metadataFields: ReadonlyArray<readonly [string, string]> = [
      ['Evaluation model ID', modelId],
      ['Evaluation model version', modelVersion],
      ['Evaluation model digest', modelDigest],
      ['Evaluation deployment revision', 'aura0098-e2e'],
      ['Evaluation reproducibility seed', '98'],
      ['Evaluation fixture revision', 'synthetic-run-coordinator-v1'],
      ['Evaluation prompt revision', evaluationPrivacyMarker],
      ['Evaluation policy revision', 'aura0098-policy'],
    ];
    for (const [label, value] of metadataFields)
      await workspace.getByLabel(label).fill(value, { timeout: evaluationActionTimeoutMs });
    const bindMetadata = workspace.getByRole('button', { name: 'Bind selected metadata' });
    await expect(bindMetadata).toBeEnabled({ timeout: evaluationActionTimeoutMs });
    await bindMetadata.click({ timeout: evaluationActionTimeoutMs });
    recordEvaluationMilestone(evaluationMilestones, 'metadataBoundMs', evaluationScenarioStartedAt);
    const submitResponse = page.waitForResponse(
      (response) =>
        response.request().method() === 'POST' &&
        new URL(response.url()).pathname === '/api/v1/evaluation/jobs',
      { timeout: 30_000 },
    );
    await workspace
      .getByRole('button', { name: 'Submit evaluation' })
      .click({ timeout: evaluationActionTimeoutMs });
    recordEvaluationMilestone(
      evaluationMilestones,
      'submissionClickedMs',
      evaluationScenarioStartedAt,
    );
    const submittedResponse = await submitResponse;
    expect(submittedResponse.status()).toBe(202);
    const submittedJob = recordValue(await submittedResponse.json());
    const submittedJobId = resourceIdentifier(submittedJob, 'jobId');
    expect(submittedJobId).toBeTruthy();
    if (!submittedJobId)
      throw new Error('AURA-0098 evaluation submission omitted its job identifier');
    evaluationTargetIds.jobId = submittedJobId;
    evaluationUiDiagnostics.push({
      pathname: '/api/v1/evaluation/jobs',
      method: 'POST',
      status: submittedResponse.status(),
      submittedJobId,
      submittedJobKeys: Object.keys(submittedJob).sort().slice(0, 32),
      submittedJobProvenanceKeys: recordValue(submittedJob['provenance'])
        ? Object.keys(recordValue(submittedJob['provenance']) ?? {})
            .sort()
            .slice(0, 32)
        : null,
    });

    let terminalJob: Record<string, unknown> | undefined;
    const writeTerminalPollDiagnostic = async (failurePhase: string) => {
      let attemptStatus = 0;
      let attemptPayload: unknown = null;
      try {
        const response = await page.request.get(
          new URL(
            `/api/v1/evaluation/jobs/${encodeURIComponent(submittedJobId)}/attempts?page_size=50`,
            observatoryBaseUrl,
          ).toString(),
          { headers: { Accept: 'application/json' } },
        );
        attemptStatus = response.status();
        if (response.ok()) attemptPayload = await response.json();
      } catch {
        attemptStatus = 0;
      }
      let resultStatus = 0;
      let resultPayload: unknown = null;
      try {
        const response = await page.request.get(
          new URL(
            `/api/v1/evaluation/results?page_size=50&job_id=${encodeURIComponent(submittedJobId)}`,
            observatoryBaseUrl,
          ).toString(),
          { headers: { Accept: 'application/json' } },
        );
        resultStatus = response.status();
        if (response.ok()) resultPayload = await response.json();
      } catch {
        resultStatus = 0;
      }
      await writeBoundedBrowserArtifact('evaluation-terminal-diagnostic.json', {
        schemaVersion: 1,
        failurePhase,
        submittedJobId,
        pollTimedOut: failurePhase === 'terminal-job-poll-timeout',
        terminalJob: boundedEvaluationTerminalMetadata(terminalJob),
        lastJob: boundedEvaluationTerminalMetadata(terminalJob),
        jobGetAttempts: evaluationJobGetDiagnostics.slice(-64),
        attempts: {
          status: attemptStatus,
          ...boundedEvaluationMetadataCollection(attemptPayload),
        },
        results: {
          status: resultStatus,
          ...boundedEvaluationMetadataCollection(resultPayload),
        },
      });
    };
    try {
      await expect
        .poll(
          async () => {
            const response = await page.request.get(
              new URL(
                `/api/v1/evaluation/jobs/${encodeURIComponent(submittedJobId)}`,
                observatoryBaseUrl,
              ).toString(),
              { headers: { Accept: 'application/json' } },
            );
            let candidate: Record<string, unknown> | undefined;
            try {
              candidate = recordValue(await response.json());
            } catch {
              candidate = undefined;
            }
            if (evaluationJobGetDiagnostics.length < 64) {
              evaluationJobGetDiagnostics.push({
                request: 'GET /api/v1/evaluation/jobs/{jobId}',
                status: response.status(),
                metadata: boundedEvaluationTerminalMetadata(candidate),
              });
            }
            if (!response.ok()) return false;
            const status = candidate?.['status'];
            if (candidate) terminalJob = candidate;
            if (!candidate || !['succeeded', 'failed', 'cancelled'].includes(String(status)))
              return false;
            return true;
          },
          { timeout: 120_000, intervals: [500, 1_000, 2_000, 5_000] },
        )
        .toBe(true);
    } catch (cause) {
      try {
        await writeTerminalPollDiagnostic('terminal-job-poll-timeout');
      } catch {
        // Preserve the poll failure if the browser is already closing.
      }
      throw cause;
    }
    expect(terminalJob).toBeDefined();
    const jobId = resourceIdentifier(terminalJob, 'jobId');
    expect(jobId).toBe(submittedJobId);
    try {
      await expect(workspace.getByRole('row', { name: new RegExp(jobId as string) })).toContainText(
        /succeeded|failed|cancelled/i,
      );
    } catch (cause) {
      await drainEvaluationUiDiagnostics();
      const renderedState = await page.evaluate((targetJobId) => {
        const workspace = document.querySelector('observatory-evaluation-workspace');
        const rows = Array.from(workspace?.querySelectorAll('tbody tr') ?? [])
          .slice(0, 50)
          .map((row) => row.textContent?.replace(/\s+/g, ' ').trim().slice(0, 240) ?? '');
        return {
          targetJobId,
          rows,
          rowCount: rows.length,
          activeTab:
            workspace
              ?.querySelector('[role="tablist"] [aria-selected="true"]')
              ?.textContent?.trim() ?? null,
          metrics:
            workspace
              ?.querySelector('.workspace-metrics')
              ?.textContent?.replace(/\s+/g, ' ')
              .trim()
              .slice(0, 240) ?? '',
          notices: Array.from(workspace?.querySelectorAll('[role="alert"], .live-banner') ?? [])
            .map((node) => node.textContent?.replace(/\s+/g, ' ').trim().slice(0, 240) ?? '')
            .filter(Boolean),
        };
      }, submittedJobId);
      await writeBoundedBrowserArtifact('evaluation-job-diagnostic.json', {
        schemaVersion: 1,
        failurePhase: 'terminal-job-render',
        submittedJobId,
        terminalStatus: terminalJob?.['status'] ?? null,
        uiRequests: evaluationUiDiagnostics.slice(-32),
        renderedState,
      });
      throw cause;
    }
    if ((terminalJob as Record<string, unknown>)['status'] !== 'succeeded') {
      let terminalAttemptStatus = 0;
      let terminalAttemptPayload: unknown = null;
      try {
        const response = await page.request.get(
          new URL(
            `/api/v1/evaluation/jobs/${encodeURIComponent(submittedJobId)}/attempts?page_size=50`,
            observatoryBaseUrl,
          ).toString(),
          { headers: { Accept: 'application/json' } },
        );
        terminalAttemptStatus = response.status();
        if (response.ok()) terminalAttemptPayload = await response.json();
      } catch {
        terminalAttemptStatus = 0;
      }
      const terminalAttemptDiagnostics: Record<string, unknown> = {
        status: terminalAttemptStatus,
        ...boundedEvaluationMetadataCollection(terminalAttemptPayload),
      };
      const terminalResultResponse = await page.request
        .get(
          new URL(
            `/api/v1/evaluation/results?page_size=50&job_id=${encodeURIComponent(submittedJobId)}`,
            observatoryBaseUrl,
          ).toString(),
          { headers: { Accept: 'application/json' } },
        )
        .catch(() => undefined);
      let terminalResultPayload: unknown = null;
      if (terminalResultResponse?.ok()) {
        try {
          terminalResultPayload = await terminalResultResponse.json();
        } catch {
          terminalResultPayload = null;
        }
      }
      await writeBoundedBrowserArtifact('evaluation-terminal-diagnostic.json', {
        schemaVersion: 1,
        failurePhase: 'terminal-job-status',
        submittedJobId,
        terminalJob: boundedEvaluationTerminalMetadata(terminalJob),
        jobGetAttempts: evaluationJobGetDiagnostics.slice(-64),
        attempts: terminalAttemptDiagnostics,
        results: {
          status: terminalResultResponse?.status() ?? 0,
          ...boundedEvaluationMetadataCollection(terminalResultPayload),
        },
      });
      throw new Error('AURA-0098 evaluation round trip did not reach a successful terminal result');
    }

    let terminalResult: Record<string, unknown> | undefined;
    let terminalScores: unknown[] = [];
    await expect
      .poll(
        async () => {
          const collectionResponse = await page.request.get(
            new URL('/api/v1/evaluation/results?page_size=50', observatoryBaseUrl).toString(),
            { headers: { Accept: 'application/json' } },
          );
          if (!collectionResponse.ok()) return false;
          const resultCandidate = collectionItems(await collectionResponse.json()).find((item) => {
            const value = recordValue(item);
            return value?.['jobId'] === submittedJobId;
          });
          const resultId = resourceIdentifier(resultCandidate, 'resultId');
          if (!resultId) return false;

          const detailResponse = await page.request.get(
            new URL(
              `/api/v1/evaluation/results/${encodeURIComponent(resultId)}`,
              observatoryBaseUrl,
            ).toString(),
            { headers: { Accept: 'application/json' } },
          );
          if (!detailResponse.ok()) return false;
          const detail = recordValue(await detailResponse.json());
          if (
            !detail ||
            detail['jobId'] !== submittedJobId ||
            detail['status'] !== 'completed' ||
            typeof detail['scoreCount'] !== 'number' ||
            detail['scoreCount'] < 1 ||
            typeof detail['partialEvidence'] !== 'boolean' ||
            typeof detail['reproducible'] !== 'boolean'
          )
            return false;

          const scoresResponse = await page.request.get(
            new URL(
              `/api/v1/evaluation/results/${encodeURIComponent(resultId)}/scores?page_size=50`,
              observatoryBaseUrl,
            ).toString(),
            { headers: { Accept: 'application/json' } },
          );
          if (!scoresResponse.ok()) return false;
          const scores = collectionItems(await scoresResponse.json());
          if (scores.length < 1) return false;
          terminalResult = detail;
          terminalScores = scores;
          return true;
        },
        { timeout: 120_000, intervals: [500, 1_000, 2_000, 5_000] },
      )
      .toBe(true);
    expect(terminalResult).toBeDefined();
    const resultId = resourceIdentifier(terminalResult, 'resultId');
    expect(resultId).toBeTruthy();
    expect(resourceIdentifier(terminalResult, 'jobId')).toBe(submittedJobId);
    expect(terminalScores.length).toBeGreaterThan(0);

    await selectTab('results', 'Results, scores, coverage, and reproducibility');
    const resultRow = workspace.getByRole('row', {
      name: new RegExp(escapeRegExp(resultId as string)),
    });
    await expect(resultRow).toContainText(/completed/i);
    await expect(resultRow).toContainText(/complete/i);
    await expect(resultRow).toContainText(/yes/i);
    await resultRow.getByRole('button', { name: resultId as string, exact: true }).click();
    await expect(
      workspace.getByRole('heading', { name: 'Scores for selected result' }),
    ).toBeVisible();
    const scoresTable = workspace.getByRole('table', { name: 'Scores for selected result' });
    await expect(scoresTable).toBeVisible();
    await expect.poll(async () => scoresTable.getByRole('row').count()).toBeGreaterThan(1);
    expect(await scoresTable.innerText()).toMatch(/\d/);
    const sinkScan = await scanBrowserPrivacySinks(
      page,
      evaluationPrivacyMarker,
      '/api/v1/evaluation/events',
      { eventType: 'evaluation.changed', resourceId: submittedJobId },
    );
    expect(sinkScan).toEqual({
      urlSafe: true,
      eventSafe: true,
      storageSafe: true,
      cacheSafe: true,
      indexedDbSafe: true,
      overflowSafe: true,
      overflowSources: [],
      overflowDetails: [],
    });
    expect(privacyConsoleCapture.overflowed).toBe(false);
    expect(
      privacyConsoleCapture.messages.some((message) => message.includes(evaluationPrivacyMarker)),
    ).toBe(false);
  });

  test('exercises seeded owner review, experiment, operations, and artifact workflows', async ({
    page,
  }) => {
    test.skip(
      !operationsFullStackEnabled,
      'set AURA_OBSERVATORY_OPERATIONS_FULL_STACK=1 only when the public operations/evaluation fixture is seeded',
    );
    const observatory = identityConfig('AURA_OBSERVATORY_', observatoryBaseUrl);
    test.skip(!observatory, 'provide Observatory owner OIDC variables');
    if (!observatory) return;

    await signIn(page, observatory, '/api/v1/auth/me', 'observatory');
    const results = await requirePublicCollection(
      page,
      '/api/v1/evaluation/results?page_size=50',
      'evaluation result',
    );
    const calibrations = await requirePublicCollection(
      page,
      '/api/v1/evaluation/calibrations?page_size=50',
      'evaluation calibration',
    );
    const artifacts = await requirePublicCollection(
      page,
      '/api/v1/evaluation/artifacts?page_size=50',
      'evaluation artifact',
    );
    const experiments = await requirePublicCollection(
      page,
      '/api/v1/evaluation/experiments?page_size=50',
      'evaluation experiment',
    );
    const baselines = await requirePublicCollection(
      page,
      '/api/v1/evaluation/baselines?page_size=50',
      'evaluation baseline',
    );
    const alertInstances = await requirePublicCollection(
      page,
      '/api/v1/alert-instances?page_size=50',
      'alert instance',
    );

    const result = recordValue(results[0]);
    const resultId = resourceIdentifier(result, 'resultId');
    const scoresResponse = resultId
      ? await page.request.get(
          new URL(
            `/api/v1/evaluation/results/${encodeURIComponent(resultId)}/scores?page_size=50`,
            observatoryBaseUrl,
          ).toString(),
          { headers: { Accept: 'application/json' } },
        )
      : null;
    const scores = scoresResponse?.ok() ? collectionItems(await scoresResponse.json()) : [];
    const calibration = recordValue(calibrations[0]);
    const calibrationId = resourceIdentifier(calibration, 'calibrationId');
    const scoreId = resourceIdentifier(recordValue(scores[0]), 'scoreId');
    const authorizationRecordId = envValue('AURA_OBSERVATORY_OWNER_AUTHORIZATION_RECORD_ID');
    if (!resultId || !calibrationId || !scoreId || !authorizationRecordId) {
      throw new Error(
        'AURA-0098 platform blocker: public result review requires a completed result score, calibration, and owner authorization record',
      );
    }

    await page.goto('/', { waitUntil: 'domcontentloaded' });
    await page.getByRole('button', { name: 'Evaluations', exact: true }).click();
    const evaluation = page.locator('observatory-evaluation-workspace');
    await expect(
      evaluation.getByRole('heading', { name: 'Controlled evaluation metadata' }),
    ).toBeVisible();
    await evaluation.getByRole('tab', { name: 'Results & scores' }).click();
    await expect(
      evaluation.getByRole('row', { name: new RegExp(escapeRegExp(resultId)) }),
    ).toBeVisible();
    await evaluation.getByRole('button', { name: resultId, exact: true }).click();
    await evaluation.getByRole('tab', { name: 'Reviews & calibration' }).click();
    await evaluation
      .getByRole('combobox', { name: 'Review score selection' })
      .selectOption(scoreId);
    await evaluation
      .getByRole('combobox', { name: 'Review calibration selection' })
      .selectOption(calibrationId);
    await evaluation.getByRole('spinbutton', { name: 'Review score' }).fill('0.5');
    await evaluation
      .getByRole('textbox', { name: 'Review authorization record ID' })
      .fill(authorizationRecordId);
    const reviewResponse = page.waitForResponse(
      (response) =>
        response.request().method() === 'POST' &&
        new URL(response.url()).pathname === '/api/v1/evaluation/reviews',
    );
    await evaluation.getByRole('button', { name: 'Record review', exact: true }).click();
    expect((await reviewResponse).status()).toBe(201);
    await expect(
      evaluation.getByRole('status').filter({ hasText: /Review .* was recorded/i }),
    ).toBeVisible();

    const candidateBaseline = baselines.find(
      (item) => recordValue(item)?.['state'] === 'candidate',
    );
    const candidateBaselineId = resourceIdentifier(candidateBaseline, 'baselineId');
    const candidateBaselineVersion = recordValue(candidateBaseline)?.['baselineVersion'];
    if (!candidateBaselineId || typeof candidateBaselineVersion !== 'string') {
      throw new Error(
        'AURA-0098 platform blocker: public experiment fixture has no baseline candidate eligible for explicit promotion',
      );
    }
    expect(experiments.length).toBeGreaterThan(0);
    await page.getByRole('button', { name: 'Experiments', exact: true }).click();
    const experimentWorkspace = page.locator('observatory-experiment-workspace');
    await expect(
      experimentWorkspace.getByRole('heading', { name: 'Compare controlled evaluation evidence' }),
    ).toBeVisible();
    const baselineRow = experimentWorkspace.getByRole('row', {
      name: new RegExp(escapeRegExp(candidateBaselineId)),
    });
    await baselineRow.getByRole('button', { name: 'Promote', exact: true }).click();
    const promotionDialog = experimentWorkspace.getByRole('dialog', {
      name: 'Promote immutable baseline?',
    });
    await expect(promotionDialog).toContainText(/irreversible|source result range|capture/i);
    await promotionDialog.locator('input').fill('PROMOTE_IMMUTABLE_BASELINE');
    await promotionDialog.getByRole('button', { name: 'Promote baseline', exact: true }).click();
    await expect(baselineRow).toContainText('promoted');

    const promotedBaseline = experimentWorkspace.getByRole('combobox', {
      name: 'Promoted baseline',
    });
    await expect(promotedBaseline).toBeEnabled();
    await promotedBaseline.selectOption(`${candidateBaselineId}@${candidateBaselineVersion}`);
    const candidateVariant = experimentWorkspace.getByRole('combobox', {
      name: 'Candidate variant',
    });
    await expect(candidateVariant).toBeVisible();
    const candidateVariantValue = await candidateVariant
      .locator('option')
      .evaluateAll((options) =>
        options.map((option) => option.getAttribute('value')).find((value) => Boolean(value)),
      );
    if (!candidateVariantValue) {
      throw new Error(
        'AURA-0098 platform blocker: promoted baseline has no distinct declared candidate variant',
      );
    }
    await candidateVariant.selectOption(candidateVariantValue);
    const cohorts = experimentWorkspace
      .getByRole('group', { name: 'Compatible cohorts' })
      .getByRole('checkbox');
    const candidateResults = experimentWorkspace
      .getByRole('group', { name: 'Candidate result IDs' })
      .getByRole('checkbox');
    if ((await cohorts.count()) === 0 || (await candidateResults.count()) === 0) {
      throw new Error(
        'AURA-0098 platform blocker: promoted baseline has no compatible cohort and persisted candidate result for comparison',
      );
    }
    await cohorts.first().check();
    await candidateResults.first().check();
    const compareResponse = page.waitForResponse(
      (response) =>
        response.request().method() === 'POST' &&
        new URL(response.url()).pathname === '/api/v1/evaluation/regressions',
      { timeout: 30_000 },
    );
    await experimentWorkspace
      .getByRole('button', { name: 'Compare evidence', exact: true })
      .click();
    const createdRegressionResponse = await compareResponse;
    expect(createdRegressionResponse.status()).toBe(201);
    const createdRegression = recordValue(await createdRegressionResponse.json());
    const createdRegressionId = resourceIdentifier(createdRegression, 'regressionId');
    if (!createdRegressionId) {
      throw new Error('AURA-0098 comparison response omitted its persisted regression identifier');
    }
    await expect(
      experimentWorkspace.getByRole('heading', {
        name: new RegExp(`Regression ${escapeRegExp(createdRegressionId)}`),
      }),
    ).toBeVisible();
    await experimentWorkspace
      .getByRole('button', { name: 'Load exact-version attribution', exact: true })
      .click();
    await expect(
      experimentWorkspace.getByRole('heading', { name: 'Attribution evidence', exact: true }),
    ).toBeVisible();
    await auditResponsiveAccessibility(page, 'observatory-experiment-workspace');

    const firingAlert = alertInstances.find((item) => recordValue(item)?.['state'] === 'firing');
    const firingRuleId = recordValue(recordValue(firingAlert)?.['rule'])?.['resourceId'];
    if (!firingAlert) {
      throw new Error(
        'AURA-0098 platform blocker: public operations fixture has no firing alert instance for acknowledgement, suppression, and incident creation',
      );
    }
    if (typeof firingRuleId !== 'string') {
      throw new Error('AURA-0098 platform blocker: firing alert omitted its bounded rule identity');
    }

    await page.getByRole('button', { name: 'Operations', exact: true }).click();
    const operations = page.locator('observatory-operations-workspace');
    await expect(
      operations.getByRole('heading', { name: 'Dashboards, alerts, and incidents' }),
    ).toBeVisible();
    await operations.getByRole('button', { name: 'Alert triage', exact: true }).click();
    const alertRow = operations
      .locator('table.alert-table tbody tr')
      .filter({ hasText: firingRuleId })
      .first();
    await expect(alertRow).toBeVisible();
    await alertRow.locator('select').selectOption('investigating');
    await alertRow.getByRole('button', { name: 'Acknowledge', exact: true }).click();
    await expect(alertRow).toContainText(/acknowledged/i);
    const suppress = alertRow.getByRole('button', { name: 'Suppress', exact: true });
    await suppress.click();
    const suppressionDialog = operations.getByRole('dialog');
    await expect(suppressionDialog).toContainText(
      /scope|consequence|observation remains available/i,
    );
    await page.keyboard.press('Escape');
    await expect(suppressionDialog).toBeHidden();
    await expect(suppress).toBeFocused();
    await suppress.click();
    await suppressionDialog.getByRole('button', { name: 'Confirm', exact: true }).focus();
    await page.keyboard.press('Enter');
    await expect(alertRow).toContainText(/suppressed/i);

    await operations.getByRole('button', { name: 'Incidents', exact: true }).click();
    await operations.locator('fieldset.alert-picker input[type="checkbox"]').first().check();
    const incidentTitle = `AURA-0098 browser incident ${Date.now()}`;
    await operations.getByRole('textbox', { name: 'Title' }).fill(incidentTitle);
    await operations.getByRole('button', { name: 'Create incident', exact: true }).click();
    const incidentCard = operations
      .locator('article.incident-card')
      .filter({ hasText: incidentTitle });
    await expect(incidentCard).toBeVisible();
    await incidentCard.getByRole('button', { name: 'Transition', exact: true }).click();
    const incidentDialog = operations.getByRole('dialog');
    await expect(incidentDialog).toContainText(/scope|consequence|audited transition/i);
    await incidentDialog.getByRole('button', { name: 'Confirm', exact: true }).click();
    await expect(incidentCard).toContainText(/investigating|mitigated|resolved/i);

    const incidents = await requirePublicCollection(
      page,
      '/api/v1/incidents?page_size=50',
      'incident',
    );
    const createdIncident = recordValue(
      incidents.find((item) => recordValue(item)?.['title'] === incidentTitle),
    );
    const incidentId = resourceIdentifier(createdIncident, 'incidentId');
    const incidentVersion = createdIncident?.['incidentVersion'];
    if (!incidentId || typeof incidentVersion !== 'string') {
      throw new Error(
        'AURA-0098 platform blocker: created incident was not returned by the public collection',
      );
    }
    await operations.getByRole('button', { name: 'Annotations', exact: true }).click();
    await operations.getByRole('textbox', { name: 'Target ID' }).fill(incidentId);
    await operations.getByRole('textbox', { name: 'Target version' }).fill(incidentVersion);
    const annotationText = 'AURA-0098 owner lifecycle annotation';
    await operations.getByRole('textbox', { name: 'Annotation text' }).fill(annotationText);
    await operations.getByRole('button', { name: 'Save annotation', exact: true }).click();
    const annotationCard = operations
      .locator('article.annotation-card')
      .filter({ hasText: annotationText });
    await expect(annotationCard).toBeVisible();
    const deleteAnnotation = annotationCard.getByRole('button', {
      name: 'Delete annotation',
      exact: true,
    });
    await deleteAnnotation.click();
    const annotationDialog = operations.getByRole('dialog');
    await expect(annotationDialog).toContainText(/scope|consequence|audit record remains/i);
    await page.keyboard.press('Escape');
    await expect(annotationDialog).toBeHidden();
    await expect(deleteAnnotation).toBeFocused();
    await deleteAnnotation.click();
    await annotationDialog.getByRole('button', { name: 'Confirm', exact: true }).press('Enter');
    await expect(annotationCard).toHaveCount(0);
    await auditResponsiveAccessibility(page, 'observatory-operations-workspace');

    const artifactId = resourceIdentifier(recordValue(artifacts[0]), 'artifactId');
    if (!artifactId)
      throw new Error('AURA-0098 platform blocker: artifact fixture omitted artifactId');
    await page.getByRole('button', { name: 'Evaluations', exact: true }).click();
    const evaluationWorkspace = page.locator('observatory-evaluation-workspace');
    await evaluationWorkspace.getByRole('tab', { name: 'Artifacts' }).click();
    await expect(
      evaluationWorkspace.getByRole('row', { name: new RegExp(escapeRegExp(artifactId)) }),
    ).toBeVisible();
    await evaluationWorkspace.getByRole('tab', { name: 'Retention' }).click();
    const evaluationCutoff = evaluationWorkspace.getByRole('textbox', {
      name: 'Deletion cutoff in local time',
    });
    await evaluationCutoff.fill('2099-01-01T00:00');
    const dismissDeletion = page.waitForEvent('dialog');
    await evaluationWorkspace
      .getByRole('button', { name: 'Delete before cutoff', exact: true })
      .click();
    const dismissed = await dismissDeletion;
    expect(dismissed.message()).toMatch(/DELETE_EVALUATION_ARTIFACTS.*cutoff.*cannot be undone/i);
    await dismissed.dismiss();
    const confirmDeletion = page.waitForEvent('dialog');
    await evaluationWorkspace
      .getByRole('button', { name: 'Delete before cutoff', exact: true })
      .click();
    const confirmed = await confirmDeletion;
    expect(confirmed.message()).toMatch(/DELETE_EVALUATION_ARTIFACTS.*cutoff.*cannot be undone/i);
    await confirmed.accept();
    await expect(
      evaluationWorkspace.getByRole('status').filter({ hasText: /Deletion .*succeeded/i }),
    ).toBeVisible({ timeout: 60_000 });
    await expect(
      evaluationWorkspace.getByRole('row', { name: new RegExp(escapeRegExp(artifactId)) }),
    ).toHaveCount(0);
  });

  test('preserves the owner session across a disposable Observatory restart', async ({ page }) => {
    test.skip(
      lifecyclePhase !== 'preserved',
      'runner must restart the disposable Observatory services with session volume retained',
    );
    const observatory = identityConfig('AURA_OBSERVATORY_', observatoryBaseUrl);
    test.skip(!observatory, 'provide Observatory owner OIDC variables');
    if (!observatory) return;
    await expect
      .poll(async () => (await sessionStatus(page, observatory, '/api/v1/auth/me')).status, {
        timeout: 30_000,
      })
      .toBe(200);
    const eventsResponse = page.waitForResponse(
      (response) =>
        response.request().method() === 'GET' &&
        new URL(response.url()).pathname === '/api/v1/events',
      { timeout: 30_000 },
    );
    await page.goto('/', { waitUntil: 'domcontentloaded' });
    expect((await eventsResponse).status()).toBe(200);
    await expect(
      page.getByRole('heading', { level: 1, name: 'Operational metadata' }),
    ).toBeVisible();
  });

  test('rejects the old session after reset and permits owner reauthentication', async ({
    page,
  }) => {
    test.skip(
      lifecyclePhase !== 'reset',
      'runner must remove only the disposable Observatory session volume',
    );
    const observatory = identityConfig('AURA_OBSERVATORY_', observatoryBaseUrl);
    test.skip(!observatory, 'provide Observatory owner OIDC variables');
    if (!observatory) return;
    await expect
      .poll(async () => (await sessionStatus(page, observatory, '/api/v1/auth/me')).status, {
        timeout: 30_000,
      })
      .toBe(401);
    await signIn(page, observatory, '/api/v1/auth/me', 'observatory');
    await page.goto('/', { waitUntil: 'domcontentloaded' });
    await expect(
      page.getByRole('heading', { level: 1, name: 'Operational metadata' }),
    ).toBeVisible();
  });

  test('keeps Core usable after the platform stops Observatory services', async ({ page }) => {
    test.skip(
      process.env['AURA_OBSERVATORY_SERVICES_STOPPED'] !== '1',
      'platform runner must stop Observatory services and set AURA_OBSERVATORY_SERVICES_STOPPED=1',
    );
    const core = identityConfig('AURA_E2E_', coreBaseUrl);
    test.skip(!core, 'provide Core owner OIDC variables');
    if (!core) return;
    await signIn(page, core, '/api/v1/auth/session', 'core');
    const outagePrompt = `${prompt}_CORE_STILL_USABLE`;
    // Reuse the normal Core path so this outage assertion waits for
    // asynchronous conversation/model/agent readiness, verifies that the
    // configured selectable model is present, and still proves a completed
    // response after Observatory services have stopped.
    const accepted = await createCoreRun(page, core, outagePrompt);
    await waitForCoreRun(page, core, accepted);
  });
});
