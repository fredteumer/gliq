/**
 * Cloud SQL (PostgreSQL) — the comps corpus and the pitch pipeline record.
 *
 * Private IP only. A public-IP instance with authorised networks would be the
 * quicker path, but it puts a database on the internet in a project whose
 * entire network design is "only Component A is reachable" — so the instance
 * lives on the VPC and is reached from a laptop through the subnet route that
 * gliq-scoring advertises to the tailnet (`tailscale up --accept-routes`).
 *
 * Private IP is not a flag. It requires a reserved address range plus a
 * servicenetworking peering between the VPC and Google's service producer
 * network, both declared below — Cloud SQL then allocates inside that range.
 *
 * 💰 Cloud SQL bills hourly and Memorystore has no free tier, so this is
 * deliberately the smallest tier that works. Tear it down between sessions:
 * the corpus is reproducible from `data/` ETL, so nothing here is precious.
 */

import * as gcp from "@pulumi/gcp";
import * as pulumi from "@pulumi/pulumi";
import { EnabledApis } from "./apis";
import { SERVICES_PEERING_CIDR } from "./networking";

export interface DatabaseResources {
    instance: gcp.sql.DatabaseInstance;
    database: gcp.sql.Database;
    user: gcp.sql.User;
    /** Private IP on the VPC. This is what components connect to. */
    privateIp: pulumi.Output<string>;
}

export interface DatabaseArgs {
    project: string;
    region: string;
    network: gcp.compute.Network;
    /** Postgres password for the application user. A Pulumi config secret. */
    password: pulumi.Output<string>;
    /** db-f1-micro is the cheapest tier and is ample for ~100k corpus rows. */
    tier: string;
    databaseName: string;
    userName: string;
    apis: EnabledApis;
}

export function createDatabase(args: DatabaseArgs): DatabaseResources {
    const { project, region, network, apis } = args;
    const apiReady: pulumi.ResourceOptions = { dependsOn: apis.all };

    // The range Cloud SQL allocates its private IP from. It must not overlap
    // the component subnet (10.10.0.0/24) — this is a separate block handed to
    // Google's service producer network, not part of our subnet.
    const peeringRange = new gcp.compute.GlobalAddress(
        "gliq-sql-peering-range",
        {
            project,
            name: "gliq-sql-peering-range",
            purpose: "VPC_PEERING",
            addressType: "INTERNAL",
            address: SERVICES_PEERING_CIDR.split("/")[0],
            prefixLength: Number(SERVICES_PEERING_CIDR.split("/")[1]),
            network: network.id,
            description: "Reserved for Cloud SQL private services access.",
        },
        apiReady,
    );

    // The peering itself. Cloud SQL instances with a private IP are created
    // inside the producer network and reached across this connection.
    const peering = new gcp.servicenetworking.Connection(
        "gliq-sql-peering",
        {
            network: network.id,
            service: "servicenetworking.googleapis.com",
            reservedPeeringRanges: [peeringRange.name],
        },
        apiReady,
    );

    const instance = new gcp.sql.DatabaseInstance(
        "gliq-postgres",
        {
            project,
            region,
            name: "gliq-postgres",
            databaseVersion: "POSTGRES_15",
            // Without this, `pulumi destroy` refuses and the instance keeps
            // billing. Teardown between sessions is the whole cost strategy.
            deletionProtection: false,
            settings: {
                tier: args.tier,
                // Smallest disk that Cloud SQL accepts. The corpus is ~100k
                // rows; autoresize covers being wrong about that.
                diskSize: 10,
                diskType: "PD_HDD",
                diskAutoresize: true,
                availabilityType: "ZONAL",
                ipConfiguration: {
                    // No public IP at all. The only path in is the VPC.
                    ipv4Enabled: false,
                    privateNetwork: network.id,
                },
                backupConfiguration: {
                    // Off on purpose: the corpus is reproducible from the ETL
                    // script and pitch records are disposable during
                    // development. Backups on a torn-down-nightly instance
                    // would be pure cost.
                    enabled: false,
                },
                insightsConfig: {
                    queryInsightsEnabled: true,
                },
            },
        },
        // The instance cannot be created until the peering exists, and Pulumi
        // cannot infer that from the arguments alone — nothing about the
        // instance references the connection.
        //
        // ⚠️ Do NOT write `{ dependsOn: [peering], ...apiReady }`. apiReady is
        // itself `{ dependsOn: apis.all }`, so spreading it last overwrites the
        // key and silently drops the peering dependency — Cloud SQL then races
        // ahead and fails with "the network doesn't have at least 1 private
        // services connection". Merge the lists explicitly instead.
        { dependsOn: [peering, ...apis.all] },
    );

    const database = new gcp.sql.Database("gliq-db", {
        project,
        name: args.databaseName,
        instance: instance.name,
    });

    const user = new gcp.sql.User("gliq-db-user", {
        project,
        name: args.userName,
        instance: instance.name,
        password: args.password,
    });

    const privateIp = instance.privateIpAddress;

    return { instance, database, user, privateIp };
}
