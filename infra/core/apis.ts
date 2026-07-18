/**
 * Enabled GCP APIs.
 *
 * Several are enabled ahead of the pass that needs them — enabling is free and
 * propagation is slow enough to be annoying to discover mid-deploy.
 */

import * as gcp from "@pulumi/gcp";

export interface EnabledApis {
    /** All service resources, for `dependsOn`. */
    all: gcp.projects.Service[];
    byName: Record<string, gcp.projects.Service>;
}

const API_NAMES = [
    "compute",
    "pubsub",
    "sqladmin",
    "redis",
    "storage",
    "iam",
    "servicenetworking", // required later for Cloud SQL private IP
    "secretmanager", // holds the Tailscale auth key
];

export function enableGcpApis(project: string): EnabledApis {
    const byName: Record<string, gcp.projects.Service> = {};

    for (const name of API_NAMES) {
        byName[name] = new gcp.projects.Service(`api-${name}`, {
            project,
            service: `${name}.googleapis.com`,
            // Tearing down GreenlightIQ should not disable APIs that other
            // things in the project might be relying on.
            disableOnDestroy: false,
        });
    }

    return { all: Object.values(byName), byName };
}
