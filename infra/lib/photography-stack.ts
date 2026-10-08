import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as apigwv2 from 'aws-cdk-lib/aws-apigatewayv2';
import * as integrations from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import * as path from 'path';

export interface PhotographyStackProps extends cdk.StackProps {
  stage: string;
  allowedOrigins: string[];
}

export class PhotographyStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: PhotographyStackProps) {
    super(scope, id, props);

    const { stage, allowedOrigins } = props;
    const isProd = stage === 'prod';
    const urlTtlSeconds = 3600;

    // Private bucket: objects are only reachable through the presigned URLs the Lambda issues.
    const photosBucket = new s3.Bucket(this, 'PhotosBucket', {
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption: s3.BucketEncryption.S3_MANAGED,
      enforceSSL: true,
      versioned: true,
      lifecycleRules: [{ noncurrentVersionExpiration: cdk.Duration.days(90) }],
      removalPolicy: isProd ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: !isProd,
    });

    const listPhotosFn = new lambda.Function(this, 'ListPhotosFn', {
      functionName: `photography-list-photos-${stage}`,
      runtime: lambda.Runtime.PYTHON_3_12,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(__dirname, '../../lambdas/photos'), {
        exclude: ['tests', '__pycache__', '.pytest_cache', 'requirements-dev.txt'],
      }),
      environment: {
        PHOTOS_BUCKET_NAME: photosBucket.bucketName,
        PRESIGNED_URL_TTL: String(urlTtlSeconds),
      },
      memorySize: 256,
      timeout: cdk.Duration.seconds(25),
      logGroup: new logs.LogGroup(this, 'ListPhotosLogs', {
        retention: logs.RetentionDays.ONE_MONTH,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
    });

    // Read-only: GetObject/HeadObject on the albums prefix, plus ListBucket to enumerate it.
    photosBucket.grantRead(listPhotosFn, 'albums/*');

    const api = new apigwv2.HttpApi(this, 'PhotographyApi', {
      apiName: `photography-api-${stage}`,
      createDefaultStage: false,
      corsPreflight: {
        allowOrigins: allowedOrigins,
        allowMethods: [apigwv2.CorsHttpMethod.GET],
        allowHeaders: ['Content-Type'],
        maxAge: cdk.Duration.days(1),
      },
    });

    // The endpoint is public and each call signs URLs, so cap the request rate.
    new apigwv2.HttpStage(this, 'DefaultStage', {
      httpApi: api,
      stageName: '$default',
      autoDeploy: true,
      throttle: { rateLimit: 10, burstLimit: 20 },
    });

    api.addRoutes({
      path: '/albums',
      methods: [apigwv2.HttpMethod.GET],
      integration: new integrations.HttpLambdaIntegration('ListPhotosIntegration', listPhotosFn),
    });

    new cdk.CfnOutput(this, 'ApiUrl', { value: api.apiEndpoint });
    new cdk.CfnOutput(this, 'PhotosBucketName', { value: photosBucket.bucketName });
  }
}
