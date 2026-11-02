import { describe, expect, it } from 'vitest';
import { emptyArtifactCommand } from './artifacts-models';

describe('artifact models', () => { it('creates an expected-version command without local persistence', () => { expect(emptyArtifactCommand(4)).toEqual({ expectedVersion: 4 }); }); });
