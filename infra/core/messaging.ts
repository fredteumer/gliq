/**
 * Pub/Sub — the messaging fabric.
 *
 * A publishes requests. B reads them and publishes results. C reads results.
 * Deliberately no reverse grants: C cannot publish, A cannot subscribe.
 *
 * Subscriptions are **pull**, not push: a push subscription would require the
 * consumer to expose a public HTTPS endpoint, which is exactly what B and C
 * must not have.
 */

import * as gcp from "@pulumi/gcp";
import * as pulumi from "@pulumi/pulumi";
import { EnabledApis } from "./apis";
import { ComponentAccounts, memberOf } from "./iam";

export interface MessagingResources {
    topicRequested: gcp.pubsub.Topic;
    topicCompleted: gcp.pubsub.Topic;
    topicDeadLetter: gcp.pubsub.Topic;
    subRequested: gcp.pubsub.Subscription;
    subCompleted: gcp.pubsub.Subscription;
    subDeadLetter: gcp.pubsub.Subscription;
}

/** Deliveries before a poison message is diverted to the dead-letter topic. */
const MAX_DELIVERY_ATTEMPTS = 5;

export function createMessaging(
    project: string,
    projectNumber: pulumi.Output<string>,
    accounts: ComponentAccounts,
    apis: EnabledApis,
    names: {
        topicRequested: string;
        subRequested: string;
        topicCompleted: string;
        subCompleted: string;
    },
): MessagingResources {
    const apiReady: pulumi.ResourceOptions = { dependsOn: apis.all };

    /**
     * Google's own Pub/Sub service agent — not one of our service accounts.
     * This is the principal that actually moves a poison message onto the
     * dead-letter topic; dead-lettering silently no-ops if it lacks permission.
     */
    const pubsubAgent = pulumi.interpolate`serviceAccount:service-${projectNumber}@gcp-sa-pubsub.iam.gserviceaccount.com`;

    const topicRequested = new gcp.pubsub.Topic(
        "topic-scoring-requested",
        { project, name: names.topicRequested },
        apiReady,
    );

    const topicCompleted = new gcp.pubsub.Topic(
        "topic-scoring-completed",
        { project, name: names.topicCompleted },
        apiReady,
    );

    // A malformed message that crashes B on every redelivery would otherwise
    // block the subscription indefinitely. After 5 attempts it lands here
    // instead, where it can be inspected without holding up the pipeline.
    const topicDeadLetter = new gcp.pubsub.Topic(
        "topic-dead-letter",
        { project, name: "gliq-dead-letter" },
        apiReady,
    );

    const subscription = (resource: string, name: string, topic: gcp.pubsub.Topic) =>
        new gcp.pubsub.Subscription(resource, {
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

    const subRequested = subscription(
        "sub-scoring-requested",
        names.subRequested,
        topicRequested,
    );
    const subCompleted = subscription(
        "sub-scoring-completed",
        names.subCompleted,
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

    // --- Dead-lettering permissions for Google's Pub/Sub service agent ------
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

    // --- Component permissions ---------------------------------------------

    new gcp.pubsub.TopicIAMMember("iam-intake-publish-requested", {
        project,
        topic: topicRequested.name,
        role: "roles/pubsub.publisher",
        member: memberOf(accounts.intake),
    });

    new gcp.pubsub.SubscriptionIAMMember("iam-scoring-sub-requested", {
        project,
        subscription: subRequested.name,
        role: "roles/pubsub.subscriber",
        member: memberOf(accounts.scoring),
    });

    new gcp.pubsub.TopicIAMMember("iam-scoring-publish-completed", {
        project,
        topic: topicCompleted.name,
        role: "roles/pubsub.publisher",
        member: memberOf(accounts.scoring),
    });

    new gcp.pubsub.SubscriptionIAMMember("iam-report-sub-completed", {
        project,
        subscription: subCompleted.name,
        role: "roles/pubsub.subscriber",
        member: memberOf(accounts.report),
    });

    return {
        topicRequested,
        topicCompleted,
        topicDeadLetter,
        subRequested,
        subCompleted,
        subDeadLetter,
    };
}
