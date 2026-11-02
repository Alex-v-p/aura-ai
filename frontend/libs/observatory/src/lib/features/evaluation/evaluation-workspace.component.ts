import { ChangeDetectionStrategy, Component, computed, effect, ElementRef, HostListener, inject, signal, ViewChild } from '@angular/core';
import { CommonModule } from '@angular/common';
import type {
  CreateEvaluationDatasetRequest,
  CreateEvaluationSuiteRequest,
  EvaluationJob,
  EvaluationModelBinding,
  EvaluationReproducibility,
  EvaluationResult,
} from '@aura/observatory-api-client';
import { ObservatoryStore } from '../../observatory-store';
import { EvaluationWorkspaceStore, type EvaluationWorkspaceTab, type JobAdmissionBindings } from './evaluation-workspace.store';

type SuiteField = 'dataset' | 'evaluator' | 'definition' | 'component' | 'limits' | 'threshold' | 'provenance';

@Component({
  selector: 'observatory-evaluation-workspace',
  standalone: true,
  imports: [CommonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section class="evaluation" aria-labelledby="evaluation-title">
      <header class="section-heading">
        <div><p class="eyebrow">Evaluation workbench</p><h2 id="evaluation-title" tabindex="-1">Controlled evaluation metadata</h2><p class="muted">Definitions, progress, scores, provenance, and retention. Evaluation content stays in its authorized store.</p></div>
        <button class="secondary" type="button" (click)="refresh()" [disabled]="!!store.busyAction()">Refresh</button>
      </header>
      @if (!store.error()) { <div class="live-banner" [class.partial]="store.state() === 'partial'" [class.rejected]="store.state() === 'rejected'" role="status" aria-live="polite">{{ stateLabel() }}@if (store.lastUpdatedAt()) { <span>Last snapshot {{ store.lastUpdatedAt() | slice:0:19 }} UTC</span> }</div> }
      @if (store.error()) { <div class="live-banner notice error recovery-region" [class.rejected]="store.state() === 'rejected'" role="alert"><div><strong>{{ stateLabel() }}</strong>@if (store.state() !== 'rejected') { <p>{{ store.error() }}</p> }</div><div class="recovery-actions"><button type="button" class="primary" (click)="refresh()" [disabled]="!!store.busyAction()">Retry all</button>@if (store.state() === 'rejected' && store.rejectedScopes().length) { <details class="retry-details" open><summary>Retry an affected scope</summary><div class="retry-scopes">@for (scope of store.rejectedScopes(); track scope) { <button type="button" class="secondary" (click)="retryScope(scope)" [disabled]="!!store.busyAction()">Retry {{ scope }}</button> }</div></details> }</div></div> }
      @if (store.notice()) { <div class="notice" role="status">{{ store.notice() }}</div> }
      @if (store.busyAction()) { <div class="action-progress" role="status" aria-live="polite">{{ actionLabel() }}</div> }
      <div class="workspace-metrics" aria-label="Evaluation workspace summary"><div><span>Datasets</span><strong>{{ store.data().datasets?.items.length ?? 0 }}</strong></div><div><span>Running jobs</span><strong>{{ runningJobCount() }}</strong></div><div><span>Results</span><strong>{{ store.data().results?.items.length ?? 0 }}</strong></div><p>Review the current metadata snapshot first; authoring controls are scoped to the selected workspace.</p></div>

      <label class="mobile-tab-select">Workspace <select aria-label="Evaluation workspace" [value]="tab()" (change)="selectFromEvent($event)">@for (item of tabs; track item.id) { <option [value]="item.id">{{ item.label }}</option> }</select></label>
      <nav class="workspace-tabs" aria-label="Evaluation workspaces" role="tablist" (keydown)="onTabKeydown($event)">
        @for (item of tabs; track item.id) { <button type="button" role="tab" [id]="'evaluation-tab-' + item.id" [attr.aria-controls]="'evaluation-panel-' + item.id" [attr.tabindex]="tab() === item.id ? 0 : -1" [attr.aria-selected]="tab() === item.id" [class.active]="tab() === item.id" (click)="select(item.id)" (focus)="tab() === item.id && ensureTabVisible($event)">{{ item.label }}</button> }
      </nav>

      @if (store.state() === 'loading' && !store.hasData()) {
        <div class="panel loading-panel" role="status" aria-live="polite">Loading evaluation metadata…</div>
      } @else {
        <div class="tab-panel" role="tabpanel" [id]="'evaluation-panel-' + tab()" [attr.aria-labelledby]="'evaluation-tab-' + tab()" tabindex="0">
          @switch (tab()) {
            @case ('datasets') {
              <section class="panel" aria-labelledby="datasets-title">
                <div class="panel-heading"><div><h3 id="datasets-title">Datasets and cases</h3><p class="muted">Versioned dataset definitions and bounded case references. Raw case content is not displayed.</p></div><button class="primary" type="button" (click)="createSyntheticDataset()" [disabled]="store.busyAction() === 'create-dataset'">{{ store.busyAction() === 'create-dataset' ? 'Creating…' : 'Create synthetic dataset' }}</button></div>
                <table class="data-table"><caption>Evaluation datasets</caption><thead><tr><th scope="col">Dataset</th><th scope="col">Version</th><th scope="col">Cases</th><th scope="col">State</th><th scope="col">Capture</th><th scope="col">Action</th></tr></thead><tbody>@for (dataset of store.data().datasets?.items ?? []; track dataset.datasetId) { <tr><th scope="row">{{ dataset.datasetId }}</th><td data-label="Version">{{ dataset.datasetVersion }}</td><td data-label="Cases">{{ dataset.caseCount }}</td><td data-label="State"><span class="state">{{ dataset.state }}</span></td><td data-label="Capture">{{ dataset.captureClassification }}</td><td class="actions" data-label="Action"><button class="secondary" type="button" [attr.aria-pressed]="store.selectedDataset()?.datasetId === dataset.datasetId" (click)="inspectDataset(dataset)">Inspect cases</button><button class="danger-link" type="button" (click)="deleteDataset(dataset.datasetId)" [disabled]="store.busyAction() === 'delete:dataset'">Delete</button></td></tr> } @empty { <tr><td colspan="6" class="empty">No datasets are available.</td></tr> }</tbody></table>
                @if (store.data().cases; as cases) { <h4>Cases for selected dataset</h4><table class="data-table"><caption>Bounded evaluation cases</caption><thead><tr><th scope="col">Case</th><th scope="col">Version</th><th scope="col">Artifacts</th></tr></thead><tbody>@for (item of cases.items; track item.caseId) { <tr><th scope="row">{{ item.caseId }}</th><td data-label="Version">{{ item.caseVersion }}</td><td data-label="Artifacts">{{ item.artifactIds.length }}</td></tr> } @empty { <tr><td colspan="3" class="empty">No cases are available.</td></tr> }</tbody></table> }
              </section>
            }
            @case ('suites') {
              <section class="panel" aria-labelledby="suites-title"><div class="panel-heading"><div><h3 id="suites-title">Suite composition</h3><p class="muted">Suites bind datasets, evaluators, component versions, budgets, and manual thresholds.</p></div></div><form class="suite-form" aria-label="Compose authoritative immutable suite" (submit)="createBoundSuite($event)">
                <p id="suite-readiness" class="notice" role="status" aria-live="polite">{{ suiteReadiness().message }}</p>
                <fieldset aria-describedby="suite-definition-errors"><legend>Definition</legend><label data-suite-field="definition">Dataset/version <select required aria-label="Suite dataset version" [attr.aria-invalid]="!suiteFieldValid('dataset')" aria-describedby="suite-definition-errors" [value]="suiteDatasetSelection()" (change)="setSuiteDataset($event)"><option value="">Select authoritative dataset version</option>@for (dataset of store.data().datasets?.items ?? []; track dataset.datasetId) { <option [value]="dataset.datasetId + '|' + dataset.datasetVersion">{{ dataset.datasetId }} @ {{ dataset.datasetVersion }}</option> }</select></label><label data-suite-field="evaluator">Evaluator/version <select required aria-label="Suite evaluator version" [attr.aria-invalid]="!suiteFieldValid('evaluator')" aria-describedby="suite-definition-errors" [value]="suiteEvaluatorSelection()" (change)="setSuiteEvaluator($event)"><option value="">Select immutable deterministic evaluator version</option>@for (evaluator of store.data().evaluators?.items ?? []; track evaluator.evaluatorId) { @if (evaluator.kind === 'deterministic') { <option [value]="evaluator.evaluatorId + '|' + evaluator.evaluatorVersion">{{ evaluator.evaluatorId }} @ {{ evaluator.evaluatorVersion }}</option> } }</select></label><label>Suite version <input required pattern="[0-9]+\\.[0-9]+\\.[0-9]+" aria-label="Suite version" [attr.aria-invalid]="!suiteFieldValid('definition')" aria-describedby="suite-definition-errors" [value]="suiteVersion()" (input)="setSuiteVersion($event)" /></label>@if (suiteGroupError('definition'); as error) { <p id="suite-definition-errors" class="field-error" role="alert">{{ error }}</p> }</fieldset>
                <fieldset aria-describedby="suite-component-errors"><legend>Component</legend><label>Component ID <input required aria-label="Suite component ID" [attr.aria-invalid]="!suiteFieldValid('component')" aria-describedby="suite-component-errors" [value]="suiteComponentId()" (input)="setSuiteComponentId($event)" /></label><label>Component version <input required pattern="[0-9]+\\.[0-9]+\\.[0-9]+" aria-label="Suite component version" [attr.aria-invalid]="!suiteFieldValid('component')" aria-describedby="suite-component-errors" [value]="suiteComponentVersion()" (input)="setSuiteComponentVersion($event)" /></label><label>Code revision <input required aria-label="Suite code revision" [attr.aria-invalid]="!suiteFieldValid('component')" aria-describedby="suite-component-errors" [value]="suiteCodeRevision()" (input)="setSuiteCodeRevision($event)" /></label><label>Image digest <input required pattern="[0-9a-fA-F]{64}" aria-label="Suite image digest" [attr.aria-invalid]="!suiteFieldValid('component')" aria-describedby="suite-component-errors" [value]="suiteImageDigest()" (input)="setSuiteImageDigest($event)" /></label><label>Configuration revision <input required aria-label="Suite configuration revision" [attr.aria-invalid]="!suiteFieldValid('component')" aria-describedby="suite-component-errors" [value]="suiteConfigurationRevision()" (input)="setSuiteConfigurationRevision($event)" /></label>@if (suiteGroupError('component'); as error) { <p id="suite-component-errors" class="field-error" role="alert">{{ error }}</p> }</fieldset>
                <fieldset aria-describedby="suite-limit-errors"><legend>Limits</legend><label>Case budget <input required type="number" min="1" max="10000" aria-label="Suite case budget" [attr.aria-invalid]="!suiteFieldValid('limits')" aria-describedby="suite-limit-errors" [value]="suiteCaseBudget()" (input)="setSuiteLimit('caseBudget', $event)" /></label><label>Timeout milliseconds <input required type="number" min="1" max="3600000" aria-label="Suite timeout milliseconds" [attr.aria-invalid]="!suiteFieldValid('limits')" aria-describedby="suite-limit-errors" [value]="suiteTimeoutMs()" (input)="setSuiteLimit('timeoutMs', $event)" /></label><label>Max concurrency <input required type="number" min="1" max="128" aria-label="Suite max concurrency" [attr.aria-invalid]="!suiteFieldValid('limits')" aria-describedby="suite-limit-errors" [value]="suiteMaxConcurrency()" (input)="setSuiteLimit('maxConcurrency', $event)" /></label><label>Max attempts <input required type="number" min="1" max="10" aria-label="Suite max attempts" [attr.aria-invalid]="!suiteFieldValid('limits')" aria-describedby="suite-limit-errors" [value]="suiteMaxAttempts()" (input)="setSuiteLimit('maxAttempts', $event)" /></label><label>Max cases <input required type="number" min="1" max="10000" aria-label="Suite max cases" [attr.aria-invalid]="!suiteFieldValid('limits')" aria-describedby="suite-limit-errors" [value]="suiteMaxCases()" (input)="setSuiteLimit('maxCases', $event)" /></label><label>Max input bytes <input required type="number" min="0" max="100000000" aria-label="Suite max input bytes" [attr.aria-invalid]="!suiteFieldValid('limits')" aria-describedby="suite-limit-errors" [value]="suiteMaxInputBytes()" (input)="setSuiteLimit('maxInputBytes', $event)" /></label><label>Max output bytes <input required type="number" min="0" max="100000000" aria-label="Suite max output bytes" [attr.aria-invalid]="!suiteFieldValid('limits')" aria-describedby="suite-limit-errors" [value]="suiteMaxOutputBytes()" (input)="setSuiteLimit('maxOutputBytes', $event)" /></label><label>Max tokens <input required type="number" min="0" max="1000000" aria-label="Suite max tokens" [attr.aria-invalid]="!suiteFieldValid('limits')" aria-describedby="suite-limit-errors" [value]="suiteMaxTokens()" (input)="setSuiteLimit('maxTokens', $event)" /></label>@if (suiteGroupError('limits'); as error) { <p id="suite-limit-errors" class="field-error" role="alert">{{ error }}</p> }</fieldset>
                <fieldset aria-describedby="suite-threshold-errors"><legend>Threshold</legend><label>Threshold metric <input required pattern="[a-zA-Z][a-zA-Z0-9_.-]{1,63}" aria-label="Suite threshold metric" [attr.aria-invalid]="!suiteFieldValid('threshold')" aria-describedby="suite-threshold-errors" [value]="suiteThresholdMetric()" (input)="setSuiteThresholdMetric($event)" /></label><label>Threshold value <input required type="number" min="0" max="1" step="0.01" aria-label="Suite threshold value" [attr.aria-invalid]="!suiteFieldValid('threshold')" aria-describedby="suite-threshold-errors" [value]="suiteThresholdValue()" (input)="setSuiteThresholdValue($event)" /></label><p class="muted">At least (≥) semantics are fixed: mean aggregation, ratio unit, minimum sample size 1, and missing evidence is inconclusive. The generated request stores the metric/value; these semantics are persisted by the Observatory backend.</p>@if (suiteGroupError('threshold'); as error) { <p id="suite-threshold-errors" class="field-error" role="alert">{{ error }}</p> }</fieldset>
                <fieldset aria-describedby="suite-provenance-errors"><legend>Provenance</legend><label>Created-by component <input required aria-label="Suite provenance component" [attr.aria-invalid]="!suiteFieldValid('provenance')" aria-describedby="suite-provenance-errors" [value]="suiteCreatedByComponent()" (input)="setSuiteCreatedByComponent($event)" /></label><label>Created-by version <input required pattern="[0-9]+\\.[0-9]+\\.[0-9]+" aria-label="Suite provenance version" [attr.aria-invalid]="!suiteFieldValid('provenance')" aria-describedby="suite-provenance-errors" [value]="suiteCreatedByVersion()" (input)="setSuiteCreatedByVersion($event)" /></label><label>Source digest <input required pattern="[0-9a-fA-F]{64}" aria-label="Suite provenance source digest" [attr.aria-invalid]="!suiteFieldValid('provenance')" aria-describedby="suite-provenance-errors" [value]="suiteSourceDigest()" (input)="setSuiteSourceDigest($event)" /></label><p class="muted">Capture classification is metadata_only and retention is manual_only; raw evaluation content is never submitted.</p>@if (suiteGroupError('provenance'); as error) { <p id="suite-provenance-errors" class="field-error" role="alert">{{ error }}</p> }</fieldset>
                <div class="suite-actions"><button class="secondary" type="button" (click)="focusFirstInvalidSuiteField()">Check suite readiness</button><button class="primary" type="submit" [disabled]="!canComposeSuite() || store.busyAction() === 'create-suite'">{{ store.busyAction() === 'create-suite' ? 'Creating…' : 'Compose immutable suite' }}</button></div>
              </form>
              @if (suiteAcceptedSummary(); as accepted) { <section id="created-suite-summary" class="selected-suite-summary" tabindex="-1" aria-labelledby="created-suite-title"><h4 id="created-suite-title">Selected suite created</h4><p><strong>Accepted version:</strong> {{ accepted.suiteVersion }} <span class="muted">({{ accepted.suiteId }})</span></p><dl><div><dt>Dataset</dt><dd>{{ accepted.dataset.id }} @ {{ accepted.dataset.version }}</dd></div><div><dt>Component</dt><dd>{{ accepted.components[0].componentId }} @ {{ accepted.components[0].componentVersion }} · code {{ accepted.components[0].codeRevision }} · image {{ accepted.components[0].imageDigest }} · config {{ accepted.components[0].configurationRevision }}</dd></div><div><dt>Limits</dt><dd>{{ accepted.limits.maxCases }} cases · {{ accepted.limits.timeoutMs }} ms · concurrency {{ accepted.limits.maxConcurrency }} · attempts {{ accepted.limits.maxAttempts }}</dd></div><div><dt>Threshold</dt><dd>{{ accepted.manualThresholds?.[suiteThresholdMetric()] }} {{ accepted.manualThresholds ? suiteThresholdMetric() : '' }} · At least (≥), mean, ratio, minimum sample 1, missing evidence inconclusive</dd></div><div><dt>Provenance</dt><dd>{{ accepted.provenance.createdByComponent }} @ {{ accepted.provenance.createdByVersion }} · source {{ accepted.provenance.sourceDigest }}</dd></div></dl></section> }
              <table class="data-table"><caption>Evaluation suites</caption><thead><tr><th scope="col">Suite</th><th scope="col">Dataset</th><th scope="col">Evaluators</th><th scope="col">Budget</th><th scope="col">State</th><th scope="col">Action</th></tr></thead><tbody>@for (suite of store.data().suites?.items ?? []; track suite.suiteId) { <tr><th scope="row">{{ suite.suiteId }}</th><td>{{ suite.dataset.resourceId }}</td><td>{{ suite.evaluators.length }}</td><td>{{ suite.caseBudget }} cases</td><td>{{ suite.state }}</td><td class="actions"><button class="secondary" type="button" [attr.aria-pressed]="store.selectedSuite()?.suiteId === suite.suiteId" (click)="selectSuite(suite)">Use this version</button><button class="danger-link" type="button" (click)="deleteSuite(suite.suiteId)" [disabled]="store.busyAction() === 'delete:suite'">Delete</button></td></tr> } @empty { <tr><td colspan="6" class="empty">No suites are available.</td></tr> }</tbody></table></section>
            }
            @case ('evaluators') {
              <section class="panel" aria-labelledby="evaluators-title"><h3 id="evaluators-title">Evaluator bindings</h3><table class="data-table"><caption>Evaluation evaluators</caption><thead><tr><th scope="col">Evaluator</th><th scope="col">Kind</th><th scope="col">Metric</th><th scope="col">Components</th><th scope="col">Version</th><th scope="col">Action</th></tr></thead><tbody>@for (evaluator of store.data().evaluators?.items ?? []; track evaluator.evaluatorId) { <tr><th scope="row">{{ evaluator.evaluatorId }}</th><td><span class="state">{{ evaluator.kind }}</span></td><td>{{ evaluator.metric }}</td><td>{{ evaluator.componentIds.length }}</td><td>{{ evaluator.evaluatorVersion }}</td><td class="actions"><button class="secondary" type="button" [attr.aria-pressed]="store.selectedEvaluator()?.evaluatorId === evaluator.evaluatorId" (click)="selectEvaluator(evaluator)">Use this version</button></td></tr> } @empty { <tr><td colspan="6" class="empty">No evaluators are available.</td></tr> }</tbody></table></section>
            }
            @case ('jobs') {
              <section class="panel" aria-labelledby="jobs-title"><div class="panel-heading"><div><h3 id="jobs-title">Jobs and progress</h3><p class="muted">Submit only after selecting authoritative dataset, suite, evaluator, case, component, model, deployment, and limit metadata.</p>@if (store.jobAdmissionReason(); as reason) { <p class="notice" role="status">{{ reason }}</p> }</div><button class="primary" type="button" (click)="submit()" [disabled]="store.busyAction() === 'submit' || !!store.jobAdmissionReason()">{{ store.busyAction() === 'submit' ? 'Submitting…' : 'Submit evaluation' }}</button></div><section class="runner-panel" aria-labelledby="job-binding-title" [attr.aria-controls]="'evaluation-panel-jobs'"><h3 id="job-binding-title">Authoritative runner metadata</h3><p class="muted">Bind selected model, deployment, and reproducibility metadata before submitting. Values are never inferred from evaluation content.</p><form class="review-form" aria-label="Bind authoritative evaluation runner metadata" (submit)="bindJobMetadata($event)"><label>Model ID <input required aria-label="Evaluation model ID" [value]="jobModelId()" (input)="setJobModelId($event)" /></label><label>Model version <input required aria-label="Evaluation model version" [value]="jobModelVersion()" (input)="setJobModelVersion($event)" /></label><label>Model digest <input required pattern="[0-9a-fA-F]{64}" aria-label="Evaluation model digest" [value]="jobModelDigest()" (input)="setJobModelDigest($event)" /></label><label>Deployment revision <input required aria-label="Evaluation deployment revision" [value]="jobDeploymentRevision()" (input)="setJobDeploymentRevision($event)" /></label><label>Seed <input required type="number" aria-label="Evaluation reproducibility seed" [value]="jobSeed()" (input)="setJobSeed($event)" /></label><label>Fixture revision <input required aria-label="Evaluation fixture revision" [value]="jobFixtureRevision()" (input)="setJobFixtureRevision($event)" /></label><label>Prompt revision <input required aria-label="Evaluation prompt revision" [value]="jobPromptRevision()" (input)="setJobPromptRevision($event)" /></label><label>Policy revision <input required aria-label="Evaluation policy revision" [value]="jobPolicyRevision()" (input)="setJobPolicyRevision($event)" /></label><button class="secondary" type="submit" [disabled]="!canBindJobMetadata() || store.busyAction() === 'submit'">Bind selected metadata</button></form></section><table class="data-table"><caption>Evaluation jobs</caption><thead><tr><th scope="col">Job</th><th scope="col">Status</th><th scope="col">Attempts</th><th scope="col">Results</th><th scope="col">Recovery</th><th scope="col">Action</th></tr></thead><tbody>@for (job of store.data().jobs?.items ?? []; track job.jobId) { <tr><th scope="row">{{ job.jobId }}</th><td data-label="Status"><span class="state" [attr.data-status]="job.status">{{ job.status }}</span>@if (job.status === 'failed' || job.status === 'cancelled') { <small class="job-cause">Cause: {{ sanitizedJobCause(job) }}</small> }</td><td data-label="Attempts">{{ job.attemptCount }}</td><td data-label="Results">{{ job.resultCount }}</td><td data-label="Recovery">@if (job.status === 'failed' || job.status === 'cancelled') { <button class="secondary" type="button" (click)="inspectFailedJob(job)">Inspect</button><button class="secondary" type="button" (click)="recoverJob(job)" [disabled]="!!store.busyAction()">Prepare new evaluation</button> } @else { <span class="muted">No recovery needed</span> }</td><td class="actions" data-label="Action">@if (job.status === 'queued' || job.status === 'running') { <button class="danger-link" type="button" (click)="cancel(job)" [disabled]="!!store.busyAction()">{{ store.busyAction() === 'cancel:' + job.jobId ? 'Cancel pending…' : 'Cancel' }}</button> } <button class="danger-link" type="button" (click)="deleteJob(job.jobId)" [disabled]="store.busyAction() === 'delete:job'">{{ store.busyAction() === 'delete:job' ? 'Delete pending…' : 'Delete' }}</button></td></tr> } @empty { <tr><td colspan="6" class="empty">No jobs are available.</td></tr> }</tbody></table></section>
            }
            @case ('results') {
              <section class="panel" aria-labelledby="results-title"><h3 id="results-title">Results, scores, coverage, and reproducibility</h3><table class="data-table"><caption>Evaluation results</caption><thead><tr><th scope="col">Result</th><th scope="col">Status</th><th scope="col">Scores</th><th scope="col">Evidence</th><th scope="col">Reproducible</th></tr></thead><tbody>@for (result of store.data().results?.items ?? []; track result.resultId) { <tr><th scope="row"><button class="id-link" type="button" (click)="openResult(result)">{{ result.resultId }}</button></th><td>{{ result.status }}</td><td>{{ result.scoreCount }}</td><td>{{ result.partialEvidence ? 'Partial' : 'Complete' }}</td><td>{{ result.reproducible ? 'Yes' : 'No' }}</td></tr> } @empty { <tr><td colspan="5" class="empty">No results are available.</td></tr> }</tbody></table>@if (store.data().scores; as scores) { <h4>Scores for selected result</h4><table class="data-table"><caption>Scores for selected result</caption><thead><tr><th scope="col">Metric</th><th scope="col">Value</th><th scope="col">Outcome</th><th scope="col">Disposition</th></tr></thead><tbody>@for (score of scores.items; track score.scoreId) { <tr><th scope="row">{{ score.metric }}</th><td>{{ score.value ?? 'Not observed' }}</td><td>{{ score.outcome ?? 'Inconclusive' }}</td><td>{{ score.scoreDisposition }}</td></tr> }</tbody></table> }</section>
            }
            @case ('artifacts') {
              <section class="panel" aria-labelledby="artifacts-title"><h3 id="artifacts-title">Artifact metadata</h3><p class="muted">Only purpose, classification, size, digest, retention, and deletion state are shown. Content transfer is unavailable.</p><table class="data-table"><caption>Evaluation artifact metadata</caption><thead><tr><th scope="col">Artifact</th><th scope="col">Purpose</th><th scope="col">Media</th><th scope="col">Bytes</th><th scope="col">Retention</th></tr></thead><tbody>@for (artifact of store.data().artifacts?.items ?? []; track artifact.artifactId) { <tr><th scope="row">{{ artifact.artifactId }}</th><td>{{ artifact.purpose }}</td><td>{{ artifact.mediaClassification }}</td><td>{{ artifact.byteCount }}</td><td>{{ artifact.deletionStatus }}</td></tr> } @empty { <tr><td colspan="5" class="empty">No artifact metadata is available.</td></tr> }</tbody></table></section>
            }
            @case ('reviews') {
              <section class="panel" aria-labelledby="reviews-title"><h3 id="reviews-title">Human review and calibration</h3><p class="muted">Review labels and bounded score disagreement are visible; comments remain content-bearing artifacts.</p><form class="review-form" aria-label="Submit metadata-only human review" (submit)="submitReviewForm($event)"><label>Score selection <select required aria-label="Review score selection" [value]="selectedReviewScoreId()" (change)="setSelectedReviewScore($event)"><option value="">Select a score</option>@for (score of store.data().scores?.items ?? []; track score.scoreId) { <option [value]="score.scoreId">{{ score.metric }} · {{ score.value ?? 'not observed' }}</option> }</select></label><label>Calibration selection <select required aria-label="Review calibration selection" [value]="selectedReviewCalibrationId()" (change)="setSelectedReviewCalibration($event)"><option value="">Select calibration</option>@for (calibration of store.data().calibrations?.items ?? []; track calibration.calibrationId) { <option [value]="calibration.calibrationId">{{ calibration.calibrationId }} · {{ calibration.eligibility }}</option> }</select></label><label>Review score <input type="number" min="0" max="1" step="0.01" aria-label="Review score" [value]="reviewScore()" (input)="setReviewScore($event)" /></label><label>Authorization record ID <input required pattern="[0-9a-fA-F-]{36}" aria-label="Review authorization record ID" [value]="reviewAuthorizationId()" (input)="setReviewAuthorization($event)" /></label><label>Outcome <select aria-label="Review outcome" [value]="reviewOutcome()" (change)="setReviewOutcome($event)"><option value="pass">Pass</option><option value="fail">Fail</option><option value="inconclusive">Inconclusive</option></select></label><label>Label <select aria-label="Review label" [value]="reviewLabel()" (change)="setReviewLabel($event)"><option value="accepted">Accepted</option><option value="rejected">Rejected</option><option value="needs_review">Needs review</option></select></label><button class="primary" type="submit" [disabled]="!canSubmitReview() || store.busyAction() === 'review'">{{ store.busyAction() === 'review' ? 'Recording…' : 'Record review' }}</button></form>@if (!canSubmitReview()) { <p class="notice" role="status">Select the exact score and calibration, inspect a result, and provide owner authorization before recording a review.</p> }<table class="data-table"><caption>Human reviews</caption><thead><tr><th scope="col">Review</th><th scope="col">Label</th><th scope="col">Outcome</th><th scope="col">Disagreement</th><th scope="col">Calibration</th></tr></thead><tbody>@for (review of store.data().reviews?.items ?? []; track review.reviewId) { <tr><th scope="row">{{ review.reviewId }}</th><td>{{ review.reviewLabel }}</td><td>{{ review.reviewOutcome }}</td><td>{{ review.disagreement }}</td><td>{{ review.calibration.resourceId }}</td></tr> } @empty { <tr><td colspan="5" class="empty">No human reviews are available.</td></tr> }</tbody></table>@if (store.data().calibrations; as calibrations) { <h4>Calibration results</h4><div class="metric-grid">@for (calibration of calibrations.items; track calibration.calibrationId) { <div class="metric"><strong>{{ calibration.calibrationId }}</strong><span>{{ calibration.eligibility }}</span><small>{{ calibration.sampleCount }} labels · {{ calibration.agreementRate | percent:'1.0-1' }} agreement</small></div> }</div> }</section>
            }
            @case ('qualification') {
              <section class="panel" aria-labelledby="qualification-title"><h3 id="qualification-title">Judge qualification</h3><form class="review-form" aria-label="Qualify local judge" (submit)="qualifyForm($event)"><label>Calibration selection <select required aria-label="Qualification calibration selection" [value]="selectedQualificationCalibrationId()" (change)="setSelectedQualificationCalibration($event)"><option value="">Select eligible calibration</option>@for (calibration of store.data().calibrations?.items ?? []; track calibration.calibrationId) { @if (calibration.eligibility === 'eligible') { <option [value]="calibration.calibrationId">{{ calibration.calibrationId }} · eligible</option> } }</select></label><label>Prompt artifact selection <select required aria-label="Qualification prompt artifact selection" [value]="selectedPromptArtifactId()" (change)="setSelectedPromptArtifact($event)"><option value="">Select judge prompt</option>@for (artifact of store.data().artifacts?.items ?? []; track artifact.artifactId) { @if (artifact.purpose === 'judge_prompt') { <option [value]="artifact.artifactId">{{ artifact.artifactId }} · {{ artifact.artifactVersion }}</option> } }</select></label><label>Rubric artifact selection <select required aria-label="Qualification rubric artifact selection" [value]="selectedRubricArtifactId()" (change)="setSelectedRubricArtifact($event)"><option value="">Select judge rubric</option>@for (artifact of store.data().artifacts?.items ?? []; track artifact.artifactId) { @if (artifact.purpose === 'judge_rubric') { <option [value]="artifact.artifactId">{{ artifact.artifactId }} · {{ artifact.artifactVersion }}</option> } }</select></label><label>Authorization record ID <input required pattern="[0-9a-fA-F-]{36}" aria-label="Qualification authorization record ID" [value]="qualificationAuthorizationId()" (input)="setQualificationAuthorization($event)" /></label><label>Calibration result ID <input required pattern="[0-9a-fA-F-]{36}" aria-label="Calibration result ID" [value]="calibrationResultId()" (input)="setCalibrationResult($event)" /></label><label>Judge definition ID <input required pattern="[0-9a-fA-F-]{36}" aria-label="Judge definition ID" [value]="judgeDefinitionId()" (input)="setJudgeDefinition($event)" /></label><label>Judge definition version <input required pattern="[0-9]+\\.[0-9]+\\.[0-9]+" aria-label="Judge definition version" [value]="judgeDefinitionVersion()" (input)="setJudgeDefinitionVersion($event)" /></label><label>Model ID <input required aria-label="Local model ID" [value]="modelId()" (input)="setModelId($event)" /></label><label>Model version <input required aria-label="Local model version" [value]="modelVersion()" (input)="setModelVersion($event)" /></label><label>Model digest <input required pattern="[0-9a-fA-F]{64}" aria-label="Local model digest" [value]="modelDigest()" (input)="setModelDigest($event)" /></label><button class="primary" type="submit" [disabled]="!canQualify() || store.busyAction() === 'qualify'">{{ store.busyAction() === 'qualify' ? 'Qualifying…' : 'Qualify local judge' }}</button></form>@if (!canQualify()) { <p class="notice" role="status">Select exact dataset, suite, evaluator, eligible calibration, prompt/rubric artifacts, and bounded local model metadata before qualifying.</p> }<table class="data-table"><caption>Judge qualifications</caption><thead><tr><th scope="col">Qualification</th><th scope="col">State</th><th scope="col">Disposition</th><th scope="col">Calibration</th><th scope="col">Action</th></tr></thead><tbody>@for (qualification of store.data().qualifications?.items ?? []; track qualification.qualificationId) { <tr><th scope="row">{{ qualification.qualificationId }}</th><td data-label="State">{{ qualification.state }}</td><td data-label="Disposition">{{ qualification.scoreDisposition }}</td><td data-label="Calibration">{{ qualification.calibration.resourceId }}</td><td class="actions" data-label="Action">@if (qualification.state === 'qualified') { <button class="danger-link" type="button" (click)="revoke(qualification.qualificationId)" [disabled]="!!store.busyAction()">{{ store.busyAction() === 'revoke:' + qualification.qualificationId ? 'Revoke pending…' : 'Revoke' }}</button> } @else { <span class="muted">Revoked</span> }</td></tr> } @empty { <tr><td colspan="5" class="empty">No qualification history is available.</td></tr> }</tbody></table></section>
            }
            @case ('history') { <section class="panel" aria-labelledby="history-title"><h3 id="history-title">Evaluation history</h3>@if (store.rejectedScopes().includes('history')) { <div class="notice error" role="alert"><strong>History query was rejected.</strong><p>The other evaluation views are independent and may still be usable. Retry only this history scope or refresh all.</p><button type="button" class="secondary" (click)="retryScope('history')" [disabled]="!!store.busyAction()">Retry history</button></div> } @else if (store.unavailableScopes().includes('history')) { <div class="notice" role="status"><strong>History is incomplete.</strong><p>The history source returned a partial page; this does not mean the evaluation service is unavailable.</p><button type="button" class="secondary" (click)="retryScope('history')" [disabled]="!!store.busyAction()">Retry history</button></div> }<table class="data-table"><caption>Evaluation history</caption><thead><tr><th scope="col">Resource</th><th scope="col">Change</th><th scope="col">State</th><th scope="col">Occurred</th></tr></thead><tbody>@for (entry of store.data().history?.items ?? []; track entry.historyId) { <tr><th scope="row">{{ entry.resourceType }} <code>{{ entry.resourceId }}</code></th><td>{{ entry.change }}</td><td>{{ entry.state }}</td><td>{{ entry.occurredAt | date:'medium' }}</td></tr> } @empty { <tr><td colspan="4" class="empty">{{ store.rejectedScopes().includes('history') ? 'History rows cannot be shown until this scope is accepted.' : store.unavailableScopes().includes('history') ? 'No complete history rows are available in this snapshot.' : 'No history is available.' }}</td></tr> }</tbody></table></section> }
            @case ('retention') { <section class="panel" aria-labelledby="retention-title"><h3 id="retention-title">Data retention</h3><p class="muted">Evaluation data remains until an owner explicitly deletes it. These controls never delete catalogues or audit history.</p><div class="retention-actions"><label>Delete observations and artifacts before <input #cutoff type="datetime-local" aria-label="Deletion cutoff in local time" (input)="setCutoff(cutoff.value)" /></label><button class="danger" type="button" (click)="deleteBefore(cutoffValue())" [disabled]="!cutoffValue() || !!store.busyAction()">{{ store.busyAction()?.startsWith('delete:') ? 'Deleting…' : 'Delete before cutoff' }}</button><button class="danger" type="button" (click)="purgeAll()" [disabled]="!!store.busyAction()">{{ store.busyAction() === 'delete:purge_all' ? 'Purging…' : 'Purge all evaluation data' }}</button></div>@if (store.data().deletion; as deletion) { <div class="progress" role="status" aria-live="polite">Deletion {{ deletion.actionId }}: {{ deletion.status }}. Metadata removed: {{ deletion.deletedMetadataCount }}; artifacts removed: {{ deletion.deletedArtifactCount }}.</div> }</section> }
          }
          @if (nextCursor(); as cursor) { <div class="load-more" role="status" aria-live="polite"><span>More metadata is available (cursor {{ cursor.slice(0, 8) }}…).</span><button class="secondary" type="button" (click)="loadMore()" [disabled]="isLoadMoreBusy()">{{ isLoadMoreBusy() ? 'Loading…' : 'Load more' }}</button></div> }
        </div>
      }
      @if (pendingConfirmation; as confirmation) { <dialog #confirmationDialog class="confirmation-dialog" aria-labelledby="evaluation-confirmation-title" aria-describedby="evaluation-confirmation-description" (cancel)="cancelConfirmation($event)" (keydown)="handleConfirmationKeydown($event)"><h3 id="evaluation-confirmation-title" tabindex="-1">{{ confirmation.title }}</h3><p id="evaluation-confirmation-description"><strong>Target:</strong> {{ confirmation.resource }}<br /><strong>Scope:</strong> {{ confirmation.scope }}<br /><strong>Consequence:</strong> {{ confirmation.consequence }}</p>@if (dialogError) { <p class="notice error" role="alert">{{ dialogError }}</p> }<p class="action-progress" role="status" aria-live="polite">{{ dialogBusy ? 'Action in progress…' : 'Review the exact target and consequence before continuing.' }}</p><div class="dialog-actions"><button type="button" (click)="cancelConfirmation()" [disabled]="dialogBusy">Cancel</button><button class="danger" type="button" (click)="confirmPending()" [disabled]="dialogBusy">{{ dialogBusy ? 'Working…' : 'Confirm action' }}</button></div></dialog> }
    </section>
  `,
  styles: [`
    :host { display:block; }
    .evaluation { display:grid; gap:1rem; scroll-padding-block-start:1rem; }
    .section-heading,.panel-heading { align-items:flex-start; display:flex; gap:1rem; justify-content:space-between; }
    h2,h3,h4 { margin:.1rem 0 .45rem; }
    .eyebrow { color:var(--accent-hover); font-size:.75rem; font-weight:800; letter-spacing:.1em; margin:0; text-transform:uppercase; }
    .muted { color:var(--text-muted); }
    .live-banner,.notice { background:var(--info-surface); border:1px solid var(--border); border-radius:.55rem; padding:.7rem .85rem; }
    .live-banner { display:flex; flex-wrap:wrap; gap:1rem; justify-content:space-between; }
    .live-banner.partial,.live-banner.rejected,.notice.error { background:var(--warning-surface); color:var(--warning-text); }
    .action-progress { background:var(--info-surface); border:1px solid var(--border); border-radius:.55rem; color:var(--info-text); padding:.7rem .85rem; }
    .workspace-tabs { border-bottom:1px solid var(--border); display:flex; gap:.15rem; overflow:auto; }
    .workspace-tabs button { background:transparent; border:0; border-bottom:3px solid transparent; color:var(--text-muted); cursor:pointer; min-height:2.8rem; padding:.6rem .8rem; scroll-margin-block-start:1rem; white-space:nowrap; }
    .workspace-tabs button.active { border-bottom-color:var(--accent); color:var(--text); font-weight:700; }
    .mobile-tab-select { display:none; }
    button { border-radius:.45rem; cursor:pointer; padding:.55rem .8rem; position:relative; scroll-margin-block-start:1rem; z-index:1; }
    button:focus,input:focus,select:focus { outline:3px solid var(--focus); outline-offset:2px; }
    button:disabled { cursor:not-allowed; opacity:.55; }
    .primary { background:var(--accent); border:0; color:var(--on-accent); }
    .secondary { background:transparent; border:1px solid var(--control-border); color:var(--text); }
    .danger,.danger-link { background:var(--danger); border:0; color:var(--on-danger); }
    .danger-link { border-radius:.35rem; padding:.35rem .55rem; }
    .panel { background:var(--surface); border:1px solid var(--border); border-radius:.8rem; min-width:0; padding:clamp(1rem,3vw,1.5rem); scroll-margin-block-start:1rem; }
    .data-table { border-collapse:collapse; max-width:100%; width:100%; }
    .data-table th,.data-table td { border-bottom:1px solid var(--border); padding:.7rem .55rem; text-align:left; vertical-align:top; }
    .data-table th { font-size:.85rem; }
    .data-table tbody th { font-weight:650; }
    .data-table td.actions { display:flex; flex-wrap:wrap; gap:.4rem; min-width:13rem; }
    .state { background:var(--surface-subtle); border:1px solid var(--border); border-radius:999px; display:inline-block; padding:.15rem .45rem; }
    .job-cause { display:block; color:var(--warning-text); margin-top:.35rem; }
    .empty { color:var(--text-muted); padding:1.4rem!important; text-align:center!important; }
    .id-link { background:transparent; border:0; color:var(--accent-hover); padding:0; text-decoration:underline; }
    .metric-grid { display:grid; gap:.75rem; grid-template-columns:repeat(auto-fit,minmax(12rem,1fr)); }
    .metric { border:1px solid var(--border); border-radius:.55rem; display:grid; gap:.25rem; padding:.75rem; }
    .metric small { color:var(--text-muted); }
    .review-form,.suite-form { display:grid; gap:.75rem; grid-template-columns:repeat(auto-fit,minmax(12rem,1fr)); margin:1rem 0; }
    .suite-form fieldset { border:1px solid var(--border-subtle,#cbd5e1); border-radius:.5rem; display:grid; gap:.75rem; grid-column:1/-1; grid-template-columns:repeat(auto-fit,minmax(12rem,1fr)); margin:0; padding:1rem; }
    .suite-form legend { font-weight:700; padding:0 .35rem; }
    .suite-form .muted,.suite-form .notice,.suite-form .field-error,.suite-form .suite-actions { grid-column:1/-1; }
    .suite-form .field-error { color:var(--danger,#b42318); margin:0; }
    .suite-form [aria-invalid="true"] { border-color:var(--danger,#b42318); outline-color:var(--danger,#b42318); }
    .suite-actions { display:flex; flex-wrap:wrap; gap:.75rem; }
    .selected-suite-summary { border:2px solid var(--accent,#2563eb); border-radius:.5rem; margin:1rem 0; padding:1rem; }
    .selected-suite-summary:focus { outline:3px solid var(--focus,#2563eb); outline-offset:3px; }
    .selected-suite-summary dl { display:grid; gap:.5rem; margin:0; }
    .selected-suite-summary dl div { display:grid; gap:.25rem; grid-template-columns:minmax(7rem,12rem) minmax(0,1fr); }
    .selected-suite-summary dt { font-weight:700; }
    .selected-suite-summary dd { margin:0; overflow-wrap:anywhere; }
    .load-more { align-items:center; display:flex; gap:.75rem; justify-content:space-between; margin-top:1rem; }
    .retention-actions { align-items:end; display:flex; flex-wrap:wrap; gap:.75rem; }
    label { display:grid; gap:.3rem; }
    input,select { border:1px solid var(--control-border); border-radius:.35rem; min-width:0; padding:.55rem; }
    .progress { background:var(--success-surface); color:var(--success-text); margin-top:1rem; padding:.75rem; }
    .confirmation-dialog { background:var(--surface); border:2px solid var(--border); border-radius:.7rem; color:var(--text); margin:auto; max-height:min(90vh,42rem); max-width:min(34rem,calc(100vw - 2rem)); overflow:auto; padding:1.25rem; width:100%; }
    .confirmation-dialog::backdrop { background:rgb(0 0 0 / .62); }
    .dialog-actions { display:flex; flex-wrap:wrap; gap:.6rem; justify-content:flex-end; margin-top:1rem; }
    .workspace-metrics { align-items:center; background:var(--surface-raised); border:1px solid var(--border); display:flex; flex-wrap:wrap; gap:.8rem 1.4rem; padding:.8rem 1rem; }
    .workspace-metrics div { display:grid; gap:.1rem; min-width:7rem; }
    .workspace-metrics span { color:var(--text-muted); font-size:.72rem; font-weight:750; text-transform:uppercase; }
    .workspace-metrics strong { font-size:1.2rem; }
    .workspace-metrics p { color:var(--text-muted); font-size:.82rem; margin:0 0 0 auto; }
    code { overflow-wrap:break-word; }
    .sr-only { height:1px; margin:-1px; overflow:hidden; padding:0; position:absolute; width:1px; clip:rect(0,0,0,0); white-space:nowrap; }
    @media (max-width:900px) {
      .data-table { border:1px solid var(--border); border-radius:.55rem; display:block; overflow-x:auto; }
      .data-table thead { position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0 0 0 0); }
      .data-table tbody,.data-table tr { display:block; }
      .data-table tr { border-bottom:1px solid var(--border); padding:.5rem; }
      .data-table tr:last-child { border-bottom:0; }
      .data-table th,.data-table td { border:0; display:grid; gap:.5rem; grid-template-columns:minmax(7rem,38%) minmax(0,1fr); overflow-wrap:break-word; white-space:normal; }
      .data-table tbody th::before,.data-table td::before { color:var(--text-muted); font-size:.75rem; font-weight:700; }
      .data-table td[data-label]::before { content:attr(data-label) !important; }
      section[aria-labelledby="datasets-title"] .data-table:nth-of-type(1) tbody th::before { content:'Dataset'; }
      section[aria-labelledby="datasets-title"] .data-table:nth-of-type(1) td:nth-child(2)::before { content:'Version'; }
      section[aria-labelledby="datasets-title"] .data-table:nth-of-type(1) td:nth-child(3)::before { content:'Cases'; }
      section[aria-labelledby="datasets-title"] .data-table:nth-of-type(1) td:nth-child(4)::before { content:'State'; }
      section[aria-labelledby="datasets-title"] .data-table:nth-of-type(1) td:nth-child(5)::before { content:'Capture'; }
      section[aria-labelledby="datasets-title"] .data-table:nth-of-type(1) td.actions::before { content:'Action'; }
      section[aria-labelledby="datasets-title"] .data-table:nth-of-type(2) tbody th::before { content:'Case'; }
      section[aria-labelledby="datasets-title"] .data-table:nth-of-type(2) td:nth-child(2)::before { content:'Version'; }
      section[aria-labelledby="datasets-title"] .data-table:nth-of-type(2) td:nth-child(3)::before { content:'Artifacts'; }
      section[aria-labelledby="suites-title"] .data-table tbody th::before { content:'Suite'; }
      section[aria-labelledby="suites-title"] .data-table td:nth-child(2)::before { content:'Dataset'; }
      section[aria-labelledby="suites-title"] .data-table td:nth-child(3)::before { content:'Evaluators'; }
      section[aria-labelledby="suites-title"] .data-table td:nth-child(4)::before { content:'Budget'; }
      section[aria-labelledby="suites-title"] .data-table td:nth-child(5)::before { content:'State'; }
      section[aria-labelledby="suites-title"] .data-table td.actions::before { content:'Action'; }
      section[aria-labelledby="evaluators-title"] .data-table tbody th::before { content:'Evaluator'; }
      section[aria-labelledby="evaluators-title"] .data-table td:nth-child(2)::before { content:'Kind'; }
      section[aria-labelledby="evaluators-title"] .data-table td:nth-child(3)::before { content:'Metric'; }
      section[aria-labelledby="evaluators-title"] .data-table td:nth-child(4)::before { content:'Components'; }
      section[aria-labelledby="evaluators-title"] .data-table td:nth-child(5)::before { content:'Version'; }
      section[aria-labelledby="evaluators-title"] .data-table td.actions::before { content:'Action'; }
      section[aria-labelledby="jobs-title"] .data-table tbody th::before { content:'Job'; }
      section[aria-labelledby="jobs-title"] .data-table td:nth-child(2)::before { content:'Status'; }
      section[aria-labelledby="jobs-title"] .data-table td:nth-child(3)::before { content:'Attempts'; }
      section[aria-labelledby="jobs-title"] .data-table td:nth-child(4)::before { content:'Results'; }
      section[aria-labelledby="jobs-title"] .data-table td:nth-child(5)::before { content:'Recovery'; }
      section[aria-labelledby="jobs-title"] .data-table td.actions::before { content:'Action'; }
      section[aria-labelledby="results-title"] .data-table:nth-of-type(1) tbody th::before { content:'Result'; }
      section[aria-labelledby="results-title"] .data-table:nth-of-type(1) td:nth-child(2)::before { content:'Status'; }
      section[aria-labelledby="results-title"] .data-table:nth-of-type(1) td:nth-child(3)::before { content:'Scores'; }
      section[aria-labelledby="results-title"] .data-table:nth-of-type(1) td:nth-child(4)::before { content:'Evidence'; }
      section[aria-labelledby="results-title"] .data-table:nth-of-type(1) td:nth-child(5)::before { content:'Reproducible'; }
      section[aria-labelledby="results-title"] .data-table:nth-of-type(2) tbody th::before { content:'Metric'; }
      section[aria-labelledby="results-title"] .data-table:nth-of-type(2) td:nth-child(2)::before { content:'Value'; }
      section[aria-labelledby="results-title"] .data-table:nth-of-type(2) td:nth-child(3)::before { content:'Outcome'; }
      section[aria-labelledby="results-title"] .data-table:nth-of-type(2) td:nth-child(4)::before { content:'Disposition'; }
      section[aria-labelledby="artifacts-title"] .data-table tbody th::before { content:'Artifact'; }
      section[aria-labelledby="artifacts-title"] .data-table td:nth-child(2)::before { content:'Purpose'; }
      section[aria-labelledby="artifacts-title"] .data-table td:nth-child(3)::before { content:'Media'; }
      section[aria-labelledby="artifacts-title"] .data-table td:nth-child(4)::before { content:'Bytes'; }
      section[aria-labelledby="artifacts-title"] .data-table td:nth-child(5)::before { content:'Retention'; }
      section[aria-labelledby="reviews-title"] .data-table tbody th::before { content:'Review'; }
      section[aria-labelledby="reviews-title"] .data-table td:nth-child(2)::before { content:'Label'; }
      section[aria-labelledby="reviews-title"] .data-table td:nth-child(3)::before { content:'Outcome'; }
      section[aria-labelledby="reviews-title"] .data-table td:nth-child(4)::before { content:'Disagreement'; }
      section[aria-labelledby="reviews-title"] .data-table td:nth-child(5)::before { content:'Calibration'; }
      section[aria-labelledby="qualification-title"] .data-table tbody th::before { content:'Qualification'; }
      section[aria-labelledby="qualification-title"] .data-table td:nth-child(2)::before { content:'State'; }
      section[aria-labelledby="qualification-title"] .data-table td:nth-child(3)::before { content:'Disposition'; }
      section[aria-labelledby="qualification-title"] .data-table td:nth-child(4)::before { content:'Calibration'; }
      section[aria-labelledby="qualification-title"] .data-table td.actions::before { content:'Action'; }
      section[aria-labelledby="history-title"] .data-table tbody th::before { content:'Resource'; }
      section[aria-labelledby="history-title"] .data-table td:nth-child(2)::before { content:'Change'; }
      section[aria-labelledby="history-title"] .data-table td:nth-child(3)::before { content:'State'; }
      section[aria-labelledby="history-title"] .data-table td:nth-child(4)::before { content:'Occurred'; }
      .data-table td.actions { display:grid; grid-template-columns:minmax(7rem,38%) minmax(0,1fr); min-width:0; }
      .data-table td.actions button { width:max-content; max-width:100%; }
    }
    @media (max-width:640px) {
      .section-heading,.panel-heading { display:grid; }
      .workspace-metrics p { flex-basis:100%; margin:0; }
      .workspace-tabs { display:none; }
      .mobile-tab-select { display:grid; gap:.3rem; }
      .load-more { align-items:stretch; display:grid; }
      .retention-actions { align-items:stretch; display:grid; }
      .retention-actions button { width:100%; }
    }
    @media (prefers-reduced-motion:reduce) { *,*::before,*::after { scroll-behavior:auto!important; transition-duration:.01ms!important; } }
    @media (forced-colors:active) { .primary,.danger { background:ButtonFace !important; border:2px solid ButtonBorder !important; color:ButtonText !important; forced-color-adjust:auto; } }
  `, `
    .evaluation { gap:var(--obs-section-gap); }
    .section-heading { gap:1.25rem; }
    .section-heading > div { max-width:var(--obs-prose-max); }
    .live-banner,.action-progress { border-left:3px solid currentColor; border-radius:.45rem; padding:.85rem 1rem; }
    .recovery-region { align-items:flex-start; display:flex; flex-wrap:wrap; gap:1rem 1.5rem; justify-content:space-between; max-width:var(--obs-prose-max); }
    .recovery-region p { margin:.35rem 0 0; }
    .recovery-actions { align-items:center; display:flex; flex-wrap:wrap; gap:.65rem; }
    .retry-details { min-width:12rem; }
    .retry-details summary { color:var(--accent-hover); cursor:pointer; font-size:.82rem; font-weight:750; padding:.35rem 0; }
    .retry-scopes { display:flex; flex-wrap:wrap; gap:.5rem; margin-top:.5rem; }
    .workspace-tabs { gap:.35rem; padding-bottom:1px; }
    .workspace-tabs button { padding-inline:1rem; }
    .workspace-metrics { border-radius:.5rem; gap:1rem 2rem; padding:1rem 1.25rem; }
    .panel { border-radius:.65rem; padding:clamp(1.25rem,2vw,1.75rem); }
    .metric-grid { gap:1rem; }
    .metric { padding:1rem; }
    .review-form,.suite-form { gap:1rem; margin-top:1.25rem; }
    .data-table th,.data-table td { padding:.8rem .65rem; }
    .empty { background:var(--surface-subtle); border:1px dashed var(--control-border); border-radius:.45rem; }
    @media (max-width:640px) { .panel { padding:1rem; } .workspace-tabs button { padding-inline:.75rem; } }
    @media (min-width:641px) { .workspace-tabs { flex-wrap:wrap; overflow:visible; } }
  `],
})
export class EvaluationWorkspaceComponent {
  readonly store = inject(EvaluationWorkspaceStore);
  private readonly session = inject(ObservatoryStore);
  @ViewChild('confirmationDialog') private confirmationDialog?: ElementRef<HTMLDialogElement>;
  readonly tab = signal<EvaluationWorkspaceTab>('datasets');
  readonly cutoffValue = signal('');
  readonly suiteDatasetSelection = signal('');
  readonly suiteEvaluatorSelection = signal('');
  readonly suiteVersion = signal('1.0.0');
  readonly suiteComponentId = signal('');
  readonly suiteComponentVersion = signal('');
  readonly suiteCodeRevision = signal('');
  readonly suiteImageDigest = signal('');
  readonly suiteConfigurationRevision = signal('');
  readonly suiteCaseBudget = signal('1');
  readonly suiteTimeoutMs = signal('30000');
  readonly suiteMaxConcurrency = signal('1');
  readonly suiteMaxAttempts = signal('1');
  readonly suiteMaxCases = signal('1');
  readonly suiteMaxInputBytes = signal('0');
  readonly suiteMaxOutputBytes = signal('0');
  readonly suiteMaxTokens = signal('0');
  readonly suiteThresholdMetric = signal('quality.pass_rate');
  readonly suiteThresholdValue = signal('0.8');
  readonly suiteCreatedByComponent = signal('aura.observatory.evaluation_api');
  readonly suiteCreatedByVersion = signal('1.0.0');
  readonly suiteSourceDigest = signal('');
  readonly suiteRetentionMode = signal<'manual_only'>('manual_only');
  readonly suiteAcceptedSummary = signal<CreateEvaluationSuiteRequest | null>(null);
  pendingConfirmation: { readonly title: string; readonly resource: string; readonly scope: string; readonly consequence: string; readonly run: () => Promise<void>; readonly restore: HTMLElement | null; } | null = null;
  dialogBusy = false;
  dialogError: string | null = null;
  readonly suiteReadiness = computed(() => {
    const validity = this.suiteFieldValidity();
    const invalid = (Object.keys(validity) as SuiteField[]).filter((field) => !validity[field]);
    return {
      valid: invalid.length === 0,
      firstInvalid: invalid[0] ?? null,
      message: invalid.length ? `Suite is not ready. Complete ${invalid.join(', ')}.` : 'Suite is ready to compose with fixed At least (≥) threshold semantics.',
    } as const;
  });
  readonly reviewScore = signal('0.5');
  readonly reviewAuthorizationId = signal('');
  readonly reviewOutcome = signal<'pass' | 'fail' | 'inconclusive'>('pass');
  readonly reviewLabel = signal<'accepted' | 'rejected' | 'needs_review'>('accepted');
  readonly selectedReviewScoreId = signal('');
  readonly selectedReviewCalibrationId = signal('');
  readonly selectedQualificationCalibrationId = signal('');
  readonly selectedPromptArtifactId = signal('');
  readonly selectedRubricArtifactId = signal('');
  readonly qualificationAuthorizationId = signal('');
  readonly judgeDefinitionId = signal('');
  readonly judgeDefinitionVersion = signal('1.0.0');
  readonly modelId = signal('');
  readonly modelVersion = signal('');
  readonly modelDigest = signal('');
  readonly calibrationResultId = signal('');
  readonly jobModelId = signal('');
  readonly jobModelVersion = signal('');
  readonly jobModelDigest = signal('');
  readonly jobDeploymentRevision = signal('');
  readonly jobSeed = signal('0');
  readonly jobFixtureRevision = signal('');
  readonly jobPromptRevision = signal('');
  readonly jobPolicyRevision = signal('');
  readonly tabs: ReadonlyArray<{ id: EvaluationWorkspaceTab; label: string }> = [
    { id: 'datasets', label: 'Datasets & cases' }, { id: 'suites', label: 'Suites' }, { id: 'evaluators', label: 'Evaluators' }, { id: 'jobs', label: 'Jobs' }, { id: 'results', label: 'Results & scores' }, { id: 'artifacts', label: 'Artifacts' }, { id: 'reviews', label: 'Reviews & calibration' }, { id: 'qualification', label: 'Qualification' }, { id: 'history', label: 'History' }, { id: 'retention', label: 'Retention' },
  ];
  private hadAuthenticatedSession = false;
  private lastEvaluationRevision = 0;
  runningJobCount(): number { return this.store.data().jobs?.items.filter((job) => job.status === 'queued' || job.status === 'running').length ?? 0; }

  constructor() {
    void this.store.load();
    effect(() => {
      const authState = this.session.authState();
      if (authState === 'authenticated') this.hadAuthenticatedSession = true;
      if (this.store.state() === 'unauthorized' || (this.hadAuthenticatedSession && authState !== 'authenticated')) this.store.clear();
      if (authState === 'authenticated') this.store.setEvaluationLiveDisconnected(this.session.evaluationLiveDisconnected());
      const revision = this.session.evaluationRevision();
      if (revision > this.lastEvaluationRevision && authState === 'authenticated') {
        this.lastEvaluationRevision = revision;
        const change = this.session.evaluationChange();
        if (change) void this.store.refreshResource(change.resourceType);
        else void this.store.load();
      } else if (revision > this.lastEvaluationRevision) this.lastEvaluationRevision = revision;
    });
  }

  select(tab: EvaluationWorkspaceTab): void {
    this.tab.set(tab);
    queueMicrotask(() => {
      const tabElement = document.getElementById(`evaluation-tab-${tab}`);
      if (tabElement instanceof HTMLElement && tabElement.offsetParent !== null) tabElement.focus();
    });
  }
  selectFromEvent(event: Event): void { const target = event.target; if (target instanceof HTMLSelectElement) { const selected = this.tabs.find((item) => item.id === target.value); if (selected) this.select(selected.id); } }
  onTabKeydown(event: KeyboardEvent): void {
    const current = this.tabs.findIndex((item) => item.id === this.tab());
    if (current < 0 || !['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? this.tabs.length - 1 : (current + (event.key === 'ArrowLeft' || event.key === 'ArrowUp' ? -1 : 1) + this.tabs.length) % this.tabs.length;
    this.select(this.tabs[next].id);
  }
  ensureTabVisible(event: FocusEvent): void { if (event.currentTarget instanceof HTMLElement) event.currentTarget.scrollIntoView({ block: 'nearest', inline: 'nearest' }); }
  nextCursor(): string | null { const value = this.store.data(); switch (this.tab()) { case 'datasets': return value.datasets?.nextCursor ?? null; case 'suites': return value.suites?.nextCursor ?? null; case 'evaluators': return value.evaluators?.nextCursor ?? null; case 'jobs': return value.jobs?.nextCursor ?? null; case 'results': return value.results?.nextCursor ?? null; case 'artifacts': return value.artifacts?.nextCursor ?? null; case 'reviews': return value.reviews?.nextCursor ?? null; case 'qualification': return value.qualifications?.nextCursor ?? null; case 'history': return value.history?.nextCursor ?? null; default: return null; } }
  loadMore(): void { const mapping: Partial<Record<EvaluationWorkspaceTab, Parameters<EvaluationWorkspaceStore['loadMore']>[0]>> = { datasets: 'datasets', suites: 'suites', evaluators: 'evaluators', jobs: 'jobs', results: 'results', artifacts: 'artifacts', reviews: 'reviews', qualification: 'qualifications', history: 'history' }; const resource = mapping[this.tab()]; if (resource) void this.store.loadMore(resource); }
  isLoadMoreBusy(): boolean { const mapping: Partial<Record<EvaluationWorkspaceTab, string>> = { datasets: 'datasets', suites: 'suites', evaluators: 'evaluators', jobs: 'jobs', results: 'results', artifacts: 'artifacts', reviews: 'reviews', qualification: 'qualifications', history: 'history' }; return this.store.busyAction() === `load-more:${mapping[this.tab()]}`; }
  retryScope(scope: string): void { void this.store.retryScope(scope); }
  actionLabel(): string { const action = this.store.busyAction(); if (!action) return ''; if (action === 'submit') return 'Submitting evaluation…'; if (action === 'review') return 'Recording review…'; if (action === 'qualify') return 'Qualifying local judge…'; if (action.startsWith('cancel:')) return 'Cancellation pending…'; if (action.startsWith('delete:')) return 'Deletion pending…'; if (action.startsWith('load-more:')) return 'Loading additional metadata…'; return 'Updating evaluation metadata…'; }
  refresh(): void { void this.store.load(); }
  setCutoff(value: string): void { this.cutoffValue.set(value); }
  setSuiteDataset(event: Event): void { if (event.target instanceof HTMLSelectElement) this.suiteDatasetSelection.set(event.target.value); }
  setSuiteEvaluator(event: Event): void { if (event.target instanceof HTMLSelectElement) this.suiteEvaluatorSelection.set(event.target.value); }
  setSuiteVersion(event: Event): void { if (event.target instanceof HTMLInputElement) this.suiteVersion.set(event.target.value); }
  setSuiteComponentId(event: Event): void { if (event.target instanceof HTMLInputElement) this.suiteComponentId.set(event.target.value); }
  setSuiteComponentVersion(event: Event): void { if (event.target instanceof HTMLInputElement) this.suiteComponentVersion.set(event.target.value); }
  setSuiteCodeRevision(event: Event): void { if (event.target instanceof HTMLInputElement) this.suiteCodeRevision.set(event.target.value); }
  setSuiteImageDigest(event: Event): void { if (event.target instanceof HTMLInputElement) this.suiteImageDigest.set(event.target.value); }
  setSuiteConfigurationRevision(event: Event): void { if (event.target instanceof HTMLInputElement) this.suiteConfigurationRevision.set(event.target.value); }
  setSuiteLimit(name: 'caseBudget' | 'timeoutMs' | 'maxConcurrency' | 'maxAttempts' | 'maxCases' | 'maxInputBytes' | 'maxOutputBytes' | 'maxTokens', event: Event): void { if (!(event.target instanceof HTMLInputElement)) return; const value = event.target.value; const signals = { caseBudget: this.suiteCaseBudget, timeoutMs: this.suiteTimeoutMs, maxConcurrency: this.suiteMaxConcurrency, maxAttempts: this.suiteMaxAttempts, maxCases: this.suiteMaxCases, maxInputBytes: this.suiteMaxInputBytes, maxOutputBytes: this.suiteMaxOutputBytes, maxTokens: this.suiteMaxTokens }; signals[name].set(value); }
  setSuiteThresholdMetric(event: Event): void { if (event.target instanceof HTMLInputElement) this.suiteThresholdMetric.set(event.target.value); }
  setSuiteThresholdValue(event: Event): void { if (event.target instanceof HTMLInputElement) this.suiteThresholdValue.set(event.target.value); }
  setSuiteCreatedByComponent(event: Event): void { if (event.target instanceof HTMLInputElement) this.suiteCreatedByComponent.set(event.target.value); }
  setSuiteCreatedByVersion(event: Event): void { if (event.target instanceof HTMLInputElement) this.suiteCreatedByVersion.set(event.target.value); }
  setSuiteSourceDigest(event: Event): void { if (event.target instanceof HTMLInputElement) this.suiteSourceDigest.set(event.target.value); }
  setReviewScore(event: Event): void { if (event.target instanceof HTMLInputElement) this.reviewScore.set(event.target.value); }
  setReviewAuthorization(event: Event): void { if (event.target instanceof HTMLInputElement) this.reviewAuthorizationId.set(event.target.value); }
  setReviewOutcome(event: Event): void { if (event.target instanceof HTMLSelectElement && (event.target.value === 'pass' || event.target.value === 'fail' || event.target.value === 'inconclusive')) this.reviewOutcome.set(event.target.value); }
  setReviewLabel(event: Event): void { if (event.target instanceof HTMLSelectElement && (event.target.value === 'accepted' || event.target.value === 'rejected' || event.target.value === 'needs_review')) this.reviewLabel.set(event.target.value); }
  setSelectedReviewScore(event: Event): void { if (event.target instanceof HTMLSelectElement) this.selectedReviewScoreId.set(event.target.value); }
  setSelectedReviewCalibration(event: Event): void { if (event.target instanceof HTMLSelectElement) this.selectedReviewCalibrationId.set(event.target.value); }
  setSelectedQualificationCalibration(event: Event): void { if (event.target instanceof HTMLSelectElement) this.selectedQualificationCalibrationId.set(event.target.value); }
  setSelectedPromptArtifact(event: Event): void { if (event.target instanceof HTMLSelectElement) this.selectedPromptArtifactId.set(event.target.value); }
  setSelectedRubricArtifact(event: Event): void { if (event.target instanceof HTMLSelectElement) this.selectedRubricArtifactId.set(event.target.value); }
  setQualificationAuthorization(event: Event): void { if (event.target instanceof HTMLInputElement) this.qualificationAuthorizationId.set(event.target.value); }
  setJudgeDefinition(event: Event): void { if (event.target instanceof HTMLInputElement) this.judgeDefinitionId.set(event.target.value); }
  setJudgeDefinitionVersion(event: Event): void { if (event.target instanceof HTMLInputElement) this.judgeDefinitionVersion.set(event.target.value); }
  setModelId(event: Event): void { if (event.target instanceof HTMLInputElement) this.modelId.set(event.target.value); }
  setModelVersion(event: Event): void { if (event.target instanceof HTMLInputElement) this.modelVersion.set(event.target.value); }
  setModelDigest(event: Event): void { if (event.target instanceof HTMLInputElement) this.modelDigest.set(event.target.value); }
  setCalibrationResult(event: Event): void { if (event.target instanceof HTMLInputElement) this.calibrationResultId.set(event.target.value); }
  setJobModelId(event: Event): void { if (event.target instanceof HTMLInputElement) this.jobModelId.set(event.target.value); }
  setJobModelVersion(event: Event): void { if (event.target instanceof HTMLInputElement) this.jobModelVersion.set(event.target.value); }
  setJobModelDigest(event: Event): void { if (event.target instanceof HTMLInputElement) this.jobModelDigest.set(event.target.value); }
  setJobDeploymentRevision(event: Event): void { if (event.target instanceof HTMLInputElement) this.jobDeploymentRevision.set(event.target.value); }
  setJobSeed(event: Event): void { if (event.target instanceof HTMLInputElement) this.jobSeed.set(event.target.value); }
  setJobFixtureRevision(event: Event): void { if (event.target instanceof HTMLInputElement) this.jobFixtureRevision.set(event.target.value); }
  setJobPromptRevision(event: Event): void { if (event.target instanceof HTMLInputElement) this.jobPromptRevision.set(event.target.value); }
  setJobPolicyRevision(event: Event): void { if (event.target instanceof HTMLInputElement) this.jobPolicyRevision.set(event.target.value); }

  canBindJobMetadata(): boolean { const dataset = this.store.selectedDataset(); const suite = this.store.selectedSuite(); const evaluator = this.store.selectedEvaluator(); const suiteMatchesDataset = Boolean(dataset && suite?.dataset.resourceId === dataset.datasetId && suite.dataset.resourceVersion === dataset.datasetVersion); const suiteMatchesEvaluator = Boolean(evaluator && suite?.evaluators.some((binding) => binding.evaluatorId === evaluator.evaluatorId && binding.evaluatorVersion === evaluator.evaluatorVersion)); return Boolean(suiteMatchesDataset && suiteMatchesEvaluator && suite?.limits?.maxCases && suite.components?.length && suite.components.every((component) => component.componentId && component.componentVersion && component.codeRevision && component.imageDigest && component.configurationRevision) && evaluator?.kind && this.jobModelId() && /^\d+\.\d+\.\d+$/.test(this.jobModelVersion()) && /^[0-9a-fA-F]{64}$/.test(this.jobModelDigest()) && this.jobDeploymentRevision() && /^-?\d+$/.test(this.jobSeed()) && this.jobFixtureRevision() && this.jobPromptRevision() && this.jobPolicyRevision()); }
  bindJobMetadata(event: Event): void { event.preventDefault(); const suite = this.store.selectedSuite(); if (!this.canBindJobMetadata() || !suite?.limits) return; const model: EvaluationModelBinding = { modelId: this.jobModelId(), modelVersion: this.jobModelVersion(), modelDigest: this.jobModelDigest() }; const reproducibility: EvaluationReproducibility = { seed: Number(this.jobSeed()), clockMode: 'fixed', timeZone: 'UTC', locale: 'en-US', fixtureRevision: this.jobFixtureRevision(), promptRevision: this.jobPromptRevision(), policyRevision: this.jobPolicyRevision() }; const bindings: JobAdmissionBindings = { model, deploymentRevision: this.jobDeploymentRevision(), reproducibility, limits: suite.limits }; this.store.setJobAdmissionBindings(bindings); this.store.notice.set('Authoritative runner metadata is bound to the selected suite and evaluator.'); }

  canSubmitReview(): boolean { const data = this.store.data(); const score = data.scores?.items.find((item) => item.scoreId === this.selectedReviewScoreId()); const calibration = data.calibrations?.items.find((item) => item.calibrationId === this.selectedReviewCalibrationId()); return Boolean(this.store.selectedDataset() && this.store.selectedResult() && score && calibration && this.isUuid(this.reviewAuthorizationId()) && Number.isFinite(Number(this.reviewScore())) && Number(this.reviewScore()) >= 0 && Number(this.reviewScore()) <= 1); }
  submitReviewForm(event: Event): void { event.preventDefault(); const dataset = this.store.selectedDataset(); const result = this.store.selectedResult(); const data = this.store.data(); const score = data.scores?.items.find((item) => item.scoreId === this.selectedReviewScoreId()); const calibration = data.calibrations?.items.find((item) => item.calibrationId === this.selectedReviewCalibrationId()); if (!dataset || !result || !score || !calibration || !this.canSubmitReview()) return; const reviewId = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function' ? crypto.randomUUID() : '00000000-0000-4000-8000-000000000104'; void this.store.submitReview({ reviewId, reviewVersion: '1.0.0', calibration: { calibrationId: calibration.calibrationId, calibrationVersion: calibration.calibrationVersion }, caseId: score.caseId, resultId: result.resultId, scoreId: score.scoreId, reviewScore: Number(this.reviewScore()), reviewOutcome: this.reviewOutcome(), reviewLabel: this.reviewLabel(), disagreement: 'none', authorizationRecordId: this.reviewAuthorizationId(), retentionMode: 'manual_only', provenance: dataset.provenance, createdAt: new Date().toISOString() }); }
  canQualify(): boolean { const data = this.store.data(); const dataset = this.store.selectedDataset(); const suite = this.store.selectedSuite(); const evaluator = this.store.selectedEvaluator(); const calibration = data.calibrations?.items.find((item) => item.calibrationId === this.selectedQualificationCalibrationId() && item.eligibility === 'eligible'); const prompt = data.artifacts?.items.find((item) => item.artifactId === this.selectedPromptArtifactId() && item.purpose === 'judge_prompt'); const rubric = data.artifacts?.items.find((item) => item.artifactId === this.selectedRubricArtifactId() && item.purpose === 'judge_rubric'); return Boolean(dataset && suite && evaluator && calibration && prompt && rubric && this.isUuid(this.qualificationAuthorizationId()) && this.isUuid(this.calibrationResultId()) && this.isUuid(this.judgeDefinitionId()) && /^\d+\.\d+\.\d+$/.test(this.judgeDefinitionVersion()) && this.modelId() && this.modelVersion() && /^[0-9a-fA-F]{64}$/.test(this.modelDigest())); }
  qualifyForm(event: Event): void { event.preventDefault(); if (!this.canQualify()) return; const data = this.store.data(); const dataset = this.store.selectedDataset(); const suite = this.store.selectedSuite(); const evaluator = this.store.selectedEvaluator(); const calibration = data.calibrations?.items.find((item) => item.calibrationId === this.selectedQualificationCalibrationId() && item.eligibility === 'eligible'); const prompt = data.artifacts?.items.find((item) => item.artifactId === this.selectedPromptArtifactId() && item.purpose === 'judge_prompt'); const rubric = data.artifacts?.items.find((item) => item.artifactId === this.selectedRubricArtifactId() && item.purpose === 'judge_rubric'); if (!dataset || !suite || !evaluator || !calibration || !prompt || !rubric) return; const qualificationId = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function' ? crypto.randomUUID() : '00000000-0000-4000-8000-000000000105'; const now = new Date().toISOString(); this.openConfirmation({ title: 'Qualify local judge', resource: qualificationId, scope: `Dataset ${dataset.datasetId}@${dataset.datasetVersion}; suite ${suite.suiteId}@${suite.suiteVersion}; evaluator ${evaluator.evaluatorId}@${evaluator.evaluatorVersion}`, consequence: 'This enables calibrated judge scores for the exact persisted metadata selected in this form.', run: () => this.store.qualifyJudge({ qualificationId, qualificationVersion: '1.0.0', action: 'qualify', actionMode: 'manual_owner_action', dataset: { id: dataset.datasetId, version: dataset.datasetVersion }, suite: { id: suite.suiteId, version: suite.suiteVersion }, evaluator: { evaluatorId: evaluator.evaluatorId, evaluatorVersion: evaluator.evaluatorVersion, kind: evaluator.kind }, judgeDefinition: { judgeDefinitionId: this.judgeDefinitionId(), judgeDefinitionVersion: this.judgeDefinitionVersion() }, model: { provider: 'local_ollama', modelName: this.modelId(), modelVersion: this.modelVersion(), modelDigest: this.modelDigest() }, prompt: { artifactId: prompt.artifactId, artifactVersion: prompt.artifactVersion, purpose: 'judge_prompt', digest: prompt.digest }, rubric: { artifactId: rubric.artifactId, artifactVersion: rubric.artifactVersion, purpose: 'judge_rubric', digest: rubric.digest }, calibration: { calibrationId: calibration.calibrationId, calibrationVersion: calibration.calibrationVersion }, calibrationResultId: this.calibrationResultId(), calibrationEligibility: 'eligible', authorizationRecordId: this.qualificationAuthorizationId(), scoreDisposition: 'calibrated_enabled', qualifiedAt: now, retentionMode: 'manual_only', provenance: dataset.provenance, createdAt: now }) }); }
  revoke(qualificationId: string): void { if (!this.isUuid(this.qualificationAuthorizationId())) { this.store.notice.set('Provide the owner authorization record before revoking qualification.'); return; } this.openConfirmation({ title: 'Revoke judge qualification', resource: qualificationId, scope: 'The selected qualification record', consequence: 'This disables calibrated judge scores for this qualification. The audit history remains available.', run: () => this.store.revokeQualification(qualificationId, { action: 'revoke', authorizationRecordId: this.qualificationAuthorizationId(), revokedAt: new Date().toISOString() }) }); }
  private isUuid(value: string): boolean { return /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89ab][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$/.test(value); }

  createSyntheticDataset(): void { const datasetId = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function' ? crypto.randomUUID() : '00000000-0000-4000-8000-000000000101'; const caseId = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function' ? crypto.randomUUID() : '00000000-0000-4000-8000-000000000102'; const request: CreateEvaluationDatasetRequest = { datasetId, datasetVersion: '1.0.0', sourceKind: 'synthetic', scopeIds: ['environment'], captureClassification: 'metadata_only', cases: [{ caseId, caseVersion: '1.0.0', artifactIds: [], captureClassification: 'metadata_only' }], retentionMode: 'manual_only' }; void this.store.createDataset(request); }
  private suiteFieldValidity(): Readonly<Record<SuiteField, boolean>> {
    const [datasetId, datasetVersion] = this.suiteDatasetSelection().split('|');
    const [evaluatorId, evaluatorVersion] = this.suiteEvaluatorSelection().split('|');
    const dataset = this.store.data().datasets?.items.find((item) => item.datasetId === datasetId && item.datasetVersion === datasetVersion);
    const evaluator = this.store.data().evaluators?.items.find((item) => item.evaluatorId === evaluatorId && item.evaluatorVersion === evaluatorVersion);
    const integer = (value: string, minimum: number, maximum: number): boolean => /^\d+$/.test(value) && Number(value) >= minimum && Number(value) <= maximum;
    return {
      dataset: Boolean(dataset),
      evaluator: evaluator?.kind === 'deterministic',
      definition: /^\d+\.\d+\.\d+$/.test(this.suiteVersion()),
      component: Boolean(this.suiteComponentId()) && /^\d+\.\d+\.\d+$/.test(this.suiteComponentVersion()) && Boolean(this.suiteCodeRevision()) && /^[0-9a-fA-F]{64}$/.test(this.suiteImageDigest()) && Boolean(this.suiteConfigurationRevision()),
      limits: integer(this.suiteCaseBudget(), 1, 10000) && integer(this.suiteTimeoutMs(), 1, 3600000) && integer(this.suiteMaxConcurrency(), 1, 128) && integer(this.suiteMaxAttempts(), 1, 10) && integer(this.suiteMaxCases(), 1, 10000) && integer(this.suiteMaxInputBytes(), 0, 100000000) && integer(this.suiteMaxOutputBytes(), 0, 100000000) && integer(this.suiteMaxTokens(), 0, 1000000),
      threshold: /^[a-zA-Z][a-zA-Z0-9_.-]{1,63}$/.test(this.suiteThresholdMetric()) && Number.isFinite(Number(this.suiteThresholdValue())) && Number(this.suiteThresholdValue()) >= 0 && Number(this.suiteThresholdValue()) <= 1,
      provenance: Boolean(this.suiteCreatedByComponent()) && /^\d+\.\d+\.\d+$/.test(this.suiteCreatedByVersion()) && /^[0-9a-fA-F]{64}$/.test(this.suiteSourceDigest()),
    };
  }
  suiteFieldValid(field: SuiteField): boolean { return this.suiteFieldValidity()[field]; }
  suiteGroupError(group: Exclude<SuiteField, 'dataset' | 'evaluator'> | 'definition'): string {
    const fields: Record<string, SuiteField[]> = { definition: ['dataset', 'evaluator', 'definition'], component: ['component'], limits: ['limits'], threshold: ['threshold'], provenance: ['provenance'] };
    const invalid = fields[group].filter((field) => !this.suiteFieldValid(field));
    return invalid.length ? 'Complete valid ' + invalid.join(', ') + ' fields before composing.' : '';
  }
  focusFirstInvalidSuiteField(): void {
    const controls = Array.from(document.querySelectorAll<HTMLInputElement | HTMLSelectElement>('.suite-form input, .suite-form select'));
    const invalid = controls.find((control) => !control.checkValidity());
    (invalid ?? controls[0])?.focus();
  }
  canComposeSuite(): boolean { return this.suiteReadiness().valid; }
  async createBoundSuite(event?: Event): Promise<void> {
    event?.preventDefault();
    if (!this.canComposeSuite()) { this.focusFirstInvalidSuiteField(); return; }
    const [datasetId, datasetVersion] = this.suiteDatasetSelection().split('|');
    const [evaluatorId, evaluatorVersion] = this.suiteEvaluatorSelection().split('|');
    const dataset = this.store.data().datasets?.items.find((item) => item.datasetId === datasetId && item.datasetVersion === datasetVersion);
    const evaluator = this.store.data().evaluators?.items.find((item) => item.evaluatorId === evaluatorId && item.evaluatorVersion === evaluatorVersion && item.kind === 'deterministic');
    if (!dataset || !evaluator) { this.focusFirstInvalidSuiteField(); return; }
    const suiteId = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function' ? crypto.randomUUID() : '00000000-0000-4000-8000-000000000106';
    const request: CreateEvaluationSuiteRequest = { suiteId, suiteVersion: this.suiteVersion(), dataset: { id: dataset.datasetId, version: dataset.datasetVersion }, evaluators: [{ evaluatorId: evaluator.evaluatorId, evaluatorVersion: evaluator.evaluatorVersion, kind: 'deterministic' }], components: [{ componentId: this.suiteComponentId(), componentVersion: this.suiteComponentVersion(), codeRevision: this.suiteCodeRevision(), imageDigest: this.suiteImageDigest(), configurationRevision: this.suiteConfigurationRevision() }], limits: { timeoutMs: Number(this.suiteTimeoutMs()), maxConcurrency: Number(this.suiteMaxConcurrency()), maxAttempts: Number(this.suiteMaxAttempts()), maxCases: Number(this.suiteMaxCases()), maxInputBytes: Number(this.suiteMaxInputBytes()), maxOutputBytes: Number(this.suiteMaxOutputBytes()), maxTokens: Number(this.suiteMaxTokens()) }, manualThresholds: { [this.suiteThresholdMetric()]: Number(this.suiteThresholdValue()) }, retentionMode: this.suiteRetentionMode(), provenance: { createdByComponent: this.suiteCreatedByComponent(), createdByVersion: this.suiteCreatedByVersion(), sourceDigest: this.suiteSourceDigest() }, createdAt: new Date().toISOString() };
    await this.store.createSuite(request);
    const created = this.store.selectedSuite();
    if (created?.suiteId === request.suiteId && created.suiteVersion === request.suiteVersion) {
      this.suiteAcceptedSummary.set(request);
      setTimeout(() => document.getElementById('created-suite-summary')?.focus(), 0);
    }
  }
  inspectDataset(dataset: Parameters<EvaluationWorkspaceStore['inspectDataset']>[0]): void { void this.store.inspectDataset(dataset); }
  selectSuite(suite: Parameters<EvaluationWorkspaceStore['selectSuite']>[0]): void { this.store.selectSuite(suite); }
  selectEvaluator(evaluator: Parameters<EvaluationWorkspaceStore['selectEvaluator']>[0]): void { this.store.selectEvaluator(evaluator); }
  submit(): void { void this.store.submitJob(); }
  cancel(job: EvaluationJob): void { this.openConfirmation({ title: 'Cancel evaluation job', resource: job.jobId, scope: 'Exactly this queued or running evaluation job', consequence: 'The job will stop and be recorded as cancelled; completed metadata is not erased.', run: () => this.store.cancelJob(job) }); }
  deleteJob(jobId: string): void { this.openConfirmation({ title: 'Delete evaluation job', resource: jobId, scope: 'Exactly this evaluation job', consequence: 'Its evaluation observations and artifacts are permanently removed. Audit history remains.', run: () => this.store.delete('job', jobId) }); }
  deleteDataset(datasetId: string): void { this.openConfirmation({ title: 'Delete evaluation dataset', resource: datasetId, scope: 'Exactly this dataset and its observations', consequence: 'The dataset observations and artifacts are permanently removed. Catalogues and audit history remain.', run: () => this.store.delete('dataset', datasetId) }); }
  deleteSuite(suiteId: string): void { this.openConfirmation({ title: 'Delete evaluation suite', resource: suiteId, scope: 'Exactly this suite and its observations', consequence: 'The suite observations and artifacts are permanently removed. Immutable definitions are not recreated.', run: () => this.store.delete('suite', suiteId) }); }
  openResult(result: EvaluationResult): void { void this.store.inspectResult(result.resultId); }
  deleteBefore(cutoff: string): void { const iso = new Date(cutoff).toISOString(); this.openConfirmation({ title: 'Delete evaluation data before cutoff', resource: iso, scope: 'All observations and artifacts before this UTC cutoff', consequence: 'Matching evaluation data is permanently removed; catalogues and audit history are retained.', run: () => this.store.delete('cutoff', undefined, iso) }); }
  purgeAll(): void { this.openConfirmation({ title: 'Purge all evaluation data', resource: 'All evaluation observations and artifacts', scope: 'Every evaluation dataset, suite, job, result, and artifact in this owner workspace', consequence: 'All matching evaluation data is permanently removed. Catalogues and audit history remain.', run: () => this.store.delete('purge_all') }); }
  private openConfirmation(action: Omit<NonNullable<EvaluationWorkspaceComponent['pendingConfirmation']>, 'restore'>): void { if (this.dialogBusy || this.pendingConfirmation) return; this.pendingConfirmation = { ...action, restore: document.activeElement instanceof HTMLElement ? document.activeElement : null }; this.dialogError = null; setTimeout(() => { const dialog = this.confirmationDialog?.nativeElement; if (dialog && !dialog.open) dialog.showModal(); dialog?.querySelector<HTMLHeadingElement>('h3')?.focus(); }); }
  private restoreConfirmationFocus(action: NonNullable<EvaluationWorkspaceComponent['pendingConfirmation']>): void { if (action.restore?.isConnected) action.restore.focus(); else document.getElementById('evaluation-title')?.focus(); }
  cancelConfirmation(event?: Event): void { event?.preventDefault(); if (this.dialogBusy) return; this.confirmationDialog?.nativeElement.close(); const action = this.pendingConfirmation; this.pendingConfirmation = null; this.dialogError = null; if (action) this.restoreConfirmationFocus(action); }
  async confirmPending(): Promise<void> { const action = this.pendingConfirmation; if (!action || this.dialogBusy) return; this.dialogBusy = true; this.dialogError = null; try { await action.run(); this.confirmationDialog?.nativeElement.close(); this.pendingConfirmation = null; setTimeout(() => this.restoreConfirmationFocus(action)); } catch { this.dialogError = 'The action could not be completed. Your draft is preserved; retry when the service is available.'; } finally { this.dialogBusy = false; } }
  handleConfirmationKeydown(event: KeyboardEvent): void { if (event.key === 'Escape') { event.preventDefault(); this.cancelConfirmation(event); return; } if (event.key !== 'Tab') return; const dialog = this.confirmationDialog?.nativeElement; if (!dialog) return; const focusable = Array.from(dialog.querySelectorAll<HTMLElement>('button:not([disabled]), h3[tabindex="-1"]')); if (!focusable.length) return; const first = focusable[0]; const last = focusable[focusable.length - 1]; if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); } else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); } }
  @HostListener('window:keydown', ['$event']) handleWindowConfirmationKeydown(event: KeyboardEvent): void { if (event.key === 'Escape' && this.pendingConfirmation && !this.dialogBusy) this.cancelConfirmation(event); }
  sanitizedJobCause(job: EvaluationJob): string { const causes: Record<string, string> = { runner_failed: 'Runner failed', provider_unavailable: 'Provider unavailable', cancelled_by_owner: 'Cancelled by owner', deadline: 'Deadline exceeded' }; return causes[job.failureCode ?? ''] ?? (job.status === 'cancelled' ? 'Cancelled' : 'Unavailable'); }
  inspectFailedJob(job: EvaluationJob): void { this.store.notice.set(`Job ${job.jobId} is ${job.status}; only bounded metadata is available for inspection.`); }
  recoverJob(job: EvaluationJob): void { if (job.status !== 'failed' && job.status !== 'cancelled') return; const provenance = job.provenance; this.jobModelId.set(provenance.model.modelId); this.jobModelVersion.set(provenance.model.modelVersion); this.jobModelDigest.set(provenance.model.modelDigest); this.jobDeploymentRevision.set(provenance.deploymentRevision); this.jobSeed.set(String(provenance.reproducibility.seed)); this.jobFixtureRevision.set(provenance.reproducibility.fixtureRevision); this.jobPromptRevision.set(provenance.reproducibility.promptRevision); this.jobPolicyRevision.set(provenance.reproducibility.policyRevision); this.select('jobs'); this.store.notice.set(`Prepared a new evaluation from job ${job.jobId}. Review the authoritative selections and bind metadata before submitting.`); }
  stateLabel(): string { const state = this.store.state(); const scopes = this.store.unavailableScopes(); const rejected = this.store.rejectedScopes(); if (state === 'partial' && scopes.length) return `Some evaluation data is unavailable: ${scopes.join(', ')}.`; if (state === 'rejected' && rejected.length) return `Evaluation views were rejected: ${rejected.join(', ')}. Retry a scope or refresh all.`; return ({ loading: 'Loading evaluation metadata…', ready: 'Evaluation metadata is current.', empty: 'No evaluation metadata is available.', partial: 'Some evaluation data is unavailable; showing a partial snapshot.', rejected: 'Evaluation data was rejected; retry the affected scope.', stale: 'Showing a stale evaluation snapshot.', disconnected: 'Live evaluation updates are disconnected.', unauthorized: 'Your owner session is required.', 'provider-unavailable': 'The local judge provider is unavailable; deterministic metadata remains available.', error: 'Evaluation service unavailable.' } as Record<string, string>)[state]; }
}
