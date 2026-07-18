/**
 * The three component VMs — one OS process per VM, per the assignment's
 * "at least 3 distinct processes deployed as real OS processes" requirement.
 *
 * | Node          | External IP        | Tags                        | Notes                    |
 * |---------------|--------------------|-----------------------------|--------------------------|
 * | gliq-intake   | static (A only)    | gliq-public, gliq-internal  | nginx + certbot          |
 * | gliq-scoring  | none               | gliq-internal               | advertises subnet routes |
 * | gliq-report   | none               | gliq-internal               |                          |
 *
 * B and C reach the internet through Cloud NAT and are reachable only over the
 * tailnet. There is no bastion and no public port 22 anywhere.
 *
 * ## Subnet routing
 *
 * B advertises `10.10.0.0/24` to the tailnet. This is NOT needed to SSH to the
 * VMs — every node runs `tailscaled` and is directly addressable. It exists for
 * the *managed* services: Cloud SQL and Memorystore cannot run a Tailscale
 * agent, and will only ever have private IPs inside this subnet. Without an
 * advertised route, `psql` and `redis-cli` from a laptop cannot reach them.
 *
 * B is the advertiser because B is the component that talks to both, so a B
 * outage already means a broken pipeline. Grove uses a dedicated e2-micro for
 * this; here it is a free property of a VM that has other work to do.
 *
 * ⚠️ Routes must be approved once, manually:
 *    https://login.tailscale.com/admin/machines
 */

import * as fs from "fs";
import * as path from "path";
import * as gcp from "@pulumi/gcp";
import * as pulumi from "@pulumi/pulumi";
import { EnabledApis } from "./apis";
import { ComponentAccounts } from "./iam";
import { NetworkingResources, SUBNET_CIDR, TAG_INTERNAL, TAG_PUBLIC } from "./networking";
import { SecretResources } from "./secrets";

export interface ComputeResources {
    intake: gcp.compute.Instance;
    scoring: gcp.compute.Instance;
    report: gcp.compute.Instance;
    all: Array<[string, gcp.compute.Instance]>;
}

/** Debian 12 ships Python 3.11 — matches the project's requires-python floor. */
const BOOT_IMAGE = "debian-cloud/debian-12";

export interface ComputeArgs {
    project: string;
    region: string;
    zone: string;
    machineType: string;
    /** Tailscale ACL tag, e.g. `tag:gliq`. Empty = untagged (see secrets.ts). */
    tailscaleTag: string;
    /**
     * Local account Tailscale SSH maps the admin identity onto.
     *
     * Tailscale SSH uses the *connecting* user's name unless one is given
     * explicitly, so a generic `admin` requires either `ssh admin@<host>` or a
     * `Host gliq-* / User admin` block in the operator's ~/.ssh/config. Set it
     * to your own username instead if you prefer bare `ssh <host>`.
     */
    adminUser: string;
    network: NetworkingResources;
    accounts: ComponentAccounts;
    secrets: SecretResources;
    apis: EnabledApis;
}

export function createCompute(args: ComputeArgs): ComputeResources {
    const { project, zone, machineType, tailscaleTag, adminUser, network, accounts, secrets, apis } =
        args;

    const template = fs.readFileSync(
        path.join(__dirname, "..", "scripts", "node-startup.sh"),
        "utf8",
    );

    /**
     * Render the startup script for one node.
     *
     * `tailscale up` REJECTS a key that is not authorised for the tag it is
     * asked to advertise, so the tag flag is omitted entirely when unset rather
     * than passed empty — an unset tag must not break the boot.
     */
    const startupScript = (hostname: string, extraFlags: string[]): pulumi.Output<string> =>
        pulumi.all([secrets.tailscaleAuthKeyName]).apply(([secretName]) => {
            const flags = [...extraFlags];
            if (tailscaleTag) {
                flags.push(`--advertise-tags=${tailscaleTag}`);
            }
            return template
                .replace(/__PROJECT__/g, project)
                .replace(/__SECRET_NAME__/g, secretName)
                .replace(/__HOSTNAME__/g, hostname)
                .replace(/__ADMIN_USER__/g, adminUser)
                .replace(/__EXTRA_TS_FLAGS__/g, flags.join(" \\\n    "));
        });

    const bootDisk = {
        initializeParams: {
            image: BOOT_IMAGE,
            size: 20,
            type: "pd-balanced",
        },
    };

    const dependsOn: pulumi.Resource[] = [
        ...apis.all,
        network.subnet,
        network.firewalls.tailscale,
        secrets.tailscaleAuthKey,
    ];

    /**
     * Shared instance options.
     *
     * The bootstrap script goes in `metadata["startup-script"]` rather than the
     * `metadataStartupScript` convenience field: the latter is ForceNew, so
     * every edit to the script would destroy and recreate all three VMs. The
     * metadata map is updatable in place — edit the script, `pulumi up`, then
     * reset the instance to re-run it. (GCE runs startup scripts on *every*
     * boot, not just the first.)
     *
     * `resourceManagerTags` is ignored because the provider reports it as a
     * null addition we never set, and any bootDisk diff triggers replacement.
     */
    const instanceOpts: pulumi.CustomResourceOptions = {
        dependsOn,
        ignoreChanges: ["bootDisk.initializeParams.resourceManagerTags"],
    };

    // --- Component A — the only publicly reachable node ---------------------
    const intake = new gcp.compute.Instance(
        "vm-intake",
        {
            project,
            zone,
            name: "gliq-intake",
            machineType,
            bootDisk,
            networkInterfaces: [
                {
                    network: network.network.id,
                    subnetwork: network.subnet.id,
                    // Claims the reserved address, which also makes it free.
                    accessConfigs: [{ natIp: network.intakeIp.address }],
                },
            ],
            tags: [TAG_PUBLIC, TAG_INTERNAL],
            metadata: { "startup-script": startupScript("gliq-intake", []) },
            serviceAccount: {
                email: accounts.intake.email,
                scopes: ["https://www.googleapis.com/auth/cloud-platform"],
            },
            labels: { component: "intake", project: "greenlightiq" },
            allowStoppingForUpdate: true,
        },
        instanceOpts,
    );

    // --- Component B — no external IP; advertises the subnet ----------------
    const scoring = new gcp.compute.Instance(
        "vm-scoring",
        {
            project,
            zone,
            name: "gliq-scoring",
            machineType,
            bootDisk,
            networkInterfaces: [
                {
                    network: network.network.id,
                    subnetwork: network.subnet.id,
                    // No accessConfigs => no external IP. Egress via Cloud NAT.
                },
            ],
            tags: [TAG_INTERNAL],
            // Required for subnet routing; without it advertised routes silently
            // fail to carry traffic.
            canIpForward: true,
            metadata: {
                "startup-script": startupScript("gliq-scoring", [
                    `--advertise-routes=${SUBNET_CIDR}`,
                ]),
            },
            serviceAccount: {
                email: accounts.scoring.email,
                scopes: ["https://www.googleapis.com/auth/cloud-platform"],
            },
            labels: { component: "scoring", project: "greenlightiq" },
            allowStoppingForUpdate: true,
        },
        instanceOpts,
    );

    // --- Component C — no external IP ---------------------------------------
    const report = new gcp.compute.Instance(
        "vm-report",
        {
            project,
            zone,
            name: "gliq-report",
            machineType,
            bootDisk,
            networkInterfaces: [
                {
                    network: network.network.id,
                    subnetwork: network.subnet.id,
                },
            ],
            tags: [TAG_INTERNAL],
            metadata: { "startup-script": startupScript("gliq-report", []) },
            serviceAccount: {
                email: accounts.report.email,
                scopes: ["https://www.googleapis.com/auth/cloud-platform"],
            },
            labels: { component: "report", project: "greenlightiq" },
            allowStoppingForUpdate: true,
        },
        instanceOpts,
    );

    return {
        intake,
        scoring,
        report,
        all: [
            ["intake", intake],
            ["scoring", scoring],
            ["report", report],
        ],
    };
}
