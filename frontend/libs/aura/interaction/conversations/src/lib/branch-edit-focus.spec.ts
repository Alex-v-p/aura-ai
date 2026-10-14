import { AfterViewChecked, Component, signal } from '@angular/core';
import { getTestBed, TestBed } from '@angular/core/testing';
import { BrowserDynamicTestingModule, platformBrowserDynamicTesting } from '@angular/platform-browser-dynamic/testing';
import { afterEach, describe, expect, it } from 'vitest';
import { focusBranchEditTarget, retryBranchActionAndFocus, type BranchEditFocusRequest } from './branch-edit-focus';

getTestBed().initTestEnvironment(BrowserDynamicTestingModule, platformBrowserDynamicTesting());

@Component({ standalone: true, template: '<span hidden>{{ pending().triggerId }}</span> @if (showTarget()) { <button id="edit-branch-user-alt" data-branch-edit-trigger data-focus-test-target>Alternative edit</button> }' })
class BranchEditFocusHarness implements AfterViewChecked {
  readonly pending = signal<BranchEditFocusRequest>({ triggerId: 'edit-branch-original', fallback: 'first-edit-or-toolbar' });
  readonly showTarget = signal(false);

  ngAfterViewChecked(): void {
    if (focusBranchEditTarget(this.pending())) this.pending.set({ triggerId: '', fallback: 'first-edit-or-toolbar' });
  }
}

describe('branch edit focus rendering', () => {
  afterEach(() => {
    document.querySelector('[data-focus-test-target]')?.remove();
    TestBed.resetTestingModule();
  });

  it('focuses a connected replacement action during the committed view check', () => {
    TestBed.configureTestingModule({ imports: [BranchEditFocusHarness] });
    const fixture = TestBed.createComponent(BranchEditFocusHarness);
    fixture.detectChanges();

    const replacement = (): HTMLElement | null => document.getElementById('edit-branch-user-alt');
    fixture.componentInstance.showTarget.set(true);
    fixture.changeDetectorRef.markForCheck();

    fixture.detectChanges();

    expect(document.activeElement).toBe(replacement());
  });

  it('awaits the retry wrapper and focuses only after a successful edit branch', async () => {
    TestBed.configureTestingModule({ imports: [BranchEditFocusHarness] });
    const fixture = TestBed.createComponent(BranchEditFocusHarness);
    fixture.detectChanges();
    const retry = async (): Promise<boolean> => {
      fixture.componentInstance.showTarget.set(true);
      fixture.changeDetectorRef.markForCheck();
      fixture.detectChanges();
      return true;
    };
    let focused = false;
    await retryBranchActionAndFocus('edit', retry, () => { focused = document.activeElement?.id === 'edit-branch-user-alt'; });
    expect(focused).toBe(true);
  });
});
