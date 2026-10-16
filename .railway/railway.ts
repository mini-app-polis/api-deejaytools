// api-deejaytools as Railway Infrastructure as Code, applied by CI
// (.github/workflows/railway-apply.yml): a push to dev that changes .railway/
// applies it to development; the dev → main pull request is planned
// against production (.github/workflows/railway-plan.yml), and merging it
// applies to production, refusing to remove anything unless the pull request
// is labelled allow-destroy. As mini-app-polis/infra applies Terraform.
// Nothing is applied from a workstation, and Railway itself does not read
// this file when it deploys. Replaces railway.json, which Railway
// never applied to this service and stops reading on 2026-12-01. MINI-69.
//
// A named partial: the ecosystem project holds services from several
// repositories, so this file manages api-deejaytools and nothing else.
// Removing the service from this file would delete it on the next apply.
//
// The start command is the image's CMD (Dockerfile), and is deliberately not
// declared here as well: two declarations are two to keep in step.
//
// Variables are references to the environment's shared variables, which
// Doppler keeps current (`ctx.shared.NAME` is `${{shared.NAME}}`); no value
// lives in this file. Only the names the service reads are declared — the
// Settings fields in src/api_deejaytools/config.py and what
// mini_app_polis reads on its behalf. The two that are not shared, the
// database URL (a reference to Postgres-deejaytools) and PORT, are kept as
// Railway has them.

import { defineRailway, github, preserve, project, service } from "railway/iac";

export const partial = "api-deejaytools";

const REGION = "us-east4-eqdc4a";

const BRANCH: Record<string, string> = { production: "main", development: "dev" };
const DOMAIN: Record<string, string> = {
  production: "api.deejaytools.com",
  development: "api-dev.deejaytools.com",
};

const SHARED = [
  "AWS_REGION",
  "BREVO_API_KEY",
  "DEEJAYTOOLS_BREVO_API_KEY",
  "DEEJAYTOOLS_CLERK_ISSUER",
  "DEEJAYTOOLS_CLERK_JWKS_URL",
  "DEEJAYTOOLS_CORS_ORIGINS",
  "DISCORD_WEBHOOK_URL_ACTIVITY",
  "DISCORD_WEBHOOK_URL_ERRORS",
  "ENVIRONMENT",
  "EVALUATION_QUEUE_PRODUCER_KEY_ID",
  "EVALUATION_QUEUE_PRODUCER_SECRET",
  "GOOGLE_DRIVE_PARENT_FOLDER_ID",
  "GOOGLE_SERVICE_ACCOUNT_EMAIL",
  "GOOGLE_SERVICE_ACCOUNT_PRIVATE_KEY",
  "LOGGING_LEVEL",
  "SENTRY_DSN_APIS",
  "TICK_SECRET",
];

export default defineRailway((ctx) => {
  const environment = ctx.environment ?? "";
  const name = ctx.projectName ?? "mini-app-polis ecosystem";
  if (!(environment in BRANCH)) {
    return project(name, { resources: [] });
  }

  const api = service("api-deejaytools", {
    source: github("mini-app-polis/api-deejaytools", { branch: BRANCH[environment] }),
    // Root Dockerfile with digest-pinned bases and a non-root user (CD-021).
    build: { builder: "DOCKERFILE" as const, dockerfilePath: "Dockerfile" },
    replicas: { [REGION]: 1 },
    domains: [{ domain: DOMAIN[environment], port: 3001 }],
    deploy: {
      healthcheckPath: "/health",
      healthcheckTimeout: 300,
      restartPolicyType: "ON_FAILURE" as const,
      restartPolicyMaxRetries: 10,
      limitOverride: { containers: { cpu: 4, memoryBytes: 4294967296 } },
    },
    env: {
      ...Object.fromEntries(SHARED.map((n) => [n, ctx.shared[n]])),
      DEEJAYTOOLS_DATABASE_URL: preserve(),
      PORT: preserve(),
    },
  });

  return project(name, { resources: [api] });
});
