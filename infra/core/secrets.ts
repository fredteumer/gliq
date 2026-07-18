/**
 * Secret Manager — the Tailscale auth key.
 *
 * Grove substitutes this key directly into `metadataStartupScript`, which
 * leaves it in **plaintext GCE instance metadata**: visible in the console and
 * readable by anyone holding `compute.instances.get`. Here the startup script
 * fetches it at boot using the VM's own service account instead, so the key
 * never lands in metadata.
 *
 * ⚠️ This limits *where the key is exposed*, not *what it is worth if exposed*.
 * An **untagged** auth key mints nodes that authenticate as the key's owner —
 * i.e. as Fred — and under Tailscale's default allow-all ACL such a node can
 * reach every device on the tailnet, including a laptop with Tailscale SSH
 * enabled. The fix for that is a **tagged** key (`tag:gliq`), set via
 * `pulumi config set tailscaleTag tag:gliq`. See core/compute.ts.
 */

import * as gcp from "@pulumi/gcp";
import * as pulumi from "@pulumi/pulumi";
import { EnabledApis } from "./apis";
import { ComponentAccounts, memberOf } from "./iam";

export interface SecretResources {
    tailscaleAuthKey: gcp.secretmanager.Secret;
    tailscaleAuthKeyName: pulumi.Output<string>;
}

export function createSecrets(
    project: string,
    authKey: pulumi.Output<string>,
    accounts: ComponentAccounts,
    apis: EnabledApis,
): SecretResources {
    const apiReady: pulumi.ResourceOptions = { dependsOn: apis.all };

    const tailscaleAuthKey = new gcp.secretmanager.Secret(
        "secret-tailscale-auth-key",
        {
            project,
            secretId: "tailscale-auth-key",
            replication: { auto: {} },
        },
        apiReady,
    );

    new gcp.secretmanager.SecretVersion("secret-tailscale-auth-key-version", {
        secret: tailscaleAuthKey.id,
        secretData: authKey,
    });

    // Every component VM reads it once at boot. Scoped to this one secret —
    // not a project-level accessor role.
    for (const [component, sa] of accounts.all) {
        new gcp.secretmanager.SecretIamMember(`iam-${component}-tailscale-key`, {
            project,
            secretId: tailscaleAuthKey.secretId,
            role: "roles/secretmanager.secretAccessor",
            member: memberOf(sa),
        });
    }

    return { tailscaleAuthKey, tailscaleAuthKeyName: tailscaleAuthKey.secretId };
}
