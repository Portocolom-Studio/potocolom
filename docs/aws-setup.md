# AWS setup guide

The operator's runbook for standing up the cloud profile: which services to create, in what order, with which parameters, roles and secrets. [cloud-infrastructure.md](cloud-infrastructure.md) explains what the pieces are and why they were chosen; this document is the how. Values below are reference defaults for the launch scale; verify instance prices and availability before purchasing.

One fact shapes everything here: **the self-hosted version is the base**. The cloud deploys the exact same container images and the same SPA build that self-hosters run, so this guide contains zero application build steps. Every difference is an environment variable or a managed service standing in behind an existing seam.

## What the cloud reuses from the self-hosted base

| Layer | Self-hosted | Cloud | Shared code |
|---|---|---|---|
| API surface and calls | all endpoints in [api.md](api.md) | identical, byte for byte | 100 percent |
| Frontend | one static build, served by the API | same artifact, served by CloudFront | 100 percent |
| Container images | GHCR `:vX.Y.Z` | same digests, mirrored to ECR | 100 percent |
| Worker and protocol | dials the compose hostname | dials the public ALB hostname | 100 percent |
| Dispatch and relay | in-process implementations | Redis implementations, same interfaces | interfaces shared |
| Quota | UnlimitedQuota | BillingQuota over QUOTA_SERVICE_URL | interface shared |
| Storage | local filesystem | S3 + signed CloudFront URLs | interface shared |
| Auth mode | `none` or `local` | `oauth` with providers | one module, config selects |

What the cloud adds is outside the application: this infrastructure, and the two private-repo services (billing, fleet autoscaler) reached over HTTP.

## 0. Prerequisites

- An AWS Organization with two member accounts, staging and production (the management account holds only consolidated billing), with administrator access for the bootstrap; day-to-day changes then go through Terraform and the CI roles. See [decisions.md](decisions.md), "AWS accounts".
- The domain (potocolom.com) in a Route 53 hosted zone.
- Terraform 1.7 or newer, AWS CLI v2, and repository admin on GitHub (for the OIDC deploy role).
- Accounts at the non-AWS parties: Cloudflare (R2 weights mirror), RunPod or vast.ai (GPU fleet), Stripe (billing service, private repo), Sentry (error tracking, free tier).
- Region: eu-west-1 for everything regional; us-east-1 only for the CloudFront certificates.

## 1. Terraform state bootstrap

Created once, by hand, before any Terraform runs:

```
aws s3api create-bucket --bucket potocolom-terraform-state \
  --region eu-west-1 --create-bucket-configuration LocationConstraint=eu-west-1
aws s3api put-bucket-versioning --bucket potocolom-terraform-state \
  --versioning-configuration Status=Enabled
aws dynamodb create-table --table-name potocolom-terraform-locks \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH --billing-mode PAY_PER_REQUEST
```

## 2. CI deploy identity (GitHub OIDC, no long-lived keys)

Create the OIDC provider for `token.actions.githubusercontent.com`, then a role `potocolom-deploy` with this trust policy, so only this repository's main branch can assume it:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Federated": "arn:aws:iam::<account>:oidc-provider/token.actions.githubusercontent.com"},
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {"token.actions.githubusercontent.com:aud": "sts.amazonaws.com"},
      "StringLike": {"token.actions.githubusercontent.com:sub": "repo:Portocolom-Studio/potocolom:ref:refs/heads/main"}
    }
  }]
}
```

Permissions policy for the role, least privilege: ECR push to the project repositories, `ecs:UpdateService`/`ecs:DescribeServices`/`ecs:RegisterTaskDefinition` on the project cluster, `s3:PutObject`/`s3:ListBucket` on the SPA bucket, `cloudfront:CreateInvalidation` on the SPA distribution, and `iam:PassRole` restricted to the two task roles below.

## 3. Network

| Parameter | Value |
|---|---|
| VPC CIDR | 10.0.0.0/16, two AZs (eu-west-1a, eu-west-1b) |
| Public subnets | 10.0.0.0/20, 10.0.16.0/20 (ALB, one NAT gateway) |
| Private subnets | 10.0.128.0/20, 10.0.144.0/20 (Fargate tasks, RDS, Redis) |
| NAT | one gateway (cost decision; a second is an availability upgrade later) |

Security groups, each referencing the previous rather than CIDRs: `alb-sg` allows 443 from the internet; `api-sg` allows 8080 from `alb-sg` only; `rds-sg` allows 5432 from `api-sg` and the private services' SG; `redis-sg` allows 6379 the same way. Nothing in a private subnet is reachable from outside the VPC.

## 4. Data stores

- RDS PostgreSQL 16: `db.t4g.small`, 50 GB gp3, single AZ, automated backups with a 14 day PITR window, deletion protection on, master credentials generated into SSM (never in Terraform state as plain values). Staging: `db.t4g.micro`.
- ElastiCache Redis 7: `cache.t4g.micro`, one node, no replica at launch (the resilience decision: Redis is never the source of truth).

## 5. Buckets, CDN, DNS, certificates

Buckets: `potocolom-spa` (private, CloudFront OAC only), `potocolom-images` (private, versioning enabled, lifecycle rule expiring the `trial/` prefix after 30 days).

CloudFront: two distributions with Origin Access Control.

- SPA distribution (app.potocolom.com): default root `index.html`, and 403/404 responses rewritten to `/index.html` with status 200, which is the SPA fallback the static adapter expects.
- Images distribution (img.potocolom.com): trusted key group for signed URLs (the API holds the private key via SSM and signs short-lived GETs); a `/shared/*` behavior with a short TTL (60 s) for revocable share links.

Certificates: ACM in us-east-1 for both CloudFront domains, ACM in eu-west-1 for api.potocolom.com on the ALB. Route 53: `app` and `img` alias to their distributions, `api` alias to the ALB.

## 6. ECS

One cluster, three services (API public repo; billing and autoscaler from the private repo). Two IAM roles per service:

- Execution role: the managed `AmazonECSTaskExecutionRolePolicy` plus `ssm:GetParameters` and `kms:Decrypt` scoped to `/potocolom/<env>/*`, so tasks pull secrets at start.
- API task role (what the running code can do):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObject"],
     "Resource": "arn:aws:s3:::potocolom-images/*"},
    {"Effect": "Allow", "Action": "ses:SendEmail", "Resource": "*",
     "Condition": {"StringEquals": {"ses:FromAddress": "no-reply@potocolom.com"}}},
    {"Effect": "Allow", "Action": "cloudwatch:PutMetricData", "Resource": "*",
     "Condition": {"StringEquals": {"cloudwatch:namespace": "potocolom"}}}
  ]
}
```

API task definition: 0.5 vCPU, 1 GB, image from ECR, port 8080, `awslogs` driver to `/potocolom/<env>/api` (30 day retention). Service: 2 tasks in the private subnets, the ALB target group from [blueprint.md](blueprint.md) (IP targets, health check `/api/v1/health`, deregistration delay 120), autoscaling 2 to 6 tasks on 60 percent average CPU.

Environment for the API task, values resolved from SSM where secret:

| Variable | Value |
|---|---|
| AUTH_MODE | `oauth` |
| OAUTH_PROVIDERS | `google,github,apple` |
| BILLING_ENABLED | `true` |
| SAFETY_CHECKS | `true` |
| LOG_FORMAT | `json` |
| DATABASE_URL | SSM `/potocolom/prod/database_url` |
| REDIS_URL | ElastiCache endpoint |
| STORAGE_BACKEND | `s3`, plus bucket and the CloudFront signing key reference |
| PUBLIC_URL | `https://app.potocolom.com` |
| QUOTA_SERVICE_URL | the billing service through Service Connect |
| FLEET_TOKEN_KEY | SSM `/potocolom/prod/fleet_token_key` |
| SENTRY_DSN | SSM `/potocolom/prod/sentry_dsn_api` |

## 7. SSM Parameter Store layout

All SecureString under one prefix per environment; the execution roles are scoped to it:

```
/potocolom/prod/database_url
/potocolom/prod/fleet_token_key
/potocolom/prod/cloudfront_signing_key
/potocolom/prod/oauth/google_client_id       oauth/google_client_secret
/potocolom/prod/oauth/github_client_id       oauth/github_client_secret
/potocolom/prod/oauth/apple_key_id           oauth/apple_private_key
/potocolom/prod/sentry_dsn_api               sentry_dsn_worker    sentry_dsn_frontend
/potocolom/prod/stripe_webhook_secret        (billing service)
/potocolom/prod/runpod_api_key               (fleet autoscaler)
/potocolom/prod/r2_access_key                r2_secret_key
```

## 8. SES

Verify the domain (Route 53 DKIM records land automatically when both are in the same account), set a custom MAIL FROM subdomain (`mail.potocolom.com`), and file the production access request to leave the sandbox before launch; approval takes about a day and asks how bounces and complaints are handled (SNS topic, wired to the admin email).

## 9. GPU fleet and the weights mirror

Not in AWS and not in Terraform (the autoscaler rents machines at runtime):

- Cloudflare R2: bucket `potocolom-weights`, a read-only API token for workers, and the vetted weights uploaded with their manifest checksums. R2 charges no egress, which is the reason it exists here.
- RunPod: an API key (into SSM) for the autoscaler; a machine template that runs `ghcr.io/portocolom-studio/potocolom-worker:vX.Y.Z-cuda` with `API_URL=wss://api.potocolom.com/api/v1/fleet`, a short-lived `FLEET_TOKEN` minted by the autoscaler, `DEVICE=cuda`, `LOG_FORMAT=json`, and the R2 mirror URL for the boot-time weights pull.
- The autoscaler's spend rails from [decisions.md](decisions.md): an absolute machine ceiling and a monthly budget are its own configuration, set before the first machine is rented.

## 10. Observability and budgets

- CloudWatch alarms, all notifying one SNS topic with an email subscription: ALB 5xx rate, target group unhealthy hosts, API CPU sustained over 80 percent, queue depth over threshold, no worker heartbeat metric for 5 minutes, RDS free storage under 20 percent, RDS CPU over 80 percent.
- An AWS Budget at the expected monthly total (about 170 USD baseline) with alerts at 80 and 120 percent, because the autoscaler's budget cap covers GPUs but nothing else.
- Sentry: three projects (api, worker, frontend); DSNs into SSM.

## 11. Terraform layout and environments

The project's Terraform lives in the private repository, not here: environment shapes, sizes and account wiring are commercial operational data ([repository-boundary.md](repository-boundary.md)). Its layout, for orientation:

```
terraform/            (private repository)
  modules/        network, ecs-service, rds, redis, cdn, dns
  envs/staging/   one API task, db.t4g.micro, no always-on GPU
  envs/prod/      the sizes above
```

Staging and production are separate member accounts, so the section 1 state bootstrap and the section 2 OIDC role are created once per account. The GPU fleet is deliberately absent from Terraform. Apply order on a fresh account: state bootstrap (section 1), OIDC (2), then `envs/staging` end to end before touching prod. An operator standing up their own cloud from this guide keeps their Terraform wherever they like; only the project's own is private.

The delivery workflow, recorded in [decisions.md](decisions.md) ("Cloud delivery"): `terraform plan` posts on every private-repo pull request and merging applies to staging; application deploys follow the pipeline in [repository-boundary.md](repository-boundary.md) (GHCR to ECR by digest, contract simulation, gated migration task, ECS staging roll); production waits behind a manual approval. Rollout is ECS rolling update with the deployment circuit breaker and alarm-based rollback. A scheduled nightly `terraform plan` reports drift. There is no Kubernetes anywhere in this deployment. The full picture - accounts, every principal's permissions, the stage-by-stage promotion path and rollback - is in [cloud-delivery.md](cloud-delivery.md).

## 12. Go-live checklist

1. `terraform apply` in staging; confirm `curl https://api-staging.../api/v1/health` returns `{"status": "ok"}` through the ALB.
2. `curl .../api/v1/config` shows `auth_methods` for the configured providers and `billing_enabled: true`.
3. Start one rented GPU worker against staging; confirm registration in the logs and a full realtime session from a browser.
4. Run the deploy pipeline once end to end: a tagged release reaches GHCR, ECR, and the staging service; the gated migration task runs before tasks roll.
5. SES out of sandbox, a real verification email delivered.
6. Stripe webhooks reaching the billing service through the ALB path (test mode).
7. Alarm test: stop the worker, watch the heartbeat alarm fire and the SNS email arrive.
8. Only then: `envs/prod` apply, DNS cutover, and the always-on worker floor.
