/**
 * GreenlightIQ infrastructure — main orchestrator.
 *
 * Composes the modules under `core/`. Every provisioned resource is declared
 * here so teardown is a single `pulumi destroy` — this matters because the
 * project runs on finite trial credits ($300, expiring 2026-09-30), and an
 * undeclared resource is one that keeps billing after teardown.
 *
 * Layout mirrors ../grove/infrastructure: thin orchestrator, one module per
 * concern, startup scripts as real files under `scripts/` rather than inline
 * heredocs.
 */

import * as gcp from "@pulumi/gcp";
import * as pulumi from "@pulumi/pulumi";

import { enableGcpApis } from "./core/apis";
import { createNetworking } from "./core/networking";
import { createServiceAccounts } from "./core/iam";
import { createMessaging } from "./core/messaging";
import { createStorage } from "./core/storage";
import { createSecrets } from "./core/secrets";
import { createCompute } from "./core/compute";

//=============================================================================
// Configuration
//=============================================================================

const config = new pulumi.Config();
const gcpConfig = new pulumi.Config("gcp");

const project = gcpConfig.require("project");
const region = gcpConfig.require("region");

const projectInfo = gcp.organizations.getProject({ projectId: project });
const projectNumber = pulumi.output(projectInfo).number;

//=============================================================================
// Core infrastructure
//=============================================================================

// 1. Enable required GCP APIs.
const apis = enableGcpApis(project);

// 2. VPC, subnet, NAT, firewall rules, Component A's static IP.
const network = createNetworking(project, region, apis);

// 3. One least-privilege service account per component.
const accounts = createServiceAccounts(project, apis);

// 4. Pub/Sub topics, pull subscriptions, dead-letter path.
const messaging = createMessaging(project, projectNumber, accounts, apis, {
    topicRequested: config.get("topicScoringRequested") ?? "scoring-requested",
    subRequested: config.get("subScoringRequested") ?? "scoring-requested-sub",
    topicCompleted: config.get("topicScoringCompleted") ?? "scoring-completed",
    subCompleted: config.get("subScoringCompleted") ?? "scoring-completed-sub",
});

// 5. Report artifact bucket.
const storage = createStorage(project, region, accounts, apis);

// 6. Tailscale auth key in Secret Manager, readable only by the component SAs.
const secrets = createSecrets(
    project,
    config.requireSecret("tailscaleAuthKey"),
    accounts,
    apis,
);

// 7. The three component VMs.
const compute = createCompute({
    project,
    region,
    zone: gcpConfig.get("zone") ?? `${region}-a`,
    machineType: config.get("machineType") ?? "e2-small",
    // Empty until a tagged Tailscale key exists. See core/secrets.ts for why
    // this matters more than where the key is stored.
    tailscaleTag: config.get("tailscaleTag") ?? "",
    // A generic account rather than a personal one, so it stays correct
    // regardless of who administers the box.
    //
    // ⚠️ Tailscale SSH maps a tailnet identity onto a local account named after
    // the *connecting* user, so bare `ssh gliq-intake` looks for a local `ft`
    // and fails. Either use `ssh admin@gliq-intake`, or add to ~/.ssh/config:
    //     Host gliq-*
    //         User admin
    adminUser: config.get("adminUser") ?? "admin",
    network,
    accounts,
    secrets,
    apis,
});

//=============================================================================
// Outputs — consumed by infra/env-from-stack.py
//=============================================================================

export const gcpProject = project;
export const gcpRegion = region;
export const networkName = network.network.name;
export const subnetName = network.subnet.name;
export const intakeStaticIp = network.intakeIp.address;
export const topicScoringRequested = messaging.topicRequested.name;
export const subScoringRequested = messaging.subRequested.name;
export const topicScoringCompleted = messaging.topicCompleted.name;
export const subScoringCompleted = messaging.subCompleted.name;
export const deadLetterTopic = messaging.topicDeadLetter.name;
export const artifactsBucket = storage.artifacts.name;
export const saIntakeEmail = accounts.intake.email;
export const saScoringEmail = accounts.scoring.email;
export const saReportEmail = accounts.report.email;
export const networkTagPublic = "gliq-public";
export const networkTagInternal = "gliq-internal";

// --- Compute -------------------------------------------------------------
const internalIp = (vm: gcp.compute.Instance) =>
    vm.networkInterfaces.apply((nics) => nics?.[0]?.networkIp ?? "not-available");

export const vmIntakeName = compute.intake.name;
export const vmScoringName = compute.scoring.name;
export const vmReportName = compute.report.name;
export const vmIntakeInternalIp = internalIp(compute.intake);
export const vmScoringInternalIp = internalIp(compute.scoring);
export const vmReportInternalIp = internalIp(compute.report);
/** The subnet B advertises to the tailnet — approve it in the admin console. */
export const advertisedSubnet = "10.10.0.0/24";
