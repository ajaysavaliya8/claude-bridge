// Image sharing helpers. Images are validated + base64-encoded by the asker,
// sent over HTTP, and saved to disk on the answering side so its (interactive)
// Claude can Read them — no API key needed.

import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { basename, join } from "node:path";

export const MAX_IMAGE_BYTES = 5_000_000; // 5 MB per image
export const MAX_IMAGES = 5;
export const MAX_PAYLOAD_BYTES = 30_000_000; // HTTP body cap (a few images, base64)

const EXT = { "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp" };

// Sniff the media type from magic bytes (don't trust the extension).
function detectMediaType(buf) {
  if (buf.length >= 4 && buf[0] === 0x89 && buf[1] === 0x50 && buf[2] === 0x4e && buf[3] === 0x47) return "image/png";
  if (buf.length >= 3 && buf[0] === 0xff && buf[1] === 0xd8 && buf[2] === 0xff) return "image/jpeg";
  if (buf.length >= 4 && buf[0] === 0x47 && buf[1] === 0x49 && buf[2] === 0x46 && buf[3] === 0x38) return "image/gif";
  if (buf.length >= 12 && buf.toString("ascii", 0, 4) === "RIFF" && buf.toString("ascii", 8, 12) === "WEBP") return "image/webp";
  return null;
}

// Read local image files → [{ name, media_type, data(base64) }]. Throws a
// readable message on a missing / oversize / unsupported file (surfaced to the model).
export function encodeImages(paths) {
  if (!paths || paths.length === 0) return [];
  if (paths.length > MAX_IMAGES) throw new Error(`too many images (${paths.length}); max is ${MAX_IMAGES}`);
  return paths.map((p) => {
    let buf;
    try { buf = readFileSync(p); } catch (e) { throw new Error(`cannot read image '${p}': ${e.message}`); }
    if (buf.length > MAX_IMAGE_BYTES) {
      throw new Error(`image '${p}' is ${(buf.length / 1e6).toFixed(1)} MB; max is ${MAX_IMAGE_BYTES / 1e6} MB`);
    }
    const media_type = detectMediaType(buf);
    if (!media_type) throw new Error(`'${p}' is not a supported image (PNG, JPEG, GIF, or WebP)`);
    return { name: basename(p), media_type, data: buf.toString("base64") };
  });
}

// Save received images to `dir` → [absolute paths]. Skips anything malformed.
export function saveImages(images, dir) {
  if (!images || images.length === 0) return [];
  mkdirSync(dir, { recursive: true });
  const out = [];
  images.forEach((img, i) => {
    if (!img || typeof img.data !== "string") return;
    const ext = EXT[img.media_type] || "";
    const safe = String(img.name || `image${i}`).replace(/[^\w.\-]/g, "_");
    const file = join(dir, `${i}_${safe}${safe.toLowerCase().endsWith(ext) ? "" : ext}`);
    try {
      writeFileSync(file, Buffer.from(img.data, "base64"));
      out.push(file);
    } catch {
      /* skip a bad image rather than fail the whole question */
    }
  });
  return out;
}
