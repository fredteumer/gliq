/**
 * One service account per component, least-privilege.
 *
 * The only project-level grants any component gets are logging and metrics.
 * Everything else is scoped to an individual topic, subscription, bucket or
 * secret — a project-level `pubsub.editor` would be far less typing and would
 * also let any one compromised component drain every queue.
 */

import * as gcp from "@pulumi/gcp";
import * as pulumi from "@pulumi/pulumi";
import { EnabledApis } from "./apis";

export interface ComponentAccounts {
    intake: gcp.serviceaccount.Account;
    scoring: gcp.serviceaccount.Account;
    report: gcp.serviceaccount.Account;
    /** Iteration order for grants that apply to all three. */
    all: Array<[string, gcp.serviceaccount.Account]>;
}

/** `serviceAccount:` IAM member string for an account. */
export const memberOf = (sa: gcp.serviceaccount.Account): pulumi.Output<string> =>
    pulumi.interpolate`serviceAccount:${sa.email}`;

export function createServiceAccounts(project: string, apis: EnabledApis): ComponentAccounts {
    const apiReady: pulumi.ResourceOptions = { dependsOn: apis.all };

    const intake = new gcp.serviceaccount.Account(
        "sa-intake",
        { project, accountId: "gliq-intake", displayName: "GreenlightIQ Component A (intake)" },
        apiReady,
    );

    const scoring = new gcp.serviceaccount.Account(
        "sa-scoring",
        { project, accountId: "gliq-scoring", displayName: "GreenlightIQ Component B (scoring)" },
        apiReady,
    );

    const report = new gcp.serviceaccount.Account(
        "sa-report",
        { project, accountId: "gliq-report", displayName: "GreenlightIQ Component C (reporting)" },
        apiReady,
    );

    const all: Array<[string, gcp.serviceaccount.Account]> = [
        ["intake", intake],
        ["scoring", scoring],
        ["report", report],
    ];

    for (const [component, sa] of all) {
        for (const role of ["roles/logging.logWriter", "roles/monitoring.metricWriter"]) {
            const slug = role.split("/")[1].toLowerCase();
            new gcp.projects.IAMMember(`iam-${component}-${slug}`, {
                project,
                role,
                member: memberOf(sa),
            });
        }
    }

    return { intake, scoring, report, all };
}
