# Jester's Rodeo

Annual krewe parade registration site. Serverless Python (FastAPI on Lambda),
DynamoDB, Stripe Checkout, SES email, Cognito admin auth. Deployed via AWS CDK.

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