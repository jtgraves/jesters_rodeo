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
| `ses_sender_email` | The From address for confirmation emails -- must be at `domain_name` when that's configured (see SES production access below) |

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

Create these SecureString/String parameters under `SecureParamPrefix` **before
the first request the app receives** — `session_secret` is required, not
optional: the app refuses to start (every request 500s, visible immediately
in CloudWatch as an init failure) rather than silently signing sessions with
the public placeholder in `app/config.py`. Test and live Stripe credentials
live side by side — `stripe_mode` picks which pair is active, so both sets
can be filled in ahead of time and switched with no redeploy. See README.md's
"Switching Stripe modes" for how to flip it later.

```bash
PREFIX=<the SecureParamPrefix output, e.g. /jesters-rodeo/prod>

aws ssm put-parameter --type String       --name "$PREFIX/stripe_mode"                  --value "test"
aws ssm put-parameter --type SecureString --name "$PREFIX/stripe_test_secret_key"       --value "sk_test_..."
aws ssm put-parameter --type SecureString --name "$PREFIX/stripe_test_webhook_secret"   --value "whsec_..."
aws ssm put-parameter --type SecureString --name "$PREFIX/stripe_test_publishable_key"  --value "pk_test_..."
aws ssm put-parameter --type SecureString --name "$PREFIX/stripe_live_secret_key"       --value "sk_live_..."
aws ssm put-parameter --type SecureString --name "$PREFIX/stripe_live_webhook_secret"   --value "whsec_..."
aws ssm put-parameter --type SecureString --name "$PREFIX/stripe_live_publishable_key"  --value "pk_live_..."
aws ssm put-parameter --type SecureString --name "$PREFIX/session_secret"               --value "$(openssl rand -hex 32)"
```

The live-mode values can be filled in with placeholders and rotated in later,
once that Stripe account and webhook exist (see "Stripe setup" below) — an
empty/placeholder live key just means switching to live mode too early fails
loudly at checkout, not silently.

The names must match exactly — they are the field names in `SECURE_FIELDS` in
`app/config.py`. A missing or misnamed Stripe parameter does not fail the
deploy: the app logs a warning at cold start (and at every refresh — see
README.md) and falls back to an empty/default value, which just makes the
next Stripe call fail loudly. A missing `session_secret` is the one exception
— that one fails the Lambda's cold start outright (see above), by design.
Grep CloudWatch Logs for `is missing or unreadable` to find which parameter
needs attention. Add `--overwrite` to rotate a value that already exists.

## SES production access

New accounts start in the SES sandbox and can only send to verified addresses.
Before go-live:
1. Confirm the `ses_sender_email` identity.
   - **With `domain_name`/`hosted_zone_id` configured** (the recommended
     path): the stack verifies a *domain* identity for `domain_name`, with
     DKIM — CDK writes the DKIM and MAIL FROM records into the Route 53
     hosted zone itself, so there's no manual click-through step. `Verified`
     status in the SES console can lag a few minutes behind the deploy while
     those DNS records propagate. `ses_sender_email` must be an address at
     that same domain (e.g. `noreply@register.example.com`) — DKIM signing
     applies to the domain in the message's From header, so an address on a
     different domain wouldn't actually be covered by it.
   - **Without a custom domain**: the stack falls back to a plain email
     identity for `ses_sender_email` itself — click the verification link
     AWS emails to that address.
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
3. Copy the signing secret into the `stripe_test_webhook_secret` parameter.
4. Complete a full test purchase (`stripe_mode` = `test`) before going live.
   Going live means new live-mode keys *and* a **separate** live-mode webhook
   endpoint with its own signing secret — copy that one into
   `stripe_live_webhook_secret`. A test-mode secret will not validate live
   events or vice versa.

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
You will be prompted to set a permanent password on first sign-in. MFA is
required for every admin: right after that, the Hosted UI shows a QR code —
scan it with an authenticator app (Google Authenticator, Authy, 1Password,
etc.) and enter the 6-digit code once to finish enrolling. From then on every
sign-in asks for a fresh code after the password. Have an authenticator app
ready before running through this. Once enrolled, you land on `/admin/events`
with a session cookie.

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
7. Flip `stripe_mode` to `live` (README.md, "Switching Stripe modes"), confirm
   the **LIVE** badge appears in the admin nav within a minute, then open
   registration.
