# Deploying the lab

Nothing here is applied by hand. ArgoCD runs `prune: true, selfHeal: true`, so a
merged manifest is a deployment and an out-of-band `kubectl edit` is reverted
within minutes.

## What goes where

| Path | Applied by | Purpose |
|---|---|---|
| `platform/sealed-secrets.yaml` | Copy into the platform repo's `k8s-infrastructure/argocd-apps/` | The controller that lets secrets live in git |
| `platform/ai-automation-lab.yaml` | Copy into the same directory | The one Application that points at `argocd/` here |
| `argocd/` | The Application above | Child Applications: the n8n chart, the lab's own resources |
| `manifests/` | `ai-lab-resources` | Namespace, runtime ConfigMap, database bootstrap job |

The platform repository gains exactly two files. Everything else stays
reviewable in the repository that owns it.

## Order of operations

Steps 1 and 2 change the cluster. Nothing else here does anything until they
are merged.

**1. Deploy the sealed-secrets controller.** Copy
`platform/sealed-secrets.yaml` into the platform repo and commit. Confirm the
controller is up before sealing anything:

```bash
kubectl -n kube-system get deploy sealed-secrets-controller
```

**2. Install kubeseal locally** (it is not currently on this machine):

```bash
brew install kubeseal
```

**3. Seal the secrets.** The n8n database password is sealed twice, once per
namespace, because Kubernetes secrets are namespaced and the bootstrap job runs
where the Postgres admin credential already lives. Both seals carry the same
plaintext password. Generate the values first; never reuse an example.

```bash
N8N_DB_PASSWORD="$(openssl rand -base64 24)"
N8N_ENCRYPTION_KEY="$(openssl rand -base64 32)"
```

The encryption key is the one value that cannot be rotated casually: n8n uses it
to encrypt stored credentials, so losing it means re-entering every credential,
and changing it after the fact orphans them. Keep a copy in the password
manager before it goes anywhere near the cluster.

```bash
# For n8n itself, in ai-lab.
kubectl create secret generic n8n-secret \
  --namespace ai-lab \
  --from-literal=db-password="$N8N_DB_PASSWORD" \
  --from-literal=encryption-key="$N8N_ENCRYPTION_KEY" \
  --dry-run=client -o yaml \
  | kubeseal --format yaml > manifests/n8n-secret.sealed.yaml

# For the bootstrap job, in apps, where postgres-secret already is.
kubectl create secret generic n8n-db-bootstrap \
  --namespace apps \
  --from-literal=db-password="$N8N_DB_PASSWORD" \
  --dry-run=client -o yaml \
  | kubeseal --format yaml > manifests/n8n-db-bootstrap.sealed.yaml
```

A SealedSecret is safe to commit: it can only be decrypted by the controller
holding the cluster's private key, and it is bound to the namespace and name it
was sealed for.

**4. Publish the hostname.** In the platform repo, add `"n8n"` to
`local.tunnel_hosts` in `terraform/cloudflare/main.tf`, then apply. Until
step 5, that hostname is anonymous, so do both in the same sitting.

**5. Put Cloudflare Access in front of it.** This is the first Zero Trust policy
in the account. Every other admin surface in this cluster is protected by not
having a DNS record, which is not an option for a host that has to receive a
webhook. Add a `cloudflare_zero_trust_access_application` for
`n8n.batpepe.online` with an email policy, alongside the existing tunnel config.

Webhook traffic never goes through this path: Alertmanager reaches n8n at
`http://n8n.ai-lab.svc:5678` inside the cluster, and so does the workflow sync.

**6. Deploy the lab.** Copy `platform/ai-automation-lab.yaml` into the platform
repo and commit. On first sync n8n may crashloop briefly: the database bootstrap
job is a PostSync hook, so it runs after the namespace and secrets exist, and
n8n retries until its database is there.

## Verifying

```bash
kubectl -n ai-lab get pods
kubectl -n apps logs job/n8n-db-bootstrap
argocd app get n8n
```

Then create an API key in the n8n UI under Settings, seal it as
`opsagent-secret`, and prove the round trip:

```bash
OPSAGENT_N8N_URL=http://localhost:5678 OPSAGENT_N8N_API_KEY=... uv run opsagent n8n export
```

with `kubectl -n ai-lab port-forward svc/n8n 5678:80` open in another terminal.
`opsagent n8n diff` must then report `in sync`.
