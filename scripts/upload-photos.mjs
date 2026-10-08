// Uploads <dir>/<album>/* to the photos bucket as originals plus generated thumbnails.
//
//   node upload-photos.mjs <dir> --bucket <name> [--dry-run]
//
// Bucket name may also come from PHOTOS_BUCKET. AWS credentials come from the usual SDK sources.
import { readdir, readFile, stat } from 'node:fs/promises';
import { extname, join, parse, resolve } from 'node:path';
import { parseArgs } from 'node:util';
import { HeadObjectCommand, PutObjectCommand, S3Client } from '@aws-sdk/client-s3';
import sharp from 'sharp';

const EXTENSIONS = { '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp', '.avif': 'image/avif' };
const THUMB = { size: 480, quality: 78 };
const CONCURRENCY = 4;

const { values, positionals } = parseArgs({
  allowPositionals: true,
  options: { bucket: { type: 'string' }, 'dry-run': { type: 'boolean', default: false } },
});
const bucket = values.bucket ?? process.env.PHOTOS_BUCKET;
const dryRun = values['dry-run'];
if (positionals.length !== 1 || !bucket) {
  console.error('usage: node upload-photos.mjs <dir> --bucket <name> [--dry-run]');
  process.exit(1);
}
const srcDir = resolve(positionals[0]);
const s3 = new S3Client({});

const slug = (s) =>
  s
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '') || 'untitled';
const titleCase = (s) =>
  s
    .replace(/[-_]+/g, ' ')
    .trim()
    .replace(/\b\w/g, (c) => c.toUpperCase());

async function head(key) {
  try {
    return await s3.send(new HeadObjectCommand({ Bucket: bucket, Key: key }));
  } catch (err) {
    if (err.$metadata?.httpStatusCode === 404) return null;
    throw err;
  }
}

async function put(key, body, contentType, metadata) {
  if (dryRun) return;
  await s3.send(new PutObjectCommand({ Bucket: bucket, Key: key, Body: body, ContentType: contentType, Metadata: metadata }));
}

async function albumMetadata(dir, fallbackTitle) {
  try {
    const data = JSON.parse(await readFile(join(dir, 'album.json'), 'utf8'));
    if (data && typeof data === 'object' && !Array.isArray(data)) {
      return { ...data, title: typeof data.title === 'string' && data.title.trim() ? data.title.trim() : fallbackTitle };
    }
  } catch (err) {
    if (err.code !== 'ENOENT') throw err;
  }
  return { title: fallbackTitle };
}

async function uploadImage(albumId, id, file, title) {
  const originalKey = `albums/${albumId}/originals/${id}${extname(file).toLowerCase()}`;
  const thumbKey = `albums/${albumId}/thumbs/${id}.webp`;
  const size = (await stat(file)).size;

  const existing = await head(originalKey);
  const thumbExists = existing ? await head(thumbKey) : null;
  if (existing && thumbExists && existing.ContentLength === size && existing.Metadata?.width) {
    return 'unchanged';
  }

  const image = sharp(file);
  const { width, height, orientation } = await image.metadata();
  // EXIF orientations 5-8 are rotated 90 degrees, so the displayed size swaps.
  const swap = orientation >= 5;
  const metadata = {
    width: String(swap ? height : width),
    height: String(swap ? width : height),
    title: encodeURIComponent(title),
  };

  const thumb = await image
    .rotate()
    .resize({ width: THUMB.size, height: THUMB.size, fit: 'inside', withoutEnlargement: true })
    .webp({ quality: THUMB.quality })
    .toBuffer();

  await put(originalKey, await readFile(file), EXTENSIONS[extname(file).toLowerCase()], metadata);
  await put(thumbKey, thumb, 'image/webp');
  return 'uploaded';
}

async function runPool(tasks, limit) {
  const results = [];
  let next = 0;
  await Promise.all(
    Array.from({ length: limit }, async () => {
      while (next < tasks.length) {
        const i = next++;
        results[i] = await tasks[i]();
      }
    }),
  );
  return results;
}

const albumDirs = (await readdir(srcDir, { withFileTypes: true }))
  .filter((e) => e.isDirectory() && !e.name.startsWith('.'))
  .map((e) => e.name)
  .sort();

const counts = { uploaded: 0, unchanged: 0 };
for (const name of albumDirs) {
  const dir = join(srcDir, name);
  const albumId = slug(name);
  const files = (await readdir(dir)).filter((f) => extname(f).toLowerCase() in EXTENSIONS).sort();

  const used = new Set();
  const tasks = files.map((file) => {
    const base = parse(file).name;
    let id = slug(base);
    for (let n = 2; used.has(id); n++) id = `${slug(base)}-${n}`;
    used.add(id);
    return () => uploadImage(albumId, id, join(dir, file), titleCase(base));
  });

  const meta = await albumMetadata(dir, titleCase(name));
  await put(`albums/${albumId}/album.json`, JSON.stringify(meta, null, 2), 'application/json');

  const results = await runPool(tasks, CONCURRENCY);
  for (const r of results) counts[r]++;
  console.log(`${albumId}: ${files.length} image(s)`);
}

console.log(`${dryRun ? '[dry run] ' : ''}${counts.uploaded} uploaded, ${counts.unchanged} unchanged`);
