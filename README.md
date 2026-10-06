# Jesters' Reaux-de-Eaux

Annual krewe parade registration site. Serverless Python (FastAPI on Lambda),
DynamoDB, Stripe Checkout, Gmail SMTP email, Cognito admin auth. Deployed via AWS CDK.

## Local development

    python3 -m venv .venv && source .venv/bin/activate
    pip install -r requirements-dev.txt
    pytest

## Deploy

Requires Docker running (the Lambda bundle is built in a container) and three
CDK context values. See `docs/DEPLOYMENT.md` for the full procedure.

    cd infra
    pip install -r requirements.txt
    cdk deploy \
      -c site_url=https://register.example.com \
      -c cognito_domain_prefix=jesters-rodeo-admin \
      -c ses_sender_email=noreply@example.com

## Switching Stripe modes

Both a test and a live Stripe credential set live side by side in SSM Parameter
Store, under `<SecureParamPrefix>/`:

    stripe_mode                    ("test" or "live")
    stripe_test_secret_key         stripe_live_secret_key
    stripe_test_webhook_secret     stripe_live_webhook_secret
    stripe_test_publishable_key    stripe_live_publishable_key

`SecureParamPrefix` is a `cdk deploy` output, e.g. `/jesters-rodeo/JestersRodeoStack`.

**To go live**, or **back to test**, flip one value — no redeploy, no code
change:

    aws ssm put-parameter --type String --overwrite \
      --name "<SecureParamPrefix>/stripe_mode" --value "live"   # or "test"

Console works too: Systems Manager → Parameter Store → `<SecureParamPrefix>/stripe_mode`
→ Edit → change the value → Save. Rotating the actual key values (e.g. after a
Stripe key rotation) works the same way — they're `SecureString` parameters,
edited via the same Edit button, masked with a "Show" toggle.

The app re-checks SSM at most once a minute (`SECURE_PARAMS_TTL_SECONDS` in
`app/config.py`) rather than only at Lambda cold start, so the flip reaches
every request — checkout, the Stripe webhook, and every admin page view —
within about 60 seconds. A missing or misspelled `stripe_mode` always falls
back to `"test"`, never `"live"`.

Admins always see a small mode badge in the nav — a muted **TEST** in test
mode, a louder **LIVE** once real cards are running — so which mode is active
is never a guess. See `docs/processing-fee.md`
and the "Stripe setup" / "Secrets" sections of `docs/DEPLOYMENT.md` for the
first-time setup of both key sets.