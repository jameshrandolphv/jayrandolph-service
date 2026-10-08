# jayrandolph-service

Backend for [jayrandolph](https://github.com/jameshrandolphv/jayrandolph): photos live in a private S3 bucket and a Lambda behind an HTTP API lists them, returning album/photo metadata plus presigned URLs for each thumbnail and original.

## Layout

- `infra/` – AWS CDK app (private S3 bucket, Lambda, HTTP API).
- `lambdas/photos/` – the Lambda (`GET /albums`) and its tests.
- `scripts/upload-photos.mjs` – generates thumbnails and uploads originals.

## Bucket layout

```
albums/<album-id>/album.json            optional {"title": "...", ...any extra metadata}
albums/<album-id>/originals/<id>.<ext>  original; user metadata: width, height, title
albums/<album-id>/thumbs/<id>.webp      thumbnail
```

Objects whose file name starts with `private-` (case-insensitive) are never listed or presigned. Add the prefix to both the original and its thumbnail (the upload script keeps a `private-` file name as is), or just the original.

Use the upload script rather than uploading by hand: the Lambda skips photos that have no thumbnail or no `width`/`height` metadata (it logs a warning).

## API

`GET /albums`

```json
{
  "albums": [
    {
      "id": "2026-09-01-kodak-portra-400",
      "title": "Kodak Portra 400",
      "metadata": { "camera": "Nikon F3" },
      "images": [
        {
          "id": "scan-1",
          "name": "Scan (1)",
          "thumb": "<presigned URL>",
          "src": "<presigned URL>",
          "width": 4876,
          "height": 3304,
          "size": 5003846,
          "lastModified": "2026-10-08T13:20:32+00:00"
        }
      ]
    }
  ],
  "expiresIn": 3600,
  "expiresAt": 1791465600
}
```

Everything in `album.json` other than `title` is returned as the album's `metadata`. Presigned URLs last `expiresIn` seconds (`PRESIGNED_URL_TTL`, set in `infra/lib/photography-stack.ts`); clients should refetch before `expiresAt` (epoch seconds). The Lambda's temporary credentials can cut a URL's life short, so refetch well before it.

## Deploy

```bash
cd infra && npm install
npx cdk bootstrap                                  # once per account/region
npx cdk deploy -c stage=dev                        # CORS allows http://localhost:4200
npx cdk deploy -c stage=prod -c origins=https://your-domain   # comma-separate multiple origins
```

The `ApiUrl` and `PhotosBucketName` outputs go into the site config and the upload command below. Deploying replaces the previous Aurora/VPC-based stack, so that stack's database is removed (the prod database was set to retain, so it must be deleted manually).

## Upload photos

Source directory: one folder per album, images inside (jpg, jpeg, png, webp, avif); an optional `album.json` with `{ "title": "…" }`.

```bash
cd scripts && npm install
node upload-photos.mjs ../../jay-randolph/photos-src --bucket <PhotosBucketName>
```

Re-running skips originals that are already uploaded with the same size. To remove an album or photo, delete its keys under `albums/` (for example `aws s3 rm s3://<bucket>/albums/<album-id>/ --recursive`).

## Tests

```bash
cd lambdas/photos
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```
