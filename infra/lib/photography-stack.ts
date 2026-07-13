import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as rds from 'aws-cdk-lib/aws-rds';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as apigwv2 from 'aws-cdk-lib/aws-apigatewayv2';
import * as integrations from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as path from 'path';

export interface PhotographyStackProps extends cdk.StackProps {
  stage: string;
  allowedOrigin: string;
}

export class PhotographyStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: PhotographyStackProps) {
    super(scope, id, props);

    const { stage, allowedOrigin } = props;
    const isProd = stage === 'prod';

    // ── S3 ──────────────────────────────────────────────────────────────────
    const photosBucket = new s3.Bucket(this, 'PhotosBucket', {
      bucketName:        `jayrandolph-photos-${stage}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      encryption:        s3.BucketEncryption.S3_MANAGED,
      versioned:         true,
      removalPolicy:     isProd ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
      autoDeleteObjects: !isProd,
    });

    // ── VPC (Aurora requires one; Lambda uses Data API so stays outside VPC) ─
    const vpc = new ec2.Vpc(this, 'Vpc', {
      maxAzs: 2,
      natGateways: 0,
      subnetConfiguration: [
        { name: 'isolated', subnetType: ec2.SubnetType.PRIVATE_ISOLATED },
      ],
    });

    // ── Aurora Serverless v2 PostgreSQL ──────────────────────────────────────
    const dbCluster = new rds.DatabaseCluster(this, 'DbCluster', {
      clusterIdentifier:       `photography-db-${stage}`,
      engine: rds.DatabaseClusterEngine.auroraPostgres({
        version: rds.AuroraPostgresEngineVersion.VER_16_4,
      }),
      serverlessV2MinCapacity: 0.5,
      serverlessV2MaxCapacity: isProd ? 8 : 2,
      writer:                  rds.ClusterInstance.serverlessV2('writer'),
      vpc,
      vpcSubnets:              { subnetType: ec2.SubnetType.PRIVATE_ISOLATED },
      defaultDatabaseName:     'photography',
      enableDataApi:           true,
      removalPolicy:           isProd ? cdk.RemovalPolicy.RETAIN : cdk.RemovalPolicy.DESTROY,
    });

    // ── Shared Lambda layer ──────────────────────────────────────────────────
    const sharedLayer = new lambda.LayerVersion(this, 'SharedLayer', {
      layerVersionName:   `photography-shared-${stage}`,
      code:               lambda.Code.fromAsset(
                            path.join(__dirname, '../../lambdas/shared_layer')
                          ),
      compatibleRuntimes: [lambda.Runtime.PYTHON_3_12],
      description:        'Shared DB + response utilities',
    });

    // ── IAM role shared by both Lambdas ──────────────────────────────────────
    const lambdaRole = new iam.Role(this, 'LambdaRole', {
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          'service-role/AWSLambdaBasicExecutionRole'
        ),
      ],
    });

    lambdaRole.addToPolicy(new iam.PolicyStatement({
      actions:   ['rds-data:ExecuteStatement', 'rds-data:BatchExecuteStatement'],
      resources: [dbCluster.clusterArn],
    }));

    dbCluster.secret!.grantRead(lambdaRole);

    const sharedEnv: Record<string, string> = {
      DB_CLUSTER_ARN: dbCluster.clusterArn,
      DB_SECRET_ARN:  dbCluster.secret!.secretArn,
      DB_NAME:        'photography',
      ALLOWED_ORIGIN: allowedOrigin,
    };

    // ── Lambda: list photo groups ────────────────────────────────────────────
    const listGroupsFn = new lambda.Function(this, 'ListPhotoGroupsFn', {
      functionName: `photography-list-groups-${stage}`,
      runtime:      lambda.Runtime.PYTHON_3_12,
      handler:      'handler.handler',
      code:         lambda.Code.fromAsset(
                      path.join(__dirname, '../../lambdas/photo_groups')
                    ),
      layers:       [sharedLayer],
      role:         lambdaRole,
      environment:  sharedEnv,
      timeout:      cdk.Duration.seconds(10),
    });

    // ── Lambda: get photos for group (with presigned URLs) ───────────────────
    const getPhotosFn = new lambda.Function(this, 'GetPhotosFn', {
      functionName: `photography-get-photos-${stage}`,
      runtime:      lambda.Runtime.PYTHON_3_12,
      handler:      'handler.handler',
      code:         lambda.Code.fromAsset(
                      path.join(__dirname, '../../lambdas/photos_by_group')
                    ),
      layers:       [sharedLayer],
      role:         lambdaRole,
      environment:  {
        ...sharedEnv,
        PHOTOS_BUCKET_NAME: photosBucket.bucketName,
        PRESIGNED_URL_TTL:  '900',
      },
      timeout:      cdk.Duration.seconds(15),
    });

    // GetObject only — no ListBucket, no Put
    photosBucket.grantRead(getPhotosFn);

    // ── HTTP API Gateway ─────────────────────────────────────────────────────
    const api = new apigwv2.HttpApi(this, 'PhotographyApi', {
      apiName: `photography-api-${stage}`,
      corsPreflight: {
        allowOrigins: [allowedOrigin],
        allowMethods: [apigwv2.CorsHttpMethod.GET],
        allowHeaders: ['Content-Type'],
        maxAge:       cdk.Duration.days(1),
      },
    });

    api.addRoutes({
      path:        '/photo-groups',
      methods:     [apigwv2.HttpMethod.GET],
      integration: new integrations.HttpLambdaIntegration(
                     'ListGroupsIntegration', listGroupsFn
                   ),
    });

    api.addRoutes({
      path:        '/photo-groups/{groupId}/photos',
      methods:     [apigwv2.HttpMethod.GET],
      integration: new integrations.HttpLambdaIntegration(
                     'GetPhotosIntegration', getPhotosFn
                   ),
    });

    // ── Outputs ──────────────────────────────────────────────────────────────
    new cdk.CfnOutput(this, 'ApiUrl', {
      value:      api.apiEndpoint,
      exportName: `PhotographyApiUrl-${stage}`,
    });

    new cdk.CfnOutput(this, 'PhotosBucketName', {
      value:      photosBucket.bucketName,
      exportName: `PhotosBucketName-${stage}`,
    });

    new cdk.CfnOutput(this, 'DbClusterArn', {
      value:      dbCluster.clusterArn,
      exportName: `PhotographyDbClusterArn-${stage}`,
    });
  }
}
