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
| `ses_sender_email` | The Gmail address mail sends from over SMTP (see "Email sending" below) -- name kept from an earlier SES-based setup |

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
aws ssm put-parameter --type SecureString --name "$PREFIX/smtp_password"                --value "<the Gmail App Password -- see Email sending below>"
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

## Email sending (Gmail SMTP)

Mail (`app/emails.py`) sends over SMTP through a Gmail account, authenticated
with an App Password — not through SES. This sidesteps the SES sandbox
entirely (which restricts sending to individually pre-verified recipient
addresses until AWS grants production access): a Gmail account has no such
per-recipient restriction, so there's no approval step to wait on before
going live.

1. Sign in to the Gmail account you want mail to send from (create a
   dedicated one for this, e.g. `jestersrodeo@gmail.com`, rather than reusing
   a personal account).
2. Turn on **2-Step Verification**: myaccount.google.com → **Security** →
   **2-Step Verification**, and follow the prompts. This is required —
   App Passwords don't exist without it.
3. Generate an App Password at **myaccount.google.com/apppasswords**. Name it
   something recognizable (e.g. "Jesters' Reaux-de-Eaux site"). Google shows
   the 16-character password once — copy it immediately.
4. Store it as the `smtp_password` SSM parameter (see the secrets block
   above) — never commit it or put it in `cdk.json`.
5. Set `-c ses_sender_email=<that Gmail address>` on deploy (the context key
   name is a holdover from the SES-based setup this replaced; it's just the
   From address now).

**Gmail's own sending limit** applies instead of SES's: roughly 500
recipients per rolling 24-hour window for a free account, shared across
*everything* this account sends that day (ticket confirmations and
announcement blasts both draw from the same pool). Comfortable for this
app's normal volume; worth keeping in mind before sending one announcement
to a large roster on a day that's also seen a run of ticket sales.

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
off pool-wide (`mfa=cognito.Mfa.OFF` in the stack) -- no authenticator-app
enrollment step. Once you set the password, you land on `/admin/events` with
a session cookie.

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
