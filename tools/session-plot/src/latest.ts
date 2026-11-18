// Tracks the most recent request so a response that arrives after a newer one can be dropped.
// start() begins a request; mark() reads the current token without starting one.
export class LatestRequest {
  private n = 0;

  start(): number {
    return ++this.n;
  }

  mark(): number {
    return this.n;
  }

  isCurrent(token: number): boolean {
    return token === this.n;
  }
}
