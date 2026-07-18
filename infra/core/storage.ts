/**
 * Cloud Storage — rendered report artifacts.
 *
 * C writes reports; A serves them back to the producer who submitted the pitch.
 */

import * as gcp from "@pulumi/gcp";
import * as pulumi from "@pulumi/pulumi";
import { EnabledApis } from "./apis";
import { ComponentAccounts, memberOf } from "./iam";

export interface StorageResources {
    artifacts: gcp.storage.Bucket;
}

export function createStorage(
    project: string,
    region: string,
    accounts: ComponentAccounts,
    apis: EnabledApis,
): StorageResources {
    const apiReady: pulumi.ResourceOptions = { dependsOn: apis.all };

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

    new gcp.storage.BucketIAMMember("iam-report-artifacts-write", {
        bucket: artifacts.name,
        role: "roles/storage.objectAdmin",
        member: memberOf(accounts.report),
    });

    new gcp.storage.BucketIAMMember("iam-intake-artifacts-read", {
        bucket: artifacts.name,
        role: "roles/storage.objectViewer",
        member: memberOf(accounts.intake),
    });

    return { artifacts };
}
