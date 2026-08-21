# Deployment Guide

## Prerequisites
1. An AWS account, with the AWS CLI configured (`aws configure`) for an IAM
   principal that can deploy CloudFormation.
2. Node and the CDK CLI: `npm install -g aws-cdk`
3. **Docker running.** The Lambda asset is built by pip-installing
   `requirements.txt` inside the Python 3.12 build image. Without Docker,
   `cdk deploy` fails at the bundling step.
4. One-time CDK bootstrap: `cd infra && cdk bootstrap`

## Required context values

`infra/cdk.json` exists only to tell the CDK CLI how to invoke the app
(`python3 app.py`) — it deliberately carries no context values. Every deploy
needs three, supplied as `-c key=value` (none of them are secret, so they may
also be committed into `cdk.json`'s `context` block if you prefer):

| Key | Meaning |
| --- | --- |
| `site_url` | Public base URL, no trailing slash |
| `cognito_domain_prefix` | Globally unique Hosted UI prefix, e.g. `jesters-rodeo-admin` |
| `ses_sender_email` | The From address for confirmation emails |

Two more are optional and must be supplied together to enable a custom domain:
`domain_name` and `hosted_zone_id`.

## The chicken-and-egg on `site_url`

Cognito needs the callback URL before the API exists, so `site_url` is an input
rather than something the stack derives. That gives two paths:

- **With a custom domain (recommended):** you already know the URL. Create the
  Route 53 hosted zone first, note its zone ID, and deploy once.
- **Without one:** deploy with a placeholder, read the `ApiUrl` output, then
  deploy a second time with `-c site_url=<that URL>`. Only the second deploy
  produces a working login.

## First deploy

```bash
cd infra
source .venv/bin/activate
cdk deploy \
  -c site_url=https://register.example.com \
  -c cognito_domain_prefix=jesters-rodeo-admin \
  -c ses_sender_email=noreply@example.com \
  -c domain_name=register.example.com \
  -c hosted_zone_id=Z0123456789ABCDEFGHIJ
```

Note the `ApiUrl`, `SiteUrl`, `UserPoolId`, `UserPoolClientId`, `CognitoDomain`,
and `SecureParamPrefix` outputs.

## Secrets (Task 13)

Create all four SecureString parameters under `SecureParamPrefix` before the
first real checkout. Start with Stripe **test** keys.

```bash
PREFIX=<the SecureParamPrefix output, e.g. /jesters-rodeo/prod>

aws ssm put-parameter --type SecureString --name "$PREFIX/stripe_secret_key"      --value "sk_test_..."
aws ssm put-parameter --type SecureString --name "$PREFIX/stripe_webhook_secret"  --value "whsec_..."
aws ssm put-parameter --type SecureString --name "$PREFIX/stripe_publishable_key" --value "pk_test_..."
aws ssm put-parameter --type SecureString --name "$PREFIX/session_secret"         --value "$(openssl rand -hex 32)"
```

The names must match exactly — they are the field names in `SECURE_FIELDS` in
`app/config.py`. A missing or misnamed parameter does not fail the deploy: the
app logs a warning at cold start and falls back to an empty/default value, so
grep CloudWatch Logs for `is missing or unreadable` after the first request.
Add `--overwrite` to rotate a value that already exists.

## SES production access

New accounts start in the SES sandbox and can only send to verified addresses.
Before go-live:
1. Confirm the `ses_sender_email` identity (CDK requests verification; click the
   link in the email AWS sends).
2. In the SES console choose **Request production access**, describing the use
   case as transactional order-confirmation email for an event registration
   site. It is free and usually approved within a day.

Until this is granted, only verified recipients receive tickets — which is fine
for testing and fatal on sale day, so do it early.

## Stripe setup

1. Create a Stripe account and start in test mode.
2. Add a webhook endpoint at `https://<site_url>/webhooks/stripe`, subscribed to
   **`checkout.session.completed`** and **`checkout.session.expired`**. Both are
   required: without the expiry event, abandoned checkouts hold their seats
   until the cleanup job catches them.
3. Copy the signing secret into the `stripe_webhook_secret` parameter.
4. Complete a full test purchase before switching to live keys. Going live means
   new live-mode keys *and* a new live-mode webhook endpoint with its own
   signing secret — the test-mode secret will not validate live events.

## Create the first admin user

Cognito self-signup is disabled, so admins are created explicitly:

```bash
aws cognito-idp admin-create-user \
  --user-pool-id <UserPoolId> \
  --username admin@example.com \
  --user-attributes Name=email,Value=admin@example.com Name=email_verified,Value=true \
  --temporary-password 'ChangeMe-Temp-123!'
```

Then visit `<site_url>/admin/login`, which redirects to the Cognito Hosted UI.
You will be prompted to set a permanent password on first sign-in, and land back
on `/admin/events` with a session cookie.

## Pre-event checklist (run before opening registration each year)

1. Create the new year's event in the admin dashboard; opening it automatically
   closes last year's.
2. Confirm Stripe is in the intended mode and the webhook endpoint points at the
   current `site_url`.
3. Run a full test purchase end to end: checkout → webhook → confirmation email
   with a scannable QR code.
4. Scan that ticket at `/admin/checkin?event_id=...` and confirm a second scan
   reports "already checked in".
5. Refund the test order and confirm the ticket then reports "voided" at
   check-in and capacity is returned.
6. Export the CSV and open it in a spreadsheet.
7. Switch to live Stripe keys, then open registration.
