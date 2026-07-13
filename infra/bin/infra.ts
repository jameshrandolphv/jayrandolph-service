import * as cdk from 'aws-cdk-lib';
import { PhotographyStack } from '../lib/photography-stack';

const app = new cdk.App();
const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region:  process.env.CDK_DEFAULT_REGION ?? 'us-east-1',
};

new PhotographyStack(app, 'PhotographyStackDev', {
  stage:         'dev',
  allowedOrigin: 'http://localhost:4200',
  env,
});

new PhotographyStack(app, 'PhotographyStackProd', {
  stage:         'prod',
  allowedOrigin: 'https://YOUR_AMPLIFY_DOMAIN.amplifyapp.com', // ← replace before deploying prod
  env,
});
