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
| Auth mode | `none` or `accounts` | `accounts` with providers | one module, config selects |

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
| NAT | one NAT instance, fck-nat pattern on t4g.nano ([decisions.md](decisions.md), "AWS baseline"); a managed gateway is the availability upgrade later |
| VPC endpoints | S3 gateway endpoint (free), so image and weights traffic never crosses NAT |

Security groups, each referencing the previous rather than CIDRs: `alb-sg` allows 443 from the internet; `api-sg` allows 8080 from `alb-sg` only; `rds-sg` allows 5432 from `api-sg` and the private services' SG; `redis-sg` allows 6379 the same way. Nothing in a private subnet is reachable from outside the VPC.

## 4. Data stores

- RDS PostgreSQL 16: `db.t4g.small`, 50 GB gp3, single AZ, automated backups with a 14 day PITR window, deletion protection on, master credentials generated into SSM (never in Terraform state as plain values). Staging: `db.t4g.micro`.
- ElastiCache, Valkey engine: `cache.t4g.micro`, one node, no replica at launch (the resilience decision: Redis-protocol state is never the source of truth). Valkey is protocol-compatible; the application configures a Redis URL and cannot tell the difference.

## 5. Buckets, CDN, DNS, certificates

Buckets: `potocolom-spa` (private, CloudFront OAC only), `potocolom-images` (private, versioning enabled, lifecycle rules expiring the `trial/` prefix after 30 days and the `dispatch/` prefix after 24 hours). The images bucket has versioning on, so a current-object `Expiration` action only writes a delete marker. The `dispatch/` rule must also expire noncurrent versions after 24 hours and drop expired delete markers, or a replayed upload stays billable.

CloudFront: two distributions with Origin Access Control.

- SPA distribution (app.potocolom.com): default root `index.html`, and 403/404 responses rewritten to `/index.html` with status 200, which is the SPA fallback the static adapter expects.
- Images distribution (img.potocolom.com): trusted key group for signed URLs (the API holds the private key via SSM and signs short-lived GETs). No cached behavior for shares, and no path that carries a share token: the browser posts the token to `POST /api/v1/shared` and the answer carries a signed URL for the picture that lasts 60 seconds. An edge TTL on a share path would decide how long a revoked link keeps working, which is the design [decisions.md](decisions.md) rejected under "Sharing: a token in the fragment, resolved by POST".

Certificates: ACM in us-east-1 for both CloudFront domains, ACM in eu-west-1 for api.potocolom.com on the ALB. Route 53: `app` and `img` alias to their distributions, `api` alias to the ALB.

## 6. ECS

One cluster, three services (API public repo; billing and autoscaler from the private repo). Two IAM roles per service:

- Execution role: the managed `AmazonECSTaskExecutionRolePolicy` plus `ssm:GetParameters` and `kms:Decrypt` scoped to `/potocolom/<env>/*`, so tasks pull secrets at start.
- API task role (what the running code can do). The bucket is versioned, so reclaiming an
  output means deleting its versions, not writing a delete marker: the API lists versions on
  the bucket and deletes them on the objects. Without those four actions the terminal
  cleanup paths fail with AccessDenied and a rejected upload stays billable forever.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObject",
                                   "s3:DeleteObject", "s3:DeleteObjectVersion"],
     "Resource": "arn:aws:s3:::potocolom-images/*"},
    {"Effect": "Allow", "Action": ["s3:ListBucketVersions"],
     "Resource": "arn:aws:s3:::potocolom-images"},
    {"Effect": "Allow", "Action": "ses:SendEmail", "Resource": "*",
     "Condition": {"StringEquals": {"ses:FromAddress": "no-reply@potocolom.com"}}},
    {"Effect": "Allow", "Action": "cloudwatch:PutMetricData", "Resource": "*",
     "Condition": {"StringEquals": {"cloudwatch:namespace": "potocolom"}}}
  ]
}
```

API task definition: 0.5 vCPU, 1 GB, Graviton (`runtimePlatform` ARM64, images built multi-arch), image from ECR, port 8080, `awslogs` driver to `/potocolom/<env>/api` (30 day retention). Service: 2 tasks in the private subnets, the ALB target group from [blueprint.md](blueprint.md) (IP targets, health check `/api/v1/health`, deregistration delay 120), autoscaling 2 to 6 tasks on 60 percent average CPU.

Environment for the API task, values resolved from SSM where secret:

| Variable | Value |
|---|---|
| AUTH_MODE | `accounts`. Password, Google and GitHub sign-in all work. Registration stays invitation-only: a provider can sign in an identity somebody linked, and can never create an account |
| ROOT_KEYS | versioned root key ring for account secrets, newest first; separate key material from the fleet secret, and required whenever AUTH_MODE is accounts |
| EMAIL_BACKEND | `ses`, with `MAIL_FROM` and `SES_REGION`. The API refuses to start if either is missing, and refuses a `PUBLIC_URL` that is not https, because the invitation link is the capability. `MAIL_FROM` must equal the address allowed by the task role's `ses:FromAddress` condition; startup does not check the value, so a mismatch shows up only as AccessDenied on every send. Bounce and complaint feedback needs the SNS topic and subscription in step 8, plus `SES_FEEDBACK_TOPIC_ARN` |
| OAUTH_PROVIDERS | `google,github`. A provider is offered only when its client id and secret are also set, so a half configuration shows no button rather than a broken one |
| GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET | required to offer Google. The authorized redirect URI must be exactly `{PUBLIC_URL}/api/v1/auth/callback/google` |
| GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET | required to offer GitHub. The callback URL must be exactly `{PUBLIC_URL}/api/v1/auth/callback/github` |
| BILLING_ENABLED | `true` |
| SAFETY_CHECKS | `true` |
| LOG_FORMAT | `json` |
| DATABASE_URL | SSM `/potocolom/prod/database_url` |
| REDIS_URL | ElastiCache endpoint |
| STORAGE_BACKEND | `s3`, plus bucket and the CloudFront signing key reference |
| PUBLIC_URL | `https://app.potocolom.com` |
| QUOTA_SERVICE_URL | the billing service, reached and authenticated as described under "Cloud contracts" in [cloud-infrastructure.md](cloud-infrastructure.md); designed, and no such call is made yet |
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
/potocolom/prod/sentry_dsn_api               sentry_dsn_worker    sentry_dsn_frontend
/potocolom/prod/stripe_webhook_secret        (billing service)
/potocolom/prod/runpod_api_key               (fleet autoscaler)
/potocolom/prod/r2_access_key                r2_secret_key
```

## 8. SES

Verify the domain (Route 53 DKIM records land automatically when both are in the same account), set a custom MAIL FROM subdomain (`mail.potocolom.com`), and file the production access request to leave the sandbox before launch; approval takes about a day and asks how bounces and complaints are handled.

Bounce and complaint feedback reaches the API rather than a mailbox, because a bounced address has to stop being invited and only the API holds that list:

1. Create an SNS topic, `potocolom-ses-feedback`, and set its `SignatureVersion` to `2`. The API accepts only version 2; version 1 signs with SHA-1, and refusing it is the reason this is a step rather than a default.
2. Give the topic an access policy that lets only SES publish to it: `sns:Publish` for principal `ses.amazonaws.com`, with `aws:SourceAccount` set to this account and `aws:SourceArn` set to the verified identity's ARN. Without it SES cannot publish at all, and without the conditions the topic ARN stops being evidence of who sent a message, which is the thing the API checks.
3. Set that topic as the Bounce and Complaint notification topic on the verified identity (`SetIdentityNotificationTopic`, or Notifications on the identity in the console). Not delivery notifications: nothing here reads them and they are charged for. A configuration-set event destination also works and the API reads both shapes, but the API does not send a `ConfigurationSetName`, so on this deployment the identity is where it has to be set.
4. Set `SES_FEEDBACK_TOPIC_ARN` on the API task to that topic's ARN and let the new task definition roll, and grant that task role `sns:ConfirmSubscription` on the topic. Do this before subscribing, not after: SNS delivers the confirmation the moment the subscription is created, and an API that does not yet know its topic refuses it with a `403`. SNS does not retry a refusal, so the subscription would sit pending until somebody noticed and requested confirmation again.
5. Subscribe the topic to `https://api.potocolom.com/api/v1/mail/feedback` over HTTPS. The API confirms it once it has verified the signature and matched the topic, by calling SNS rather than by fetching the `SubscribeURL`, so the one-use token never reaches a log. It confirms with `AuthenticateOnUnsubscribe`, so ending the subscription later needs an AWS credential rather than just the unsubscribe link SNS puts in every notification.

The endpoint presents no credential of its own and needs none: SNS signs each message with the regional key, the topic ARN says the message is ours, and the topic policy in step 2 is what keeps anybody else from publishing to it. A permanent bounce and a complaint retire the address; a transient bounce does not, because a full mailbox is not a dead address.

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

## 12. Operator recovery

The commands that get back into a locked-out installation are the same ones a self-hoster runs, and in the cloud they run inside the API task rather than on a laptop. They need `DATABASE_URL` and `ROOT_KEYS`. The execution role reads those from SSM and KMS when the task starts and injects them; the API task role itself needs neither, and a shell in a running task simply inherits the values already in its environment.

```bash
aws ecs execute-command --cluster potocolom --task <task-id> --container api --interactive \
  --command "python -m app.operator reclaim --restore admin@example.com"
```

- `reclaim --restore EMAIL` makes one account an active administrator again, for the case where every administrator was suspended, deleted or demoted at once.
- `reclaim --claim` mints a fresh setup link and retires whatever was outstanding. It is refused while the install still has accounts: the setup link adopts the implicit local user, which the claim route allows only on an install nobody has claimed. On a running cloud installation `--restore` is the command.
- `python -m app.recovery EMAIL` prints a one-use ten-minute password link for one administrator. It is printed and never mailed: a credential recoverable from a mailbox is only as strong as that mailbox.
- `rotate-keys` moves stored secrets onto a new `ROOT_KEYS` entry; `rotate-keys --check` changes nothing and reports which older versions are still holding something. Put the new key at the front of the SSM parameter, roll the tasks so every replica can read both, rotate, check, and only then remove the old one.
- `collapse` has no sensible cloud meaning: turning accounts off on a multi-tenant install would destroy every customer's account and answer every request as an administrator. Nothing in the code stops it, and this is a policy rather than a guard: anybody who can open an ECS Exec session in the API task can run it, which is one more reason Exec stays off between incidents. If that is not tolerable for your installation, drop the confirmation phrase from an environment the task cannot read, or run the API image with the operator module removed.

ECS Exec is off between incidents, because a shell in the API task is a shell with the root key ring in its environment. Turning it on and off is a deployment each way: the setting applies to new tasks only.

Four prerequisites. The first three fail as a hanging `execute-command` rather than a clear error; the fourth says what is wrong but not where:

- `enableExecuteCommand` on the service.
- `ssmmessages:CreateControlChannel`, `CreateDataChannel`, `OpenControlChannel` and `OpenDataChannel` on the **task role**, not the execution role.
- A network path to SSM. The API tasks run in private subnets, so that is either NAT egress on 443 or an `ssmmessages` interface VPC endpoint the task security group can reach on 443.
- On the operator's own machine: `ecs:ExecuteCommand` for that cluster, and the Session Manager plugin installed beside the AWS CLI. Without the plugin the command fails with `SessionManagerPlugin is not found`, which reads like a service problem and is not one.

```bash
# Before: no session logging, because the recovery command prints a live credential.
aws ecs update-cluster --cluster potocolom \
  --configuration 'executeCommandConfiguration={logging=NONE}'
aws ecs update-service --cluster potocolom --service api \
  --enable-execute-command --force-new-deployment
aws ecs wait services-stable --cluster potocolom --services api

# The setting applies to new tasks, and the agent starts a moment after the
# task does. Check the task you are about to use, not the service.
aws ecs describe-tasks --cluster potocolom --tasks <new-task-id> \
  --query 'tasks[].{exec:enableExecuteCommand,
                    agent:containers[?name==`api`].managedAgents[?name==`ExecuteCommandAgent`].lastStatus}'
# exec: true, agent: RUNNING

# ... run the recovery command against that task ...

# After: close it again and put the cluster's logging back.
aws ecs update-service --cluster potocolom --service api \
  --disable-execute-command --force-new-deployment
aws ecs wait services-stable --cluster potocolom --services api
aws ecs describe-tasks --cluster potocolom --tasks <replacement-task-id> \
  --query 'tasks[].enableExecuteCommand'   # false on the replacements
aws ecs update-cluster --cluster potocolom \
  --configuration 'executeCommandConfiguration={logging=DEFAULT}'
```

`logging=NONE` comes first for a reason. `python -m app.recovery EMAIL` prints a live one-use link to stdout, and Exec's `DEFAULT` logging copies the session into the task's CloudWatch stream, where it sits for the retention period as a working credential anybody with log access can spend. Any other sink is the same: whatever holds these sessions holds recovery links and has to be treated as credential-bearing. Put the logging configuration back afterwards, or the next Exec session anybody opens for an unrelated reason goes unrecorded.

## 13. Go-live checklist

1. `terraform apply` in staging; confirm `curl https://api-staging.../api/v1/health` returns `{"status": "ok"}` through the ALB.
2. `curl .../api/v1/config` shows `auth_methods` for the configured providers and `billing_enabled: true`. With no OAuth credentials configured that is `["password"]`.
3. Start one rented GPU worker against staging; confirm registration in the logs and a full realtime session from a browser. The realtime socket authenticates the session on the upgrade, so this check works in either mode.
4. Run the deploy pipeline once end to end: a tagged release reaches GHCR, ECR, and the staging service; the gated migration task runs before tasks roll.
5. SES out of sandbox, a real verification email delivered.
6. Stripe webhooks reaching the billing service through the ALB path (test mode).
7. Alarm test: stop the worker, watch the heartbeat alarm fire and the SNS email arrive.
8. Only then: `envs/prod` apply, DNS cutover, and the always-on worker floor.
