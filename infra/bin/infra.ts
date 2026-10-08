import * as cdk from 'aws-cdk-lib';
import { PhotographyStack } from '../lib/photography-stack';

// cdk deploy -c stage=dev
// cdk deploy -c stage=prod -c origins=https://www.example.com,https://example.com
const app = new cdk.App();
const stage: string = app.node.tryGetContext('stage') ?? 'dev';
if (stage !== 'dev' && stage !== 'prod') {
  throw new Error(`Unknown stage "${stage}"; use -c stage=dev or -c stage=prod`);
}

const origins: string | undefined = app.node.tryGetContext('origins');
const allowedOrigins = origins
  ? origins.split(',').map((o) => o.trim()).filter(Boolean)
  : stage === 'dev'
    ? ['http://localhost:4200']
    : [];
if (allowedOrigins.length === 0) {
  throw new Error('The prod stage needs the site origin(s): -c origins=https://your-domain');
}

new PhotographyStack(app, stage === 'prod' ? 'PhotographyStackProd' : 'PhotographyStackDev', {
  stage,
  allowedOrigins,
  env: {
    account: process.env.CDK_DEFAULT_ACCOUNT,
    region: process.env.CDK_DEFAULT_REGION ?? 'us-east-1',
  },
});
