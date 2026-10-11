export class ReminderSessionHydrator {
  private token = '';
  private pending: Promise<void> | null = null;
  private epoch = 0;
  constructor(private readonly loadToken: () => Promise<string>, private readonly sessionChanged: () => Error) {}
  clearSession(): void { this.epoch += 1; this.token = ''; this.pending = null; }
  currentToken(): string { return this.token; }
  async ensure(): Promise<void> {
    if (this.token) return;
    if (this.pending) return this.pending;
    const epoch = this.epoch;
    const promise = this.loadToken().then((token) => { if (epoch !== this.epoch) throw this.sessionChanged(); this.token = token; });
    this.pending = promise;
    try { await promise; } finally { if (this.pending === promise) this.pending = null; }
  }
}
