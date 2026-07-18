/**
 * VPC, subnet, egress NAT, firewall rules, and Component A's static IP.
 *
 * Custom-mode VPC, not the default: the auto-created default network ships
 * permissive rules including SSH from anywhere. B and C are supposed to have no
 * inbound surface at all, so we build up from deny-by-default rather than
 * subtracting from a permissive baseline. (The default VPC was deleted from
 * this project entirely — see docs/ARCHITECTURE.md.)
 */

import * as gcp from "@pulumi/gcp";
import * as pulumi from "@pulumi/pulumi";
import { EnabledApis } from "./apis";

/** Component A only — the sole publicly reachable host. */
export const TAG_PUBLIC = "gliq-public";
/** All three components. */
export const TAG_INTERNAL = "gliq-internal";

/** The subnet every component lives in. Advertised to the tailnet by B. */
export const SUBNET_CIDR = "10.10.0.0/24";

export interface NetworkingResources {
    network: gcp.compute.Network;
    subnet: gcp.compute.Subnetwork;
    router: gcp.compute.Router;
    nat: gcp.compute.RouterNat;
    intakeIp: gcp.compute.Address;
    firewalls: {
        https: gcp.compute.Firewall;
        tailscale: gcp.compute.Firewall;
        internal: gcp.compute.Firewall;
    };
}

export function createNetworking(
    project: string,
    region: string,
    apis: EnabledApis,
): NetworkingResources {
    const apiReady: pulumi.ResourceOptions = { dependsOn: apis.all };

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
        ipCidrRange: SUBNET_CIDR,
        // Without this, the no-external-IP VMs cannot reach googleapis.com.
        privateIpGoogleAccess: true,
    });

    const router = new gcp.compute.Router("gliq-router", {
        project,
        region,
        name: "gliq-router",
        network: network.id,
    });

    // Outbound-only. B and C have no external IP but still need package access
    // during provisioning, and egress to the Tailscale control plane.
    const nat = new gcp.compute.RouterNat("gliq-nat", {
        project,
        region,
        name: "gliq-nat",
        router: router.name,
        natIpAllocateOption: "AUTO_ONLY",
        sourceSubnetworkIpRangesToNat: "ALL_SUBNETWORKS_ALL_IP_RANGES",
    });

    // Note there is deliberately no rule opening port 22 to the internet. Admin
    // access is over Tailscale, which needs no inbound rule of its own — it
    // either negotiates a direct path via the UDP rule below, or falls back to
    // Google's DERP relays over outbound 443.
    const https = new gcp.compute.Firewall("gliq-allow-https", {
        project,
        name: "gliq-allow-https",
        network: network.id,
        description: "Public HTTPS to Component A only.",
        direction: "INGRESS",
        sourceRanges: ["0.0.0.0/0"],
        targetTags: [TAG_PUBLIC],
        allows: [{ protocol: "tcp", ports: ["80", "443"] }],
    });

    const tailscale = new gcp.compute.Firewall("gliq-allow-tailscale", {
        project,
        name: "gliq-allow-tailscale",
        network: network.id,
        description: "Tailscale direct connections. Falls back to DERP relay if blocked.",
        direction: "INGRESS",
        sourceRanges: ["0.0.0.0/0"],
        targetTags: [TAG_INTERNAL],
        allows: [{ protocol: "udp", ports: ["41641"] }],
    });

    const internal = new gcp.compute.Firewall("gliq-allow-internal", {
        project,
        name: "gliq-allow-internal",
        network: network.id,
        description: "Intra-subnet traffic between the three components.",
        direction: "INGRESS",
        sourceRanges: [SUBNET_CIDR],
        targetTags: [TAG_INTERNAL],
        allows: [{ protocol: "tcp" }, { protocol: "udp" }, { protocol: "icmp" }],
    });

    // Reserved before Component A existed so greenlightiq.fredt.io could be
    // pointed and allowed to propagate early. Free while attached to a running
    // VM; small hourly charge while unattached.
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

    return { network, subnet, router, nat, intakeIp, firewalls: { https, tailscale, internal } };
}
