import { appendFileSync, existsSync, readFileSync } from "node:fs";
import { ROUTES } from "./routes.js";

/**
 * Route coverage for the conformance suite: every route in ROUTES (the
 * deejaytools-api contract) must be called by at least one test, or be
 * listed in NOT_EXERCISED with the reason it is not. Checked once the whole
 * suite has run (global-setup.ts's teardown).
 *
 * Copied from deejaytools-api. The one change: the route table is the fixed
 * list in routes.ts rather than the in-process app's, because this copy only
 * drives a target over HTTP.
 */

/** "METHOD /path/pattern" → why no test calls it (yet). */
export const NOT_EXERCISED: Record<string, string> = {};

// Hits are shared through a file: each test file runs in its own module
// scope, and the check runs in the main process. Global setup creates the
// directory and hands it to the workers.
let HITS_FILE = "";

export function useLedgerDir(dir: string): void {
  HITS_FILE = `${dir}/route-hits.txt`;
}

/** Called by the harness for every request a test makes. */
export function recordHit(method: string, path: string): void {
  if (HITS_FILE) appendFileSync(HITS_FILE, `${method.toUpperCase()} ${path.split("?")[0]}\n`);
}

function toMatcher(pattern: string): { pattern: string; re: RegExp; params: number } {
  const [method, path] = pattern.split(" ");
  const params = (path.match(/:[^/]+/g) ?? []).length;
  const body = path.replace(/[.+*?^${}()|[\]\\]/g, "\\$&").replace(/:[^/]+/g, "[^/]+");
  return { pattern, re: new RegExp(`^${method} ${body}$`), params };
}

/** The route pattern a concrete request matched: the most specific one, as
 * the router picks a static segment over a parameter. */
export function matchRoute(hit: string, patterns: readonly string[]): string | undefined {
  return patterns
    .map(toMatcher)
    .filter((m) => m.re.test(hit))
    .sort((a, b) => a.params - b.params)[0]?.pattern;
}

/** The problems with the ledger once the suite has run; empty when it holds. */
export function ledgerProblems(): string[] {
  const hits = existsSync(HITS_FILE) ? readFileSync(HITS_FILE, "utf8").split("\n").filter(Boolean) : [];
  const exercised = new Set(hits.map((h) => matchRoute(h, ROUTES)).filter((r): r is string => !!r));

  const problems: string[] = [];
  for (const route of ROUTES) {
    if (!exercised.has(route) && !(route in NOT_EXERCISED)) {
      problems.push(`${route} — no test calls it. Add one, or list it in NOT_EXERCISED with the reason.`);
    }
  }
  for (const route of Object.keys(NOT_EXERCISED)) {
    if (!ROUTES.includes(route)) problems.push(`${route} is in NOT_EXERCISED but the contract has no such route.`);
    else if (exercised.has(route)) problems.push(`${route} is in NOT_EXERCISED but a test now calls it; remove the entry.`);
  }
  return problems;
}
