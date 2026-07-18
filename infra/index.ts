/**
 * GreenlightIQ infrastructure — foundation pass.
 *
 * Declares everything the three components need *except* billable compute:
 * enabled APIs, a custom VPC, least-privilege service accounts, the Pub/Sub
 * messaging fabric, and the artifact bucket. Cloud SQL, Memorystore and the
 * VMs land in a later pass so credits aren't burned while components are
 * stubs.
 *
 * Design notes worth knowing before editing:
 *
 * - **Custom-mode VPC, not the default.** The auto-created default VPC ships
 *   permissive firewall rules including SSH from anywhere. Components B and C
 *   are supposed to have no inbound surface at all, so we build the network up
 *   from deny-by-default rather than subtracting from a permissive baseline.
 * - **Private Google Access is on.** B and C have no external IP, so without
 *   it they could not reach the Pub/Sub API at all.
 * - **Cloud NAT exists for egress only.** B and C still need outbound access
 *   for apt/pip during provisioning. NAT bills per VM-hour, so it costs
 *   ~nothing until the VM pass lands.
 * - **IAM is per-resource, not per-project.** A can publish to one topic and
 *   nothing else; B and C can each read exactly one subscription. A
 *   project-level pubsub.editor would be far less typing and would also let
 *   any compromised component drain every queue.
 */

import * as pulumi from "@pulumi/pulumi";
import * as gcp from "@pulumi/gcp";

const config = new pulumi.Config();
const gcpConfig = new pulumi.Config("gcp");

const project = gcpConfig.require("project");
const region = gcpConfig.require("region");

const projectInfo = gcp.organizations.getProject({ projectId: project });
const projectNumber = pulumi.output(projectInfo).number;

/**
 * Google's own Pub/Sub service agent — not one of our service accounts. This
 * is the principal that actually moves a poison message onto the dead-letter
 * topic; dead-lettering silently no-ops if it lacks permission.
 */
const pubsubAgent = pulumi.interpolate`serviceAccount:service-${projectNumber}@gcp-sa-pubsub.iam.gserviceaccount.com`;

// --------------------------------------------------------------------------
// APIs
// --------------------------------------------------------------------------

// sqladmin and redis are enabled now even though nothing uses them yet —
// enabling is free and propagation is slow enough to be annoying mid-deploy.
const apiNames = [
    "compute",
    "pubsub",
    "sqladmin",
    "redis",
    "storage",
    "iam",
    "servicenetworking", // required later for Cloud SQL private IP
];

const services = apiNames.map(
    (name) =>
        new gcp.projects.Service(`api-${name}`, {
            project,
            service: `${name}.googleapis.com`,
            // Tearing down GreenlightIQ should not disable APIs that other
            // things in the project might be relying on.
            disableOnDestroy: false,
        }),
);

const apiReady: pulumi.ResourceOptions = { dependsOn: services };

// --------------------------------------------------------------------------
// Network
// --------------------------------------------------------------------------

const network = new gcp.compute.Network(
    "gliq-vpc",
    {
        project,
        name: "gliq-vpc",
        autoCreateSubnetworks: false,
        description: "GreenlightIQ VPC. Deny-by-default; only Component A is reachable.",
    },
    apiReady,
);

const subnet = new gcp.compute.Subnetwork("gliq-subnet", {
    project,
    region,
    name: "gliq-subnet",
    network: network.id,
    ipCidrRange: "10.10.0.0/24",
    // Without this, the no-external-IP VMs cannot reach googleapis.com.
    privateIpGoogleAccess: true,
});

const router = new gcp.compute.Router("gliq-router", {
    project,
    region,
    name: "gliq-router",
    network: network.id,
});

// Outbound-only. Gives B and C package access during provisioning without
// giving them an inbound address.
const nat = new gcp.compute.RouterNat("gliq-nat", {
    project,
    region,
    name: "gliq-nat",
    router: router.name,
    natIpAllocateOption: "AUTO_ONLY",
    sourceSubnetworkIpRangesToNat: "ALL_SUBNETWORKS_ALL_IP_RANGES",
});

// Network tags. A VM gets firewall rules by wearing the matching tag, so the
// VM pass only needs to attach the right string.
const TAG_PUBLIC = "gliq-public"; // Component A only
const TAG_INTERNAL = "gliq-internal"; // all three

// Note there is deliberately no rule opening port 22 to the internet. Admin
// access is over Tailscale, which needs no inbound rule of its own — it either
// negotiates a direct path via the UDP rule below, or falls back to Google's
// DERP relays over outbound 443.
const firewallHttps = new gcp.compute.Firewall("gliq-allow-https", {
    project,
    name: "gliq-allow-https",
    network: network.id,
    description: "Public HTTPS to Component A only.",
    direction: "INGRESS",
    sourceRanges: ["0.0.0.0/0"],
    targetTags: [TAG_PUBLIC],
    allows: [{ protocol: "tcp", ports: ["80", "443"] }],
});

const firewallTailscale = new gcp.compute.Firewall("gliq-allow-tailscale", {
    project,
    name: "gliq-allow-tailscale",
    network: network.id,
    description: "Tailscale direct connections. Falls back to DERP relay if blocked.",
    direction: "INGRESS",
    sourceRanges: ["0.0.0.0/0"],
    targetTags: [TAG_INTERNAL],
    allows: [{ protocol: "udp", ports: ["41641"] }],
});

const firewallInternal = new gcp.compute.Firewall("gliq-allow-internal", {
    project,
    name: "gliq-allow-internal",
    network: network.id,
    description: "Intra-subnet traffic between the three components.",
    direction: "INGRESS",
    sourceRanges: ["10.10.0.0/24"],
    targetTags: [TAG_INTERNAL],
    allows: [{ protocol: "tcp" }, { protocol: "udp" }, { protocol: "icmp" }],
});

// Reserved ahead of the VM pass so greenlightiq.fredt.io DNS can be pointed
// and allowed to propagate before Component A exists. A reserved-but-unattached
// address bills a small hourly rate; attached to a running VM it is free.
const intakeIp = new gcp.compute.Address(
    "gliq-intake-ip",
    {
        project,
        region,
        name: "gliq-intake-ip",
        addressType: "EXTERNAL",
        description: "Static IP for Component A (greenlightiq.fredt.io).",
    },
    apiReady,
);

// --------------------------------------------------------------------------
// Service accounts — one per component
// --------------------------------------------------------------------------

const saIntake = new gcp.serviceaccount.Account(
    "sa-intake",
    {
        project,
        accountId: "gliq-intake",
        displayName: "GreenlightIQ Component A (intake)",
    },
    apiReady,
);

const saScoring = new gcp.serviceaccount.Account(
    "sa-scoring",
    {
        project,
        accountId: "gliq-scoring",
        displayName: "GreenlightIQ Component B (scoring)",
    },
    apiReady,
);

const saReport = new gcp.serviceaccount.Account(
    "sa-report",
    {
        project,
        accountId: "gliq-report",
        displayName: "GreenlightIQ Component C (reporting)",
    },
    apiReady,
);

const memberOf = (sa: gcp.serviceaccount.Account) =>
    pulumi.interpolate`serviceAccount:${sa.email}`;

// The only project-level grants any component gets. Everything else is scoped
// to an individual topic, subscription or bucket.
const componentAccounts: Array<[string, gcp.serviceaccount.Account]> = [
    ["intake", saIntake],
    ["scoring", saScoring],
    ["report", saReport],
];

for (const [component, sa] of componentAccounts) {
    for (const role of ["roles/logging.logWriter", "roles/monitoring.metricWriter"]) {
        const slug = role.split("/")[1].toLowerCase();
        new gcp.projects.IAMMember(`iam-${component}-${slug}`, {
            project,
            role,
            member: memberOf(sa),
        });
    }
}

// --------------------------------------------------------------------------
// Pub/Sub — the messaging fabric
// --------------------------------------------------------------------------

const topicRequested = new gcp.pubsub.Topic(
    "topic-scoring-requested",
    { project, name: config.get("topicScoringRequested") ?? "scoring-requested" },
    apiReady,
);

const topicCompleted = new gcp.pubsub.Topic(
    "topic-scoring-completed",
    { project, name: config.get("topicScoringCompleted") ?? "scoring-completed" },
    apiReady,
);

// A malformed message that crashes B on every redelivery would otherwise block
// the subscription indefinitely. After 5 attempts it lands here instead, where
// it can be inspected without holding up the pipeline.
const topicDeadLetter = new gcp.pubsub.Topic(
    "topic-dead-letter",
    { project, name: "gliq-dead-letter" },
    apiReady,
);

const MAX_DELIVERY_ATTEMPTS = 5;

/**
 * A pull subscription with dead-lettering and a 60s ack deadline.
 *
 * Pull, not push: a push subscription would require the consumer to expose a
 * public HTTPS endpoint, which is exactly what B and C must not have.
 */
function subscription(
    resource: string,
    name: string,
    topic: gcp.pubsub.Topic,
): gcp.pubsub.Subscription {
    return new gcp.pubsub.Subscription(resource, {
        project,
        name,
        topic: topic.name,
        ackDeadlineSeconds: 60,
        // Long enough to survive a consumer restart without losing work.
        messageRetentionDuration: "604800s", // 7 days
        retainAckedMessages: false,
        expirationPolicy: { ttl: "" }, // never expire
        deadLetterPolicy: {
            deadLetterTopic: topicDeadLetter.id,
            maxDeliveryAttempts: MAX_DELIVERY_ATTEMPTS,
        },
        retryPolicy: { minimumBackoff: "10s", maximumBackoff: "600s" },
    });
}

const subRequested = subscription(
    "sub-scoring-requested",
    config.get("subScoringRequested") ?? "scoring-requested-sub",
    topicRequested,
);

const subCompleted = subscription(
    "sub-scoring-completed",
    config.get("subScoringCompleted") ?? "scoring-completed-sub",
    topicCompleted,
);

// Lets a human (or a future replay tool) drain the dead-letter topic.
const subDeadLetter = new gcp.pubsub.Subscription("sub-dead-letter", {
    project,
    name: "gliq-dead-letter-sub",
    topic: topicDeadLetter.name,
    ackDeadlineSeconds: 60,
    messageRetentionDuration: "604800s",
    expirationPolicy: { ttl: "" },
});

// --- Dead-lettering permissions for Google's Pub/Sub service agent ---------
// Without both of these, dead-lettering fails silently and poison messages
// redeliver forever.

new gcp.pubsub.TopicIAMMember("iam-dlq-agent-publisher", {
    project,
    topic: topicDeadLetter.name,
    role: "roles/pubsub.publisher",
    member: pubsubAgent,
});

for (const [label, sub] of [
    ["requested", subRequested],
    ["completed", subCompleted],
] as Array<[string, gcp.pubsub.Subscription]>) {
    new gcp.pubsub.SubscriptionIAMMember(`iam-dlq-agent-subscriber-${label}`, {
        project,
        subscription: sub.name,
        role: "roles/pubsub.subscriber",
        member: pubsubAgent,
    });
}

// --- Component permissions ------------------------------------------------
// A publishes requests. B reads them and publishes results. C reads results.
// Deliberately no reverse grants: C cannot publish, A cannot subscribe.

new gcp.pubsub.TopicIAMMember("iam-intake-publish-requested", {
    project,
    topic: topicRequested.name,
    role: "roles/pubsub.publisher",
    member: memberOf(saIntake),
});

new gcp.pubsub.SubscriptionIAMMember("iam-scoring-sub-requested", {
    project,
    subscription: subRequested.name,
    role: "roles/pubsub.subscriber",
    member: memberOf(saScoring),
});

new gcp.pubsub.TopicIAMMember("iam-scoring-publish-completed", {
    project,
    topic: topicCompleted.name,
    role: "roles/pubsub.publisher",
    member: memberOf(saScoring),
});

new gcp.pubsub.SubscriptionIAMMember("iam-report-sub-completed", {
    project,
    subscription: subCompleted.name,
    role: "roles/pubsub.subscriber",
    member: memberOf(saReport),
});

// --------------------------------------------------------------------------
// Storage — rendered report artifacts
// --------------------------------------------------------------------------

const artifacts = new gcp.storage.Bucket(
    "gliq-artifacts",
    {
        project,
        name: `${project}-gliq-artifacts`,
        location: region,
        uniformBucketLevelAccess: true,
        publicAccessPrevention: "enforced",
        forceDestroy: true, // a graded project should tear down cleanly
        versioning: { enabled: true },
        lifecycleRules: [{ action: { type: "Delete" }, condition: { age: 90 } }],
    },
    apiReady,
);

// C writes reports; A serves them back to the producer who submitted the pitch.
new gcp.storage.BucketIAMMember("iam-report-artifacts-write", {
    bucket: artifacts.name,
    role: "roles/storage.objectAdmin",
    member: memberOf(saReport),
});

new gcp.storage.BucketIAMMember("iam-intake-artifacts-read", {
    bucket: artifacts.name,
    role: "roles/storage.objectViewer",
    member: memberOf(saIntake),
});

// --------------------------------------------------------------------------
// Outputs — these populate .env / the systemd EnvironmentFile
// --------------------------------------------------------------------------

export const gcpProject = project;
export const gcpRegion = region;
export const networkName = network.name;
export const subnetName = subnet.name;
export const intakeStaticIp = intakeIp.address;
export const topicScoringRequested = topicRequested.name;
export const subScoringRequested = subRequested.name;
export const topicScoringCompleted = topicCompleted.name;
export const subScoringCompleted = subCompleted.name;
export const deadLetterTopic = topicDeadLetter.name;
export const artifactsBucket = artifacts.name;
export const saIntakeEmail = saIntake.email;
export const saScoringEmail = saScoring.email;
export const saReportEmail = saReport.email;
export const networkTagPublic = TAG_PUBLIC;
export const networkTagInternal = TAG_INTERNAL;
