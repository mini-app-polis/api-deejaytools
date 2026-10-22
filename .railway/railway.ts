// api-deejaytools as Railway Infrastructure as Code, applied by CI: a pull
// request into main that changes .railway/ is planned
// (.github/workflows/railway-plan.yml), and merging it plans again and
// applies (railway-apply.yml), refusing to remove anything unless the pull
// request is labelled allow-destroy. As mini-app-polis/infra applies
// Terraform. Nothing is applied from a workstation, and Railway itself does
// not read this file when it deploys. Replaces railway.json, which Railway
// never applied to this service and stops reading on 2026-12-01.
//
// Production only. Planned against any other environment it declares
// nothing, and a partial that owns nothing there changes nothing; the
// development service keeps the settings its dashboard holds. Development
// is not managed here because Railway refuses `railway config plan` with a
// development-scoped project token ("Unauthorized", even for a file that
// declares nothing) while the same plan works with a production one
// (Oct 2026). Add it back when Railway fixes that.
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
//
// The rest of the Settings fields the service reads in production are not
// shared either, and are kept as Railway has them with preserve(): set on
// the service when needed and left unset otherwise, so the code's default
// applies without Doppler holding every one of them.
// tests/unit/test_railway_variables.py fails when a Settings field is
// neither declared here nor listed there as deliberately left out.

import { defineRailway, github, preserve, project, service } from "railway/iac";

export const partial = "api-deejaytools";

const REGION = "us-east4-eqdc4a";

const SHARED = [
  "AWS_REGION",
  "BREVO_API_KEY",
  "DEEJAYTOOLS_CLERK_ISSUER",
  "DEEJAYTOOLS_CLERK_JWKS_URL",
  "DEEJAYTOOLS_CORS_ORIGINS",
  "DISCORD_WEBHOOK_URL_ACTIVITY",
  "DISCORD_WEBHOOK_URL_ERRORS",
  "ENVIRONMENT",
  "EVALUATION_QUEUE_PRODUCER_KEY_ID",
  "EVALUATION_QUEUE_PRODUCER_SECRET",
  "GOOGLE_CREDENTIALS_JSON",
  "GOOGLE_DRIVE_PARENT_FOLDER_ID",
  "LOGGING_LEVEL",
  "SENTRY_DSN_APIS",
  "TICK_SECRET",
];

export default defineRailway((ctx) => {
  const name = ctx.projectName ?? "mini-app-polis ecosystem";
  if (ctx.environment !== "production") {
    return project(name, { resources: [] });
  }

  const api = service("api-deejaytools", {
    source: github("mini-app-polis/api-deejaytools", { branch: "main" }),
    // Root Dockerfile with digest-pinned bases and a non-root user (CD-021).
    build: { builder: "DOCKERFILE" as const, dockerfilePath: "Dockerfile" },
    replicas: { [REGION]: 1 },
    domains: [{ domain: "api.deejaytools.com", port: 3001 }],
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
      // OPS-002: the schema-owning role's connection, read by
      // scripts/apply_migrations.py. Like the one above, a database URL.
      DEEJAYTOOLS_DATABASE_URL_MIGRATIONS: preserve(),
      PORT: preserve(),
      // The fallback for every Discord channel without its own webhook.
      DISCORD_WEBHOOK_URL: preserve(),
      // Mutes the change feed without a deploy (docs/OPERATIONS.md).
      NOTIFY_DATA_CHANGES: preserve(),
      // The scheduler's pace, and its off switch.
      TICK_INTERVAL_MS: preserve(),
      DISABLE_SCHEDULER: preserve(),
    },
  });

  return project(name, { resources: [api] });
});
