import { describe, expect, it } from 'vitest';
import { taskSourceLabel } from './tasks-models';

describe('task models', () => { it('normalizes source labels for the Task Center', () => { expect(taskSourceLabel('research')).toBe('Web research'); expect(taskSourceLabel('memory_reindex')).toBe('Memory reindex'); }); });
